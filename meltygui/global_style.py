from typing import Any

from meltygui.rendering.decorators.window_decoration import window


@window
class BackgroundSettings:
    dot_spacing = 4.917
    dot_size = 1.798
    emphasis_size = 1.2
    dot_color = (0.2, 0.23, 0.3, 1.0)
    bg_color = (0.027, 0.055, 0.07, 1.0)


class GlobalStyle:
    object_corner_radius = 5.0
    window_corner_radius = 5.0
    item_corner_radius = 5.0

    attr_name_indent = 20
    base_value = 0.3
    secondary_value = 0.5
    background_settings = BackgroundSettings()
    widget_styles = {}
    excluded_names = set()
    skip_if_none = set()
    profiler = {
        "object_attr": {
            'dynamic_value': 0.042,
            'dynamic_offset': -3.855,
            'dynamic_saturation_factor': -3.855,
            'dynamic_saturation_offset': -3.855,
            'value': -3.855,
            'saturation': 0.5
        },
    }
    dropdown = {
        "standard": {
            "background": {
                "value": 0.01,
                "saturation": 1.0,
                "alpha": 1.0,
                'max_value': 1.0
            },
            "text": {
                "value": 0.01,
                "saturation": 1.0,
                "alpha": 1.0,
                'max_value': 1.0
            },
            "button": {
                "value": 0.12,
                "saturation": 0.9,
                "alpha": 1.0,
                'max_value': 1.0
            }
        },
        "hovered": {
            "background": {
                "value": 0.066,
                "saturation": 1.0,
                "alpha": 1.0,
                'max_value': 1.0
            },
            "text": {
                "value": 0.01,
                "saturation": 1.0,
                "alpha": 1.0,
                'max_value': 1.0
            },
            "button": {
                "value": 0.12,
                "saturation": 0.9,
                "alpha": 1.0,
                'max_value': 1.0
            }
        },
    }

    indented_bg = {
        "dict_child": {
            "value": 0.01,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        },
        "dict_child_outline": {
            "value": 0.01,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        },
        "object_attr": {
            "value": 0.12,
            "saturation": 0.9,
            "alpha": 1.0,
            'max_value': 1.0,
            'dynamic_value': 0.042,
            'dynamic_offset': -3.855,
        },
        "object_attr_outline": {
            "value": 0.01,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        },
    }
    radio_button = {
        "active_base": {
            "value": 0.2,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        },
        "active_hover": {
            "value": 0.5,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        },
        "active_pressed": {
            "value": 0.3,
            "saturation": 0.4,
            "alpha": 0.35,
            'max_value': 1.0
        },
        "inactive_base": {
            "value": 0.2,
            "saturation": 0.4,
            "alpha": 0.35,
            'max_value': 1.0
        },
        "inactive_hover": {
            "value": 0.4,
            "saturation": 0.5,
            "alpha": 1.0,
            'max_value': 1.0
        },
        "inactive_pressed": {
            "value": 0.35,
            "saturation": 0.5,
            "alpha": 0.35,
            'max_value': 1.0
        },

    }

    main_const = {
        "window": {
            "background": {
                "value": 0.01,
                "saturation": 0.7,
                "alpha": 1.0
            },
            "border": {
                "value": 0.34,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "child_bg": {
                "value": 0.01,
                "saturation": 0.7,
                "alpha": 1.0
            },
            "popup_bg": {
                "value": 0.01,
                "saturation": 0.7,
                "alpha": 1.0
            },
            "title_bg": {
                "value": 0.01,
                "saturation": 0.7,
                "alpha": 1.0
            },
            "title_bg_active": {
                "value": 0.01,
                "saturation": 0.7,
                "alpha": 1.0
            },
            "title_bg_collapsed": {
                "value": 0.01,
                "saturation": 0.5,
                "alpha": 1.0
            },
            "header": {
                "value": 0.01,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "header_hovered": {
                "value": 0.45,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "header_active": {
                "value": 0.55,
                "saturation": 1.0,
                "alpha": 1.0
            }
        },

        "widget": {
            "text": {
                "value": 0.95,
                "saturation": 0.2,
                "alpha": 1.0
            },
            "text_disabled": {
                "value": 0.5,
                "saturation": 0.2,
                "alpha": 1.0
            },
            "resize_grip": {
                "value": 0.35,
                "saturation": 0.8,
                "alpha": 1.0
            },
            "resize_hovered": {
                "value": 0.45,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "resize_active": {
                "value": 0.55,
                "saturation": 1.0,
                "alpha": 1.0
            },
            "button": {
                "value": 0.35,
                "saturation": 0.8,
                "alpha": 1.0
            },
            "button_hovered": {
                "value": 0.45,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "button_active": {
                "value": 0.55,
                "saturation": 1.0,
                "alpha": 1.0
            },
            "check_mark": {
                "value": 0.9,
                "saturation": 1.0,
                "alpha": 1.0
            },
            "text_selected_bg": {
                "value": 0.35,
                "saturation": 0.8,
                "alpha": 1.0
            },
            "slider_grab": {
                "value": 0.5,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "slider_grab_active": {
                "value": 0.6,
                "saturation": 1.0,
                "alpha": 1.0
            },
            "scrollbar_grab": {
                "value": 0.5,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "scrollbar_grab_hovered": {
                "value": 0.5,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "scrollbar_grab_active": {
                "value": 0.5,
                "saturation": 0.9,
                "alpha": 1.0
            }
        },

        "frame": {
            "frame_bg": {
                "value": 0.15,
                "saturation": 0.9,
                "alpha": 1.0
            },
            "frame_hovered": {
                "value": 0.25,
                "saturation": 0.5,
                "alpha": 1.0
            },
            "frame_active": {
                "value": 0.3,
                "saturation": 0.6,
                "alpha": 1.0
            },
            "separator": {
                "value": 0.4,
                "saturation": 0.7,
                "alpha": 1.0
            }
        },

        "tab": {
            "tab": {
                "value": 0.25,
                "saturation": 0.7,
                "alpha": 1.0
            },
            "tab_hovered": {
                "value": 0.35,
                "saturation": 0.8,
                "alpha": 1.0
            },
            "tab_active": {
                "value": 0.4,
                "saturation": 0.9,
                "alpha": 1.0
            }
        }
    }

    @classmethod
    def get_global_constant(cls, constant_name: str, folder, default_type=None, default: Any = None):

        if not hasattr(cls, folder) or getattr(cls, folder) is None:
            setattr(cls, folder, {})
        constant_dict = getattr(cls, folder)
        if constant_name not in constant_dict or constant_dict[constant_name] is None and default is not None:
            constant_dict[constant_name] = default

        if default_type is not None and (constant_name not in constant_dict
                                         or not isinstance(constant_dict[constant_name], default_type)):
            constant_dict[constant_name] = default_type()
        #
        # if hasattr(self.settings, "_base_pref") and self.settings._base_pref is not None:
        #     setattr(self.settings._base_pref._constants, folder, f"global_constant.{folder}")

        return constant_dict[constant_name]
