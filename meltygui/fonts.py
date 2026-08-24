import ctypes
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

import imgui
import numpy as np

from src.lsd.gl_gui.model.model_enums import RelaxedEnum

_RESOURCES = Path(__file__).parent / "resources"
_DEJAVU_SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Font Awesome 5+ private use range; trailing 0 terminates the imgui's list.
_FA_ICON_RANGE: Tuple[int, ...] = (0xF000, 0xFFFF, 0)

# Glyph ranges for a monospace terminal font: Latin plus the box-drawing, block,
# shape, arrow, and dingbat ranges that TUIs (Crude Code, vim, ...) draw their
# borders/spinners with. Specifying ranges replaces imgui's ASCII-only default, so
# Latin (0x0020-0x00FF) is listed first. JetBrains Mono covers all of these. Trailing
# 0 terminates the list.
_MONO_TUI_RANGE: Tuple[int, ...] = (
    0x0020, 0x00FF,  # ASCII Latin + Latin-1 Supplement
    0x2010, 0x2027,  # general punctuation (dashes, smart quotes, ellipsis)
    0x2190, 0x21FF,  # arrows
    0x2500, 0x257F,  # box drawing
    0x2580, 0x259F,  # block elements
    0x25A0, 0x25FF,  # geometric shapes (○ ◯ □ ▪)
    0x2600, 0x27BF,  # misc symbols + dingbats (  )
    0x2B00, 0x2BFF,  # misc symbols and arrows
    0,
)


# Default glyph ranges for every non-merged font. imgui's own default is
# Latin-only (0x0020-0x00FF), which leaves U+2026 "..." (elisions, "dot..."),
# en/em dashes, smart quotes, arrows and the geometric bullets rendering
# as "?" all over the app. DejaVu Sans and JetBrains Mono cover all of
# these; the atlas grows by a few hundred glyphs per font. Trailing 0
# terminates the list.
_UI_RANGE: Tuple[int, ...] = (
    0x0020, 0x00FF,  # Basic Latin + Latin-1 Supplement
    0x2010, 0x2027,  # general punctuation (dashes, smart quotes, ellipsis)
    0x2190, 0x21FF,  # arrows
    0x25A0, 0x25FF,  # geometric shapes (● ◯ ▶ ▪)
    0x2600, 0x27BF,  # misc symbols + dingbats (  )
    0,
)


@dataclass(frozen=True)
class FontSpec:
    path: str
    size: float
    merge: bool = False
    glyph_ranges: Optional[Tuple[int, ...]] = None
    extra_spacing: float = 0.0
    # HORIZONTAL oversampling (vertical is always 1, see prewarm). MUST stay
    # 3: the renderer's LCD text shader (split_overlay_renderer.py) reads
    # the atlas as a subpixel bitmap - every glyph quad spans exactly
    # `oversample` atlas texels per sub width, and the shader samples at
    # -1/0/+1 texels for the R/G/B subpixel coverages. stb's box prefilter
    # over those 3 texels is FreeType's 'light' LCD filter. Any other value
    # puts the R/B samples off the subpixel centres.
    oversample: int = 3
    # Replace stb's raw bitmaps with FreeType auto-hinted LCD renders
    # (see FontManager.hint_atlas). Off for decorative sizes where hinting
    # buys nothing; sizes above HINT_MAX_SIZE px are skipped regardless.
    hint: bool = True
    # Baked at boot by prewarm(). Everything else lazy-loads: the first
    # get() queues the font and Melty.apply_ui_scale bakes it between
    # frames. Keep this limited to what the FIRST FRAME needs - every eager
    # font is baked inside boot's critical path (stb rasterize + FreeType
    # hint; the full 17-entry atlas cost 230ms).
    eager: bool = False


_JETBRAINS_MONO = str(_RESOURCES / "JetBrainsMono-Regular.ttf")


