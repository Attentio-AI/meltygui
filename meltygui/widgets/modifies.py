import imgui

from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.new_converters import (
    code_file_io, convert_in_and_out_value, string_to_cst_module, cst_module_to_string,
    cst_module_to_dict, dict_to_cst_module)
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any, draw_collection
from src.lsd.gl_gui.view.invalidation_tracker import Note
from src.lsd.gl_gui.view.mode import Mode


class ModifiesPlayground:

    def __init__(self, playground):
        self.playground = playground

    def modify(self, modification):
        self.playground.modify(modification)


class InCode:
    value = 2
    is_tree = True
    name = "Unset"
    show_bg = True
    use_cache = True
    some_val = 78888
    some_list = [-106,19,27,1]
    om_val2  =109


 # [tint=(0.7813953, 0.5462583, 0.30892375111579895)]
    class Nested:
        inner_value = [137,152,0]

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

string_proxy = RenderHost(wrapper=code_file_io, input_value=InCode, name="String Proxy",
                          renderer=RenderFuncs.draw_blank)   # held value is source string

dict_proxy = RenderHost(
    wrapper=convert_in_and_out_value, input_value=string_proxy, name="Tree Proxy", renderer=RenderFuncs.draw_blank,
    child_kwargs={
        "chain_in": [string_to_cst_module, cst_module_to_dict],
        "chain_out": [dict_to_cst_module, cst_module_to_string],
        "route": {cst_module_to_dict: ("code_dict", "jump_to", "run_jedi", "drive")},
    })


@window
@render_func()
def draw_modifies_playground(_, draw_state):
    # Both proxies show themselves in their own windows (draw_main → draw()). This
    # window just inspects them AS ordinary dicts - the framework has no idea they're
    # proxies; it's rendering ordinary dicts whose single value was materialized by
    # their wrappers. Editing here bubbles back exactly the same way.
    imgui.text("String Proxy — {value: <source>}")
    changed, value = draw_collection(string_proxy, name="String Proxy Dict", column=0, disable_scroll=False)
    if changed:
        draw_state.invalidate_up_by_obj(obj=dict_proxy, frame_delta=2, note=Note(name="String Proxy Edit", tint=(1,1,1)), max_depth=10)


        # request_render()
    imgui.separator()
    imgui.text("Tree Proxy — {value: <GeneralParse>}")
    changed, value = draw_collection(dict_proxy, name="Tree Proxy Dict", column=1, disable_scroll=False)
