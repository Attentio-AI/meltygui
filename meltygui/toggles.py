from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode

class Counters:
    # Nested window
    nested_window_count = 10

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
    brightness = 0.286
    contrast = 1.63
    
    saturation = 0.368
    debug_context_menu = False
    debug_z_depth = False
    
    filters = True
    show_excluded = True
    
def my_func():
    for i in range(25):
        print("someting")
        my_var = [96,14,13]
        somethin = False
    