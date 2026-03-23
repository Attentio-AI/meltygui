import inspect
import sys
import threading
import types
from collections import deque, defaultdict
from collections.abc import MutableMapping
from dataclasses import dataclass
from enum import Enum
from inspect import Parameter
from math import sqrt
from pathlib import Path
from types import NoneType
from typing import Optional

import OpenGL.GL as gl
import glfw
import numpy
from imgui.core import _DrawList

from src.lsd.gl_gui import toggles
from src.lsd.gl_gui.melty import Melty, CollectionAction, ManagedWindow
from src.lsd.gl_gui.model.core_model.draw_state import ZoomState, TileMode
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, push_style_var, \
    push_style_color, pop_style_color, pop_style_var, end, begin
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import Comment
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line, new_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.codec_register import registry as FILE_CODECS
from src.lsd.gl_gui.view.core_views.core_meta import Meta
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import hotkey
from src.lsd.gl_gui.view.core_views.decoration.invalidation_decoration import live
from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer, draw_header_end
from src.lsd.gl_gui.view.core_views.inspect_utils import set_fn_defaults
from src.lsd.gl_gui.view.core_views.monitor import Monitor
from src.lsd.gl_gui.view.core_views.text_editor import draw_text
from src.shader_library.shader_manager.texture_manager import PendingTexture


@render_func(use_cache=True, show_bg=False, width=20, height=22, tile_mode=TileMode.MAX,
             auto_resize=False, just_shadow=True, selectable=False, no_cursor=True, temp=True)
def empty(input_val):
    pass


@render_func(use_cache=True, auto_resize=False, closable=True, selectable=False,
             show_bg=True, melty_window=True, draggable=True, show_tint=True, tile_mode=TileMode.MAX,
             with_header=draw_header, with_header_end=draw_header_end, indent_size=5,
             with_footer=draw_footer)
def draw_window(input_value:any, view_func=None, draw_state=None, delete_down=False, search_text="", glfw_close_down=False, **kwargs):
    if delete_down and imgui.get_io().key_ctrl:
        draw_state.closed = True


    if 'name' not in kwargs:
        kwargs['name'] = str(input_value)

    kwargs['show_bg'] = False
    kwargs['selectable'] = False
    kwargs['return_extras'] = True
    kwargs['with_header'] = None
    kwargs['with_header_end'] = None
    kwargs['with_footer'] = None
    kwargs['is_tree'] = False
    kwargs['closable'] = False
    kwargs['auto_resize'] = True
    kwargs.pop('max_height', None)
    kwargs['shadow'] = False
    if search_text != "":
        kwargs['search_text'] = search_text

    return_val = draw_any(input_value, view_func=view_func, **kwargs)
    if len(return_val) == 3:
        return_val = (return_val[0], return_val[1], draw_state)


    return return_val

@render_func(is_default_for=types.ModuleType, use_cache=True, show_bg=True, with_header=draw_header, with_footer=draw_footer)
def draw_module(input_value: types.ModuleType, draw_state, **kwargs):
    imgui.text(f"Module: {input_value.__name__}")

@render_func(is_default_for=(dict, MutableMapping, defaultdict, types.MappingProxyType), use_cache=True,
             show_bg=True, show_instance_vars=False, manual_content_height=True, disable_scroll=True,
             shadow=True, wrap=False, with_header=draw_header, indent_size=5, searchable=True)
def draw_collection(input_value, draw_state, depth, style_manager, meta, mode=None, keys=None, get_attr=None, set_attr=None, show_excluded=False,
                    child_kwargs=None, nested_func=None, show_bg=True, show_search=True, on_collapse=False, search_text="",
                    on_expand=False, show_add_delete=True, item_spacing_y=1,
                    horizontal=False, show_indices=False, **kwargs):
    """
    Universal collection renderer
    """
    if draw_state.name == "cst_dict":
        pass

    if child_kwargs is None:
        child_kwargs = {}
    if isinstance(input_value, defaultdict):
        pass
    changed = False
    # ----- SIMPLE NORMALIZER (lowercase, remove spaces, '_' and '-') -----
    _TRANS = str.maketrans("", "", " _-")

    def norm_string(s) -> str:
        if s is None:
            return ""
        try:
            s = str(s)
        except Exception:
            s = ""
        return s.lower().translate(_TRANS)

    if hasattr(input_value, 'children') and isinstance(input_value.children, (list, dict, defaultdict,
                                                                              types.MappingProxyType, deque)):
        input_value = input_value.children

    if show_bg and draw_state.total_z_offset < 0:
        imgui.dummy(1,3)
    else:
        imgui.dummy(1,1)

    search_token = ""

    # --- Setup per collection type ---
    collection = input_value
    parent_type = input_value.__class__

    if keys is None:
        if isinstance(input_value, (dict, list, tuple, set, defaultdict, MutableMapping, types.MappingProxyType, deque)):
            apply_change = True
            parent_type = input_value.__class__
            if isinstance(input_value, types.MappingProxyType):
                keys = input_value.keys()
            elif isinstance(input_value, (dict, defaultdict, MutableMapping, types.MappingProxyType)):
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
                if input_value is None or input_value.__dict__ is None:
                    return False, input_value
                keys = input_value.__dict__.keys()
            collection = input_value.__dict__
            use_tint = False
            apply_change = True
        else:
            imgui.text("No view for type: " + str(type(input_value)))
            return False, input_value

        keys = list(keys)[:]

    # --- unified loop ---
    drew_any = False
    all_meta = []

    start_cursor = imgui.get_cursor_pos()[1]
    rect = Melty.get_clip_rect()

    premature_break = False

    Melty.collection_index_stack.append(0)
    this_collection = len(Melty.collection_index_stack) - 1

    start_index = 0
    end_index = len(keys) - 1

    scroll_offset = draw_state.scroll_offset
    true_left = draw_state.left - scroll_offset[0]
    true_top = draw_state.top - scroll_offset[1]

    for idx in range(start_index, end_index + 1):
        key = keys[idx]
        relative_pos = imgui.get_cursor_screen_pos()
        relative_pos = (relative_pos[0] - true_left,
                        relative_pos[1] - true_top + item_spacing_y)

        child_draw_state = draw_state._children.get(idx, None)
        if not horizontal:
            if child_draw_state is not None and (
                    not draw_state.invalid_content_height or imgui.is_mouse_down(0) or imgui.is_mouse_down(
                1) or imgui.is_mouse_down(2)):
                if not Melty.frame_count <= 2:
                    if child_draw_state.relative_pos is not None:
                        screen_pos = (true_left + child_draw_state.relative_pos[0],
                                      true_top + child_draw_state.relative_pos[1] - child_draw_state.header_height)

                        bottom = screen_pos[1] + child_draw_state.height + child_draw_state.header_height
                        if (bottom + child_draw_state.height < rect[1] or screen_pos[1] > rect[3]):
                            imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0],
                                                         (true_top + child_draw_state.relative_pos[1] +
                                                          child_draw_state.height)))
                            continue

        Melty.collection_index_stack[this_collection] = idx
        item = None
        if get_attr is None:
            if isinstance(collection, dict) and key not in collection:
                imgui.text("Key not found: " + str(key))
                continue

            if hasattr(input_value, "__dict__") and hasattr(input_value, key):
                item = getattr(input_value, key, None)
            else:
                item = collection[key]
        else:
            try:
                item = get_attr(input_value, key)
            except Exception as e:
                imgui.text(f"Error getting key {key}")

        # visual separator (object extras)
        if key is None and item is None:
            seperator(Melty.spacing[1])
            continue

        if not show_excluded and hasattr(type(input_value), "__excluded_attrs__"):
            if not Toggles.show_excluded:
                if str(key) in type(input_value).__excluded_attrs__:
                    continue
        display_name = None

        # apply global skip to all types
        if isinstance(key, (float, Enum, NoneType)):
            key_str = f"{input_value.__class__.__name__}"
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)

        if not show_excluded and ((key_str.startswith("_") or key_str.endswith("_")) or
                                  key_str.endswith("meta")):
            continue

        # ----- SEARCH CHECK (keys + item.name if present) -----
        if search_token != "":
            name_field = getattr(item, "name", None) or getattr(item, "__name__", "")
            if ((search_token not in norm_string(key_str)) and
                    (search_token not in norm_string(name_field))):
                continue

        item_meta = Meta.get_child_meta(parent_type, field_name=key, value=item)
        item_meta.collection_type = meta.field_type

        prev_tint = None
        try:
            if isinstance(collection, FolderProxy):
                codec = FILE_CODECS.for_name(key)
                if codec is not None and hasattr(codec, 'tint'):
                    prev_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*codec.tint)

            y_offset = Melty.collection_spacing
            all_meta.append(item_meta)
            if show_indices:
                display_name = f"{str(idx)}"

            if nested_func is None:
                if item_meta is None:
                    if hasattr(Meta, 'get_child_meta'):
                        item_meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)

                if item_meta.view_function is None:
                    item_meta.view_function = draw_collection

                item_func = item_meta.view_function
            # else:

            item_func = nested_func

            item_kwargs = {
                'return_extras': True,
                'key': key,
                'meta': item_meta,
                'on_collapse': on_collapse,
                'on_expand': on_expand,
                'collection': input_value,
                'name': key_str,
                'display_name': display_name,
                'parent_show_add_delete': show_add_delete,
                'show_add_delete': show_add_delete,
                'y_offset': y_offset,
                'mode': mode,

            }

            item_kwargs.update(child_kwargs)
            # if mode is None:
            #     mode = Melty.mode_stack[-1] if len(Melty.mode_stack) > 0 else None
            # if mode is not None:
            #     # Loop over super types
            #     mode_config = None
            #     for super_type in type(item).__mro__:
            #         mode_config = mode.value.get(super_type, None)
            #         if mode_config is not None:
            #             break
            #     if mode_config is not None:
            #         override_kwargs = mode_config.kwargs
            #         item_kwargs = item_kwargs | override_kwargs
            #         if mode_config.func is not None:
            #             item_func = mode_config.func
            #     else:
            #         item_kwargs['mode'] = mode

            item_return = draw_any(item, **item_kwargs)

            # item_return = item_func(item, **item_kwargs)

            if len(item_return) == 3:
                item_changed, out_val, returned_ds = item_return
            else:
                item_changed, out_val, returned_ds = item_return[0], item_return[1], None

            if returned_ds is not None:
                draw_state._children[idx] = returned_ds
                returned_ds._collection_draw_state = draw_state
                returned_ds.relative_pos = relative_pos
                if horizontal:
                    imgui.same_line(spacing=0)
                    imgui.set_cursor_screen_pos((returned_ds.left + returned_ds.width, returned_ds.top))
                    rect = Melty.get_clip_rect()
                    right_edge = rect[2]
                    space_left = right_edge - (returned_ds.left + returned_ds.width)

                    if space_left < returned_ds.width:
                        imgui.new_line()
                        imgui.dummy(0, item_spacing_y)

                else:

                    imgui.dummy(0, item_spacing_y)

            if isinstance(out_val, CollectionAction):
                # perform the move; this should mutate the plain dicts you supply
                result = Melty.to_apply(out_val)
                item_changed, out_val = False, None

            if set_attr is not None and item_changed:
                try:
                    set_attr(input_value, key, out_val)
                except Exception as e:
                    print(f"Error setting key {key} to value {out_val}: {e}")
            else:
                if item_changed and apply_change and key is not None:
                    if isinstance(input_value, (dict, defaultdict, MutableMapping, types.MappingProxyType)):
                        input_value[key] = out_val
                    elif isinstance(input_value, list):
                        input_value[key] = out_val
                    elif isinstance(input_value, deque):
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
            print_colored_traceback(*sys.exc_info())

        finally:
            if prev_tint is not None:
                style_manager.set_imgui_tint(*prev_tint)

    Melty.collection_index_stack.pop()
    end_pos = imgui.get_cursor_pos()[1]
    content_height = (end_pos - start_cursor)
    imgui.dummy(1, 1)

    # if len(children_draw_states) == len(keys):
    #     draw_state._children = children_draw_states

    #
    # over_layer_draw_list: _DrawList = imgui.get_overlay_draw_list()
    # color = imgui.get_color_u32_rgba(1, 0, 0, 1)
    # over_layer_draw_list.add_text(draw_state.left, draw_state.abs_top, color,f"{Melty.nested_collections}  {break_index} {len(keys)} ")
    # if nested_collection:
    #     Melty.nested_collections -= 1

    # imgui.set_cursor_screen_pos((current_cursor[0], current_cursor[1] + draw_state.scroll_offset[1]))
    # current_cursor = imgui.get_cursor_screen_pos()
    if not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2) and not premature_break:
        draw_state.content_height = snap_int(content_height)
        draw_state.invalid_content_height = False

    draw_state.premature_break = premature_break

    # ----------------- top spacing -----------
    last_key = list(keys)[-1] if len(keys) > 0 else None

    if not drew_any:
        last_key = None

    last_meta = all_meta[-1] if len(all_meta) > 0 else None
    if hasattr(last_meta, 'tmp_draw_state'):
        last_draw_state = last_meta.tmp_draw_state if last_meta is not None else draw_state

        # if Melty.window_enabled and melty.drag_in_progress:
        #     # last_item = collection[last_key] if (isinstance(collection, dict) and last_key in collection) else None
        #     _, flow_spacing = draw_drag_drop_target(do_flow=True, enable_flow=True, melty=melty, offset=0,
        #                                             collection=ordered_driver, key=last_key, on_drag=False,
        #                                             draw_state=last_draw_state, tag="bottom")
    # ------------------ end spacing -----------

    # if drew_any and len(keys) > 1:
    #     imgui.dummy(0, 2)

    return changed, input_value


