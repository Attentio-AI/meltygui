"""LoRA UI models shared by the studio and lightweight Melty apps.

PEFT is only imported when an adapter action runs; inspecting a LoRA tree
needs neither model weights nor the studio's ML imports.
"""
from typing import Any, Dict

from meltygui.core.dict_conversion import DictConversion
from meltygui.state.core_enums import generate_id
from meltygui.core.core_decoration import defaults
from meltygui.core.window_decoration import window


###### Legacy context windows
# [tint=(0.11837751418352127, 0.21678964793682098, 0.33488374948501587)]

@window
@defaults(show_tint=True, show_add_delete=False, child_kwargs={'show_add_delete':False})
@defaults(attr="adapter", visible_in_ui=True)
class Lora(DictConversion):
    # [tint=(0.7023256, 0.5572568, 0.29726338386535645)]
    tint = (0.1111, 0.0111, 0.0844)
    adapter: Any = None
    parent_model = None
    alpha = 7.062
    lora_dropout = 0.31
    root_window = None
    name: str = "Lora "
    rank=1
    target_modules = ["q_proj", "v_proj"]
@defaults(show_add_delete=True)
@defaults(attr="loras", icon="", show_add_types={"Folder": dict}, child_kwargs={})
class LoraCollection(DictConversion):
    loras: Dict[str, Lora] = {}

    def __init__(self):
        super().__init__()
        self.id = generate_id()
        self.name: str = "Lora Collection"

        self.loras: Dict[str, Lora] = {}
        self._collection_type = Lora  

