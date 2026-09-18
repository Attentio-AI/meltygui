import inspect
import os
import re
import sys
import threading
import time
import traceback
import types
from collections import deque, defaultdict, namedtuple
from collections.abc import MutableMapping
from enum import Enum
from inspect import Parameter
from math import sqrt
from pathlib import Path
from types import NoneType
from typing import Any

import OpenGL.GL as gl
import meltygui.core.windowing.window_api as glfw
import math
import numpy
from meltygui_imgui.core import _DrawList

from meltygui.core.styling.fonts import Font
from meltygui.core.styling.global_style import GlobalStyle
from meltygui.core.melty import Melty
from meltygui.core.melty import CollectionAction
from meltygui.core.melty import ManagedWindow
from meltygui.core.melty import SearchTerm
from meltygui.core.conversion.render_host import RenderHost
from meltygui.core.rendering.shaped import Shaped
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.modes import Modes
from meltygui.core.diagnostics.notifications import display
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.runtime.toggles import Tint
from meltygui.core.runtime.toggles import mix
from meltygui.core.runtime.toggles import rgb_to_hsv
from meltygui.core.runtime.toggles import hsv_to_rgb
from meltygui.core.graphics.gl_state import GLState
from meltygui.utils.render_utils import print_colored_traceback
from meltygui.utils.render_utils import push_style_var
from meltygui.utils.render_utils import pop_style_var
from meltygui.utils.render_utils import end
from meltygui.utils.render_utils import begin
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.conversion.bubbling import _BubblingDict
from meltygui.core.conversion.bubbling import _DeepPath
from meltygui.core.conversion.cache_tree import UNSET_VALUE
from meltygui.code.libcst_conversion import Comment
from meltygui.code.libcst_conversion import GeneralParse
from meltygui.code.libcst_conversion import UsageRef
from meltygui.code.libcst_conversion import CallParse
from meltygui.code.libcst_conversion import ClassParse
from meltygui.code.libcst_conversion import EnumParse
from meltygui.code.libcst_conversion import FunctionParse
from meltygui.code.libcst_conversion import SymbolUsage
from meltygui.code.libcst_conversion import cst_module_to_dict
from meltygui.code.libcst_conversion import dict_to_cst_module
from meltygui.code.new_codecs import CallSite
from meltygui.code.new_converters import code_file_io
from meltygui.code.new_converters import convert_in_and_out_value
from meltygui.code.new_converters import cst_module_to_string
from meltygui.code.new_converters import string_to_cst_module
from meltygui.code.new_converters import code_hosts_for
from meltygui.code.new_converters import host_code_state
from meltygui.code.new_converters import recompile_button
from meltygui.code.new_converters import recompile_status
from meltygui.code.new_converters import run_recompile
from meltygui.core.conversion.path_finder import Pending
from meltygui.core.layout.cursor_core import same_line
from meltygui.core.cache.tile_cache import snap_int
from meltygui.core.cache.tile_cache import add_shadow
from meltygui.core.core_render import render_func
from meltygui.core.core_render import render_func_kwarg_names
from meltygui.core.core_render import SCROLL_BAR_WIDTH_DEFAULT
from meltygui.core.core_render import SCROLLBAR_MARGIN
from meltygui.core.rendering.parameter_core import SourcePriority
from meltygui.core.rendering.parameter_core import _source_priority
from meltygui.core.rendering.parameter_core import _sources_for
from meltygui.core.rendering.parameter_core import _driving_source
from meltygui.core.rendering.parameter_core import _setting_source
from meltygui.core.rendering.parameter_core import default_write_source
from meltygui.core.rendering.parameter_core import get_value_for_source
from meltygui.core.rendering.parameter_core import get_source_for
from meltygui.core.rendering.parameter_core import from_anywhere
from meltygui.core.rendering.parameter_core import anywhere_value
from meltygui.core.rendering.parameter_core import set_anywhere
from meltygui.core.rendering.parameter_core import flush_deferred_writes
from meltygui.core.rendering.parameter_core import SET_ANYWHERE_PARAMS
# Module import (not "from ... import DragDrop`) so hotswaps rebind cleanly.
import meltygui.core.input.drag_drop_core as _drag_drop
from meltygui.model.code_proxy_model import *
from meltygui.core.rendering.core_decoration import hotkey
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.cache.invalidation_decoration import live
from meltygui.core.rendering.window_decoration import window
from meltygui.view.header_view import draw_header
from meltygui.core.diagnostics.inspection_core import set_fn_defaults
from meltygui.view.text_view import draw_text
from meltygui.editor.text_editor import _scroll_into_view
from meltygui.graphics.texture_manager import PendingTexture
from meltygui.core.rendering.core_decoration import defaults
from meltygui.code.symbol_roster import pass_scope


def some_text(input_value: str, draw_state, **kwargs):
    imgui.text(f"Text: {input_value}")


# --- Word-aware fuzzy matcher (global search) ---------------------------------
# Identifiers are WORDS ("draw_any" -> draw, any; "TextEditor" -> text,
# editor). A query matches when its words each claim a DISTINCT target word:
#   * a query word claims a target word it PREFIXES exactly ("dr" -> draw),
#     or is an exact mid-word substring of when >= 2 chars ("raw" -> draw,
#     "ny" -> any -- never a lone char: "a" must START a word, so "draw_a"
#     never lands on draw_int / draw_collection via the "a" in draw), or
#     fuzzily matches when its FIRST character matches ("amy" -> any: 1 edit)
#     -- fuzz budget is only spent where the word start agrees;
#   * words are unordered ("any_draw" -> draw_any);
#   * a query without separators ("anydraw", "drawany", "rawany", "dra") is
#     tried as one word, then SEGMENTED into pieces that each claim a word by
#     the same rules ("any" + "draw").
# _word_match() returns the total edit cost (0 = exact) or None.
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def print_hello():
    print("Hello, world!")


from meltygui.core.automation.search_core import search_activate_target


from meltygui.view.collection_view import draw_collection
from meltygui.view.collection_view import fast_draw_collection


def main_header(input_value, name, **kwargs):
    imgui.text("Main Header")


    # value = input_value.fget(input_value)
    # draw_any(value, name="Value", show_bg=True, draw_state=draw_state)


@render_func(is_lens_for=(type), skip_draw=True)
def type_lens(input_value, view_func, child_kwargs, **kwargs):
    changed, value = view_func(**child_kwargs)
    if changed:
        for k, v in value.items():
            if hasattr(input_value, k):
                if k.startswith("_"):
                    continue
                try:
                    setattr(input_value, k, v)
                except Exception as e:
                    pass

    return changed, value


@render_func()
def class_to_var_dict(input_value: type, changed, draw_state, **kwargs):
    class_vars = {**{k: getattr(input_value, k) for k in vars(input_value)}}
    class_vars["__original__"] = input_value

    return changed, class_vars


@render_func()
def var_dict_to_class(input_value, changed, **kwargs):
    original_class = input_value.get("__original__", None)
    if original_class is None:
        imgui.text_colored("Error: No original class found in dict", 1.0, 0.0, 0.0, 1.0)
        return False, input_value

    if changed:
        for k, v in input_value.items():
            if k.startswith("_"):
                continue
            try:
                imgui.text(f"Setting attribute {k} to value {v} on class {original_class.__name__}")
                setattr(original_class, k, v)
            except Exception as e:
                imgui.text(f"Error setting attribute {k} on class {original_class.__name__}: {e}")

    return False, input_value


some_float = [0.0]
cst_dict = {}
test_code = None
selected_tabs = ["Alpha"]


@render_func(show_bg=True, with_header=draw_header)
def test_columns():
    draw_str("Column 1", name="col1", column=0)
    draw_int(123, name="col2", column=1)
    draw_float(0.5, name="col3", column=2)
    draw_float(0.5, name="test_5", column=5)
    draw_float(0.5, name="test_5_b", column=5)

    for i in range(10):
        draw_float(0.4, name=f"float_{i}", column=2)


@render_func(use_cache=False, shadow=False, show_bg=False, disable_scroll=False, selectable=False)
def run_chain(input_value, chain=None, draw_state=None, route=None,
              s_key_pressed=False, enter_key_pressed=False, unique=None, debug=False, **kwargs):
    """Debug render function: executes a chain step by step with imgui output.

    Shows function name, changed flag, output type, and a value preview
    at each stage.  Color coded: green=changed, gray=cached, yellow=pending.
    """
    if chain is None:
        imgui.text("No chain provided")
        return False, input_value

    value = input_value
    changed = False

    if debug:
        imgui.text(f"Chain: {len(chain)} nodes")
        imgui.text(f"Input: {type(input_value).__name__}")
        imgui.separator()

    cache_tree = draw_state._chain_stack
    cache_tree.begin()

    mode_cache = chain[0][1].get("mode_cache", False) if isinstance(chain[0], dict) else False
    if mode_cache:
        value = cache_tree.step(changed, value)

    to_route = {}

    for i, func in enumerate(chain):
        if isinstance(func, tuple):
            func, func_kwargs = func[0], func[1]
        else:
            func_kwargs = {}

        if debug:
            if not changed:
                name = getattr(func, '__name__', repr(func))
                imgui.text(f"  [{i}] {name} — (no change)")

        func_kwargs['name'] = f"{func.__name__}{i}{kwargs.get('name', f'')}{unique}"
        func_kwargs['shadow'] = False
        func_kwargs['changed'] = changed
        func_kwargs['show_header'] = False
        func_kwargs['s_key_pressed'] = s_key_pressed
        func_kwargs['enter_key_pressed'] = enter_key_pressed
        func_kwargs['draw'] = True
        func_kwargs['real_type'] = type(input_value)
        for arg_name, arg_val in to_route.values():
            func_kwargs[arg_name] = arg_val

        next_cached = cache_tree.peek()
        if isinstance(value, str):
            imgui.text(f"  [{i}] {func.__name__} — str: '{value[:30]}'")

        imgui.begin_group()
        changed, value = func(input_value=value, reference=next_cached, **func_kwargs)
        imgui.end_group()
        #
        # if not changed:
        #     value = None

        if isinstance(value, Pending):
            changed = False
            value = None

        mode_cache = func_kwargs.get('mode_cache', True)
        if mode_cache:
            value = cache_tree.step(changed, value)

        if route is not None:
            if func in route:
                arg_name = route[func]
                to_route[arg_name] = arg_name, value

    cache_tree.end()

    return changed, value

