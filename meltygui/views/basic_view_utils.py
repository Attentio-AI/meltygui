import inspect
import sys

import imgui

from src.lsd.gl_gui.model.core_model.core_model import Metadata, BasePref, SettingsScope
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.utils.custom_views import LSDView, print_stack_trace, push_style_color, pop_style_color, \
    print_colored_traceback


def set_cursor_pos_y(pos_y):
    current_y = imgui.get_cursor_pos_y()
    imgui.set_cursor_pos_y(pos_y)
    height = imgui.get_cursor_pos_y() - current_y
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_pos_x(pos_x):
    current_x = imgui.get_cursor_pos_x()
    imgui.set_cursor_pos_x(pos_x)
    width = imgui.get_cursor_pos_x() - current_x
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_screen_pos(pos):
    imgui.set_cursor_screen_pos(pos)
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_screen_position(pos):
    imgui.set_cursor_screen_position(pos)
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_pos(pos):
    current_pos = imgui.get_cursor_pos()
    imgui.set_cursor_pos(pos)
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def spacing():
    imgui.spacing()
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(0, 0.1, 0.7, 0.8)):
            print_stack_trace(skip=-2)


def indent(indent_size=None, attr_name=None):
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(0.1, 0.1, 1.0, 0.8)):
            print_stack_trace(skip=-2)
            if attr_name is not None:
                print(f"=======Indenting for {attr_name}=========")

        if not (is_hovered(width=5, height=None) and imgui.is_mouse_down(imgui.MOUSE_BUTTON_MIDDLE)):
            imgui.indent(indent_size)
    else:
        imgui.indent(indent_size)


def same_line(spacing=None):
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(1.0, 0.5, 0.1, 0.5)):
            print_stack_trace(skip=-2)
        if not (is_hovered(width=3, height=None) and imgui.is_mouse_down(imgui.MOUSE_BUTTON_MIDDLE)):
            if spacing is None:
                imgui.same_line()
            else:
                imgui.same_line(spacing=spacing)
    else:
        if spacing is None:
            imgui.same_line()
        else:
            imgui.same_line(spacing=spacing)


def new_line():
    if LSDView().vis.root.global_toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(0.5, 0, 0, 0.5)):
            print_stack_trace(skip=-2)
        if not (is_hovered(width=3, height=None) and imgui.is_mouse_down(imgui.MOUSE_BUTTON_MIDDLE)):
            imgui.new_line()
    else:
        imgui.new_line()


def validate(expected_type=None, input_value=None, config=None, expected_settings_type=None):
    vis = LSDView().vis
    valid = True
    actual_type_melty = vis.root.datatypes._name_to_class.get(type(input_value).__name__, None)

    if config is None:
        config = Metadata()
    if config.attr_name is None or config.unique is None:
        config.vis = LSDView().vis
        config.datatype = actual_type_melty
        config.attr_name = f"unnamed_{actual_type_melty.name}"
        config.settings, _ = get_pref(input_value, config)
        caller_function_name = inspect.currentframe().f_back.f_code.co_name
        config.unique = f"{caller_function_name}##{config.attr_name}{actual_type_melty.name}"

    if expected_settings_type is not None:
        caller_function = inspect.currentframe().f_back.f_code.co_name
        if caller_function in config.vis.root.view_functions._name_to_func:
            view_function = config.vis.root.view_functions._name_to_func.get(caller_function, None)
            if view_function is None:
                imgui.text_colored(f"View function not found: {caller_function}",
                                   *(1.0, 0.0, 0.0))
                return False, config
            expected_type_name = expected_settings_type.__name__
            expected_type_melty = vis.root.datatypes._name_to_class.get(expected_type_name, None)
            if expected_type_melty is None:
                imgui.text_colored(f"Expected type not found",
                                   *(1.0, 0.0, 0.0))
                return False, config

            actual_settings_type = view_function.settings_datatype.selected_object
            input_value

            if not isinstance(config.settings, expected_settings_type):
                push_style_color(imgui.COLOR_TEXT, *(1.0, 0.0, 0.0))
                imgui.text(f"Settings type mismatch:")
                results = {
                    "expected": expected_type_melty.name,
                    "actual": actual_settings_type.name
                }
                render_dict_as_table(input_value=results, config=config)
                pop_style_color()

                imgui.text(f"Set settings type to {view_function.name}")
                if imgui.button(f"Fix Settings Type##{config.unique}"):
                    view_function.settings_datatype.selected_object = expected_type_melty

                return False, config

    if expected_type is not None:
        expected_type_name = expected_type.__name__
        expected_type_melty = vis.root.datatypes._name_to_class.get(expected_type_name, None)
        actual_type_melty = vis.root.datatypes._name_to_class.get(type(input_value).__name__, None)
        valid = True

        if expected_type_melty is None or actual_type_melty is None:
            imgui.text_colored(f"Type not found in datatypes: {expected_type_name}", *(1.0, 0.0, 0.0))
            valid = False

        if not isinstance(input_value, expected_type):
            push_style_color(imgui.COLOR_TEXT, *(1.0, 0.0, 0.0))
            results = {
                "expected": expected_type_name,
                "actual": type(input_value).__name__
            }
            imgui.text(f"Input value type mismatch:")
            render_dict_as_table(input_value=results, config=config)
            pop_style_color()
            valid = False

    return valid, config