def main_header(input_value, name, **kwargs):
    imgui.text("Main Header")


@render_func(is_default_for=(property))
def draw_property(input_value:property, draw_state, **kwargs):
    imgui.text_colored(f"Property: {input_value.fget.__name__}", 1.0, 0.5, 0.0, 1.0)
    # value = input_value.fget(input_value)
    # draw_any(value, name="value", show_bg=True, draw_state=draw_state)

@render_func(is_default_for=(type), show_bg=False, tint=(0.01406166236847639, 0.2259240746498108, 0.30232560634613037), with_header=draw_header, with_footer=draw_footer)
def draw_type(input_value:type, draw_state, **kwargs):

    draw_collection(vars(input_value), name="vars", show_excluded=True)
    # draw_collection(dir(input_value), name="dir", show_excluded=True)
    # draw_collection(input_value.__dict__, name="__dict__", show_excluded=True)
    # draw_collection(inspect.getmembers(input_value), name="inspect")
    keys = list(set(dir(input_value)) | set(vars(type(input_value))))
    type_get_attr = lambda obj, key: getattr(obj, key, None)
    type_set_attr = lambda obj, key, value: setattr(obj, key, value)
    #draw_collection(input_value, name="inspect", keys=keys, get_attr=type_get_attr, set_attr=type_set_attr)

some_float = [0.0]
cst_dict = {}
test_code = None
#
# def code_to_dict():
#     global cst_dict
#     global test_code
#     cst_tree = convert(test_code, cst.Module, registry=Melty)
#     cst_dict = convert(cst_tree, dict, registry=Melty)
#     return cst_dict
#
# def dict_to_code():
#     global cst_dict
#     global test_code
#     cst_tree = convert(cst_dict, cst.Module, registry=Melty)
#     test_code = cst_tree.code
#     return test_code
#
# def path_to_text():
#     path = Path("/home/lukas/test_folder/test_list.txt")
#     text = convert(path, path=[Path, bytes, str], registry=Melty)
#     return text
#


@render_func(show_bg=True, with_header=draw_header)
def test_columns():
    draw_str("Column 1", name="col1", column=0)
    draw_int(123, name="col2", column=1)
    draw_float(0.5, name="col3", column=2)
    draw_float(0.5, name="test_5", column=5)
    draw_float(0.5, name="test_5_b", column=5)

    for i in range(10):
        draw_float(0.4, name=f"float_{i}", column=2)


@render_func(use_cache=True, show_bg=False, shadow=False, disable_scroll=True, selectable=False, with_header=draw_header)
def draw_with_modes(input_value, modes):
    changed = False
    value = input_value
    for idx, mode in enumerate(modes):
        mode_changed, value = draw_any(input_value, name=f"Mode: {mode}", mode=mode, z_offset=3, selectable=False, auto_resize=True, disable_scroll=False,
                                       show_bg=True, shadow=True, column=idx)
        changed |= mode_changed

    return changed, value

@render_func
def draw_draw_state(input_value, **kwargs):
    pass


