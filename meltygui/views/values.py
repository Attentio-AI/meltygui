import inspect
import types
from copy import copy
from enum import Enum
from functools import wraps
from inspect import Parameter
from math import sqrt
from types import NoneType

import glfw
import imgui

from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, tree, request_render, push_style_var, \
    push_style_color, pop_style_color, pop_style_var
from src.lsd.gl_gui.melty import Melty, CollectionAction, OperationType, apply_collection_action, add_to_collection, \
    delete_from_collection
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line, new_line
from src.lsd.gl_gui.view.core_views.core_render import render_func, tmp_undo_stack, redo_stack, push_id, pop_id, ui_id, \
    render_wrapper, annotation_track, listens_for, get_draw_state
from src.lsd.gl_gui.model.core_model.new_core_model import KeyMod, Hotkey
from src.lsd.gl_gui.view.core_views.cst_proxy import *
import libcst as cst

from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy
from src.lsd.gl_gui.view.core_views.inspect_utils import get_params, set_fn_defaults
from collections.abc import MutableMapping


@render_wrapper(wraps=render_func)
def with_header_minimal(func, *args, **o_kwargs):
    def wrapper(next_kwargs=None, **kwargs):
        annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
        if annotation is not None: return annotation

        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        next_kwargs['y_offset'] = 0
        next_kwargs['enable_flow'] = False
        next_kwargs['show_bg'] = False
        next_kwargs['is_tree'] = False
        next_kwargs['min_width'] = kwargs.get('min_width', 200)

        return core_header(**next_kwargs)

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header_minimal")
    return wrapper


@render_wrapper(wraps=render_func)
def with_header(func, *args, **o_kwargs):
    def wrapper(next_kwargs=None, draw_state=None, **kwargs):
        annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
        if annotation is not None: return annotation

        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        if 'width' in kwargs:
            pass
        return core_header(**next_kwargs)

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header ")

    return wrapper


source = "x = foo(val=1)\nprint(x)\nsome_list=[0, 1, 2, 3]\n"
module = cst.parse_module(source)
proxy = cst_wrap(module)
name_edits = {}
code_export_str = "Test"

filesystem_proxy = FolderProxy("/home/lukas/test_folder", text_mode=True)
# Main draw function, called by the GUI framework
def draw(vis):

    draw_window(vis.root.lora_collection, name="Lora Root")
    draw_window(Melty.hotkey_registry, name="Hotkeys", is_window=True)
    draw_window(module, name="CST Module")

    global proxy
    draw_window(proxy, name="CST Proxy")

    global filesystem_proxy
    draw_window(filesystem_proxy, name="Filesystem")

    global code_export_str
    changed, code_str = draw_window(code_export_str, name="Code Export")

    if changed:
        print("Code changed")
        print(code_str)
        try:
            code_export_str = code_str
            new_module = cst.parse_module(code_str)
            proxy = cst_wrap(new_module)
            print("Code parsed successfully")
        except Exception as e:
            print_colored_traceback(e)
            print("Error parsing code")


    draw_window(export_code, name="Code Export")

    # draw_any(vis.root.synth_collection, is_window=False)
    # #
    # draw_any("hello there", is_window=True)

def export_code(test_param_2: int = 5):
    # print(f"hello {test_param_2}")
    global code_export_str
    code_export_str = proxy.node.code


######################## libCST START ##########################

@render_wrapper(wraps=render_func)
def cst_header(func, *args, **o_kwargs):
    def wrapper(next_kwargs=None, draw_state=None, **kwargs):
        annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
        if annotation is not None: return annotation

        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        next_kwargs['show_add_delete'] = False
        next_kwargs['show_bg'] = kwargs.get('show_bg', True)
        next_kwargs['is_tree'] = False

        next_kwargs['y_offset'] = Melty.collection_spacing
        if 'width' in kwargs:
            pass
        return core_header(**next_kwargs)

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header ")

    return wrapper

@cst_header(is_default_for=cst.SimpleStatementLine, header_same_line=True)
def draw_cst_single_line(input_value: cst.SimpleStatementLine, **kwargs):
    # An Assign has one or more targets, an AssignEqual token, and a value
    draw_any(input_value.body)


@cst_header(is_default_for=cst.Comment)
def draw_comment(input_value: cst.Comment, **kwargs):
    # imgui.text(f"# {input_value.value}")
    pass

@cst_header(is_default_for=cst.SimpleWhitespace, header_same_line=True, show_name=False)
def draw_cst_simple_whitespace(input_value: cst.SimpleWhitespace, **kwargs):
    # An Assign has one or more targets, an AssignEqual token, and a value
    # imgui.same_line()
    pass

    # imgui.button("SWS")
    # imgui.set_item_allow_overlap()
    # imgui.same_line()


# --- Assignments ---
def assign_name(input_value: cst.Assign):
    if len(input_value.targets) == 1:
        target = list(input_value.targets.values())[0]
        if isinstance(target, cst.AssignTarget):
            if isinstance(target.target, cst.Name):
                return target.target.value
            else:
                return str(target.target)
        else:
            return str(target)
    else:
        return "Multiple Targets"

@cst_header(is_default_for=cst.Assign, header_same_line=True, name_func=assign_name)
def draw_cst_assign(input_value: cst.Assign, **kwargs):
    # An Assign has one or more targets, an AssignEqual token, and a value
    # draw_collection(input_value.targets)
    imgui.text_colored("=", *(1, 1.1, 1, 0.5))
    imgui.same_line()
    draw_any(input_value.value)


# --- Names ---
@cst_header(is_default_for=cst.AssignTarget)
def draw_assign_target(input_value: cst.AssignTarget):
    draw_any(input_value.target)


# --- Names ---
@render_func(is_default_for=cst.Name)
def draw_cst_name(input_value: cst.Name):
    # imgui.text(f"{input_value.value}")
    pass


@cst_header(is_default_for=(cst.Expr, cst.Element), header_same_line=True, show_header=False,
            show_bg=False, show_name=False)
def draw_cst_expr(input_value):
    # Just render the wrapped expression
    draw_any(input_value.value)
# --- Function Calls ---

# Call Name
def call_name(call: cst.Call):
    if isinstance(call.func, cst.Name):
        return call.func.value
    else:
        return ""

@cst_header(is_default_for=cst.Call, header_same_line=True, name_func=call_name)
def draw_cst_call(input_value: cst.Call):
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text("(")
    imgui.same_line()
    draw_any(input_value.args, horizontal=True)
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text(")")


@with_header(is_default_for=cst.Module, show_add_delete=False)
def draw_cst_module(input_value: cst.Module):
    return draw_any(input_value.body, show_name=False)

@cst_header(is_default_for=cst.List, show_name=False, show_bg=False,
            header_same_line=True)
def draw_cst_list(input_value: cst.List):
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text("[")
    imgui.same_line()
    draw_any(input_value.elements, horizontal=True)
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text("]")

@render_func(is_default_for=CSTDictProxy, header_same_line=True,
             show_bg=False, indent_size=0)
def draw_cst_dict(input_value: CSTDictProxy, **kwargs):
    if len(input_value) > 0:
        # Show line numbers for dicts of simple statements
        if isinstance(list(input_value.values())[0], cst.SimpleStatementLine):
            kwargs['show_indices'] = True
    kwargs['show_name'] = False

    draw_collection(input_value, **kwargs)