def get_pref(input_value, config, renderer=None, new_settings=False):
    try:
        vis = config.vis
        object_type = input_value.__class__.__name__ if hasattr(input_value, '__class__') else "object"
        super_type = input_value.__class__.__bases__[0].__name__ if hasattr(input_value, '__class__') else "object"
        root_window = config.root_window
        parent = config.parent
        attr_name = config.attr_name
        direct_parent = config.direct_parent

        if attr_name == "max_value":
            pass

        potential_datatype = None
        if object_type in vis.root.datatypes._name_to_class:
            potential_datatype = vis.root.datatypes._name_to_class[object_type]
        elif super_type in vis.root.datatypes._name_to_class:
            potential_datatype = vis.root.datatypes._name_to_class[super_type]

        if potential_datatype is None:
            melty_datatype = vis.root.datatypes._name_to_class["object"]
        else:
            melty_datatype = potential_datatype

        if root_window is not None and hasattr(root_window, 'theme'):
            theme = root_window.theme.selected_object
        else:
            theme = list(vis.root.theme_collection.themes.values())[0]
            if root_window is not None:
                root_window.theme = theme

        parent_datatype_name = parent.__class__.__name__ if parent is not None else "Any"
        if parent_datatype_name not in theme.view_prefs:
            theme.view_prefs[f"{parent_datatype_name}"] = {}

        if (attr_name not in theme.view_prefs[parent_datatype_name] or
                theme.view_prefs[parent_datatype_name][attr_name] is None) or new_settings:
            base_pref = BasePref()
            theme.view_prefs[f"{parent_datatype_name}"][f"{attr_name}"] = base_pref
            base_pref.renderer = list(melty_datatype.func_mapping.values())[0].selected_object
            base_pref.attr_name = attr_name
            base_pref.input_value = input_value
        else:
            base_pref = theme.view_prefs[f"{parent_datatype_name}"][f"{attr_name}"]

        if renderer is not None:
            base_pref.renderer = renderer

        settings_type = base_pref.renderer.settings_datatype.selected_object
        if settings_type is None or settings_type._type is None:
            new_settings_type = vis.root.datatypes._name_to_class.get("AnySettings", None)
            base_pref.renderer.settings_datatype.selected_object = new_settings_type

        if config.settings is not None:
            if not isinstance(base_pref.settings, settings_type._type):
                # Change the type to updated, but keep the old settings
                old_settings = base_pref.settings
                base_pref.settings = settings_type._type()
                print(f"Created settings at theme.view_prefs[\"{parent_datatype_name}\"][\"{attr_name}\"]")
                base_pref.settings._base_pref = base_pref
                copy_attributes(old_settings, base_pref.settings)
        else:
            if base_pref.settings is None:
                if base_pref.renderer.default_setting is None:
                    base_pref.settings = settings_type._type()
                    base_pref.renderer.default_setting = settings_type._type()
                else:
                    base_pref.settings = base_pref.renderer.default_setting

            if not isinstance(base_pref.settings, settings_type._type):
                # Change the type to updated, but keep the old settings
                old_settings = base_pref.settings
                base_pref.settings = settings_type._type()
                print(f"Created settings at theme.view_prefs[\"{parent_datatype_name}\"][\"{attr_name}\"]")
                base_pref.settings._base_pref = base_pref
                copy_attributes(old_settings, base_pref.settings)

        base_pref.settings._base_pref = base_pref
        base_pref.parent = config.parent
        base_pref.value_type = melty_datatype

        if isinstance(input_value, DictConversion) or hasattr(input_value, '_settings'):
            input_value._settings = base_pref.settings

        if parent is not None and hasattr(parent, '_attr_settings'):
            if parent._attr_settings is None:
                parent._attr_settings = {}
            parent._attr_settings[attr_name] = base_pref.settings

        if direct_parent is not None and hasattr(direct_parent, '_attr_settings'):
            if direct_parent._attr_settings is None:
                direct_parent._attr_settings = {}
            direct_parent._attr_settings[attr_name] = base_pref.settings

        if base_pref.settings._base_pref is None:
            pass

        config.settings = base_pref.settings
        config.datatype = melty_datatype

        try:
            if config.parent is not None and config.parent._settings is not None and hasattr(config.parent._settings, 'child_view_function'):
                if config.parent._settings.child_view_function is not None:
                    child_function = config.parent._settings.child_view_function
                    settings_type = child_function.settings_datatype.selected_object

                    if config.parent._settings.child_settings is None or not isinstance(
                            config.parent._settings.child_settings, settings_type._type):
                        old_settings = config.parent._settings.child_settings
                        config.parent._settings.child_settings = settings_type._type()
                        copy_attributes(old_settings, config.parent._settings.child_settings)

                    base_pref.settings = config.parent._settings.child_settings

                    base_pref.settings._base_pref = base_pref
        except Exception as e:
            print(f"Error setting child view function settings: {str(e)}")

        return base_pref.settings, potential_datatype
    except Exception as e:
        return None, None
        # global_settings = LSDView().vis.root.global_standard_settings
        # if global_settings._base_pref is None:
        #     global_settings._base_pref = BasePref()
        #     global_settings._base_pref.settings = global_settings
        #     global_settings._base_pref.renderer = LSDView().vis.root.view_functions._name_to_func["render_object"]
        # return global_settings