@render_func(use_cache=False, show_bg=True, selectable=False, show_tint=True, bg_offset=-1, with_header=draw_header)
def draw_main(input_value, vis):
    global test_obj
    return_val = draw_window(Melty.profiles_results, show_bg=True, name="Profile Results")
    return_val2 = draw_window(Melty.registered_windows, is_tree=True, show_add_delete=False, return_extras=True,
                              name="Window Manager",
                              z_absolute=-1)
    global cst_dict
    global test_code
    from src.lsd.gl_gui.view.mode import Mode

    changed, value = draw_any(draw_header, name="draw_header", show_bg=True, mode=(Mode.CODE_UI, Mode.WINDOW))
    if changed:
        test_code = value

    changed, value = draw_with_modes(input_value=draw_bg, name="draw_bg",
                                     show_bg=True, mode=(Mode.WINDOW), modes=[Mode.CODE_PLAIN_TEXT, Mode.CODE_UI, Mode.CODE_DICT_STR])
    if changed:
        test_code = value

    changed, value = draw_with_modes(input_value=toggles, name="Toggles",
                                     show_bg=True, mode=(Mode.WINDOW), modes=[Mode.CODE_PLAIN_TEXT, Mode.CODE_UI])

    from src.lsd.gl_gui.model.app_model import Lora
    changed, value = draw_with_modes(input_value=Lora, name="lora class",
                                     show_bg=True, mode=(Mode.WINDOW), modes=[Mode.CODE_PLAIN_TEXT, Mode.CODE_UI, Mode.CODE_DICT_STR])


    some_path = Path("/home/lukas/test_folder/test_list.txt")
    changed, value = draw_any(some_path, name="test_path_render", mode=(Mode.FILE_META, Mode.WINDOW))
    if changed:
        path = value

    from src.lsd.gl_gui.model.app_model import TensorView
    draw_window(TensorView, name="Tensorview")

    global_toggles = vis.root.global_toggles
    draw_any(global_toggles, name="Toggles", auto_resize=True, wrap=True, use_cache=True, show_bg=True, mode=Mode.WINDOW)
    changed, new_val = draw_any(Melty.cache.enabled, name="Offscreen Rendering", wrap=True, show_bg=True, use_cache=True)
    if changed:
        if new_val:
            Melty.cache.set_enabled(True)
        else:
            Melty.cache.set_enabled(False)
        Melty.cache.invalidate_all()
        request_render()

    changed, new_val = draw_any(Melty.cache.copy_debug_mode, name="Offscreen Debug", wrap=True, use_cache=True)
    if changed:
        # Melty.cache.offscreen_debug_mode = new_val
        Melty.cache.copy_debug_mode = new_val
        Melty.cache.invalidate_all()
        request_render()

    changed, new_val = draw_any(Melty.cache.offscreen_scale, name="Debug Scale",
                                min_value=0.0, max_value=255.0, wrap=True, use_cache=True)
    if changed:
        Melty.cache.offscreen_scale = new_val
        Melty.cache.invalidate_all()
        request_render()

    global some_float
    changed, new_float = draw_window(some_float[0], name="Conversion Test", view_func=draw_collection, convert=dict)
    if changed:
        print("New float value:", new_float)
        some_float[0] = new_float

    draw_window(Monitor, name="Monitor")

    draw_window(threading.enumerate(), name="Threads")

    # draw_window(core_model, name="Module test")

    test_columns(input_value="Nolkjlkjne", mode=Mode.WINDOW, name="Test columns")

    draw_window(draw_main, name="Draw Main Function")

    draw_window(test_obj, name="Layer 1")

    draw_window(filesystem_proxy, name="Filesystem Test")

    draw_any(input_value=proxy, name="CST Proxy", mode=Mode.WINDOW)

    draw_window(vis.root.lora_collection, name="Test Window 1")
    draw_window(vis.root.lora_collection.loras, name="Test Window 2", child_kwargs={
        'expanded': False, 'is_tree':False, 'show_add_delete': False})
    draw_window(Melty.last_invalid, show_bg=True, name="Last Invalid")

    draw_window(input_value=Melty.type_to_default_view_func, is_tree=True,
                show_add_delete=False, name="Type Defaults")

    # draw_window(Melty.registered_windows, is_tree=True,
    #             show_add_delete=False, name="Test window manager")

    draw_window("test", name="Test Widget Window")
    draw_window(Melty.cache.snapshot_tex, show_bg=True, name="Snapshot Texture", live=True)

    changed, new_val = draw_window(0.0, layer=31, name="Test return")
    if changed:
        print("Value changed:", new_val)

    changed, new_val = draw_window([1, 2, 3, 4, 5], name="Test List", horizontal=True, tint=(1, 0, 0))

    normalized_sub_mask, _, _ = Melty.filter.normalize(Melty.cache._mask_tex)
    draw_window(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
                max_brightness=30, name="mask_tex", live=True)

    normalized_sub_mask, _, _ = Melty.filter.normalize(Melty.cache._full_mask_tex)
    draw_window(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
                max_brightness=30, name="full_mask_tex", live=True)

    draw_window("input_val", name="Outer live", view_func=test_widget, live=True)
    draw_window("input_val", name="Outer no live", view_func=test_widget, live=False)

    # draw_window(Melty.last_request_render, show_bg=True, name="Last Invalid")

    mouse_pos = imgui.get_mouse_pos()
    ds_under_mouse = Melty.bvh_query(mouse_pos[0], mouse_pos[1])
    ds_names = [ds.name for ds in ds_under_mouse]
    draw_any(ds_names, name="Draw State under mouse", show_bg=True, wrap=True, use_cache=True, mode=Mode.WINDOW, live=True)
    #
    last_ds_under_mouse = list(Melty.selected)[-1] if len(Melty.selected) > 0 else None
    # if last_ds_under_mouse is not None:
    #     Melty.active_layer = last_ds_under_mouse.layer
    #     last_ds_under_mouse.layer = Melty.active_layer
    #     last_ds_under_mouse.z_pos = Melty.z_pos
    #     last_ds_under_mouse.depth_and_layer = (Melty.shadow_depth, Melty.active_layer)
    #     last_ds_under_mouse._kwargs['active_layer'] = Melty.active_layer
    #
    #     if Melty.channels_split:
    #         imgui.get_window_draw_list().channels_set_current(min(Melty.active_layer, Melty.max_depth - 1))
    #     Melty.draw(last_ds_under_mouse, detached=True)


@render_func
def test_widget(input_value, name, unique, **kwargs):
    imgui.text("Test Widget")
    draw_text("Editable Text", name="editable_text", show_bg=True)


source = "x = foo(val=1)\nprint(x)\nsome_list=[0, 1, 2, 3]\n"
module = cst.parse_module(source)
proxy = cst_wrap(module)
name_edits = {}
code_export_str = "Test"

filesystem_proxy = FolderProxy("/home/lukas/test_folder", text_mode=True)



# Main draw function, called by the GUI framework

@live
class TestObj:
    def __init__(self):
        self.test_val = 0.0
        self.test_list = [1, 2, 3, 4, 5]


test_obj = TestObj()


def draw_melty_windows(vis):
    flags = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
             imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS |
             imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS | imgui.WINDOW_NO_NAV_INPUTS | imgui.WINDOW_NO_NAV |
             imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS)

    # style.frame_padding = (4, 2)

    imgui.set_next_window_position(0, 0)
    # Fill the entire screen
    fb_w, fb_h = map(int, imgui.get_io().display_size)

    imgui.set_next_window_size(fb_w, fb_h)
    title = "main##window_melty"
    opened, _ = begin(title, closable=False, flags=flags)

    Melty.imgui_main_window_hovered = imgui.is_window_hovered()

    Melty.begin_frame()

    imgui.invisible_button("window_blocker", width=fb_w, height=fb_h)
    imgui.set_cursor_screen_pos((0, 0))
    imgui.set_item_allow_overlap()

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    Melty.window_stack.append((title, True))

    draw_main(name="Main Window", vis=vis, width=fb_w, height=fb_h)
    from src.lsd.gl_gui.applet.test_applet import render_app
    render_app()

    Melty.end_frame()

    # End frame ###############
    Melty.window_stack.pop()
    draw_list.channels_merge()
    Melty.channels_split = False

    end()


@render_func(is_default_for=PendingTexture, use_cache=True, z_offset=0, selectable=False,
             show_bg=False, auto_resize=True, with_header=draw_header)
def draw_pending_texture(input_value: PendingTexture, draw_state):
    if input_value.texture_id is None:
        imgui.text(f"Uploading... {id(input_value)}")
        return False, None

    return_val = draw_texture(input_value.texture_id, name=f"{draw_state.id}_inner",
                              auto_resize=False, show_header=False, use_cache=True, wrap=False)

    return return_val


@render_func(is_default_for=numpy.uint32, show_bg=True,
             use_cache=False, show_add_delete=False, z_offset=2, fill_height=True,
             indent_size=0, min_width=100, min_height=100, wrap=False, disable_scroll=True,
             enable_scroll=True, zoom_speed=0.3, with_header=draw_header, manual_content_height=True)
