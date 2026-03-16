

class Toggles:
    invalidate_stack_trace = False
    debug_threads = False
    slow_down_threads = False
    render_depth = True
    # pass

def some_func(my_var=10):
    if my_var == 5:
        my_var = 1

    print("hello")