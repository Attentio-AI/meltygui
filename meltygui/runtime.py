from enum import Enum
from typing import MutableMapping

import glfw
import imgui
import libcst as cst
from src.lsd.gl_gui.model.core_model.core_enums import generate_id


class Action:

    def __init__(self, trigger_condition, clear_condition, re_arm_condition=None):
        self.trigger_condition = trigger_condition
        self.clear_condition = clear_condition
        self.re_arm_condition = re_arm_condition


def drag_released(unique):
    mouse_released = imgui.is_mouse_released(0)
    if mouse_released:
        pass
    drag_released = (imgui.is_mouse_released(0) and
                     unique == Melty.triggered_actions.get('on_drag', None))
    return drag_released

class ActionType(Enum):
    CLICK = 'on_click'
    DOWN = 'on_mouse_down'
    DRAG = 'on_drag'
    DRAG_UP = 'on_drag_up'
    HOVERED = 'on_hover'

class MouseAction:
    def __init__(self, action_type: ActionType, button=0):
        self.action_type = action_type
        self.button = button

class OperationType(Enum):
    COPY = 'copy'
    MOVE = 'move'
    ADD = 'add'
    DELETE = 'delete'
    NAME_CHANGE = 'name_change'

class CollectionAction:
    def __init__(self,
                 target_unique = None,
                 source_unique=None,
                 target_tag=None,
                 target_key=None,
                 source_key=None,
                 source_collection=None,
                 target_collection=None,
                 operation=OperationType.MOVE):
        self.target_unique = target_unique
        self.target_key = target_key
        self.target_tag = target_tag
        self.target_collection = target_collection
        self.target_draw_state = None

        self.source_unique = source_unique
        self.source_key = source_key
        self.source_collection = source_collection
        self.source_draw_state = None

        self.operation = operation
        self.class_move = False
        self.target_class = None

    def print(self):
        print(f"CollectionAction: op {self.operation}\n"
              f"source_key {self.source_key}\n"
              f"source_unique {self.source_unique}\n"
              f"target_key {self.target_key}\n"
              f"target_unique {self.target_unique}\n"
              f"target_tag {self.target_tag}\n"
              f"source collection {type(self.source_collection).__name__}\n"
              f"target collection {type(self.target_collection).__name__}")


from enum import Enum


def add_to_collection(collection, item, preferred_key=None):
    """
    Add an item to a collection (list or dict).
    If a dict and preferred_key is given, use it if unique; else generate_id() until unique.
    Returns:
      - None on success, or an error message (str) on failure.
    """
    if hasattr(collection, "append_to"):
        collection.append_to(item)
        return collection
    elif isinstance(collection, list):
        collection.append(item)
        return None
    elif isinstance(collection, (dict, MutableMapping)):
        if hasattr(item, 'id'):
            preferred_key = item.id
        key = preferred_key
        if key is not None and key in collection:
            key = None
        if key is None:
            key = generate_id()
        collection[key] = item

    elif hasattr(collection, '__dict__'):
        collection = collection.__dict__
        if hasattr(item, 'id'):
            preferred_key = str(item.id)

        key = preferred_key
        if key is not None and key in collection:
            key = None
        if key is None:
            key = generate_id()
        collection[key] = item
    return collection


def delete_from_collection(key, collection):
    if isinstance(collection, list):
        try:
            idx = int(key)
            if 0 <= idx < len(collection):
                collection.pop(idx)
                return None
            else:
                return f"Index {idx} out of range for list of length {len(collection)}."
        except Exception as e:
            return f"Error removing index {key} from list: {e}"
    elif isinstance(collection, (dict, MutableMapping)):
        if key in collection:
            collection.pop(key)
            return None
        else:
            return f"Key {key!r} not found in dict."
    elif hasattr(collection, '__dict__'):
        collection = collection.__dict__
        if key in collection:
            collection.pop(key)
            return None
        else:
            return f"Key {key!r} not found in object's __dict__."


def _supports_reorder(mp) -> bool:
    return hasattr(mp, "reorder") and callable(getattr(mp, "reorder"))


