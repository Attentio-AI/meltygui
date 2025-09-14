from copy import copy
from enum import Enum
from functools import wraps
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

import libcst as cst

def generate_class_diff(obj, updates):
    clsname = obj.__class__.__name__
    for field, new_val in updates.items():
        old_val = getattr(obj, field)
        print(f"# Diff: {clsname}.{field} changed to {new_val}")


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
    def wrapper(next_kwargs=None, **kwargs):
        __tracebackhide__ = True

        annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
        if annotation is not None: return annotation

        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        next_kwargs['y_offset'] = Melty.collection_spacing
        if 'width' in kwargs:
            pass
        return core_header(**next_kwargs)

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header ")

    return wrapper


# Main draw function, called by the GUI framework
def draw(vis):
    draw_window(vis.root.lora_collection, name="Loras")
    draw_window(Melty.hotkey_registry, name="Hotkeys", is_window=True)
    draw_window(module, nmae="CST Module")
    # draw_any(vis.root.synth_collection, is_window=False)
    # #
    # draw_any("hello there", is_window=True)


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

    if width > 0 and height > 0:
        imgui.set_next_window_size(width, height + padding_fudge * 4)

    if not decorations:
        if pos_x is not None and pos_y is not None:
            imgui.set_next_window_position(pos_x - Melty.current_indent - padding_x - fudge_x,
                                           pos_y - padding_fudge - 2)
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
        flags = ( imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
                    imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS |
                    imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS)
        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (fudge_x, padding_fudge))

    window_title = f"{title}##window_{str(unique)}"
    # Bring to front without collapse
    opened, _ = imgui.begin(f"{title}##window_{str(unique)}", closable, flags=flags)
    if not decorations:
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)
        imgui.indent(Melty.current_indent - indent_size + padding_x)

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    Melty.window_hovered = imgui.is_mouse_hovering_rect(*window_rect)

    Melty.window_stack.append((window_title, enable))

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)

    #
    window_func(*args, **kwargs)
    Melty.window_stack.pop()

    if not decorations:
        imgui.unindent(Melty.current_indent - indent_size + padding_x)
        imgui.pop_style_var(1)
    draw_list.channels_merge()
    imgui.end()

    if hasattr(input_value, 'tint'):
        style_manager.set_imgui_tint(*previous_tint)

    redo_stack(unique)


source = "x = foo(1)\nprint(x)\n"
module = cst.parse_module(source)
name_edits = {}


@with_header(is_default_for=cst.SimpleStatementLine)
def draw_cst_single_line(input_value: cst.SimpleStatementLine, **kwargs):
    # An Assign has one or more targets, an AssignEqual token, and a value
    draw_any(input_value.body)

@render_func(is_default_for=cst.SimpleWhitespace)
def draw_cst_simple_whitespace(input_value: cst.SimpleWhitespace, **kwargs):
    # An Assign has one or more targets, an AssignEqual token, and a value
    imgui.text_colored("SWS", *(1,1,1, 0.2))


# --- Assignments ---
@with_header_minimal(is_default_for=cst.Assign)
def draw_cst_assign(input_value: cst.Assign, **kwargs):
    # An Assign has one or more targets, an AssignEqual token, and a value
    for target in input_value.targets:
        draw_any(target)
    imgui.text("=")
    draw_any(input_value.value)


# --- Names ---
@render_func(is_default_for=cst.Name)
def draw_cst_name(input_value: cst.Name):
    imgui.text(f"{input_value.value}")


@render_func(is_default_for=cst.Expr)
def draw_cst_expr(input_value: cst.Expr):
    # Just render the wrapped expression
    draw_any(input_value.value)
# --- Function Calls ---
@render_func(is_default_for=cst.Call)
def draw_cst_call(input_value: cst.Call):
    draw_any(input_value.func)
    imgui.text("(")
    for i, arg in enumerate(input_value.args):
        draw_any(arg)
        if i < len(input_value.args) - 1:
            imgui.same_line()
            imgui.text(",")
            imgui.same_line()
    imgui.text(")")