# --- Parameters ---
@cst_header(is_default_for=cst.Parameters)
def draw_cst_parameters(input_value: cst.Parameters):
    first = True
    for param in input_value.params:
        if not first:
            imgui.same_line()
            imgui.text(",")
            imgui.same_line()
        draw_any(param)
        first = False


# --- Individual Parameter ---
@cst_header(is_default_for=cst.Param)
def draw_cst_param(input_value: cst.Param):
    draw_any(input_value.name)
    if input_value.default:
        imgui.same_line()
        imgui.text_colored("=", *(1,1,1,0.5))
        imgui.same_line()
        draw_any(input_value.default)


# @with_header(is_default_for=cst.Module, show_add_delete=False)
# def draw_cst_module(input_value: cst.Module):
#
#     return draw_any(input_value.body, show_name=False)

# --- Args ---
def arg_name(arg: cst.Arg):
    if arg.keyword:
        return arg.keyword.value
    elif isinstance(arg.value, cst.Name):
        return arg.value.value
    else:
        return None
@with_header_minimal(is_default_for=cst.Arg, show_bg=True, name_attrib="keyword",
                     name_func=arg_name, show_add_delete=False, header_same_line=True)
def draw_cst_arg(input_value: cst.Arg):
    draw_any(input_value.value)


# --- Individual Parameter ---
@render_func(is_default_for=cst.UnaryOperation, header_same_line=True, show_add_delete=False)
def draw_cst_int(input_value, width=None):
    int_str = input_value.value
    cast_str_to_int = int(int_str, 0)
    if cast_str_to_int == 25:
        pass

    changed, new_val = draw_int(cast_str_to_int, indent_size=0, show_name=False,
                                show_add_delete=False)
    if changed:
        input_value.value = str(new_val)


# --- Individual Parameter ---
@render_func(is_default_for=(cst.UnaryOperation, cst.Integer), header_same_line=True, show_add_delete=False)
def draw_cst_int(input_value, width=None):
    # Format the magnitude to match the original literal's base/prefix/casing.
    def _format_like(template: str, magnitude: int) -> str:
        if template.startswith(("0x", "0X")):
            s = hex(magnitude)  # '0x2a'
            return s if template.startswith("0x") else "0X" + s[2:].upper()
        elif template.startswith(("0o", "0O")):
            s = oct(magnitude)  # '0o52'
            return s if template.startswith("0o") else "0O" + s[2:]
        elif template.startswith(("0b", "0B")):
            s = bin(magnitude)  # '0b101010'
            return s if template.startswith("0b") else "0B" + s[2:]
        else:
            return str(magnitude)

    # Determine the current value and the template string to preserve formatting.
    if input_value.__class__ == cst.UnaryOperation:
        expr = input_value.expression  # expect an Integer
        op = input_value.operator
        inner_text = expr.value
        magnitude = int(inner_text, 0)
        sign = -1 if isinstance(op, cst.Minus) else 1
        current_val = sign * magnitude
        fmt_template = inner_text
        is_unary = True
    else:  # cst.Integer
        inner_text = input_value.value
        current_val = int(inner_text, 0)
        fmt_template = inner_text
        is_unary = False

    changed, new_val = draw_int(current_val, indent_size=0, show_name=False, show_add_delete=False)
    if not changed:
        return False, input_value

    if is_unary:
        if new_val < 0:
            # Keep UnaryOperation with Minus; update inner Integer magnitude.
            if not isinstance(input_value.operator, cst.Minus):
                input_value.operator = cst.Minus()
            input_value.expression.value = _format_like(fmt_template, -new_val)
            return True, input_value
        else:
            # Collapse to a plain Integer.
            replacement = cst.Integer(value=_format_like(fmt_template, new_val))
            return True, replacement
    else:
        if new_val < 0:
            # Expand to UnaryOperation(Minus(), Integer(abs)).
            replacement = cst.UnaryOperation(
                operator=cst.Minus(),
                expression=cst.Integer(value=_format_like(fmt_template, -new_val)),
            )
            return True, replacement
        else:
            # Stay as Integer; update literal text.
            input_value.value = _format_like(fmt_template, new_val)
            return True, input_value


# @render_func(is_default_for=cst.Integer, header_same_line=True, show_add_delete=False)
# def draw_cst_int(input_value, width=None):
#     int_str = input_value.value
#     cast_str_to_int = int(int_str, 0)
#     if cast_str_to_int == 25:
#         pass
#
#
#     changed, new_val = draw_int(cast_str_to_int, indent_size=0, show_name=False,
#                                 show_add_delete=False)
#     if changed:
#         input_value.value = str(new_val)

####################### libCST END ##################

def core_draw_window(input_value, name, unique, window_func,
                     window_stack, style_manager,
                     args, kwargs, indent_size=10, width=0, height=0, pos_x=None, pos_y=None,
                     decorations=True, focus=False, enable=True):
    tmp_undo_stack(unique)
    title = name or input_value.__class__.__name__
    padding_fudge = imgui.get_style().frame_padding.y + 2
    padding_x = imgui.get_style().frame_padding.x
    fudge_x = 3

    if focus:
        imgui.set_next_window_focus()

    if width is not None:
        if width > 0 and height > 0:
            imgui.set_next_window_size(width, height + padding_fudge * 4)

    if not decorations:
        if pos_x is not None and pos_y is not None:
            imgui.set_next_window_position(pos_x - indent_size - 2,
                                           pos_y - padding_fudge)
    else:
        if pos_x is not None and pos_y is not None:
            imgui.set_next_window_position(pos_x, pos_y)

    previous_tint = style_manager.get_tint()
    if hasattr(input_value, 'tint') and input_value.tint is not None:
        style_manager.set_imgui_tint(*input_value.tint)
    closable = True
    flags = 0
    if not decorations:
        closable = False
        flags = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
                 imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS |
                 imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS)
        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (fudge_x, padding_fudge))

    window_title = f"{title}##window_{str(unique)}"

    # Bring to front without collapse
    opened, _ = imgui.begin(f"{title}##window_{str(unique)}", closable, flags=flags)
    # if not decorations:
    #     imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    Melty.window_hovered = imgui.is_mouse_hovering_rect(*window_rect)

    Melty.window_stack.append((window_title, enable))

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)

    changed, new_value = window_func(*args, **kwargs)
    Melty.window_stack.pop()
    if not decorations:
        imgui.pop_style_var(1)
    draw_list.channels_merge()
    imgui.end()

    if hasattr(input_value, 'tint'):
        style_manager.set_imgui_tint(*previous_tint)

    redo_stack(unique)

    return changed, new_value


@render_func
def draw_window(input_value, window_stack=None, style_manager=None,
                indent_size=0, name="", unique=0, *args, **kwargs):
    kwargs['input_value'] = input_value
    kwargs['style_manager'] = style_manager
    kwargs['window_stack'] = window_stack
    kwargs['show_header'] = False
    kwargs['show_bg'] = False
    kwargs['name'] = name
    kwargs['indent_size'] = 0

    type_default_meta = Melty.type_defaults.get(input_value.__class__, None)
    if type_default_meta is not None and hasattr(type_default_meta, 'view_function'):
        window_func = type_default_meta.view_function
    else:
        window_func = draw_object
    return core_draw_window(window_func=window_func, input_value=input_value, window_stack=window_stack,
                     style_manager=style_manager, name=name, indent_size=indent_size,
                     unique=unique, args=args, kwargs=kwargs)

