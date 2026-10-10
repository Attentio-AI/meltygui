"""Throwaway Rust @gui proving ground inside the existing tile manager.

Both experimental hosts execute the SAME four Python bodies. The separate
"Existing collection" mode deliberately includes production features absent
from this prototype, so its ratio is not a wrapper-only speedup claim.
"""
from collections import deque
from collections.abc import MutableMapping
from statistics import median
from time import perf_counter_ns

import meltygui_imgui as imgui

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.core_render import render_func
from meltygui.core.layout.tile_manager_core import TileManagerState, draw_tiles
from meltygui.core.rendering.core_decoration import no_save_exclude
from functools import partial
from meltygui.core.rendering.gui_prototype import _new_native, _bind_native
from meltygui.core.runtime.toggles import Tint, Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.hdr_color import pack_color
from meltygui.model.tile_model import Split, Tile
from meltygui.view.header_view import flat_button as host_button


class ButtonState(DictConversion):
    def __init__(self):
        super().__init__()
        self.clicks = 0


def make_views(decorate, counts):
    """Same bodies, layout and work for native and render_func host comparisons."""
    gui = decorate
    views = {}

    @gui(height=26, events=('left_mouse_clicked',))
    def flat_button(input_value: str, draw_state=None, button_state: ButtonState = None,
                    left_mouse_clicked=None, mouse_pos=(-1, -1), **kwargs):
        counts[0] += 1
        left, top = imgui.get_cursor_screen_pos()
        width, height = draw_state.content_width, 26
        hovered = left <= mouse_pos[0] < left + width and top <= mouse_pos[1] < top + height
        dl = imgui.get_window_draw_list()
        color = Tint.checkbox_bg_hovered() if hovered else Tint.checkbox_bg()
        dl.add_rect_filled(left, top, left + width, top + height, pack_color(*color), 4)
        # Submit text through the current ImGui font/style stack, just like the
        # numeric labels. Keep the button's layout and event rectangle fixed.
        imgui.set_cursor_screen_pos((left + 9, top + 4))
        imgui.text(str(input_value))
        imgui.set_cursor_screen_pos((left, top))
        imgui.dummy(width, height)
        clicked = bool(left_mouse_clicked)
        if clicked:
            button_state.clicks += 1
        return clicked, input_value

    @gui(height=25)
    def draw_int(input_value: int, draw_state=None, label='', speed=1.0,
                 min_value=-100000, max_value=100000, editable=True, **kwargs):
        counts[0] += 1
        imgui.text(label)
        imgui.same_line(position=max(80, draw_state.content_width * .45))
        if not editable:
            imgui.text(str(input_value))
            return False, input_value
        imgui.push_item_width(max(50, draw_state.content_width * .5))
        try:
            changed, value = imgui.drag_int('##value', int(input_value), change_speed=speed,
                                           min_value=min_value, max_value=max_value)
            return changed, type(input_value)(value) if changed else input_value
        finally:
            imgui.pop_item_width()

    @gui(height=25)
    def draw_float(input_value: float, draw_state=None, label='', speed=.05,
                   min_value=-100000., max_value=100000., precision=3, editable=True, **kwargs):
        counts[0] += 1
        imgui.text(label)
        imgui.same_line(position=max(80, draw_state.content_width * .45))
        if not editable:
            imgui.text(str(input_value))
            return False, input_value
        imgui.push_item_width(max(50, draw_state.content_width * .5))
        try:
            changed, value = imgui.drag_float('##value', float(input_value), change_speed=speed,
                                             min_value=min_value, max_value=max_value,
                                             format=f'%.{precision}f')
            return changed, type(input_value)(value) if changed else input_value
        finally:
            imgui.pop_item_width()

    @gui()
    def draw_collection(input_value: object, draw_state=None, name='Values', expanded=True,
                        child_kwargs=None, mouse_pos=(-1, -1), **kwargs):
        counts[0] += 1
        child_kwargs = dict(child_kwargs or {})
        width = max(80, draw_state.content_width - 12)
        opened = getattr(draw_state, 'expanded', expanded)
        clicked, _ = views['flat_button'](
            f'{"▾" if opened else "▸"} {name} ({len(input_value)})', key='header',
            name=f'{name} header', width=width, mouse_pos=mouse_pos)
        if clicked:
            opened = not opened
            draw_state.expanded = opened
        if not opened:
            return False, input_value
        changed = False
        mutable = isinstance(input_value, (MutableMapping, list))
        items = input_value.items() if isinstance(input_value, MutableMapping) else enumerate(input_value)
        output = list(input_value) if isinstance(input_value, tuple) else input_value
        imgui.indent(10)
        try:
            for key, value in items:
                arguments = {**child_kwargs, 'key': key, 'name': str(key), 'label': str(key),
                             'width': width, 'mouse_pos': mouse_pos}
                if isinstance(value, (MutableMapping, list, tuple)):
                    child_changed, result = views['draw_collection'](value, **arguments)
                elif isinstance(value, int) and not isinstance(value, bool):
                    child_changed, result = views['draw_int'](value, **arguments)
                elif isinstance(value, float):
                    child_changed, result = views['draw_float'](value, **arguments)
                else:
                    imgui.text(f'{key}: {value}')
                    child_changed, result = False, value
                if child_changed:
                    output[key] = result
                    changed = True
        finally:
            imgui.unindent(10)
        if changed and not mutable and isinstance(input_value, tuple):
            output = type(input_value)(output)
        return changed, output if changed else input_value

    views.update(flat_button=flat_button, draw_int=draw_int, draw_float=draw_float,
                 draw_collection=draw_collection)
    return views


