# Testing commentlkj
class Toggles:
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    
def my_func():
    for i in range(30):
        print("someting")
        my_var = 76
    