import inspect
import types
from pathlib import Path

import imgui

from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_conversion.new_converters import (
    code_file_io, convert_in_and_out_value, string_to_cst_module, cst_module_to_string,
    cst_module_to_dict, dict_to_cst_module)
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.columns import draw_columns
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any, draw_collection
from src.lsd.gl_gui.view.core_views.text_editor import draw_text
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

_code_host_cache: dict = {}


def code_hosts_for(ref):
    """The shared (source_str_host, cst_dict_host) RenderHost pair for a
    function / class / module / CallSite — the same wiring draw_input_tab used
    to build per menu-open, now built ONCE per distinct reference and reused:

        str_host   code_file_io <- ref          (the editable source text)
        dict_host  convert_in_and_out_value     (source <-> cst dict via the
                   <- str_host                   NEW_CODE chain, "code_dict"
                                                 routed to the editor)

    Lazy: nothing loads until the first consumer draws the host. Consumers that
    read the value outside the host's own draw loop must still register via
    host.notify_on_change(draw_state), exactly as before."""
    # Key by the ref ITSELF (object-equality), not str(id(ref)). See the matching
    # note at new_file_view.code_hosts_for: an identity key leaks a new host pair on
    # every CallSite(f, ln) / Path, and recycles ids into wrong cache hits.
    try:
        pair = _code_host_cache.get(ref)
        cacheable = True
    except TypeError:  # genuinely unhashable ref - skip the cache
        pair, cacheable = None, False
    if pair is None:
        from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
        label = getattr(ref, "__name__", None) or type(ref).__name__
        tag = id(ref)  # unique among concurrently-cached (keep-alive) refs
        str_host = RenderHost(io_function=code_file_io, input_value=ref,
                              name=f"##code_cache_{label}{tag}_str",
                              child_kwargs={"auto_load_edits": True, "auto_load": True})

        # string_proxy = RenderHost(io_function=code_file_io, input_value=draw_text, name="String Proxy test",
        #
        #                           child_kwargs={"auto_load_edits": False, "auto_load":True})   # auto-reload on external file change
        #

        # Whole-FILE refs get the static name/signature checker (code_checks): the
        # buffer is self-contained, so an unresolved name really IS a NameError.
        # A partial ref (func/class/CallSite) sees none of its module's imports
        # # and would flag every one - no lint_path, no lint.
        lint_path = None
        # if isinstance(ref, Path) and ref.suffix == ".py":
        #     lint_path = str(ref)
        # elif isinstance(ref, types.ModuleType):
        #     lint_path = getattr(ref, "__file__", None)
        dict_host = RenderHost(
            io_function=convert_in_and_out_value, input_value=str_host,
            name=f"##code_cache_{label}{tag}_dict",
            child_kwargs={
                "chain_in": [string_to_cst_module, cst_module_to_dict],
                "chain_out": [dict_to_cst_module, cst_module_to_string],
                "route": {cst_module_to_dict: ("code_dict", "jump_to", "run_jedi", "drive")},
                **({"run_chain_kwargs": {"lint_path": lint_path}} if lint_path else {}),
            })
        pair = (str_host, dict_host)
        if cacheable:
            _code_host_cache[ref] = pair
    return pair


string_proxy, dict_proxy = code_hosts_for(draw_text)


@window(disable_scroll=False, use_cache=True)
@render_func(tint=(0.078, 0.232, 0.439), auto_resize=True)
def test_code_ui(_, draw_state):
    # Both proxies show themselves in their own windows (draw_main → draw()). This
    # window just inspects them AS ordinary dicts - the framework has no idea they're
    # proxies; it's rendering ordinary dicts whose single value was materialized by
    # their wrappers. Editing here bubbles back exactly the same way.

    #
    # changed, value = draw_collection(string_proxy, name="String Proxy", disable_scroll=False, child_kwargs={"view_func": draw_text, "code_dict": dict_proxy})
    # changed, value = draw_collection(dict_proxy, name="Dict Proxy", disable_scroll=False)

    draw_columns({"text":string_proxy, "dict": dict_proxy})

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