@render_wrapper(wraps=render_func)
def render_with_foo(func, *args, **kwargs):

    def wrapper(window_stack=None, *args, **kwargs):
        imgui.text("Some wrapper")
        return func(skfs=False, *args, **kwargs)

    return wrapper


@render_func
def draw_drop_target(input_value, draw_state, on_drag, do_flow, depth,
                     collection, key, melty, y_offset, enable_flow,
                     unique, tag, style_manager, global_style, offset=0):
    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Melty.is_window_enabled():
        return False, 0.0

    if melty.initial_drag_offset is None:
        return False, 0.0

    if key is None:
        pass
    # ----------------- top spacing -----------
    falloff = 25.0 # Higher is gentler
    if enable_flow:
        drop_gap = 6.0
    else:
        drop_gap = 0.0

    drag_delta_curve = 1.0 - max(0.0, min(1.0, 1.0 - abs(melty.drag_delta[1] / 15.0)))

    mouse_pos = imgui.get_mouse_pos()
    cursor_top = imgui.get_cursor_screen_pos()[1]
    static_offset = drop_gap
    distance_to_mouse = abs(mouse_pos[1] - cursor_y_screen -
                            melty.initial_drag_offset[1] - drop_gap + static_offset)
    bell_curve = max(0.0, min(1.0, 1.0 - (distance_to_mouse / falloff)))

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    mouse_over_window = imgui.is_mouse_hovering_rect(*window_rect)

    if melty.drag_in_progress:
        if melty.dragged_item._input_value == collection:
            return False, 0.0

    if melty.drag_in_progress and do_flow and not on_drag and mouse_over_window:
        flow_spacing = drop_gap * bell_curve * drag_delta_curve
    else:
        flow_spacing = 0.0
        drag_delta_curve = 1.0

    if tag == "top":
        Melty.flow_spacing += int(flow_spacing)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + flow_spacing)

    draw_state.flow_spacing = flow_spacing

    draw_list = imgui.get_window_draw_list()
    if Melty.inside_window():
        draw_list.channels_set_current(min(Melty.max_depth - 1, depth + 2))

    line_width = imgui.get_style().frame_padding.y * 2.0
    color = style_manager.make_color_rgb(*(1.0, 1.0, 1.0), factor=1.0,
                                         value=1.0, alpha=1.0, saturation_scale=0.3)

    # cursor_bottom = imgui.get_cursor_screen_pos()[1]
    # ------------------ end spacing -----------
    cursor_bottom = cursor_top + max(2.0, flow_spacing)

    if tag == "bottom":
        # span = cursor_bottom - cursor_top
        cursor_bottom += 0
        cursor_top += 0

    if melty.drag_in_progress and not on_drag and do_flow and mouse_over_window:
        if draw_state.height is not None:
            active_drop = (melty.drag_drop_target == draw_state.unique
                           and tag == melty.drag_drop_target_tag)

            if Melty.inside_window():
                draw_list.channels_set_current(min(depth + 1, Melty.max_depth + 1))

                if active_drop:
                    draw_list.channels_set_current(min(depth + 2, Melty.max_depth - 1))
                    cursor_bottom += ((1.0 - drag_delta_curve) * drop_gap)

            if distance_to_mouse < melty.nearest_drop_distance:
                melty.nearest_drop_distance = distance_to_mouse
                melty.nearest_drop_target = draw_state.unique
                melty.nearest_drop_target_tag = tag

                melty.drag_drop_action.target_unique = draw_state.unique
                melty.drag_drop_action.target_tag = tag
                melty.drag_drop_action.target_key = key
                melty.drag_drop_action.target_collection = collection
                melty.drag_drop_action.target_draw_state = draw_state

                if melty.drag_drop_action.target_key is None:
                    pass

            height_as_factor = 800.0
            drag_distance = sqrt(melty.drag_delta[0] ** 2 + melty.drag_delta[1] ** 2)
            initial_fade_offset = max(min(1.0, melty.total_drag_distance / 10.0), 0.0)
            if melty.total_drag_frames < 1:
                initial_fade_offset = 0.0
            opacity = max(0.0, min(1.0, 1.0 - (distance_to_mouse / (height_as_factor * 0.3))))
            opacity *= initial_fade_offset
            # opacity = 1.0 if active_drop else opacity

            bg_tint = Melty.get_bg_color(-1)
            bg_style = global_style.get_global_constant("bg_style", folder="bg_styles")

            color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                        value=1.3,
                                                        alpha=opacity, saturation=0.8)

            # color = style_manager.make_color_rgb(*bg_tint, factor=0.0,
            #                                      value=1.0, alpha=opacity, saturation_scale=1.0)
            inactive_color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                              value=0.7,
                                                              alpha=opacity, saturation=0.8)
            if draw_state.width == None:
                draw_state.width = 1
            if draw_state.left == None:
                draw_state.left = 1

            padding = imgui.get_style().frame_padding.x
            color = color if active_drop else inactive_color
            draw_list.add_rect_filled(draw_state.left + offset + padding, cursor_top - 1,
                                    draw_state.left + draw_state.width - padding * 2.0,
                                    max(cursor_top, cursor_bottom - 1),
                                    col=imgui.get_color_u32_rgba(*color), rounding=4.0)
            #
            # draw_list.add_line(draw_state.left, draw_state.top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.top - 2 - offset,
            #                    col=imgui.get_color_u32_rgba(*color), thickness=3)

    return False, flow_spacing

@render_func
def draw_header_end(global_style, unique, style_manager, show_search,
                    on_search, draw_state, collection, key,
                    melty, show_add_delete=True, parent_show_add_delete=False):
    imgui.push_id(f"header_end_{unique}")
    bg_style = {
        "value": 0.01,
        "saturation": 1.0,
        "alpha": 1.0,
        'max_value': 1.0
    }
    bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
    search_color = (style_manager.
                    make_color_style_value(input=bg_style, saturation=0.7,
                                           value=1.0))

    # if draw_state._left_rel is not None:
    #     cursor_pos = imgui.get_cursor_pos()
    #     # imgui.set_cursor_pos((draw_state._left_rel, cursor_pos[1]))
    #     # imgui.dummy(draw_state.width - draw_state._end_header_size[0],0)
    #     imgui.same_line()
    start_x, end_x = 0, 0

    imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
    imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))

    imgui.begin_group()
    imgui.pop_style_var(2)

    if parent_show_add_delete:
        imgui.same_line()
        start_x = imgui.get_cursor_screen_pos()[0]
        imgui.push_style_color(imgui.COLOR_TEXT, *search_color)
        imgui.push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        if imgui.button(f"\uf1f8##del"):
            melty.to_delete(key, collection)
            print("No selected_views or remove_view method")
        same_line(spacing=0.0)
        padding = imgui.get_style().frame_padding.x
        end_x = imgui.get_cursor_screen_pos()[0] + padding
        imgui.pop_style_color(2)

    # if show_search or draw_state.search_active:
    #     imgui.same_line()
    #     imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + 2)
    #
    #     icon = "\uf002"
    #     imgui.text_colored(icon, *search_color)
    #     imgui.same_line()
    #     search_width = 150.0
    #     imgui.set_next_item_width(search_width)
    #     search_changed, new_search = imgui.input_text(f"##search{unique}", draw_state.search_text)
    #
    #     if search_changed:
    #         draw_state.search_text = new_search
    #         imgui.set_keyboard_focus_here(-1)
    #         request_render()
    #
    #     if not draw_state.search_active:
    #         draw_state.search_text = ""
    #
    #     if on_search:
    #         draw_state.search_active = True
    #         imgui.set_keyboard_focus_here(-1)
    #         request_render()
    #
    #     draw_state.search_active = imgui.is_item_focused()

    imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
    imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    imgui.end_group()
    imgui.pop_id()
    imgui.pop_style_var(2)
    draw_state._end_header_size = (end_x - start_x, imgui.get_item_rect_size()[1])