def draw_texture(input_value: numpy.uint32, hovered, scroll_y_changed, middle_mouse_drag, right_mouse_drag,
                 zoom_state: ZoomState, zoom_speed, header_height=0, min_zoom=0.1,
                 max_zoom=50.0, style_manager=None, max_brightness=5.0, max_contrast=5.0,
                 draw_state=None, jet=False, **kwargs):
    original_id = input_value
    texture_id = input_value
    imgui.dummy(draw_state.width, draw_state.height - 20)

    # Ensure we have valid state if this is the first run
    if not hasattr(zoom_state, 'zoom'):
        zoom_state.zoom = 1.0
        zoom_state.center_u = 0.5
        zoom_state.center_v = 0.5

    # Check if opengl texture ID is valid
    if not gl.glIsTexture(texture_id):
        imgui.text(f"Error: {texture_id} is not a valid texture")
        return False, None

    # 1. Query Texture Properties
    original_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)

    width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH)
    height = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    # if draw_state.width is None:
    #     draw_state.width = (width)
    #
    # if draw_state.height is None:
    #     draw_state.height = (height)

    if width > 16384 or height > 16384:
        imgui.text(f"Error: Texture size {width}x{height} exceeds maximum supported size.")
        return False, None

    if width == 0 or height == 0:
        return False, None

    # 2. Canvas Setup (Fill available space)
    view_width = max(1, draw_state.width)
    view_height = max(1, draw_state.height)

    # 3. Calculate Aspect Ratio Corrections
    tex_aspect = width / height
    view_aspect = view_width / view_height

    # Calculate the visible UV width/height based on zoom and aspect ratio.
    if view_aspect > tex_aspect:
        # View is wider: Fit to Height
        uv_height_size = 1.0 / zoom_state.zoom
        uv_width_size = uv_height_size * (view_aspect / tex_aspect)
    else:
        # View is taller: Fit to Width
        uv_width_size = 1.0 / zoom_state.zoom
        uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # # 4. Handle Input and Interaction
    # Melty.cache.mask_mark_rect(draw_state, Melty.max_z - 1, draw_state.shadow_index,
    #                            draw_state.left, draw_state.abs_top, view_width, view_height,
    #                            key=f"texture_{original_id}")

    mixed_color = (1, 1, 1, 1)
    highlight_color = (1, 1, 1, 1)

    if style_manager is not None:
        mixed_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                   value=0.3, factor=0.9, saturation_scale=1.0, alpha=1.0)
        highlight_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                       value=1.0, factor=0.9, saturation_scale=1.0, alpha=1.0)
    io = imgui.get_io()
    overlay: _DrawList = imgui.get_overlay_draw_list()

    if right_mouse_drag:
        b_str = f"{zoom_state.brightness:.3f}"
        overlay.add_text(right_mouse_drag.x, right_mouse_drag.y - 30,
                         col=imgui.get_color_u32_rgba(*highlight_color[:3], 1),
                         text=f"brightness:{zoom_state.brightness:.3}\ncontrast:{zoom_state.contrast:.3}")

        if io.key_shift:
            if io.key_ctrl:
                zoom_state.hue += right_mouse_drag.dx * 0.001
                zoom_state.saturation -= right_mouse_drag.dy * 0.001
            else:
                zoom_state.brightness += right_mouse_drag.dx * 0.001
                zoom_state.contrast -= right_mouse_drag.dy * 0.001
        else:
            if io.key_ctrl:
                zoom_state.hue += right_mouse_drag.dx * 0.005
                zoom_state.saturation -= right_mouse_drag.dy * 0.005
            else:
                zoom_state.brightness += right_mouse_drag.dx * 0.005
                zoom_state.contrast -= right_mouse_drag.dy * 0.005

        # zoom_state.brightness = max(0.0, min(max_brightness, zoom_state.brightness))
        # zoom_state.contrast = max(0.0, min(max_contrast, zoom_state.contrast))

    if jet:
        texture_id = Melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Melty.filter.jet(texture_id, offset=zoom_state.hue)
    else:
        texture_id = Melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Melty.filter.hue_saturation(
            texture_id,
            saturation=(zoom_state.saturation),
            hue_shift=(zoom_state.hue),
        )

    # texture_id = Melty.filter.swirl(
    #     input_value,
    #     radius=1.0,
    #     angle=(zoom_state.brightness * 5),
    # )
    # texture_id = Melty.filter.swirl(
    #     texture_id,
    #     angle=zoom_state.brightness,
    #     radius=zoom_state.contrast
    # )

    p_min = (draw_state.abs_left + 2, draw_state.abs_top + 2)
    p_max = (draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height - 2)
    p_min_x, p_min_y = p_min[0], p_min[1]

    scroll_delta = 0
    if scroll_y_changed is not None:
        scroll_delta = scroll_y_changed.value

    # --- Logic: Zoom and Pan ---

    zoom_delta = 0.0

    # 4a. Handle Zoom Triggers (Scroll & Keyboard)

    # Keyboard Shortcuts (1, 2, 3, 4)
    forced_zoom = -1.0
    key_1 = 49
    numpad_key_1 = 321
    if hovered:
        if imgui.is_key_pressed(key_1) or imgui.is_key_pressed(numpad_key_1):  # Key '1'
            forced_zoom = 1.0
            # Reset Pan to Center
            zoom_state.center_u = 0.5
            zoom_state.center_v = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(50):  # Key '2'
            forced_zoom = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(51):  # Key '3'
            forced_zoom = 0.25
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(52):  # Key '4'
            forced_zoom = 0.125
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0

    if forced_zoom > 0:
        zoom_state.zoom = forced_zoom
        # Recalculate uv_size immediately for consistent bounding this frame
        if view_aspect > tex_aspect:
            uv_height_size = 1.0 / zoom_state.zoom
            uv_width_size = uv_height_size * (view_aspect / tex_aspect)
        else:
            uv_width_size = 1.0 / zoom_state.zoom
            uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # Scroll Logic
    if scroll_delta != 0:
        if io.key_shift:
            zoom_delta = scroll_delta * zoom_speed * 0.3
        else:
            zoom_delta = scroll_delta * zoom_speed
    elif middle_mouse_drag and middle_mouse_drag.modifiers == glfw.MOD_CONTROL:
        zoom_delta = io.mouse_delta.y * -0.008

    # 4b. Handle Pan (Middle Click Drag)
    if middle_mouse_drag and middle_mouse_drag.modifiers != glfw.MOD_CONTROL:
        u_scale = uv_width_size / view_width
        v_scale = uv_height_size / view_height
        if middle_mouse_drag.modifiers == glfw.MOD_SHIFT:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale * 0.5
            zoom_state.center_v += middle_mouse_drag.dy * v_scale * 0.5
        else:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale
            zoom_state.center_v += middle_mouse_drag.dy * v_scale

    # 4c. Apply Zoom Logic (Zoom to Cursor)
    if zoom_delta != 0.0:
        zoom_factor = 1.0 + zoom_delta
        new_zoom = max(min_zoom, min(zoom_state.zoom * zoom_factor, max_zoom))

        if new_zoom != zoom_state.zoom:
            mouse_pos = imgui.get_mouse_pos()

            if io.key_ctrl:
                mouse_u_ratio, mouse_v_ratio = (0.5, 0.5)
            else:
                mouse_u_ratio = (mouse_pos[0] - p_min_x) / view_width
                mouse_v_ratio = (mouse_pos[1] - p_min_y) / view_height

            curr_uv_w = uv_width_size
            curr_uv_h = uv_height_size

            # Recalculate new UV dimensions
            if view_aspect > tex_aspect:
                new_uv_h = 1.0 / new_zoom
                new_uv_w = new_uv_h * (view_aspect / tex_aspect)
            else:
                new_uv_w = 1.0 / new_zoom
                new_uv_h = new_uv_w * (tex_aspect / view_aspect)

            diff_w = curr_uv_w - new_uv_w
            diff_h = curr_uv_h - new_uv_h

            zoom_state.center_u += diff_w * (mouse_u_ratio - 0.5)
            zoom_state.center_v += diff_h * (0.5 - mouse_v_ratio)

            zoom_state.zoom = new_zoom

            # Use these for Step 5
            uv_width_size = new_uv_w
            uv_height_size = new_uv_h

    # 5. Calculate Final UVs and Clamp to Bounds
    half_uv_w = uv_width_size * 0.5
    half_uv_h = uv_height_size * 0.5

    # --- Bounding Logic Start ---
    margin_px = 20.0

    pixel_u = uv_width_size / view_width
    pixel_v = uv_height_size / view_height
    margin_u = margin_px * pixel_u
    margin_v = margin_px * pixel_v

    min_u = -half_uv_w + margin_u
    max_u = 1.0 + half_uv_w - margin_u

    if min_u > max_u:
        zoom_state.center_u = 0.5
    else:
        zoom_state.center_u = max(min_u, min(zoom_state.center_u, max_u))

    min_v = -half_uv_h + margin_v
    max_v = 1.0 + half_uv_h - margin_v

    if min_v > max_v:
        zoom_state.center_v = 0.5
    else:
        zoom_state.center_v = max(min_v, min(zoom_state.center_v, max_v))
    # --- Bounding Logic End ---

    uv_x_min = zoom_state.center_u - half_uv_w
    uv_x_max = zoom_state.center_u + half_uv_w
    uv_y_min = zoom_state.center_v - half_uv_h
    uv_y_max = zoom_state.center_v + half_uv_h

    uv_a = (uv_x_min, uv_y_max)
    uv_b = (uv_x_max, uv_y_min)

    # 6. Project and Draw
    scale_u_px = view_width / uv_width_size
    scale_v_px = view_height / uv_height_size

    # Project Texture Edges
    raw_img_left = p_min_x + (0.0 - uv_x_min) * scale_u_px
    raw_img_right = p_min_x + (1.0 - uv_x_min) * scale_u_px
    raw_img_top = p_min_y + (uv_y_max - 1.0) * scale_v_px
    raw_img_bottom = p_min_y + (uv_y_max - 0.0) * scale_v_px

    # Intersect with Viewport
    clip_left = max(p_min_x, raw_img_left)
    clip_right = min(p_max[0], raw_img_right)
    clip_top = max(p_min_y, raw_img_top)
    clip_bottom = min(p_max[1], raw_img_bottom)

    draw_list: _DrawList = imgui.get_window_draw_list()

    if imgui.is_mouse_hovering_rect(clip_left, clip_top, clip_right, clip_bottom):
        draw_state.hover_reported = True
    else:
        draw_state.hover_reported = False

    Melty.push_clip((clip_left, clip_top, clip_right - 3, clip_bottom))
    draw_list.add_image_rounded(texture_id,
                                a=p_min,
                                b=p_max,
                                uv_a=uv_a,
                                uv_b=uv_b,
                                rounding=5.0)
    draw_list.add_rect(raw_img_left, raw_img_top, raw_img_right + 1, raw_img_bottom + 1,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       0.0, 0, 1.0)
    Melty.pop_clip()

    line_height = imgui.get_text_line_height()
    draw_list.add_text(max(p_min_x + 5, raw_img_left), clip_top - line_height - 5,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       text=f"{original_id} - {texture_id} - {width}x{height} - Zoom: {zoom_state.zoom:.2f}x")

    gl.glBindTexture(gl.GL_TEXTURE_2D, original_texture)

    return False, draw_state

    # draw_window(Melty.last_request_render, show_bg=True, name="Last Invalid")



@render_func(is_default_for=ManagedWindow, is_tree=False, show_name=False, use_cache=True, shadow=False,
             show_bg=False, selectable=False, show_add_delete=False, show_tint=False, wrap=False,
             with_header=draw_header)
