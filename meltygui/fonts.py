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

    JETBRAINS_MONO_14 = FontSpec(_JETBRAINS_MONO, 14.0)
    JETBRAINS_MONO_16 = FontSpec(_JETBRAINS_MONO, 16.0)
    JETBRAINS_MONO_18 = FontSpec(_JETBRAINS_MONO, 18.0)
    JETBRAINS_MONO_19 = FontSpec(_JETBRAINS_MONO, 20, glyph_ranges=_MONO_TUI_RANGE)
    FONTAWESOME_MONO_19 = _fa_merge(16.0)

    JETBRAINS_MONO_20 = FontSpec(_JETBRAINS_MONO, 20.0)
    JETBRAINS_MONO_22 = FontSpec(_JETBRAINS_MONO, 22.0)
    JETBRAINS_MONO_40 = FontSpec(_JETBRAINS_MONO, 40.0)
    FONTAWESOME_MONO_40 = _fa_merge(31.0)
    JETBRAINS_MONO_50 = FontSpec(_JETBRAINS_MONO, 50.0)
    FONTAWESOME_MONO_50 = _fa_merge(39.0)


class FontManager:
    def __init__(self, io):
        self.io = io
        self._handles: dict = {}

    def prewarm(self):
        for font in Font:
            spec = font.value
            if spec.merge:
                cfg = imgui.FontConfig(
                    merge_mode=True,
                    glyph_extra_spacing_x=spec.extra_spacing,
                    glyph_extra_spacing_y=spec.extra_spacing,
                )
            else:
                cfg = imgui.FontConfig(
                    oversample_h=spec.oversample,
                    oversample_v=spec.oversample,
                    pixel_snap_h=True,
                )
            try:
                if spec.glyph_ranges is not None:
                    ranges = imgui.GlyphRanges(list(spec.glyph_ranges))
                    handle = self.io.fonts.add_font_from_file_ttf(spec.path, spec.size, cfg, ranges)
                else:
                    handle = self.io.fonts.add_font_from_file_ttf(spec.path, spec.size, cfg)
                self._handles[font] = handle
            except Exception as e:
                print(f"FontManager: failed to load {font.name} from {spec.path}: {e}")
                self._handles[font] = None

    def get(self, font: Font):
        return self._handles.get(font)