def core_header(func, outer_func, input_value=None, collection=None, key=None, indent_size=10, depth=0, draw_state=None,
                window_stack=None, is_tree=True, is_window=False, spacing=Melty.spacing, padding=Melty.padding,
                show_header=True, show_bg=True, unique=0, name="", style_manager=None, global_style=None, parent_show_add_delete=True,
                selected_views=None, on_drag=False, on_drag_up=False, do_flow=True, melty=None, enable_flow=True, header_same_line=False,
                on_hover=False, next_kwargs=None, meta=None, on_same_line=False, y_offset=0, width=None, min_width=1, **kwargs):

        if window_stack is None or len(window_stack) == 0:
            pass

        return_value = None
        changed = False

        if meta is not None:
            meta.tmp_draw_state = draw_state

        inside_window = len(Melty.window_stack) > 0
        draw_list = imgui.get_window_draw_list()

        if inside_window and show_bg:
            draw_list.channels_set_current(min(Melty.max_depth - 1, depth))

        if not show_bg:
            y_offset = 0

        Melty.indent(indent_size)

        # ----------------- top spacing -----------
        _, flow_spacing = draw_drop_target(do_flow=True, enable_flow=enable_flow,
                         collection=collection, key=key, on_drag=False,
                         draw_state=draw_state, tag="top")
        # ------------------ end spacing -----------
        if width is None:
            if draw_state.width is not None and draw_state.width > 0:
                width = draw_state.width
            else:
                width = min_width
                draw_state.width = min_width
        else:
            draw_state.width = min(width, min_width)

        content_region = imgui.get_content_region_available()[0]
        width = min(width, content_region)

        draw_state._left_rel = imgui.get_cursor_pos()[0]
        # if show_bg:
        #     draw_state._left_rel += indent_size

        start_x_pos = imgui.get_cursor_screen_pos()[0]
        start_y_pos = imgui.get_cursor_screen_pos()[1]
        cutoff = 50
        y_margin = y_offset / 2.0
        imgui.dummy(0, y_margin)

        if on_drag:
            imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() - Melty.flow_spacing)
            Melty.flow_spacing = 0.0

        bg_tint = None
        bg_selected = False
        header_on_same_line = False

        if show_header:
            next_kwargs['highlight'] = on_hover
            if on_drag:
                next_kwargs['opacity'] = 0.0
            next_kwargs.pop('spacing', None)
            next_kwargs.pop('padding', None)

            # --------------------- HEADER -----------------
            if not on_drag:
                changed, return_value = draw_header(spacing=(spacing[0], Melty.spacing[1]),
                                              padding=(padding[0], Melty.padding[1] + 1),
                                              **next_kwargs)

            if on_drag:
                next_kwargs['opacity'] = 1.0
                next_kwargs['on_drag'] = False
                next_kwargs['do_flow'] = False
                mouse_pos = imgui.get_mouse_pos()
                start_pos_x = draw_state.mouse_btn_state[0].initial_screen_pos[0]
                start_pos_y = draw_state.mouse_btn_state[0].initial_screen_pos[1]
                mouse_down_x = draw_state.mouse_btn_state[0].mouse_down_pos[0]
                mouse_down_y = draw_state.mouse_btn_state[0].mouse_down_pos[1]
                drag_delta = (mouse_pos[0] - mouse_down_x, mouse_pos[1] - mouse_down_y)

                pos_x = start_pos_x + drag_delta[0]
                pos_y = start_pos_y + drag_delta[1]

                core_draw_window(window_func=outer_func, input_value=input_value,
                                 window_stack=window_stack,
                                 pos_x=pos_x, pos_y=pos_y, height=draw_state.height,
                                 width=imgui.get_content_region_available()[0], indent_size=indent_size,
                                 style_manager=style_manager, name=name, decorations=False,
                                 focus=True, unique=unique, enable=False, args=(), kwargs=next_kwargs)
            else:
                next_kwargs['do_flow'] = True

            rect_size = imgui.get_item_rect_size()
            header_width = rect_size[0]

            # Auto indent is decided here
            if not on_drag:
                if header_same_line:
                    same_line(spacing=0.0)

                else:
                    space_available = width - header_width
                    if (not isinstance(input_value, (dict, list, tuple)) and not hasattr(input_value, '__dict__')):
                        if draw_state.height is not None:

                            if draw_state.expanded_height is None:
                                draw_state.expanded_height = draw_state.height

                            height = max(draw_state.height, draw_state.expanded_height)

                            if height is None or height < 79 or on_same_line:
                                if (space_available > cutoff and draw_state.expanded) or on_same_line:
                                    header_same_line = True
                                    same_line(spacing=0.0)

                if not header_same_line and not on_drag:
                    # ----------------- end header for dict ---------------
                    # This is the version with auto indent, probably a dict header
                    draw_header_end(**next_kwargs)


        if show_bg:
            style_manager.get_tint()
            Melty.bg_stack.append(style_manager.get_tint())


        if not is_tree or draw_state.expanded:
            if not on_drag:
                next_kwargs.pop('spacing', None)
                next_kwargs.pop('padding', None)

                end_header_with = draw_state._end_header_size[0]

                current_x = imgui.get_cursor_screen_pos()[0]
                space_used = max(0, current_x - start_x_pos)
                space_available = width - space_used

                padding_x = imgui.get_style().frame_padding.x
                request_width = space_available - end_header_with - padding_x - 7
                min_width = min(imgui.get_content_region_available()[0] - end_header_with,
                                min_width - space_used)
                min_width = max(min_width, request_width)
                imgui.set_next_item_width(min_width)

                ######################## MAIN FUNC CALL ########################
                func_changed, func_return_val = func(spacing=(spacing[0], Melty.spacing[1]),
                                    padding=(padding[0], Melty.padding[1]),
                                    **next_kwargs)
                ############### END MAIN FUNC CALL #############################
                if func_changed:
                    return_value = func_return_val
                changed |= func_changed
                #
                if header_same_line and show_header:
                    # ----------------- end header single lines---------------
                    # This is the version for single lines probably
                    draw_header_end(**next_kwargs)
        if not on_drag:
            imgui.dummy(0, 1)

        if on_drag:
            same_line(spacing=0.0)
            imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
            imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
            imgui.dummy(draw_state.width - indent_size,
                        draw_state.height)
            imgui.pop_style_var(2)

        if show_bg:
            style_manager.get_tint()
            Melty.bg_stack.pop()

        same_line(spacing=0)
        imgui.dummy(0, y_margin)

        end_y_pos = imgui.get_cursor_screen_pos()[1]

        padding_x = imgui.get_style().frame_padding.x * 2.0
        background_width = width - indent_size
        background_height = draw_state.height or 0

        draw_state.top = start_y_pos
        draw_state.left = start_x_pos

        if inside_window:
            if show_bg:
                draw_list.channels_set_current(max(0, min(Melty.max_depth - 2, depth - 1)))
                if not on_drag:
                    draw_bg(bypass=True, left=start_x_pos, top=y_margin + start_y_pos - 1,
                            width=background_width, height=background_height - Melty.spacing[1] / 2.0 - y_offset,
                            tint=bg_tint, depth=depth, selected=bg_selected, global_style=global_style,
                            style_manager=style_manager)
                else:
                    shadow_color = (style_manager.make_color_rgb(*(0.0, 0.0, 0.0), factor=1.0,
                                                                 value=0.00, alpha=0.12, saturation_scale=0.3))
                    outline_shadow = (style_manager.make_color_rgb(*(0.0, 0.0, 0.0), factor=1.0,
                                                                   value=0.0, alpha=0.3, saturation_scale=0.3))
                    draw_bg(bypass=True, left=start_x_pos + 2, top=y_margin + start_y_pos, global_style=global_style,
                            style_manager=style_manager, depth=depth,
                            width=background_width - 4, height=background_height - Melty.spacing[1] / 2.0 - 1 - y_offset,
                            tint=shadow_color, outline_tint=outline_shadow, selected=bg_selected)

                if draw_state.expanded or not is_tree:
                    draw_state.expanded_height = end_y_pos - start_y_pos

            if draw_state.height is not None:
                imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                imgui.pop_style_var(2)
        Melty.unindent(indent_size)

        if inside_window:
            draw_list.channels_set_current(Melty.max_depth - 1)

        if on_drag_up:
            melty.drag_in_progress = False
            new_action = copy(melty.drag_drop_action)
            new_action.operation = OperationType.MOVE
            new_action.source_key = key
            new_action.source_unique = unique
            new_action.source_collection = collection
            new_action.source_draw_state = draw_state

            return False, new_action

        return changed, return_value