def draw_managed_window(input_value, name, draw_state, style_manager, unique=0, mouse_down=False, **kwargs):
    try:
        window_draw_state = input_value.draw_state
    except Exception as e:
        imgui.text(f"Error accessing draw_state: {e}")
        return False, None

    window_input_value = input_value.input_value
    name = window_draw_state.name

    start_cursor = imgui.get_cursor_screen_pos()
    imgui.dummy(5, 20)
    imgui.same_line()

    if not window_draw_state.persistent and not window_draw_state.seen and window_draw_state.closed:
        Melty.delete_window(window_draw_state)

    window_tint = None

    if hasattr(window_input_value, 'tint') and window_input_value.tint is not None:
        changed, new_tint = draw_tuple(window_input_value.tint, name="")
        if changed:
            window_input_value.tint = new_tint
            window_draw_state.tint = window_input_value.tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_input_value.tint

    elif window_draw_state.tint is not None:
        changed, new_tint = draw_tuple(window_draw_state.tint, name="")
        if changed:
            window_draw_state.tint = new_tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_draw_state.tint

    imgui.same_line()

    if mouse_down:
        window_draw_state.closed = not window_draw_state.closed

    if name == "Window Manager":
        button(f"{name}", color=(0, 0, 0, 0),
               saturation=1.3, width=130)[0]
        return

    if window_draw_state.closed:
        if button(f"{name}", color=window_tint, z_offset=-2, value=0.1, factor=0.95, text_value=0.3,
                  saturation=1.2, width=draw_state.content_width - 60, height=30)[0]:
            window_draw_state.closed = False
    else:
        if button(f"{name}", saturation=1.5, z_offset=0, color=window_tint, factor=0.6, value=0.2, text_value=1.0,
                  width=draw_state.content_width - 60, height=30)[0]:
            window_draw_state.closed = True

    imgui.same_line()

    target_icon = ""  # Target icon (FontAwesome Unicode)
    if button(f"{target_icon}", width=22, height=22, color=window_tint, z_offset=-1, value=0.4, factor=0.9,
              saturation=0.2)[0]:
        this_window_right = draw_state.abs_left + draw_state.width
        from_zero_x = window_draw_state.abs_left - window_draw_state.window_pos[0]
        from_zero_y = window_draw_state.abs_top - window_draw_state.window_pos[1]
        window_draw_state.window_pos = (this_window_right + 10 - from_zero_x, draw_state.abs_top - from_zero_y)
        Melty.move_window_to_front(window_draw_state)
        Melty.cache.invalidate_up_by_obj(input_value)

    imgui.set_cursor_screen_pos(start_cursor)

    if window_draw_state.live:
        fa_live_icon = "\uf0e7"
        imgui.text_colored(fa_live_icon, 1.0, 0.0, 0.0)
        imgui.same_line()

    if imgui.is_item_hovered():
        imgui.begin_tooltip()
        draw_state = window_draw_state.to_dict()
        import json
        json_str = json.dumps(draw_state, indent=2)

        imgui.text(json_str)
        imgui.end_tooltip()
    # debug text


def draw(vis):
    draw_melty_windows(vis)


def export_code(test_param_2: int = 5):
    # print(f"hello {test_param_2}")
    global code_export_str
    code_export_str = proxy.node.code


@render_func(use_cache=False)
def draw_drag_drop_target(input_value, draw_state, on_drag, do_flow, depth,
                          collection, key, melty, y_offset, enable_flow, min_width,
                          unique, tag, style_manager, global_style, offset=0, indent_size=10):
    if Melty.active_layer == Melty.drag_layer:
        return False, 0.0

    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Melty.is_window_enabled():
        return False, 0.0

    if melty.initial_drag_offset is None:
        return False, 0.0

    if key is None:
        pass
    # ----------------- top spacing -----------
    falloff = 25.0  # Higher is gentler
    if enable_flow:
        drop_gap = 6.0
    else:
        drop_gap = 0.0

    drag_delta_curve = 1.0 - max(0.0, min(1.0, 1.0 - abs(melty.drag_delta[1] / 15.0)))

    mouse_pos = imgui.get_mouse_pos()
    cursor_top = imgui.get_cursor_screen_pos()[1]
    cursor_left = imgui.get_cursor_screen_pos()[0]
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
        if melty.dragged_item is None:
            melty.drag_in_progress = False

        elif id(melty.dragged_item._input_value) == id(collection):
            return False, 0.0

    if melty.drag_in_progress and do_flow and not on_drag and mouse_over_window:
        flow_spacing = drop_gap * bell_curve * drag_delta_curve
    else:
        flow_spacing = 0.0
        drag_delta_curve = 1.0

    if tag == "top":
        Melty.flow_spacing += (flow_spacing)
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + (flow_spacing))

    draw_list = imgui.get_window_draw_list()
    # if Melty.channels_split:
    #     draw_list.channels_set_current(min(Melty.max_depth - 1, depth + 2))

    # line_width = imgui.get_style().frame_padding.y * 2.0
    # color = style_manager.make_color_rgb(*(1.0, 1.0, 1.0), factor=1.0,
    #                                      value=1.0, alpha=1.0, saturation_scale=0.3)

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

            if Melty.channels_split:
                draw_list.channels_set_current(min(Melty.get_channel() + 1, Melty.max_depth - 1))

                if active_drop:
                    draw_list.channels_set_current(min(Melty.get_channel() + 2, Melty.max_depth - 1))
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
            # if draw_state.width == None:
            #     draw_state.width = min_width
            # if draw_state.left == None:
            #     draw_state.left = 1

            padding = imgui.get_style().frame_padding.x

            color = color if active_drop else inactive_color

            top = cursor_top - 1
            bottom = max(draw_state.abs_top, cursor_bottom - 1)
            left = draw_state.abs_left + offset
            right = draw_state.abs_left + draw_state.width - indent_size
            width = draw_state.width
            height = draw_state.height

            draw_list.add_rect_filled(left, top, right, bottom,
                                      col=imgui.get_color_u32_rgba(*color), rounding=4.0)

            if opacity > 0:
                Melty.cache.mask_mark_rect(draw_state, Melty.max_depth - 1, draw_state.shadow_depth, left, top, width,
                                           height,
                                           key=f"{left}x{top}_flow")
            #
            # draw_list.add_line(draw_state.left, draw_state.abs_top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.abs_top - 2 - offset,
            #                    col=imgui.get_color_u32_rgba(*color), thickness=3)

    return False, flow_spacing


@hotkey(glfw.KEY_O)
def toggle_offscreen():
    if Melty.cache.enabled:
        Melty.cache.set_enabled(False)
    else:
        Melty.cache.set_enabled(True)


import imgui


def draw_vertical_scrollbar(content_height: float,
                            view_height: float,
                            view_width: float,
                            scroll_offset: float,
                            scrollbar_width: float,
                            left: float = 0.0,
                            top: float = 0.0,
                            *,
                            pad: float = 0.0,
                            rounding: float = 3.0,
                            min_grab_size: float | None = None):
    # Style & colors
    style = imgui.get_style()
    if min_grab_size is None:
        min_grab_size = float(style.grab_min_size)

    col_track = imgui.get_color_u32_rgba(0, 0, 0, 0.1)
    col_grab = imgui.get_color_u32_rgba(1, 1, 1, 0.3)
    col_border = imgui.get_color_u32(imgui.COLOR_BORDER)

    # Early clamps & deriveds
    view_height = max(0.0, float(view_height))
    view_width = max(0.0, float(view_width))
    content_height = max(0.0, float(content_height))
    scrollbar_width = max(0.0, float(scrollbar_width))

    max_scroll = max(0.0, content_height - view_height)
    scroll_offset = float(max(0.0, min(scroll_offset, max_scroll)))

    # Anchor the container at the current cursor position in screen space
    origin_x, origin_y = (left, top)

    bar_margin = 4.0
    bar_margin_x = 1.0

    # Track geometry (stick it to the right edge of the container)
    track_w = min(scrollbar_width, view_width)
    track_h = view_height
    track_x1 = origin_x + (view_width - track_w) - bar_margin_x
    track_y1 = origin_y + bar_margin
    track_x2 = track_x1 + track_w - bar_margin_x
    track_y2 = track_y1 + track_h - bar_margin * 2

    # Compute grab size & position
    if content_height <= 0.0 or track_h <= 0.0:
        grab_h = 0.0
        t = 0.0
    else:
        # Proportional size with a minimum; cap to track height.
        ratio = view_height / content_height if content_height > 0.0 else 1.0
        grab_h = max(min_grab_size, ratio * track_h)
        grab_h = min(grab_h, track_h)

        # Normalized scroll position -> grab top
        travel = max(0.0, track_h - grab_h)
        t = 0.0 if max_scroll == 0.0 else (scroll_offset / max_scroll)
        t = max(0.0, min(1.0, t))  # clamp just in case

    grab_y1 = track_y1 + (max(0.0, track_h - grab_h) * t)
    grab_y2 = grab_y1 + grab_h

    # Inner padding for nicer visuals
    inner_x1 = track_x1 + pad
    inner_x2 = track_x2 - pad
    inner_y1 = track_y1 + pad
    inner_y2 = track_y2 - pad
    grab_x1 = inner_x1
    grab_x2 = inner_x2
    grab_y1 = max(inner_y1, min(grab_y1, inner_y2 - (grab_y2 - grab_y1)))
    grab_y2 = grab_y1 + max(0.0, min(grab_h, inner_y2 - inner_y1))

    # Draw
    dl = imgui.get_window_draw_list()
    # Track
    track_w = track_x2 - track_x1
    track_h = track_y2 - track_y1
    dl.add_rect_filled(track_x1, track_y1, track_x2, track_y2, col_track, rounding)
    # Melty.cache.mask_mark_rect(Melty.depth, track_x1, track_y1, track_w, track_h,
    #                            key=str(Melty.unique_stack[-1]) + "scrollbar")

    dl.add_rect(track_x1, track_y1, track_x2, track_y2, col_border, rounding)
    # Grab
    if grab_y2 > grab_y1 and grab_x2 > grab_x1:
        dl.add_rect_filled(grab_x1, grab_y1, grab_x2, grab_y2, col_grab, rounding)
        dl.add_rect(grab_x1, grab_y1, grab_x2, grab_y2, col_border, rounding)

    return {
        "offset": scroll_offset,
        "track_min": (track_x1, track_y1),
        "track_max": (track_x2, track_y2),
        "grab_min": (grab_x1, grab_y1),
        "grab_max": (grab_x2, grab_y2),
        "visible": content_height > view_height
    }


