# This works?
class Toggles:
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = True
    render_depth = False
    
    # This works
def my_func():
    for i in range(43):
        print("someting")
        my_var = 46
    