def seperator(height):
    imgui.dummy(0, height / 2)
    imgui.separator()
    imgui.dummy(0, height / 2)


@with_header(is_default_for=(MutableMapping))
def draw_collection(input_value, draw_state, depth, style_manager,
                    meta, suffix, melty, show_search=True, on_collapse=False, on_drag_up=False, y_offset=0,
                    on_expand=False, width=None, indent_size=10, global_style=None, global_toggles=None, show_add_delete=True,
                    show_instance_vars=True, unique=0, horizontal=False, show_indices=False, **kwargs):
    """
    Universal collection renderer
    """
    changed = False
    base_suffix = suffix  # keep original arg intact
    # ----- SIMPLE NORMALIZATION (lowercase; remove spaces, '_' and '-') -----
    _TRANS = str.maketrans("", "", " _-")
    def norm_string(s) -> str:
        if s is None:
            return ""
        try:
            s = str(s)
        except Exception:
            s = ""
        return s.lower().translate(_TRANS)

    if hasattr(input_value, 'children') and isinstance(input_value.children, (list, dict)):
        input_value = input_value.children

    search_token = norm_string(draw_state.search_text) if show_search else ""

    # --- configure per collection type ---
    ordered_driver = input_value
    if isinstance(input_value, (dict, list, tuple, set, MutableMapping)):
        use_tint = True
        use_child_meta = True
        apply_change = True
        parent_type = input_value.__class__
        if isinstance(input_value, (dict, MutableMapping)):
            keys = input_value.keys()
            collection = input_value
        else:
            keys = range(len(input_value))
            collection = list(input_value)

    elif hasattr(input_value, "__dict__") and depth < Melty.max_depth:
        if hasattr(type(input_value), "__field_defaults__") and hasattr(input_value, 'to_dict'):
            type(input_value).__field_defaults__.update(input_value.__dict__)
            keys = type(input_value).__field_defaults__.keys()
        else:
            keys = input_value.__dict__.keys()
        collection = input_value.__dict__
        use_tint = False
        use_child_meta = True
        apply_change = True
        parent_type = input_value.__class__
    else:

        return False, input_value

    # --- unified loop ---
    drew_any = False
    collection_spacing = 0
    all_meta = []
    for idx, key in enumerate(keys):
        if isinstance(collection, dict) and key not in collection:
            continue
        item = collection[key]

        # visual separator (object extras)
        if key is None and item is None:
            seperator(Melty.spacing[1])
            continue

        if hasattr(type(input_value), "__excluded_attrs__"):
            if not global_toggles.force_show_excluded:
                if str(key) in type(input_value).__excluded_attrs__:
                    continue
        display_name = None

        # apply global filter for all types
        if isinstance(key, (int, float, Enum, NoneType)):
            key_str = f"{input_value.__class__.__name__}"
        else:
            key_str = str(key)

        if ((key_str.startswith("__") and key_str.endswith("__")) or
                key_str.endswith("meta") or key_str.startswith("_")):
            continue

        # ----- SEARCH CHECK (keys + item.name if present) -----
        if search_token:
            name_field = getattr(item, "name", None) or getattr(item, "__name__", "")
            if ((search_token not in norm_string(key_str)) and
                    (search_token not in norm_string(name_field))):
                continue

        # meta selection
        from src.lsd.gl_gui.view.core_views.core_meta import Meta
        if use_child_meta and hasattr(Meta, 'get_child_meta'):
            item_meta = Meta.get_child_meta(parent_type, field_name=key, value=item)
        else:
            item_meta = meta

        item_meta.collection_type = meta.field_type

        # view function & identifier
        view_fn = getattr(item_meta, "view_function", draw_collection)
        if view_fn is None:
            view_fn = draw_collection
        item_suffix = f"{base_suffix}_{key_str}"
        obj_unique = ui_id(datatype=item.__class__, suffix=item_suffix)

        trigger_collapse = False
        if isinstance(input_value, (dict, MutableMapping)) and on_collapse:
            trigger_collapse = True

        trigger_expand = False
        if isinstance(input_value, (dict, MutableMapping)) and on_expand:
            trigger_expand = True

        prev_tint = None
        try:
            show_bg = getattr(item_meta, "show_bg", False)
            if hasattr(item, "tint") or show_bg:
                prev_tint = style_manager.get_tint()
                style_manager.set_imgui_tint(*item.tint)

            y_offset = Melty.collection_spacing
            all_meta.append(item_meta)
            if show_indices or isinstance(collection, (list, tuple, set)):
                display_name = f"{str(idx)}"

            if horizontal:
                if item_meta is not None and hasattr(item_meta, 'tmp_draw_state'):
                    imgui.same_line()
                    if item_meta.tmp_draw_state.width is not None:
                        space_left = imgui.get_content_region_available()[0] - item_meta.tmp_draw_state.width
                        if space_left < 0:
                            imgui.new_line()

            item_changed, out_val = draw_any(item, indent_size=10, key=key, meta=item_meta, trigger_collapse=trigger_collapse,
                             trigger_expand=trigger_expand, y_offset=y_offset, on_collapse=on_collapse, on_expand=on_expand,
                             collection=ordered_driver, suffix=obj_unique, name=key_str, display_name=display_name,
                                             parent_show_add_delete=show_add_delete,
                                             show_add_delete=show_add_delete)

            if isinstance(out_val, CollectionAction):
                # perform the move - this should mutate the plain dicts you attached
                result = melty.to_apply(out_val)
                item_changed, out_val = False, None

            if item_changed and apply_change and key is not None:
                if isinstance(input_value, (dict, MutableMapping)):
                    input_value[key] = out_val
                elif isinstance(input_value, list):
                    input_value[key] = out_val
                elif isinstance(input_value, tuple):
                    temp = list(input_value)
                    temp[key] = out_val
                    input_value = parent_type(temp)
                else:
                    setattr(input_value, key_str, out_val)

            changed |= item_changed
            drew_any = True
        except Exception as e:
            print(f"Error rendering field '{key_str}' of {type(input_value).__name__}: {e}")
            print_colored_traceback()
        finally:
            if prev_tint is not None:
                style_manager.set_imgui_tint(*prev_tint)

    # ----------------- flow spacing -----------
    last_key = list(keys)[-1] if len(keys) > 0 else None

    if not drew_any:
        last_key = None

    last_meta = all_meta[-1] if len(all_meta) > 0 else None
    if hasattr(last_meta, 'tmp_draw_state'):
        last_draw_state = last_meta.tmp_draw_state if last_meta is not None else draw_state

        last_item = collection[last_key] if (isinstance(collection, dict) and last_key in collection) else None
        _, flow_spacing = draw_drop_target(do_flow=True, enable_flow=True, melty=melty, offset=indent_size,
                                           collection=ordered_driver, key=last_key, on_drag=False,
                                           draw_state=last_draw_state, tag="bottom")
    # ------------------ end spacing -----------

    if drew_any and len(keys) > 1:
        imgui.dummy(0, Melty.end_collection_spacing)

    return changed, input_value


