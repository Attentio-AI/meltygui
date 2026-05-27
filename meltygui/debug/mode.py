import types
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Any

from src.lsd.gl_gui.model.core_model.draw_state import Anchor
from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.toggles import WindowManager
from src.lsd.gl_gui.view.core_conversion.chain_converters import module_to_address, address_to_general_parse, \
    general_parse_to_address, address_to_module, class_to_address, address_to_class, function_to_address, \
    address_to_function, general_parse_to_str, str_to_general_parse, focus
from src.lsd.gl_gui.view.core_conversion.file_converters import path_to_dict, bytes_to_str, load_text, recompile_module, \
    recompile, fn_to_cst, cst_to_fn, recompile_fn, \
    mod_to_cst, cst_to_mod, recompile_mod_fn, \
    cls_to_cst, cst_to_cls, recompile_cls_fn, \
    rf_dict_to_path, rf_str_to_bytes, rf_dict_to_str, load_file_bytes
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import GeneralParse, Conditional, Comment, \
    cst_to_dict, dict_to_cst, cst_module_to_str, str_to_cst_module
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_footer, draw_header_end, draw_header
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection, draw_comment, \
    sort_dict_alphabetically, unsort_dict_alphabetically, draw_with_modes, draw_type, \
    class_to_var_dict, var_dict_to_class, draw_dropdown, draw_blank, draw_drop_down_item
from src.lsd.gl_gui.view.core_views.text_editor import draw_text


def compute_height(draw_state):
    if draw_state._parent is not None:
        top_offset = draw_state.abs_top - draw_state._parent.abs_top
        return draw_state._parent.height - top_offset - 40
    else:
        return 500

@dataclass
class ModeOverrides:
    kwargs: Optional[dict] = None
    func: Optional[callable] = None
    recursive:Optional[bool] = True

