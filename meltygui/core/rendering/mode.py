import types
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PosixPath
from typing import Optional, Any

from meltygui.state.new_core_model import Anchor
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.runtime.toggles import WindowManager
# Register built-in views used by lazy render-function handles in the modes.
import meltygui.view.color_view
import meltygui.view.decoration_view
import meltygui.view.diagnostic_view
import meltygui.view.search_view
import meltygui.view.tab_view
import meltygui.view.texture_view
import meltygui.view.window_view
from meltygui.core.rendering.window_decoration import window
from meltygui.view.header_view import draw_footer
from meltygui.view.header_view import draw_header_end
from meltygui.view.header_view import draw_header
from meltygui.view.collection_view import draw_collection
from meltygui.core.rendering.render_dispatch import sort_dict_alphabetically
from meltygui.core.rendering.render_dispatch import unsort_dict_alphabetically
from meltygui.view.inspection_view import draw_with_modes
from meltygui.view.inspection_view import draw_type
from meltygui.view.inspection_view import draw_type_name
from meltygui.core.rendering.render_dispatch import class_to_var_dict
from meltygui.core.rendering.render_dispatch import var_dict_to_class
from meltygui.view.dropdown_view import draw_drop_down_item
from meltygui.core.rendering.render_dispatch import type_lens
from meltygui.view.text_view import draw_text


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
    recursive: Optional[bool] = True
    route: Optional[dict] = None


class _CodeMode:
    """The value of a Mode member whose policies belong to the code stack
    (chain_converters, libcst_conversion, new_converters, code_view: the
    libcst import). ``build()`` returns the member's {type: ModeOverrides}
    and runs on the member's first get_config_for, so the enum and every
    window mode load without the code stack — an app that never shows code
    never imports it (core/runtime/app.py). The dict literal lives in the
    builder, where a reader expects it; hotswapping the builder rebuilds the
    policies on the next use (Mode.__init__ / Mode._policies)."""

    def __init__(self, build):
        self.build = build

    def __repr__(self):
        return f'<code mode: {self.build.__name__}>'


def _code_ui_policies():
    from meltygui.code.chain_converters import module_to_address, address_to_general_parse
    from meltygui.code.chain_converters import general_parse_to_address, address_to_module
    from meltygui.code.chain_converters import class_to_address, address_to_class
    from meltygui.code.chain_converters import function_to_address, address_to_function
    from meltygui.code.libcst_conversion import GeneralParse, Conditional, Comment
    from meltygui.view.code_view import draw_comment
    code_ui_auto_load = True
    code_ui_params = {'save': True,
                      'recompile': False}
    return {
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
            kwargs={"tint": (0.2, 0.2, 0.1), 'show_add_delete': True, 'is_tree': False},
            recursive=True,
        ),
        Comment: ModeOverrides(
            kwargs={"tint": (0.1, 0.1, 0.1), "show_bg": False, "shadow": False, 'show_add_delete': True,
                    'is_tree': False},
            func=draw_comment,
            recursive=True,
        ),
        GeneralParse: ModeOverrides(
            kwargs={'show_add_delete': False, "disable_scroll": False, "is_tree": True},
            recursive=True,
            func=draw_collection
        ),
    }