@render_func
def draw_bg(left=0, top=0, width=20, height=20, depth=0,
            global_style=None, outline=True,
            style_manager=None, tint=None, outline_tint=None, selected=False,
            hovered=False):
    # Render background
    def current_indent_px():
        return Melty.current_indent

    # float_style = global_styles.get_global_constant(constant_name="bg_style", default_type=Style, folder="bg_styles")
    # float_style.apply(global_styles=global_styles, style_manager=style_manager, depth=depth)
    #
    rounding = global_style.get_global_constant("rounding", default=0.0, folder="bg_styles")

    # Draw rect
    if left == 0:
        left = imgui.get_cursor_screen_pos()[0]

    if top == 0:
        top = imgui.get_cursor_screen_pos()[1]
    right =  left + width + rounding
    bottom =  top + height + rounding

    rect = (left, top, right, bottom)
    rect_outline = (left - 1, top - 1, right + 1, bottom + 1)
    # rounding
    rounding = min(current_indent_px(), rounding)

    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))
    bg_style = {
        "value": 0.01,
        "saturation": 1.0,
        "alpha": 1.0,
        'max_value': 1.0
    }
    hovered_offset = 0.0
    if selected:
        hovered_offset = 0.1
    elif hovered:
        hovered_offset = 0.3

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
    outline_saturation = global_style.get_global_constant("outline_saturation", default=0.5, folder="bg_styles")

    outline_offset = global_style.get_global_constant("outline_offset", default=0.0, folder="bg_styles") - 0.1
    outline_factor = global_style.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    bleed_factor = 0.05
    bg_bleed = Melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.7,
                                                alpha=1.0, saturation=1.8)

    outline_color = (style_manager.
                     make_color_style_value(input=bg_style, saturation=outline_saturation,
                                                  value=max(0, dynamic_value * outline_factor + outline_offset)))
    outline_color = mix_colors(outline_color, bg_bleed, 0.01)
    # if tint is not None:
    #     outline_color = imgui.get_color_u32_rgba(*tint)

    if outline:
        outline_color = imgui.get_color_u32_rgba(*outline_color, 1.0)

        if outline_tint is not None:
            outline_color = imgui.get_color_u32_rgba(*outline_tint)
        imgui.get_window_draw_list().add_rect(*rect_outline, col=outline_color, rounding=rounding, thickness=2.0)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))


    bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)

    imgui_bg_color = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)

    if tint is not None:
        imgui_bg_color = imgui.get_color_u32_rgba(*tint)

    imgui.get_window_draw_list().add_rect_filled(*rect, col=imgui_bg_color, rounding=rounding)