# --- Arguments ---
@render_func(is_default_for=cst.Arg)
def draw_cst_arg(input_value: cst.Arg):
    if input_value.keyword:
        imgui.text(f"{input_value.keyword.value}=")
        imgui.same_line()
    draw_any(input_value.value)


# --- Function Definitions ---
@render_func(is_default_for=cst.FunctionDef)
def draw_cst_functiondef(input_value: cst.FunctionDef, **kwargs):
    imgui.text(f"def {input_value.name.value}(")
    draw_any(input_value.params)
    imgui.text("):")
    for stmt in input_value.body.body:
        draw_any(stmt)


# --- Parameters ---
@render_func(is_default_for=cst.Parameters)
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
@render_func(is_default_for=cst.Param)
def draw_cst_param(input_value: cst.Param):
    draw_any(input_value.name)
    if input_value.default:
        imgui.same_line()
        imgui.text("=")
        imgui.same_line()
        draw_any(input_value.default)


# --- Individual Parameter ---
@render_func(is_default_for=cst.Integer)
def draw_cst_int(input_value: cst.Integer):
    imgui.text("Integer")
    imgui.text(f"{input_value.value}")


@render_func
def draw_window(input_value, window_stack=None, style_manager=None,
                show_header=False, is_window=True, draw_state=None,
                is_tree=False, show_bg=False, indent_size=0, name="", unique=0, *args, **kwargs):
    kwargs['input_value'] = input_value
    kwargs['is_window'] = is_window
    kwargs['is_tree'] = is_tree
    kwargs['style_manager'] = style_manager
    kwargs['window_stack'] = window_stack
    kwargs['name'] = name

    type_default_meta = Melty.type_defaults.get(type(input_value), None)
    if type_default_meta is not None and hasattr(type_default_meta, 'view_function'):
        window_func = type_default_meta.view_function
    else:
        window_func = draw_object
    return core_draw_window(window_func=window_func, input_value=input_value, window_stack=window_stack,
                     style_manager=style_manager, name=name,
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
                     unique, tag, style_manager, global_style):
    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Melty.is_window_enabled():
        return False, 0.0

    if key is None or melty.initial_drag_offset is None:
        return False, 0.0
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
    cursor_bottom = cursor_top + flow_spacing

    if tag == "bottom":
        # span = cursor_bottom - cursor_top
        cursor_bottom += 0
        cursor_top += 0

    if melty.drag_in_progress and not on_drag and do_flow and mouse_over_window:
        if draw_state.height is not None:
            active_drop = (melty.drag_drop_target == draw_state.unique
                           and tag == melty.drag_drop_target_tag)

            if Melty.inside_window():
                draw_list.channels_set_current(min(depth + 1, Melty.max_depth - 1))

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

                if melty.drag_drop_action.target_key is None:
                    pass

            height_as_factor = 800.0
            drag_distance = sqrt(melty.drag_delta[0] ** 2 + melty.drag_delta[1] ** 2)
            initial_fade_offset = max(min(1.0, melty.total_drag_distance / 10.0), 0.0)
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

            color = color if active_drop else inactive_color
            draw_list.add_rect_filled(draw_state.left, cursor_top - 1,
                                    draw_state.left + draw_state.width,
                                    max(cursor_top, cursor_bottom - 1),
                                    col=imgui.get_color_u32_rgba(*color), rounding=2.0)
            #
            # draw_list.add_line(draw_state.left, draw_state.top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.top - 2 - offset,
            #                    col=imgui.get_color_u32_rgba(*color), thickness=3)

    return False, flow_spacing

