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


@dataclass(frozen=True)
class FontSpec:
    path: str
    size: float
    merge: bool = False
    glyph_ranges: Optional[Tuple[int, ...]] = None
    extra_spacing: float = 0.0
    oversample: int = 3


class Font(RelaxedEnum):
    # Order matters: a `merge=True` font is folded into the most recently
    # added non-merged font, so primary fonts must come before their merges.
    DEJAVU_SANS_18 = FontSpec(_DEJAVU_SANS, 18.0)
    FONTAWESOME_14 = FontSpec(
        str(_RESOURCES / "fontawesome-webfont.ttf"),
        14.0,
        merge=True,
        glyph_ranges=_FA_ICON_RANGE,
        extra_spacing=2.0,
    )
    DEJAVU_SANS_50 = FontSpec(_DEJAVU_SANS, 50.0)
    JETBRAINS_MONO_14 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 14.0)
    JETBRAINS_MONO_16 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 16.0)
    JETBRAINS_MONO_18 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 18.0)
    JETBRAINS_MONO_19 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 20)

    JETBRAINS_MONO_20 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 20.0)
    JETBRAINS_MONO_22 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 22.0)
    JETBRAINS_MONO_40 = FontSpec(str(_RESOURCES / "JetBrainsMono-Regular.ttf"), 40.0)


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
