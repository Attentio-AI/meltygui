from collections import defaultdict
from typing import Any

from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


@window(view_func=RenderFuncs.draw_type, disable_scroll=False)
class PendingSave:


    pending_saves = defaultdict(Any)
    originals = defaultdict(Any)

    @classmethod
    def mark_load(cls, address, codec, data, **kwargs):
        cls.originals[address] = data

    @classmethod
    def queue_save(cls, address, codec, **kwargs):
        cls.pending_saves[address] = codec, kwargs

    @classmethod
    def apply_all_saves(cls):
        for address, (codec, kwargs) in cls.pending_saves.items():
            codec.save(address=address, **kwargs)

        cls.pending_saves.clear()