from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


class Counters:
    # Nested window
    nested_window_count = 16

@window
class Toggles:
    # ---------- Invalidation Settings -----
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    ds_invalidate_stack = False
    profile_mode = ProfileMode.LIGHT
    offscreen_debug = False
    debug_stale_tint = False
    
    #---------- Visual Settings ---------
    brightness = 0.22
    contrast = 1.445

    saturation = 0.351

    debug_z_depth = False
    filters = True
    show_excluded = True
    filters = True
    show_excluded = True

# Utils
def shadow_depth_at(depth, active_layer):
    scaling = 53.42
    cap = 5.975

    divisor = max(cap, depth - scaling)

    from src.lsd.gl_gui.melty import Melty
    depth_and_layer = active_layer * Melty.max_depth + (depth * (scaling / (divisor)))
    depth_and_layer *= Melty.layer_inc
    return depth_and_layer
    