def python_gui(func=None, **decoration):
    """Use the production wrapper with exactly the same experimental bodies."""
    decoration.pop('events', None)
    decoration = {'use_cache': False, 'show_bg': False, 'with_header': None, 'show_name': False,
                  'disable_scroll': True, 'tint': (.22, .34, .45), **decoration}
    if func is None:
        return lambda function: python_gui(function, **decoration)
    # The production registry is keyed by function name; these experimental
    # copies must not replace RenderFuncs.draw_int/draw_float/draw_collection.
    func.__name__ = f'prototype_python_{func.__name__}'
    return render_func(func, **decoration)


def sample_data(rows):
    return {f'Row {index:04d}': {'count': index, 'gain': index / 10.0} for index in range(rows)}


@no_save_exclude('runtime', 'native_views', 'python_views', 'native_counts', 'python_counts',
                 'native_times', 'python_times', 'native_stats', 'tree')
class PrototypeState(DictConversion):
    def __init__(self):
        super().__init__()
        self.rows = 25
        self.baseline = 'Same bodies'
        self.render_python = True
        self.render_native = True
        self.native_counts, self.python_counts = [0], [0]
        self.runtime = _new_native()
        self.native_views = make_views(partial(_bind_native, self.runtime), self.native_counts)
        self.python_views = make_views(python_gui, self.python_counts)
        self.native_times, self.python_times = deque(maxlen=120), deque(maxlen=120)
        self.native_stats = {}
        self.native_data, self.python_data = sample_data(self.rows), sample_data(self.rows)
        self.tree = Split('x', [
            Tile('Python comparison', render_func=draw_python_panel, input_value=self),
            Tile('Rust @gui', render_func=draw_native_panel, input_value=self),
        ])

    def reset(self, rows=None):
        self.rows = self.rows if rows is None else rows
        self.native_data, self.python_data = sample_data(self.rows), sample_data(self.rows)
        self.native_times.clear()
        self.python_times.clear()


def timing_label(samples):
    if not samples:
        return 'Warming up'
    ordered = sorted(samples)
    return f'{median(ordered):.2f} ms median   {ordered[int((len(ordered) - 1) * .95)]:.2f} ms p95'


@render_func(tint=(.22, .32, .46), use_cache=False, with_header=None, disable_scroll=False)
def draw_python_panel(input_value: object, draw_state=None, mouse_pos=(-1, -1)):
    state = input_value
    imgui.text(f'Python @render_func  |  {state.baseline}')
    imgui.text(timing_label(state.python_times))
    imgui.text(f'{state.rows} rows   {state.python_counts[0]} body calls' if state.baseline == 'Same bodies'
               else f'{state.rows} rows   Production collection and controls')
    imgui.separator()
    if not state.render_python:
        imgui.text('Python panel paused. Resume it for the side-by-side comparison.')
        return False, state
    state.python_counts[0] = 0
    start = perf_counter_ns()
    if state.baseline == 'Same bodies':
        changed, value = state.python_views['draw_collection'](
            state.python_data, name='Values', width=draw_state.content_width - 12,
            mouse_pos=imgui.get_mouse_pos(), child_kwargs={'precision': 3})
    else:
        from meltygui.view.collection_view import draw_collection
        # Select the actual render_func host for nested collections too. This
        # is a local experiment setting; restore the user's global immediately.
        previous = Toggles.Collection.fast_draw_collection
        Toggles.Collection.fast_draw_collection = False
        try:
            changed, value = draw_collection(
                state.python_data, name='Values', width=draw_state.content_width - 12,
                use_cache=False, expanded=True, child_kwargs={'use_cache': False, 'expanded': True})
        finally:
            Toggles.Collection.fast_draw_collection = previous
    state.python_times.append((perf_counter_ns() - start) / 1e6)
    if changed:
        state.python_data = value
    return changed, state


