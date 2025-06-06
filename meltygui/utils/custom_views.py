import inspect
import os
import threading
import traceback
from collections import defaultdict
from enum import Enum
from typing import Dict

import glfw
import imgui
import psutil

from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.lsd_utils import singleton


class GroupType(Enum):
    WINDOW = 0
    CHILD = 1
    FRAME = 2
    STYLE = 3
    COLOR = 4

@singleton
class LSDView:

    def __init__(self):
        self.group_stack = []
        self.style_stack = []
        self.color_stack = []
        self.style_manager = None

    def set_style_manager(self, style_manager):
        """
        Set the style manager for this view.
        :param style_manager: The style manager to set.
        """
        self.style_manager = style_manager

    def unstack_group(self):
        try:
            for group_type in reversed(self.style_stack):
                if group_type == GroupType.WINDOW:
                    imgui.end()
                elif group_type == GroupType.CHILD:
                    imgui.end_child()
                elif group_type == GroupType.FRAME:
                    imgui.end_frame()
                elif group_type == GroupType.STYLE:
                    imgui.pop_style_var(1)
                elif group_type == GroupType.COLOR:
                    imgui.pop_style_color(1)
        except Exception as e:
            print(f"Error unstacking styles: {e}")
            print(self.style_stack)
            print_colored_traceback(*sys.exc_info(), limit=50)

            self.style_stack.clear()

        try:
            for group_type in reversed(self.color_stack):
                if group_type == GroupType.WINDOW:
                    imgui.end()
                elif group_type == GroupType.CHILD:
                    imgui.end_child()
                elif group_type == GroupType.FRAME:
                    imgui.end_frame()
                elif group_type == GroupType.STYLE:
                    imgui.pop_style_var(1)
                elif group_type == GroupType.COLOR:
                    imgui.pop_style_color(1)
        except Exception as e:
            print(f"Error unstacking colors: {e}")

            print(self.color_stack)
            print_colored_traceback(*sys.exc_info(), limit=50)

            self.color_stack.clear()

        try:
            for group_type in reversed(self.group_stack):
                if group_type == GroupType.WINDOW:
                    imgui.end()
                elif group_type == GroupType.CHILD:
                    imgui.end_child()
                elif group_type == GroupType.FRAME:
                    imgui.end_frame()
                elif group_type == GroupType.STYLE:
                    imgui.pop_style_var(1)
                elif group_type == GroupType.COLOR:
                    imgui.pop_style_color(1)
        except Exception as e:
            print(f"Error unstacking group: {e}")
            # Print out the stack
            print_colored_traceback(*sys.exc_info(), limit=50)

            print(self.group_stack)

            self.group_stack.clear()


        self.style_stack.clear()
        self.group_stack.clear()

def list_width(str_list):
    max_width = 0
    for string in str_list:
        width = imgui.calc_text_size(string).x
        if width > max_width:
            max_width = width
    return max_width

def radio_buttons_enum(vis, name, selected_enum: RelaxedEnum, label_width=0, grey_out=False):
    imgui.set_next_item_width(imgui.get_content_region_available().x)
    selected_idx = 0
    visible_name = name.split("##")[0]
    changed = False

    if grey_out:
        push_style_var(imgui.STYLE_ALPHA, 0.5)

    left_edge = imgui.get_cursor_pos_x()
    margin = imgui.get_style().item_spacing.x * 2
    if len(visible_name) > 0:
        imgui.text(visible_name)
        imgui.same_line()
        if label_width > 0:
            imgui.set_cursor_pos_x(left_edge + label_width + margin)

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 5))
    for i, option in enumerate(selected_enum.__class__):
        pretty_name = option.name.replace("_", " ").capitalize()
        if vis.square_radio_button(f"{pretty_name}##{name.split('##')[1]}", selected_enum.value == i):
            selected_idx = i
            changed = True
        imgui.same_line()
    imgui.new_line()
    enum_class = selected_enum.__class__
    selected_enum = enum_class(selected_idx)
    pop_style_var(1)

    if grey_out:
        pop_style_var(1)

    return changed, selected_enum


def radio_buttons(vis, name, options, selected_idx):
    imgui.set_next_item_width(imgui.get_content_region_available().x)
    changed = False

    visible_name = name.split("##")[0]
    if len(visible_name) > 0:
        imgui.text(visible_name)
    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 5))
    for i, option in enumerate(options):
        if vis.square_radio_button(f"{option}##{name}", selected_idx == i):
            selected_idx = i
            changed = True
        imgui.same_line()
    imgui.new_line()

    pop_style_var(1)
    return changed, selected_idx