bg_style_default = {
    "value": 0.01,
    "saturation": 1.2,
    "alpha": 1.0,
    'max_value': 1.0
}


def get_bg_color(depth, rounding, global_style, style_manager, auto_resize):
    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))

    hovered_offset = 0.0

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    global bg_style_default
    bg_style = global_style.get_global_constant("bg_style", default=bg_style_default, folder="bg_styles")
    outline_factor = global_style.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    if not auto_resize:
        outline_factor *= 1.3

    if auto_resize:
        bleed_factor = 0.2
    else:
        bleed_factor = 0.0
    bg_bleed = Melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.6,
                                                alpha=1.0, saturation=1.8)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))
    bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)
    return bg_color


def seperator(height):
    imgui.dummy(0, snap_int(height / 2))
    imgui.separator()
    imgui.dummy(0, snap_int(height / 2))

def draw_bg(left=80, top=3, width=0, height=55, depth=0, rounding=4.016,
            global_style=None, outline=True, bg_color=None, opacity=-1.135,
            style_manager=None, tint=None, outline_tint=None, selected=False,
            hovered=False, pressed=False, nested_bg=False, **kwargs):

    # -- Constants ---------------------------------
    depth_wrap        = 34
    depth_scale       = 1.058
    corner_radius     = 4.0
    border_inset      = 1.548
    border_inset_half = 0.462
    stroke_width      = 2.738
    # How depth maps to color intensity
    intensity_factor  = 0.04
    intensity_offset  = 1.424\
    # Outline color tuning
    outline_base      = 1.914
    outline_depth_mul = 0.85
    outline_sat       = {'default': 1.752, 'nested': 3.211}

    # Beed color
    bleed_mix         = {'nested': 0.201, 'default': 0.454}
    bleed_style       = {'value': -0.111, 'alpha': 0.994, 'saturation': 5.459}
    outline_bleed_mix = 0.308

    # Hover offset per interaction state
    hover_offset_by_state = {
        'default':    -1.813,
        'selected':    -2.203,
        'pressed_hi': -2.288,   # pressed + opacity > 0.5
        'pressed_lo':  -0.371,
    }

    bg_style = {
        'value': 0.127, 'saturation': 2.808,
        'alpha': -2.072, 'max_value': 0.81,
    }
    

    # ── Helpers ────────────────────────────────────────────────
    def current_indent_px():
        return Melty.current_indent

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )
    # -- Depth calculation -------------------
    wrapped_depth = max(0.0, Melty.bg_depth) % depth_wrap
    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = max(0, (scaled_depth + intensity_offset) * intensity_factor)

    # ── Geometry ───────────────────────────────────────────────
    right = left + width
    bottom = top + height

    fill_rect = (
        snap_int(left) + border_inset,     snap_int(top) + border_inset,
        snap_int(right) - border_inset,    snap_int(bottom) - border_inset,
    )
    outline_rect = (

        snap_int(left) + border_inset_half,  snap_int(top) + border_inset_half,
        snap_int(right) - border_inset_half, snap_int(bottom) - border_inset_half,
    )

    # ── Interaction hover offset ───────────────────────────────
    hover_offset = hover_offset_by_state['default']
    if selected:
        hover_offset = hover_offset_by_state['selected']
    elif pressed:
        if opacity > 0.5:
            hover_offset = hover_offset_by_state['pressed_hi']
        else:
            hover_offset = hover_offset_by_state['pressed_lo']

    # ── Outline style ──────────────────────────────────────────
    sat = outline_sat['default']
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00
        sat = outline_sat['nested']

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = bleed_mix['nested'] if nested_bg else bleed_mix['default']

    bleed_base = Melty.get_bg_color(-1)
    bleed_color = style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Outline rendering ────────────────────────────────────
    outline_value = max(0, depth_intensity * depth_mul + outline_base + hover_offset)
    outline_color = style_manager.make_color_style_value(
        input=bg_style, saturation=sat, value=outline_value,
    )
    outline_color = mix_colors(outline_color, bleed_color, outline_bleed_mix)

    if outline:
        packed_outline = imgui.get_color_u32_rgba(*outline_color[:3], 1.0)
        if outline_tint is not None:
            packed_outline = imgui.get_color_u32_rgba(*outline_tint[:3], 1.0)
        imgui.get_window_draw_list().add_rect(
            *outline_rect, col=packed_outline, rounding=corner_radius, thickness=stroke_width,
        )



    # ── Fill rendering ─────────────────────────────────────────
    if bg_color is None:
        bg_color = style_manager.make_color_style_value(input=bg_style, value=max(0, depth_intensity))
        bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    packed_fill = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)
    if tint is not None:
        packed_fill = imgui.get_color_u32_rgba(*tint[:3], opacity)

    if opacity > 0.0:
        imgui.get_window_draw_list().add_rect_filled(*fill_rect, col=packed_fill, rounding=corner_radius)

    return False, bg_color




@render_func(use_cache=True, shadow=True, selectable=False, show_bg=False, min_width=10,
             min_height=10, wrap=True)
def button(input_value="", corner_radius=4, draw_state=None, left_mouse_held=False, left_mouse_down=False,
           color=(0.5, 0.5, 0.5), hovered=False, width=None, height=None, style_manager=None,
           factor=1.0, value=0.4, text_value=1.0, saturation=0.8, unique=0):
    if color is not None:
        if left_mouse_held:
            draw_state.z_offset = -2.0
        else:
            draw_state.z_offset = 3.0

        if hovered:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2],
                                                       value=value + 0.05, factor=factor, saturation_scale=saturation,
                                                       alpha=1.0)
        else:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2],
                                                       value=value, factor=factor, saturation_scale=saturation,
                                                       alpha=1.0)
        text_color = style_manager.make_color_rgb(color[0], color[1], color[2],
                                                  value=text_value, factor=factor, saturation_scale=0.4, alpha=1.0)
    else:
        text_color = (1.0, 1.0, 1.0)
        mixed_color = (0, 0, 0)

    draw_state.corner_radius = corner_radius
    min_size = imgui.calc_text_size(input_value)
    width = max(min_size[0] + 15, width or 0)
    height = max(min_size[1], height or 0)
    imgui.dummy(width, height)
    draw_list: _DrawList = imgui.get_window_draw_list()
    draw_list.add_rect_filled(draw_state.abs_left, draw_state.abs_top, draw_state.abs_left + width,
                              draw_state.abs_top + height, imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                              rounding=corner_radius)

    text_size = imgui.calc_text_size(input_value)

    draw_list.add_text(draw_state.abs_left + (width - text_size[0]) / 2.0 + 2,
                       draw_state.abs_top + (height - text_size[1]) / 2.0 - 1,
                       imgui.get_color_u32_rgba(*text_color[:3], 1.0), input_value)

    if left_mouse_down:
        request_render()
        return True, input_value

    return False, input_value


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




@render_func(header_same_line=True, is_default_for=(NoneType), shadow=False, is_tree=False, with_header=draw_header)
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text("None")
    return False, input_value


@render_func(is_default_for=(bool), header_same_line=True, use_cache=False, is_tree=False,
             shadow=False, with_header=draw_header)
def draw_bool(input_value: bool):
    changed, is_checked = imgui.checkbox("##bool", input_value)
    if changed:
        return True, is_checked
        
    return False, None



@render_func(is_default_for=(str), shadow=False, show_bg=False, wrap=False, is_tree=False,
             show_add_delete=False, use_cache=False, disable_scroll=True, with_header=draw_header)
def draw_label(input_value: str, draw_state):
    text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
    imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
    imgui.text_wrapped(str(input_value))
    imgui.pop_text_wrap_pos()

    return False, input_value



@render_func(is_default_for=(str), shadow=False, show_bg=False, wrap=False, is_tree=False,
             show_add_delete=False, use_cache=False, disable_scroll=True, with_header=draw_header)
