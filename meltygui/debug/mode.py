import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Any

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
from src.lsd.gl_gui.view.core_views.headers import draw_footer, draw_header_end, draw_header
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection, draw_comment, draw_search_results, \
    draw_general_parse
from src.lsd.gl_gui.view.core_views.text_editor import draw_text


@dataclass
class ModeOverrides:
    kwargs: Optional[dict] = None
    func: Optional[callable] = None
    recursive:Optional[bool] = True

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

    SEARCH = {
        dict: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True},
            recursive=False,
            func=draw_search_results
        ),

        str: ModeOverrides(
            kwargs={"show_bg": True, "selectable": False, "use_cache": True},
            recursive=False,
            func=draw_search_results
        )
    }

    WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "melty_window":True, "closable":True,
                    "with_header_end":draw_header_end, "auto_resize":False, "draggable":True,
                    "show_tint":True, "show_header":True, "with_footer":draw_footer, "min_width":60, "min_height":50,
                    "disable_scroll":False},
            recursive=False
        )
    }


    WINDOW_CLEAN = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "shadow":True,
                    "melty_window":True, "closable":True,
                    "auto_resize":True, "is_tree":False, "show_tint":False, "show_header":True,
                    "disable_scroll":False},
            recursive=False
        )
    }

    CODE_DICT_STR = {
        cst.Module: ModeOverrides(
            kwargs={"convert_in": [cst_to_dict, rf_dict_to_str],
                    "convert_out": [str_to_cst_module],
                    },
            recursive=True,
            func=draw_text
        ),

        types.FunctionType: ModeOverrides(
            kwargs={"convert_in": [fn_to_cst],
                    "convert_out": [cst_to_fn],

                    },
            recursive=True,
        ),

        types.ModuleType: ModeOverrides(
            kwargs={"convert_in": [mod_to_cst],
                    "convert_out": [cst_to_mod],
                    },

            recursive=True
        ),
    }
    CODE_UI = {
        types.FunctionType: ModeOverrides(
            recursive=True,
            func=(function_to_address,
                  (address_to_general_parse, {'load': True}),
                  draw_collection,
                  (general_parse_to_address, {'save': True,
                                              'recompile': True}),
                  address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            func=(module_to_address,
                  (address_to_general_parse, {'load': True}),
                  draw_collection,
                  (general_parse_to_address, {'save': True,
                                              'recompile': True}),
                  address_to_module),
        ),
        type: ModeOverrides(
            func=(class_to_address,
                  (address_to_general_parse, {'load': True}),
                  draw_collection,
                  (general_parse_to_address, {'save': True,
                                              'recompile': True}),
                  address_to_class),
            recursive=True
        ),



        Conditional: ModeOverrides(
            kwargs={"tint": (0.2, 0.2, 0.1), 'show_add_delete': False, 'is_tree':False},
            recursive=True,
        ),

        Comment: ModeOverrides(
            kwargs={"tint": (0.2, 0.2, 0.1), 'show_add_delete': False, 'is_tree':False},
            func=draw_comment,
            recursive=True,
        ),

        GeneralParse: ModeOverrides(
            kwargs={'show_add_delete': False, "disable_scroll": False},
            recursive=True,
            func=draw_general_parse
        ),

    }

    DEFAULT = {
        Any: ModeOverrides(
            kwargs={},
            recursive=True,
        )
    }

    CODE_PLAIN_TEXT = {

        types.FunctionType: ModeOverrides(
            recursive=True,
            func=(function_to_address,
                  (address_to_general_parse, {'load': True}),
                  general_parse_to_str,
                  draw_text,
                  str_to_general_parse,
                  (general_parse_to_address, {'save': False,
                                              'recompile': False}),
                  address_to_function),
        ),
        types.ModuleType: ModeOverrides(
            recursive=True,
            func=(module_to_address,
                  (address_to_general_parse, {'load': True}),
                  general_parse_to_str,
                  draw_text,
                  str_to_general_parse,
                  (general_parse_to_address, {'save': False,
                                              'recompile': False}),
                  address_to_module),
        ),
        type: ModeOverrides(
            func=(class_to_address,
                  (address_to_general_parse, {'load': True}),
                  general_parse_to_str,
                  draw_text,
                  str_to_general_parse,
                  (general_parse_to_address, {'save': False,
                                              'recompile': False}),
                  address_to_class),
            recursive=True
        ),

        # str: ModeOverrides(
        #     kwargs={"mode": None, "indent_size": 30, "with_header": draw_header},
        #     func=draw_text,
        #     recursive=True,
        # ),
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

    # ── File as plain text ───────────────────────────────────
    #
    # Path on disk → decoded string, drawn in a text editor.
    # Good for: README, .txt, .md, .json - anything you want as raw text.

    # FILE_TEXT = {
    #     Path: ModeOverrides(
    #         kwargs={"convert": [Path, bytes, str]},
    #         func=draw_str,
    #         recursive=True,
    #     ),
    # }