def _compute_reordered_keys(mp, moving_key: str, anchor_key: str | None, tag: str) -> list[str]:
    keys = list(mp.keys())
    if moving_key in keys:
        keys.remove(moving_key)
    if anchor_key is not None and anchor_key in keys:
        idx = keys.index(anchor_key) + (1 if tag == "bottom" else 0)
    else:
        idx = 0 if tag == "top" else len(keys)
    keys.insert(idx, moving_key)
    return keys


def _reorder_keys_in_mapping(mp, keys: list[str]) -> bool:
    if _supports_reorder(mp):
        mp.reorder(keys)
        return True
    if isinstance(mp, dict):  # was: type(mp) is dict
        old = dict(mp)
        mp.clear()
        for k in keys:
            if k in old:
                mp[k] = old[k]
        for k, v in old.items():
            if k not in mp:
                mp[k] = v
        return True
    return False


def _insert_relative_in_mapping(mp, new_key: str, value, anchor_key: str | None, tag: str) -> bool:
    """
    Insert/ensure key and position it relative to anchor without destructive deletes.
    Returns True if positioned; False if mapping can't be safely reordered.
    """
    if new_key not in mp:
        mp[new_key] = value  # inserts at end (FolderProxy will create dir; others set value)
    keys = _compute_reordered_keys(mp, new_key, anchor_key, tag)
    return _reorder_keys_in_mapping(mp, keys)