def draw_str(input_value: str, draw_state, editable=True, alpha=1.0):
    if not editable:
        imgui.push_style_var(imgui.STYLE_ALPHA, alpha)

        text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.text_wrapped(str(input_value))
        imgui.pop_text_wrap_pos()

        imgui.pop_style_var(1)
        return False, input_value


    line_count = input_value.count('\n') + 1
    line_height = imgui.get_text_line_height()
    text_height = imgui.calc_text_size(str(input_value))[1] + line_height * 2
    if line_count == 1:
        padding = imgui.get_style().frame_padding.y
        height = imgui.get_text_line_height() + padding

    else:
        text_bottom = draw_state.abs_top + text_height
        clamped_bottom = text_bottom
        height = clamped_bottom - draw_state.abs_top

    show_controls = True

    if not show_controls:
        imgui.push_style_var(imgui.STYLE_ALPHA, 0)

    if line_count == 1:
        imgui.set_next_item_width(draw_state.content_width)
        changed, value = imgui.input_text("##str", str(input_value),
                                          flags=imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    else:
        imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
        # disable scrolling
        changed, value = draw_text(str(input_value), editable=True, with_header=draw_header, show_name=False, is_tree=False)
        imgui.dummy(draw_state.content_width, text_height - height + 10)


    if not show_controls:
        imgui.pop_style_var(1)

    if changed:
        return True, value
    return changed, value


@render_func(is_default_for=(Comment), shadow=False, with_header=None, is_tree=False, tint=(0.2, 0.2, 0.1))
def draw_comment(input_value: Comment, draw_state, cursor_hover=False):
    line_height = imgui.get_text_line_height()
    changed, value = False, input_value
    help_yellow= (0.8, 0.8, 0.3)
    help_icon = "\u2753"
    draw_list: _DrawList = imgui.get_window_draw_list()
    character_width = imgui.calc_text_size(help_icon)[0]
    # Draw circle background for comment
    radius = 18 / 2
    center_x = draw_state.abs_left + radius
    center_y = draw_state.abs_top + radius
    color = imgui.get_color_u32_rgba(*help_yellow, 0.3)
    imgui.dummy(min(max(30, 30), 300), radius * 2)
    cursor_hover = imgui.is_item_hovered()
    draw_list.add_circle_filled(center_x, center_y, radius, color)
    draw_list.add_text(center_x - character_width / 2, center_y - line_height / 2,
                       imgui.get_color_u32_rgba(0.8, 0.8, 0.7, 1.0), help_icon)

    if cursor_hover:
        popup_max_width = 300
        text_size = imgui.calc_text_size(str(input_value), wrap_width=popup_max_width)
        popup_width = popup_max_width

        imgui.set_cursor_screen_pos((draw_state.abs_left + radius * 2 + 5, draw_state.abs_top))
        draw_window(str(input_value), editable=False, window_pos=(0,0), width=popup_width, height=text_size[1] + 5,
                    with_header_end=None, with_header=None, with_footer=None)
    imgui.same_line(spacing=0)
    draw_str(str(input_value[1:]), alpha=0.2, selectable=False, editable=False, is_tree=False, with_header=None, show_name=False)


    if changed:
        return True, value
    return changed, value


@render_func(is_default_for=('tint'), has_popup=True, indent_size=0, is_tree=False,
             show_name=True, selectable=False,
             use_cache=False, with_header=draw_header)
def draw_tuple(input_value: tuple, unique):
    if len(input_value) > 0 and isinstance(input_value[0], (float, int)):
        if len(input_value) == 4:
            # imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (4, 0))
            # imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))

            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit4(
                f"##picker_edit{unique}",
                color_list[0], color_list[1], color_list[2], color_list[3],
                flags=color_flags)

            # imgui.pop_style_var(2)
            if changed:
                input_value = (color[0], color[1], color[2], color[3])
        elif len(input_value) == 3:
            # imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (4, 0))
            # imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))

            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL |
                           imgui.COLOR_EDIT_NO_ALPHA | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit3(
                f"##picker_edit{unique}",
                color_list[0], color_list[1], color_list[2],
                flags=color_flags)

            # imgui.pop_style_var(2)
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


class TestClass(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 2
        self.str_val = "Test"


@render_func
def draw_float_ctx(input_value):
    imgui.text('Float content menu')
    imgui.dummy(30, 30)
    draw_float(0.0, name="test")
    imgui.text(f"WxH {input_value.width} {input_value.height}")
    imgui.text(f"Content width {input_value.content_width} {input_value.height}")

    imgui.text(f"Abs Left/Top {input_value.abs_left} {input_value.abs_top}")
    imgui.text(f"Header WxH {input_value.header_width} {input_value.header_height}")

    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_rect(upper_left_x=input_value.abs_left, upper_left_y=input_value.abs_top,
                       lower_right_x=input_value.abs_left + input_value.width,
                       lower_right_y=input_value.abs_top + input_value.height,
                       col=imgui.get_color_u32_rgba(1, 0, 0, 0.5), thickness=1.0)



@render_func(is_default_for=float, use_cache=False, shadow=False, window_pos=(0,0), show_bg=False, wrap=False, is_tree=False,
             with_header=draw_header, with_header_end=draw_header_end)
def draw_float(input_value: float, draw_state, min_value=-100.0, max_value=100.0, speed=0.001):
    imgui.set_next_item_width(min(600, max(30, draw_state.content_width)))
    changed, value = imgui.drag_float("", input_value,
                                      format='%.3f',
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)

    if changed:
        return True, value


@render_func(is_default_for=(Parameter), wraps=render_func, with_header=draw_header)
def draw_parameter(input_value):
    parameter_default = input_value.default
    if parameter_default is inspect.Parameter.empty:
        imgui.same_line()
        imgui.text("<No Default>")
    else:
        return draw_any(parameter_default, show_name=False, show_add_delete=False)


@render_func(is_default_for=(types.MappingProxyType), show_add_delete=False, with_header=draw_header)
def draw_mapping_proxy(input_value):
    # To list first, then back to mapping proxy
    try:
        dict_values = dict(input_value)
        changed, new_dict = draw_collection(dict_values, show_bg=False, indent_size=0, show_header=False,
                                            show_add_delete=False)
        if changed:
            return True, types.MappingProxyType(new_dict)

    except Exception as e:
        imgui.text(f"Error converting MappingProxyType to dict: {e}")
        return False, input_value

    return changed, input_value


@render_func(wraps=render_func, show_add_delete=False, with_header=draw_header)
def eval_function(input_value, draw_state):
    signature = inspect.signature(input_value)
    params = signature.parameters
    changed, new_val = draw_any(params, name="Parameters", show_add_delete=False)
    if changed:
        set_fn_defaults(input_value, new_val)

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))
    push_style_var(imgui.STYLE_FRAME_PADDING, (8, 6))
    push_style_var(imgui.STYLE_FRAME_ROUNDING, 6)

    function_args = inspect.signature(input_value).parameters
    kwargs = {}
    for name, param in function_args.items():
        if param.default is not inspect.Parameter.empty:
            kwargs[name] = param.default
        else:
            kwargs[name] = None
    try:
        result = input_value(**kwargs)
        draw_any(result, name="Result", show_header=True, show_add_delete=False)
        if draw_state._result != result:
            Melty.cache.invalidate_all()
        draw_state._result = result

    except Exception as e:
        print(f"Error calling function '{input_value.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())

    pop_style_var(3)

    return changed, input_value


@render_func(is_default_for="LSDStudio", show_bg=True, tint=(0.6, 0.2, 0.8), with_header=draw_header)
def draw_vis(input_val):
    imgui.text("An LSD Studio Instance")


@render_func(is_default_for="ImGuiStyleManager", show_bg=True, tint=(0.7, 0.7, 0.1), with_header=draw_header)
def draw_vis(input_val):
    imgui.text("Style Manager")


@render_func(is_default_for="AppModel", show_bg=True, tint=(0.6, 0.2, 0.8), with_header=draw_header)
def draw_app_model(input_val):
    imgui.text("An App Model Instance")


@render_func(is_default_for=(types.FunctionType, types.MethodType), show_add_delete=False,
             show_bg=True, parent_show_add_delete=False,
             is_tree=False, show_name=False, with_header=draw_header)
def draw_function(input_value, name, draw_state, unique):
    if not callable(input_value):
        imgui.text("Not a callable function")
        return False, input_value
    signature = inspect.signature(input_value)
    params = signature.parameters
    if len(draw_state.params) != len(params):
        param_dict = {}
        for name, param in params.items():
            if name == 'kwargs':
                continue
            if param.default is not inspect.Parameter.empty:
                param_dict[name] = param.default
            else:
                param_type = param.annotation
                default_value = param.default
                if default_value is not inspect.Parameter.empty:
                    param_dict[name] = default_value
                else:
                    if name in Melty.global_attrs:
                        param_dict[name] = Melty.global_attrs[name]

        draw_state.params = param_dict
    if len(draw_state.params) > 0:
        changed, new_val = draw_collection(draw_state.params, name="Parameters", show_add_delete=False, horizontal=True, child_kwargs={"wrap":True, "show_bg":True})
        if changed:
            draw_state.params = new_val

    # push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))
    # push_style_var(imgui.STYLE_FRAME_PADDING, (6, 6))
    # push_style_var(imgui.STYLE_FRAME_ROUNDING, 4)

    if imgui.button(f"{input_value.__name__}##{unique}"):
        try:
            draw_state.result = input_value(**draw_state.params)
            Melty.cache.invalidate_up_current(force=True)
        except Exception as e:
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback(*sys.exc_info())

    if draw_state.result is not None:
        draw_any(draw_state.result, name="Result", header_same_line=True, show_header=False, show_add_delete=False)

    # pop_style_var(3)

    return False, input_value


@render_func(is_default_for=(int), shadow=False,
             is_tree=False, wrap=True, header_same_line=True,
             with_header=draw_header)
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05, unique=0):
    int_text_width = imgui.calc_text_size(str(input_value))[0]
    imgui.set_next_item_width(int_text_width + 20)

    max_int = 2147483647
    if input_value < max_int:
        changed, value = imgui.drag_int("##int", input_value,
                                        change_speed=speed,
                                        min_value=min_value,
                                        max_value=max_value)
        if changed:
            return True, value

        return changed, value
    return False, input_value


@render_func(show_header=False, show_name=False, show_bg=True, with_header=draw_header)
def draw_debug_label(input_value: str):
    imgui.text(input_value)


@render_func(is_default_for=Enum, show_add_delete=False, is_tree=False, shadow=False, header_same_line=True, parent_show_add_delete=False, with_header=draw_header)
def draw_enum(input_value: Enum, global_style=None,  style_manager=None, enum_tint=(0.3, 0.3, 0.3)):
    unique = "enum"
    # imgui.set_next_item_width(imgui.get_content_region_available().x)
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
            print(f"Selected enum option: {option}")
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


def draw_debug(x,y, label, color=(1, 0, 0), size=16):
    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_circle_filled(x, y, size, imgui.get_color_u32_rgba(*color, 1.0))
    draw_list.add_text(x + size + 2, y - size / 2, imgui.get_color_u32_rgba(*color, 1.0), label)


