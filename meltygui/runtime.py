from enum import Enum

import glfw
import imgui

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

        self.source_unique = source_unique
        self.source_key = source_key
        self.source_collection = source_collection

        self.operation = operation

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


def apply_collection_action(action: CollectionAction):
    """
    Returns:
        None on success, or an error message (str) on failure.

    Semantics:
      - COPY: insert the same object reference into target; source unchanged.
      - MOVE: same insert + remove from source.
      - 'top' = before anchor; 'bottom' = after anchor.

    Supports:
      - list <-> list  (now accepts index OR id-string anchors for lists of dicts/objects)
      - dict <-> dict  (reorder within same dict, or transfer; cross-dict collisions rekey via obj.id or generate_id())
      - dict <-> list  (both directions; list anchors may be index OR id string)

    Behavior:
      - Validation-first; no mutation until all checks pass.
      - No raises; returns error strings and leaves collections untouched.
      - Same-dict downward reorders fixed (skip original key during rebuild).
    """

    # ---------------- helpers (no mutation) ----------------
    def _norm_tag(tag):
        if tag is None:
            return "top"
        t = str(tag).lower()
        return t if t in ("top", "bottom") else None

    def _get_existing_id(obj):
        # Use an object's "id" if available (dict["id"] or obj.id)
        if isinstance(obj, dict) and "id" in obj:
            return str(obj["id"])
        maybe = getattr(obj, "id", None)
        if maybe is not None:
            return str(maybe)
        return None

    def _resolve_list_index(lst, key_or_index):
        """
        Try to resolve a list anchor as:
          1) integer index (0..len-1), or
          2) id string matching element['id'] or element.id (first match).
        Returns an int index, or None if not found/resolvable.
        """
        # Case 1: numeric index
        try:
            idx = int(key_or_index)
            return idx if 0 <= idx < len(lst) else None
        except Exception:
            pass

        # Case 2: id string
        needle = str(key_or_index)
        for i, el in enumerate(lst):
            if isinstance(el, dict) and "id" in el and str(el["id"]) == needle:
                return i
            maybe = getattr(el, "id", None)
            if maybe is not None and str(maybe) == needle:
                return i
        return None

    def _insert_pos_for_list(anchor_index, tag):
        # For validated anchor_index in [0, len-1]
        return anchor_index if tag == "top" else anchor_index + 1

    def _unique_key_for_dict(d: dict, preferred: str | None):
        # Prefer provided id if unique; else use generate_id() until unique
        if preferred and preferred not in d:
            return preferred
        gen = globals().get("generate_id")
        if not callable(gen):
            return None
        k = gen()
        while k in d:
            k = gen()
        return k

    # ---------------- phase 1: validate and plan ----------------
    src = action.source_collection
    dst = action.target_collection
    if src is None or dst is None:
        return "Both source_collection and target_collection must be set on the action."

    tag = _norm_tag(action.target_tag)
    if tag is None:
        return "target_tag must be 'top' or 'bottom'."

    op = action.operation.value if isinstance(action.operation, Enum) else str(action.operation).lower()
    if op not in ("move", "copy"):
        return "operation must be OperationType.MOVE or OperationType.COPY."
    is_move = (op == "move")

    same_collection = (src is dst)
    plan = {"kind": None}

    # list -> list
    if isinstance(src, list) and isinstance(dst, list):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")
        t_idx = _resolve_list_index(dst, action.target_key)
        if t_idx is None:
            return (f"Target key {action.target_key!r} not found in target list "
                    f"as index or id (len={len(dst)}). If you intended to move into a dict, "
                    f"pass the parent dict as target_collection and a dict key as target_key.")

        insert_at = _insert_pos_for_list(t_idx, tag)
        if is_move and same_collection:
            # Adjust for index shift after pop
            base = t_idx if tag == "top" else t_idx + 1
            if s_idx < base:
                base -= 1
            insert_at = max(0, min(base, len(dst)))

        plan.update(kind="list->list", s_idx=s_idx, insert_at=insert_at)

    # dict -> dict
    elif isinstance(src, dict) and isinstance(dst, dict):
        s_key = action.source_key
        t_key = action.target_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."
        if t_key not in dst:
            return f"Target anchor key {t_key!r} not found in target dict."

        if same_collection:
            if s_key == t_key:
                plan.update(kind="noop")  # copying/reordering on itself is a no-op
            else:
                plan.update(kind="dict->dict-reorder", s_key=s_key, t_key=t_key)
        else:
            # Transfer; resolve collisions in target
            item = src[s_key]
            final_key = s_key
            if s_key in dst:
                preferred = _get_existing_id(item)
                final_key = _unique_key_for_dict(dst, preferred)
                if final_key is None:
                    return "generate_id() unavailable or failed to produce a unique key for dict->dict transfer."
            plan.update(kind="dict->dict-xfer", s_key=s_key, t_key=t_key, final_key=final_key)

    # dict -> list
    elif isinstance(src, dict) and isinstance(dst, list):
        s_key = action.source_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."
        t_idx = _resolve_list_index(dst, action.target_key)
        if t_idx is None:
            return (f"Target key {action.target_key!r} not found in target list "
                    f"as index or id (len={len(dst)}).")
        insert_at = _insert_pos_for_list(t_idx, tag)
        plan.update(kind="dict->list", s_key=s_key, insert_at=insert_at)

    # list -> dict
    elif isinstance(src, list) and isinstance(dst, dict):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")
        t_anchor = action.target_key
        if t_anchor not in dst:
            return f"Target anchor key {t_anchor!r} not found in target dict."

        item = src[s_idx]
        preferred_id = _get_existing_id(item)
        new_key = _unique_key_for_dict(dst, preferred_id)
        if new_key is None:
            return "generate_id() is not available to create a unique key for list->dict."
        plan.update(kind="list->dict", s_idx=s_idx, t_anchor=t_anchor, new_key=new_key)

    else:
        return "Unsupported collection types. Expected list or dict for both source and target."

    # ---------------- phase 2: execute (mutate) ----------------
    try:
        kind = plan["kind"]

        if kind == "noop":
            return None

        # list -> list
        if kind == "list->list":
            s_idx = plan["s_idx"]
            insert_at = max(0, min(plan["insert_at"], len(dst)))
            print(f"insert at {insert_at} from {s_idx}")
            item = src[s_idx]

            if is_move and same_collection:
                print("same-list move")
                popped = src.pop(s_idx)
                try:
                    dst.insert(insert_at, popped)
                except Exception as e:
                    src.insert(s_idx, popped)  # rollback
                    return f"Internal error during same-list move insert: {e}"
            else:
                dst.insert(insert_at, item)
                if is_move:
                    try:
                        src.pop(s_idx)
                    except Exception as e:
                        # rollback the insert
                        try:
                            dst.pop(insert_at)
                        except Exception:
                            pass
                        return f"Internal error removing from source after insert: {e}"

        # dict -> dict reorder within the same dict (fixed for downward moves)
        elif kind == "dict->dict-reorder":
            s_key = plan["s_key"]
            t_key = plan["t_key"]
            value = src[s_key]

            new_d = {}
            for k, v in dst.items():
                if k == s_key:
                    # Skip old instance of s_key; we'll insert it relative to t_key
                    continue

                if tag == "top" and k == t_key:
                    new_d[s_key] = value
                new_d[k] = v
                if tag == "bottom" and k == t_key:
                    new_d[s_key] = value

            # Safety: ensure s_key exists even if something odd happens
            if s_key not in new_d:
                new_d[s_key] = value

            dst.clear()
            dst.update(new_d)
            # NOTE: COPY within same dict is effectively a reorder (no duplicate keys).

        # dict -> dict transfer (possibly rekeyed) between different dicts
        elif kind == "dict->dict-xfer":
            s_key = plan["s_key"]
            t_key = plan["t_key"]
            final_key = plan["final_key"]
            value = src[s_key]

            new_d = {}
            for k, v in dst.items():
                if tag == "top" and k == t_key:
                    new_d[final_key] = value
                new_d[k] = v
                if tag == "bottom" and k == t_key:
                    new_d[final_key] = value

            dst.clear()
            dst.update(new_d)

            if is_move:
                src.pop(s_key, None)

        # dict -> list
        elif kind == "dict->list":
            s_key = plan["s_key"]
            item = src[s_key]
            insert_at = max(0, min(plan["insert_at"], len(dst)))
            dst.insert(insert_at, item)
            if is_move:
                src.pop(s_key, None)

        # list -> dict
        elif kind == "list->dict":
            s_idx = plan["s_idx"]
            t_anchor = plan["t_anchor"]
            new_key = plan["new_key"]
            item = src[s_idx]

            new_d = {}
            for k, v in dst.items():
                if tag == "top" and k == t_anchor:
                    new_d[new_key] = item
                new_d[k] = v
                if tag == "bottom" and k == t_anchor:
                    new_d[new_key] = item

            dst.clear()
            dst.update(new_d)

            if is_move:
                try:
                    src.pop(s_idx)
                except Exception as e:
                    # rollback: remove inserted key
                    try:
                        tmp = {k: v for k, v in dst.items() if k != new_key}
                        dst.clear()
                        dst.update(tmp)
                    except Exception:
                        pass
                    return f"Internal error removing from source list after dict insert: {e}"

        else:
            return "Internal planning error: unknown operation kind."

    except Exception as e:
        # Defensive catch-all: report without raising; collections should be intact or rolled back.
        return f"Unexpected error during apply: {e}"

    return None

