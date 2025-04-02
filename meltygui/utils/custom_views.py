import threading
import traceback
from enum import Enum

import glfw
import imgui

from lsd.lsd_utils import singleton


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


    def unstack_group(self):
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
        self.style_stack.clear()

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

        self.group_stack.clear()

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
    LSDView().style_stack.append(GroupType.STYLE)
    return imgui.push_style_color(ImGuiCol_variable, float_r, float_g, float_b, float_a)


def pop_style_color(size=1):
    for _ in range(size):
        if LSDView().style_stack[-1] == GroupType.STYLE:
            LSDView().style_stack.pop()
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

import sys
import traceback
# Create a console for rich output

def print_colored_traceback(exc_type, exc_value, exc_traceback, limit=None, file=None):
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


    """
    Print the traceback with colors to make it easier to read.

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

    # Color and print each line
    for line in traceback_lines:
        # Color the "Traceback" header
        if line.startswith("Traceback"):
            line = f"{COLORS['BOLD']}{COLORS['YELLOW']}{line}{COLORS['RESET']}"
        # Color the "File" lines
        elif line.strip().startswith("File "):
            parts = line.split('"')
            if len(parts) >= 3:
                # Color the filename
                filename = f"{COLORS['GREEN']}{parts[1]}{COLORS['RESET']}"
                # Color the line number
                line_parts = parts[2].split(", line ")
                if len(line_parts) >= 2:
                    line_num_parts = line_parts[1].split(",")
                    if len(line_num_parts) >= 2:
                        line_num = f"{COLORS['BOLD']}{COLORS['GREEN']}line {line_num_parts[0]}{COLORS['RESET']}"
                        rest = ",".join(line_num_parts[1:])
                        parts[2] = line_parts[0] + ", " + line_num + "," + rest
                line = parts[0] + '"' + filename + '"' + parts[2]
        # Color the exception type and message
        elif any(exc_name in line for exc_name in ["Error:", "Exception:", "Warning:"]):
            line = f"{COLORS['BOLD']}{COLORS['RED']}{line}{COLORS['RESET']}"

        file.write(line)


import gc
import torch


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

    # Run Python garbage collector to collect objects that are no longer referenced
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
