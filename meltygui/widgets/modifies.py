import imgui

from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
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

string_proxy = RenderHost(io_function=code_file_io, input_value=Toggles, name="String Proxy",
                          renderer=RenderFuncs.draw_blank,  # held value is source code
                          child_kwargs={"auto_load_edits": True})   # auto-reload on external file change

dict_proxy = RenderHost(
    io_function=convert_in_and_out_value, input_value=string_proxy, name="Tree Proxy", renderer=RenderFuncs.draw_blank,
    child_kwargs={
        "chain_in": [string_to_cst_module, cst_module_to_dict],
        "chain_out": [dict_to_cst_module, cst_module_to_string],
        "route": {cst_module_to_dict: ("code_dict", "jump_to", "run_jedi", "drive")},
    })


@window
@render_func(tint=(0.0923043042421341, 0.041103292256593704, 0.23255813121795654), auto_resize=True)
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

    myval = InCode.show_bg