def apply_collection_action(action: CollectionAction):
    """
    Returns:
        None on success, or an error message (str) on failure.

    Notes:
      - Uses action.source_unique / action.target_unique with resolved indices
        to infer list-unique bases and shift neighbor draw-states accordingly.
      - Records the moved/copied object's draw state in Melty.move_draw_state_pending
        as { id(obj): action.source_draw_state } for the render loop to remap.
    """

    # ---------------- helpers (no mutation) ----------------
    def _norm_tag(tag):
        if tag is None:
            return "top"
        t = str(tag).lower()
        return t if t in ("top", "bottom") else None

    def _get_existing_id(obj):
        if isinstance(obj, (dict, MutableMapping)) and "id" in obj:
            return str(obj["id"])
        maybe = getattr(obj, "id", None)
        return str(maybe) if maybe is not None else None

    def _resolve_list_index(lst, key_or_index):
        # numeric index?
        try:
            idx = int(key_or_index)
            return idx if 0 <= idx < len(lst) else None
        except Exception:
            pass
        # id string?
        needle = str(key_or_index)
        for i, el in enumerate(lst):
            if isinstance(el, (dict, MutableMapping)) and "id" in el and str(el["id"]) == needle:
                return i
            maybe = getattr(el, "id", None)
            if maybe is not None and str(maybe) == needle:
                return i
        return None

    def _supports_reorder(mp) -> bool:
        return hasattr(mp, "reorder") and callable(getattr(mp, "reorder"))

    def _looks_like_dir_value(val) -> bool:
        # Keep this narrow: FolderProxy directory value
        try:
            import FolderProxy  # or import at top
        except Exception:
            FolderProxy = ()
        return isinstance(val, FolderProxy)

    def _insert_pos_for_list(anchor_index, tag):
        return anchor_index if tag == "top" else anchor_index + 1

    def _unique_key_for_dict(d, preferred: str | None):
        if preferred and preferred not in d:
            return preferred
        gen = globals().get("generate_id")
        if not callable(gen):
            return None
        k = gen()
        while k in d:
            k = gen()
        return k

    # Draw-state of the moved/copied item itself (neighbors handled separately)
    def _record_draw_state(obj):
        try:
            if obj is None or action.source_draw_state is None:
                return
            if not hasattr(Melty, "move_draw_state_pending") or Melty.move_draw_state_pending is None:
                Melty.move_draw_state_pending = {}
            Melty.move_draw_state_pending[id(obj)] = action.source_draw_state
        except Exception:
            pass  # never break the transform

    # --- List neighbor shifting via inferred base (unique(i) = base + i) ---
    def _infer_base(known_unique, known_index):
        try:
            if isinstance(known_unique, int) and isinstance(known_index, int):
                return known_unique - known_index
        except Exception:
            pass
        return None

    def _shift_range_by_base(base: int | None, start_idx: int, end_idx: int, delta: int):
        """
        Shift draw_state_registry keys for indices [start_idx..end_idx] by `delta`,
        using unique(i) = base + i. No-ops if base is None.
        """
        if base is None or delta == 0 or start_idx > end_idx:
            return
        registry = Melty.vis.root.draw_state_registry
        # Stage moves to avoid collisions
        moves = []
        for i in range(start_idx, end_idx + 1):
            old_u = base + i
            ds = registry.get(old_u)
            if ds is not None:
                new_u = old_u + delta  # invariant: Δunique == Δindex
                moves.append((old_u, new_u, ds))
        # Remove then write
        for old_u, _, _ in moves:
            registry.pop(old_u, None)
        for _, new_u, ds in moves:
            if hasattr(ds, "unique"):
                ds.unique = new_u
            registry[new_u] = ds

    # ---------------- normalize inputs ----------------
    src_owner = action.source_collection
    dst_owner = action.target_collection
    if src_owner is None or dst_owner is None:
        return "Both source_collection and target_collection must be set on the action."

    # Capture owner types BEFORE any __dict__ coercion (for __field_defaults__)
    src_owner_type = type(src_owner)
    dst_owner_type = type(dst_owner)

    tag = _norm_tag(action.target_tag)
    if tag is None:
        return "target_tag must be 'top' or 'bottom'."

    op = action.operation.value if isinstance(action.operation, Enum) else str(action.operation).lower()
    if op not in ("move", "copy"):
        return "operation must be OperationType.MOVE or OperationType.COPY."
    is_move = (op == "move")

    # Work on raw containers (lists or dict views of objects)
    src = src_owner
    dst = dst_owner
    if not isinstance(src, (list, dict, MutableMapping)) and hasattr(src, "__dict__"):
        src = src.__dict__
    if not isinstance(dst, (list, dict, MutableMapping)) and hasattr(dst, "__dict__"):
        dst = dst.__dict__

    same_collection = (src is dst)

    # If destination is a dict but CLASS exposes a shared __field_defaults__,
    # reorder *dst* to match class_defaults order (order-only; no insertion/rebinding).
    if (not isinstance(dst, list)
            and hasattr(dst_owner_type, "__field_defaults__")
            and type(dst) is dict):
        class_defaults = dst_owner_type.__field_defaults__
        ordered_keys = [k for k in class_defaults.keys() if k in dst]
        extra_keys = [k for k in list(dst.keys()) if k not in class_defaults]
        if ordered_keys or extra_keys:
            old = dict(dst)
            dst.clear()
            for k in ordered_keys:
                dst[k] = old[k]
            for k in extra_keys:
                dst[k] = old[k]

    # ---------------- six explicit cases ----------------

    # 1) LIST -> LIST (includes list-to-self)
    if isinstance(src, list) and isinstance(dst, list):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")

        if len(dst) == 0:
            t_idx = 0
        else:
            t_idx = _resolve_list_index(dst, action.target_key)
            if t_idx is None:
                t_idx = len(dst) - 1  # last element as anchor

        # infer bases from (unique, index)
        base_same = _infer_base(action.source_unique, s_idx) if same_collection else None
        base_src = _infer_base(action.source_unique, s_idx) if not same_collection else None
        base_dst = _infer_base(action.target_unique, t_idx) if not same_collection else None

        # capture lengths BEFORE mutation
        src_len_before = len(src)
        dst_len_before = len(dst)

        # compute insert index (adjust if same list and move across pop)
        insert_at = _insert_pos_for_list(t_idx, tag)
        if is_move and same_collection:
            base = t_idx if tag == "top" else t_idx + 1
            if s_idx < base:
                base -= 1
            insert_at = max(0, min(base, len(dst)))

        item = src[s_idx]
        if is_move and same_collection:
            popped = src.pop(s_idx)
            try:
                dst.insert(insert_at, popped)
            except Exception as e:
                src.insert(s_idx, popped)
                return f"Internal error during same-list move insert: {e}"

            # neighbors in SAME list
            if insert_at < s_idx:
                _shift_range_by_base(base_same, start_idx=insert_at, end_idx=s_idx - 1, delta=+1)
            elif insert_at > s_idx:
                _shift_range_by_base(base_same, start_idx=s_idx + 1, end_idx=insert_at, delta=-1)

            _record_draw_state(popped)

        else:
            # cross-list copy/move OR same-list copy
            try:
                dst.insert(insert_at, item)
            except Exception as e:
                return f"Internal error inserting into target list: {e}"

            # target neighbors shift right from insert_at
            _shift_range_by_base(base_dst, start_idx=insert_at, end_idx=dst_len_before - 1, delta=+1)

            if is_move:
                try:
                    src.pop(s_idx)
                except Exception as e:
                    # rollback best-effort
                    try:
                        dst.pop(insert_at)
                    except Exception:
                        pass
                    return f"Internal error removing from source after insert: {e}"

                # source neighbors collapse left after s_idx
                _shift_range_by_base(base_src, start_idx=s_idx + 1, end_idx=src_len_before - 1, delta=-1)

            _record_draw_state(item)

    # 2) DICT -> DICT (reorder or transfer)
    elif isinstance(src, (dict, MutableMapping)) and isinstance(dst, (dict, MutableMapping)):
        s_key = action.source_key
        t_key = action.target_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."

        if t_key is None and len(dst) > 0:
            t_key = next(iter(dst.keys()))

        value = src[s_key]

        if same_collection:
            # pure reorder; never use clear/pop on mappings that might have side-effects
            if not (s_key == t_key or t_key is None):
                keys = _compute_reordered_keys(dst, s_key, t_key, tag)
                ok = _reorder_keys_in_mapping(dst, keys)
                if not ok:
                    return "Cannot safely reorder this mapping without destructive deletes."
                _record_draw_state(value)
        else:
            final_key = s_key
            if s_key in dst:
                is_move = False  # collision -> copy

            # If the source owner exposes a true move, use it (duck-typed; generic)
            if is_move and hasattr(src_owner, "move_item") and callable(getattr(src_owner, "move_item")):
                try:
                    # perform the physical move; returns the final key name at dst
                    final_key = src_owner.move_item(dst_owner, s_key, new_name=s_key)
                    # position it relative to t_key without destructive deletes
                    _insert_relative_in_mapping(dst, final_key, dst[final_key], t_key, tag)
                    _record_draw_state(dst[final_key])
                    return
                except NotImplementedError:
                    pass
                except Exception as e:
                    # Fall back to safe copy semantics if hook fails
                    is_move = False

            # No move hook: do a safe insert+reorder only
            ok = _insert_relative_in_mapping(dst, final_key, value, t_key, tag)
            if not ok:
                if type(dst) is dict:
                    tmp = dict(dst)
                    tmp[final_key] = value
                    keys = _compute_reordered_keys(tmp, final_key, t_key, tag)
                    dst.clear()
                    for k in keys:
                        dst[k] = tmp[k]
                else:
                    return "Target mapping cannot be reordered safely."

            # IMPORTANT: never pop a value item unless we actually moved it
            if is_move:
                if _looks_like_dir_value(value) and (_supports_reorder(src) or _supports_reorder(dst)):
                    # Treat as copy for safety (we didn't really move on disk)
                    is_move = False
                else:
                    src.pop(s_key, None)

            _record_draw_state(value)

    # 3) DICT -> LIST (insert into list)
    elif isinstance(src, (dict, MutableMapping)) and isinstance(dst, list):
        s_key = action.source_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."
        item = src[s_key]

        dst_len_before = len(dst)
        if dst_len_before == 0:
            t_idx = 0
        else:
            t_idx = _resolve_list_index(dst, action.target_key)
            if t_idx is None:
                t_idx = len(dst) - 1
        insert_at = max(0, min(_insert_pos_for_list(t_idx, tag), len(dst)))

        base_dst = _infer_base(action.target_unique, t_idx)

        try:
            dst.insert(insert_at, item)
        except Exception as e:
            return f"Internal error inserting into list: {e}"

        # target neighbors shift right
        _shift_range_by_base(base_dst, start_idx=insert_at, end_idx=dst_len_before - 1, delta=+1)

        if is_move:
            src.pop(s_key, None)

        _record_draw_state(item)

    # 4) LIST -> DICT (remove from list)
    elif isinstance(src, list) and isinstance(dst, (dict, MutableMapping)):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")
        item = src[s_idx]

        src_len_before = len(src)
        base_src = _infer_base(action.source_unique, s_idx)

        t_anchor = action.target_key
        if t_anchor is not None and len(dst) > 0 and t_anchor not in dst:
            t_anchor = list(dst.keys())[-1]

        preferred_id = _get_existing_id(item)
        new_key = _unique_key_for_dict(dst, preferred_id)
        if new_key is None:
            return "generate_id() is not available to create a unique key for list->dict."

        ok = _insert_relative_in_mapping(dst, new_key, item, t_anchor, tag)
        if not ok:
            # Fallback for plain dicts only
            if type(dst) is dict:
                tmp = dict(dst)
                if new_key not in tmp:
                    tmp[new_key] = item
                keys = _compute_reordered_keys(tmp, new_key, t_anchor, tag)
                dst.clear()
                for k in keys:
                    dst[k] = tmp[k]
            else:
                return "Target mapping cannot be reordered safely."

        if is_move:
            try:
                src.pop(s_idx)
            except Exception as e:
                # rollback best-effort
                try:
                    if new_key in dst:
                        del dst[new_key]
                except Exception:
                    pass
                return f"Internal error removing from source list after dict insert: {e}"

            # collapse gap in source list
            _shift_range_by_base(base_src, start_idx=s_idx + 1, end_idx=src_len_before - 1, delta=-1)

        _record_draw_state(item)

    else:
        return "Unsupported collection types. Expected list or dict for both source and target."

    # ---------------- reflect order into __field_defaults__ (order-only, in place) ----------------
    if not isinstance(dst, list) and hasattr(dst_owner_type, "__field_defaults__"):
        class_defaults = dst_owner_type.__field_defaults__
        dst_keys = list(dst.keys())
        defaults_keys = list(class_defaults.keys())

        common_in_dst_order = [k for k in dst_keys if k in class_defaults]
        defaults_only_tail = [k for k in defaults_keys if k not in dst]

        new_order = common_in_dst_order + defaults_only_tail
        if new_order != defaults_keys:
            old_vals = {k: class_defaults[k] for k in class_defaults.keys()}
            class_defaults.clear()
            for k in new_order:
                class_defaults[k] = old_vals.get(k)

    return None


