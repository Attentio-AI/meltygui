"""Opt-in device entry: safe zone, pacing, keyboard and Metal touch controls.

Copy into the staged app and select device_ui_check as its entry module.
Test state and the result live under Library/Caches/ui-check.
"""
import json
from pathlib import Path
import statistics
import time
import traceback


def install_check(application):
    config = application.config
    output = Path(config['cache']) / 'ui-check'
    sandbox = output / str(time.time_ns())
    sandbox.mkdir(parents=True)
    (output / 'result.json').unlink(missing_ok=True)
    Path.home = classmethod(lambda cls: sandbox)
    from meltygui.core.runtime import app
    from meltygui import glfw_window
    from meltygui.core.core_render import render_func
    from meltygui.view.text_view import draw_text
    from meltygui.view.collection_view import draw_tuple_fast
    from meltygui.view.dropdown_view import draw_dropdown
    from meltygui.view import color_view
    import meltygui_imgui as imgui
    state = dict(mode='cadence', text=''.join(f'line {i:03d}\n' for i in range(100)),
                 color=(0.3, 0.5, 0.8, 1.0))
    wide_texture = color_view._wide_square_texture

    def observe_square(*args):
        state['square'] = tuple(imgui.get_cursor_screen_pos())
        return wide_texture(*args)

    color_view._wide_square_texture = observe_square

    @glfw_window(name='Device UI check', app_id='melty-ui-check')
    @render_func(use_cache=False)
    def body(input_value, draw_state):
        state['root'] = draw_state
        if state['mode'] == 'cadence':
            x = 20 + time.monotonic()*100 % 250
            imgui.get_window_draw_list().add_rect_filled(x, 100, x+20, 120, 0xFFFFFFFF)
        elif state['mode'] == 'text':
            _, _, state['text_ds'] = draw_text(
                state['text'], name='keyboard text', width=380,
                height=imgui.get_io().display_size.y-80,
                show_header=False, with_header=None, with_footer=None, is_tree=False,
                show_widgets=False, autocomplete=False, show_file_header=False,
                show_jump_bar=False, return_extras=True)
        elif state['mode'] == 'menu':
            imgui.set_cursor_screen_pos((40, 100))
            _, _, state['menu'] = draw_dropdown(
                0, collection={f'Item {i}': i for i in range(8)}, name='menu',
                width=220, show_header=False, with_header=None, return_extras=True)
        else:
            _, state['color'] = draw_tuple_fast(
                state['color'], draw_state, view_id='probe', x=40, y=100,
                size=30, view_owner=draw_state)
        return False, input_value

    app.run()
    from meltygui.core.rendering.core_decoration import Core
    assert Core.melty.is_touch
    return CheckedApplication(application, state, output)