def _fa_merge(size: float, eager: bool = False) -> FontSpec:
    """FontAwesome icon spec to fold into the preceding base font.

    Sized at ~0.78x the host glyph size (matching the 14/18 ratio of the
    DejaVu UI font) so merged icons sit at a comparable cap height.
    """
    return FontSpec(
        str(_RESOURCES / "fontawesome-webfont.ttf"),
        size,
        merge=True,
        glyph_ranges=_FA_ICON_RANGE,
        extra_spacing=2.0,
        eager=eager,
    )


class Font(RelaxedEnum):
    # Order matters: a `merge=True` font is folded into the most recently
    # added non-merged font, so primary fonts must come before their merges.
    # eager=True marks the first-frame fonts (imgui's main font - the first
    # entry - and the editor face); everything else bakes on first get().
    DEJAVU_SANS_18 = FontSpec(_DEJAVU_SANS, 18.0, eager=True)
    FONTAWESOME_14 = _fa_merge(14.0, eager=True)
    DEJAVU_SANS_50 = FontSpec(_DEJAVU_SANS, 50.0)
    # DEJAVU_SANS_22 = FontSpec(_DEJAVU_SANS, 22.0)

    JETBRAINS_MONO_30 = FontSpec(_JETBRAINS_MONO, 30.0)
    JETBRAINS_MONO_13 = FontSpec(_JETBRAINS_MONO, 13.0)
    JETBRAINS_MONO_14 = FontSpec(_JETBRAINS_MONO, 14.0)
    JETBRAINS_MONO_15 = FontSpec(_JETBRAINS_MONO, 15.0)
    JETBRAINS_MONO_16 = FontSpec(_JETBRAINS_MONO, 16.0, glyph_ranges=_MONO_TUI_RANGE)
    JETBRAINS_MONO_18 = FontSpec(_JETBRAINS_MONO, 18.0)
    JETBRAINS_MONO_19 = FontSpec(_JETBRAINS_MONO, 18.5, glyph_ranges=_MONO_TUI_RANGE,
                                 eager=True)
    FONTAWESOME_MONO_19 = _fa_merge(16.0, eager=True)

    JETBRAINS_MONO_20 = FontSpec(_JETBRAINS_MONO, 20.0)
    JETBRAINS_MONO_22 = FontSpec(_JETBRAINS_MONO, 22.0)
    JETBRAINS_MONO_40 = FontSpec(_JETBRAINS_MONO, 40.0)
    FONTAWESOME_MONO_40 = _fa_merge(31.0)
    JETBRAINS_MONO_50 = FontSpec(_JETBRAINS_MONO, 50.0)
    FONTAWESOME_MONO_50 = _fa_merge(39.0)


# imgui folds a merge=True spec into the most recently added non-merged font,
# so a base font and its contiguous merge entries must always bake TOGETHER
# and in enum order. The lazy-load unit is therefore a GROUP - get() on any
# member queues the whole group.
def _build_groups():
    groups = []
    for font in Font:
        if font.value.merge and groups:
            groups[-1].append(font)
        else:
            groups.append([font])
    return {font: tuple(group) for group in groups for font in group}


_GROUP_OF = _build_groups()


def detect_auto_scale(window=None) -> float:
    """dp scale for the monitor the OS window currently sits on: 1.5 on
    4k-and-larger panels (video mode at/above 3840 wide or 2160 tall, so a
    portrait 4k still counts), 1.0 otherwise. The window's monitor is found
    by which video mode contains the window's center; falls back to the
    primary monitor when the window is None or sits off every monitor
    (mid-drag between screens). 1.0 on any glfw failure."""
    try:
        import glfw
        target = None
        if window is not None:
            wx, wy = glfw.get_window_pos(window)
            ww, wh = glfw.get_window_size(window)
            cx, cy = wx + ww / 2, wy + wh / 2
            for m in glfw.get_monitors():
                mx, my = glfw.get_monitor_pos(m)
                mode = glfw.get_video_mode(m)
                if (mx <= cx < mx + mode.size.width
                        and my <= cy < my + mode.size.height):
                    target = m
                    break
        if target is None:
            target = glfw.get_primary_monitor()
        mode = glfw.get_video_mode(target)
        return 1.5 if (mode.size.width >= 3840 or mode.size.height >= 2160) else 1.0
    except Exception:
        return 1.0