class DepthState:
    def __init__(self):
        self.flow_spacing = 0.0


class MeltyState:
    def __init__(self):
        self.hover_stack = []
        self.hotkey_stack = []
        self.triggered_actions = {}
        self.dragged_item = None
        self.max_distance = 200

        self.selected_views = {}
        self.drag_in_progress = False

        self.initial_drag_offset = (0, 0)
        self.mouse_down_pos = (0, 0)
        self.total_drag_distance = 0.0
        self.total_drag_frames = 0
        self.last_mouse_pos = None
        self.drag_delta = (0,0)

        self.nearest_drop_target = None
        self.nearest_drop_target_tag = None
        self.nearest_drop_distance = self.max_distance
        self.flow_spacing = 0.0

        self.drag_drop_target = None
        self.drag_drop_target_tag = None
        self.drag_target_key = None
        self.drag_target_collection = None
        self.drag_drop_action = CollectionAction()

        self.target_distance = self.max_distance

        self.actions_to_apply = []
        self.items_to_delete = []

    def check_event(self, unique, mouse_btn, event_type):
        if unique in self.triggered_actions:
            action = self.triggered_actions[unique]
            if action.action_type == event_type and action.button == mouse_btn:
                return True
        return False
    def mark_event(self, unique, mouse_btn, event_type: ActionType):
        self.triggered_actions[unique] = MouseAction(event_type, mouse_btn)

    def to_apply(self, action: CollectionAction):
        self.actions_to_apply.append(action)

    def to_delete(self, key, collection):
        self.items_to_delete.append((key, collection))

