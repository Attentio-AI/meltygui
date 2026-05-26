from typing import Dict, Any

from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, DecorationManager
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


@window(tint=(0.14247702062129974,0.17209303379058838,0.0))
@defaults(attrib="keep_for_frames", tint=(0.22271497547626495,0.674418568611145,0.2395225167274475))
class InvalidateTracker:
    invalidations: Dict[str, Any] = {}
    keep_for_frames = 1

    @classmethod
    def on_frame_end(cls):
        to_delete = []

        for key in list(cls.invalidations.keys()):
            inv = cls.invalidations[key]
            on_frame = inv[1]
            if on_frame + cls.keep_for_frames < DecorationManager.melty.frame_count:
                to_delete.append(key)

        for key in to_delete:
            cls.invalidations.pop(key, None)
