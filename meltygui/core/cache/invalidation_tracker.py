from dataclasses import dataclass
from typing import Dict, Any

from meltygui.core.diagnostics.notifications import notify
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.rendering.core_decoration import defaults
from meltygui.core.rendering.core_decoration import Core

@dataclass
class Note:
    name: str = ""
    tint: tuple = (1,0,0)
    reason: str = ""
    draw_state: any = None
    frame: int = 0
    rect: any=None


@dataclass
class RenderNote:
    """One cached view's live render: its own render-thread time (ms),
    the nested cached views' time excluded, on the frame it rendered."""
    draw_state: any = None
    frame: int = 0
    ms: float = 0.0


_hot_lut = None


def hot_color(ms):
    """The `hot` LUT (lut_model: black → red → yellow → white) sampled at
    ms / Toggles.InvalidateTracker.render_time_max_ms, clamped to white."""
    global _hot_lut
    if _hot_lut is None:
        from meltygui.model.lut_model import make_luts
        _hot_lut = make_luts()["hot"]
    n = len(_hot_lut) // 3
    v = max(0.0, min(1.0, ms / max(1e-6, Toggles.InvalidateTracker.render_time_max_ms)))
    i = 3 * min(n - 1, int(v * (n - 1) + 0.5))
    return _hot_lut[i], _hot_lut[i + 1], _hot_lut[i + 2]


@defaults(attrib="keep_for_frames", tint=(0.22271497547626495,0.674418568611145,0.2395225167274475))
class InvalidateTracker:
    invalidations: Dict[str, Any] = {}
    # tile key -> RenderNote of its latest live render (tile_cache
    # mark_end_offscreen); the overlay in Melty.end_frame paints them.
    render_times: Dict[str, RenderNote] = {}

    @classmethod
    def note_render(cls, key, draw_state, ms):
        if not Toggles.InvalidateTracker.enable:
            return
        cls.render_times[key] = RenderNote(draw_state=draw_state, frame=Core.melty.frame_count, ms=ms)

    @classmethod
    def on_frame_end(cls):
        if not Toggles.InvalidateTracker.enable and not Toggles.InvalidateTracker.draw_bvh:
            cls.invalidations = {}
            cls.render_times = {}
            return

        keep = Toggles.InvalidateTracker.keep_for_frames
        for key in [k for k, r in cls.render_times.items() if r.frame + keep < Core.melty.frame_count]:
            cls.render_times.pop(key, None)

        to_delete = []

        for key in list(cls.invalidations.keys()):
            inv = cls.invalidations[key]
            on_frame = inv.frame

            if on_frame + Toggles.InvalidateTracker.keep_for_frames < Core.melty.frame_count:
                to_delete.append(key)

        for key in to_delete:
            cls.invalidations.pop(key, None)


        if Toggles.InvalidateTracker.enable:
            notify(f"Invalidations: {len(cls.invalidations)}", tag="InvalidateTracker", tint=(1,1,0.4), urgent=False)