# Nested sample data for the recursive dropdown demo.
dropdown_demo_data = {
    "small": 12,
    "medium": 16,
    "large": 24,
    "color": {
        "rgb": {"red": (1.0, 0.0, 0.0), "green": (0.0, 1.0, 0.0)},
        "named": {"steel": "#4682b4", "teal": "#008080"},
    },
    "alignment": ["left", "center", "right"],
}


drop_down_selection = None
# draw_main logs its section split to the perf log for any call slower than
# this (ms) - the root handler's frame-by-frame spikes were untraceable
# otherwise. [tint=(0.95, 0.55, 0.15)]
_DM_TRACE_MS = 1.5
# hey there

@render_func
def test_widget(input_value, name, unique, **kwargs):
    imgui.text("Test Widget")
    draw_text("Editable Text", name="editable_text", show_bg=True)


source = "x = foo(val=1)\nprint(x)\nsome_list=[0, 1, 2, 3]\n"
module = cst.parse_module(source)
proxy = cst_wrap(module)
name_edits = {}
code_export_str = "Test"


# Main draw function, called by the GUI framework

@live
class TestObj:
    def __init__(self):
        self.test_val = 0.0
        self.test_list = [1, 2, 3, 4, 5]


test_obj = TestObj()


def draw(vis):
    draw_melty_windows(vis)


def export_code(test_param_2: int = 5):
    # print(f"hello {test_param_2}")
    global code_export_str
    code_export_str = proxy.node.code


@hotkey(glfw.KEY_O)
def toggle_offscreen():
    if Core.melty.cache.enabled:
        Core.melty.cache.set_enabled(False)
    else:
        Core.melty.cache.set_enabled(True)


import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import scale_saturation
# new comment


bg_style_default = {
    "value": 0.01,
    "saturation": 1.2,
    "alpha": 1.0,
    'max_value': 1.0
}


def get_bg_color(depth, rounding, style_manager, auto_resize):
    depth_factor = GlobalStyle.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = GlobalStyle.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))

    hovered_offset = 0.0

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    global bg_style_default
    bg_style = GlobalStyle.get_global_constant("bg_style", default=bg_style_default, folder="bg_styles")
    outline_factor = GlobalStyle.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    if not auto_resize:
        outline_factor *= 1.3

    if auto_resize:
        bleed_factor = 0.2
    else:
        bleed_factor = 0.0
    bg_bleed = Core.melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.6,
                                                alpha=1.0, saturation=1.8)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))
    bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)
    return bg_color


def seperator(height):
    imgui.dummy(0, snap_int(height / 2))
    imgui.separator()
    imgui.dummy(0, snap_int(height / 2))


def test_func():
    # Some comment
    # Comment here
    some_val = 1.706
    some_dict = {"some_key": -0.02,
                 "key": False,
                 "key_2": 2.421
                 }


from meltygui.model.color_model import _clamp_bg_value


def compute_bg_color(bg_offset=0, tint=None, nested_bg=False, max_bg_depth=None, max_bg_value=None):
    depth_wrap = 34
    depth_scale = 1.629
    intensity_factor = 0.021
    intensity_offset = -0.336
    outline_depth_mul = 0.786
    # More text
    bleed_style = {'value': -0.111, 'alpha': 1.12, 'saturation': 7.045}

    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )

    # -- Depth calculation -------------------
    max_depth = 15
    bg_depth = Core.melty.bg_depth if Core.melty.bg_depth is not None else 0
    bg_offset = bg_offset if bg_offset is not None else 0
    wrapped_depth = min(max_depth, (bg_depth % depth_wrap) + bg_offset)
    # Caller-supplied ceiling on the effective depth: past this step the color
    # keeps the palette value for max_bg_depth instead of getting lighter.
    if max_bg_depth is not None:
        wrapped_depth = min(wrapped_depth, max_bg_depth)
    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = (scaled_depth + intensity_offset) * intensity_factor
    max_depth_intensity = 0.652
    depth_intensity = min(depth_intensity, max_depth_intensity)

    # ── Outline color ──────────────────────────────────────────
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = 0.501 if nested_bg else 0.446

    bleed_base = Core.melty.get_bg_color(-1)
    bleed_color = Melty.style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Fill rendering ─────────────────────────────────────────
    bg_color = Melty.style_manager.make_color_style_value(input=bg_style, value=max(0.0, depth_intensity))
    bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    return _clamp_bg_value(bg_color, max_bg_value)


# (style hsv, bg colour −2, bg colour −1, outline value, sat) → (bleed, outline)
_DRAW_BG_COLOUR_MEMO = globals().get("_DRAW_BG_COLOUR_MEMO", {})
_DRAW_BG_FILL_MEMO = globals().get("_DRAW_BG_FILL_MEMO", {})     # (colour key, sat, depth, bleed) → fill rgb


from meltygui.view.control_view import text


from meltygui.view.control_view import draw_str


def sort_dict_alphabetically(input_value, **kwargs):
    changed = False
    attr_name = "name"
    first_item = next(iter(input_value.items()), None)[1]
    if hasattr(first_item, attr_name):
        sorted_dict = dict(sorted(input_value.items(), key=lambda item: str(getattr(item[1], attr_name)).lower()))
        return changed, sorted_dict
    else:
        imgui.text("Cannot sort: items do not have 'name' attribute")
        return False, input_value


@render_func()
def unsort_dict_alphabetically(input_value, ref=None, changed=False):
    if ref is None:
        imgui.text("Original order not available")
        return False, input_value
    else:
        # Ref is the original dict
        ref.update(input_value)
        return changed, ref


def param_source_matrix(input_value, keys=None, func=None, include_unmatched=False, **kwargs):
    """Pivot a render function's possible INPUTS into a parameter × source
    table — the inputs-tab aggregator. `input_value` is the collected sources,
    a {source_name: {param: value}} mapping (caller kwargs, @defaults on the
    model class, mode kwargs, @render_func decorator kwargs, signature
    defaults, …); `keys` is the function's parameter-name list (rows) — pass it
    directly, or pass `func` and they're derived via inspect (unwrapped, minus
    the catch-all params). Returns {param: {source_name: value}}:

        rows     one per parameter, in parameter order — an EMPTY row means no
                 source sets it (still shown: the point is mapping the full
                 input surface in one spot)
        columns  one per source that sets the param, in source order

    Cells alias the source values (no copies). With include_unmatched, keys a
    source sets that are NOT parameters append as extra rows at the end —
    typos and **kwargs ride-throughs stay visible instead of vanishing.
    Shaped like sort_dict_alphabetically: a plain (changed, value) chain node;
    apply_param_source_matrix below is the unsort-style reverse.
    Source COLOR-CODING is not this function's job: codecs carry a tint
    (new_codecs.render_kwargs) that core_render merges in as the lowest
    kwargs layer, so codec-backed values color themselves wherever drawn."""
    changed = False
    if isinstance(input_value, dict):
        items = list(input_value.items())
    else:
        items = [(f"source_{i}", s) for i, s in enumerate(input_value or ())]
    items = [(str(n), s) for n, s in items if isinstance(s, dict)]

    if keys is None and func is not None:
        try:
            keys = [p for p in inspect.signature(inspect.unwrap(func)).parameters
                    if p not in ("args", "kwargs", "o_kwargs", "next_kwargs")]
        except (TypeError, ValueError):
            keys = []
        # A render func's input surface is its signature PLUS the kwargs the
        # @render_func machinery itself consumes (width/height/tint/shadow and
        # the flag zoo) - shared by every render func, loaded once per process
        # by re-scanning the decorator source (render_func_kwarg_names).
        if getattr(func, "__render_func__", False):
            _seen = set(keys)
            keys += [k for k in render_func_kwarg_names() if k not in _seen]
    keys = list(keys or [])

    matrix = {}
    for k in keys:
        row = {}
        for sname, sdict in items:
            if k in sdict:
                row[sname] = sdict[k]
        matrix[k] = row
    if include_unmatched:
        for sname, sdict in items:
            for k in sdict:
                # Parse metadata is NOT an input: skip non-plain-str keys
                # (Comment objects keying their own line in a class-body
                # parse), dunders/underscored bookkeeping, and the parse's
                # section keys - only real attribute names become rows.
                if (type(k) is not str or k.startswith('_')
                        or k in _PARSE_SECTION_KEYS):
                    continue
                if k not in matrix or (k not in keys and sname not in matrix[k]):
                    matrix.setdefault(k, {})[sname] = sdict[k]
    return changed, matrix


# Attributes ALWAYS in the inputs-tab param list (pinned right after the view
# function's own signature params), even if no source sets them organically -
# they come from the @render_func machinery, not the view function's signature.
MATRIX_DEFAULT_PRIORITY = ("view_func", "width", "height", "min_height", "min_width", "tint")

# Structural sections of a GeneralParse dict - never attribute names, so
# param_source_matrix's include_unmatched must not turn them into rows when a
# a class/function parse is registered as a source (the class-var source).
_PARSE_SECTION_KEYS = frozenset({"decorators", "parameters", "locals"})

# Framework-injected parameters: present in most view-function signatures but
# never user-tunable, so they don't belong in the signature section.
_MATRIX_FRAMEWORK_PARAMS = {"input_value", "draw_state", "args", "kwargs",
                            "o_kwargs", "next_kwargs", "meta", "viewstate",
                            "self", "unique", "changed"}