def _code_plain_text_policies():
    from meltygui.code.chain_converters import module_to_address, address_to_general_parse
    from meltygui.code.chain_converters import general_parse_to_address, address_to_module
    from meltygui.code.chain_converters import class_to_address, address_to_class
    from meltygui.code.chain_converters import function_to_address, address_to_function
    from meltygui.code.chain_converters import general_parse_to_str, str_to_general_parse
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
    return {
        types.FunctionType: ModeOverrides(
            recursive=True,
            route={function_to_address: "jump_to", address_to_general_parse: "code_tree"},
            func=(function_to_address, *draw_text_funcs, address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            route={module_to_address: "jump_to", address_to_general_parse: "code_tree"},
            func=(module_to_address, *draw_text_funcs, address_to_module),
        ),
        type: ModeOverrides(
            route={class_to_address: "jump_to", address_to_general_parse: "code_tree"},
            func=(class_to_address, *draw_text_funcs, address_to_class),
            recursive=True
        ),
    }


def _new_code_policies():
    # The code-host-cache route (vs inline convertors): code_file_io owns
    # load/save of the span. draw_code_tabs_from_cache shows the structured |
    # text tabs with the parse loaded from the shared dict_host
    # (code_hosts_for). A class edit in the structured tab also drives the
    # live type immediately (_live_apply_class_vars) ahead of any recompile.
    from meltygui.code.new_codecs import CallSite, Decorations
    from meltygui.code.new_converters import code_file_io
    from meltygui.view.code_view import draw_code_tabs_from_cache
    return {
        (type, types.FunctionType, types.ModuleType, CallSite, Decorations): ModeOverrides(
            kwargs={"auto_load_edits": True,
                    "auto_load": True,
                    "auto_save": True,
                    'view_func': draw_code_tabs_from_cache,
                    "disable_scroll": True,
                    "with_header": draw_header,
                    "child_kwargs": {
                        'column_widths': [358],
                    },
                    },
            func=code_file_io
        )
    }


def _file_meta_policies():
    # Path on disk → metadata dict (name, size, modified, raw bytes).
    # Good for: file browsers, file inspectors, drag-and-drop targets.
    from meltygui.code.file_converters import path_to_dict, rf_dict_to_path
    from meltygui.code.file_converters import rf_bytes_to_str, rf_str_to_bytes
    return {
        Path: ModeOverrides(
            kwargs={"convert_in": [path_to_dict],
                    "convert_out": [rf_dict_to_path],
                    },
            func=draw_collection,
            recursive=True,
        ),
        bytes: ModeOverrides(
            kwargs={"convert_in": [rf_bytes_to_str],
                    "convert_out": [rf_str_to_bytes]},
            func=draw_text,
            recursive=True,
        ),
    }


def _file_tree_policies():
    # THE main editor mode: code_file_io + draw_text_from_code_cache, with
    # the parse pulled from the shared code-host cache (code_hosts_for) instead
    # of a local convert chain. Each ref's codec owns its load/edit/save
    # round-trip: a Path leaf in a folder tree (playground.folder_files,
    # whole-file TextFileCodec), and equally a function / class / module /
    # CallSite / Decorations span (the context menu's render-func and class
    # tabs). Recursive so the route survives any depth of folder nesting.
    from meltygui.code.new_codecs import CallSite, Decorations
    from meltygui.code.new_converters import code_file_io
    from meltygui.view.code_view import draw_text_from_code_cache
    return {
        (Path, type, types.FunctionType, types.ModuleType, CallSite, Decorations): ModeOverrides(
            func=code_file_io,
            # Pin the text editor: draw_any forwards a mode's func override as
            # the `view_func` kwarg, which would otherwise hand code_file_io
            # ITSELF as its nested view (str → "No codec"). Mode kwargs win
            # over call kwargs (`kwargs | override_kwargs`), so this corrects it.
            # draw_text_from_code_cache = draw_text fed the cst node from the
            # global code-host cache (code_cache_for), so usage links and
            # syntax-error highlighting work without an inline chain.
            kwargs={"auto_load_edits": True, "disable_scroll":True, "view_func": draw_text_from_code_cache},
            recursive=True,
        ),
    }


def _code_inner_text_policies():
    from meltygui.code.chain_converters import general_parse_to_str, str_to_general_parse
    from meltygui.code.libcst_conversion import GeneralParse
    return {
        GeneralParse: ModeOverrides(
            recursive=True,
            func=(general_parse_to_str,
                  (draw_text, {}),
                  str_to_general_parse),
        ),
    }


def _code_inner_ui_policies():
    from meltygui.code.libcst_conversion import GeneralParse
    return {
        GeneralParse: ModeOverrides(
            recursive=True,
            func=((draw_collection, {"show_add_delete": True}),),
        ),
    }


def _code_policies():
    """Mode.CODE: the chain runs the address resolution + GeneralParse
    load/save ONCE per Mode.CODE invocation. draw_with_modes then dispatches
    each selected tab to its inner mode (text or UI), which receive the
    already-loaded GeneralParse. One file-watcher registration, one cache,
    regardless of how many columns are active."""
    from meltygui.code.chain_converters import module_to_address, address_to_general_parse
    from meltygui.code.chain_converters import general_parse_to_address, address_to_module
    from meltygui.code.chain_converters import class_to_address, address_to_class
    from meltygui.code.chain_converters import function_to_address, address_to_function
    inner_modes = (Mode.CODE_INNER_TEXT, Mode.CODE_INNER_UI)

    def chain_for(address_in, address_out):
        return ModeOverrides(
            kwargs={"disable_scroll": True, "searchable": True},
            recursive=True,

            func=(address_in,
                  (address_to_general_parse, {'load': True, }),
                  (draw_with_modes, {'modes': inner_modes, 'disable_scroll': True, 'fill_height': compute_height}),
                  (general_parse_to_address, {'save': True, 'recompile': False}),
                  address_out),
        )

    return {
        types.FunctionType: chain_for(function_to_address, address_to_function),
        types.ModuleType: chain_for(module_to_address, address_to_module),
        type: chain_for(class_to_address, address_to_class),
    }


@window
class Mode(Enum):
    def __reduce_ex__(self, protocol):
        # Pickle BY NAME. Enum's default pickles by VALUE, and the values are
        # ModeOverrides dicts full of live functions/classes - unpicklable, so
        # any structure holding a resolved Mode member (a cst-dict parse of a
        # span saying `mode=Mode.X`) silently failed to serialize and disabled
        # the cst-dict cache for that file (full cold parse every session).
        # getattr(Mode, name) at load also drives hotswap's enum member
        # reconcile: the loading session's live member is returned.
        return (getattr, (self.__class__, self._name_))

    def get_config_for(self, input_value=None, the_type=None):
        unwrapped = self._policies()
        if input_value is not None:
            the_type = type(input_value)
        config = unwrapped.get(the_type, None)
        if config is not None:
            return config

        for super_type in type(input_value).__mro__:
            config = unwrapped.get(super_type, None)
            if config is not None:
                return config

        if Any in unwrapped:
            return unwrapped[Any]
        return None

    def __init__(self, *args, **kwargs):
        self.unwrapped = {}
        # A _CodeMode value builds its policies on first use (_policies); the
        # enum hotswap copies the fresh member's `_build` over the live one,
        # so an edited builder applies at the next get_config_for.
        self._build = self.value.build if isinstance(self.value, _CodeMode) else None
        if isinstance(self.value, dict):
            self._unwrap(self.value)

    def _unwrap(self, policies):
        for key, value in policies.items():
            if isinstance(key, tuple):
                for sub_key in key:
                    self.unwrapped[sub_key] = value
            else:
                self.unwrapped[key] = value

    def _policies(self):
        """The member's {type: ModeOverrides}, built now for a _CodeMode."""
        build = self._build
        if build is not None:
            self._build = None
            self.unwrapped = {}
            self._unwrap(build())
        return self.unwrapped

    NEW_CODE = _CodeMode(_new_code_policies)

    READ_ONLY = {
        (Any): ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "header_same_line": True, "show_header": True,
                    "show_add_delete": False, "bg_offset": -1,
                    "use_cache": True, "initial": {"expanded": False, "closed": False}},
            recursive=True,
        ),
        (str, float, int, bool, types.NoneType): ModeOverrides(
            kwargs={"show_bg": False, "selectable": False, "show_add_delete": False, "show_header": True,
                    "expanded": True, "use_cache": True, "shadow": False},
        ),
        (type): ModeOverrides(
            func=draw_type_name,
            kwargs={"show_bg": False, "selectable": False, "show_add_delete": False, "show_header": True,
                    'is_tree': False, "use_cache": True, "shadow": False, 'tint': (0.8, 0.8, 0.6)},
        ),

    }

    # [tint=(0.2,0.1,0.1)]
    SORT = {
        defaultdict: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True},
            recursive=True,
            func=(sort_dict_alphabetically,
                  (draw_collection, {"show_add_delete": False, "selectable": False, "show_bg": False}),

                  ))
    }

    DROPDOWN_WINDOW = {
        (Any): ModeOverrides(
            kwargs={"show_bg": False, "selectable": False, "min_width": -2, "min_height": 18, "wrap": True,
                    "z_offset": 0,
                    "shadow": False, "is_tree": False, "use_cache": True, "layer_offset": 1},
            recursive=False,
            func=draw_drop_down_item
        ),
        (dict, list, tuple): ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False,
                    "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": True, "draggable": True,
                    'shadow': True, "return_item": True,
                    "show_tint": False, "show_header": False, 'indent_size': 5,
                    "disable_scroll": False, "searchable": True, "wrap": True,
                    "child_kwargs": {"force_initial": True, "initial": {"window_pos": (-20, -1), "closed": False},
                                     "bg_offset": 2, "swoosh": False, "auto_resize": True, "closed": False,
                                     "return_item": True,
                                     "inline": True, "anchor": Anchor.TOP_LEFT, "parent_anchor": Anchor.TOP_LEFT},
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
                                     "excluded": WindowManager.excluded_windows}),
                  unsort_dict_alphabetically)
        )
    }

    WINDOW_NO_HEADER = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': False,
                    "show_tint": False, "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "search_text": "",
                    "show_add_delete": False, "with_header": draw_header, "min_width": 200, "min_height": 60,
                    "initial": {"width": 319, "height": 600, "window_pos": (100, 500)}},

            recursive=False
        )
    }
    WINDOW_AUTO_FIT = {
        Any: ModeOverrides(
            kwargs={"use_cache": True, "melty_window": False, "closable": True, "layer_offset": 4,
                    "auto_resize": True, "draggable": True, 'min_width': 600, "swoosh": False,
                    "inline": True, "show_header": False,
                    "initial": {"width": 400, "height": 320}},

            recursive=False
        )
    }

    # WINDOW_AUTO_FIT's chrome without the auto-fit: the user sizes the window
    # and content lays out to it (global search - its columns/rows wrap to the
    # width you give it).
    WINDOW_RESIZABLE = {
        Any: ModeOverrides(
            kwargs={"use_cache": True, "melty_window": False, "closable": True, "layer_offset": 4,
                    "auto_resize": False, "draggable": True, 'min_width': 600, "swoosh": False,
                    "inline": True, "show_header": False,
                    "initial": {"width": 400, "height": 320}},

            recursive=False
        )
    }

    WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': True,
                    "show_tint": True, "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "disable_scroll": False, "bg_offset": -1,
                    "min_width": 200, "min_height": 60,
                    "initial": {"width": 400, "height": 320, "window_pos": (100, 500)}},
            recursive=False
        )
    }
    
    LIVE_WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "draggable": True, 'shadow': True,
                    "show_tint": True, "show_header": True, "with_footer": draw_footer,
                    "disable_scroll": False, "bg_offset": -1, "is_tree":False, 'show_add_delete':False,
                    "with_header": draw_header,
                    # No initial height: closable windows without one adopt
                    # their content's measured height on first render (the
                    # _height_from_content path in core_render), so live-view
                    # value windows open wrapped to their contents.
                    # max_height caps only that first-render adopt; the
                    # user's resize handle isn't bound by it.
                    "max_height": 800,
                    "initial": {"window_pos": (100, 500), "width": 400}},
            recursive=False
        )
    }

    WINDOW_PARAMS = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': True,
                    "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "disable_scroll": False, "bg_offset": -1,
                    "with_header": draw_header, "min_width": 200, "min_height": 60,
                    "initial": {"width": 400, "height": 700, "window_pos": (500, 100)}},
            recursive=False
        )
    }

    # Lightweight anchored popover: a small auto-fitting, header-less, non-draggable
    # temp window (like the dropdown menu). The caller supplies closed / window_pos /
    # parent_window to anchor it to a trigger and toggle visibility. `popover` marks
    # the window for Melty.popover_orphaned: it lives only while
    # Melty.popover_focused_ds names it or one of its ancestors; end_frame
    # discards it the moment the slot moves away, whether or not the view that
    # drew it ever runs again (a tab switch, a cached spawner).
    POPOVER = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": False, "melty_window": False,
                    "closable": True, "auto_resize": True, "draggable": False, "shadow": True,
                    "show_tint": False, "show_header": False, "with_header": None, "disable_scroll": True,
                    "temp": True, "swoosh": False, "indent_size": 2, "popover": True},
            recursive=False
        )
    }

    HOST_WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': False,
                    "show_tint": True, "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "disable_scroll": False, "bg_offset": -1,
                    "show_add_delete": False, "with_header": draw_header, "min_width": 50, "min_height": 60,
                    "initial": {"width": 400, "height": 320, "window_pos": (50, 40)}},

            recursive=False
        )
    }

    TERMINAL_WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': True,
                    "show_tint": True, "show_header": True, "with_footer": draw_footer, 'indent_size': 5, "bg_offset": -1,
                    "show_add_delete": False, "with_header": draw_header, "min_width": 200, "min_height": 60,
                    "initial": {"width": 750, "height": 1120, "closed": False, "disable_scroll":True, "swoosh": False}, "force_initial":True},

            recursive=False
        )
    }

    MODE_WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "melty_window": False, "closable": True,
                    "with_header_end": draw_header_end, "auto_resize": False, "draggable": True, 'shadow': True,
                    "show_tint": True, "show_header": True, "with_footer": draw_footer, 'indent_size': 5,
                    "show_add_delete": False, "with_header": draw_header,
                    "initial": {"width": 400, "height": 320, "window_pos": (100, 500)}, 'icon': None},

            recursive=False
        )
    }

    FLOATING = {
        Any: ModeOverrides(
            kwargs={"use_cache": True, "melty_window": False, "closable": True, "layer_offset": 0,
                    "auto_resize": True, "draggable": True, "window_pos": (0, 0), "swoosh": False,
                    "inline": True,
                    "initial": {"width": 400, "height": 320, "window_pos": (0, 0)}},

            recursive=False
        )
    }

    WINDOW_CLEAN = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "shadow": True,
                    "melty_window": True, "closable": True, "width":500,
                    "auto_resize": True, "is_tree": False, "show_tint": False, "show_header": True,
                    "disable_scroll": False},
            recursive=False
        )
    }

    # WINDOW_CLEAN with the width left to the caller (a mode's kwargs win
    # over the call's, so WINDOW_CLEAN's 500 cannot be overridden): the
    # floating find pill (core_render's draw_search window) sizes itself
    # to its one child.
    WINDOW_PILL = {
        Any: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True, "shadow": True,
                    "melty_window": True, "closable": True,
                    "auto_resize": True, "is_tree": False, "show_tint": False, "show_header": True,
                    "disable_scroll": False},
            recursive=False
        )
    }

    CODE_UI = _CodeMode(_code_ui_policies)

    CODE_PLAIN_TEXT = _CodeMode(_code_plain_text_policies)

    RUNNING = {
        type: ModeOverrides(
            func=(draw_type),
            recursive=False
        ),

    }

    # ── File metadata ────────────────────────────────────────
    FILE_META = _CodeMode(_file_meta_policies)

    # ── Function parameters (draw_function) ─────────────────
    #
    # String parameter values draw with the text editor instead of the
    # default type renderer.
    FUNCTION_PARAMS = {
        str: ModeOverrides(
            func=draw_text,
            recursive=True,
        ),
    }

    # ── TEXT - reference mode for all mode-attached child kwargs ─────────
    #
    # A dict of strings: the dict entry draws the collection, the str entry
    # sends each leaf by draw_text. The leaves' TEXT STYLE lives in this
    # entry's child_kwargs - draw_collection merges child_kwargs into every
    # recursive call, so line_height/wrap/syntax_highlight land in draw_text
    # without every caller passing them.
    #
    # That is what the inputs tab's "child_kwargs (mode)" source row edits:
    # open the tab on one of the string leaves and the row is backed by THIS
    # enum, with the jump button pointing at the TEXT member below. A change
    # round-trips through the Mode enum's code host straight back into
    # mode.py - distinct from the plain "child_kwargs" row, which shows
    # whichever NON-mode entry source (caller / @defaults) sets one.
    #
    # Live usage: tests/playground/mode_test_playground.py (draw_mode_test).
    TEXT = {
        dict: ModeOverrides(
            kwargs={"show_bg": True, "use_cache": True, "show_header": True,
                    "child_kwargs": {"line_height": 1.35,
                                     "wrap": True,
                                     "syntax_highlight": False,
                                     'tint': (0.87, 0.604, 0.05)}},
            recursive=True,
        ),
        str: ModeOverrides(
            func=draw_text,
            # Disjoint from child_kwargs above ON PURPOSE: mode kwargs win over
            # call kwargs (`kwargs | override_kwargs`), so anything added here
            # would shadow the child_kwargs entry and the source row would edit a
            # value that never reaches the view.
            kwargs={"use_cache": True, "show_header": True},
            recursive=True,
        ),
    }

    # ── File tree / cache-backed code editor ────────────────────────────
    FILE_TREE = _CodeMode(_file_tree_policies)

    # ── File tree, names only ───────────────────────────────────────────
    #
    # The display-only sibling of FILE_TREE: a {name: Path} dict's folder tree
    # where folders are plain collapsing draw_collection entries and each Path
    # leaf renders as just its file name (double-click → open_file_tab).
    # Nothing loads file content. Live usage: playground.file_tree
    # (render_file_tree_melty).

    # is_tree is STATIC - no expanded kwarg allowed: core_render's
    # collapsed-view path (1226) rewrites is_tree=False whenever a falsy
    # `expanded` flows through, which eats the tree style. PosixPath is the
    # concrete leaf type folder_io's CodeHost hands back.
    FILE_TREE_NAMES = {
        (dict, defaultdict): ModeOverrides(
            kwargs={"is_tree": True, "show_bg": False, "use_cache": True,
                    "show_add_delete": True, "indent_size": 8},
            recursive=True,
        ),
        # (Path, PosixPath): ModeOverrides(
        #     func=draw_file_name,
        #     recursive=True,
        # ),
    }

    # ── Inner modes for draw_with_modes children ────────────
    # Each operates on a GeneralParse and wraps a simple renderer in just
    # the converter chain it needs. Used as the `modes` arg to
    # draw_with_modes in Mode.CODE so that load / save / file-watch
    # registration happen ONCE upstream and are shared across columns.

    CODE_INNER_TEXT = _CodeMode(_code_inner_text_policies)

    CODE_INNER_UI = _CodeMode(_code_inner_ui_policies)

    # Outer code mode: the chain references Mode.CODE_INNER_TEXT /
    # Mode.CODE_INNER_UI, enum members that only exist once the class is.
    CODE = _CodeMode(_code_policies)

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
def _address_pairs():
    from meltygui.code.chain_converters import class_to_address, address_to_class
    from meltygui.code.chain_converters import function_to_address, address_to_function
    from meltygui.code.chain_converters import module_to_address, address_to_module
    return {
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
                      include_overrides=False):
    """CODE_UI with `focus` in place of draw_collection: parse → focus → save.

    A class span parses to {<ClassName>: {...}}, so things INSIDE the class
    (class vars, decorator kwargs) need the class name prefixed onto the focus
    path. An override `# [...]` comment, however, lives at the span's MODULE
    level (above the class) and round-trips there — so code_comment passes
    prefix_class=False to target the top-level __overrides__ directly.

    ensure_import=(module, name) makes the save also insert that import if the
    file lacks it (so a synthesized @defaults decorator resolves).
    include_overrides extends a class span up to its leading override comment
    (class_to_address_incl_overrides)."""
    from meltygui.code.chain_converters import address_to_general_parse, general_parse_to_address
    from meltygui.code.chain_converters import class_to_address_incl_overrides, focus
    pairs = _address_pairs()
    load_node, save_node = pairs.get(type(root), pairs[type])
    if include_overrides and isinstance(root, type):
        load_node = class_to_address_incl_overrides
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
    label: str  # unique row id (kind + name) - drives draw_collection identity
    name: str  # the field/leaf name this lens reads/writes
    root: Any  # draw_state -> the object the lens starts from
    path: tuple  # in-place kinds: root -> path to the leaf
    default: Any = None  # value the "+ Add" affordance stamps in
    chain: Any = None  # code kinds: root -> generated chain tuple; None for in-place
    kind: str = ""  # short, clean display name (no attr name, no internal keys)
    tint: Optional[tuple] = None  # optional override tint for this source


