"""
Integration test for render_func with convert_in / convert_out.

Spawns a real GLFW window + imgui context and runs render_func
through multiple simulated frames to verify the full conversion
pipeline including Background.run, caching, and draw_state management.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# conftest.py handles sys.path, imgui context, and GL setup

import meltygui_imgui as imgui
from conftest import _ensure_gl_context, begin_frame, end_frame


# ═══════════════════════════════════════════════════════════════════════════
#  Minimal Melty state required for render_func
# ═══════════════════════════════════════════════════════════════════════════

def _init_melty():
    """Set up the minimum Melty class state for render_func to run."""
    from meltygui.melty import Melty
    from meltygui.core.tile_cache import TileCacheMasked

    if Melty.cache is None:
        Melty.cache = TileCacheMasked()
    Melty.cache.enabled = False  # no offscreen rendering in tests
    Melty.frame_count = 0
    Melty.depth = 0
    Melty.shadow_depth = 0
    Melty.active_layer = 0
    Melty.z_pos = 0
    Melty.annotation_mode = False
    Melty.on_drag = False
    Melty.window_drag = False
    Melty.imgui_active = False
    Melty.imgui_popup_open = False
    Melty.imgui_any_item_active = False
    Melty.imgui_main_window_hovered = True
    Melty.channels_split = False
    Melty.unique_stack = []
    Melty.suffix_stack = []
    Melty.mode_stack = []
    Melty.melty_window_stack = []
    Melty.draw_state_stack = []
    Melty.input_value_stack = [None]
    Melty.size_stack = []
    Melty.wrap_stack = []
    Melty.bg_stack = []
    Melty.bg_color_stack = []
    Melty.collection_stack = []
    Melty.fixed_size_stack = []
    Melty.clip_stack = []
    Melty.content_height_stack = []
    Melty.indent_count = 0
    Melty.unindent_count = 0
    Melty.bg_depth = 0
    Melty.detached = False
    Melty.silence_invalidate = False
    Melty.all_uniques = set()
    Melty.seen_unique = set()
    Melty.nested_collections = 0

    # imgui style defaults (set in begin_frame normally)
    style = imgui.get_style()
    if Melty.original_spacing is None:
        Melty.original_spacing = style.item_spacing
        Melty.original_window_padding = style.window_padding
        Melty.original_frame_padding = style.frame_padding

    # Style manager stub
    if Melty.style_manager is None:
        sm = MagicMock()
        sm.get_tint.return_value = (0.0, 0.0, 0.0)
        sm.set_imgui_tint = MagicMock()
        Melty.style_manager = sm

    # Global attrs needed by many render_func
    if 'style_manager' not in Melty.global_attrs:
        Melty.global_attrs['style_manager'] = Melty.style_manager

    # Vis / draw_state_registry stub
    if Melty.vis is None:
        vis = MagicMock()
        vis.root.draw_state_registry = {}
        Melty.vis = vis
        Melty.draw_state_registry = vis.root.draw_state_registry

    return Melty


def _tick_frame(meltygui):
    """Advance one frame."""
    meltygui.frame_count += 1
    meltygui.depth = 0
    meltygui.shadow_depth = 0
    meltygui.z_pos = 0
    meltygui.active_layer = 0
    meltygui.unique_stack = []
    meltygui.suffix_stack = []
    meltygui.draw_state_stack = []
    meltygui.input_value_stack = [None]
    meltygui.size_stack = []
    meltygui.wrap_stack = []
    meltygui.bg_stack = []
    meltygui.bg_color_stack = []
    meltygui.fixed_size_stack = []
    meltygui.clip_stack = []
    meltygui.all_uniques = set()
    meltygui.seen_unique = set()


# ═══════════════════════════════════════════════════════════════════════════
#  Test target: a python function in a temp dire
# ═══════════════════════════════════════════════════════════════════════════

_SAMPLE_SOURCE = """\
x = 1
y = 2
"""


# ═══════════════════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════════════════







if __name__ == '__main__':
    unittest.main()
