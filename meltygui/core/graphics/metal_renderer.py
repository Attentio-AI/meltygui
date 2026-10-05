"""Melty's Metal command submission; owns resources, never an ImGui context.

Draw buffers come from the installed meltygui-imgui binding. The native host
copies them before this frame ends, so no second ImGui binary/context is linked.
All native methods are called on the host's render thread.
"""
from __future__ import annotations
import ctypes
import json
import hashlib
import struct


class MetalRenderer:
    max_texture_size = 16384

    def __init__(self, native):
        import meltygui_imgui as imgui
        if (imgui.VERTEX_SIZE, imgui.INDEX_SIZE, imgui.VERTEX_BUFFER_POS_OFFSET,
                imgui.VERTEX_BUFFER_UV_OFFSET, imgui.VERTEX_BUFFER_COL_OFFSET) != (20, 4, 0, 8, 16):
            raise RuntimeError("Metal requires the meltygui-imgui 20-byte vertex / 32-bit index ABI")
        self.native = native
        self.imgui = imgui
        self.io = imgui.get_io()
        self.programs = json.loads(native.program_manifest())
        self._textures = {}
        self._targets = {}
        self._font_texture = None
        self._palette = None
        self.scene_framebuffer = None
        self._has_overlay = False
        self._delete_queue = []
        self._zero = self.create_texture(1, 1, "rgba8", bytes(4))

    def create_texture(self, width, height, format="rgba16f", pixels=None):
        handle = self.native.create_texture(int(width), int(height), format, pixels)
        self._textures[handle] = (int(width), int(height), format)
        if pixels is None:
            self.native.clear_texture(handle, (0, 0, 0, 0))
        return handle

    def delete_texture(self, handle):
        if handle and handle in self._textures:
            self.native.delete_texture(handle)
            self._textures.pop(handle)

    def target(self, key, width, height, format="rgba16f"):
        previous = self._targets.get(key)
        if previous and self._textures[previous] == (int(width), int(height), format):
            return previous
        handle = self.create_texture(width, height, format)
        self._targets[key] = handle
        self.delete_texture(previous)
        return handle

    def begin_scene(self, width, height, clear_rgba):
        self.scene_framebuffer = self.target("scene", width, height)
        self.native.clear_texture(self.scene_framebuffer, tuple(clear_rgba))

    def end_scene(self):
        self.native.set_scene(self.scene_framebuffer)

    def ensure_palette(self, width):
        self._palette = self.target("palette", width, 1, "rgba32f")
        return self._palette

    def upload_palette(self, rgba_float32, count):
        self.native.upload_texture(self._palette, int(count), 1, rgba_float32)

    def refresh_font_texture(self):
        # Mobile text uses grayscale coverage. No desktop LCD subpixel ordering
        # is assumed; the same packed colors, contrast policy and gamma apply.
        width, height, pixels = self.io.fonts.get_tex_data_as_rgba32()
        old = self._font_texture
        self._font_texture = self.create_texture(width, height, "rgba8", pixels)
        self.io.fonts.texture_id = self._font_texture
        self.io.fonts.clear_tex_data()
        self.delete_texture(old)

    def draw(self, name, target, *, rect=None, uv=(1, 1, 0, 0), blend="replace", clip=None, **values):
        """Queue one built-in pass. Coordinates and UVs retain Melty's Y-up contract."""
        metadata = self.programs[name]
        slots = max((entry["slot"] for entry in metadata if "slot" in entry), default=-1) + 1
        params = [0.0] * (slots * 4)
        textures = []
        for entry in metadata:
            field = entry["name"]
            value = values.get(field, entry.get("default"))
            if value is None and field == "texture_size" and values.get("u_texture"):
                value = self._textures[values["u_texture"]][:2]
            if entry["type"] == "sampler2D":
                textures.append(value or self._zero)
            else:
                if value is None:
                    raise ValueError(f"Metal {name} requires uniform {field}")
                value = tuple(value) if isinstance(value, (tuple, list)) else (value,)
                index = entry["slot"] * 4
                params[index:index + len(value)] = value
        self.quad(target, name + "_fragment", struct.pack(f"{len(params)}f", *params),
                  rect=rect, uv=uv, textures=textures, blend=blend, clip=clip)

    def quad(self, target, shader, params=b"", *, rect=None, uv=(1, 1, 0, 0), textures=(), blend="replace", clip=None):
        width, height, _ = self._textures[target]
        self.native.quad(target, shader, params, rect or (0, 0, width, height), uv, tuple(textures), blend, clip)

    def clear_rect(self, target, rect, color=(0, 0, 0, 0)):
        self.quad(target, "solid_fragment", struct.pack("4f", *color), rect=rect)

    def copy(self, source, target, *, source_rect=None, target_rect=None, clip=None):
        w, h, _ = self._textures[source]
        x, y, cw, ch = source_rect or (0, 0, w, h)
        self.quad(target, "copy_fragment", textures=(source,), rect=target_rect,
                  uv=(cw / w, ch / h, x / w, y / h), clip=clip)

    def draw_backgrounds(self, rectangles, root):
        width, height, _ = self._textures[self.scene_framebuffer]
        self._context = self.target("style_context", width, height)
        self.native.clear_texture(self._context, tuple(root))
        if not rectangles:
            return
        sx, sy = self.io.display_fb_scale
        for index, entry in enumerate(rectangles):
            if entry is None:
                continue
            x, y, w, h, (cl, ct, cr, cb), radius = entry
            self.draw("style_context", self._context, rect=(x*sx, height-(y+h)*sy, w*sx, h*sy),
                      clip=(cl*sx, height-cb*sy, (cr-cl)*sx, (cb-ct)*sy), blend="over",
                      palette=self._palette, palette_index=index, rect_size=(w*sx,h*sy), radius=radius*sx)

    def begin_frame_split(self):
        from meltygui.core.runtime.toggles import Toggles
        if Toggles.dynamic_styles:
            from meltygui.core.styling.style import adjust_text_color
            source=adjust_text_color()
            if hashlib.sha256(source.encode()).hexdigest()!=self.programs.get("_text_policy_sha256"):
                raise NotImplementedError("Live GLSL text-policy changes require rebuilding the Metal shader library")
        self._has_overlay = self.imgui.get_foreground_draw_list().vtx_buffer_size > 0

    def _submit(self, draw_list, commands, target=None):
        from meltygui.core.runtime.toggles import Toggles
        vertices = ctypes.string_at(draw_list.vtx_buffer_data, draw_list.vtx_buffer_size * 20)
        indices = ctypes.string_at(draw_list.idx_buffer_data, draw_list.idx_buffer_size * 4)
        params = struct.pack("12f", *self.io.display_size, *self.io.display_fb_scale,
                             float(Toggles.HDR.vertex_range), float(Toggles.HDR.vertex_octaves),
                             2.0**float(Toggles.HDR.text_max_stops), 0,
                             float(Toggles.dynamic_styles), float(Toggles.dynamic_text_contrast),
                             float(Toggles.Fonts.text_gamma), -1)
        self.native.mesh(target or self.scene_framebuffer, vertices, indices, commands, params,
                         self._font_texture, self._context, self._occlusion)

    def _commands(self, draw_list, ranges=None):
        sx, sy = self.io.display_fb_scale
        offset = 0
        for command in draw_list.commands:
            end = offset + command.elem_count
            x, y, right, bottom = command.clip_rect
            for channel, first, last in ranges or [(-1, offset, end)]:
                lo, hi = max(offset, first), min(end, last)
                if hi > lo:
                    yield (command.texture_id, lo, hi-lo, x*sx, y*sy, right*sx, bottom*sy, channel)
            offset = end

    def render_except_overlay(self, draw_data):
        width, height, _ = self._textures[self.scene_framebuffer]
        self._occlusion = self.target("overlay_occlusion", width, height, "r16")
        self.native.clear_texture(self._occlusion, (0, 0, 0, 0))
        if not hasattr(self, "_context"):
            self._context = self.target("style_context", width, height)
        lists = draw_data.commands_lists
        for draw_list in lists[:-1] if self._has_overlay and lists else lists:
            self._submit(draw_list, list(self._commands(draw_list)))

    def render_overlay_only(self, draw_data):
        if not self._has_overlay or not draw_data.commands_lists:
            return
        from meltygui.core.melty import Melty
        sx, sy = self.io.display_fb_scale
        height = self._textures[self.scene_framebuffer][1]
        seen = set()
        windows = [getattr(w, "draw_state", None) for w in Melty.registered_windows.values()]
        windows += [ds for roots in Melty.root_draw_states.values() for ds in roots]
        for ds in windows:
            if ds is None or id(ds) in seen or ds.closed or getattr(ds, "_hidden_offscreen", False):
                continue
            seen.add(id(ds))
            if None in (ds.abs_left,ds.abs_top,ds.width,ds.height):
                continue
            channel = Melty.overlay_channel_for(ds) / Melty.max_layer
            self.draw("mask", self._occlusion, rect=(ds.abs_left*sx,height-(ds.abs_top+ds.height)*sy,ds.width*sx,ds.height*sy),
                      uRankNorm=channel, blend="max")
        ranges = getattr(Melty, "_overlay_channel_ranges", None)
        if ranges:
            ranges = [(channel / Melty.max_layer, start, end) for channel,start,end in ranges]
        draw_list = draw_data.commands_lists[-1]
        self._submit(draw_list, list(self._commands(draw_list,ranges)))
        self._has_overlay = False

    def compose_scene(self, cache):
        """Apply the same built-in linear/HDR filters as Melty.post_frame."""
        from meltygui.core.melty import Melty
        from meltygui.core.runtime.toggles import Toggles
        w,h,_=self._textures[self.scene_framebuffer]
        scratch=self.target("scene_filter",w,h)
        if Toggles.filter_brightness:
            self.draw("brightness_contrast",scratch,u_texture=self.scene_framebuffer,
                      brightness=Toggles.brightness,contrast=Toggles.contrast)
            self.copy(scratch,self.scene_framebuffer)
        if Toggles.filters:
            layers=100./((Melty.max_layer-1.)*(Melty.max_depth-1.))
            down=max(1,int(Toggles.shadow_downscale))
            size=(max(1,w//down),max(1,h//down))
            shadow=self.target("shadow_cast",*size)
            self.draw("shadow_cast",shadow,u_texture=cache.full_mask_tex,
                max_steps=layers*65535./2.,depth_scale=1./layers,
                light_dir=tuple(Toggles.shadow_light_dir),height_scale=Toggles.shadow_height_scale,
                blur_scale=Toggles.shadow_blur_scale,blur_exponent=Toggles.shadow_blur_exponent,
                blur_samples=max(1,int(Toggles.shadow_blur_samples)),hit_strength=Toggles.shadow_hit_strength,
                hit_falloff=Toggles.shadow_hit_falloff,shadow_strength=Toggles.shadow_strength)
            if not Toggles.draw_legacy:
                glow=cache.glow_tex if cache.glow_active and Toggles.glow else None
                self.draw("shadow_composite",scratch,u_texture=self.scene_framebuffer,
                    shadow_map=shadow,frame_origin=(0,0),frame_radius=0,frame_size=(w,h),
                    depth_map=cache.full_mask_tex,depth_scale=1./layers,
                    shadow_opacity=Toggles.shadow_opacity,shadow_color=tuple(Toggles.shadow_color),
                    shadow_size=size,depth_sharpness=Toggles.shadow_edge_sharpness,
                    glow_map=glow or shadow,glow_strength=Toggles.glow_strength if glow else 0,
                    glow_shadow_cut=Toggles.glow_shadow_cut,light_dir=tuple(Toggles.shadow_light_dir),
                    specular_bevel=Toggles.specular_bevel,specular_roughness=Toggles.specular_roughness,
                    specular_strength=Toggles.specular_opacity,specular_fade=Toggles.specular_fade,
                    specular_fade_rel=Toggles.specular_fade_rel,
                    win_mask=getattr(cache,"_win_mask_tex",None),win_rects=getattr(cache,"_win_rects_tex",None),
                    specular_depth_falloff=Toggles.specular_depth_falloff,specular_slope_tol=Toggles.specular_slope_tol)
                self.copy(scratch,self.scene_framebuffer)
        if Toggles.glow_debug_view and cache.glow_tex is not None:
            self.copy(cache.glow_tex,self.scene_framebuffer)

    def create_tile_cache(self):
        from meltygui.core.cache.tile_cache import TileCacheMasked
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        return TileCacheMasked(gpu=MetalTileBackend(self))

    def flush_deletes(self):
        from meltygui.core.graphics.gl_state import GLState
        return GLState.flush_deletes()

    def on_window_deleted(self, ds):
        from meltygui.core.graphics.gl_state import GLState
        GLState.on_window_deleted(ds)

    def shutdown(self):
        for handle in list(self._textures):
            self.delete_texture(handle)
