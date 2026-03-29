from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
class Counters:
    # Nested window
    nested_window_count = 23
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
    
    #---------- Visual Settings ---------
    brightness = 0.098
    
    some_dict = [3,2,3,3]
    
    some_val = 2
    contrast = 2.816
    saturation = 0.815
    debug_context_menu = False
    debug_z_depth = False
    filters = True
    show_excluded = False
    filters = True
    show_excluded = False
    
    my_var= {1:2}

def shadow_depth_at(depth, active_layer):
    scaling = 53.64
    cap = 2.17

    divisor = max(cap, depth - scaling)

    from src.lsd.gl_gui.melty import Melty
    depth_and_layer = active_layer * Melty.max_depth + (depth * (scaling / (divisor)))
    depth_and_layer *= Melty.layer_inc
    return depth_and_layer
    
def kfunc():

    for i in range(25):
        print("someting")
        my_var = [56,70,13]
        somethin = False
        some_func()