# @render_func(is_default_for=(FileWatch))
# def draw_file_watch(input_value: FileWatch):
#     imgui.text(f"Watching: {input_value._path}")
#     imgui.text(f"Size: {input_value.size} bytes")
#     imgui.text(f"Last Modified: {input_value.modified_time}")
#
#     return False, None
@render_func(with_header=draw_header, use_cache=True, is_tree=False)
def default_context_menu(input_value, draw_state, cursor_hover_inverted, func, **kwargs):
    # imgui.text(type(input_value._input_value).__name__)

    def draw_overlay_rect(rect, color=(1, 0, 0, 0.5), name=None):

        draw_list: _DrawList = imgui.get_overlay_draw_list()
        if name is not None:
            name_size = imgui.calc_text_size(name)
            draw_list.add_text(rect[2] - name_size[0] - 4,
                               rect[1] + 2,
                               imgui.get_color_u32_rgba(*color), name)

        draw_list.add_rect(rect[0], rect[1], rect[2], rect[3], imgui.get_color_u32_rgba(*color), thickness=1.0, rounding=4)


    draw_str(f"{str(input_value._kwargs.get('mode', None))}", name="mode", editable=False, column=0)
    draw_str(f"{str(input_value.content_height)}", name="content_height", editable=False,column=0)
    draw_str(f"{str(input_value.height)}", name="height", editable=False,column=0)
    draw_any(input_value._source, name="Height source", editable=False, column=0)

    draw_str(f"{input_value.scroll_visible}", name="scroll_visible", column=0)
    draw_str(f"{input_value._kwargs.get('disable_scroll', False)}", name="disable_scroll", column=0)
    draw_str(str(input_value.scroll_offset), name="scroll_offset", column=0)
    draw_str(f"Clip Rect {str(input_value.clip_rect)}", name="clip_rect", column=0)

    # draw_collection(draw_state._all_pending, name="All Pending", column=1, fill_height=True)

    # imgui.new_line()
    # imgui.separator()

    changed, value = draw_bool(input_value._print_last_invalid, name="Print Invalid", column=0)
    if changed:
        input_value._print_last_invalid = value

    if input_value._last_invalidate is not None:
        if input_value._print_last_invalid:
            print_stack_trace(frames=input_value._last_invalidate)

    if input_value.explain_convert is not None:
        draw_str(str(input_value.explain_convert), name="explain_convert", column=0)

    if Toggles.debug_context_menu:
        if imgui.is_mouse_hovering_rect(draw_state.abs_left, draw_state.abs_top,
                                        draw_state.abs_left + draw_state.width,
                                        draw_state.abs_top + draw_state.height):
            draw_list = imgui.get_overlay_draw_list()
            rect = (input_value.abs_left, input_value.abs_top, input_value.abs_left + input_value.width,
                    input_value.abs_top + input_value.height)
            draw_overlay_rect(rect, color=(1, 1, 0, 0.5))
            draw_overlay_rect(input_value.clip_rect, color=(0, 1, 0, 0.5), name="clip")

            draw_debug(input_value.abs_left, input_value.abs_top, "Abs Left/Top", color=(1, 0, 0), size=8)

    draw_str(input_value._kwargs["func"].__name__, name="view_func", column=0)
    from src.lsd.gl_gui.view.mode import Mode
    draw_any(input_value._kwargs["func"], column=1, mode=Mode.CODE_PLAIN_TEXT, name="Render Function")

    draw_str(str(type(input_value._raw_input_value)), name="Input Type", column=0)
    draw_any(type(input_value._raw_input_value), column=2, mode=Mode.CODE_PLAIN_TEXT, name="Input Type")

    return False, None


@render_func(use_cache=True, with_header=draw_header, show_bg=True, is_default_for=Pending)
def draw_pending(input_value, draw_state=None):
    imgui.text(input_value.originated.__name__)
    imgui.push_text_wrap_pos(draw_state.left + draw_state.content_width)
    imgui.text_wrapped(str(input_value.status))
    imgui.pop_text_wrap_pos()

    return False, None


from src.lsd.gl_gui.model.core_model.core_enums import PendingAction
from src.lsd.gl_gui.view.core_conversion.search_conversion import SearchResults


@render_func(is_default_for=SearchResults, use_cache=True, show_bg=True,
             with_header=draw_header, indent_size=5)
def draw_search_results(input_value: SearchResults, draw_state=None):
    """Render search results — shows top_results via draw_collection."""
    top_results = input_value.get("top_results", {})
    imgui.text("Found {} results".format(len(top_results)))
    changed, new_top = draw_collection(top_results, name="results",
                                        show_add_delete=False)
    if changed:
        input_value["top_results"] = new_top
        return True, input_value
    return False, input_value


@render_func(use_cache=True, show_header=False, shadow=True)
def pending_window(input_value, button_name, pending=None, draw_state=None,
                   show_revert=False, show_load=False):
    draw_text(str(pending.status), width=draw_state.width, name="Status", show_bg=True, shadow=False, with_footer=None)
    imgui.dummy(0, 5)

    if button(str(button_name), width=100, height=25)[0]:
        return True, PendingAction.APPLY
    if show_revert:
        same_line()
        if button("Revert", width=100, height=25, color=(0.8, 0.3, 0.3), factor=0.3, value=0.0, text_value=2.0, saturation=0.4)[0]:
            return True, PendingAction.REVERT
    if show_load:
        same_line()
        if button("Load", width=100, height=25, color=(0.3, 0.5, 0.8), factor=0.8)[0]:
            return True, PendingAction.LOAD
            
    return False, None

@render_func(use_cache=True, show_header=True, selectable=False, with_header=draw_header)
def draw_single(input_value:any, view_func=None, mode:any=None, **kwargs):
    changed, return_val = view_func(input_value, mode=mode)
    return changed, return_val


def draw_any(input_value:any, view_func=None, mode:any=None, **kwargs):
    # # meta selection
    kwargs_view_func = view_func
    if view_func is None:
        meta = kwargs.get("meta", None)
        if meta is None:
            if hasattr(Meta, 'get_child_meta'):
                meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)
        if meta.view_function is None or 'draw_any' in meta.view_function.__name__:
            meta.view_function = draw_collection
        view_func = meta.view_function
        kwargs_view_func = meta.view_function

    if mode is None:
        mode = Melty.mode_stack[-1] if len(Melty.mode_stack) > 0 else None

    if isinstance(mode, tuple) and len(mode) > 0:
        main_mode = mode[0]
    else:
        main_mode = mode

    # --- Search: inject search converters BEFORE the routing ---
    # This way the routing sees SearchResults as the output type and
    # routes to draw_search_results instead of draw_collection.
    from src.lsd.gl_gui.view.core_conversion.search_conversion import SearchResults
    _consumed_search = None
    if (len(Melty.search_stack) > 0
            and getattr(kwargs_view_func, '_searchable', False)
            and not isinstance(input_value, SearchResults)):
        from src.lsd.gl_gui.view.core_conversion.search_conversion import (
            dict_to_search, search_to_dict, str_to_search, search_to_str,
        )
        import inspect as _inspect

        # Determine what the convert_in chain produces (or the raw input type)
        existing_in = kwargs.get("convert_in", None)
        if existing_in is not None:
            _last = existing_in[-1]
            _out_type = _inspect.signature(_last).return_annotation
        else:
            _out_type = type(input_value)

        if _out_type is not None and isinstance(_out_type, type) and issubclass(_out_type, dict):
            kwargs["convert_in"] = list(existing_in or []) + [dict_to_search]
            kwargs["convert_out"] = [search_to_dict] + list(kwargs.get("convert_out", None) or [])
            kwargs["search_text"] = Melty.search_stack[-1]
            from src.lsd.gl_gui.view.mode import Mode
            main_mode = Mode.SEARCH
            _consumed_search = Melty.search_stack.pop()
        elif _out_type == str:
            from src.lsd.gl_gui.view.mode import Mode
            main_mode = Mode.SEARCH
            kwargs["convert_in"] = list(existing_in or []) + [str_to_search]
            kwargs["convert_out"] = [search_to_str] + list(kwargs.get("convert_out", None) or [])
            kwargs["search_text"] = Melty.search_stack[-1]
            _consumed_search = Melty.search_stack.pop()

    if main_mode is not None:
        # Loop over super types
        mode_config = main_mode.get_config_for(input_value)
        if mode_config is not None and mode_config.func is None and (
                mode_config.kwargs.get("convert", None) is not None
                or mode_config.kwargs.get("convert_in", None) is not None):
            convert_in = mode_config.kwargs.get("convert_in", None)
            if convert_in is not None:
                # Infer target type from the return annotation of the last converter
                import inspect
                last_fn = convert_in[-1]
                ret = inspect.signature(last_fn).return_annotation
                convert_to_type = ret if ret is not inspect.Parameter.empty else None
            else:
                convert_to_type = mode_config.kwargs["convert"][-1]
            mode_config = main_mode.get_config_for(the_type=convert_to_type) if convert_to_type is not None else None
            if mode_config is not None and mode_config.func is not None:
                view_func = draw_single
                kwargs_view_func = mode_config.func

        elif mode_config is not None and mode_config.func is not None:
            view_func = mode_config.func
            kwargs_view_func = view_func


    kwargs['use_cache'] = True
    kwargs['mode'] = mode
    kwargs['view_func'] = kwargs_view_func

    return_val = view_func(input_value, **kwargs)

    # Restore search stack so siblings at the same level can also search
    if _consumed_search is not None:
        Melty.search_stack.append(_consumed_search)

    return return_val

