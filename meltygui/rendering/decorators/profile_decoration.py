import functools
from typing import Callable, Any
from collections import deque, defaultdict

from meltygui.melty import Melty


def profile(func: Callable) -> Callable:
    """
    Decorator that profiles a function line-by-line and stores results in Melty.profiles_results.

    Usage:
        @profile
        def my_function():
            # your code here
            pass

    The profiling results will be available at:
        Melty.profiles_results['my_function'][-1]  # Most recent call

    Each result is a list of (line_of_code, time_ms) tuples sorted by time.

    Note: Requires line_profiler package. Install with: pip install line_profiler
    """
    # Track if we're already profiling this function (for recursive calls)
    _profiling = False

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        nonlocal _profiling

        # Only profile at the top level to avoid nested profiler conflicts
        if _profiling:
            return func(*args, **kwargs)

        try:
            from line_profiler import LineProfiler
        except ImportError:
            # Fallback: just run the function without profiling
            print("Warning: line_profiler not installed. Install with: pip install line_profiler")
            return func(*args, **kwargs)

        if func.__name__ not in Melty.profiles_results:
            # Create a line profiler
            profiler = LineProfiler()
            profiler.add_function(func)

            # Profile the function execution
            _profiling = True
            try:
                profiler.enable()
                result = func(*args, **kwargs)
                profiler.disable()
            finally:
                _profiling = False

            # Extract the line-by-line stats
            filtered_results = []
            stats = profiler.get_stats()

            # stats.timings is a dict: {(filename, line_start, func_name): [(lineno, nhits, time), ...]}
            for key, timings in stats.timings.items():
                filename, line_start, func_name_inner = key

                # Read the source code
                import linecache

                for lineno, nhits, time in timings:
                    if nhits > 0:  # Only include lines that were executed
                        source_line = linecache.getline(filename, lineno).strip()
                        if source_line:
                            # line_profiler time is in units (typically nanoseconds, 1e-09)
                            # Convert to milliseconds: time * unit * 1000 (to go from seconds to ms)
                            time_ms = time * stats.unit * 1000
                            filtered_results.append((source_line, time_ms, lineno))

            # Sort by time (slowest first)
            filtered_results.sort(key=lambda x: x[1], reverse=True)

            # Store the profile results (deque automatically to max 5 items)
            Melty.profiles_results[func.__name__] = filtered_results

            return result
        else:
            return func(*args, **kwargs)

    return wrapper