@render_func
def draw_header_end(global_style, unique, style_manager, show_search,
                    on_search, draw_state, collection, key,
                    melty, show_add_delete=True):
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

    start_x, end_x = 0, 0
    imgui.begin_group()
    if show_add_delete:
        imgui.same_line()
        start_x = imgui.get_cursor_screen_pos()[0]
        imgui.push_style_color(imgui.COLOR_TEXT, *search_color)
        imgui.push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        if imgui.button(f"\uf1f8##del"):
            melty.to_delete(key, collection)
            print("No selected_views or remove_view method")
        same_line()
        imgui.same_line()
        imgui.dummy(5, 0)
        imgui.same_line()
        end_x = imgui.get_cursor_screen_pos()[0]

        imgui.pop_style_color(2)

    if show_search or draw_state.search_active:
        imgui.same_line()
        imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + 2)

        icon = "\uf002"
        imgui.text_colored(icon, *search_color)
        imgui.same_line()
        search_width = 150.0
        imgui.set_next_item_width(search_width)
        search_changed, new_search = imgui.input_text(f"##search{unique}", draw_state.search_text)

        if search_changed:
            draw_state.search_text = new_search
            imgui.set_keyboard_focus_here(-1)
            request_render()

        if not draw_state.search_active:
            draw_state.search_text = ""

        if on_search:
            draw_state.search_active = True
            imgui.set_keyboard_focus_here(-1)
            request_render()

        draw_state.search_active = imgui.is_item_focused()


    imgui.end_group()
    imgui.pop_id()
    draw_state._end_header_size = (end_x - start_x, imgui.get_item_rect_size()[1])


