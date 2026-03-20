from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode

class Counters:
    nested_window_count = 0
    

class Toggles:
    # ---------- Invalidation Settings -----
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    ds_invalidate_stack = True

    profile_mode = ProfileMode.LIGHT
    offscreen_debug = False
    
    #---------- Visual Settings ---------
    brightness = 0.222
    contrast = 1.63
    
    saturation = 0.368
    debug_context_menu = False
    
    filters = True
    show_excluded = True
    
def my_func():
    for i in range(25):
        print("someting")
        my_var = [50,9,15]
        somethin = False
    