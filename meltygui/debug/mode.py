import types
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Any

from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.toggles import WindowManager
from src.lsd.gl_gui.view.core_conversion.chain_converters import module_to_address, address_to_general_parse, \
    general_parse_to_address, address_to_module, class_to_address, address_to_class, function_to_address, \
    address_to_function, general_parse_to_str, str_to_general_parse
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
 sort_dict_alphabetically, unsort_dict_alphabetically, draw_with_modes
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
        config = self.value.get(the_type, None)
        if config is not None:
            return config

        for super_type in type(input_value).__mro__:
            config = self.value.get(super_type, None)
            if config is not None:
                return config
            elif Any in self.value:
                return self.value[Any]
        return None
        
    # [tint=(0.2,0.5,0.1)]
    SORT = {
        defaultdict: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True},
            recursive=True,
            func=(sort_dict_alphabetically,
                  (draw_collection, {"show_add_delete":False, "selectable":False, "show_bg": False}),
                  
          ))
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

    auto_load = True
    auto_save = True
    auto_recompile = False

    CODE_UI = {
        types.FunctionType: ModeOverrides(
            recursive=True,
            func=(function_to_address,
                  (address_to_general_parse, {'load': auto_load}),
                  (draw_collection, {"show_add_delete": True}),
                  (general_parse_to_address, {'save': True,
                                              'recompile': True}),
                  address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            func=(module_to_address,
                  (address_to_general_parse, {'load': auto_load}),
                  (draw_collection, {"show_add_delete": True}),
                  (general_parse_to_address, {'save': True,
                                              'recompile': True}),
                  address_to_module),
        ),
        type: ModeOverrides(
            func=(class_to_address,
                  (address_to_general_parse, {'load': auto_load}),
                  (draw_collection, {"show_add_delete": False}),
                  (general_parse_to_address, {'save': True,
                                              'recompile': True}),
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
 
    code_plain_text_auto_load = False
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
                  (address_to_general_parse, {'load': True}),
                  (draw_with_modes, {'modes': inner_modes, 'disable_scroll': True, 'fill_height': compute_height}),
                  (general_parse_to_address, {'save': True, 'recompile': False}),
                  address_out),
        )

    Mode.CODE.value.update({
        types.FunctionType: chain_for(function_to_address, address_to_function),
        types.ModuleType: chain_for(module_to_address, address_to_module),
        type: chain_for(class_to_address, address_to_class),
    })


_populate_code_mode()


class ModeGroup:
    CODE = (Mode.CODE_UI, Mode.CODE_PLAIN_TEXT)

