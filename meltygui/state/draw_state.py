from typing import Dict

from src.lsd.gl_gui.model.app_model import Lora
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.core_settings import window


@window
class NewLoraCollection(DictConversion):
    def __init__(self):
        super().__init__()
        self.name: str = "Lora Collection"
        self.test_path = "batch_gen_collection.batch_gens"

        self.loras: Dict[str, Lora] = {}
        self._collection_type = Lora
        self.tint = (0.2, 0.26, 0.34)


class SynthColors(DictConversion):
    def __init__(self):
        super().__init__()
        self.letter_to_color = {}


# @window
# class Lora(DictConversion):
#     def __init__(self):
#         super().__init__()
#         self.tint = (0.2, 0.26, 0.34)
#         self.name: str = "Lora"
#         self.rank = 4
#
#         ignore_render
#         self.alpha = 2
#         self.lora_scale = 0.1
#         self.parent_module = None
#         self.adapter = None
#         self.target_modules = ["q_proj", "k_proj", "v_proj", "o_proj"]
