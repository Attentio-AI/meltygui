"""LoRA UI models shared by the studio and lightweight Melty apps.

PEFT is only imported when an adapter action runs; inspecting a LoRA tree
needs neither model weights nor the studio's ML imports.
"""
from typing import Any, Dict

from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.core_model.core_enums import generate_id
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


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
    def on_load(self, vis, root):
        from peft import LoraConfig
        if not isinstance(self.alpha, (int, float)):
            self.alpha = 1
        try:
            self.adapter = LoraConfig(
                r=self.rank,
                lora_alpha=int(self.alpha),
                target_modules=self.target_modules,
            lora_dropout=self.lora_dropout,
            )
        except Exception as e:
            print("Error creating LoraConfig:", e)

    def attach_adapter(self, input_value, settings=None, vis=None,
                       root_window=None, parent=None, direct_parent=None, unique=None,
                       attr_name=None, display_name=None,
                       datatype=None, expanded=True, depth=1):

        from peft import get_peft_model

        # Actually attach the adapter
        model = get_peft_model(direct_parent.parent_model.model.model, direct_parent.adapter)
        direct_parent.parent_model.model = model

    def reload(self, input_value, settings=None, vis=None,
               root_window=None, parent=None, direct_parent=None, unique=None,
               attr_name=None, display_name=None,
               datatype=None, expanded=True, depth=0):
        from peft import LoraConfig

        direct_parent.adapter = LoraConfig(
            r=direct_parent.rank,
            lora_alpha=int(direct_parent.alpha),
            target_modules=direct_parent.target_modules,
            lora_dropout=direct_parent.lora_dropout,
        )

@defaults(show_add_delete=True)
@defaults(attr="loras", icon="", show_add_types={"Folder": dict}, child_kwargs={})
class LoraCollection(DictConversion):
    loras: Dict[str, Lora] = {}
    test_path = "batch_gen_collection.batch_gens"

    def __init__(self):
        super().__init__()
        self.id = generate_id()
        self.name: str = "Lora Collection"

        self.loras: Dict[str, Lora] = {}
        self._collection_type = Lora  