@render_func
def draw_header(input_value=None, name="", display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, is_window=False, on_click=False, show_tint=True,
                on_hover=False, highlight=False, opacity=1.0, show_add_delete=True,
                on_right_click=False, show_bg=False, selected_views=None, is_hovered=False,
                on_drag=False, on_drag_released=False, on_action=None, style_manager=None,
                global_style=None, global_toggles=None, width=None, depth=0, shift_click=False):

    if display_name is not None:
        name = display_name

    on_change = False
    return_val = on_action
    imgui.push_style_var(imgui.STYLE_ALPHA, opacity)
    value_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles")
    value_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles")

    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles")
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") + 0.2
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))
    bg_style = global_style.get_global_constant("bg_style", default=None, folder="bg_styles")
    saturation = -0.5

    saturation = bg_style['saturation'] + saturation
    name_color = (style_manager.
                  make_color_style_value(input=bg_style, saturation=saturation,
                                         value=max(0, dynamic_value * value_factor + value_offset)))
    hover_color = (style_manager.
                  make_color_style_value(input=bg_style, saturation=1.2, alpha=1.0,
                                         value=0.67))

    outline_color = (style_manager.
                   make_color_style_value(input=bg_style, saturation=0.8, alpha=1.0,
                                          value=0.9))
    if is_tree:
        if trigger_collapse:
            draw_state.expanded = False
        if trigger_expand:
            draw_state.expanded = True

        imgui.push_style_color(imgui.COLOR_TEXT, *outline_color)
        imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (2,4))
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0,3))
        draw_state.expanded = tree("##tree", draw_state.expanded, width=50)
        imgui.pop_style_var(2)
        imgui.pop_style_color(1)

        same_line()
    cursor_start = imgui.get_cursor_pos()

    if show_name and name != "" and name is not None and name != "None":
        if isinstance(input_value, (dict, MutableMapping)):
            folder_icon = "\uf07b"
            imgui.text_colored(folder_icon, *name_color)
            same_line(spacing=0.0)

        imgui.align_text_to_frame_padding()
        padding = imgui.get_style().frame_padding.x
        text_width = imgui.calc_text_size(name)[0]
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        imgui.push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        imgui.push_style_color(imgui.COLOR_TEXT, *name_color)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *hover_color)
        imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE, *hover_color)
        imgui.push_style_var(imgui.STYLE_FRAME_ROUNDING, 2.0)

        if on_drag:
            imgui.push_style_var(imgui.STYLE_FRAME_BORDERSIZE, 2.0)
            imgui.push_style_color(imgui.COLOR_BORDER, *outline_color)

        min_name_width = 30.0
        name_width = max(text_width + padding * 2, min_name_width)
        if not draw_state._name_edit:
            imgui.button(f"{name}", width=name_width)
            if imgui.is_mouse_double_clicked(0) and imgui.is_item_hovered():
                draw_state._name_edit = True
                imgui.set_keyboard_focus_here(0)

            imgui.pop_style_color(4)
            imgui.pop_style_var(2)
        else:
            imgui.set_next_item_width(name_width)
            # Selected text on focus
            flags = imgui.INPUT_TEXT_ENTER_RETURNS_TRUE | imgui.INPUT_TEXT_AUTO_SELECT_ALL
            changed, new_name = imgui.input_text(f"##edit{name}_{unique}", name,
                                                 flags=flags)
            imgui.pop_style_color(4)
            imgui.pop_style_var(2)
            if changed:
                draw_state._name_edit = False

            if imgui.is_key_pressed(imgui.KEY_ESCAPE):
                draw_state._name_edit = False

            if not imgui.is_item_active():
                draw_state._name_edit = False
        if on_drag:
            imgui.pop_style_color(1)
            imgui.pop_style_var(1)

        same_line()

    name_end = imgui.get_cursor_pos()

    if show_type:
        imgui.text_colored(f"({input_value.__class__.__name__})", *(0.8, 0.0, 0.5, 1.0))
        same_line()

    if show_unique:
        imgui.text_colored(f"({str(unique)[-3:]})", *(0.4, 0.6, 0.9, 1.0))
        same_line()
    if show_name and name != "":
        same_line()
        imgui.set_item_allow_overlap()

    if show_tint and hasattr(input_value, "tint"):
        tint_changed, tint_value = draw_tuple(input_value.tint, min_width=0, show_header=False)
        if tint_changed:
            input_value.tint = tint_value
        same_line()

    if show_add_delete and isinstance(input_value, (list, dict)) or hasattr(input_value, "__dict__"):
        bg_style = global_style.get_global_constant("bg_style", default=None, folder="bg_styles")
        text_color = (style_manager.
                      make_color_style_value(input=bg_style, saturation=0.2,
                                             value=1.0))
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() - 2)
        if show_add_delete:
            if imgui.button(f"\uf067##add", width=20):
                # Use str as default hinted type
                hinted_type = NoneType
                if meta.field_type is not None and hasattr(meta.field_type, "__args__"):
                    if len(meta.field_type.__args__) == 2:
                        hinted_type = meta.field_type.__args__[1]
                add_to_collection(input_value, hinted_type())
                on_change = True
                return_val = input_value
            same_line(spacing=0.0)

    same_line(spacing=0.0)

    do_profile = global_toggles.profiler == ProfileMode.ON
    if do_profile:
        profile_time = draw_state.render_time
        render_profiler_time(input_value=profile_time, brief=True,
                             style_manager=style_manager, global_style=global_style)
    imgui.pop_style_var(1)

    return on_change, return_val

def render_profiler_time(input_value=None, brief=False, style_manager=None,
                         global_style=None):
    """
    Renders the time taken for a specific operation in the profiler.
    """
    in_ms = input_value * 1000.0
    if brief:
        if in_ms >= 0.99:
            formatted_value = f"{(in_ms):.1f}ms"
        else:
            formatted_value = f"{(in_ms):.2f}ms"
        if formatted_value.startswith("0."):
            formatted_value = formatted_value[1:]
    else:
        formatted_value = f"{in_ms:.2f} ms"
    golden_yellow = (2.0, 0.5, 0)
    dynamic_saturation_factor = global_style.profiler["object_attr"][
        "dynamic_saturation_factor"]
    dynamic_saturation_offset = global_style.profiler["object_attr"][
        "dynamic_saturation_offset"]
    saturation = global_style.profiler["object_attr"]["saturation"]
    value = global_style.profiler["object_attr"]["value"]
    dynamic_sat = (float(in_ms + dynamic_saturation_offset) * dynamic_saturation_factor)
    text_tint = style_manager.make_color_rgb(*golden_yellow, factor=1.0 - dynamic_sat,
                                                       value=min(1.0, max(0, value + dynamic_sat * 0.5)),
                                                       alpha=1.0,
                                                       saturation_scale=max(0, saturation - dynamic_sat))[:3]
    imgui.text_colored(f"{formatted_value}", *text_tint)
    return False, input_value



@render_func
@listens_for(Hotkey("on_search", glfw.KEY_F, KeyMod.CTRL))
@listens_for(Hotkey("on_collapse", glfw.KEY_MINUS, KeyMod.CTRL, scoped=False))
@listens_for(Hotkey("on_expand", glfw.KEY_EQUAL, KeyMod.CTRL, scoped=False))
def draw_object(input_value=None, draw_state=None, meta=None, name="", style_manager=None,
                depth=0, unique=0, suffix="", collection=None, key=None, *args, **kwargs):
    # if is_tree and not draw_state.expanded:
    #     return False, None
    is_collection = isinstance(input_value, (dict, list, tuple, set)) or (
            hasattr(input_value, "__dict__") and depth < Melty.max_depth)
    if is_collection:

        # Handle collections
        changed, new_value = draw_collection(input_value=input_value, collection=collection,
                                             name=name, key=key, suffix=suffix, **kwargs)
    else:
        return_value = None
        push_id(unique)
        try:
            imgui.text(f"Render object {name}")
        except Exception as e:
            print_colored_traceback()
        finally:
            pop_id()
            if return_value is None:
                changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                changed, new_value = return_value
            else:
                imgui.text("Unsupported return from render_func")
                changed, new_value = False, None
    return changed, new_value


@render_func
def draw_any(input_value, indent_size=0, *args, **kwargs):
    if input_value.__class__.__name__ == cst.Integer.__name__:
        pass

    kwargs['indent_size'] = indent_size

    # meta info
    meta = kwargs.get("meta", None)
    if meta is None:
        from src.lsd.gl_gui.view.core_views.core_meta import Meta
        if hasattr(Meta, 'get_child_meta'):
            meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)

    if meta.view_function is None or 'draw_any' in meta.view_function.__name__ :
        meta.view_function = draw_collection

    if kwargs['global_toggles'].force_show_view_fn:
        imgui.text_colored(f"[{meta.view_function.__name__}]", 0.8, 0.5, 0.9)
    if kwargs['global_toggles'].force_show_unique and hasattr(meta, 'unique'):
        imgui.text_colored(f"[{meta.unique}]", 0.8, 0.5, 0.9)

    if kwargs['global_toggles'].force_show_datatype:
        start_pos = imgui.get_cursor_screen_pos()


        datatype_text = f"{input_value.__class__.__name__} ({type(input_value).__name__}) {kwargs.get('name', '')}"

        # Draw bg rect
        rect = (start_pos[0] - 4, start_pos[1] - 2,
                start_pos[0] + imgui.calc_text_size(datatype_text)[0] + 4,
                start_pos[1] + imgui.get_text_line_height_with_spacing() + 2)

        if imgui.is_mouse_hovering_rect(*rect[0:2], *rect[2:4]):
            imgui.get_foreground_draw_list().add_rect_filled(*rect, col=imgui.get_color_u32_rgba(0.1, 0.1, 0.1, 0.7), rounding=4.0)
            imgui.get_foreground_draw_list().add_rect(*rect, col=imgui.get_color_u32_rgba(0.8, 0.5, 0.9, 0.8), rounding=4.0, thickness=1.0)

            # Draw text
            imgui.get_foreground_draw_list().add_text(start_pos[0], start_pos[1],
                                                   imgui.get_color_u32_rgba(0.8, 0.5, 0.9, 1.0), datatype_text)

        else:
            imgui.get_window_draw_list().add_rect_filled(*rect, col=imgui.get_color_u32_rgba(0.1, 0.1, 0.1, 0.7),
                                                          rounding=4.0)
            imgui.get_window_draw_list().add_rect(*rect, col=imgui.get_color_u32_rgba(0.8, 0.5, 0.9, 0.8),
                                                   rounding=4.0, thickness=1.0)

            # Draw text
            imgui.get_window_draw_list().add_text(start_pos[0], start_pos[1],
                                                   imgui.get_color_u32_rgba(0.8, 0.5, 0.9, 1.0), datatype_text)

        # No padding
        push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
        push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        cursor_pos = imgui.get_cursor_pos()

        # Reset cursor to avoid spacing issues
        imgui.set_cursor_pos(cursor_pos)

        pop_style_var(2)

    return meta.view_function(input_value, *args, **kwargs)


