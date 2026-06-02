from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import tint, Core, defaults
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

class Counters:
    # Nested window
    nested_window_count = 18
    some_dict = [1,1,1]
    some_dict2 = {1:1}
    
@window
class Tint:
    # Context menu tints
    context_select_tint = (1.0, 0.7, 0.2)
    context_select_outline_alpha = -0.015
    context_select_bg_alpha = 0.592
    context_select_rounding = 4.988
    
    # Background constants
    context_menu_bg_offset = 4.0

    # Highlight outline boxes (parent + child views)
    highlight_outline_thickness = 3.0   # outline line thickness
    highlight_outline_rounding = 7.868    # corner radius of the outline boxes
    highlight_outline_alpha = 0.472      # outline opacity
    highlight_bg_alpha = 0.114           # parent fill opacity

    # Selection rect (child views)
    select_outline_thickness = 2.0       # selection outline line thickness
    select_outline_alpha = 0.65          # selection outline opacity
    select_bg_alpha = 0.08               # selection fill opacity

@window
class Swoosh:
    # Nested-window "swoosh" connector (parent outline -> nested view)
    tint = (1.0, 0.7, 0.2)   # fallback color if no style manager is available
    value = 1.023              # intensity of the highlight (super-bright yellow)
    saturation = 0.791        # saturation scale applied to the current tint
    alpha = 1.0             # opacity of the swoosh
    end_thickness = 2.304      # half-width at the two endpoints (thick)
    cap_scale = 0.962          # end-cap dot radius as a multiple of end thickness
    mid_thickness = 0.441      # half-width at the middle (thin)
    curve = 0.22             # max arc bow as a fraction of endpoint distance
    curve_ramp = 0.581        # how the bow eases in with slope (>1 stays straighter longer)
    edge_softness = 1.138     # px smoothing window for the shared-edge anchor (0 = hard)
    segments = 31            # tessellation count (higher = smoother)
    taper = 10.0              # slope of the end->middle thickness falloff
    aa_width = 1.5           # antialiased edge-stroke width in px (0 = none)
    
    some_text = False
    some_dict = [1,1,1,1]


@window(tint=(0.5, 0.6, 0.7))
class Toggles:
    test_float = 3.303
    debug_scroll = False
    @defaults(tint=(0.3,0.2,0.2))
    class InvalidateTracker:
        keep_for_frames = 2
        enable = False
        draw_bvh = False
        
    @defaults(tint=(0.10598, 0.2410416603088379,0.320930242538452))
    class Debug:
        slow_frame_rate = False
    # Overlay a transparent green square on any draw_state whose blit tile is
    # fully filled (filled_bbox covers full size). Used to verify the scroll-
    # invalidation stop-at-filled gate is actually marking tiles complete.
    show_filled_tiles = False
    # Invalidation settings
    invalidate_stack_trace = False
    text_focus_stack_trace = False
    attrib_change_stack_trace = False
    attrib_churn_log = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    ds_invalidate_stack = False
    profile_mode = ProfileMode.LIGHT
    debug_stale_tint = False
    show_line_break = False
    # Filter SettingS

    brightness = 0.317
    contrast = 1.574
    saturation = -0.4
    prefered_header_width = 204
    max_preferred_header_width = 196
    debug_z_depth = False
    filters = True
    show_excluded = True
    layer_stack_trace = False
    show_line_breaks = False
    # Shadow Settings
    shadow_downscale = 2
    shadow_edge_sharpness = 50.0
    draw_legacy = False

    # Scroll settings 
    scroll_speed = 280.0

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

    depth_and_layer = active_layer * Core.melty.max_depth + (depth * (scaling / (divisor)))
    depth_and_layer *= Core.melty.layer_inc
    return depth_and_layer


class WindowManager:
    excluded_windows = ["demo test", "Egg Time", "Layer 1"]