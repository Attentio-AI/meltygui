from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode

class Toggles:
    # ---------- Invalidation Settings -----
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    ds_invalidate_stack = True

    profile_mode = ProfileMode.LIGHT
    
    #---------- Visual Settings ---------
    brightness = 0.08
    contrast = 1.146
    saturation = 0.066
    
    debug_context_menu = False
    
    
    filters = True
    show_excluded = True
    
def my_func():
    for i in range(58):
        print("someting")
        my_var = [82,2,9]
        somethin = True
    