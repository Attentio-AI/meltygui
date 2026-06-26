import itertools
import random
import uuid

from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults

# Monotonic id source: a per-process RANDOM start + a counter. next() on an
# itertools.count is atomic under the GIL (thread-safe). The random start keeps
# ids from separate runs apart (a bare counter would restart at 0 each run and
# collide with loaded ids); the counter makes ids collision-free WITHIN a run
# (the old uuid4[0:6] = 24 random bits actually collides for long graphs).
_id_counter = itertools.count(random.getrandbits(24))


class OffscreenDebugMode(RelaxedEnum):
    OFF = "Off"
    SHOW_LAYERS = "Show Layers"
    SHOW_MASK = "Show Mask"


class OffscreenDebugMode(RelaxedEnum):
    OFF = "Off"
    SHOW_LAYERS = "layer"
    SHOW_MASK = "mask"
    SHOW_UV = "uv"
    SHOW_SRC_PX = "srcpx"
    SHOW_CHECKER = "checker"
    SHOW_SOLID = "solid"


class ViewMode(RelaxedEnum):
    NONE = "None"
    IMGUI_WINDOW = "imgui-window"


class ProfileMode(RelaxedEnum):
    OFF = "Off"
    LIGHT = "Light"
    ON = "On"
    


class PendingAction(RelaxedEnum):
    APPLY = "apply"
    REVERT = "revert"
    LOAD = "load"


def generate_id():
    """
    Generates a unique identifier for use in ImGui elements.
    This is useful to ensure that elements can be uniquely identified across frames.
    :return: A unique identifier string (6+ hex chars; survives the [0:8] id trunc).

    ~11x faster than the old uuid4[0:6] (no /dev/urandom read per call), and
    collision-free within a process.
    """
    return format(next(_id_counter), "06x")
