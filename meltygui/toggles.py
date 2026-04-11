from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
class Counters:
    # Nested window
    nested_window_count = 6
def some_func():
    print("hello world")
    

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
    brightness = 0.098
    
    some_dict = [21,13,3,3]
    
    some_val = 2
    contrast = 15.513
    saturation = 0.815
    debug_context_menu = False
    debug_z_depth = False
    filters = True
    show_excluded = True
    filters = True
    show_excluded = True
    
    my_var= {1:15}


def shadow_depth_at(depth, active_layer):
    scaling = 52.85
    cap = 6.05

    divisor = max(cap, depth - scaling)

    from src.lsd.gl_gui.melty import Melty
    depth_and_layer = active_layer * Melty.max_depth + (depth * (scaling / (divisor)))
    depth_and_layer *= Melty.layer_inc
    return depth_and_layer
    
def some_func():

    for i in range(25):
        print("someting")
        my_var = [100,69,13]
        somethin = False
        some_func()