@window
class Mode(Enum):
    def get_config_for(self, input_value=None, the_type=None):
        if input_value is not None:
            the_type = type(input_value)

        config = self.unwrapped.get(the_type, None)
        if config is not None:
            return config

        for super_type in type(input_value).__mro__:
            config = self.unwrapped.get(super_type, None)
            if config is not None:
                return config

        if Any in self.unwrapped:
            return self.unwrapped[Any]
        return None

    def __init__(self, *args, **kwargs):
        unwrapped = {}
        if isinstance(self.value, dict):
            for key, value in self.value.items():
                if isinstance(key, tuple):
                    for sub_key in key:
                        unwrapped[sub_key] = value
                else:
                    unwrapped[key] = value
            self.unwrapped = unwrapped


    # [tint=(0.2,0.1,0.1)]
    SORT = {
        defaultdict: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True},
            recursive=True,
            func=(sort_dict_alphabetically,
                  (draw_collection, {"show_add_delete":False, "selectable":False, "show_bg": False}),

          ))
    }


    DROPDOWN_WINDOW = {
        (Any): ModeOverrides(
            kwargs={"show_bg": False, "selectable": False, "min_width": 10, "wrap":True, "z_offset":0,
                    "shadow":False, "is_tree": False, "use_cache": True},
            recursive=False,
            func=draw_drop_down_item
        ),
        (dict, list, tuple): ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False,
                    "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": True, "draggable": True,
                    'shadow': True, "return_item": True,
                    "show_tint": False, "show_header": False, 'indent_size': 5,
                    "disable_scroll": False, "searchable": True, "wrap":True,
                    "child_kwargs": {"force_initial":True, "initial": {"window_pos": (0, 0), "closed":False},
                                     "bg_offset":2, "swoosh":False, "auto_resize":True, "closed":False,
                                     "return_item": True,
                                     "inline":True, "anchor": Anchor.TOP_LEFT, "parent_anchor": Anchor.TOP_LEFT},
                    "show_add_delete": False, "with_header": draw_header, "min_width": 10, "min_height": 20},
            recursive=True,
        )
    }

    WINDOW_MANAGER_SORTED = {
        defaultdict: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True},
            recursive=False,
            func=(sort_dict_alphabetically,
                  (draw_collection, {"show_add_delete": False, "selectable": False, "show_bg": False,
                                     "excluded":WindowManager.excluded_windows}),
                  unsort_dict_alphabetically)
        )
    }
    
    WINDOW_NO_HEADER = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': True,
                    "show_tint": False, "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "disable_scroll": False, "search_text": "", "searchable": True,
                    "show_add_delete": False, "with_header": draw_header, "min_width": 200, "min_height": 60,
                    "initial": {"width": 400, "height": 420, "window_pos": (100, 500)}},

            recursive=False
        )
    }

    WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "melty_window":False, "closable":True,
                    "with_header_end":draw_header_end, "auto_resize":False, "draggable":True, 'shadow':True,
                    "show_tint":True, "show_header":True, "with_footer":draw_footer, 'indent_size':5,
                    "disable_scroll":False, "searchable": True,
                   "show_add_delete":False, "with_header":draw_header, "min_width": 200, "min_height": 60,
                    "initial":{"width": 400, "height": 320, "window_pos": (100, 500)}},

            recursive=False
        )
    }

    WINDOW_CLEAN = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "shadow":True,
                    "melty_window":True, "closable":True,
                    "auto_resize":True, "is_tree":False, "show_tint":False, "show_header":True,
                    "disable_scroll":False, "searchable": True,
                    "initial": {"width": 400, "height": 320}},
            recursive=False
        )
    }

    code_ui_auto_load = True
    code_ui_params = {'save': True,
                     'recompile': False}
    CODE_UI = {
        types.FunctionType: ModeOverrides(
            recursive=True,
            func=(function_to_address,
                  (address_to_general_parse, {'load': code_ui_auto_load}),
                  (draw_collection, {"show_add_delete": True}),
                  (general_parse_to_address, code_ui_params),
                  address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            func=(module_to_address,
                  (address_to_general_parse, {'load': code_ui_auto_load}),
                  (draw_collection, {"show_add_delete": True}),
                  (general_parse_to_address, code_ui_params),
                  address_to_module),
        ),
        type: ModeOverrides(
            func=(class_to_address,
                  (address_to_general_parse, {'load': code_ui_auto_load}),
                  (draw_collection, {"show_add_delete": False}),
                  (general_parse_to_address, code_ui_params),
                  address_to_class),
            recursive=True
        ),
        Conditional: ModeOverrides(
            kwargs={"tint": (0.2, 0.2, 0.1), 'show_add_delete': True, 'is_tree':False},
            recursive=True,
        ),
        Comment: ModeOverrides(
            kwargs={"tint": (0.1, 0.1, 0.1), "show_bg":False, "shadow":False, 'show_add_delete': True, 'is_tree':False},
            func=draw_comment,
            recursive=True,
        ),
        GeneralParse: ModeOverrides(
            kwargs={'show_add_delete': False, "disable_scroll": False, "is_tree": True},
            recursive=True,
            func=draw_collection
        ),

    }

    code_plain_text_auto_load = True
    code_plain_text_params = {'save': True,
                              'recompile': False}
    CODE_PLAIN_TEXT = {

        types.FunctionType: ModeOverrides(
            recursive=True,
            func=(function_to_address,
                  (address_to_general_parse, {'load': code_plain_text_auto_load}),
                  general_parse_to_str,
                  draw_text,
                  str_to_general_parse,
                  (general_parse_to_address, code_plain_text_params),
                  address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            func=(module_to_address,
                  (address_to_general_parse, {'load': code_plain_text_auto_load}),
                  general_parse_to_str,
                  draw_text,
                  str_to_general_parse,
                  (general_parse_to_address, code_plain_text_params),
                  address_to_module),
        ),
        type: ModeOverrides(
            func=(class_to_address,
                  (address_to_general_parse, {'load': code_plain_text_auto_load}),
                  general_parse_to_str,
                  draw_text,
                  str_to_general_parse,
                  (general_parse_to_address, code_plain_text_params),
                  address_to_class),
            recursive=True
        ),
    }

    RUNNING = {
        type: ModeOverrides(
            func=(draw_type),
            recursive=False
        ),

    }

    # ── File metadata ────────────────────────────────────────
    #
    # Path on disk → metadata dict (name, size, modified, raw bytes).
    # Good for: file browsers, file inspectors, drag-and-drop targets.

    FILE_META = {
        Path: ModeOverrides(
            kwargs={"convert_in": [path_to_dict],
                    "convert_out": [rf_dict_to_path],
                    },
            func=draw_collection,
            recursive=True,
        ),
        bytes: ModeOverrides(
            kwargs={"convert_in": [bytes_to_str],
                    "convert_out": [rf_str_to_bytes]},
            func=draw_text,
            recursive=True,
        ),
    }

    # ── Inner modes for draw_with_modes children ────────────
    # Each operates on a GeneralParse and wraps a simple renderer in just
    # the converter chain it needs. Used as the `modes` arg to
    # draw_with_modes in Mode.CODE so that load / save / file-watch
    # registration happen ONCE upstream and are shared across columns.
    
    CODE_INNER_TEXT = {
        GeneralParse: ModeOverrides(
            recursive=True,
            func=(general_parse_to_str,
                  (draw_text, {}),
                  str_to_general_parse),
        ),
    }

    CODE_INNER_UI = {
        GeneralParse: ModeOverrides(
            recursive=True,
            func=((draw_collection, {"show_add_delete": True}),),
        ),
    }

    # Outer code mode. Populated outside class body (via _populate_code_mode
    # below) because the chain references Mode.CODE_INNER_TEXT /
    # Mode.CODE_INNER_UI as enum members, which only exist post-finalization.
    CODE = {}



def _populate_code_mode():
    """Fill in Mode.CODE.value. Deferred until after the Mode class is
    defined so the chain can reference Mode.CODE_INNER_TEXT /
    Mode.CODE_INNER_UI as proper enum members.

    The chain runs the address resolution + GeneralParse load/save ONCE per
    Mode.CODE invocation. draw_with_modes then dispatches each selected tab
    to its inner mode (text or UI), which receive the already-loaded
    GeneralParse. One file-watcher registration, one cache, regardless of
    how many columns are active.
    """
    inner_modes = (Mode.CODE_INNER_TEXT, Mode.CODE_INNER_UI)

    def chain_for(address_in, address_out):
        return ModeOverrides(
            kwargs={"disable_scroll": True, "searchable":True},
            recursive=True,

            func=(address_in,
                  (address_to_general_parse, {'load': True,}),
                  (draw_with_modes, {'modes': inner_modes, 'disable_scroll': True, 'fill_height': compute_height}),
                  (general_parse_to_address, {'save': True, 'recompile': True}),
                  address_out),
        )

    Mode.CODE.unwrapped.update({
        types.FunctionType: chain_for(function_to_address, address_to_function),
        types.ModuleType: chain_for(module_to_address, address_to_module),
        type: chain_for(class_to_address, address_to_class),
    })


_populate_code_mode()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Tint lenses - named, static accessors for "where does this tint live"       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# Each TintLens member is a bidirectional accessor for one tint source. The whole
# point: every source plugs into the *same* draw_tuple picker, `focus`, and the
# tint's location is static config that lives here in code - never in draw_state
# (which is GC'd on a short TTL).
#
# Adding a source is one line, with a factory:
#
#   mem_lens("Draw state", lambda ds: ds)                  # live attr/dict in memory
#   code_lens("Code comment", ("__overrides__", "tint"))   # parsed from source code
#
#   - mem_lens  → focus reads/writes the live attribute or dict-key directly.
#   - code_lens → the CODE_UI chain is GENERATED from the path + the source
#                 object's type (class / function / module), with `focus` slotted
#                 in where draw_collection sits in CODE_UI. draw_any(root, chain=...)
#                 runs it: parse → focus → save+recompile. No paired save mode.
#
# `default` is what the "+ Add" affordance in focus stamps in when the source has
# no tint yet. Render a TintLens member with draw_any to get the radio dropdown.

_TINT_DEFAULT = (0.485, 0.61, 0.76)

# Address-resolver pairs (load-side, save-side) per source-object type - the same
# pairs CODE_UI dispatches on. Generating from these keeps code lenses one-liners.
_ADDR_PAIRS = {
    type: (class_to_address, address_to_class),
    types.FunctionType: (function_to_address, address_to_function),
    types.ModuleType: (module_to_address, address_to_module),
}
_TINT_SAVE = {'save': True, 'recompile': True}


def _build_code_chain(root, path, default):
    """CODE_UI with `focus` in place of draw_collection: parse → focus → save."""
    load_node, save_node = _ADDR_PAIRS.get(type(root), _ADDR_PAIRS[type])
    return (load_node,
            (address_to_general_parse, {'load': True}),
            (focus, {'path': path, 'default': default}),
            (general_parse_to_address, _TINT_SAVE),
            save_node)


@dataclass
class LensSpec:
    label: str
    root: Any                      # draw_state -> the object the lens starts from
    path: tuple                    # key/attr path to the tint leaf
    default: tuple = _TINT_DEFAULT  # value the "+ Add" affordance stamps in
    chain: Any = None              # code lenses: root -> generated chain tuple; None = in-memory


def mem_lens(label, root, path=("tint",), default=_TINT_DEFAULT):
    """A tint that lives as a live attribute/dict-key in memory (draw_state, data
    class). focus reads/writes it directly — no parse, no save side."""
    return LensSpec(label, root=root, path=path, default=default, chain=None)


def code_lens(label, path, default=_TINT_DEFAULT):
    """A tint that lives in source code (a comment / decoration). The parse→save
    chain is generated from the path + owning object type at dispatch time."""
    return LensSpec(label, root=_tint_source_object, path=path, default=default,
                    chain=lambda root: _build_code_chain(root, path, default))


def _tint_source_object(draw_state):
    """The class / function / module whose source code owns this element — i.e.
    where a code-comment or decoration tint would be parsed from. For a data
    instance that's its (non-builtin) class; for a primitive it's None (the
    caller falls back to an in-memory lens)."""
    raw = getattr(draw_state, "_raw_input_value", None)
    if raw is None:
        return None
    if isinstance(raw, (types.FunctionType, type, types.ModuleType)):
        return raw
    the_type = type(raw)
    if getattr(the_type, "__module__", None) in (None, "builtins", "_collections_abc"):
        return None
    return the_type


class TintLens(Enum):
    DRAW_STATE   = mem_lens("Draw state", lambda ds: ds)
    DATA_CLASS   = mem_lens("Data class", lambda ds: getattr(ds, "_raw_input_value", None))
    CODE_COMMENT = code_lens("Code comment", ("__overrides__", "tint"))
    DECORATION   = code_lens("Decoration", ("decorators", "defaults", "tint"))


class ModeGroup:
    CODE = (Mode.CODE_UI, Mode.CODE_PLAIN_TEXT)

