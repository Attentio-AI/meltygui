from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

import imgui

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
    oversample: int = 3


_JETBRAINS_MONO = str(_RESOURCES / "JetBrainsMono-Regular.ttf")


def _fa_merge(size: float) -> FontSpec:
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
    )


class Font(RelaxedEnum):
    # Order matters: a `merge=True` font is folded into the most recently
    # added non-merged font, so primary fonts must come before their merges.
    DEJAVU_SANS_18 = FontSpec(_DEJAVU_SANS, 18.0)
    FONTAWESOME_14 = _fa_merge(14.0)
    DEJAVU_SANS_50 = FontSpec(_DEJAVU_SANS, 50.0)
    DEJAVU_SANS_22 = FontSpec(_DEJAVU_SANS, 22.0)


    JETBRAINS_MONO_30 = FontSpec(_JETBRAINS_MONO, 30.0)
    JETBRAINS_MONO_13 = FontSpec(_JETBRAINS_MONO, 13.0)
    JETBRAINS_MONO_14 = FontSpec(_JETBRAINS_MONO, 14.0)
    JETBRAINS_MONO_15 = FontSpec(_JETBRAINS_MONO, 15.0)
    JETBRAINS_MONO_16 = FontSpec(_JETBRAINS_MONO, 16.0, glyph_ranges=_MONO_TUI_RANGE)
    JETBRAINS_MONO_18 = FontSpec(_JETBRAINS_MONO, 18.0)
    JETBRAINS_MONO_19 = FontSpec(_JETBRAINS_MONO, 20, glyph_ranges=_MONO_TUI_RANGE)
    FONTAWESOME_MONO_19 = _fa_merge(16.0)

    JETBRAINS_MONO_20 = FontSpec(_JETBRAINS_MONO, 20.0)
    JETBRAINS_MONO_22 = FontSpec(_JETBRAINS_MONO, 22.0)
    JETBRAINS_MONO_40 = FontSpec(_JETBRAINS_MONO, 40.0)
    FONTAWESOME_MONO_40 = _fa_merge(31.0)
    JETBRAINS_MONO_50 = FontSpec(_JETBRAINS_MONO, 50.0)
    FONTAWESOME_MONO_50 = _fa_merge(39.0)


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

    def rebuild(self, scale: float, impl=None) -> bool:
        """Re-bake every font at `scale` and hand the new atlas to the
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
        self._handles.clear()
        self.io.fonts.clear()
        self.prewarm()
        if impl is not None:
            impl.refresh_font_texture()
        return True

    def _oversample(self, spec: FontSpec) -> int:
        """Oversampling for a spec at the current scale.

        Oversampling buys SUBPIXEL positioning accuracy, so what matters is
        samples per glyph relative to glyph size — scaling the glyph up
        already delivers that. Left exactly at the authored value for scale
        <= 1 (so 1.0 bakes the atlas it always did) and walked down as the
        scale grows, because atlas AREA goes as oversample squared: holding
        it at 3 turns a 64 MB atlas into 256 MB at 2x for no visible gain.
        """
        if self.scale <= 1.0:
            return spec.oversample
        return min(spec.oversample, max(1, round(spec.oversample / self.scale)))

    def prewarm(self):
        for font in Font:
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
                cfg = imgui.FontConfig(
                    oversample_h=over,
                    oversample_v=over,
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
        return self._handles.get(font)
