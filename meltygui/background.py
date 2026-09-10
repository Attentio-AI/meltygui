import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import sys

from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace, get_live_frames, _print_lock, trace_group, request_render
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
from src.lsd.gl_gui.view.invalidation_tracker import Note


class Background:
    _user_cache = {}  # user_id -> OrderedDict{hash -> result}
    _cache_size = 1
    _active = set()
    _user_tasks = {}
    _lock = threading.Lock()
    _pool = ThreadPoolExecutor(max_workers=16)
    _debounce_timers = {}   # debounce_key -> Timer
    _debounce_latest = {}   # debounce_key -> dict of latest call params
    _hash_times = {}        # type_name -> [total_time_sec, count]
    _dict_key_times = {}    # dict key name -> [total_time_sec, count]
    _task_times = {}        # func_name -> [total_time_sec, count]
    did_shutdown = False
    @classmethod
    def _record_dict_key_time(cls, key: str, elapsed: float):
        # No lock here - called from simple_hash which may recurse deeply;
        # some inaccuracy from races is acceptable for profiling.
        entry = cls._dict_key_times.get(key)
        if entry is None:
            cls._dict_key_times[key] = [elapsed, 1]
        else:
            entry[0] += elapsed
            entry[1] += 1

    @classmethod
    def _timed_hash(cls, value, user_id):
        import time
        type_name = type(value).__name__
        t0 = time.perf_counter()
        h = cls.simple_hash(value=value) + user_id
        elapsed = time.perf_counter() - t0

        with cls._lock:
            if type_name not in cls._hash_times:
                cls._hash_times[type_name] = [0.0, 0]
            cls._hash_times[type_name][0] += elapsed
            cls._hash_times[type_name][1] += 1

        return h

    @classmethod
    def shutdown(cls):
        print("Shutting down background system...")
        # with cls._lock:
        cls._active.clear()
        cls._user_tasks.clear()
        cls._user_cache.clear()
        for timer in cls._debounce_timers.values():
            timer.cancel()
        cls._debounce_timers.clear()
        cls._debounce_latest.clear()

        print_timings = False

        if print_timings:
            print("\n--- Hash timing averages (by type) ---")
            for type_name, (total, count) in sorted(cls._hash_times.items()):
                avg_ms = (total / count) * 1000
                print(f"  {type_name:<30} avg={avg_ms:.3f}ms  n={count}")
            print("--------------------------------------\n")

            print("--- Hash timing averages (dict keys, sorted by avg) ---")
            sorted_keys = sorted(cls._dict_key_times.items(), key=lambda x: x[1][0] / x[1][1], reverse=True)
            for key_name, (total, count) in sorted_keys:
                avg_ms = (total / count) * 1000
                print(f"  {key_name:<40} avg={avg_ms:.3f}ms  n={count}")
            print("-------------------------------------------------------\n")

            print("--- Task timing averages (by function, sorted by avg) ---")
            sorted_tasks = sorted(cls._task_times.items(), key=lambda x: x[1][0] / x[1][1], reverse=True)
            for func_name, (total, count) in sorted_tasks:
                avg_ms = (total / count) * 1000
                print(f"  {func_name:<50} avg={avg_ms:.3f}ms  n={count}")
            print("----------------------------------------------------------\n")

            print("Shutting down background thread pool...")

        cls._pool.shutdown(wait=True)
        print("Background thread pool shut down successfully.")
        cls.did_shutdown = True

    @classmethod
    def run(cls, func, user_id, func_kwargs=None, *,
            stateful=False, no_cache=False, invalidate_id=None,
            on_frame=None, frames=None, debounce=10):
        """Run func with func_kwargs, with caching, debouncing, and background dispatch.

        Background.run's own parameters (user_id, stateful, no_cache, etc.) are
        cleanly separated from the function's kwargs via the func_kwargs dict.
        """
        if func_kwargs is None:
            func_kwargs = {}

        if "input_value" in func_kwargs:
            h = cls._timed_hash(func_kwargs.get("input_value", None), user_id)
        else:
            print(f"Warning no 'value' or 'input_value' in func_kwargs for {func.__name__} with user_id {user_id}, using 0 as hash")
            h = "0_" + str(user_id)

        # A side-effect payload (e.g. a save's `code_str`) isn't the hashed input
        # but DOES change the operation. The hashed input for _do_save is the
        # Address, which is stable across edits - so without folding the payload
        # in, two writes to the same location share a hash and the debounce /
        # in-flight dedup below sees the second as a duplicate and drops its
        # content. That's the intermittent "lost save". Fold it in so distinct
        # content gets a distinct key (identical content still dedups, correctly).
        if "code_str" in func_kwargs:
            h = f"{h}|{cls.simple_hash(value=func_kwargs.get('code_str'))}"

        # if "search_text" in func_kwargs:
        #     h_s = cls._timed_hash(func_kwargs.get('search_text', None), user_id)
        #     h = str(h) + "_" + str(h_s)
        #     print(f"{func_kwargs.get('search_text')}")

        # Inline path for stateful converters (no apply, short chains)
        if stateful:
            if not no_cache:
                cache = cls._user_cache.get(user_id)
                if cache and h in cache:
                    cache.move_to_end(h)
                    return cache[h]

            try:
                result = func(**func_kwargs)
            except Exception:
                result = Pending(originated=cls, status="Read Only", state=PendingState.ERROR)

            if user_id not in cls._user_cache:
                cls._user_cache[user_id] = OrderedDict()
            cls._user_cache[user_id][h] = result
            cls._user_cache[user_id].move_to_end(h)
            while len(cls._user_cache[user_id]) > cls._cache_size:
                cls._user_cache[user_id].popitem(last=False)

            return result


        from src.lsd.gl_gui.melty import Melty
        if Melty.frame_count < 2:
            debounce = None

        # --- debounce path ---
        if debounce is not None:
            debounce_key = user_id

            cache = cls._user_cache.get(user_id)

            stale_value = None
            if cache and h in cache:
                if no_cache:
                    stale_value = cache[h]
                else:
                    cache.move_to_end(h)
                    return cache[h]

            if h in cls._active:
                return stale_value if stale_value is not None else Pending(originated=cls, status="background thread active", state=PendingState.BACKGROUND)

            latest = cls._debounce_latest.get(debounce_key)
            if latest is not None and latest.get("hash") == h:
                return stale_value if stale_value is not None else Pending(originated=cls, status="debounce waiting", state=PendingState.BACKGROUND)

            prev = cls._debounce_timers.pop(debounce_key, None)
            if prev is not None:
                prev.cancel()

            cls._debounce_latest[debounce_key] = dict(
                hash=h,
                func=func, user_id=user_id, no_cache=no_cache,
                invalidate_id=invalidate_id, on_frame=on_frame,
                func_kwargs=func_kwargs,
            )

            if Toggles.debug_threads:
                frames = get_live_frames()

            def _fire():
                cls._debounce_timers.pop(debounce_key, None)
                latest = cls._debounce_latest.pop(debounce_key, None)
                if latest is not None:
                    cls.run(
                        latest["func"], latest["user_id"],
                        func_kwargs=latest["func_kwargs"],
                        no_cache=latest["no_cache"],
                        invalidate_id=latest["invalidate_id"],
                        on_frame=latest["on_frame"],
                        debounce=None, frames=frames,
                    )
                    if Toggles.InvalidateTracker.invalidate_stack_trace:
                        print_stack_trace(frames=frames)

            timer = threading.Timer(debounce / 1000.0, _fire)
            cls._debounce_timers[debounce_key] = timer
            timer.start()

            return stale_value if stale_value is not None else Pending(originated=cls, status="debounce waiting", state=PendingState.BACKGROUND)

        # --- normal (background thread) path ---

        cache = cls._user_cache.get(user_id)
        stale_value = None
        if cache and h in cache:
            if no_cache:
                stale_value = cache[h]
            else:
                cache.move_to_end(h)
                return cache[h]

        cls._user_tasks[user_id] = h

        if h in cls._active:
            return stale_value if stale_value is not None else Pending(originated=cls, status="background thread active", state=PendingState.BACKGROUND)

        if Toggles.debug_threads and frames is None:
            frames = get_live_frames()
        cls._active.add(h)

        def _task():
            # Check for shutdown
            if cls.did_shutdown:
                print("Background system is shut down; cannot run new tasks.")
                print_stack_trace()
                return Pending(originated=cls, status="shutdown", state=PendingState.ERROR)

            import time
            func_name = getattr(func, "__qualname__", None) or getattr(func, "__name__", repr(func))
            sig = func_name
            _t0 = time.perf_counter()

            try:
                result = func(**func_kwargs)
            except Exception as e:
                with trace_group(f"JOB {user_id}", hash=h) as g:
                    print_stack_trace(frames=frames, section="UI Thread",
                                      group=g, watch=["draw_state.name", "input_value", "convert_path", "fn", "clean_args.input_value"])
                    print_stack_trace(exception=e, section="Background Thread",
                                      group=g, watch=["value", "path", "watch", "watch.original_data", "data", "result", "result", "fn"])

                result = Pending(originated=cls, status="Read Only", state=PendingState.ERROR)

            elapsed = time.perf_counter() - _t0
            entry = cls._task_times.get(sig)
            if entry is None:
                cls._task_times[sig] = [elapsed, 1]
            else:
                entry[0] += elapsed
                entry[1] += 1
            if on_frame is not None:
                result = (result, on_frame)
            for uid, task_h in cls._user_tasks.items():
                if task_h == h:
                    if no_cache:
                        cls._user_cache.pop(uid, None)
                    if uid not in cls._user_cache:
                        cls._user_cache[uid] = OrderedDict()
                    cls._user_cache[uid][h] = result
                    cls._user_cache[uid].move_to_end(h)

                    while len(cls._user_cache[uid]) > cls._cache_size:
                        cls._user_cache[uid].popitem(last=False)
            cls._active.discard(h)
            if invalidate_id is not None:
                from src.lsd.gl_gui.melty import Melty
                from src.lsd.gl_gui.utils.glfw_utils import request_render
                if on_frame is None or abs(Melty.frame_count - on_frame) >= 1:
                    note = Note(name=f"Background Invalidate {invalidate_id}", reason=f"func={func_name}", tint=(0,0,1))
                    Melty.cache.invalidate_up(invalidate_id, max_depth=5, note=note)
                    request_render()

        cls._pool.submit(_task)
        return stale_value if stale_value is not None else Pending(originated=cls, status="background thread", state=PendingState.BACKGROUND)

    @staticmethod
    def compute_hash(cls, exclude=None, memo=None, depth=0, do_print=False, include_hidden=False):
        """
        Create a hash of the instance's content with custom attribute exclusions.
        Recursively handles DictConversion objects, collections, and primitive types.

        Args:
            exclude: List of attribute names to exclude from hashing.
            memo: Dictionary of already-processed objects to avoid infinite recursion.
            depth: Current recursion depth for debugging.
            do_print: Whether to print debug information.

        Returns:
            A 16-bit float value representing the instance's content.
        """
        if exclude is None:
            exclude = set()

        if memo is None:
            memo = {}

        # Check if self is already in memo to avoid infinite recursion
        if isinstance(cls, (int, float, str, bool)):
            return str(cls)

        try:
            if id(cls) in memo:
                if memo[id(cls)] != "processing":
                    return memo[id(cls)]
        except Exception as e:
            print(f"Error checking memo for id(self): {e}")
            return None

        # Add self to memo immediately with a temporary value
        # This is crucial to break recursion loops
        memo[id(cls)] = "processing"  # Temporary value

        # Create a string builder for this object
        content_str = f"{cls.__class__.__name__}:"

        # Create set of attributes to exclude
        excluded_attrs = {'outliner_expanded_h', 'expanded', 'hash', '_parent', '_children', 'kwargs', "tensor",
                          "tensor_b", "tensor_c", 'buffer', 'ctx',
                          'texture', "texture3D", "cuda_buffer", "xy_renderer", "xyz_renderer",
                          'previous_mouse_x', 'previous_mouse_y', 'last_mouse_x', 'last_mouse_y'}
        if exclude:
            for excl in exclude:
                excluded_attrs.add(excl)

        # Add all non-excluded attributes to the string representation
        if hasattr(cls, '__dict__'):
            for key, value in cls.__dict__.items():
                key = str(key)
                if not include_hidden:
                    if key.startswith('_'):
                        continue
                if key in excluded_attrs or value is None:
                    continue
                value_str = cls._hash_value_to_str(value, exclude, memo, depth, do_print,
                                                              include_hidden=include_hidden)
                content_str += f"{value_str}"
        else:
            content_str = cls._hash_value_to_str(cls, exclude, memo, depth, do_print,
                                                            include_hidden=include_hidden)

        # Calculate hash
        hash_result = hashlib.sha256(content_str.encode('utf-8')).hexdigest()

        # Convert the hash to a 16-bit float (float16)
        # Take the first 4 hex chars (16 bits) and convert to integer, then normalize to float16 range
        hash_int = int(hash_result[:4], 16)

        # Float16 has 1 sign bit, 5 exponent bits, and 10 mantissa bits
        # We'll use the range -65504 to +65504 (full range for float16)
        float_value = (hash_int / 0xFFFF) * 65504 * 2 - 65504

        # Update the memo with the final value
        memo[id(cls)] = float_value

        if do_print:
            print(f"Depth: {depth}, Class: {cls.__class__.__name__}, Hash: {hash_result[:8]}..., Float16: {float_value}")

        if hasattr(cls, 'hash'):
            cls.hash = float_value
        return float_value

    @staticmethod
    def simple_hash(value, exclude=None, memo=None, depth=0, do_print=False, include_hidden=False, internal=False):
        """
        Helper method to convert a value to a string representation based on its type.

        Args:
            value: The value to convert to string
            exclude: List of attribute names to exclude from hashing
            memo: Dictionary of already-processed objects
            depth: Current recursion depth for debugging
            do_print: Whether to print debug information

        Returns:
            A string representation of the value
        """

        # if not internal:
        #     return_val = Background.simple_hash(value=value, exclude=exclude, memo=memo, depth=depth, do_print=do_print,
        #                                         include_hidden=include_hidden, internal=True)
        #     hash_result = hashlib.sha256(return_val.encode('utf-8')).hexdigest()
        #
        #     # Convert the hash to a 16-bit float (Float16)
        #     # Take the first 4 hex chars (16 bits) and convert to integer, then normalize to float16 range
        #     hash_int = int(hash_result[:4], 16)
        #     return str(hash_int)

        if exclude is None:
            exclude = {}
        depth += 1

        if memo is None:
            memo = {}

        # Check if hashable first. Catch everything, not just TypeError: the
        # structural hashes are user objects (live-view frame buffers), and a
        # buggy __hash__ - e.g. reading an attribute a mid-__init__ capture
        # never set - must fall through to the structural path, not kill the
        # render.
        try:
            standard_hash = hash(value)
            return str(standard_hash)
        except Exception:
            pass

        if do_print:
            torch = sys.modules.get('torch')  # lazy: never load torch just for a debug print
            if torch is not None and hasattr(torch, 'cuda') and torch.cuda.is_available():
                mem_str = ""
                for i in range(torch.cuda.device_count()):
                    mem_alloc = torch.cuda.memory_allocated(i) / 1024 ** 3
                    mem_str += f"GPU {i}: {mem_alloc:.2f} GB\n"
                print(f"Depth: {depth}, Value: {value}, Type: {type(value)}, Memory: {mem_str}")
            else:
                print(f"Depth: {depth}, Value: {value}, Type: {type(value)}")

        # Check if value is already in memo - crucial for avoiding infinite recursion
        if id(value) in memo:
            return f"ref:{value}"  # Return a reference indicator instead of recursing

        # Handle None
        if value is None:
            return ""

        # Handle tensors and other special types by using their type and shape/identity
        if hasattr(value, "__class__") and value.__class__.__name__ == "Tensor":
            try:
                tensor_repr = f"Tensor:shape={list(value.shape)}:dtype={value.dtype}"
            except:
                tensor_repr = f"Tensor:{id(value)}"
            memo[id(value)] = tensor_repr
            return tensor_repr

        if hasattr(value, "__class__") and "PreTrainedTokenizerBase" in str(value.__class__.__mro__):
            tokenizer_repr = f"Tokenizer:{value.__class__.__name__}"
            memo[id(value)] = tokenizer_repr
            return tokenizer_repr

        if hasattr(value, "__class__") and "Module" in str(value.__class__.__mro__):
            module_repr = f"Module:{value.__class__.__name__}"
            memo[id(value)] = module_repr
            return module_repr

        # Handle DictConversion objects - pass the depth argument correctly
        if hasattr(value, "compute_hash") and isinstance(value, Background):
            memo[id(value)] = "processing"  # Add immediately to avoid recursion
            result = Background.compute_hash(self=value, exclude=exclude, memo=memo, depth=depth,
                                                 do_print=do_print, include_hidden=include_hidden)
            memo[id(value)] = result  # Update with actual result
            return str(result)

        # Handle Enums
        if hasattr(value, "__class__") and hasattr(value.__class__,
                                                   "__module__") and "enum" in value.__class__.__module__:
            try:
                enum_repr = f"Enum:{value.__class__.__name__}.{value.name}"
            except:
                enum_repr = f"Enum:{value.__class__.__name__}"
            memo[id(value)] = enum_repr
            return enum_repr

        if isinstance(value, bytes):
            bytes_repr = f"bytes:{len(value)}"
            memo[id(value)] = bytes_repr
            return bytes_repr

        if isinstance(value, bytearray):
            bytearray_repr = f"bytearray:{len(value)}"
            memo[id(value)] = bytearray_repr
            return bytearray_repr

        # Handle lists
        if isinstance(value, list):
            memo[id(value)] = "list:processing"  # Add immediately to avoid recursion
            items_str = "["
            for item in value:
                items_str += Background.simple_hash(value=item, exclude=exclude, memo=memo, depth=depth,
                                                               do_print=do_print,
                                                               include_hidden=include_hidden) + ","
            items_str += "]"
            memo[id(value)] = items_str
            return items_str

        if isinstance(value, Pending):
            memo[id(value)] = "tuple:processing"  # Add immediately to avoid recursion
            items_str = "("
            items_str += Background.simple_hash(value=Pending.wrapped, exclude=exclude, memo=memo, depth=depth,
                                                do_print=do_print, include_hidden=include_hidden) + ","
            items_str += ")"
            memo[id(value)] = items_str
            return items_str

        # Handle tuples
        if isinstance(value, tuple):
            memo[id(value)] = "tuple:processing"  # Add immediately to avoid recursion
            items_str = "("
            for item in value:
                items_str += Background.simple_hash(value=item, exclude=exclude, memo=memo, depth=depth,
                                                               do_print=do_print, include_hidden=include_hidden) + ","
            items_str += ")"
            memo[id(value)] = items_str
            return items_str

        # Fast path for dict subclasses that know how to hash themselves cheaply.
        # Implement __bg_hash__(self) -> str on any dict subclass to skip this recursion.
        if isinstance(value, dict) and hasattr(value, "__bg_hash__"):
            result = value.__bg_hash__()
            memo[id(value)] = result
            return result

        # Handle dictionaries
        if isinstance(value, dict):
            import time
            memo[id(value)] = "dict:processing"  # Add immediately to avoid recursion
            items_str = "{"
            for k, v in value.items():
                k = str(k)
                if not include_hidden and k.startswith('_'):
                    continue
                if k in exclude:
                    continue
                _t0 = time.perf_counter()
                val_str = Background.simple_hash(value=v, exclude=exclude, memo=memo, depth=depth,
                                                            do_print=do_print,
                                                            include_hidden=include_hidden)
                Background._record_dict_key_time(k, time.perf_counter() - _t0)
                items_str += f"{val_str}"
            items_str += "}"
            memo[id(value)] = items_str
            return items_str

        if isinstance(value, set):
            memo[id(value)] = "set:processing"  # Add immediately to avoid recursion
            items_str = "{"
            for item in value:
                items_str += str(item) + ","
            items_str += "}"
            memo[id(value)] = items_str
            return items_str

        # Handle simple types (int, float, str, bool)
        if isinstance(value, (int, float, str, bool)):
            result = str(value)
            memo[id(value)] = result
            return result

        # Any other types - use their string representation
        other_repr = f"{str(type(value).__name__)}"  # Just use ID to prevent recursion
        memo[id(value)] = other_repr
        return other_repr