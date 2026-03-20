import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Any

from src.lsd.gl_gui.view.core_conversion.file_converters import path_to_dict, bytes_to_str, load_text, recompile_module, \
    recompile, fn_to_cst, cst_to_fn, recompile_fn
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import GeneralParse, Conditional, Comment, \
    cst_to_dict, dict_to_cst
from src.lsd.gl_gui.view.core_views.headers import draw_footer, draw_header_end, draw_header
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection, draw_comment
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

    WINDOW = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "melty_window":True, "closable":True,
                    "with_header_end":draw_header_end, "min_width":150, "min_height":40,
                    "auto_resize":False, "draggable":True, "show_tint":True, "show_header":True, "with_footer":draw_footer,
                    "disable_scroll":False},
            recursive=False
        )
    }

    WINDOW_CLEAN = {
        Any: ModeOverrides(
            kwargs={"show_bg":True, "selectable":False, "use_cache":True, "melty_window":True, "closable":True,
                    "auto_resize":True, 'min_width':300, "draggable":True, "is_tree":False, "show_tint":False, "show_header":False,
                    "disable_scroll":False},
            recursive=False
        )
    }

    CODE_DICT_STR = {
        cst.Module: ModeOverrides(
            kwargs={"convert": [cst.Module, dict, str]},
            recursive=True,
            func=draw_text
        ),

        types.FunctionType: ModeOverrides(
            kwargs={"convert": [types.FunctionType, cst.Module]},
            recursive=True,
        ),

        types.ModuleType: ModeOverrides(
            kwargs={"convert": [types.ModuleType, cst.Module]},
            recursive=True
        ),
    }
    CODE_UI = {
        cst.Module: ModeOverrides(
            kwargs={"convert": [cst.Module, dict]},
            func=draw_collection,
            recursive=True
        ),

        types.FunctionType: ModeOverrides(
            kwargs={"convert_in": [fn_to_cst, cst_to_dict],
                    "convert_out": [dict_to_cst, cst_to_fn],
                    "auto_apply": [load_text, recompile_fn]},
            recursive=True,
            func = draw_collection
        ),

        type: ModeOverrides(
            kwargs={"convert": [type, cst.Module], "auto_apply": [load_text, recompile]},
            recursive=True
        ),

        types.ModuleType: ModeOverrides(
            kwargs={"convert": [types.ModuleType, cst.Module], "auto_apply": [load_text, recompile_module]},
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
            func=draw_collection
        ),

    }

    DEFAULT = {
        Any: ModeOverrides(
            kwargs={},
            recursive=True,
        )
    }

    CODE_PLAIN_TEXT = {
        cst.Module: ModeOverrides(
            kwargs={"convert": [cst.Module, str], "horizontal":True},
            func=draw_text,
            recursive=True
        ),

        types.FunctionType: ModeOverrides(
            kwargs={"convert": [types.FunctionType, cst.Module],
                    "auto_apply": [load_text],
                    "indent_size": 5, "with_header": draw_header},
            recursive=True
        ),

        types.ModuleType: ModeOverrides(
            kwargs={"convert": [types.ModuleType, cst.Module],
                    "auto_apply": [load_text],
                    "indent_size": 5, "with_header": draw_header},
            recursive=True
        ),

        type: ModeOverrides(
            kwargs={"convert": [type, cst.Module],
                    "auto_apply": [load_text],
                    "indent_size": 5, "with_header": draw_header},
            recursive=True
        ),

        str: ModeOverrides(
            kwargs={"convert": None, "mode": None, "indent_size": 30, "with_header": draw_header},
            func=draw_text,
            recursive=True,
        ),
    }

    # ── File metadata ────────────────────────────────────────
    #
    # Path on disk → metadata dict (name, size, modified, raw bytes).
    # Good for: file browsers, file inspectors, drag-and-drop targets.



    FILE_META = {
        Path: ModeOverrides(
            kwargs={"convert": [path_to_dict]},
            func=draw_collection,
            recursive=True,
        ),
        bytes: ModeOverrides(
            kwargs={"convert": [bytes_to_str]},
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