class MeltyState:
    def __init__(self):
        self.hover_stack = []
        self.triggered_actions = {}
        self.dragged_item = None
        self.max_distance = 200

        self.selected_views = {}
        self.drag_in_progress = False

        self.initial_drag_offset = (0, 0)
        self.nearest_drop_target = None
        self.nearest_drop_target_tag = None
        self.nearest_drop_distance = self.max_distance

        self.drag_drop_target = None
        self.drag_drop_target_tag = None
        self.drag_target_key = None
        self.drag_target_collection = None
        self.drag_drop_action = CollectionAction()

        self.target_distance = self.max_distance

        self.actions_to_apply = []

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

class Melty:
    max_depth = 40
    annotation_mode = True
    depth = 0
    current_indent = 0

    @staticmethod
    def indent(amount):
        Melty.current_indent += amount
        imgui.indent(amount)

    @staticmethod
    def unindent(amount):
        Melty.current_indent -= amount
        imgui.unindent(amount)

    @staticmethod
    def inside_window():
        return len(Melty.window_stack) > 0

    @staticmethod
    def shift_down():
        return (glfw.get_key(Melty.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                     glfw.get_key(Melty.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    save_draw_state_for = 1
    spacing = (3,1)
    padding = (3,2)
    end_collection_spacing = 5

    vis = None
    type_defaults = {}
    unique_stack = []
    window_stack = []

    @staticmethod
    def shift_key():
        return (glfw.get_key(Melty.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(Melty.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @staticmethod
    def init(**kwargs):
        for key, value in kwargs.items():
            setattr(Melty, key, value)
        Melty.annotation_mode = False

    @staticmethod
    def is_key_pressed(key=glfw.KEY_ESCAPE):
        if imgui.is_any_item_focused() or imgui.is_any_item_active():
            # If any item is focused or active, we don't want to capture key presses
            return False

        if key not in Melty.vis.tracked_keys:
            Melty.vis.tracked_keys.append(key)
            Melty.vis.first_frame_keys.add(key)

        if glfw.get_key(Melty.vis.window, key) == glfw.PRESS:
            if key in Melty.vis.first_frame_keys:
                return True
        return False
