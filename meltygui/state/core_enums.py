import uuid

from src.lsd.gl_gui.model.model_enums import RelaxedEnum


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


def generate_id():
    """
    Generates a unique identifier for use in ImGui elements.
    This is useful to ensure that elements can be uniquely identified across frames.
    :return: A unique identifier string.
    """
    return str(uuid.uuid4())[0:6]
