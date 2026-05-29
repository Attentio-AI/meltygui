import types
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Any

from src.lsd.gl_gui.model.core_model.draw_state import Anchor, Pin
from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.toggles import WindowManager
from src.lsd.gl_gui.view.core_conversion.chain_converters import module_to_address, address_to_general_parse, \
    general_parse_to_address, address_to_module, class_to_address, address_to_class, function_to_address, \
    address_to_function, general_parse_to_str, str_to_general_parse, focus, \
    caller_to_address, address_to_call_parse, call_dict_to_save, \
    class_to_address_incl_overrides
from src.lsd.gl_gui.view.core_conversion.file_converters import path_to_dict, bytes_to_str, \
    rf_dict_to_path, rf_str_to_bytes
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
    route: Optional[dict]  = None


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
            kwargs={"show_bg": False, "selectable": False, "min_width": -2, "min_height":18, "wrap":True, "z_offset":0,
                    "shadow":False, "is_tree": False, "use_cache": True, "layer_offset": 1},
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
                    "child_kwargs": {"force_initial":True, "initial": {"window_pos": (-20, -1), "closed":False},
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
    WINDOW_AUTO_FIT = {
        Any: ModeOverrides(
            kwargs={"use_cache": True, "melty_window": False, "closable": True, "layer_offset": 4,
                    "auto_resize": True, "draggable": True, 'min_width':600, "swoosh": False,
                    "inline": True, "show_header":False,
                    "initial": {"width": 400, "height": 320}},

            recursive=False
        )
    }

    WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "melty_window":False, "closable":True,
                    "with_header_end":draw_header_end, "auto_resize":False, "draggable":True, 'shadow':True,
                    "show_tint":True, "show_header":True, "with_footer":draw_footer, 'indent_size':5,
                     "searchable": True, "disable_scroll": False,
                   "show_add_delete":False, "with_header":draw_header, "min_width": 200, "min_height": 60,
                    "initial":{"width": 400, "height": 320, "window_pos": (100, 500)}},

            recursive=False
        )
    }

    MODE_WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': True,
                    "show_tint": True, "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "searchable": True, "disable_scroll": True,
                    "show_add_delete": False, "with_header": draw_header, "min_width": 200, "min_height": 60,
                    "initial": {"width": 400, "height": 320, "window_pos": (100, 500)}},

            recursive=False
        )
    }

    FLOATING = {
        Any: ModeOverrides(
            kwargs={"use_cache": True, "melty_window": False, "closable": True, "layer_offset":1,
                    "auto_resize": True, "draggable": True, "window_pos":(0,0), "swoosh": False,
                    "inline": True,
                    "initial": {"width": 400, "height": 320, "window_pos": (0, 0)}},

            recursive=False
        )
    }

    WINDOW_CLEAN = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "shadow":True,
                    "melty_window":True, "closable":True,
                    "auto_resize":True, "is_tree":False, "show_tint":False, "show_header":True,
                    "disable_scroll":False, "searchable": True,
                    "initial": {"width": 400}},
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
    draw_text_funcs = ((address_to_general_parse,
                            {'load': code_plain_text_auto_load}),
                        general_parse_to_str,
                        draw_text,
                        str_to_general_parse,
                        (general_parse_to_address,
                            code_plain_text_params))

    CODE_PLAIN_TEXT = {
        types.FunctionType: ModeOverrides(
            recursive=True,
            route={function_to_address: "jump_to", address_to_general_parse: "code_tree"},
            func=(function_to_address,*draw_text_funcs,address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            route={module_to_address : "jump_to", address_to_general_parse: "code_tree"},
            func=(module_to_address,*draw_text_funcs, address_to_module),
        ),
        type: ModeOverrides(
            route={class_to_address: "jump_to", address_to_general_parse: "code_tree"},
            func=(class_to_address,*draw_text_funcs, address_to_class),
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
                  (general_parse_to_address, {'save': True, 'recompile': False}),
                  address_out),
        )

    Mode.CODE.unwrapped.update({
        types.FunctionType: chain_for(function_to_address, address_to_function),
        types.ModuleType: chain_for(module_to_address, address_to_module),
        type: chain_for(class_to_address, address_to_class),
    })


_populate_code_mode()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Lenses - generic, field-parameterized accessors for "where does X live"      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# A Lens is a bidirectional accessor parameterized by a leaf NAME. The lens KIND
# encodes the storage space (and so the read/write mechanism); the name fills in
# which leaf. There is no "tint lens" vs "outline lens" - there's `class_var` and
# you call it `class_var("tint")` or `class_var("outline_color")`.
#
# Kinds (all generic over `name`):
#
#   draw_state_attr(name)  live attr on the draw_state    → in place, ephemeral
#   instance_attr(name)    live attr on the data object   → in place
#   class_var(name)        class-body assignment in source → parse → focus → save
#   decoration(name)       @defaults(name=...) decorator     → parse → focus → save
#   code_comment(name)     # [name=...] override comment      → parse → focus → save
#
# In-place kinds run `focus(root, path)` directly. Code kinds generate a
# parse→focus→save chain (CODE_UI with `focus` swapped for draw_collection) and
# run it via draw_any(root, chain=...). A class span parses to {<ClassName>: {...}}
# (cst_module_to_dict), so class-anchored paths are prefixed with the class name.
#
# `default` is what the "+ Add" affordance in focus stamps in when the source has
# no value yet. To add a source for an attribute: append one constructor call to
# its list in LENSES_BY_ATTR.

# Address-resolver pairs (load-side, save-side) per source-object type - the same
# pairs CODE_UI dispatches on. Generating from these keeps code lenses one-liners.
_ADDR_PAIRS = {
    type: (class_to_address, address_to_class),
    types.FunctionType: (function_to_address, address_to_function),
    types.ModuleType: (module_to_address, address_to_module),
}
_CODE_SAVE = {'save': True, 'recompile': False}


def _owning_source(draw_state):
    """The class / function / module whose source code owns this element — where a
    class_var / decoration / comment would be parsed from. For a data instance
    that's its (non-builtin) class; for a primitive it's None."""
    raw = getattr(draw_state, "_raw_input_value", None)
    if raw is None:
        return None
    if isinstance(raw, (types.FunctionType, type, types.ModuleType)):
        return raw
    the_type = type(raw)
    if getattr(the_type, "__module__", None) in (None, "builtins", "_collections_abc"):
        return None
    return the_type


def _build_code_chain(root, tail, default, kind, prefix_class=True, ensure_import=None,
                      load_override=None):
    """CODE_UI with `focus` in place of draw_collection: parse → focus → save.

    A class span parses to {<ClassName>: {...}}, so things INSIDE the class
    (class vars, decorator kwargs) need the class name prefixed onto the focus
    path. An override `# [...]` comment, however, lives at the span's MODULE
    level (above the class) and round-trips there — so code_comment passes
    prefix_class=False to target the top-level __overrides__ directly.

    ensure_import=(module, name) makes the save also insert that import if the
    file lacks it (so a synthesized @defaults decorator resolves)."""
    load_node, save_node = _ADDR_PAIRS.get(type(root), _ADDR_PAIRS[type])
    if load_override is not None and isinstance(root, type):
        load_node = load_override        # e.g. extend the span to include a leading comment
    prefix = (root.__name__,) if (prefix_class and isinstance(root, type)) else ()
    save_kwargs = dict(_CODE_SAVE)
    if ensure_import is not None:
        save_kwargs['ensure_import'] = ensure_import
    return (load_node,
            (address_to_general_parse, {'load': True}),
            (focus, {'path': prefix + tail, 'default': default, 'kind': kind}),
            (general_parse_to_address, save_kwargs),
            save_node)


@dataclass
class Lens:
    label: str            # unique display id (kind + name) - drives draw_state identity
    name: str             # the field/leaf name this lens reads/writes
    root: Any             # draw_state -> the object the chain starts from
    path: tuple           # in-place kinds: attr-name path to the leaf
    default: Any = None   # value the "+ Add" affordance stamps in
    chain: Any = None     # code kinds: root -> generated chain tuple; None = in-place
    kind: str = ""        # short, clean display label (no attr name; no internal prefix)


# ── Lens kinds (generic over `name`) ──────────────────────────────────────────

def draw_state_attr(name, default=None):
    return Lens(f"Draw state · {name}", name, root=lambda ds: ds, path=(name,),
                default=default, kind="Draw state")


def instance_attr(name, default=None):
    return Lens(f"Instance attr · {name}", name,
                root=lambda ds: getattr(ds, "_raw_input_value", None),
                path=(name,), default=default, kind="Instance")


def class_var(name, default=None):
    return Lens(f"Class variable · {name}", name, root=_owning_source, path=(name,),
                default=default, kind="Class variable",
                chain=lambda root: _build_code_chain(root, (name,), default, "Class variable"))


_DEFAULTS_IMPORT = ("src.lsd.gl_gui.view.core_views.decoration.core_decoration", "defaults")


def decoration(name, default=None, decorator="defaults"):
    tail = ("decorators", decorator, name)
    # A synthesized @defaults needs its import; ensure it in the same save write.
    imp = _DEFAULTS_IMPORT if decorator == "defaults" else None
    return Lens(f"Decoration · {name}", name, root=_owning_source, path=tail,
                default=default, kind="Decoration",
                chain=lambda root: _build_code_chain(root, tail, default, "Decoration",
                                                     ensure_import=imp))


def code_comment(name, default=None):
    # An override comment lives at the span's MODULE level (above the class), so
    # no class-name prefix. The class span from getsourcelines excludes a comment
    # above the class, so we load via class_to_address_incl_overrides, which
    # extends the span up to include it - otherwise it never round-trips.
    tail = ("__overrides__", name)
    return Lens(f"Code comment · {name}", name, root=_owning_source, path=tail,
                default=default, kind="Code comment",
                chain=lambda root: _build_code_chain(root, tail, default, "Code comment",
                                                     prefix_class=False,
                                                     load_override=class_to_address_incl_overrides))


def caller_arg(name, default=None):
    """Edit the literal a caller passed for `name`, e.g. draw_text(tint=(1,0,1)).
    Anchored on the call site (draw_state._call_site — the (filename, lineno)
    resolved once when frames were grabbed on menu-open), not on the data or its
    class. The chain is fixed (independent of root type): site → call-statement
    Address → parse the Call's kwargs → focus → save.

    Reads the cached site rather than re-walking the live stack: during a tint
    drag the stack changes (parents are skipped), so re-deriving would flip the
    site mid-drag and cancel the edit."""
    chain = (caller_to_address,
             (address_to_call_parse, {'load': True}),
             (focus, {'path': (name,), 'default': default, 'kind': "Caller"}),
             (call_dict_to_save, {'save': True}))
    return Lens(f"Caller arg · {name}", name,
                root=lambda ds: getattr(ds, "_call_site", None),
                path=(name,), default=default, kind="Caller",
                chain=lambda root: chain)


# A list of lenses per attribute - draw_tint_context renders every entry, so you
# get one color picker (or "+ Add") per source. Append a constructor to extend.
_TINT_DEFAULT = (0.485, 0.61, 0.76)
LENSES_BY_ATTR = {
    "tint": [
         caller_arg("tint", _TINT_DEFAULT),
        draw_state_attr("tint", _TINT_DEFAULT),
        instance_attr("tint", _TINT_DEFAULT),
        class_var("tint", _TINT_DEFAULT),
        decoration("tint", _TINT_DEFAULT),
        code_comment("tint", _TINT_DEFAULT),
    ],
}


class ModeGroup:
    CODE = (Mode.CODE_UI, Mode.CODE_PLAIN_TEXT)