@render_func(tint=(.18, .41, .33), use_cache=False, with_header=None, disable_scroll=False)
def draw_native_panel(input_value: object, draw_state=None, style_manager=None):
    state = input_value
    imgui.text('Rust @gui  |  Dynamic native fields')
    imgui.text(timing_label(state.native_times))
    imgui.text(f'{state.rows} rows   {state.native_counts[0]} body calls')
    imgui.separator()
    if not state.render_native:
        imgui.text('Rust panel paused.')
        return False, state
    state.native_counts[0] = 0
    state.runtime.begin_frame(owner=draw_state, width=draw_state.content_width - 12,
                              mouse_pos=imgui.get_mouse_pos(), style_manager=style_manager)
    try:
        start = perf_counter_ns()
        changed, value = state.native_views['draw_collection'](
            state.native_data, name='Values', child_kwargs={'precision': 3})
        state.native_times.append((perf_counter_ns() - start) / 1e6)
        state.native_stats = state.runtime.stats()
    finally:
        state.runtime.end_frame()
    if changed:
        state.native_data = value
    return changed, state


def close_prototype(draw_state):
    state = draw_state.misc.get('prototype_state')
    if state is not None:
        state.runtime.clear()


@render_func(tint=(.22, .28, .33), use_cache=False, live=True, disable_scroll=True,
             on_cleanup=close_prototype)
def draw_rust_gui_demo(input_value: object, draw_state=None,
                       prototype_state: PrototypeState = None, tile_state: TileManagerState = None):
    state = prototype_state
    imgui.text('Rust @gui prototype')
    imgui.text('Drag numbers to edit. Click collection headers to collapse. Resize the tile divider.')
    for index, count in enumerate((25, 100, 300, 1000)):
        if index:
            imgui.same_line()
        if host_button(f'{count} rows', draw_state, f'rows-{count}', width=100):
            state.reset(count)
    imgui.same_line()
    if host_button('Reset values', draw_state, 'reset', width=110):
        state.reset()
    if host_button(f'Baseline: {state.baseline}', draw_state, 'baseline', width=230):
        state.baseline = 'Existing collection' if state.baseline == 'Same bodies' else 'Same bodies'
        state.python_times.clear()
        state.native_times.clear()
    imgui.same_line()
    if host_button('Pause Python' if state.render_python else 'Resume Python',
                   draw_state, 'pause-python', width=145):
        state.render_python = not state.render_python
        state.python_times.clear()
        state.native_times.clear()
    imgui.same_line()
    if host_button('Pause Rust' if state.render_native else 'Resume Rust',
                   draw_state, 'pause-rust', width=130):
        state.render_native = not state.render_native
        state.python_times.clear()
        state.native_times.clear()
    if state.render_native and state.render_python and state.native_times and state.python_times:
        ratio = median(state.python_times) / max(.00001, median(state.native_times))
        imgui.text(f'{ratio:.1f}x collection wall-time ratio   '
                   f'Rust wrapper: {state.native_stats.get("wrapper_us", 0) / 1000:.2f} ms   '
                   f'{state.native_stats.get("fields", 0)} dynamic fields')
    else:
        imgui.text('Warming up comparison' if state.render_native and state.render_python
                   else 'Comparison paused; timing the active panel only')
    imgui.text('Same bodies isolates host costs. Existing collection also includes its production features.')
    imgui.text('Both panels share one frame. Pause Python to try the native controls at full speed.')
    changed = draw_tiles(state.tree, draw_state, tile_state=tile_state,
                         content_top=imgui.get_cursor_screen_pos()[1],
                         use_cache=False,
                         multi_instance_renderers=(draw_python_panel, draw_native_panel))
    # This app is explicitly a continuous benchmark. Time drawing, not waiting
    # for input or replaying a previously captured texture.
    request_render()
    return changed, input_value
