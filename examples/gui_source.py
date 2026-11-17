import meltygui
from meltygui.core.rendering.gui_any import draw_any
from meltygui.core.runtime.toggles import Toggles
from meltygui.model.source_model import Source

def sample_function(my_name:str):
    print(f"Hello! {my_name}")

toggles_source = Source(Toggles)
function_source = Source(sample_function)
module_source = Source(meltygui.core.rendering.gui_any)

@meltygui.os_window(name="Toggles", app_id="toggles", width=640, height=360)
def main():

    global source
    draw_any(toggles_source.tree)

    global function_source
    draw_any(function_source.tree)

    global module_source
    draw_any(module_source.tree)