def radio_buttons_str(vis, name, options, selected_str):
    imgui.set_next_item_width(imgui.get_content_region_available().x)
    changed = False

    visible_name = name.split("##")[0]
    if len(visible_name) > 0:
        imgui.text(visible_name)
    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 5))
    for i, option in enumerate(options):
        if vis.square_radio_button(f"{option}##{name}{i}", selected_str == option):
            selected_str = option
            changed = True
            print(f"Selected: {selected_str}")
        imgui.same_line()
    imgui.new_line()
    pop_style_var(1)
    return changed, selected_str


def button(text, width=0, height=0):
    return imgui.button(text, width=width, height=height)


def delete_button(text, width=0, height=0, white_text=True):
    push_style_var(imgui.STYLE_FRAME_BORDERSIZE, 2)

    if LSDView().style_manager is not None:
        if white_text:
            r, g, b, a = LSDView().style_manager.make_color_rgb(0.5, 0.0, 0.0)
            push_style_color(imgui.COLOR_BUTTON, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(0.55, 0.0, 0.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BORDER, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(0.75, 0.0, 0.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BUTTON_HOVERED, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(0.9, 0.0, 0.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BUTTON_ACTIVE, r, g, b)

        else:
            r, g, b, a = LSDView().style_manager.make_color_rgb(1.0, 1.0, 1.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BUTTON, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(0.55, 0.0, 0.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BORDER, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(0.75, 0.0, 0.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BUTTON_HOVERED, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(0.9, 0.0, 0.0, saturation_scale=1.0)
            push_style_color(imgui.COLOR_BUTTON_ACTIVE, r, g, b)

            r, g, b, a = LSDView().style_manager.make_color_rgb(1.0, 0.1, 0.1, saturation_scale=1.0)
            push_style_color(imgui.COLOR_TEXT, r, g, b)


    else:
        push_style_color(imgui.COLOR_BUTTON, 0.5, 0.2, 0.2)
        push_style_color(imgui.COLOR_BUTTON, 0.55, 0.2, 0.2)
        push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.9, 0.3, 0.3)
        push_style_color(imgui.COLOR_BUTTON_ACTIVE, 1.0, 0.4, 0.4)

    original_cursor_pos = imgui.get_cursor_pos()
    offset_width = width

    if text.split("##")[0] == "":
        val = imgui.button(f"\uf1f8{text}", width=width, height=height)
    else:
        val = imgui.button(f"\uf1f8 {text}", width=width, height=height)

    if white_text:
        pop_style_color(4)
    else:
        pop_style_color(5)

    pop_style_var(1)

    return val

def button_red(text, width=0, height=0):
    push_style_color(imgui.COLOR_BUTTON, 0.6, 0.2, 0.2)
    push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.6, 0.3, 0.4)
    push_style_color(imgui.COLOR_BUTTON_ACTIVE, 1.0, 0.4, 0.4)
    push_style_color(imgui.COLOR_TEXT, 1.0, 1.0, 1.0)
    val = imgui.button(text, width=width, height=height)
    pop_style_color(4)
    return val

# noinspection PyArgumentList
def tree(text, open=True):
    ## Returns 'true' if the node is drawn
    if open:
        flags = imgui.TREE_NODE_DEFAULT_OPEN | imgui.TREE_NODE_COLLAPSING_HEADER
    else:
        flags = imgui.TREE_NODE_COLLAPSING_HEADER

    return imgui.tree_node(text, flags=flags)

def print_stack_trace(size=None):
    # Get the current stack frame info
    stack = traceback.extract_stack()

    # Format and print the stack trace (excluding this function call)
    if size is None:
        formatted_stack = traceback.format_list(stack[:-1])
    else:
        formatted_stack = traceback.format_list(stack[-size:-1])

    for frame in formatted_stack:
        print(frame, end='')  # end='' to avoid extra newlines


_needs_render = threading.Event()


def request_render():
    _needs_render.set()
    glfw.post_empty_event()


def does_need_render():
    return _needs_render.is_set()


def new_frame():
    LSDView().group_stack.append(GroupType.FRAME)
    return imgui.new_frame()


def end_frame():
    if LSDView().group_stack[-1] == GroupType.FRAME:
        LSDView().group_stack.pop()
        return imgui.end_frame()
    else:
        print_stack_trace()
        print("Error: end_frame() called without matching new_frame()")
        return imgui.end_frame()


def begin_child(signatures, *args, **kwargs):
    LSDView().group_stack.append(GroupType.CHILD)
    return imgui.begin_child(signatures, *args, **kwargs)


def end_child():
    if LSDView().group_stack[-1] == GroupType.CHILD:
        LSDView().group_stack.pop()
        return imgui.end_child()
    else:
        print_stack_trace()
        print("Error: end_child() called without matching begin_child()")
        return imgui.end_child()


def begin(str_label, closable=False, flags=0):
    LSDView().group_stack.append(GroupType.WINDOW)
    return imgui.begin(str_label, closable, flags)


def end():
    if LSDView().group_stack[-1] == GroupType.WINDOW:
        LSDView().group_stack.pop()
        return imgui.end()
    else:
        print_stack_trace()
        print("Error: end() called without matching begin()")
        return imgui.end()


def push_style_color(ImGuiCol_variable, float_r, float_g, float_b, float_a=1.):
    LSDView().color_stack.append(GroupType.COLOR)
    return imgui.push_style_color(ImGuiCol_variable, float_r, float_g, float_b, float_a)


def pop_style_color(size=1):
    for _ in range(size):
        if LSDView().color_stack[-1] == GroupType.COLOR:
            LSDView().color_stack.pop()
            imgui.pop_style_color(1)
        else:
            print("Error: pop_style_color() called without matching push_style_color()")
            # Print stack trace to help debug
            print_stack_trace()
            imgui.pop_style_color(1)

def push_style_var(ImGuiStyleVar_variable, value):
    LSDView().style_stack.append(GroupType.STYLE)
    return imgui.push_style_var(ImGuiStyleVar_variable, value)

def pop_style_var(size=1):
    for _ in range(size):
        if LSDView().style_stack[-1] == GroupType.STYLE:
            LSDView().style_stack.pop()
            imgui.pop_style_var(1)
        else:
            print_stack_trace()
            print(f"Error: {LSDView().style_stack[-1]} pop_style_var() called without matching push_style_var()")
            imgui.pop_style_var(1)


import sys
import traceback
import re

# ANSI color codes inspired by IntelliJ IDEA's default color scheme
COLORS = {
    # Structural elements
    'HEADER': '\033[95m',
    'RESET': '\033[0m',
    'BOLD': '\033[1m',
    'UNDERLINE': '\033[4m',

    # IntelliJ-like syntax colors
    'KEYWORD': '\033[38;5;204m',  # Pink/purple for keywords like def, class, import
    'METHOD': '\033[38;5;75m',  # Blue for method names
    'STRING': '\033[38;5;113m',  # Green for strings
    'NUMBER': '\033[38;5;141m',  # Purple for numbers
    'COMMENT': '\033[38;5;102m',  # Gray for comments
    'CONSTANT': '\033[38;5;174m',  # Light red for constants

    # Traceback specific colors
    'FILENAME': '\033[38;5;186m',  # Light yellow for filenames
    'LINENO': '\033[38;5;37m',  # Teal for line numbers
    'ERROR': '\033[38;5;196m',  # Bright red for errors
    'WARNING': '\033[38;5;214m',  # Orange for warnings
    'EXCEPTION': '\033[38;5;203m'  # Red for exception names
}

COLORS = {
    'HEADER': '\033[95m',
    'BLUE': '\033[94m',
    'CYAN': '\033[96m',
    'GREEN': '\033[92m',
    'YELLOW': '\033[93m',
    'RED': '\033[91m',
    'BOLD': '\033[1m',
    'UNDERLINE': '\033[4m',
    'RESET': '\033[0m'
}

import sys
import traceback
# Create a console for rich output

def stack_trace():
    print_colored_traceback(*sys.exc_info(), limit=50)

def print_colored_traceback(exc_type, exc_value, exc_traceback, limit=None, file=None):
    """
    Print the traceback with colors and clickable links that open in IntelliJ IDEA.

    Args:
        exc_type: Exception type
        exc_value: Exception value
        exc_traceback: Exception traceback
        limit: Maximum number of stack frames to show
        file: File to write the traceback to
    """
    if file is None:
        file = sys.stdout

    # Format the traceback
    traceback_lines = traceback.format_exception(exc_type, exc_value, exc_traceback, limit=limit)

    # Color and print each line with clickable links
    for line in traceback_lines:
        # Color the "Traceback" header
        # if line.startswith("Traceback"):
        line = f"{COLORS['BOLD']}{COLORS['YELLOW']}{line}{COLORS['RESET']}"
        # # Color the "File" lines and make them clickable
        # elif line.strip().startswith("File "):
        #     parts = line.split('"')
        #     if len(parts) >= 3:
        #         # Extract filename
        #         filename = parts[1]
        #
        #         # Parse line number
        #         line_parts = parts[2].split(", line ")
        #         if len(line_parts) >= 2:
        #             line_num_parts = line_parts[1].split(",")
        #             if len(line_num_parts) >= 2:
        #                 line_num = line_num_parts[0]
        #                 rest = ",".join(line_num_parts[1:])
        #
        #                 # Create IntelliJ URL
        #                 relative_path = os.path.abspath(filename)
        #                 intellij_url = f"{line}"
        #
        #                 # Create clickable link with ANSI escape codes
        #                 clickable_filename = f"\033]8;{intellij_url}\033\\{COLORS['GREEN']}{filename}{COLORS['RESET']}\033]8;;\033\\"
        #
        #                 # Format line number with color
        #                 colored_line_num = f"{COLORS['BOLD']}{COLORS['GREEN']}line {line_num}{COLORS['RESET']}"
        #
        #                 # Reconstruct the line
        #                 parts[1] = clickable_filename
        #                 parts[2] = line_parts[0] + ", " + colored_line_num + "," + rest
        #
        #                 line = parts[0] + '"' + parts[1] + '"' + parts[2]
        # # Color the exception type and message
        # elif any(exc_name in line for exc_name in ["Error:", "Exception:", "Warning:"]):
        #     line = f"{COLORS['BOLD']}{COLORS['RED']}{line}{COLORS['RESET']}"

        file.write(line)

import gc
import torch


def memory_flame_chart(scope=None, threshold_kb=1, depth=10000, width=80, color=True, aggregate_by_type=True,
                       include_cuda=True, scan_all_objects=True, max_objects=50000000):
    """
    Generate a flame chart visualization of memory usage by variables.

    Args:
        scope: The scope/namespace to analyze. If None, uses the caller's globals and locals.
        threshold_kb: Minimum size in KB to include in the chart (default: 1KB)
        depth: Maximum depth for nested objects to traverse (default: 3)
        width: Width of the terminal output (default: 80 characters)
        color: Whether to use ANSI colors in output (default: True)
        aggregate_by_type: Whether to aggregate memory usage by class type (default: True)
        include_cuda: Whether to include CUDA memory in the chart (default: True)
        scan_all_objects: Whether to scan all objects in memory (default: True)
        max_objects: Maximum number of objects to scan (default: 500000)

    Returns:
        None: Prints the flame chart to stdout
    """
    # ANSI color codes
    colors = {
        'reset': '\033[0m',
        'red': '\033[91m',
        'yellow': '\033[93m',
        'green': '\033[92m',
        'blue': '\033[94m',
        'cyan': '\033[96m',
        'magenta': '\033[95m',
    }

    if not color:
        # Disable colors if not wanted
        for k in colors:
            colors[k] = ''

    # Get the namespace to analyze
    if scope is None:
        # Get caller's frame to access its variables
        caller_frame = inspect.currentframe().f_back
        global_vars = caller_frame.f_globals
        local_vars = caller_frame.f_locals
    else:
        global_vars = scope
        local_vars = {}

    # Track already seen objects to avoid cycles
    seen_ids = set()

    # Store type-specific information when aggregating
    type_sizes = defaultdict(int)
    type_counts = defaultdict(int)
    type_examples = {}

    # Flag to detect if PyTorch is available
    has_pytorch = False
    try:
        import torch
        has_pytorch = True
    except ImportError:
        pass

    # Flag to detect if NumPy is available
    has_numpy = False
    try:
        import numpy as np
        has_numpy = True
    except ImportError:
        pass

    # Stores the size of each variable and its path when not aggregating
    var_sizes = []
    threshold_bytes = threshold_kb * 1024

    # For tracking tensor memory
    cpu_tensor_size = 0
    numpy_array_size = 0

    # Risky module patterns to avoid
    risky_modules_patterns = [
        r'transformers\.models\.auto',
        r'transformers\.utils\.import_utils',
        r'transformers\.deepspeed',
        r'importlib\._bootstrap',
        r'lazy_loader',
        r'google\.protobuf',
    ]

    def is_risky_object(obj):
        """Check if an object is from a module that might cause import or recursion issues"""
        try:
            if not hasattr(obj, '__class__'):
                return False

            module_name = obj.__class__.__module__
            # Check if module name matches any risky pattern
            if any(re.search(pattern, module_name) for pattern in risky_modules_patterns):
                return True

            # Check for specific object types
            if hasattr(obj, '__getattr__') and not isinstance(obj, dict) and not hasattr(obj, 'items'):
                # Objects with custom __getattr__ might trigger imports
                return True

            # Dynamic attribute objects that might trigger imports
            risky_class_names = ['LazyLoader', 'LazyImport', 'DynamicModule', '_LazyModule']
            if obj.__class__.__name__ in risky_class_names:
                return True

            return False
        except:
            # If any error occurs while checking, consider it risky
            return True

    def estimate_tensor_memory(tensor):
        """Estimate memory used by a tensor"""
        try:
            if hasattr(tensor, 'element_size') and hasattr(tensor, 'nelement'):
                return tensor.element_size() * tensor.nelement()
            elif hasattr(tensor, 'itemsize') and hasattr(tensor, 'size'):
                # For numpy arrays
                return tensor.itemsize * tensor.size
            return 0
        except:
            return 0

    def get_size(obj, name, current_depth=0, path=""):
        """Recursively find the size of objects and their attributes"""
        nonlocal cpu_tensor_size, numpy_array_size

        if current_depth > depth:
            return 0

        # Skip already seen objects
        obj_id = id(obj)
        if obj_id in seen_ids:
            return 0

        seen_ids.add(obj_id)

        # Skip risky objects
        if is_risky_object(obj):
            # Just estimate the basic size without recursion
            try:
                obj_size = sys.getsizeof(obj)

                # Record basic type information
                if aggregate_by_type:
                    obj_type = obj.__class__.__name__
                    type_sizes[obj_type] += obj_size
                    type_counts[obj_type] += 1
                    if obj_type not in type_examples:
                        type_examples[obj_type] = path if path else name

                return obj_size
            except:
                return 0

        # Default object size
        obj_size = 0
        tensor_data_size = 0

        # Special handling for PyTorch tensors
        if has_pytorch and isinstance(obj, torch.Tensor):
            try:
                # Get base object size
                obj_size = sys.getsizeof(obj)

                if obj.is_cuda:
                    # For CUDA tensors, only count the object size
                    # Record custom size information for CUDA tensors
                    if aggregate_by_type:
                        cuda_type = f"torch.Tensor(CUDA)"
                        type_sizes[cuda_type] += obj_size
                        type_counts[cuda_type] += 1
                        if cuda_type not in type_examples:
                            type_examples[cuda_type] = path
                else:
                    # For CPU tensors, estimate memory
                    tensor_data_size = estimate_tensor_memory(obj)
                    obj_size += tensor_data_size

                    # Track CPU tensor memory separately
                    cpu_tensor_size += tensor_data_size

                    # Record CPU tensor information
                    if aggregate_by_type:
                        cpu_type = f"torch.Tensor(CPU)"
                        type_sizes[cpu_type] += obj_size
                        type_counts[cpu_type] += 1
                        if cpu_type not in type_examples:
                            type_examples[cpu_type] = path
            except:
                obj_size = sys.getsizeof(obj)

        # Special handling for NumPy arrays
        elif has_numpy and isinstance(obj, np.ndarray):
            try:
                # Get base object size
                obj_size = sys.getsizeof(obj)

                # Add size of array data
                array_data_size = estimate_tensor_memory(obj)
                obj_size += array_data_size

                # Track numpy array memory separately
                numpy_array_size += array_data_size

                # Record numpy array information
                if aggregate_by_type:
                    numpy_type = f"numpy.ndarray"
                    type_sizes[numpy_type] += obj_size
                    type_counts[numpy_type] += 1
                    if numpy_type not in type_examples:
                        type_examples[numpy_type] = path
            except:
                obj_size = sys.getsizeof(obj)
        else:
            try:
                # Get the object's size for other objects
                obj_size = sys.getsizeof(obj)
            except Exception:
                # Some objects don't support getsizeof
                obj_size = 0

        current_path = f"{path}.{name}" if path else name

        # Record size information if aggregating
        if aggregate_by_type:
            if has_pytorch and isinstance(obj, torch.Tensor):
                if obj.is_cuda:
                    obj_type = "torch.Tensor(CUDA)"
                else:
                    obj_type = "torch.Tensor(CPU)"
            elif has_numpy and isinstance(obj, np.ndarray):
                obj_type = "numpy.ndarray"
            else:
                obj_type = type(obj).__name__

            type_sizes[obj_type] += obj_size
            type_counts[obj_type] += 1
            if obj_type not in type_examples:
                type_examples[obj_type] = current_path

        # Store the size info for display if not aggregating
        if not aggregate_by_type and obj_size >= threshold_bytes:
            var_sizes.append((current_path, obj_size, type(obj).__name__))

        # Skip recursive inspection of tensors and special types
        if (has_pytorch and isinstance(obj, torch.Tensor)) or (has_numpy and isinstance(obj, np.ndarray)):
            return obj_size

        # For some collection types, add the size of their items
        try:
            if isinstance(obj, (list, tuple, set, frozenset)):
                try:
                    for i, item in enumerate(obj):
                        if current_depth < depth:  # Respect depth limit
                            item_path = f"{current_path}[{i}]"
                            obj_size += get_size(item, f"[{i}]", current_depth + 1, current_path)
                except:
                    pass

            elif isinstance(obj, dict):
                try:
                    # Safe iteration over dictionary items
                    safe_items = list(obj.items())
                    for k, v in safe_items:
                        if current_depth < depth:  # Respect depth limit
                            # Convert key to string representation
                            try:
                                k_str = str(k) if len(str(k)) < 20 else f"{str(k)[:17]}..."
                            except:
                                k_str = "?"
                            item_path = f"{current_path}[{k_str}]"
                            obj_size += get_size(v, f"[{k_str}]", current_depth + 1, current_path)
                except:
                    pass

            # For custom objects, inspect attributes
            elif hasattr(obj, '__dict__') and not isinstance(obj, type):
                try:
                    # For PyTorch modules, handle parameters specially
                    if has_pytorch and hasattr(obj, 'parameters') and callable(getattr(obj, 'parameters', None)):
                        try:
                            for name, param in list(obj.named_parameters()):
                                if current_depth < depth:
                                    param_path = f"{current_path}.{name}"
                                    obj_size += get_size(param, name, current_depth + 1, current_path)
                        except:
                            pass

                    # Get standard attributes
                    safe_dict = dict(obj.__dict__)
                    for attr, value in safe_dict.items():
                        if not attr.startswith('__') and current_depth < depth:
                            attr_path = f"{current_path}.{attr}"
                            obj_size += get_size(value, attr, current_depth + 1, current_path)
                except:
                    pass
        except:
            # If any exception occurs during traversal, just use the object's own size
            pass

        return obj_size

    # Get total memory of this process as a comparison
    process = psutil.Process(os.getpid())
    total_process_memory = process.memory_info().rss

    # Analyze memory usage
    print(f"\n{colors['cyan']}===== Memory Usage Flame Chart ====={colors['reset']}")
    print(f"Process total: {total_process_memory / (1024 * 1024):.2f} MB")
    print(f"Threshold: {threshold_kb} KB\n")

    # Process variables from both globals and locals
    all_vars = {}
    all_vars.update(global_vars)
    all_vars.update(local_vars)

    # Start memory analysis from explicit variables
    print(f"{colors['blue']}Analyzing {len(all_vars)} variables in current scope...{colors['reset']}")
    for name, obj in all_vars.items():
        try:
            # Skip modules, functions, and other non-data objects for direct analysis
            if name.startswith('__') or inspect.ismodule(obj) or inspect.isfunction(obj) or inspect.isbuiltin(obj):
                continue

            get_size(obj, name)
        except Exception as e:
            # Skip objects that can't be inspected
            continue

    # If scanning all objects, use garbage collector to find objects not directly accessible
    total_objects_scanned = len(seen_ids)
    if scan_all_objects:
        print(f"{colors['blue']}Scanning all objects in memory (this may take a while)...{colors['reset']}")

        # Get all objects from garbage collector
        gc.collect()  # Force collection to free up unreferenced objects

        try:
            all_objects = gc.get_objects()

            # Skip some problematic types
            skip_types = set([type, type(None), type(NotImplemented), type(Ellipsis)])
            if has_pytorch:
                try:
                    # Skip tensor storage types
                    import torch.storage
                    skip_types.add(type(torch.storage.TypedStorage))
                    skip_types.add(type(torch.storage._TypedStorage))
                except:
                    pass

            print(f"{colors['blue']}Found {len(all_objects)} total objects. Analyzing...{colors['reset']}")

            # Process a subset of objects to avoid taking too long
            objects_to_process = min(len(all_objects), max_objects)
            for i, obj in enumerate(all_objects[:objects_to_process]):
                if i % 50000 == 0 and i > 0:
                    print(f"{colors['blue']}Processed {i}/{objects_to_process} objects...{colors['reset']}")

                try:
                    # Skip if already seen
                    if id(obj) in seen_ids:
                        continue

                    # Skip problematic types
                    if type(obj) in skip_types:
                        continue

                    # Skip modules, functions, etc.
                    if inspect.ismodule(obj) or inspect.isfunction(obj) or inspect.isbuiltin(obj):
                        continue

                    # Skip risky objects right away
                    if is_risky_object(obj):
                        # Just get a basic size estimate without traversing
                        try:
                            obj_size = sys.getsizeof(obj)

                            # Record basic type information
                            if aggregate_by_type:
                                obj_type = obj.__class__.__name__
                                type_sizes[obj_type] += obj_size
                                type_counts[obj_type] += 1
                                if obj_type not in type_examples:
                                    type_examples[obj_type] = f"<{obj_type}>"

                            seen_ids.add(id(obj))
                        except:
                            pass
                        continue

                    # Process this object - use its type name as an identifier
                    obj_type = type(obj).__name__
                    get_size(obj, f"<{obj_type}>")

                except:
                    # Skip problematic objects
                    continue

            total_objects_scanned = len(seen_ids)
            print(f"{colors['blue']}Scanned {total_objects_scanned} unique objects.{colors['reset']}")
        except Exception as e:
            print(f"{colors['red']}Error during object scanning: {str(e)}{colors['reset']}")

    # Collect CUDA memory info if available
    cuda_mem_total = 0
    cuda_entries = []
    if has_pytorch and torch.cuda.is_available() and include_cuda:
        try:
            # Get CUDA memory stats
            for device_idx in range(torch.cuda.device_count()):
                cuda_mem = torch.cuda.memory_reserved(device_idx)
                if cuda_mem > 0:
                    cuda_name = f"CUDA:{device_idx} Memory"
                    cuda_entries.append((cuda_name, cuda_mem, "cuda_memory"))
                    cuda_mem_total += cuda_mem
        except:
            # In case of errors accessing CUDA memory info
            pass

    # Prepare the data for display
    if aggregate_by_type:
        # Convert type data to the same format as var_sizes
        var_sizes = []  # Clear and rebuild with type data
        for type_name, size in type_sizes.items():
            if size >= threshold_bytes:
                example = type_examples.get(type_name, "<unknown>")
                count = type_counts[type_name]
                display_name = f"{type_name} ({count} instances)"
                var_sizes.append((display_name, size, example))

    # Get the total accounted memory (excluding CUDA - counted separately)
    ram_accounted = sum(size for _, size, _ in var_sizes)

    # Add CUDA entries to the display list
    if include_cuda:
        var_sizes.extend(cuda_entries)

    # Sort by size (largest first)
    var_sizes.sort(key=lambda x: x[1], reverse=True)

    # Calculate max name length for formatting
    max_name_len = min(max((len(name) for name, _, _ in var_sizes), default=20), 50)

    # Print the flame chart
    if not var_sizes:
        print(f"{colors['yellow']}No variables found above the threshold of {threshold_kb} KB{colors['reset']}")
        return

    # Report memory statistics
    print(f"RAM memory accounted for: {ram_accounted / (1024 * 1024):.2f} MB " +
          f"({ram_accounted / total_process_memory * 100:.1f}% of process total)")

    if cpu_tensor_size > 0:
        print(f"CPU tensor data: {cpu_tensor_size / (1024 * 1024):.2f} MB " +
              f"({cpu_tensor_size / total_process_memory * 100:.1f}% of process total)")

    if numpy_array_size > 0:
        print(f"NumPy array data: {numpy_array_size / (1024 * 1024):.2f} MB " +
              f"({numpy_array_size / total_process_memory * 100:.1f}% of process total)")

    if include_cuda and cuda_mem_total > 0:
        print(f"CUDA memory: {cuda_mem_total / (1024 * 1024):.2f} MB")
        print(f"Total (RAM + CUDA): {(ram_accounted + cuda_mem_total) / (1024 * 1024):.2f} MB")

    # For bar width calculation based on the largest object
    max_size = max(size for _, size, _ in var_sizes)

    # Print header
    if aggregate_by_type:
        print(
            f"\n{colors['magenta']}{'Type (instances)':<{max_name_len}} | {'Size':>10} | {'Example':>50} | Usage{colors['reset']}")
    else:
        print(
            f"\n{colors['magenta']}{'Variable':<{max_name_len}} | {'Size':>10} | {'Type':>50} | Usage{colors['reset']}")
    print("-" * (max_name_len + 33 + width))

    # Print each variable with a bar representing its size
    for name, size, type_info in var_sizes:
        # Format name (truncate if too long)
        if len(name) > max_name_len:
            name = name[:max_name_len - 3] + "..."

        # Calculate the bar width
        bar_width = int((size / max_size) * (width - 10))

        # Choose color based on size and type
        if type_info == "cuda_memory":
            color_code = colors['blue']  # CUDA memory in blue
        elif "torch.Tensor(CPU)" in name:
            color_code = colors['cyan']  # CPU tensors in cyan
        elif "numpy.ndarray" in name:
            color_code = colors['magenta']  # NumPy arrays in magenta
        elif size > 100 * 1024 * 1024:  # >100MB
            color_code = colors['red']
        elif size > 10 * 1024 * 1024:  # >10MB
            color_code = colors['yellow']
        else:
            color_code = colors['green']

        # Format size
        if size > 1024 * 1024 * 1024:  # GB range
            size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"
        elif size > 1024 * 1024:  # MB range
            size_str = f"{size / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{size / 1024:.2f} KB"

        # Truncate type_info if it's too long
        if len(str(type_info)) > 50:
            type_info = str(type_info)[:46] + "..."

        # Print the bar
        bar = "█" * bar_width
        print(f"{name:<{max_name_len}} | {size_str:>10} | {type_info:>50} | {color_code}{bar}{colors['reset']}")

    # Display memory that couldn't be accounted for (for RAM only)
    unaccounted = total_process_memory - ram_accounted
    if unaccounted > 0:
        print("\n" + "-" * (max_name_len + 33 + width))
        print(f"{colors['yellow']}RAM memory not accounted for: {unaccounted / (1024 * 1024):.2f} MB " +
              f"({unaccounted / total_process_memory * 100:.1f}% of process total){colors['reset']}")
        print(
            f"{colors['yellow']}This includes memory used by C extensions, memory fragmentation, and system overhead.{colors['reset']}")

        # If we've scanned all objects and still missing a lot, suggest reasons
        if scan_all_objects and unaccounted > 0.5 * total_process_memory:
            print(f"{colors['yellow']}Possible reasons for large unaccounted memory:{colors['reset']}")
            print(f"{colors['yellow']}1. Memory allocated in C/C++ extensions not visible to Python{colors['reset']}")
            print(f"{colors['yellow']}2. Memory fragmentation due to many allocations/deallocations{colors['reset']}")
            print(
                f"{colors['yellow']}3. Tensors in modules not fully traversed due to safety measures{colors['reset']}")

    # Add note about additional memory profiling
    print(
        f"\n{colors['blue']}Note: For a more complete memory profile, consider using specialized tools like:{colors['reset']}")
    print(f"  - memory_profiler: pip install memory_profiler")
    print(f"  - py-spy: pip install py-spy")
    if has_pytorch:
        print(f"  - pytorch_memlab: for PyTorch memory analysis")
        print(f"  - torch.cuda.memory_summary(): for detailed CUDA memory breakdown")

    print("\n")


def cleanup_cuda_memory(verbose=True):
    """
    Clean up unreferenced CUDA memory that might not be automatically released by PyTorch.

    Parameters:
    verbose (bool): Whether to print memory usage information before and after cleanup

    Returns:
    tuple: (initial_allocated, final_allocated, freed_memory) in MB
    """
    # Check if CUDA is available
    if not torch.cuda.is_available():
        print("CUDA is not available")
        return (0, 0, 0)

    # Get initial memory usage
    initial_allocated = torch.cuda.memory_allocated() / (1024 * 1024)  # Convert to MB
    initial_reserved = torch.cuda.memory_reserved() / (1024 * 1024)  # Convert to MB

    if verbose:
        print(f"Initial CUDA memory allocated: {initial_allocated:.2f} MB")
        print(f"Initial CUDA memory reserved: {initial_reserved:.2f} MB")

    # Clear PyTorch cache
    torch.cuda.empty_cache()

    # Run Python garbage collector to collect objects that are no longer referebnced
    gc.collect()

    # Force CUDA synchronization - ensures all operations are complete
    torch.cuda.synchronize()

    # Empty cache again after collecting garbage
    torch.cuda.empty_cache()

    # Get final memory usage
    final_allocated = torch.cuda.memory_allocated() / (1024 * 1024)  # Convert to MB
    final_reserved = torch.cuda.memory_reserved() / (1024 * 1024)  # Convert to MB

    freed_memory = initial_allocated - final_allocated

    if verbose:
        print(f"Final CUDA memory allocated: {final_allocated:.2f} MB")
        print(f"Final CUDA memory reserved: {final_reserved:.2f} MB")
        print(f"Freed memory: {freed_memory:.2f} MB")

        if final_reserved > final_allocated:
            print(
                f"Note: {final_reserved - final_allocated:.2f} MB is still reserved by PyTorch but not allocated to tensors")
            print("This memory can be used by PyTorch without additional GPU memory allocation")

    return (initial_allocated, final_allocated, freed_memory)


def find_cuda_tensors():
    """
    Find and print information about all CUDA tensors currently in memory

    Returns:
    int: Count of CUDA tensors found
    """
    cuda_tensors = []

    # Get all objects in memory
    for obj in gc.get_objects():
        try:
            # Check if it is a torch tensor and on CUDA
            if torch.is_tensor(obj) and obj.device.type == 'cuda':
                cuda_tensors.append(obj)
        except:
            # Some objects might raise exceptions when checking attributes
            pass

    # Print summary
    print(f"Found {len(cuda_tensors)} CUDA tensors in memory")

    # Group by shape for better overview
    shape_count = {}

    for tensor in cuda_tensors:
        shape = str(tensor.shape)
        if shape in shape_count:
            shape_count[shape] += 1
        else:
            shape_count[shape] = 1

    # Print shape statistics
    print("\nTensor shapes:")
    for shape, count in sorted(shape_count.items(), key=lambda x: x[1], reverse=True):
        print(f"  {shape}: {count} tensors")

    return len(cuda_tensors)
