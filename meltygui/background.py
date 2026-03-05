import hashlib
import io
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import torch

from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace, get_live_frames, _print_lock, trace_group, request_render
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState


class Background:
    _user_cache = {}  # user_id -> OrderedDict{hash -> result}
    _cache_size = 1
    _active = set()
    _user_tasks = {}
    _lock = threading.Lock()
    _pool = ThreadPoolExecutor(max_workers=16)
    _debounce_timers = {}   # debounce_key -> Timer
    _debounce_latest = {}   # debounce_key -> dict of latest call params

    @classmethod
    def shutdown(cls):
        with cls._lock:
            cls._active.clear()
            cls._user_tasks.clear()
            cls._user_cache.clear()
            for timer in cls._debounce_timers.values():
                timer.cancel()
            cls._debounce_timers.clear()
            cls._debounce_latest.clear()

        print("Shutting down background thread pool...")
        cls._pool.shutdown(wait=True)
        print("Background thread pool shut down successfully.")


    @classmethod
    def run(cls, func, user_id, no_cache=False, invalidate_id=None, on_frame=None, frames=None, debounce=6, draw_state=None, *args, **kwargs):
        h = cls.simple_hash(value=(kwargs.get("value", None))) + user_id

        from src.lsd.gl_gui.melty import Melty
        if Melty.frame_count < 2:
            debounce = None

        if kwargs.get("apply", False):
            no_cache = True

        # --- debounce path ---
        if debounce is not None:
            debounce_key = user_id

            with cls._lock:
                # Cache hit - return immediately, no wait
                cache = cls._user_cache.get(user_id)
                if cache and h in cache:
                    cache.move_to_end(h)
                    return_val = cache[h]
                    if no_cache:
                        cls._user_cache.pop(user_id, None)
                    return return_val

                # Task is already running, just wait for it
                if h in cls._active:
                    return Pending(status="background thread active", state=PendingState.BACKGROUND)

                # Same hash already pending - don't reset the timer, just keep waiting
                latest = cls._debounce_latest.get(debounce_key)
                if latest is not None and latest.get("hash") == h:
                    return Pending(status="debounce waiting", state=PendingState.BACKGROUND)

                # New content - cancel existing timer and reschedule
                prev = cls._debounce_timers.pop(debounce_key, None)
                if prev is not None:
                    prev.cancel()

                cls._debounce_latest[debounce_key] = dict(
                    hash=h,
                    func=func, user_id=user_id, no_cache=no_cache,
                    invalidate_id=invalidate_id, on_frame=on_frame,
                    args=args, kwargs=kwargs,
                )

                if Toggles.debug_threads:
                    frames = get_live_frames()

                def _fire():
                    with cls._lock:
                        cls._debounce_timers.pop(debounce_key, None)
                        latest = cls._debounce_latest.pop(debounce_key, None)
                    if latest is not None:
                        cls.run(
                            latest["func"], latest["user_id"],
                            no_cache=latest["no_cache"],
                            invalidate_id=latest["invalidate_id"],
                            on_frame=latest["on_frame"],
                            debounce=None, frames=frames,
                            *latest["args"], **latest["kwargs"],
                        )
                        if latest["invalidate_id"] is not None:
                            from src.lsd.gl_gui.melty import Melty
                            from src.lsd.gl_gui.utils.glfw_utils import request_render
                            Melty.cache.invalidate_up(latest["invalidate_id"])
                            request_render()

                timer = threading.Timer(debounce / 1000.0, _fire)
                cls._debounce_timers[debounce_key] = timer
                timer.start()

            return Pending(status="debounce waiting", state=PendingState.BACKGROUND)

        # --- normal path (unchanged below) ---

        with cls._lock:
            cache = cls._user_cache.get(user_id)
            if cache and h in cache:
                cache.move_to_end(h)
                return_val = cache[h]
                if no_cache:
                    cls._user_cache.pop(user_id, None)
                return return_val

            cls._user_tasks[user_id] = h

            if h in cls._active:
                return Pending(status="background thread active", state=PendingState.BACKGROUND)

            if Toggles.debug_threads and frames is None:
                frames = get_live_frames()
            cls._active.add(h)

        def _task():
            try:
                result = func(*args, **kwargs)
                if on_frame is not None:
                    result = (result, on_frame)
                with cls._lock:
                    cls._active.discard(h)
                    for uid, task_h in cls._user_tasks.items():
                        if task_h == h:
                            if uid not in cls._user_cache:
                                cls._user_cache[uid] = OrderedDict()
                            cls._user_cache[uid][h] = result
                            cls._user_cache[uid].move_to_end(h)


                            while len(cls._user_cache[uid]) > cls._cache_size:
                                cls._user_cache[uid].popitem(last=False)
                if invalidate_id is not None:
                    from src.lsd.gl_gui.melty import Melty
                    from src.lsd.gl_gui.utils.glfw_utils import request_render
                    if on_frame is None or abs(Melty.frame_count - on_frame) >= 1:
                        Melty.cache.invalidate_up(invalidate_id)
                        request_render()

            except Exception as e:
                with cls._lock:
                    cls._active.discard(h)

                with trace_group(f"JOB {user_id}", hash=h) as g:
                    print_stack_trace(frames=frames, section="UI Thread",
                                      group=g, watch=["draw_state.name", "input_value", "convert_path", "clean_args.input_value"])
                    print_stack_trace(exception=e, section="Background Thread",
                                      group=g, watch=["value", "path", "result"])


        cls._pool.submit(_task)
        return Pending(status="background thread", state=PendingState.BACKGROUND)

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
            exclude = {}
        import hashlib

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
                # Exclude private attributes (starting with underscore)
                key = str(key)

                if not include_hidden:
                    if key.startswith('_'):
                        continue
                if key in excluded_attrs or value is None:
                    continue
                # Get string representation of the value
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
            print(
                f"Depth: {depth}, Class: {cls.__class__.__name__}, Hash: {hash_result[:8]}..., Float16: {float_value}")

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

        # Check if hashable first
        try:
            standard_hash = hash(value)
            return str(standard_hash)
        except TypeError:
            pass

        if do_print:
            if hasattr(torch, 'cuda') and torch.cuda.is_available():
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

        # Handle dictionaries
        if isinstance(value, dict):
            memo[id(value)] = "dict:processing"  # Add immediately to avoid recursion
            items_str = "{"
            for k, v in value.items():
                k = str(k)
                if not include_hidden and k.startswith('_'):
                    continue
                if k in exclude:
                    continue
                # Convert the key to string representation
                # Get value string representation
                val_str = Background.simple_hash(value=v, exclude=exclude, memo=memo, depth=depth,
                                                            do_print=do_print,
                                                            include_hidden=include_hidden)
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