def core_header(func, outer_func, input_value=None, collection=None, key=None, indent_size=10, depth=0, draw_state=None,
                window_stack=None, is_tree=True, is_window=False, spacing=Melty.spacing, padding=Melty.padding,
                show_header=True, show_bg=True, unique=0, name="", style_manager=None, global_style=None,
                selected_views=None, on_drag=False, on_drag_up=False, do_flow=True, melty=None, enable_flow=True,
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

        Melty.indent(indent_size)

        if on_drag:
            do_flow = False

        # ----------------- top spacing -----------
        _, flow_spacing = draw_drop_target(do_flow=True, enable_flow=enable_flow,
                         collection=collection, key=key, on_drag=False,
                         draw_state=draw_state, tag="top")
        # ------------------ end spacing -----------
        if width is None:
            if draw_state.width is not None and draw_state.width > 0:
                width = draw_state.width
            else:
                width = 1
                draw_state.width = 1

        if name == "alpha":
            pass

        content_region = imgui.get_content_region_available()[0]
        width = min(width, content_region)

        start_x_pos = imgui.get_cursor_screen_pos()[0]
        end_pos_x = imgui.get_cursor_screen_pos()[0]
        start_y_pos = imgui.get_cursor_screen_pos()[1]
        cutoff = 50
        y_margin = y_offset / 2.0
        imgui.dummy(0, y_margin)

        if on_drag:
            imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() - Melty.flow_spacing)
            Melty.flow_spacing = 0.0

        bg_tint = None
        bg_selected = False
        bg_hovered = False
        header_on_same_line = False
        header_start = imgui.get_cursor_screen_pos()[0]
        header_width = 0

        if show_header:
            next_kwargs['highlight'] = on_hover
            if on_drag:
                next_kwargs['opacity'] = 0.0
            next_kwargs.pop('spacing', None)
            next_kwargs.pop('padding', None)

            # --------------------- HEADER -----------------
            changed, return_value = draw_header(spacing=(spacing[0], Melty.spacing[1]),
                                          padding=(padding[0], Melty.padding[1] + 1),
                                          **next_kwargs)
            if changed:
                pass

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
                                 width=imgui.get_content_region_available()[0],
                                 style_manager=style_manager, name=name, decorations=False,
                                 focus=True, unique=unique, enable=False, args=(), kwargs=next_kwargs)
            else:
                next_kwargs['do_flow'] = True

            rect_size = imgui.get_item_rect_size()
            header_width = rect_size[0]

            # Auto indent is decided here
            space_available = width - header_width
            if not isinstance(input_value, (dict, list, tuple)) and not hasattr(input_value, '__dict__'):
                if draw_state.height is None:
                    draw_state.height = 0

                if draw_state.expanded_height is None:
                    draw_state.expanded_height = draw_state.height

                height = max(draw_state.height, draw_state.expanded_height)

                if height is None or height < 79 or on_same_line:
                    if (space_available > cutoff and draw_state.expanded) or on_same_line:
                        header_on_same_line = True
                        same_line()

            if not header_on_same_line and not on_drag:
                # ----------------- end header ---------------
                imgui.same_line()
                current_x = imgui.get_cursor_screen_pos()[0]
                space_used = current_x - start_x_pos

                # imgui.dummy(space_available - end_header_with, 0)
                imgui.same_line()
                draw_header_end(**next_kwargs)


        if show_bg:
            style_manager.get_tint()
            Melty.bg_stack.append(style_manager.get_tint())

        if not is_tree or draw_state.expanded or is_window:
            if not on_drag:
                next_kwargs.pop('spacing', None)
                next_kwargs.pop('padding', None)
                imgui.set_cursor_pos_y(imgui.get_cursor_pos_y())
                if header_on_same_line:
                    end_header_with = draw_state._end_header_size[0]
                else:
                    end_header_with = 0

                end_header_with = max(end_header_with, 0)

                current_x = imgui.get_cursor_screen_pos()[0]
                space_used = max(0, current_x - start_x_pos)
                space_available = width - space_used

                padding_x = imgui.get_style().frame_padding.x
                request_width = space_available - end_header_with - padding_x
                imgui.set_next_item_width(max(2, request_width))

                ######################## MAIN FUNC CALL ########################
                # next_kwargs['width'] = request_width - indent_size
                func_changed, func_return_val = func(spacing=(spacing[0], Melty.spacing[1]),
                                    padding=(padding[0], Melty.padding[1]),
                                    **next_kwargs)
                ############### END MAIN FUNC CALL #############################
                if func_changed:
                    return_value = func_return_val
                changed |= func_changed

                if header_on_same_line and show_header:
                    imgui.same_line()
                    current_x = imgui.get_cursor_screen_pos()[0]
                    space_used = current_x - start_x_pos
                    space_available = width - space_used
                    end_header_with = draw_state._end_header_size[0]

                    # imgui.dummy(space_available - end_header_with, 0)
                    imgui.same_line()
                    # ----------------- end header ---------------
                    draw_header_end(**next_kwargs)
                    imgui.same_line()
                    end_pos_x = max(end_pos_x, imgui.get_cursor_screen_pos()[0])

            if on_drag:
                same_line(spacing=0.0)
                imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                imgui.dummy(draw_state.width,
                            draw_state.height - y_offset/2 - 1)
                imgui.pop_style_var(2)

        if show_header:
            padding_x = imgui.get_style().frame_padding.x
            end_pos_x = max(end_pos_x, header_start + header_width +
                            draw_state._end_header_size[0])

        if show_bg:
            style_manager.get_tint()
            Melty.bg_stack.pop()

        imgui.same_line(spacing=0)

        imgui.dummy(0, y_margin)
        end_y_pos = imgui.get_cursor_screen_pos()[1]
        background_height = end_y_pos - start_y_pos

        background_height = background_height
        draw_state.width = max(end_pos_x - start_x_pos, max(draw_state.min_width or 0, min_width))
        background_width = max(width, draw_state.width - 2)
        draw_state.height = end_y_pos - start_y_pos
        draw_state.top = start_y_pos
        draw_state.left = start_x_pos
        # ----------------- top spacing -----------
        _, flow_spacing = draw_drop_target(do_flow=do_flow, on_drag=on_drag, enable_flow=enable_flow,
                                           collection=collection, key=key,
                                           draw_state=draw_state, tag="bottom")
        # ------------------ end spacing -----------

        if inside_window:
            if show_bg:
                draw_list.channels_set_current(max(0, min(Melty.max_depth - 2, depth - 2)))
                if not on_drag:
                    draw_bg(bypass=True, left=start_x_pos, top=y_margin + start_y_pos + Melty.spacing[1] / 2.0,
                            width=background_width, height=background_height - Melty.spacing[1] / 2.0 - 1 - y_offset,
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
                current_cursor = imgui.get_cursor_screen_pos()
                imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                tree_offset = 5
                imgui.set_cursor_screen_position((draw_state.left - tree_offset, draw_state.top - tree_offset))
                imgui.invisible_button(f"##block_tree", width=max(1, draw_state.width),
                                       height=max(tree_offset + 3,1))
                imgui.set_item_allow_overlap()

                imgui.set_cursor_screen_position((draw_state.left - tree_offset, draw_state.top))
                imgui.invisible_button(f"##block_tree", width=max(1, tree_offset),
                             height=max(1, draw_state.height))
                imgui.set_item_allow_overlap()

                imgui.set_cursor_screen_position((draw_state.left - tree_offset,
                                                  draw_state.top + draw_state.height - tree_offset - 2))
                imgui.invisible_button(f"##block_tree", width=max(1, draw_state.width),
                             height=max(tree_offset + 2, 1))
                imgui.set_item_allow_overlap()

                imgui.pop_style_var(2)
                imgui.set_cursor_screen_pos(current_cursor)

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
            return False, new_action

        return changed, return_value

def seperator(height):
    imgui.dummy(0, height / 2)
    imgui.separator()
    imgui.dummy(0, height / 2)


@with_header(is_default_for=(cst.Module))
def draw_collection(input_value, draw_state, depth, style_manager,
                    meta, suffix, melty, show_search=True, on_collapse=False,
                    on_expand=False, width=None, global_style=None, show_add_delete=True,
                    show_instance_vars=True, **kwargs):
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
    if isinstance(input_value, (dict, list, tuple, set)):
        use_tint = True
        use_child_meta = True
        apply_change = True
        parent_type = input_value.__class__
        if isinstance(input_value, dict):
            keys = input_value.keys()
            collection = input_value
        else:
            keys = range(len(input_value))
            collection = list(input_value)

    elif hasattr(input_value, "__dict__") and depth < Melty.max_depth:
        if hasattr(type(input_value), "__field_defaults__"):
            type(input_value).__field_defaults__.update(input_value.__dict__)
            keys = type(input_value).__field_defaults__.keys()
            ordered_driver = input_value
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
    draw_state.min_width = 0
    for idx, key in enumerate(keys):
        if isinstance(collection, dict) and key not in collection:
            continue
        item = collection[key]
        # visual separator (object extras)
        if key is None and item is None:
            seperator(Melty.spacing[1])
            continue

        # apply global filter for all types
        if isinstance(key, (int, float, Enum, NoneType)):
            key_str = f"{str(key)} {type(item).__name__}"
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
        obj_unique = ui_id(name=key_str, datatype=type(item), suffix=item_suffix)

        trigger_collapse = False
        if isinstance(input_value, dict) and on_collapse:
            trigger_collapse = True

        trigger_expand = False
        if isinstance(input_value, dict) and on_expand:
            trigger_expand = True
        #
        prev_tint = None
        try:
            show_bg = getattr(item_meta, "show_bg", False)
            if hasattr(item, "tint") or show_bg:
                prev_tint = style_manager.get_tint()
                collection_spacing = Melty.collection_spacing
                style_manager.set_imgui_tint(*item.tint)
            item_changed, out_val = draw_any(item, key=key, meta=item_meta, trigger_collapse=trigger_collapse,
                             trigger_expand=trigger_expand,
                             collection=ordered_driver, suffix=str(obj_unique), name=key_str)

            if hasattr(item_meta, "tmp_draw_state"):
                if draw_state.left is not None:
                    child_right_edge = item_meta.tmp_draw_state.width or 0
                    this_right_edge = draw_state.width
                    overlap = child_right_edge - this_right_edge
                    draw_state.min_width = max(draw_state.width + overlap, draw_state.min_width or 0)

            if isinstance(out_val, CollectionAction):
                # perform the move - this should mutate the plain dicts you attached
                result = melty.to_apply(out_val)
                item_changed, out_val = False, None

            if item_changed and apply_change and key is not None:
                if isinstance(collection, dict):
                    collection[key] = out_val
                elif isinstance(collection, list):
                    collection[key] = out_val
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

    if drew_any:
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
def draw_header(input_value=None, name="", meta=None, unique=None, is_tree=True,
                show_name=True, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, is_window=False, on_click=False, show_tint=True,
                on_hover=False, highlight=False, opacity=1.0, show_add_delete=True,
                on_right_click=False, show_bg=False, selected_views=None, indent_size=10,
                on_drag=False, on_drag_released=False, on_action=None, style_manager=None,
                global_style=None, global_toggles=None, width=None, depth=0, shift_click=False):

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
    hover_offset = 0.0
    # Make header slightly brighter

    if highlight:
        hover_offset = 0.2

    saturation = bg_style['saturation'] + saturation - hover_offset
    name_color = (style_manager.
                  make_color_style_value(input=bg_style, saturation=saturation,
                                         value=max(0, dynamic_value * value_factor + value_offset + hover_offset)))

    if is_tree:
        if trigger_collapse:
            draw_state.expanded = False
        if trigger_expand:
            draw_state.expanded = True

        was_expanded = draw_state.expanded

        draw_state.expanded = tree("##tree", draw_state.expanded, width=50)

        if not was_expanded and draw_state.expanded:
            draw_state.height = draw_state.expanded_height
        same_line()
    cursor_start = imgui.get_cursor_pos()

    if show_name and name != "":
        imgui.align_text_to_frame_padding()
        imgui.text_colored(f"{name}", *name_color)
        same_line()

    name_end = imgui.get_cursor_pos()

    if show_type:
        imgui.text_colored(f"({type(input_value).__name__})", *(0.8, 0.0, 0.5, 1.0))
        same_line()

    if show_unique:
        imgui.text_colored(f"({str(unique)[-3:]})", *(0.4, 0.6, 0.9, 1.0))
        same_line()
    if show_name and name != "":
        same_line()
        imgui.set_item_allow_overlap()
        imgui.set_cursor_pos_x(cursor_start[0])
        button_width = max(5, name_end[0] - cursor_start[0])
        button_height = imgui.get_text_line_height() + imgui.get_style().frame_padding.y * 2
        imgui.set_item_allow_overlap()
        if imgui.invisible_button(f"##block_tree", width=button_width,
                                  height=button_height):
            pass
        imgui.set_item_allow_overlap()
        same_line()

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
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)
        if show_add_delete:
            imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (3, 2))
            imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (4, 2))
            if imgui.button(f"\uf067##add"):
                # Use str as default hinted type
                hinted_type = str
                if meta.field_type is not None and hasattr(meta.field_type, "__args__"):
                    if len(meta.field_type.__args__) == 2:
                        hinted_type = meta.field_type.__args__[1]
                add_to_collection(input_value, hinted_type())
                on_change = True
                return_val = input_value
            same_line()
            imgui.pop_style_var(2)

    # -------------- Header indent fix --------------
    if imgui.get_cursor_pos_x() < Melty.header_indent:
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
        imgui.invisible_button(f"##", width=Melty.header_indent - imgui.get_cursor_pos_x(), height=18)
        imgui.set_item_allow_overlap()
        imgui.pop_style_var(2)

    same_line()

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
                depth=0, unique=0, suffix="", collection=None, key=None,
                is_tree=True, indent_size=10, *args, **kwargs):
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
def draw_any(input_value, *args, **kwargs):
    if input_value.__class__.__name__ == cst.Integer.__name__:
        pass

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

    if kwargs['global_toggles'].force_show_datatype:
        imgui.text_colored(f"[{type(input_value).__name__}]", 0.5, 0.5, 0.5)

    return meta.view_function(input_value, *args, **kwargs)


@with_header_minimal(is_default_for=(NoneType))
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text("None")

    return False, None


@with_header_minimal(is_default_for=(bool))
def draw_bool(input_value: bool):
    changed, is_checked = imgui.checkbox("##bool", input_value)
    if changed:
        return True, is_checked

    return False, None


@with_header_minimal(is_default_for=(str))
def draw_str(input_value: str):
    changed, value = imgui.input_text("##str", input_value)
    if changed:
        return True, value

    return changed, value


@with_header_minimal(is_default_for=('tint'))
def draw_tuple(input_value: tuple, is_tree=False, show_bg=False):
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


@with_header_minimal(is_default_for=(int), wraps=render_func)
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05):
    changed, value = imgui.drag_int("##int", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value


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