def signature_param_names(func):
    """The view function's OWN tunable parameters (unwrapped signature minus
    the framework-injected names) plus MATRIX_DEFAULT_PRIORITY — the params
    pinned to the front of the inputs-tab list. For draw_float that's
    min_value/max_value/speed/…; width/height/min_height/min_width/tint ride
    along from the default list."""
    try:
        params = inspect.signature(inspect.unwrap(func)).parameters
    except (TypeError, ValueError):
        return []
    names = [p for p in params if p not in _MATRIX_FRAMEWORK_PARAMS]
    names += [k for k in MATRIX_DEFAULT_PRIORITY if k not in names]
    return names


@render_func()
def apply_param_source_matrix(input_value, ref=None, changed=False):
    """Reverse of param_source_matrix — the unsort_dict_alphabetically analog.
    `ref` is the ORIGINAL {source_name: dict} sources mapping; every edited
    cell writes back into the source dict it came from (a tint edited under
    the 'caller' column lands in the caller-kwargs dict), so each source's own
    save path can persist it. Sources absent from ref are left untouched."""
    if ref is None:
        imgui.text("Original sources not available")
        return False, input_value
    for param, row in input_value.items():
        if not isinstance(row, dict):
            continue
        for sname, val in row.items():
            src = ref.get(sname) if isinstance(ref, dict) else None
            # Skip already-equal cells: bubbling-wrapped source dicts mark
            # their host dirty on each write, so only real edits write back.
            if isinstance(src, dict) and (param not in src or src[param] is not val):
                src[param] = val
    return changed, ref


# Picker layout shared by the popover callers (draw_tuple, draw_tuple_fast,
# the editor's swatches) — they size the fixed popover window from it.
# [tint=(0.85, 0.75, 0.05)]
PICKER_SQUARE = Toggles.ColorPicker.square_size
# [tint=(0.85, 0.75, 0.05)]
PICKER_TABS_HEIGHT = Toggles.ColorPicker.tabs_height
# The sRGB+ tab's wide-gamut extension, to the RIGHT of the classic square
# (same height as the square; its width is what the tab adds to the popover).
# [tint=(0.85, 0.75, 0.05)]
PICKER_EXTENSION = Toggles.ColorPicker.extension_width
# The sRGB+ tab's exposure band ABOVE the square (and its extension): white
# at the seam up to 2^Toggles.HDR.picker_max_stops at the top. Every tab
# reserves this height above its square, so the classic square sits at the
# same screen spot whichever tab is showing.
# [tint=(0.85, 0.75, 0.05)]
PICKER_EXPOSURE_BAND = Toggles.ColorPicker.exposure_band_height
# Gap between the swatch's bottom edge and the popover's top.
# [tint=(0.85, 0.75, 0.05)]
PICKER_ANCHOR_GAP = Toggles.ColorPicker.anchor_gap
# The view-offset rows (draw_view_offsets_fast) under the colour tabs when
# the picker edits a view: a separator + one drag row per offset.
# [tint=(0.85, 0.75, 0.05)]
PICKER_OFFSETS_HEIGHT = Toggles.ColorPicker.offsets_height


from meltygui.core.styling.color_core import _style_policy_source


from meltygui.core.styling.color_core import _add_style_policy


# The view params the picker edits beside the colour, with their drag rows:
# (param, label, drag speed). Both are ints read and written through the
# owner's `locate_<param>` — the wrapper's `bg_offset` (palette depth the
# view's background is sampled at) and `z_offset` (its shadow / paint depth).
# Add a row here to expose another wrapper kwarg.
# [tint=(0.85, 0.75, 0.05)]
VIEW_OFFSET_ROWS = Toggles.ColorPicker.view_offset_rows


