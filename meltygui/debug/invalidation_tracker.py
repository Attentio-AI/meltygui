from dataclasses import dataclass
from typing import Dict, Any

from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, DecorationManager

@dataclass
class Note:
    name: str = ""
    tint: tuple = (1,0,0)
    reason: str = ""
    draw_state: any = None
    frame: int = 0
    rect: any=None


@defaults(attrib="keep_for_frames", tint=(0.22271497547626495,0.674418568611145,0.2395225167274475))
class InvalidateTracker:
    invalidations: Dict[str, Any] = {}

    @classmethod
    def on_frame_end(cls):
        if not Toggles.InvalidateTracker.enable and not Toggles.InvalidateTracker.draw_bvh:
            cls.invalidations = {}
            return

        to_delete = []

        for key in list(cls.invalidations.keys()):
            inv = cls.invalidations[key]
            on_frame = inv.frame

            if on_frame + Toggles.InvalidateTracker.keep_for_frames < DecorationManager.melty.frame_count:
                to_delete.append(key)

        for key in to_delete:
            cls.invalidations.pop(key, None)
