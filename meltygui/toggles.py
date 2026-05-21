from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import tint
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


class Counters:
    # Nested window
    nested_window_count = 16

@window
class Tint:
    # Context menu tints
    context_select_tint = (1.0, 0.7, 0.2)
    context_select_outline_alpha = 0.054
    context_select_bg_alpha = 0.592
    context_select_rounding = 5.0
    
    # Background constants
    context_menu_bg_offset = 3.0

@window
@tint({"draw_legacy": (0.8, 0.2, 0.2)})
class Toggles:
    # Invalidation settings
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    ds_invalidate_stack = False
    profile_mode = ProfileMode.LIGHT
    offscreen_debug = False
    debug_stale_tint = False
    
    # Image Settings
    brightness = 0.22
    contrast = 1.445
    saturation = 0.351

    debug_z_depth = False
    filters = True
    show_excluded = True
    filters = True
    show_excluded = True

    # Drawing Settings
    draw_melty = False
    draw_legacy = False

@window
class LegacyToggles:
    # All the padding settings from imgui style
    item_spacing = (5, 2)
    window_padding = (6, 6)
    frame_padding = (4, 1)
    line_height = 16


# =========
def shadow_depth_at(depth, active_layer):
    scaling = 53.42
    cap = 5.975

    divisor = max(cap, depth - scaling)

    from src.lsd.gl_gui.melty import Melty
    depth_and_layer = active_layer * Melty.max_depth + (depth * (scaling / (divisor)))
    depth_and_layer *= Melty.layer_inc
    return depth_and_layer


class WindowManager:
    excluded_windows = ["demo test", "Egg Time", "Layer 1"]