class CheckedApplication:
    def __init__(self, application, state, output):
        self.application, self.state, self.output = application, state, output
        self._frame = application.frame
        self.phase = 'warmup'
        self.phase_frame = 0
        self.started = self.phase_time = time.monotonic()
        self.report = {}
        self.presentations, self.callbacks, self.render_times = [], [], []
        self.diagnostics = []
        self.keyboard_geometry = []

    def advance(self, phase):
        self.phase = phase
        self.phase_frame = self.application.frames
        self.phase_time = time.monotonic()

    def frame(self, info, events):
        from meltygui.core.melty import Melty
        from meltygui.state.new_core_model import ColorPickerState
        from meltygui.core.runtime.toggles import Toggles
        state, app = self.state, self.application
        try:
            age = app.frames - self.phase_frame
            injected = []
            if self.phase == 'picker' and age in (2, 3):
                injected = [dict(kind='touch_begin' if age == 2 else 'touch_end',
                                 touch_id=41, x=52, y=112)]
            elif self.phase == 'drag' and age in (1, 2, 3):
                x, y = state['square']
                injected = [dict(kind=('touch_begin', 'touch_move', 'touch_end')[age-1],
                                 touch_id=42, x=x+(25 if age == 1 else 90),
                                 y=y+(100 if age == 1 else 140))]
            elif self.phase in ('menu_open', 'search_tap') and age in (0, 1):
                x, y = state['tap']
                injected = [dict(kind='touch_begin' if age == 0 else 'touch_end',
                                 touch_id=50 if self.phase == 'menu_open' else 51, x=x, y=y)]
            elif self.phase == 'search_tap' and age == 4:
                injected = [dict(kind='text', text='Item 6')]
            before = time.monotonic()
            more = self._frame(info, [*events, *injected])
            duration = time.monotonic() - before
            elapsed = time.monotonic() - self.phase_time
            if self.phase in ('keyboard', 'hide') and 'diagnostics' in info:
                self.keyboard_geometry.append(dict(
                    phase=self.phase, elapsed=elapsed, width=info['width'],
                    height=info['height'], scale=info['scale'],
                    surface=info['diagnostics']['surface_geometry'],
                    viewport=info['diagnostics']['viewport_geometry']))
            if self.phase == 'warmup' and app.frames >= 8:
                assert Toggles.Mobile.Safezone == 64
                self.report['safe_height'] = info['height']
                self.advance('cadence')
            elif self.phase == 'cadence':
                self.presentations.append(info['presentation_time'])
                self.callbacks.append(before)
                self.render_times.append(duration)
                if 'diagnostics' in info:
                    self.diagnostics.append(info['diagnostics'])
                if elapsed >= 2.5:
                    intervals = [b-a for a, b in zip(self.presentations, self.presentations[1:])]
                    self.report['cadence'] = dict(
                        frames=len(self.presentations),
                        presentation_hz=1/statistics.mean(intervals),
                        median_presentation_ms=1000*statistics.median(intervals),
                        callback_hz=(len(self.callbacks)-1)/(self.callbacks[-1]-self.callbacks[0]),
                        median_python_ms=1000*statistics.median(self.render_times),
                        interval_histogram_ms={str(bucket): sum(round(value*1000) == bucket for value in intervals)
                                               for bucket in sorted({round(value*1000) for value in intervals})},
                        intervals_faster_than_60hz=sum(value < 0.015 for value in intervals))
                    if self.diagnostics:
                        first, last = self.diagnostics[0], self.diagnostics[-1]
                        self.report['cadence']['diagnostics'] = dict(
                            maximum_fps=last['maximum_fps'], low_power=last['low_power'],
                            thermal_state=last['thermal_state'],
                            display_callbacks=last['display_callbacks']-first['display_callbacks'],
                            gpu_waits=last['gpu_waits']-first['gpu_waits'],
                            median_gpu_wait_ms=1000*statistics.median(d['gpu_wait_seconds'] for d in self.diagnostics),
                            median_gpu_ms=1000*statistics.median(d['gpu_seconds'] for d in self.diagnostics),
                            median_native_ms=1000*statistics.median(d['native_seconds'] for d in self.diagnostics))
                    Toggles.Mobile.Safezone = 92
                    self.advance('inset_92')
            elif self.phase == 'inset_92' and elapsed > .3:
                assert abs(info['height']-(self.report['safe_height']-28)) < 1, info
                self.report['inset_92_height'] = info['height']
                Toggles.Mobile.Safezone = 0
                self.advance('inset_zero')
            elif self.phase == 'inset_zero' and elapsed > .3:
                assert abs(info['height']-(self.report['safe_height']+64)) < 1, info
                self.report['edge_to_edge_height'] = info['height']
                Toggles.Mobile.Safezone = 64
                self.advance('inset_restore')
            elif self.phase == 'inset_restore' and elapsed > .3:
                assert abs(info['height']-self.report['safe_height']) < 1, info
                state['mode'] = 'text'
                self.advance('text_warmup')
            elif self.phase == 'text_warmup' and age >= 8:
                ds = state['text_ds']
                assert ds._stack_trace is None, repr(ds._stack_trace)
                self.report['full_height'] = info['height']
                line = min(90, int((info['height']-180) / ds._diff_line_px))
                position = state['text'].index(f'line {line:03d}')
                self.report['caret_line'] = line
                ds.text_cursor_pos = ds.text_prev_cursor_pos = position
                ds.scroll_offset = (0, 0)
                ds.invalidate()
                Melty.text_focused_ds = ds
                self.advance('keyboard')
            elif self.phase == 'keyboard' and elapsed > 1.5:
                assert info['height'] < self.report['full_height']-100, info
                ds = state['text_ds']
                top = (ds.abs_top + ds._diff_top_inset
                       + self.report['caret_line']*ds._diff_line_px - ds.scroll_offset[1])
                assert ds.scroll_offset[1] > 0, ds.scroll_offset
                assert ds.abs_top <= top and top+ds._diff_line_px <= info['height'], (top, info)
                self.report.update(keyboard_height=info['height'], caret_top=top,
                                   caret_scroll=ds.scroll_offset[1])
                Melty.text_focused_ds = None
                self.advance('hide')
            elif self.phase == 'hide' and elapsed > 1.5:
                assert abs(info['height']-self.report['full_height']) < 1, info
                self.report['restored_height'] = info['height']
                state['mode'] = 'menu'
                self.advance('menu_warmup')
            elif self.phase == 'menu_warmup' and age >= 6:
                ds = state['menu']
                rect, = [action[3] for action in ds._body_actions[1] if '_dd_trigger' in str(action[0])]
                state['tap'] = (ds.abs_left + (rect[0]+rect[2])/2,
                                ds.abs_top + (rect[1]+rect[3])/2)
                self.advance('menu_open')
            elif self.phase == 'menu_open' and elapsed > .7:
                ds = state['menu']
                assert Melty.popover_focused_ds is ds, 'First tap did not open menu'
                assert Melty.text_focused_ds is None, 'Menu automatically focused keyboard'
                assert abs(info['height']-self.report['safe_height']) < 1, info
                menu = ds.misc['drop_down_state']
                box, = [view for view in Melty._bvh_id_to_ds.values()
                        if view._tile_id == menu._search_box_tile]
                l, t, r, b = box.abs_clamped_rect
                state['tap'] = (l+r)/2, (t+b)/2
                state['search_box'] = box
                self.advance('search_tap')
            elif self.phase == 'search_tap' and elapsed > 1.5:
                assert Melty.text_focused_ds is state['search_box'], 'Search tap did not focus text'
                assert info['height'] < self.report['safe_height']-100, info
                assert state['menu'].misc['drop_down_state'].search_query == 'Item 6'
                self.report['dropdown_requires_search_tap'] = True
                self.report['dropdown_typing'] = True
                Melty.text_focused_ds = Melty.popover_focused_ds = None
                self.advance('menu_hide')
            elif self.phase == 'menu_hide' and elapsed > 1.5:
                assert abs(info['height']-self.report['safe_height']) < 1, info
                state['mode'] = 'picker'
                self.advance('picker')
            elif self.phase == 'picker' and age >= 6:
                picker = Melty.popover_focused_ds
                assert picker is not None and picker is not state['root'], 'First tap did not open picker'
                assert picker._stack_trace is None, repr(picker._stack_trace)
                state['picker'] = picker
                state['picker_state'], = [value for value in picker.misc.values()
                                          if isinstance(value, ColorPickerState)]
                self.advance('tabs')
            elif self.phase == 'tabs':
                picker = state['picker']
                assert picker._stack_trace is None, repr(picker._stack_trace)
                if age < 3:
                    state['picker_state'].tab = ('srgb', 'extended', 'wide')[age]
                    picker.invalidate()
                else:
                    state['before_color'] = state['color']
                    self.advance('drag')
            elif self.phase == 'drag' and age >= 6:
                assert state['color'] != state['before_color'], 'Picker did not update color on drag'
                assert state['picker']._stack_trace is None, repr(state['picker']._stack_trace)
                if self.keyboard_geometry:
                    stretch = max(abs(row['surface'][3]*row['scale']/row['surface'][5]-1)
                                  for row in self.keyboard_geometry)
                    error = max(abs(row['height']-row['viewport'][3])*row['scale']
                                for row in self.keyboard_geometry)
                    assert stretch < .000001, ('Keyboard scaled the Metal canvas', stretch)
                    assert error <= .5001, ('Layout jumped ahead of the animated viewport', error)
                    self.report['keyboard_texture_stretch'] = stretch
                    self.report['keyboard_viewport_error_pixels'] = error
                self.report.update(status='passed', picker_tabs=['wide', 'srgb', 'extended'],
                                   picker_drag=True, frames=app.frames,
                                   keyboard_geometry=self.keyboard_geometry)
                (self.output / 'result.json').write_text(json.dumps(self.report, indent=2))
                print('DEVICE UI CHECK PASSED: safe zone, cadence, keyboard, dropdown and picker')
                self.advance('done')
            if self.phase != 'done' and time.monotonic()-self.started > 40:
                raise TimeoutError('Device UI check did not finish: '+self.phase)
            return more or self.phase != 'done'
        except Exception:
            self.report.update(status='failed', phase=self.phase, error=traceback.format_exc())
            (self.output / 'result.json').write_text(json.dumps(self.report, indent=2))
            raise

    def presented(self):
        self.application.presented()

    def suspend(self):
        self.application.suspend()

    def resume(self):
        self.application.resume()

    def close(self):
        self.application.close()


from meltygui.core.runtime import app as _runtime
_application = _runtime._state['native_host']
_check = install_check(_application)
_application.frame = _check.frame