# ── Lens kinds (generic over `name`) ──────────────────────────────────────────

def draw_state_attr(name, default=None):
    return Lens(f"Draw state · {name}", name, root=lambda ds: ds, path=(name,),
                default=default, kind="Draw state", tint=(0.1, 0.1, 0.3))


def instance_attr(name, default=None):
    return Lens(f"Instance attr · {name}", name,
                root=lambda ds: getattr(ds, "_raw_input_value", None),
                path=(name,), default=default, kind="Instance")


def class_var(name, default=None):
    return Lens(f"Class variable · {name}", name, root=_owning_source, path=(name,),
                default=default, kind="Class variable",
                chain=lambda root: _build_code_chain(root, (name,), default, "Class variable"))


_DEFAULTS_IMPORT = ("meltygui.core.rendering.core_decoration", "defaults")


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
                                                     prefix_class=False, include_overrides=True))


def caller_arg(name, default=None):
    """Edit the literal a caller passed for `name`, e.g. draw_text(tint=(1,0,1)).
    Anchored on the call site (draw_state._call_site — the (filename, lineno)
    resolved once when frames were grabbed on menu-open), not on the data or its
    class. The chain is fixed (independent of root type): site → call-statement
    Address → parse the Call's kwargs → focus → save.

    Reads the cached site rather than re-walking the live stack: during a tint
    drag the stack changes (parents are skipped), so re-deriving would flip the
    site mid-drag and cancel the edit."""
    def chain(root):
        from meltygui.code.chain_converters import caller_to_address, address_to_call_parse
        from meltygui.code.chain_converters import call_dict_to_save, focus
        return (caller_to_address,
                (address_to_call_parse, {'load': True}),
                (focus, {'path': (name,), 'default': default, 'kind': "Caller"}),
                (call_dict_to_save, {'save': True}))
    return Lens(f"Caller arg · {name}", name,
                root=lambda ds: getattr(ds, "_call_site", None),
                path=(name,), default=default, kind="Caller", chain=chain)


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
