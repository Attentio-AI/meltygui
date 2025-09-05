import uuid

from src.lsd.gl_gui.model.model_enums import RelaxedEnum



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