class Melty:
    max_depth = 40
    indent_size = 10
    annotation_mode = True
    depth = 0
    current_indent = 0
    indent_count = 0
    unindent_count = 0

    max_indent = 0
    hotkey_registry = {}
    move_draw_state_pending = {}

    # LibCST tracking -----------------------------------------
    _path_stack: list[tuple[str, int | None]] = []  # (field, idx)
    _root_by_module: dict[str, cst.Module] = {}
    _gen_by_module: dict[str, int] = {}

    save_draw_state_for = 1
    spacing = (3, 1)
    padding = (3, 3)
    end_collection_spacing = 5
    collection_spacing = 5
    header_indent = 150

    vis = None
    type_defaults = {}
    unique_stack = [0] * max_depth
    size_stack = []
    window_stack = []
    window_hovered = False
    global_attrs = {}
    depth_state_stack = []
    flow_spacing = 0.0
    bg_stack = []
    draw_state_stack = []
    input_value_stack = [None]

    @classmethod
    def begin_frame(cls, module_id: str, root: cst.Module):
        cls._root_by_module[module_id] = root
        cls._gen_by_module.setdefault(module_id, 0)
        cls._path_stack.clear()

    @classmethod
    def current_path(cls) -> tuple[tuple[str, int | None], ...]:
        return tuple(cls._path_stack)

    @classmethod
    def push_slot(cls, field: str, idx: int | None):
        cls._path_stack.append((field, idx))

    @classmethod
    def pop_slot(cls):
        cls._path_stack.pop()

    @classmethod
    def current_root(cls, module_id: str) -> cst.Module:
        return cls._root_by_module[module_id]

    @classmethod
    def bump_gen(cls, module_id: str):
        cls._gen_by_module[module_id] += 1

    @classmethod
    def current_gen(cls, module_id: str) -> int:
        return cls._gen_by_module[module_id]



    # LibCST tracking -----------------------------------------



    @classmethod
    def indent(cls, amount):
        if amount == 0:
            return
        cls.indent_count += 1
        cls.current_indent += amount
        cls.max_indent = max(cls.max_indent, cls.current_indent)
        imgui.indent(amount)

    @classmethod
    def unindent(cls, amount):
        if amount == 0:
            return
        cls.current_indent -= amount
        imgui.unindent(amount)
        cls.unindent_count += 1

    @classmethod
    def inside_window(cls):
        return len(cls.window_stack) > 0

    @classmethod
    def shift_down(cls):
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                     glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)



    @classmethod
    def is_window_enabled(cls):
        if len(cls.window_stack) == 0:
            return True
        return cls.window_stack[-1][1]

    @classmethod
    def get_bg_color(cls, depth=None):
        if depth is None:
            depth = cls.depth
        if len(cls.bg_stack) == 0:
            return 0, 0, 0

        # Allow for negative index from end, but clamp to available range
        if depth < 0:
            depth = len(cls.bg_stack) + depth
        depth = max(0, min(depth, len(cls.bg_stack) - 1))
        return cls.bg_stack[depth][0:3]

    @classmethod
    def shift_key(cls):
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @classmethod
    def ctrl_key(cls,):
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_CONTROL) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_CONTROL) == glfw.PRESS)


    @classmethod
    def init(cls, **kwargs):
        for key, value in kwargs.items():
            setattr(cls, key, value)
            cls.global_attrs[key] = value

        cls.annotation_mode = False

    @classmethod
    def is_key_pressed(cls, key=glfw.KEY_ESCAPE):
        if imgui.is_any_item_focused() or imgui.is_any_item_active():
            if not cls.ctrl_key():
            # If any item is focused or active, we don't want to capture key presses
                return False

        if key not in cls.vis.tracked_keys:
            cls.vis.tracked_keys.append(key)
            cls.vis.first_frame_keys.add(key)

        if glfw.get_key(cls.vis.window, key) == glfw.PRESS:
            if key in cls.vis.first_frame_keys:
                return True
        return False