def copy_attributes(source, target):
    """
    Copies attributes from source to target, excluding private attributes.
    If an attribute exists in both, it will be overwritten in the target.
    """
    if not isinstance(source, DictConversion) and not isinstance(target, DictConversion):
        return

    for attr_name, attr_value in source.__dict__.items():
        setattr(target, attr_name, attr_value)


def is_hovered(x=None, y=None, width=5, height=None):
    padding = imgui.get_style().window_padding
    if x is None:
        x = imgui.get_cursor_screen_pos()[0] - padding[0]
    if y is None:
        y = imgui.get_cursor_screen_pos()[1] - padding[1]
    if height is None:
        line_height = imgui.get_text_line_height()
        height = line_height + padding[1] * 2
    return imgui.is_mouse_hovering_rect(x, y, x + max(width, 10), y + height)


def render_dict_as_table(input_value=None, config=None):
    unique = config.unique

    num_columns = 2  # For key / value pair
    imgui.columns(num_columns, f"dict_columns##{unique}", True)
    for key, value in input_value.items():
        imgui.text(f"{key}")
        imgui.next_column()
        imgui.text(f"{value}")
        imgui.next_column()

    imgui.columns(1)  # Reset to single column layout


def draw_rect(x=None, y=None, width=5, height=None, color=(1, 1, 1, 1)):
    padding = imgui.get_style().window_padding
    if x is None:
        x = imgui.get_cursor_screen_pos()[0] - padding[0]
    if y is None:
        y = imgui.get_cursor_screen_pos()[1] - padding[1]
    if height is None:
        line_height = imgui.get_text_line_height()
        height = line_height + padding[1] * 2
    imgui.get_foreground_draw_list().add_rect_filled(
        x, y, x + width, y + height, imgui.get_color_u32_rgba(*color))

    clicked = False
    if imgui.is_mouse_hovering_rect(x, y, x + max(width, 10), y + height):
        white = (1.0, 1.0, 1.0, 1.0)
        imgui.get_foreground_draw_list().add_rect(
            x, y, x + width, y + height, imgui.get_color_u32_rgba(*white))
        if imgui.is_mouse_clicked(imgui.MOUSE_BUTTON_LEFT):
            clicked = True
    return clicked