@with_header_minimal(is_default_for=(NoneType))
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text("None")

    return False, None


@with_header_minimal(is_default_for=(bool), header_same_line=True)
def draw_bool(input_value: bool):
    changed, is_checked = imgui.checkbox("##bool", input_value)
    if changed:
        return True, is_checked

    return False, None


@with_header_minimal(is_default_for=(str))
def draw_str(input_value: str):

    if "1340" in input_value:
        pass
    line_count = input_value.count('\n') + 1
    line_height = imgui.get_text_line_height_with_spacing()
    height = max(0, min(200, line_count * line_height + 8))
    changed, value = imgui.input_text_multiline("##str", input_value, height=height)
    if changed:
        return True, value

    return changed, value


@with_header_minimal(is_default_for=('tint'))
def draw_tuple(input_value: tuple, is_tree=False, draw_state=None, show_bg=False):
    if len(input_value) > 0 and isinstance(input_value[0], (float, int)):
        if len(input_value) == 4:
            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit4(
                f"##_color",
                color_list[0], color_list[1], color_list[2], color_list[3],
                flags=color_flags)
            if changed:
                input_value = (color[0], color[1], color[2], color[3])
        elif len(input_value) == 3:
            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL |
                           imgui.COLOR_EDIT_NO_ALPHA | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit3(
                f"##_color",
                color_list[0], color_list[1], color_list[2],
                flags=color_flags)
            if changed:
                input_value = (color[0], color[1], color[2])
        else:
            str_value = ", ".join([str(v) for v in input_value])
            changed, input_str = imgui.input_text("##tuple", str_value)
            if changed:
                try:
                    new_tuple = eval(f"({input_str},)")
                    if isinstance(new_tuple, tuple):
                        input_value = new_tuple
                except Exception as e:
                    print(f"Error parsing tuple: {e}")
                    pass
    else:
        changed, input_value = draw_collection(input_value=input_value)

    return changed, input_value

@with_header_minimal(is_default_for=float)
def draw_float(input_value:float, min_value=-100.0, max_value=100.0, speed=0.01):
    changed, value = imgui.drag_float("##float", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value


@with_header_minimal(is_default_for=(Parameter), wraps=render_func)
def draw_parameter(input_value):

    parameter_default = input_value.default
    if parameter_default is inspect.Parameter.empty:
        imgui.same_line()
        imgui.text("<No Default>")
    else:
        return draw_any(parameter_default, show_name=False, show_add_delete=False)


@with_header(is_default_for=(types.MappingProxyType), show_add_delete=False, wraps=render_func)
def draw_mapping_proxy(input_value):
    # To list first, then back to mapping proxy
    dict_values = dict(input_value)
    changed, new_dict = draw_collection(dict_values, show_bg=False, indent_size=0, show_header=False, show_add_delete=False)
    if changed:
        return True, types.MappingProxyType(new_dict)

    return changed, input_value


@with_header_minimal(is_default_for=(types.FunctionType), wraps=render_func)
def draw_function(input_value, unique):
    signature = inspect.signature(input_value)
    params = signature.parameters
    changed, new_val = draw_any(params, name="Parameters", show_add_delete=False)
    if changed:
        set_fn_defaults(input_value, new_val)

    imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))
    imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (8, 6))
    imgui.push_style_var(imgui.STYLE_FRAME_ROUNDING, 6)

    if imgui.button(f"{input_value.__name__}##{unique}"):
        function_args = inspect.signature(input_value).parameters
        kwargs = {}
        for name, param in function_args.items():
            if param.default is not inspect.Parameter.empty:
                kwargs[name] = param.default
            else:
                kwargs[name] = None
        try:
            input_value(**kwargs)
        except Exception as e:
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback()

    imgui.pop_style_var(3)

    return changed, input_value

@with_header_minimal(is_default_for=(int), wraps=render_func)
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05):
    int_text_width = imgui.calc_text_size(str(input_value))[0]

    imgui.set_next_item_width(int_text_width + 20)
    changed, value = imgui.drag_int("##int", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value

@with_header(show_header=False, show_name=False, show_bg=True)
def draw_debug_label(input_value:str):

    imgui.text(input_value)

@with_header_minimal(is_default_for=Enum)
def draw_enum(input_value:Enum, global_style=None, style_manager=None, enum_tint=(0.3, 0.3, 0.3)):

    unique = "enum"
    imgui.set_next_item_width(imgui.get_content_region_available().x)
    selected_idx = next(enumerate(input_value.__class__))[1]
    changed = False

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))

    for i, option in enumerate(input_value.__class__):
        a_pretty_name = option.name.replace("_", " ").capitalize()

        label = f"{a_pretty_name}##{unique}{i}"
        active = (input_value == option)
        radio_style = global_style.radio_button
        if active:
            color = style_manager.make_color_style_rgb(*enum_tint, radio_style["active_base"])
            hover = style_manager.make_color_style_rgb(*enum_tint, radio_style["active_hover"])
            pressed = style_manager.make_color_style_rgb(*enum_tint, radio_style["active_pressed"])
        else:
            color = style_manager.make_color_style_rgb(*enum_tint, radio_style["inactive_base"])
            hover = style_manager.make_color_style_rgb(*enum_tint, radio_style["inactive_hover"])
            pressed = style_manager.make_color_style_rgb(*enum_tint, radio_style["inactive_pressed"])

        push_style_color(imgui.COLOR_BUTTON, *color)
        push_style_color(imgui.COLOR_BUTTON_HOVERED, *hover)
        push_style_color(imgui.COLOR_BUTTON_ACTIVE, *pressed)

        clicked = imgui.button(label)

        pop_style_color(1)
        pop_style_color(1)
        pop_style_color(1)

        if clicked:
            selected_idx = option
            changed = True

        same_line()
    new_line()

    enum_class = input_value.__class__
    if changed:
        selected_enum = enum_class(selected_idx)
    else:
        selected_enum = input_value
    pop_style_var(1)

    return changed, selected_enum