# Fonts baked larger than this (px, after UI scale) keep stb's bitmaps:
# hinting is a small-size legibility aid and the FreeType hint costs ~1 ms
# per glyph row-set, so the 40/50 px display faces are not worth it.
HINT_MAX_SIZE = 32.0


def _expand_ranges(ranges: Tuple[int, ...]):
    """imgui glyph-range list (lo, hi, lo, hi, ..., 0) -> codepoints."""
    out = []
    it = iter(ranges)
    for lo in it:
        if lo == 0:
            break
        hi = next(it, 0)
        out.extend(range(lo, hi + 1))
    return out


class FontManager:
    # no external change
    """Owns the imgui font atlas: one handle per Font enum entry, all baked
    at `scale` x their authored pixel size.

    The scale is baked into the RASTERIZED size rather than applied as a
    draw-time multiplier (io.font_global_scale) so glyphs stay sharp — a
    scaled atlas is resampled text, which is exactly the soft/aliased look
    the whole-interface zoom had. The cost is that changing the scale means
    re-rasterizing every font (see `rebuild`), so it happens only when the
    UIScale value actually moves.
    """

    # Floor on a baked glyph size: sub-pixel-ish sizes rasterize to mush and
    # can make imgui's atlas build fail outright.
    MIN_SIZE = 4.0

    def __init__(self, io, scale: float = 1.0):
        self.io = io
        self.scale = float(scale)
        self._handles: dict = {}
        # Fonts baked into the current atlas (load failures included, stamped
        # None in _handles so they never re-queue) / fonts get() queued for
        # the next between-frames bake (flush_pending).
        self._loaded: set = set()
        self._pending: set = set()

    def rebuild(self, scale: float, impl=None) -> bool:
        """Re-bake every LOADED font at `scale` and hand the new atlas to the
        renderer. No-op (False) when the scale is unchanged.

        MUST run between frames — outside imgui.new_frame()/render() — since
        it clears the atlas the in-flight draw data would reference. Every
        previously returned handle is dangling afterwards, so callers that
        cached one (Melty.large_font, LSDStudio.fa_font) have to re-`get` it.
        """
        scale = float(scale)
        if scale == self.scale and self._handles:
            return False
        self.scale = scale
        self._loaded |= self._pending
        self._pending.clear()
        self._bake(self._loaded)
        if impl is not None:
            impl.refresh_font_texture()
        return True

    def flush_pending(self, impl=None) -> bool:
        """Bake every font get() queued since the last flush — one atlas
        re-bake covers everything the previous frame touched. No-op (False)
        when nothing is pending. Same between-frames contract and same
        dangling-handle consequence as rebuild; Melty.apply_ui_scale is the
        call site and does the handle re-gets + tile invalidation."""
        if not self._pending:
            return False
        self._loaded |= self._pending
        self._pending.clear()
        self._bake(self._loaded)
        if impl is not None:
            impl.refresh_font_texture()
        return True

    def _oversample(self, spec: FontSpec) -> int:
        """Horizontal oversampling for a spec — the authored value at EVERY
        scale. It used to walk down as the UI scale grew (atlas area), but
        the LCD text shader needs exactly 3 atlas texels per screen pixel
        regardless of scale, and with oversample_v pinned to 1 the atlas
        cost is linear in it (3x, not 9x)."""
        return spec.oversample

    def prewarm(self):
        """Bake the eager (first-frame) fonts only. Everything else loads
        lazily: get() on an unbaked font queues its group and returns None —
        exactly what callers already handle for a failed load (render with
        the current font this frame) — and Melty.apply_ui_scale bakes the
        queue between frames. Baking all 17 enum entries up front cost
        ~230ms of boot (stb rasterize + FreeType hint of a 4096² atlas);
        the eager set is a fraction of that."""
        self._loaded |= {font for font in Font if font.value.eager}
        for font in list(self._loaded):
            self._loaded.update(_GROUP_OF[font])
        self._bake(self._loaded)

    def _bake(self, include):
        """Clear the atlas and re-add every font in `include`, in enum order
        (a merge follower must directly follow its base — enum order plus the
        _GROUP_OF closure at every queue site guarantee both are present and
        adjacent). Between frames only; every previously returned handle is
        dangling afterwards."""
        self._handles.clear()
        self.io.fonts.clear()
        for font in Font:
            if font not in include:
                continue
            spec = font.value
            size = max(self.MIN_SIZE, spec.size * self.scale)
            if spec.merge:
                merge_cfg = dict(
                    merge_mode=True,
                    glyph_extra_spacing_x=spec.extra_spacing * self.scale,
                    glyph_extra_spacing_y=spec.extra_spacing * self.scale,
                )
                # Merged icon fonts cover wide ranges, so they dominate the
                # atlas - spread them out as the scale grows. Untouched at
                # scale <= 1 because the authored atlas is unchanged: imgui's
                # own defaults are (h=3, v=1), and spelling them out is
                # what keeps v from silently becoming 3 here.
                if self.scale > 1.0:
                    merge_cfg["oversample_h"] = self._oversample(spec)
                    merge_cfg["oversample_v"] = 1
                cfg = imgui.FontConfig(**merge_cfg)
            else:
                over = self._oversample(spec)
                # oversample_v stays 1 (imgui's own default): glyph Y is
                # never sub-pixel positioned (RenderText floors pos.y), so
                # vertical oversampling only smears the baseline, x-height and
                # crossbars by ~1/N px - the "slightly soft text" look. It
                # also keeps the atlas rows 1:1 with screen rows, which the
                # LCD shader's horizontal-only taps rely on.
                cfg = imgui.FontConfig(
                    oversample_h=over,
                    oversample_v=1,
                    pixel_snap_h=True,
                )
            try:
                # Merged (icon) fonts use their explicit range; base fonts
                # without one get _UI_RANGE instead of imgui's Latin-only
                # default (see the comment on _UI_RANGE).
                glyph_ranges = spec.glyph_ranges
                if glyph_ranges is None and not spec.merge:
                    glyph_ranges = _UI_RANGE
                if glyph_ranges is not None:
                    ranges = imgui.GlyphRanges(list(glyph_ranges))
                    handle = self.io.fonts.add_font_from_file_ttf(spec.path, size, cfg, ranges)
                else:
                    handle = self.io.fonts.add_font_from_file_ttf(spec.path, size, cfg)
                self._handles[font] = handle
            except Exception as e:
                print(f"FontManager: failed to load {font.name} from {spec.path}: {e}")
                self._handles[font] = None

    def get(self, font: Font):
        """Handle for `font`, or None — for a failed load AND for a font not
        baked yet. An unbaked font's group is queued here and baked between
        frames (flush_pending, driven by Melty.apply_ui_scale), so callers
        keep their existing None handling: skip push_font this frame, the
        real handle arrives next frame and the flush invalidates every
        cached tile. A failed font sits in _handles as None and never
        re-queues."""
        if font in self._handles:
            return self._handles[font]
        group = _GROUP_OF.get(font)
        if group is not None:
            self._pending.update(group)
        return None

    def peek(self, font: Font):
        """Handle for `font` if it is baked, else None — WITHOUT queueing a
        lazy load. For long-lived handle caches (Melty.large_font) that must
        refresh after every re-bake but should never force an unused font
        into the atlas: at boot the eager get(DEJAVU_SANS_50) queued a font
        nothing drew, costing a whole extra re-bake + tile invalidation."""
        return self._handles.get(font)

    # ------------------------------------------------------------------
    # FreeType hinting pass
    # ------------------------------------------------------------------
    # stb_truetype (imgui's rasterizer) has no hinter: a 1.3 px stem or an
    # x-height at 8.25 px lands wherever the outline puts it and smears over
    # two pixel rows. FreeType's light autohinter grid-fits the outline
    # VERTICALLY (baseline, x-height, crossbars snap to pixel rows) before
    # rasterizing, and its LCD render mode emits 3 subpixel coverages per
    # pixel - exactly the layout the LCD text shader already reads from the
    # 3x-oversampled stb atlas (split_overlay_renderer.py). So the pass
    # keeps imgui's glyph geometry (quads, advances, UVs) unchanged and just
    # swaps the TEXELS: for every glyph whose hinted bitmap fits its stb
    # rect it writes the FreeType coverage there; the few that grow a row
    # under hinting (arrows, accent marks, `i`/`j`/`t` at ~10 px em) keep
    # stb's bitmap. Harmony-mode FreeType pads each LCD bitmap by a zero
    # pixel per side; clipping that to the rect is lossless.
    #
    # Glyph rects are not exposed by pyimgui, so they are PROBED: one
    # throwaway imgui frame per font draws every codepoint with add_text and
    # the quad + UV come back out of the draw data (4 vertices per glyph).
    # Must run between frames (renderer init, FontManager.rebuild).

    _FT_LOAD = None  # resolved lazily: freetype may be absent

    def _ft_face(self, path: str):
        import freetype
        faces = self.__dict__.setdefault("_ft_faces", {})
        face = faces.get(path)
        if face is None:
            face = freetype.Face(path)
            faces[path] = face
        return face

    def _probe_glyph_rects(self, handle, cps, tex_w: int, tex_h: int):
        """{cp: (qx0, qy0, qx1, qy1, tx0, ty0, tx1, ty1)} — quad in px
        relative to the add_text pen, texel rect in the atlas. Codepoints
        that emit no quad (spaces) are absent; imgui's fallback glyph shows
        up as repeated rects, deduped by the caller."""
        io = self.io
        saved = (io.display_size, io.delta_time)
        io.display_size = (4096.0, 4096.0)
        io.delta_time = 1.0 / 60.0
        out = {}
        try:
            imgui.new_frame()
            dl = imgui.get_background_draw_list()
            imgui.push_font(handle)
            order = []
            cell = 64
            for i, cp in enumerate(cps):
                x = float((i % 60) * cell)
                y = float((i // 60) * cell)
                n0 = dl.vtx_buffer_size
                dl.add_text(x, y, 0xFFFFFFFF, chr(cp))
                if dl.vtx_buffer_size == n0 + 4:
                    order.append((cp, n0, x, y))
            imgui.pop_font()
            imgui.render()
            dd = imgui.get_draw_data()
            lists = dd.commands_lists
            if not lists:
                return out
            cl = lists[0]
            buf = ctypes.string_at(cl.vtx_buffer_data, cl.vtx_buffer_size * imgui.VERTEX_SIZE)
            vs = imgui.VERTEX_SIZE
            import struct
            for cp, n0, x, y in order:
                v0 = struct.unpack_from("ffff", buf, n0 * vs)
                v2 = struct.unpack_from("ffff", buf, (n0 + 2) * vs)
                out[cp] = (v0[0] - x, v0[1] - y, v2[0] - x, v2[1] - y,
                           int(round(v0[2] * tex_w)), int(round(v0[3] * tex_h)),
                           int(round(v2[2] * tex_w)), int(round(v2[3] * tex_h)))
        finally:
            io.display_size, io.delta_time = saved
        return out

    def hint_atlas(self, width: int, height: int, pixels: bytes):
        """Return RGBA32 atlas bytes with FreeType light-hinted LCD glyphs
        written into imgui's rects, or None when freetype is unavailable.
        Stats land on `self.hint_stats` as {font name: (hinted, fallback)}."""
        start_time = time.time()
        try:
            import freetype
        except ImportError:
            if not getattr(self, "_ft_warned", False):
                self._ft_warned = True
                print("FontManager: freetype-py not installed; text stays unhinted "
                      "(pip install freetype-py)")
            return None
        load_flags = (freetype.FT_LOAD_TARGET_LIGHT | freetype.FT_LOAD_FORCE_AUTOHINT
                      | freetype.FT_LOAD_NO_BITMAP)
        atlas = np.frombuffer(pixels, np.uint8).reshape(height, width, 4).copy()
        alpha = atlas[..., 3]
        stats = {}
        for font in Font:
            spec = font.value
            handle = self._handles.get(font)
            if spec.merge or not spec.hint or handle is None:
                continue
            size = max(self.MIN_SIZE, spec.size * self.scale)
            if size > HINT_MAX_SIZE:
                continue
            cps = _expand_ranges(spec.glyph_ranges or _UI_RANGE)
            rects = self._probe_glyph_rects(handle, cps, width, height)
            face = self._ft_face(spec.path)
            box = face.ascender - face.descender           # hhea, font units
            scale = size / box                              # == stbtt_ScaleForPixelHeight
            baseline = math.floor(face.ascender * scale + 1)  # imgui: IM_FLOOR(ascent + 1)
            face.set_char_size(0, int(round(size * face.units_per_EM / box * 64)), 72, 72)
            seen = set()
            hinted = fallback = 0
            for cp, (qx0, qy0, qx1, qy1, tx0, ty0, tx1, ty1) in rects.items():
                if (tx0, ty0) in seen:
                    continue                                # fallback-glyph repeats
                seen.add((tx0, ty0))
                if face.get_char_index(cp) == 0:
                    continue
                face.load_char(chr(cp), load_flags)
                g = face.glyph
                g.render(freetype.FT_RENDER_MODE_LCD)
                bm = g.bitmap
                rows, bw, pitch = bm.rows, bm.width, abs(bm.pitch)
                if rows == 0 or bw == 0:
                    continue
                rect_w, rect_h = tx1 - tx0, ty1 - ty0
                r0 = baseline - g.bitmap_top - int(round(qy0))
                if r0 < 0 or r0 + rows > rect_h:
                    fallback += 1                           # hinting grew it a row
                    continue
                # Subpixel column of bitmap col #0. imgui's quad x0 is in
                # THIRDS of a pixel (stb's 3x bitmap box), so place at
                # subpixel precision - rounding c0 to a whole pixel puts
                # glyphs +-1/3 px from their neighbours (uneven spacing).
                c0 = int(round(3 * (g.bitmap_left - qx0)))
                j0, j1 = max(0, -c0), min(bw, rect_w - c0)
                if j1 <= j0:
                    fallback += 1
                    continue
                raw = ctypes.string_at(bm._FT_Bitmap.buffer, rows * pitch)
                src = np.frombuffer(raw, np.uint8).reshape(rows, pitch)[:, :bw]
                alpha[ty0:ty1, tx0:tx1] = 0
                alpha[ty0 + r0:ty0 + r0 + rows, tx0 + c0 + j0:tx0 + c0 + j1] = src[:, j0:j1]
                hinted += 1
            stats[font.name] = (hinted, fallback)
        self.hint_stats = stats
        total_h = sum(h for h, _ in stats.values())
        total_f = sum(f for _, f in stats.values())
        print(f"FontManager: FreeType-hinted {total_h} glyphs across {len(stats)} fonts "
              f"({total_f} kept stb bitmaps)")
        end_time = time.time()
        print(f"FontManager: hinting pass took {end_time - start_time:.3f} s")
        print(f"wall time {end_time}")
        return atlas.tobytes()