class TestClass(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 2
        self.str_val = "Test"


from meltygui.view.control_view import draw_float


@render_func(wraps=render_func, show_add_delete=False, with_header=draw_header)
def eval_function(input_value, draw_state):
    signature = inspect.signature(input_value)
    params = signature.parameters
    changed, new_val = draw_any(params, name="Parameters", show_add_delete=False)
    if changed:
        set_fn_defaults(input_value, new_val)

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))
    push_style_var(imgui.STYLE_FRAME_PADDING, (8, 6))
    push_style_var(imgui.STYLE_FRAME_ROUNDING, 6)

    function_args = inspect.signature(input_value).parameters
    kwargs = {}
    for name, param in function_args.items():
        if param.default is not inspect.Parameter.empty:
            kwargs[name] = param.default
        else:
            kwargs[name] = None
    try:
        result = input_value(**kwargs)
        draw_any(result, name="Result", show_header=True, show_add_delete=False)
        if draw_state._result != result:
            Core.melty.cache.invalidate_all()
        draw_state._result = result

    except Exception as e:
        print(f"Error calling function '{input_value.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())

    pop_style_var(3)

    return changed, input_value


def _format_run_error(exc):
    """One compact, UI-ready error: `Type: message`, then the deepest
    traceback frame in PROJECT code (site-packages/stdlib frames are where
    the error SURFACED, not where it's fixable) with its source line."""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    target = None
    for fr in reversed(frames):
        if "site-packages" not in fr.filename and "/lib/python" not in fr.filename:
            target = fr
            break
    if target is None and frames:
        target = frames[-1]
    text = f"{type(exc).__name__}: {exc}"
    if target is not None:
        text += f"\n{Path(target.filename).name}:{target.lineno} in {target.name}"
        if target.line:
            text += f"\n    {target.line}"
    return text


# Runner draw states holding a run's `result` - what the CUDA-OOM responder
# frees (a forward pass result is typically the largest single live object).
_RUN_RESULT_HOLDERS = globals().get("_RUN_RESULT_HOLDERS")
if _RUN_RESULT_HOLDERS is None:
    import weakref as _weakref
    _RUN_RESULT_HOLDERS = _weakref.WeakSet()


# Runner draw_states with a threaded run IN FLIGHT - draw_function's
# single-flight policy. Process-living and NOT serialized: this latch used
# to be `draw_state.misc["_run_busy"]`, and it rides in custom.pkl with
# the draw_state, so a restart while a run was in flight (2026-08-23: the
# Pending Saves recompile) reloaded the runner as "busy" in every later
# session - locked forever, every new run refused as "already running".
_RUN_BUSY = globals().get("_RUN_BUSY")
if _RUN_BUSY is None:
    import weakref as _weakref
    _RUN_BUSY = _weakref.WeakSet()


def is_run_busy(draw_state) -> bool:
    return draw_state in _RUN_BUSY


def run_busy_begin(draw_state) -> bool:
    """Claim the runner for a run. False if one is already in flight."""
    if draw_state in _RUN_BUSY:
        return False
    _RUN_BUSY.add(draw_state)
    return True


def run_busy_end(draw_state) -> None:
    _RUN_BUSY.discard(draw_state)


def _release_run_results():
    for ds in list(_RUN_RESULT_HOLDERS):
        try:
            ds.result = None
            ds.misc.pop("_result_frame", None)
        except Exception:
            pass
    _RUN_RESULT_HOLDERS.clear()


def _respond_to_cuda_oom(exc, where):
    try:
        from meltygui.core.runtime.gc_manager import respond_to_cuda_oom
        from meltygui.core.runtime.gc_manager import OOM_RELEASE_HOOKS
        if not any(getattr(h, "__name__", None) == "_release_run_results"
                   for h in OOM_RELEASE_HOOKS):
            OOM_RELEASE_HOOKS.append(_release_run_results)
        respond_to_cuda_oom(exc, where=where)
    except Exception:
        pass


from meltygui.view.control_view import draw_int


def eval_input_scope(draw_state):
    """{param: value} for every input the context menu's INPUTS tab lists on
    `draw_state`'s view: the view function's own params, the header
    function's params (the resolved `with_header`), and the kwargs the
    @render_func wrapper itself consumes (show_bg, width, tint, ...). Each
    value is what the inputs tab DISPLAYS for it — anywhere_value (the
    wrapper-resolved `_kwargs`, the ds field fallback, an in-flight
    set-anywhere value), else the declared signature default — so typing
    `show_bg` in the Eval tab answers with the value actually driving the
    view. Layered UNDER the call-time locals by run_scoped_eval: a name the
    view function receives keeps its exact argument."""
    from meltygui.core.rendering.parameter_core import anywhere_value
    from meltygui.core.rendering.parameter_core import header_param_names
    from meltygui.core.rendering.parameter_core import signature_default_for
    from meltygui.core.rendering.parameter_core import view_param_names
    from meltygui.core.core_render import render_func_kwarg_names
    names = []
    seen = set()
    for group in (view_param_names(draw_state), header_param_names(draw_state),
                  render_func_kwarg_names()):
        for name in group:
            if name not in seen and name.isidentifier():
                seen.add(name)
                names.append(name)
    scope = {}
    for name in names:
        try:
            value = anywhere_value(name, draw_state)
            if value is None:
                value = signature_default_for(name, draw_state)
        except Exception:
            value = None
        scope[name] = value
    return scope


def run_scoped_eval(code, view_func, draw_state, local_vars):
    """Run `code` with the view function's ACTUAL call-time locals in scope.

    Called from the render wrapper (core_render) right before it invokes the
    view function, so `local_vars` is the exact set of arguments the function is
    about to receive -- its initial locals (`input_value`, `draw_state`, and
    every kwarg by name). On top of those we layer the function's module globals
    (so the snippet resolves the same free names the body would) plus the `value`
    and `ds` aliases. Locals win over globals, mirroring normal scoping.

    Delegates to the same eval/exec + stdout-capture machinery the MCP
    `eval_python` tool uses, so a trailing expression's repr comes back alongside
    any printed output. The namespace is a fresh dict layered over a *copy* of
    the module globals, so assignments in the snippet don't leak back into the
    module.
    """
    from meltygui.core.automation.mcp_eval import _run_code
    ns = {}
    if view_func is not None:
        # Same free names the function body resolves.
        ns.update(getattr(view_func, "__globals__", {}))
    # Every input the eval tab lists (view / header / wrapper params) at its
    # displayed value; the call-time locals below override the ones the
    # function actually receives.
    try:
        ns.update(eval_input_scope(draw_state))
    except Exception:
        pass
    if local_vars:
        ns.update(local_vars)  # the view function's call-time locals
    ns.setdefault("draw_state", draw_state)
    ns.setdefault("ds", ns.get("draw_state"))
    ns.setdefault("value", ns.get("input_value"))

    # Record the EXACT eval scope (the function's call-time locals + the ds/value
    # aliases) into the per-function metadata cache, so the eval tab's autocomplete
    # gives type-accurate suggestions on the next load. Keyed on the wrapper
    # (draw_state._view_func) to match what the eval tab looks up. Globals aren't
    # recorded -- they're resolved live from the function's __globals__ at
    # completion time. Best-effort; a hiccup here must never fail the eval.
    try:
        from meltygui.core.rendering.func_metadata import FuncsMetadata
        cache_key = getattr(draw_state, "_view_func", None) or view_func
        scope = eval_input_scope(draw_state)
        scope.update(local_vars or {})
        for alias in ("draw_state", "ds", "value", "input_value"):
            scope[alias] = ns.get(alias)
        FuncsMetadata.record(cache_key, scope)
    except Exception:
        pass

    out, result, error = _run_code(code, ns)
    parts = []
    if out.strip():
        parts.append(out.rstrip())
    if result is not None:
        parts.append("=> " + result)
    if error:
        parts.append(error.rstrip())
    return "\n".join(parts) if parts else "(no output)"


# ── Context menu tabs ────────────────────────────────────────────────────────
# Each tab body is its own render_func. draw_context_menu places one per selected
# column by calling it with column=t_idx; the tab then owns a single-column
# region, so the inner views inside it no longer pass column themselves.

class _SourceItem(str):
    """A source name as a dropdown value. `.tint` colors its row and the
    trigger via _dd_obj_tint — yellow marks the source actively driving
    the param."""

    def __new__(cls, name, tint=None):
        self = str.__new__(cls, name)
        self.tint = tint
        return self


_ACTIVE_SRC_TINT = (0.9, 0.8, 0.2)

# Multi-key child kwargs carried by the collection dict itself (it's
# __overrides__ path): the info tab's 'header' group starts collapsed —
# `initial` only applies on the child's first frames, so the chevron still
# works afterwards. The dunder key never renders (underscore-skipped).
_INFO_GROUP_OVERRIDES = {"__header__": {"initial": {"expanded": False}}}


from meltygui.core.conversion.render_host import RenderHost


class _InstanceAttrSource(dict):
    """The 'instance attr' source row: the value object's own whitelisted
    params (core_render.OBJ_ATTR_PARAMS — the attrs the wrapper injects, e.g.
    Loras.tint). Reads snapshot at collection time; a write goes straight to
    setattr on the LIVE object — in place and immediate, no code round trip
    (the same storage the instance_attr lens edits). Unlike the host-backed
    sources, a plain setattr triggers NOTHING — so the write also invalidates
    the target view's subtree (the wrapper re-injects the attr on the next
    render, which is also what clears the anywhere in-flight cache)."""

    def __init__(self, obj, target_ds=None):
        from meltygui.core.core_render import OBJ_ATTR_PARAMS
        super().__init__({p: getattr(obj, p) for p in OBJ_ATTR_PARAMS
                          if getattr(obj, p, None) is not None
                          and (p != "view_func" or p in getattr(obj, "__dict__", {}))})
        self._obj = obj
        self._target_ds = target_ds

    def __setitem__(self, k, v):
        setattr(self._obj, k, v)
        super().__setitem__(k, v)
        ds = self._target_ds
        if ds is not None:
            # invalidate_up: the attr (tint) paints the whole subtree's bg -
            # cached middle tiles would blit-skip dirty grandchildren.
            ds.invalidate_up(max_depth=6)
            request_render()


class _CodecSource(dict):
    """The 'codec' source row: the ACTIVE codec's render_kwargs — the
    wrapper's lowest kwargs merge layer and the provenance color (the green
    on import views etc.). The codec rides every draw_state as ds._codec.

    Writes are PER-FILE when the element resolves to one: the codec stamps
    the attribute into that file's entry in AppModel.file_meta_collection
    (Codec.update_file_meta), which persists with the root save and feeds the
    folder tree's row kwargs. Reads overlay that entry back over the
    codec-wide render_kwargs. Only when no file resolves does a write mutate
    the LIVE class attr in place — immediate but codec-wide and in-memory."""

    def __init__(self, codec, target_ds):
        rk = getattr(codec, "render_kwargs", None)
        super().__init__(rk if isinstance(rk, dict) else {})
        self._codec = codec
        self._target_ds = target_ds
        # Per-FILE overlay: attributes previously attached to this element's
        # file (Codec.update_file_meta → AppModel.file_meta_collection) read
        # back over the codec-wide render_kwargs, so the row round-trips
        # across saves. `order` is folder-tree bookkeeping, not a param.
        entry = codec.file_meta_entry(target_ds)
        if isinstance(entry, dict):
            self.update({k: v for k, v in entry.items() if k != "order"})

    def __setitem__(self, k, v):
        # Per-file first: when the element resolves to a file, the attribute
        # belongs to THAT file - the codec stamps it into the file-metadata
        # object (persisted with the root; the folder tree re-applies it as
        # row kwargs). Only when no file resolves does the write fall through to
        # the codec-wide live render_kwargs.
        if not self._codec.update_file_meta(self._target_ds, k, v):
            rk = getattr(self._codec, "render_kwargs", None)
            if not isinstance(rk, dict):
                rk = {}
                self._codec.render_kwargs = rk
            rk[k] = v
        super().__setitem__(k, v)
        self._target_ds.invalidate_up(max_depth=6)
        request_render()


class _ChildKwargsSource(dict):
    """The child_kwargs row when the parent's class-level @defaults doesn't
    (yet) carry child_kwargs in SOURCE: displays the live dict, and the first
    write lazily creates `child_kwargs={...}` inside the class-level defaults
    PARSE — a non-dunder key, so plain bubbling item-writes wrap the new dict
    and dirty the host (no __overrides__-style special casing needed). The
    save + the parent-class recompile stamp then make it code."""

    def __init__(self, defaults_parse, live):
        super().__init__({k: v for k, v in (live or {}).items()
                          if isinstance(k, str) and not k.startswith("__")})
        self._dp = defaults_parse

    def __setitem__(self, k, v):
        ck = self._dp.get("child_kwargs")
        if not isinstance(ck, dict):
            self._dp["child_kwargs"] = {}  # bubbling: wraps + dirties
            ck = self._dp["child_kwargs"]
        ck[k] = v
        super().__setitem__(k, v)


class _DrawStateAttrSource(dict):
    """The 'draw state' source row: whitelisted params read from the target
    draw_state's own fields (ds.tint — the style cascade's last fallback,
    persisted with window state). The DEFAULT source: SourcePriority ranks it
    last, so it only ever drives when nothing else sets the param. Writes go
    setattr-on-the-ds, in place, plus the same subtree invalidation the
    instance adapter does."""

    def __init__(self, target_ds):
        from meltygui.core.core_render import OBJ_ATTR_PARAMS
        super().__init__({p: getattr(target_ds, p) for p in OBJ_ATTR_PARAMS
                          if getattr(target_ds, p, None) is not None})
        self._target_ds = target_ds
        if "view_func" in target_ds.auto_params:
            dict.__setitem__(self, "view_func", target_ds.auto_params["view_func"])

    def __setitem__(self, k, v):
        if k == "view_func":
            self._target_ds.auto_params[k] = v
        else:
            setattr(self._target_ds, k, v)
        super().__setitem__(k, v)
        self._target_ds.invalidate_up(max_depth=6)
        request_render()


class _LazyOverrideEntry(dict):
    """Stand-in '# [<key>]' source for a site with NO override comment yet.
    Reads as the empty entry dict; the FIRST write (the matrix's + button)
    materializes __overrides__['__<key>__'] in the owning parse and the save
    synthesizes the comment line (_patch_leading_override's creation branch).
    On the next walk the real entry exists and registers instead.

    Bubbling treats `__…` keys as internal bookkeeping — writes to them are
    stored RAW (no wrap, no dirty mark), which is exactly right for parse
    bookkeeping and exactly wrong here: naive creation leaves plain dicts the
    host never hears about (tint shows — the marker reads the same tree — but
    nothing saves). So the new structure is installed onto the host's bubble
    root explicitly, and the LEAF write goes through the wrapped entry, whose
    non-internal key fires the standard notify → dirty → save path."""

    def __init__(self, root, entry_key=None):
        # entry_key=None targets the root node's OWN __overrides__ - a
        # leading comment above a class/function def - instead of a
        # '__<key>__' field slot on its parent.
        super().__init__()
        self._root = root
        self._entry_key = entry_key

    def __setitem__(self, k, v):
        from meltygui.core.conversion.bubbling import install_bubbling
        root_node = self._root
        ovs = root_node.get("__overrides__")
        if not isinstance(ovs, dict):
            ovs = {}
        if self._entry_key is not None and not isinstance(ovs.get(self._entry_key), dict):
            ovs[self._entry_key] = {}
        broot = getattr(root_node, "_bubble_root", None)
        if broot is not None:
            # Plain dicts can't reclass - install returns the wrapped copy,
            # so store THAT (raw store: internal key). Idempotent if ovs
            # already bubbles.
            ovs = install_bubbling(ovs, broot)
        root_node["__overrides__"] = ovs
        entry = ovs if self._entry_key is None else ovs[self._entry_key]
        entry[k] = v  # non-internal key on the wrapped entry → notify → dirty
        super().__setitem__(k, v)  # same-frame reads (row refresh) see it too


def collect_input_sources(input_value, cm_state, class_to_show=None):
    """Every editable input source behind a view, collected WITHOUT drawing —
    the shared engine of draw_input_tab and set_anywhere. `input_value` is the
    TARGET draw_state; `cm_state` caches the code hosts across calls (pass the
    context menu's, or any per-target instance). Returns a dict:
      sources    {source_name: parse dict}  (placeholder {} when unparsed)
      tints      {source_name: codec tint}
      locations  {source_name: (file, line)} for jump buttons
      kinds      {source_name: kind caption} ("signature" / "caller +N" / ...)
      writable   source names whose dict is a REAL parse node (writes save)
      comment_owners {source_name: draw_state} separates inherited parameter
                     sources from the target window’s own lifecycle overrides
    """
    # Per-frame memo: the input tab plus the tint tab's get_sources_for /
    # from_anywhere / set_anywhere all this for the same target within one
    # frame, and the parses can't change mid-frame - build once per
    # (frame, class_to_show) per cm_state. Profiled at avg 2.5ms / max 12.7ms
    # per build; tripling it per frame was the input-tab drag fps drop.
    _memo_key = (Core.melty.frame_count, class_to_show)
    if getattr(cm_state, "_collect_key", None) == _memo_key:
        return cm_state._collect_cache

    # Source/cst hosts come from the process-wide code-host cache, keyed by the
    # live reference - every menu opened on the same render_func/class/call
    # site shares ONE host pair, so the code isn't re-loaded and re-parsed per
    # open (code_hosts_for in new_converters; earlier like this tab used to
    # build inline).
    host_key = (input_value._view_func, class_to_show)
    if cm_state.host_key != host_key:
        cm_state.host_key = host_key
        cm_state.render_func_str, cm_state.render_func_dict = code_hosts_for(input_value._view_func)
        cm_state.class_str, cm_state.class_dict = code_hosts_for(class_to_show)
        # The nav retargeted us at a different view; its call sites differ (and
        # may not be captured yet — see the lazy capture in draw_context_menu).
        cm_state.call_site_hosts = []
        cm_state.call_site_keys = None
        # The class's source location feeds the class-var/class-default jump
        # buttons. inspect.getsourcelines() on a CLASS AST-parses the ENTIRE
        # module file (CPython 3.9+ _ClassFinder) - ~34ms on a 7k-line file like
        # libcst_conversion.py (IntParse, the parent of a parsed int field) -
        # so it CANNOT run per frame. class_to_show is part of host_key, so the
        # location only changes on a rebuild: read it once, here.
        cm_state.class_loc = None
        if isinstance(class_to_show, type):
            try:
                cm_state.class_loc = (inspect.getsourcefile(class_to_show),
                                      inspect.getsourcelines(class_to_show)[1])
            except (TypeError, OSError):
                cm_state.class_loc = None

    decoration_func = input_value._kwargs.get("_view_func_origin", input_value._view_func)
    if getattr(cm_state, "decoration_key", None) is not decoration_func:
        cm_state.decoration_key = decoration_func
        cm_state.decoration_str, cm_state.decoration_dict = code_hosts_for(decoration_func)

    # The caller CHAIN - the direct caller, its caller, ... up to
    # Toggles.caller_walk_steps real (non-dispatch) frames, innermost-first.
    # Read off the cached _call_stack (never the live stack: a drag re-renders
    # with parents skipped, which would wipe every host). The stack can lag a
    # frame or two AFTER retargeting (lazy one-shot capture on the wrapper's
    # next render), so recompute every pass and rebuild the hosts only when the
    # resolved sites change.
    from meltygui.code.chain_converters import caller_chain
    walk_steps = max(1, int(Toggles.caller_walk_steps or 1))
    caller_frames = caller_chain(getattr(input_value, "_call_stack", None))[:walk_steps]
    caller_site_keys = tuple((f, ln) for f, ln, _ in caller_frames)
    # getattr: a cm_state created before this field existed (hotswap of an
    # already-open inputs tab) lacks caller_site_keys - treat as "needs rebuild".
    if caller_site_keys != getattr(cm_state, "call_site_keys", None):
        cm_state.call_site_keys = caller_site_keys
        cm_state.call_site_hosts = [code_hosts_for(CallSite(f, ln))
                                    for f, ln in caller_site_keys]

    # The ACTIVE mode driving this view - the wrapper stamps it into the
    # view's kwargs when a mode config matches (kwargs['current_mode'],
    # core_render; 'mode' for the recursive variant), so it rides on the
    # target's _kwargs. The host is for the mode's ENUM CLASS (whose source
    # holds every member), keyed per class so retargeting at a view under a
    # different mode enum rebuilds; which member to show is re-read each pass.
    current_mode = input_value._kwargs.get('current_mode')
    mode_cls = current_mode.__class__ if current_mode is not None else None
    if cm_state.mode_key != mode_cls:
        cm_state.mode_key = mode_cls
        cm_state.mode_str, cm_state.mode_dict = (
            code_hosts_for(mode_cls) if mode_cls is not None else (None, None))

    # ── Every input source, one parameter at a time ──────────────────────────
    # Collect each parsed source dict, match against the render function's
    # parameter list via param_source_matrix, and hand the matrix to
    # draw_param_matrix: a per-parameter SCREEN (dropdown within params)
    # listing every possible source - param default, caller, mode, class
    # default, function decoration - set or not. Sources register here even
    # when empty/unparsed (placeholder {}), so each screen always shows the
    # full source list; registration order is the screens' row order.
    # Rebuilt every frame from the live parses (cheap dict copy), so a
    # background parse landing or an edit in any source shows up immediately.
    # Edits to a cell write back into the SOURCE dict they came from
    # (apply_param_source_changes) - those dicts are the hosts'
    # bubbling-wrapped parse nodes, so the edit marks the owning host dirty
    # and rides its normal chain_out/save path. Placeholder rows are plain
    # empty dicts: they never grow cells, so no write-back can land in them.
    # Each source maps to the CODEC that owns its data - the codec's
    # render_kwargs tint IS the source color. The matrix cells are parse
    # fragments that can't adopt it naturally (type-based codec fragments), so
    # the tint map rides into draw_param_matrix, which applies it manually
    # and prominently per row.
    from meltygui.code.new_codecs import FunctionCodec
    from meltygui.code.new_codecs import CallerCodec
    from meltygui.code.new_codecs import DecorationsCodec
    from meltygui.code.new_codecs import TypeCodec
    from meltygui.code.new_codecs import ModeCodec

    def _codec_tint(codec):
        return (getattr(codec, "render_kwargs", None) or {}).get("tint")

    sources = {}
    source_tints = {}
    source_locations = {}
    source_kinds = {}
    comment_owners = {}
    writable_sources = []

    def _add_source(sname, sdict, codec, location=None, kind=None, owner=None):
        # Register even when the source sets nothing or hasn't parsed yet -
        # the per-param screen draws EVERY source row, absent ones as "not
        # set". The placeholder is a fresh empty dict, never written to;
        # only REAL parse dicts (even empty ones) are writable, so the
        # matrix's +/× buttons know where a stamped cell can legally land.
        # `kind` is the row's caption (signature / caller / mode / ...);
        # sname stays the concrete spelling (def draw_x / Mode.WINDOW / ...).
        if isinstance(sdict, dict):
            writable_sources.append(sname)
        sources[sname] = sdict if isinstance(sdict, dict) else {}
        source_tints[sname] = _codec_tint(codec)
        if kind:
            source_kinds[sname] = kind
        if kind == "code comment":
            comment_owners[sname] = input_value if owner is None else owner
        if location is not None and location[0] is not None:
            source_locations[sname] = location

    # Source names are the concrete spelling shown on the row button
    # (def draw_x / Mode.WINDOW / @render_func(draw_x), ...); the generic
    # origin goes into `kind`, shown as a caption above the button. Locations
    # feed the jump-to buttons.
    view_fn = inspect.unwrap(input_value._view_func)
    fn_name = getattr(view_fn, "__name__", "?")
    try:
        fn_file = inspect.getsourcefile(view_fn)
    except TypeError:
        fn_file = None
    fn_loc = (fn_file, getattr(getattr(view_fn, "__code__", None),
                               "co_firstlineno", None))

    # cls_name is cheap (__name__); cls_loc is cached on host key change above
    # (inspect.getsourcelines AST-parses the whole module file - never per frame).
    cls_name = class_to_show.__name__ if isinstance(class_to_show, type) else "None"
    # getattr: a cm_state created before this field existed (hotswap of an
    # already-open inputs tab) lacks the field - None just degrades the jump
    # location until the menu retargets and the host_key block recomputes it.
    cls_loc = getattr(cm_state, "class_loc", None)

    # One caller source per real frame walked (caller, caller's caller, ...),
    # innermost-first; each its own editable CallSite host parsed above. The
    # captions read "caller", "caller +1", ...; the row button shows the
    # concrete frame name (disambiguated with a ^N suffix when two frames share a
    # name, since source names are dict keys). If the stack hasn't been
    # captured yet (no real callers), still register one empty "caller"
    # row so the source-list shape stays consistent.
    caller_rows = []  # (sname, sdict, location, kind)
    _seen_caller_names = set()
    for i, ((_str_host, dict_host), (filename, lineno, func_name)) in enumerate(
            zip(cm_state.call_site_hosts, caller_frames)):
        cdict = dict_host.deep.unwrap() if dict_host else None
        cname = func_name or "caller"
        if cname in _seen_caller_names:
            cname = f"{cname} ^{i}"
        _seen_caller_names.add(cname)
        kind = "caller" if i == 0 else f"caller +{i}"
        caller_rows.append((cname, cdict, (filename, lineno), kind))
    if not caller_rows:
        caller_rows.append(("caller", None, None, "caller"))

    # MODE - the active mode is entry in its enum class source (e.g.
    # `NEW_CODE = {types...: ModeOverrides(kwargs={...})}` in mode.py), not as
    # the kwargs dict that entry stamps into this view. A mode can hold
    # SEVERAL type-keyed entries (TEXT_ONLY); the parse can't be type-matched
    # (its keys are unevaluated source), so pick the candidate whose keys best
    # overlap the LIVE matched config (get_config_for) - the entry that
    # actually drove THIS view. Edits merge into the mode_dict host's parse
    # and ride its normal chain_out/save back into the enum's source file.
    mode_label = str(current_mode) if current_mode is not None else "None"
    mode_kwargs, mode_loc = None, None
    if current_mode is not None and cm_state.mode_dict is not None:
        candidates = [c for c in
                      cm_state.mode_dict.deep[current_mode.name].kwargs.all()
                      if isinstance(c, dict)]
        live_keys = set()
        if isinstance(getattr(current_mode, "value", None), dict):
            live_cfg = current_mode.get_config_for(input_value._raw_input_value)
            if live_cfg is not None and live_cfg.kwargs:
                live_keys = set(live_cfg.kwargs)
        mode_kwargs = max(candidates,
                          key=lambda c: len(live_keys & set(c)), default=None)
        # inspect.getsourcelines on a class AST-parses its WHOLE module (the
        # class_loc lesson) - cache the member's location per (class, member),
        # never recompute per frame.
        _ml_key = (mode_cls, current_mode.name)
        if getattr(cm_state, "_mode_loc_key", None) != _ml_key:
            cm_state._mode_loc_key = _ml_key
            cm_state._mode_loc = None
            try:
                cls_lines, cls_start = inspect.getsourcelines(mode_cls)
                member_off = next(
                    (i for i, l in enumerate(cls_lines)
                     if l.lstrip().startswith((f"{current_mode.name} =",
                                               f"{current_mode.name}="))), 0)
                cm_state._mode_loc = (inspect.getsourcefile(mode_cls),
                                      cls_start + member_off)
            except (TypeError, OSError):
                pass
        mode_loc = cm_state._mode_loc

    # Registration order IS the per-param screen's row order: param default
    # (signature), caller, mode, class var, class default, function decoration.
    _add_source(f"def {fn_name}", cm_state.render_func_dict.deep.parameters(), FunctionCodec,
                location=fn_loc, kind="signature")
    for cname, cdict, cloc, ckind in caller_rows:
        from meltygui.core.input.view_selection import CallerViewSource
        if isinstance(cdict, dict):
            cdict = CallerViewSource(cdict, cloc[0] if cloc else None, allow_direct=ckind == "caller")
        _add_source(cname, cdict, CallerCodec, location=cloc, kind=ckind)
    _add_source(mode_label, mode_kwargs, ModeCodec, location=mode_loc, kind="mode")
    # CLASS VAR - the data class's body assignments (`tint = (...)` on the
    # class itself). The class parse IS that dict (fields + __cst__/comment
    # bookkeeping, filtered out by param_source_matrix), so a + here writes a
    # brand-new class var assignment and × removes one, via the same
    # bubbling/save path as every other source.
    _add_source(f"class {cls_name}",
                cm_state.class_dict.deep.unwrap() if cm_state.class_dict else None,
                TypeCodec, location=cls_loc, kind="class var")
    _add_source(f"@defaults({cls_name})", cm_state.class_dict.deep.decorators.defaults(),
                TypeCodec, location=cls_loc, kind="class default")
    # INSTANCE ATTR - the value instance's own whitelisted params (Lamp.tint):
    # what core_render's OBJ_ATTR_PARAMS injection reads. Skip-when-absent
    # like the other value-side rows; dicts/classes carry their config in
    # __overrides__ / class rows above.
    _raw_obj = getattr(input_value, "_raw_input_value", None)
    if _raw_obj is not None and not isinstance(_raw_obj, (dict, list, type)):
        _ia = _InstanceAttrSource(_raw_obj, target_ds=input_value)
        if _ia:
            _add_source(f"{type(_raw_obj).__name__} instance", _ia,
                        TypeCodec, kind="instance attr")
    # CHILD KWARGS - a parent view can drive this view's params via
    # child_kwargs={...} (draw_collection merges it into every child call).
    # The dict itself can be attached to ANY of the PARENT'S own sources (its
    # caller, decorator, comment, instance attr...), so resolve it with the
    # registry machinery ON THE PARENT: from_anywhere("child_kwargs",
    # parent). Cheap gatekeeping: only parents whose live _kwargs actually
    # carry a child_kwargs dict pay the (per-frame-memoized) parent
    # check; the root's self-parent loop is excluded. Recursion up the
    # ancestry terminates the same way: it only walks through parents that
    # themselves receive child_kwargs.
    _pds_ck = getattr(input_value, "_parent", None)
    if _pds_ck is not None and _pds_ck is not input_value:
        _my_key = (input_value._kwargs or {}).get("key")
        _ck_live = (_pds_ck._kwargs or {}).get("child_kwargs")
        _ck_live = _ck_live if isinstance(_ck_live, dict) and _ck_live else None
        _mode_src = None  # the parent's mode source name, set below
        if _ck_live is not None or isinstance(_my_key, str):
            # Ensure the PARENT's registry exists (fresh sessions have no
            # _sa_cm_state until something collects it) - memoized per frame,
            # and this path only runs when a menu/tab is open on a child.
            _psrcs = _sources_for(_pds_ck)
            _pcm = getattr(_pds_ck, "_sa_cm_state", None)
            _pdeco = (_pcm.class_dict.deep.decorators()
                      if _pcm is not None and _pcm.class_dict is not None else None)
            _pcls = (_pcm.host_key or (None, None))[1] if _pcm is not None else None
            _pcls_name = getattr(_pcls, "__name__", "?")
            _cls_defaults = None  # first non-attr @defaults parse entry
            if isinstance(_pdeco, dict):
                for _dk, _dv in _pdeco.items():
                    if not (isinstance(_dk, str) and _dk.split("#", 1)[0] == "defaults"
                            and isinstance(_dv, dict)):
                        continue
                    _dattr = _dv.get("attr") or _dv.get("attrib")
                    if _dattr is None:
                        if _cls_defaults is None:
                            _cls_defaults = _dv
                    elif str(_dattr).strip("'\"") == _my_key:
                        # ATTR-TARGETED @defaults(attr="<this field>", ...):
                        # its own source row on the field's view.
                        _add_source(f"@defaults({_pcls_name}.{_my_key})", _dv,
                                    TypeCodec, kind="attr default")
            # CHILD KWARGS (MODE) - the parent's MODE entry can carry its own
            # child_kwargs={...} (Modes.NEW_CODE stamps column_widths on every
            # child). It reaches this view through the same merge as any other
            # child_kwargs, but it lives in the mode ENUM's source, so it gets
            # its own row: an edit here writes mode.py, not the parent's
            # caller/@defaults. Absent a lazy-create adapter, but the first
            # write stamps `child_kwargs={...}` onto the mode entry's kwargs
            # parse (bubbling wraps + dirties; the mode host's code persists).
            _mode_src = next((s for s, k in _psrcs["kinds"].items()
                              if k == "mode"), None)
            _mode_parse = _psrcs["sources"].get(_mode_src) if _mode_src else None
            if isinstance(_mode_parse, dict) and _mode_src in set(_psrcs["writable"]):
                _mck = _mode_parse.get("child_kwargs")
                if not isinstance(_mck, dict):
                    _mck = _ChildKwargsSource(_mode_parse, None)
                _add_source("child_kwargs (mode)", _mck, ModeCodec,
                            location=_psrcs["locations"].get(_mode_src),
                            kind="mode child kwargs")
        if _ck_live is not None:
            # CHILD KWARGS — the dict driving this view from the parent.
            # Prefer the CODE-backed setter (the first source DRIVING
            # child_kwargs - from_anywhere's pick, inlined so the same target
            # also yields the row's jump location: the caller line /
            # @defaults class line the dict lives at). If no source sets it
            # yet, a lazy-create adapter over the class-level @defaults dict
            # makes the first write EDIT CODE (the live dict alone silently
            # kept writes in memory).
            # The MODE setter has its own row above (it outranks any other
            # parent sources, so it would otherwise always BE this row and the
            # non-mode setters would never be visible/editable): resolve this
            # row from the parent's sources MINUS mode.
            _psrcs_nm = _psrcs
            if _mode_src is not None:
                _psrcs_nm = dict(_psrcs, sources={k: v for k, v in _psrcs["sources"].items()
                                                  if k != _mode_src})
            _ck_target = _driving_source(_psrcs_nm, "child_kwargs")
            _ck_dict = (_psrcs_nm["sources"][_ck_target].get("child_kwargs")
                        if _ck_target is not None else None)
            _ck_loc = (_psrcs["locations"].get(_ck_target)
                       if _ck_target is not None else None)
            if not (isinstance(_ck_dict, dict) and _ck_dict):
                _ck_dict = (_ChildKwargsSource(_cls_defaults, _ck_live)
                            if isinstance(_cls_defaults, dict) else _ck_live)
                _ck_loc = getattr(_pcm, "class_loc", None) if _pcm is not None else None
            _add_source("child_kwargs", _ck_dict, TypeCodec,
                        location=_ck_loc, kind="child kwargs")
    # CODEC - the active codec's render_kwargs (ds._codec, stashed by the
    # wrapper for every view): the lowest kwargs merge layer and the
    # provenance color (import views' green). Skip-when-absent like the
    # other value-side rows.
    _codec_obj = getattr(input_value, "_codec", None)
    if _codec_obj is not None:
        _cs = _CodecSource(_codec_obj, input_value)
        if _cs:
            _add_source(f"codec {getattr(_codec_obj, '__name__', type(_codec_obj).__name__)}",
                        _cs, TypeCodec, kind="codec")
    # DRAW STATE - the ds's own kwargs (ds.tint): the lowest source, ranked
    # last, so windows tinted only by their persisted draw_state (the "blue
    # window no other claims" case) still resolve to a real, writable row.
    _dsa = _DrawStateAttrSource(input_value)
    if _dsa:
        _add_source("draw_state", _dsa, TypeCodec, kind="draw state")

    # ── Sources parsed off the VALUE itself ──────────────────────────────────
    # The value flowing through the view can be (or sit inside) a parse node
    # of the owning window's's bubbling tree (e.g. a nested ClassParse in
    # the Toggles window). Kwargs carried by that parse are live inputs the
    # class_to_show rows (keyed on the value's runtime TYPE) miss, and since
    # the dicts are bubbling trees, a cell edit marks the host dirty and saves
    # through its normal path - no extra save need. Walk a few parents so a
    # primitive leaf (scroll_speed) still finds its owning class parse, same
    # as the class_to_show walk. Two such sources per parse:
    #   @defaults(<name>)  the parse's decorators.defaults dict - the wrapper
    #                      reads it directly (core_render's
    #                      input_value["decorators"] tint path)
    #   # [<name>]         the '# [tint=(...)]' override comment on the
    #                      value's source line: a leaf child carries it in
    #                      __overrides__; a primitive field's comment lives on
    #                      the PARENT parse under __<key>__ (fed to the
    #                      child's kwargs by draw_collection). Saves update
    #                      the comment via _patch_leading_override /
    #                      _patch_field_overrides.
    # Both skip when absent (same rule as @window below) - a placeholder row
    # on every parsed value would be permanent noise.
    def _root_file_loc(ds, span):
        # Map a span (relative to the root host parse's source) to a file
        # location: nested parses carry no file_path, so find the ancestor
        # parse that has file_path + line_offset. The window ds holds it as
        # _input_value (post-convert), so check both. None → no jump location.
        while span is not None and ds is not None:
            for _r in (getattr(ds, "_raw_input_value", None),
                       getattr(ds, "_input_value", None)):
                if isinstance(_r, GeneralParse) and getattr(_r, "file_path", None):
                    return (str(_r.file_path), _r.line_offset + span.start_line)
            if ds._parent is ds:
                break
            ds = ds._parent
        return None

    # Explicit collection/key threading is always a comment source, even when
    # no ancestor view renders that value (standalone draw_text calls).
    slot_collection = input_value._kwargs.get("collection")
    slot_key = input_value._kwargs.get("key")
    if isinstance(slot_collection, dict) and isinstance(slot_key, str):
        overrides = slot_collection.get("__overrides__", {})
        entry = overrides.get(f"__{slot_key}__") if isinstance(overrides, dict) else None
        if isinstance(entry, dict) or isinstance(slot_collection, GeneralParse):
            if not isinstance(entry, dict):
                entry = _LazyOverrideEntry(slot_collection, f"__{slot_key}__")
            _add_source(f"# [{slot_key}]", entry, TypeCodec, kind="code comment")
    own_value = input_value._raw_input_value
    if isinstance(own_value, dict) and not isinstance(own_value, GeneralParse):
        overrides = own_value.get("__overrides__")
        if isinstance(overrides, dict):
            _add_source("# [value]", overrides, TypeCodec, kind="code comment")

    _pds, _pwalk = input_value, 4
    while _pds is not None and _pwalk >= 0:
        _praw = getattr(_pds, "_raw_input_value", None)
        if isinstance(_praw, GeneralParse):
            _pdecos = _praw.get("decorators")
            _pdefaults = _pdecos.get("defaults") if isinstance(_pdecos, dict) else None
            _pname = (getattr(_praw, "def_name", None)
                      or getattr(getattr(_praw.get("__cst__"), "name", None),
                                 "value", None))
            # The name-carry guard covers viewing ClassParse's own parse -
            # the class_to_show row already shows it there.
            if (isinstance(_pdefaults, dict) and _pdefaults and _pname
                    and f"@defaults({_pname})" not in sources):
                _add_source(f"@defaults({_pname})", _pdefaults, TypeCodec,
                            location=_root_file_loc(_pds, getattr(_praw, "span", None)),
                            kind="code class default")
            _povs = _praw.get("__overrides__")
            if _pds is input_value:
                # The view targets the parse itself - its own leading comment.
                _ov_dict, _ov_name = _povs, _pname
                _ov_span = getattr(_praw, "span", None)
            else:
                # Primitive leaf - the parent parse holds its comment under
                # __<key>__; the leaf's parent key rides in its kwargs (the
                # 'key' entry draw_collection passes every child).
                _tkey = (input_value._kwargs or {}).get("key")
                _ov_dict = (_povs.get(f"__{_tkey}__")
                            if isinstance(_povs, dict) and isinstance(_tkey, str)
                            else None)
                _ov_name = _tkey
                # _child_spans maps field keys to their assignment's span
                # (_record_child) - jump lands on the field's line, the
                # comment's one above.
                _ov_span = (getattr(_praw, "_child_spans", None) or {}).get(_tkey)
            if _ov_name:
                if not (isinstance(_ov_dict, dict) and _ov_dict):
                    # No comment yet - lazy row, same as the live-view path
                    # below: the matrix's + materializes the entry, the
                    # wrapper synthesizes the `# [...]` line. For the parse
                    # itself (a class/function node) the entry is its OWN
                    # __overrides__ (leading comment on the def); for a
                    # leaf it's the parent's __<key>__ slot. Module parses
                    # have no _pname, so they register nothing - no noise.
                    _ov_dict = (_LazyOverrideEntry(_praw)
                                if _pds is input_value
                                else _LazyOverrideEntry(_praw, f"__{_ov_name}__"))
                _add_source(f"# [{_ov_name}]", _ov_dict, TypeCodec,
                            location=_root_file_loc(_pds, _ov_span),
                            kind="code comment")
            break
        # LIVE-VIEW windows/markers: the value flowing through them is a
        # runtime capture, not a parse node - but the marker stamps the owning
        # scope's parse dict on the draw_state (live_root/live_key, the same
        # data its comment-args splat reads), so the site's `# [<key>]`
        # comment registers as an input source exactly like a primitive
        # leaf's. Edits write to the editor host's bubbling tree and save
        # through its normal path.
        _lroot = getattr(_pds, "live_root", None)
        if isinstance(_lroot, dict):
            # Resolve against the editor's CURRENT root: the stamp is the
            # marker's last render, and a reparse since (the window's def
            # scrolled off the screen, say) orphaned it - a write into the
            # orphan shows in the replay but never reaches the save.
            from meltygui.editor.live_view_views import current_live_root
            _lroot = current_live_root(_pds)
            _lkey = getattr(_pds, "live_key", None)
            if isinstance(_lkey, str):
                _lovs = _lroot.get("__overrides__")
                _lov = (_lovs.get(f"__{_lkey}__")
                        if isinstance(_lovs, dict) else None)
                if not isinstance(_lov, dict):
                    # No override yet - register a lazy entry so the matrix's
                    # + can create `# [tint=(...)]` the same way it stamps a
                    # missing @defaults(row).
                    _lov = _LazyOverrideEntry(_lroot, f"__{_lkey}__")
                _lspan = (getattr(_lroot, "_child_spans", None) or {}).get(_lkey)
                _add_source(f"# [{_lkey}]", _lov, TypeCodec,
                            location=_root_file_loc(_pds, _lspan),
                            kind="code comment", owner=_pds)
            break
        if _pds._parent is _pds:
            break
        _pds = _pds._parent
        _pwalk -= 1
    # @window on the class (e.g. `@window(tint=(0.11,0.12,0.14))` on Toggles) -
    # its kwargs drive the window rendering the value, so it's an input source.
    # Same skip-when-absent rule as the fn-side @window below.
    _cls_window_deco = (cm_state.class_dict.deep.decorators.window()
                        if cm_state.class_dict else None)
    if isinstance(_cls_window_deco, dict) and _cls_window_deco:
        _add_source(f"@window({cls_name})", _cls_window_deco,
                    DecorationsCodec, location=cls_loc, kind="class decoration")
    # @render_func(...) - the view func's decorator kwargs. Registered for
    # EVERY value (not just function values, the old gate): decorator kwargs
    # outrank signature defaults in the wrapper gauntlet, so a param set
    # there (fast_toggle's show_cache=False) is the TRUE driver and hiding
    # the row made provenance lie. The app-wide blast radius of an edit is
    # real but the row is only ever written by an explicit pick.
    decoration_name = decoration_func.__name__
    decoration_raw = inspect.unwrap(decoration_func)
    decoration_loc = (inspect.getsourcefile(decoration_raw), decoration_raw.__code__.co_firstlineno)
    _add_source(f"@render_func({decoration_name})",
                cm_state.decoration_dict.deep.decorators.render_func(),
                DecorationsCodec, location=decoration_loc, kind="decoration")
    # @window only exists as a source on actually-@window-decorated funcs -
    # a placeholder row here would be permanent noise on every other tab.
    from meltygui.core.input.view_selection import WindowViewSource
    _window_deco = cm_state.decoration_dict.deep.decorators.window()
    if isinstance(_window_deco, dict) and _window_deco:
        _add_source(f"@window({decoration_name})",
                    WindowViewSource(_window_deco, decoration_func, decoration_loc[0]),
                    DecorationsCodec, location=decoration_loc, kind="window decoration")
    # @glfw_window(...) on the view fn (`@glfw_window` over `@render_func`,
    # app.py): every kwarg past the OS window's own (title / size / window_id /
    # name) is the root VIEW's, handed to the view by app._draw_root - the OS
    # window's twin of @window, and it ranks with it. Same skip-when-absent
    # rule; a missing `@glfw_window` parses to a str and adds nothing.
    _glfw_deco = cm_state.decoration_dict.deep.decorators.glfw_window()
    if isinstance(_glfw_deco, dict) and _glfw_deco:
        _add_source(f"@glfw_window({decoration_name})",
                    WindowViewSource(_glfw_deco, decoration_func, decoration_loc[0]),
                    DecorationsCodec, location=decoration_loc, kind="glfw window decoration")

    cm_state._collect_cache = {
        "sources": sources, "tints": source_tints,
        "locations": source_locations, "kinds": source_kinds,
        "comment_owners": comment_owners,
        "writable": tuple(writable_sources), "view_func": input_value._wrapper or input_value._view_func}
    cm_state._collect_key = _memo_key
    return cm_state._collect_cache


def _ancestor_call_line(target_ds, ancestor_ds):
    """File-absolute line inside `ancestor_ds`'s view function whose statement
    (transitively) rendered `target_ds`'s element — e.g. inspecting a button
    drawn by draw_tab_bar and walking up one scope resolves the `button(...)`
    call line in draw_tab_bar. The func tab auto-selects it.

    Scans the TARGET's cached _call_stack (innermost-first tuples, captured
    once on menu-open — never the live stack, see the capture note in
    core_render) for the nearest frame executing the ancestor's function; that
    frame's lineno is the call statement. A view rendered inside a deferred
    layer has a stack that bottoms out at the layer loop, so each deferred
    ancestor's queue-time _deferred_call_stack is appended to continue the
    chain outward. None when the ancestor's frame isn't in the chain (stack
    not captured yet, or the ancestor rendered from cache with parents
    skipped)."""
    view_func = getattr(ancestor_ds, "_view_func", None)
    if view_func is None:
        return None
    code = getattr(inspect.unwrap(view_func), "__code__", None)
    if code is None:
        return None
    stack = list(getattr(target_ds, "_call_stack", None) or ())
    node = target_ds
    for _ in range(64):
        if getattr(node, "_is_deferred_layer", False):
            stack.extend(getattr(node, "_deferred_call_stack", None) or ())
        parent = getattr(node, "_parent", None)
        if node is ancestor_ds or parent is None or parent is node:
            break
        node = parent
    for filename, lineno, func_name in stack:
        if func_name == code.co_name and filename == code.co_filename:
            return lineno
    return None


def _deferred_ancestors(target_ds):
    """The draw_states on `target_ds`'s parent chain (itself included,
    nearest first) that were queued to a deferred layer at some point
    (_is_deferred_layer) — the ones whose queue-time stack a descendant's
    trace needs."""
    found = []
    node = target_ds
    for _ in range(64):
        if getattr(node, "_is_deferred_layer", False):
            found.append(node)
        parent = getattr(node, "_parent", None)
        if parent is None or parent is node:
            break
        node = parent
    return found


def _request_deferred_stacks(target_ds):
    """Ask every deferred ancestor of `target_ds` that has no queue-time
    stack yet to capture one on its next inline (queue) pass — lazy and
    one-shot, the same shape as the up-arrow's _call_site_requested. The
    ancestor's PARENT has to re-run its body for the queue branch to fire,
    hence the invalidate_up."""
    for node in _deferred_ancestors(target_ds):
        if (node._deferred_call_stack_frames is None
                and not node._deferred_stack_requested):
            node._deferred_stack_requested = True
            if Core.melty.cache is not None:
                Core.melty.cache.invalidate_up(node._tile_id, max_depth=5)
            request_render()


def _splice_deferred_stack(inline_frames, queued_frames, view_func):
    """Join a dispatch-bottomed stack onto the chain that queued its layer.

    `inline_frames` (outermost first) were captured while the deferred
    layer was being drawn from Melty.end_frame's layer loop, so their head
    is the frame loop → Melty.draw → wrapper; `queued_frames` were captured
    at queue time inside that same wrapper, so their tail is the caller
    chain → wrapper. The result keeps the queued head up to the wrapper and
    the inline tail from the wrapper on: the trace of a direct call.
    Returns `inline_frames` unchanged when the layer loop isn't in it (the
    view rendered inline this time) or when the first view body after the
    dispatch isn't `view_func` (a stale deferred mark on another ancestor)."""
    from meltygui.code.chain_converters import _is_dispatch_frame
    def _base(path):
        return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    loop = None
    for index, frame in enumerate(inline_frames):
        if _base(frame[0]) == "meltygui.py" and frame[2] == "end_frame":
            loop = index
    if loop is None:
        return inline_frames
    wrapper = None
    for index in range(loop + 1, len(inline_frames)):
        if _base(inline_frames[index][0]) == "core_render.py":
            wrapper = index
            break
    if wrapper is None:
        return inline_frames
    code = getattr(inspect.unwrap(view_func), "__code__", None) \
        if view_func is not None else None
    if code is not None:
        body = next((f for f in inline_frames[wrapper:]
                     if not _is_dispatch_frame(f[0], f[2])), None)
        if body is None or body[0] != code.co_filename \
                or body[2] != code.co_name:
            return inline_frames
    head = list(queued_frames)
    while head and _base(head[-1][0]) == "core_render.py":
        head.pop()
    return head + list(inline_frames[wrapper:])


def _merged_call_stack_frames(target_ds, menu_state):
    """The Code tab's stack: the target's menu-open capture with every
    deferred ancestor's queue-time stack spliced in, nearest layer first,
    so nested deferred windows chain outward to the real caller. Stops at
    the first ancestor whose queue-time stack hasn't landed yet (a partial
    splice past it would join the wrong layer). Memoized on the identities
    of the parts — draw_stack_trace rebuilds on the list's identity."""
    inline = getattr(target_ds, "_call_stack_frames", None)
    if not inline:
        return None
    ancestors = _deferred_ancestors(target_ds)
    key = (id(inline),) + tuple(
        id(node._deferred_call_stack_frames) for node in ancestors)
    if getattr(menu_state, "_merged_stack_key", None) == key:
        return menu_state._merged_stack
    merged = inline
    for node in ancestors:
        queued = node._deferred_call_stack_frames
        if not queued:
            break
        merged = _splice_deferred_stack(
            merged, queued, getattr(node, "_view_func", None))
    menu_state._merged_stack_key = key
    menu_state._merged_stack = merged
    return merged


# Reported by draw_context_menu_items when its Inspect row was picked: the
# wrapper (core_render's context-menu block) opens the inspector,
# draw_context_menu, in the menu's place.
INSPECT = object()


from meltygui.core.layout.dropdown_core import _ds_in_subtree


from meltygui.core.layout.dropdown_core import _dd_set_cursor


from meltygui.core.layout.dropdown_core import _dd_invalidate_rows


from meltygui.core.layout.dropdown_core import _dd_scroll_cursor_into_view


from meltygui.core.layout.dropdown_core import _dd_pick


from meltygui.core.layout.dropdown_core import _dd_close


from meltygui.core.layout.dropdown_core import _dd_handle_keys


_DD_MENU_W = Toggles.Dropdown.menu_width
_DD_ROW_H = Toggles.Dropdown.row_height
# Root popover size bounds (draw_dropdown's content fit + the resize handle):
# the wrapper's min_width / min_height for dd_menu and the old popup_size.
_DD_MENU_MIN_W = Toggles.Dropdown.min_width
_DD_MENU_MIN_H = Toggles.Dropdown.min_height
_DD_MENU_MAX_H = Toggles.Dropdown.max_height


from meltygui.core.layout.dropdown_core import _dd_update_menu_size


# Limits on the scope-label column of code-preview rows (usage-jump picker):
# the main code column clamps here, and longer labels ellipsize, so the
# code keeps most of the row's width.
_DD_CODE_LBL_MAX_W = Toggles.Dropdown.code_label_max_width


# Row tags (the right-aligned dim column): default colour - the
# autocomplete kind label's blue washed - and the gap between segments.
_DD_TAG_COLOR = Toggles.Dropdown.tag_color
_DD_TAG_GAP = Toggles.Dropdown.tag_gap
# Per-row symbol tint wash alpha (autocomplete's definition colours) -
# the tag mask re-composes it so the mask stays invisible on tinted rows.
_DD_ROW_TINT_A = Toggles.Dropdown.row_tint_alpha


from meltygui.view.control_view import draw_single


# [tint=(0.75, 0.0, 0.0), show_tint=True]
def draw_any(input_value: any = None, view_func=None, mode: any = None, chain=None, **kwargs):
    import inspect
    kwargs_view_func = view_func
    key = kwargs.get("key", None)
    real_type = kwargs.get("real_type", type(input_value))
    collection_type = kwargs.get("type_collection", type(kwargs.get("collection", None)))

    if view_func is None:
        new_default = Core.melty.get_default_view_function(real_type=real_type, collection_type=collection_type,
                                                           attrib_key=key, value=input_value)
        if new_default is None:
            new_default = fast_draw_collection
        if view_func is None:
            view_func = new_default

    if mode is None:
        mode = Core.melty.mode_stack[-1] if len(Core.melty.mode_stack) > 0 else None

    if isinstance(mode, tuple) and len(mode) > 0:
        main_mode = mode[0]
    else:
        main_mode = mode

    if main_mode is not None:
        # Loop over super types
        mode_config = main_mode.get_config_for(input_value)
        if mode_config is not None and mode_config.func is None and (
                mode_config.kwargs.get("convert", None) is not None
                or mode_config.kwargs.get("convert_in", None) is not None):
            convert_in = mode_config.kwargs.get("convert_in", None)
            if convert_in is not None:
                # Infer target type from the return annotation of the last converter
                import inspect
                last_fn = convert_in[-1]
                ret = inspect.signature(last_fn).return_annotation
                convert_to_type = ret if ret is not inspect.Parameter.empty else None
            else:
                convert_to_type = mode_config.kwargs["convert"][-1]
            mode_config = main_mode.get_config_for(the_type=convert_to_type) if convert_to_type is not None else None
            if mode_config is not None and mode_config.func is not None:
                view_func = draw_single
                kwargs_view_func = mode_config.func

        elif mode_config is not None and mode_config.func is not None:
            if isinstance(mode_config.func, tuple):
                kwargs['chain'] = mode_config.func
                kwargs['route'] = mode_config.route
                view_func = run_chain
            else:
                view_func = mode_config.func
                kwargs_view_func = view_func

    # kwargs['use_cache'] = True
    kwargs['mode'] = mode
    kwargs['view_func'] = kwargs_view_func

    from meltygui.core.input.view_selection import configured_view_func
    try:
        selected = configured_view_func(input_value, kwargs)
    except ValueError as error:
        from meltygui.core.diagnostics.notifications import notify
        notify(str(error), tag="view_func")
        selected = None
    if selected is not None and view_func is not draw_single and view_func is not run_chain:
        view_func = selected
    if "view_func" not in inspect.signature(inspect.unwrap(view_func)).parameters:
        kwargs.pop("view_func", None)
    return_val = view_func(input_value, **kwargs)

    return return_val


from meltygui.editor.source_ui import _RowSpan, _row_code_hosts
