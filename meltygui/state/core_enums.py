from src.lsd.gl_gui.model.model_enums import RelaxedEnum



class ViewMode(RelaxedEnum):
    NONE = "None"
    IMGUI_WINDOW = "imgui-window"


class ProfileMode(RelaxedEnum):
    OFF = "Off"
    LIGHT = "Light"
    ON = "On"
