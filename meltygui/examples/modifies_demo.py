import inspect
import types
from pathlib import Path

import meltygui_imgui as imgui

from meltygui.core.modes import Modes
from meltygui.core.render_funcs import RenderFuncs
from meltygui.core.toggles import Toggles
from meltygui.code.new_converters import code_file_io
from meltygui.code.new_converters import convert_in_and_out_value
from meltygui.code.new_converters import string_to_cst_module
from meltygui.code.new_converters import cst_module_to_string
from meltygui.code.new_converters import cst_module_to_dict
from meltygui.code.new_converters import dict_to_cst_module
from meltygui.core.render_host import RenderHost
from meltygui.core.column_core import draw_columns
from meltygui.core.core_render import render_func
from meltygui.core.window_decoration import window
from meltygui.core.render_dispatch import draw_any
from meltygui.core.render_dispatch import draw_collection
from meltygui.editor.text_editor import draw_text
from meltygui.core.invalidation_tracker import Note
from meltygui.core.mode import Mode

class ModifiesPlayground:

    def __init__(self, playground):
        self.playground = playground

    def modify(self, modification):
        self.playground.modify(modification)

class InCode:
    value = 949
    is_tree = False
    name = "Unset"
    show_bg = True
    use_cache = False
    some_val = 0
    some_list = [-152,69,45,-22]
    om_val2  =0


 # [tint=(0.0923043042421341, 0.041103292256593704, 0.23255813121795654)]
    class Nested:
        inner_value = [110,172,28]

# Two chained proxies. Each WRAPS a stateful func (stateful_work → view_func(value) →
# stateful_work) and HOLDS the value that func hands its view_func - so each is "just a
# dict with one value inside", at its own representation level:
#
#   string_proxy   wraps code_file_io              holds {value: <source string>}\
#   dict_proxy     wraps convert_in_and_out_value  holds {value: <GeneralParse dict>}
#
# code_file_io hands its view_func the source string; convert_in_and_out_value hands its
# view_func the chain_in OUTPUT (the new string) directly - so neither one needs a
# routed side-channel or materialize_from. Chaining is data-flow: dict_proxy.input_value
# IS string_proxy, so it reads string_proxy's held source ("just like a dict"); every
# edit to the tree flows back → convert's new source string → written onto string_proxy
# → code_file_io saves one frame later.
#
# Both are standalone, so draw_main calls draw() on each and renders it in its own
# window (string_proxy first, so its source is fresh when dict_proxy reads it).

# string_proxy = RenderHost(io_function=code_file_io, input_value=draw_text, name="String Proxy File",
#
#                           child_kwargs={"auto_load_edits": False, "auto_load":True})   # auto-reload on external file change
#
# dict_proxy = RenderHost(
#     io_function=convert_in_and_out_value, input_value=string_proxy, name="Tree Proxy",
#     child_kwargs={
#         "chain_in": [string_to_cst_module, cst_module_to_dict],
#         "chain_out": [dict_to_cst_module, cst_module_to_string],
#         "route": {cst_module_to_dict: ("code_dict", "jump_to", "run_jedi", "drive")},
#     })

# draw_text_static_file_load = inspect.getsource(draw_text)

# The shared framework pair, NOT a private copy. This module used to carry its
# own copy of code_hosts_for and its own cache, so this window passed draw_text
# through a second, independent pipeline while the editor route parsed the same
# span through the framework pair: two full 102KB str→cst→dict conversions per
# session (~430ms of duplicated background work, visible in the perf-trace
# timeline). One cache, one pair, one parse.
from meltygui.code.new_converters import code_hosts_for

string_proxy, dict_proxy = code_hosts_for(draw_text)


# @window(disable_scroll=False, use_cache=True)
# @render_func(tint=(0.043, 0.11, 0.20), auto_resize=True)
# def test_code_hosts(_, draw_state):
    # Both proxies show themselves in their own windows (draw_main → draw()). This
    # window just inspects them AS ordinary dicts - the framework has no idea they're
    # proxies; it's rendering ordinary dicts whose single value was materialized by
    # their wrappers. Editing here bubbles back exactly the same way.

    #
    # changed, value = draw_collection(string_proxy, name="String Proxy", disable_scroll=False, child_kwargs={"view_func": draw_text, "code_dict": dict_proxy})
    # changed, value = draw_collection(dict_proxy, name="Dict Proxy", disable_scroll=False)

    # draw_columns({"text":string_proxy, "dict":dict_proxy})

    # global draw_text_static_file_load
    # changed, value = draw_text(draw_text_static_file_load, show_name=True, use_cache=True, name="static baseline")
    # if changed:
    #     draw_text_static_file_load = value

    # changed, value = draw_collection(string_proxy, name="String Proxy Dict", child_kwargs={"view_func":draw_text})

    # if changed:
    #     draw_state.invalidate_up_by_obj(obj=dict_proxy, time_delta=2, note=Note(name="String Proxy changed", tint=(1,1,1)), max_depth=10)

        # request_render()
    # New trick: `.deep.unwrap()` skips the redundant wrapper rungs (value / module /
    # function name) and hands draw_collection the meaningful content dict with its keys
    # intact - no manual ["value"][...] indexing or "if key in d" guards.
    # tree = dict_proxy.deep.unwrap()
    # if tree:
    # changed, value = draw_collection(dict_proxy, name="Tree Proxy Dict", column=1, disable_scroll=False)