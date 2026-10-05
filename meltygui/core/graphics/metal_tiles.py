"""Metal execution for TileCacheMasked's retained CPU model.

Tile identities, invalidation, input/overlay ownership, subtree ordering and mark
retention stay in TileCacheMasked. This module replaces its framebuffer passes;
all coordinates/UVs keep the desktop cache's top-anchored, Y-up contract.
"""
from __future__ import annotations
from collections import defaultdict
from math import ceil, floor, radians, tan
import struct

from meltygui.core.cache.tile_cache import (
    Tile, _bucket, _bucket_h, _bump_note, _tile_alloc, _tile_pixel_size,
    _tile_uv_rect, _pixel_rect, _full_mask_rects, INV_65535,
)
from meltygui.core.cache.tile_marks import snap_int
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.state.new_core_model import TileMode


def integer_rect(bounds):
    x0, y0, x1, y1 = bounds
    x, y = floor(x0), floor(y0)
    return x, y, max(0, ceil(x1)-x), max(0, ceil(y1)-y)


def intersection(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a[0],b[0]), max(a[1],b[1]), min(a[2],b[2]), min(a[3],b[3])


class MetalTileBackend:
    def __init__(self, renderer):
        self.renderer = renderer
        self.mask_target = None
        self.targets = {}

    def target(self, name, w, h, format="r16"):
        handle = self.renderer.target((id(self), name), w, h, format)
        self.targets[name] = handle
        return handle

    def begin_masks(self, cache, w, h):
        if w <= 0 or h <= 0:
            return
        for name in ("mask", "sub_mask", "full_sub_mask", "full_mask"):
            handle = self.target(name, w, h)
            setattr(cache, "_"+name+"_tex", handle)
            setattr(cache, "_"+name+"_fbo", handle)
        cache.snapshot_tex = self.target("snapshot", w, h, "rgba16f")
        cache._snapshot_fbo = cache.snapshot_tex
        cache._fb_size = cache._fb_alloc_size = (w, h)

    def delete_tile(self, tile):
        self.renderer.delete_texture(tile.tex)
        self.renderer.delete_texture(tile.mask_tex)

    def cleanup(self, cache):
        for tile in list(cache._tiles.values()):
            self.delete_tile(tile)
        cache._tiles.clear()
        for name, handle in self.targets.items():
            self.renderer.delete_texture(handle)
            self.renderer._targets.pop((id(self),name),None)
        self.targets.clear()
        for name in ("mask", "sub_mask", "full_sub_mask", "full_mask", "snapshot", "glow", "win_mask"):
            setattr(cache, "_"+name+"_fbo", None)
            setattr(cache, "_"+name+"_tex", None)
        cache.snapshot_tex = None
        cache._win_rects_tex = None
        cache._win_mask_size = None
        cache._win_z_by_ds = {}
        cache._glow_size = None
        cache._glow_tex_empty = True
        cache._mask_sig_prev = None
        cache._fb_size = cache._fb_alloc_size = (0,0)
        self.mask_target = None

    def clear_bands(self, tile, bands):
        for rect in bands:
            if rect[2] > 0 and rect[3] > 0:
                self.renderer.clear_rect(tile.tex, rect)
                self.renderer.clear_rect(tile.mask_tex, rect)

    def ensure_tile(self, existing, w, h, frame_id, draw_state, pixel_scale):
        w, h = int(w or 0), int(h or 0)
        if w <= 0 or h <= 0:
            return None
        same_scale = existing is not None and existing.pixel_scale == pixel_scale
        if same_scale and existing.size == (w,h):
            return existing
        aw, ah = _bucket(w), _bucket_h(h)
        frozen = bool(getattr(draw_state,"freeze_resize",False))
        if frozen and existing:
            eaw,eah = _tile_alloc(existing)
            aw,ah = max(aw,snap_int(eaw)),max(ah,snap_int(eah))
        if same_scale and _tile_alloc(existing) == (aw,ah):
            ow,oh = map(snap_int,existing.size)
            if not frozen:
                bands=[]
                if h>oh: bands.append((0,ah-h,w,h-oh))
                if w>ow: bands.append((ow,ah-h,w-ow,h))
                self.clear_bands(existing,[_pixel_rect(b,pixel_scale) for b in bands])
            else:
                cs=existing.content_size or (ow,oh)
                existing.content_size=(min(aw,max(cs[0],w)),min(ah,max(cs[1],h)))
                if w<ow or h<oh:
                    existing.content_scroll=tuple(map(snap_int,getattr(draw_state,"scroll_offset",None) or (0,0)))
                    existing.content_bg=False
            existing.size,existing.alloc_size=(w,h),(aw,ah)
            if existing.filled_bbox is not None:
                l,t,r,b=existing.filled_bbox
                r,b=min(r,w),min(b,h)
                existing.filled_bbox=(l,t,r,b) if r>l and b>t else None
            _bump_note(existing,"in-bucket-resize")
            existing.last_invalidated_frame=max(existing.last_invalidated_frame,frame_id+1)
            request_render()
            return existing
        pw,ph=_pixel_rect((0,0,aw,ah),pixel_scale)[2:]
        tex=self.renderer.create_texture(pw,ph)
        mask=self.renderer.create_texture(pw,ph,"r16")
        tile=Tile(draw_state,tex,tex,mask,None,(w,h),(aw,ah),pixel_scale)
        if existing:
            ow,oh=map(snap_int,existing.size)
            cs=(existing.content_size or (ow,oh)) if frozen else (ow,oh)
            cw,ch=min(cs[0],aw if frozen else w),min(cs[1],ah if frozen else h)
            if cw>0 and ch>0:
                self.renderer.copy(existing.tex,tex,
                    source_rect=_pixel_rect((0,_tile_alloc(existing)[1]-ch,cw,ch),existing.pixel_scale),
                    target_rect=_pixel_rect((0,ah-ch,cw,ch),pixel_scale))
            cw,ch=min(ow,w),min(oh,h)
            tile.filled_bbox=(0,0,cw,ch) if cw>0 and ch>0 else None
            if frozen:
                tile.content_size=(min(aw,max(cs[0],w)),min(ah,max(cs[1],h)))
                tile.content_scroll=existing.content_scroll
                tile.content_bg=existing.content_bg
            self.delete_tile(existing)
        _bump_note(tile,"tile-recreate")
        tile.last_invalidated_frame=max(tile.last_invalidated_frame,frame_id+1)
        request_render()
        return tile

    @staticmethod
    def blend(mark):
        mode=getattr(mark.draw_state,"tile_mode",None)
        return "min" if mode==TileMode.MIN else "max" if mode==TileMode.MAX else "replace"

    def rounded(self, target, rect, rank, radius=0, margin=0, clip=None, blend="replace"):
        if rect[2]<=0 or rect[3]<=0:
            return
        if radius > 0:
            self.renderer.draw("mask_rounded",target,rect=rect,clip=clip,blend=blend,
                uRankNorm=rank,uRectSize=rect[2:],uCornerRadius=radius,uMargin=margin)
        else:
            self.renderer.draw("mask",target,rect=rect,clip=clip,blend=blend,uRankNorm=rank)

    def fresh(self, cache, mark, target, xform, *, rank, blend="replace", clip=None):
        dp_x,dp_y,sx,sy,fw,fh=xform
        rect=integer_rect(cache._screen_rect_to_fb_xyxy(mark.x,mark.y,mark.w,mark.h,dp_x,dp_y,sx,sy,fh))
        self.rounded(target,rect,rank,mark.corner_radius*sx,clip=clip,blend=blend)

    def full_mark(self, cache, mark, target, xform, exclude_key=None):
        dp_x,dp_y,sx,sy,fw,fh=xform
        ds=cache.key_to_draw_state.get(mark.key)
        tile=cache._tiles.get(mark.key)
        resize=bool(ds is not None and ds.size_change)
        frozen=bool(resize and tile and tile.mask_tex and getattr(ds,"freeze_resize",False))
        cached=bool(tile and tile.mask_tex and mark.key!=exclude_key and (not resize or frozen))
        clip=integer_rect(cache._screen_rect_to_fb_xyxy(mark.x,mark.y,mark.w,mark.h,dp_x,dp_y,sx,sy,fh))
        use_live=(ds is not None and ds.width is not None and ds.height is not None
                  and (exclude_key is not None or ((cached or resize) and mark.key in cache._key_to_ctx)))
        if use_live:
            w,h=tile.size if frozen else (ds.width,ds.height)
            rect=integer_rect(cache._screen_rect_to_fb_xyxy(ds.abs_left,ds.abs_top,w,h,dp_x,dp_y,sx,sy,fh))
        else:
            rect=clip
        if min(rect[2:]+clip[2:])<=0:
            return
        margin=getattr(mark.draw_state,"shadow_margin",0)*sx
        if cached:
            self.renderer.draw("mask_offset_rounded",target,rect=rect,clip=clip,
                uTex=tile.mask_tex,uOffset=(mark.depth_and_layer-tile.mask_layer)*INV_65535,
                uRectSize=rect[2:],uCornerRadius=mark.corner_radius*sx,uMargin=margin,
                uUVRect=_tile_uv_rect(tile))
        else:
            # Full mask uses the clipped fresh geometry, tile mask uses live
            # geometry clipped by the recorded subtree rect, as desktop does.
            if exclude_key is None: rect=clip
            self.rounded(target,rect,mark.depth_and_layer/65535.5,mark.corner_radius*sx,margin,clip)

    def build_windows(self, cache, xform):
        dp_x,dp_y,sx,sy,fw,fh=xform
        tex=self.target("win_mask",fw,fh)
        cache._win_mask_tex=cache._win_mask_fbo=tex
        cache._win_mask_size=(fw,fh)
        cache._win_z_by_ds={}
        self.renderer.native.clear_texture(tex,(0,0,0,0))
        rows=[]
        z=0
        for ds in getattr(Melty,"paint_ordered_ds",None) or ():
            if (ds is None or ds.abs_closed or ds.closed or getattr(ds,"_hidden_offscreen",False)
                    or ds.width is None or ds.height is None or ds.width<=0 or ds.height<=0):
                continue
            z+=1
            cache._win_z_by_ds[id(ds)]=z/1024
            bounds=cache._screen_rect_to_fb_xyxy(ds.abs_left,ds.abs_top,ds.width,ds.height,dp_x,dp_y,sx,sy,fh)
            if len(rows)<256: rows.append(bounds)
            self.rounded(tex,integer_rect(bounds),z/1024,max(0,float(getattr(ds,"corner_radius",6) or 0))*sx)
        cache._win_rects_tex=self.target("win_rects",256,1,"rgba32f")
        values=[v for row in rows for v in row]+[0.]*(4*(256-len(rows)))
        self.renderer.native.upload_texture(cache._win_rects_tex,256,1,struct.pack("1024f",*values))

    def stamp_shadows(self, cache, shadows, dp_x, dp_y, sx, sy, fh, scissor_fb=None, win_gate=True):
        fw=cache._fb_size[0]
        winmask=getattr(cache,"_win_mask_tex",None) or self.renderer._zero
        for mark in shadows:
            x,y,w,h,ranks,radius,margin,clip,owner,inset=mark[:10]
            bounds=cache._screen_rect_to_fb_xyxy(x,y,w,h,dp_x,dp_y,sx,sy,fh)
            rect=integer_rect(bounds)
            if min(rect[2:])<=0: continue
            sc=scissor_fb
            if clip is not None:
                sc=intersection(sc,cache._screen_rect_to_fb_xyxy(clip[0],clip[1],clip[2]-clip[0],clip[3]-clip[1],dp_x,dp_y,sx,sy,fh))
            sc=integer_rect(sc) if sc is not None else None
            if sc is not None and min(sc[2:])<=0: continue
            z=cache._win_z_for_owner(owner) if win_gate else 1.
            blend="min" if inset else "max"
            shape=mark[10] if len(mark)>10 else None
            if shape is not None:
                vertices=[v for px,py,rank in shape for v in ((px-dp_x)*sx,fh-(py-dp_y)*sy,rank/65535.5)]
                self.renderer.native.shape(self.mask_target,struct.pack(f"{len(vertices)}f",*vertices),winmask,z,blend,sc)
            else:
                self.renderer.draw("shadow_gradient",self.mask_target,rect=rect,clip=sc,blend=blend,
                    uRankCorners=tuple(r/65535.5 for r in ranks),uRectSize=rect[2:],
                    uCornerRadius=max(0,radius)*sx,uMargin=margin*sx,uWinMask=winmask,uWinZ=z,uFBSize=(fw,fh))

    def stamp_glows(self, cache, glows, dp_x, dp_y, sx, sy, fw, fh):
        down=max(1,int(Toggles.glow_downscale))
        gw,gh=max(1,fw//down),max(1,fh//down)
        tex=self.target("glow",gw,gh,"rgba16f")
        cache._glow_tex=cache._glow_fbo=tex
        cache._glow_size=(gw,gh)
        cache._glow_tex_empty=not glows
        self.renderer.native.clear_texture(tex,(0,0,0,0))
        gx,gy=gw/max(1.,fw),gh/max(1.,fh)
        tangent=tan(radians(min(80,max(-80,float(Toggles.glow_area_angle)))))
        multiplier=max(1.,1.+abs(tangent)+Toggles.glow_area_spread+Toggles.glow_area_edge_blur) if Toggles.glow_area_light else 1.
        bias=2./65535.5
        for (x,y,w,h,rgb,intensity,_,__,radius,falloff,cr,clip),delta,rank,floor_rank,live,win_z in glows:
            x,y=x+delta[0],y+delta[1]
            bounds=(x,y,x+w,y+h)
            if clip is not None: bounds=intersection(bounds,tuple(v+delta[i%2] for i,v in enumerate(clip)))
            bounds=intersection(bounds,live)
            x,y,right,bottom=bounds
            w,h=right-x,bottom-y
            if w<=0 or h<=0: continue
            expand=radius*multiplier
            b=cache._screen_rect_to_fb_xyxy(x-expand,y-expand,w+expand*2,h+expand*2,dp_x,dp_y,sx,sy,fh)
            rect=integer_rect((b[0]*gx,b[1]*gy,b[2]*gx,b[3]*gy))
            if min(rect[2:])<=0: continue
            debug=Toggles.glow_debug_no_mask
            self.renderer.draw("glow",tex,rect=rect,blend="glow",
                uColor=(*rgb,intensity),uRankLo=0 if debug else max(0,floor_rank-bias),
                uRankHi=1 if debug else rank+bias,uWinZ=1 if debug else win_z,
                uDepthMask=cache._full_mask_tex,uWinMask=getattr(cache,"_win_mask_tex",None),uGlowSize=(gw,gh),
                uRectSize=rect[2:],uCornerRadius=max(0,cr*sx*gx),uRadius=max(1,radius*sx*gx),
                uExpand=max(1,expand*sx*gx),uFalloff=max(0,falloff),
                uAreaLight=bool(Toggles.glow_area_light),uAreaHold=min(.999,max(0,Toggles.glow_area_hold)),
                uAreaSpread=max(0,Toggles.glow_area_spread),uAreaFalloff=max(.01,Toggles.glow_area_falloff),
                uAreaEdgeBlur=max(0,Toggles.glow_area_edge_blur),uAreaTanA=tangent,
                uAreaTopEdge=bool(Toggles.glow_area_top_edge),uAreaEdges=bool(Toggles.glow_area_edges),
                uDebugSolid=bool(Toggles.glow_debug_rects))

    def finalize(self, cache, framebuffer_size):
        cache.all_keys=set()
        cache.last_capture_stats=(0,0,0)
        if cache._snapshot_fbo is None:
            return
        if not (cache._pending or cache._mask_rects or cache._shadow_rects or getattr(cache,"_glow_rects",None)):
            return
        pending=list(cache._pending)
        marks=list(reversed(cache._mask_rects))
        cache.last_capture_stats=(len(pending),sum(p.size[0]*p.size[1] for p in pending),len(marks))
        xform=cache._get_draw_xform()
        dp_x,dp_y,sx,sy,fw,fh=xform
        if cache._fb_size!=(fw,fh):
            self.begin_masks(cache,fw,fh)
        subtree=defaultdict(list)
        for mark in marks:
            key=mark.key
            seen=set()
            while key is not None and mark.draw_state is not None and key not in seen:
                seen.add(key)
                subtree[key].append(mark)
                key=None if mark.draw_state.closable else cache.key_to_parent_key.get(key)
        r=self.renderer
        try:
            if pending:
                r.copy(r.scene_framebuffer,cache.snapshot_tex)
                r.native.clear_texture(cache._mask_tex,(0,0,0,0))
                for mark in marks:
                    self.fresh(cache,mark,cache._mask_tex,xform,rank=mark.layer*INV_65535,blend=self.blend(mark))
                for capture in reversed(pending):
                    bounds=cache._screen_rect_to_fb_xyxy(*capture.pos,*capture.size,dp_x,dp_y,sx,sy,fh)
                    clip=integer_rect(bounds)
                    r.clear_rect(cache._sub_mask_tex,clip)
                    for mark in subtree.get(capture.key,()):
                        self.fresh(cache,mark,cache._sub_mask_tex,xform,rank=mark.layer*INV_65535,blend=self.blend(mark),clip=clip)
                    tile=capture.tile
                    if tile is None: continue
                    lw,lh=_tile_pixel_size(tile)
                    _,ah=_tile_pixel_size(tile,allocated=True)
                    r.draw("tile_copy",tile.tex,rect=(0,ah-lh,lw,lh),
                        uSrc=cache.snapshot_tex,uTopMask=cache._mask_tex,uSubMask=cache._sub_mask_tex,
                        uFBSize=cache._fb_alloc_size,uSrcRectPx=bounds,uDebugScale=cache.offscreen_scale,
                        uCopyDebugMode=cache._copy_debug_mode_to_int(),uTint=(1,1,1,1))
                    tile.last_clean_frame=cache._frame_id
                    tile.dirty=cache._is_dirty(tile)
                    cache._accumulate_filled(tile,capture.draw_state,on_screen=cache._on_screen_tile_rect(*capture.pos,*capture.size))
            rebuild,signature=cache._mask_rebuild(subtree,pending,fw,fh,dp_x,dp_y,sx,sy)
            if rebuild:
                for capture in pending:
                    bounds=cache._screen_rect_to_fb_xyxy(*capture.pos,*capture.size,dp_x,dp_y,sx,sy,fh)
                    r.clear_rect(cache._full_sub_mask_tex,integer_rect(bounds))
                    for mark in subtree.get(capture.key,()):
                        self.full_mark(cache,mark,cache._full_sub_mask_tex,xform,exclude_key=capture.key)
                    self.mask_target=cache._full_sub_mask_tex
                    owned=cache._shadows_owned_by(capture.key)
                    if owned:
                        self.stamp_shadows(cache,owned,dp_x,dp_y,sx,sy,fh,scissor_fb=bounds,win_gate=False)
                    tile=capture.tile
                    if tile is not None and tile.mask_tex is not None:
                        lw,lh=_tile_pixel_size(tile)
                        _,ah=_tile_pixel_size(tile,allocated=True)
                        x,y,x1,y1=bounds
                        r.copy(cache._full_sub_mask_tex,tile.mask_tex,source_rect=(x,y,x1-x,y1-y),target_rect=(0,ah-lh,lw,lh))
                        tile.mask_layer=capture.depth_and_layer
                        cb=capture.draw_state.clipped_by_rect if capture.draw_state else None
                        tile.mask_clip_insets=tuple(cb) if cb is not None else (0,0,0,0)
                r.native.clear_texture(cache._full_mask_tex,(0,0,0,0))
                for mark in _full_mask_rects(subtree):
                    self.full_mark(cache,mark,cache._full_mask_tex,xform)
                self.build_windows(cache,xform)
                self.mask_target=cache._full_mask_tex
                standalone=[]
                for mark in cache._shadow_rects:
                    if mark[9] and mark[8] is not None:
                        tile=cache._tiles.get(mark[8]); ds=cache.key_to_draw_state.get(mark[8])
                        if tile and tile.mask_tex and not (ds is not None and ds.size_change and not getattr(ds,"freeze_resize",False)):
                            continue
                    standalone.append(mark)
                self.stamp_shadows(cache,standalone,dp_x,dp_y,sx,sy,fh)
                cache._finalize_retained_marks(pending,dp_x,dp_y,sx,sy,fw,fh,[])
                cache._mask_sig_prev=signature
        finally:
            cache._detect_occluder_changes(cache._mask_rects)
            for field in ("_pending","_mask_rects","_shadow_rects","_glow_rects","_glow_cleared", "_depth_frame",
                          "_depth_cleared","_emit_counts","_enq_mask_keys","_enq_copy_keys","_cancelled_keys",
                          "did_deviate","seen_ids"):
                collection=getattr(cache,field,None)
                if collection is not None: collection.clear()
            cache._recording=False
