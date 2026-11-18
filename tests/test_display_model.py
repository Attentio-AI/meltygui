"""`Display` (model/display_model.py): the nits of white now, and the thread that follows them."""
import threading

from meltygui.model import display_model
from meltygui.model.display_model import Display


def test_emitted_is_white_times_the_brightness():
    display = Display()
    assert display.emitted == 0.0                       # nobody said yet
    display.white = 400.0
    display.gain = 0.25
    assert display.emitted == 100.0


def test_no_compositor_is_no_reading(monkeypatch):
    import meltygui.core.windowing.geometry_feed as geometry_feed
    monkeypatch.setattr(geometry_feed, 'hyprland_socket_path', lambda: None)
    display = Display()
    assert display.read() is False and display.white == 0.0 and display.gain == 1.0


def test_watch_says_when_a_reading_differs_and_close_ends_it(monkeypatch):
    monkeypatch.setattr(display_model, 'PAUSE', 0.005)
    readings = iter([(400.0, 1.0), (400.0, 1.0), (400.0, 0.5)])
    told = threading.Event()
    display = Display()
    display.white = 400.0

    def read(self):
        self.white, self.gain = next(readings, (400.0, 0.5))
        return True
    monkeypatch.setattr(Display, 'read', read)
    display.watch(told.set)
    thread = display.thread
    assert told.wait(2.0) and display.gain == 0.5
    display.close()
    thread.join(2.0)
    assert not thread.is_alive() and display.thread is None


def test_ask_undoes_what_the_panel_does_to_the_light_it_is_sent():
    import numpy as np
    from meltygui.model.display_model import Tone, ask, emit
    tone = Tone(2.3, 96.0, 0.33)
    light = np.concatenate([np.geomspace(1e-4, 96.0, 400), np.linspace(96.0, 2000.0, 100)])
    assert np.allclose(emit(ask(light, tone), tone), light, rtol=1e-4, atol=1e-6)
    assert emit(np.array([48.0]), tone)[0] < 48.0               # under white the stages darken
    assert emit(np.array([500.0]), tone)[0] == 500.0 - 0.33 * (1.0 - np.exp(-500.0 / 0.33))    # above it only the offset
    assert ask(np.array([-0.5]), tone)[0] == -0.5               # no panel emits a negative light: passed through
    only_offset = Tone(0.0, 0.0, 0.33)
    assert np.allclose(emit(ask(light, only_offset), only_offset), light, rtol=1e-4, atol=1e-6)


def test_the_panels_primaries_are_read_from_its_edid(tmp_path, monkeypatch):
    import glob
    from meltygui.model import display_model
    # Ten bits each: red (0.680, 0.305), green (0.272, 0.666), blue (0.155, 0.056), white (0.313, 0.329).
    wanted = (0.6806640625, 0.3046875, 0.2724609375, 0.666015625, 0.1552734375, 0.0556640625, 0.3134765625, 0.3291015625)
    codes = [int(round(value * 1024)) for value in wanted]
    block = bytearray(128)
    block[25] = ((codes[0] & 3) << 6) | ((codes[1] & 3) << 4) | ((codes[2] & 3) << 2) | (codes[3] & 3)
    block[26] = ((codes[4] & 3) << 6) | ((codes[5] & 3) << 4) | ((codes[6] & 3) << 2) | (codes[7] & 3)
    for at, code in enumerate(codes):
        block[27 + at] = code >> 2
    path = tmp_path / 'edid'
    path.write_bytes(bytes(block))
    monkeypatch.setattr(display_model, 'EDIDS', {})
    monkeypatch.setattr(glob, 'glob', lambda pattern: [str(path)] if 'HDMI-A-9' in pattern else [])
    assert display_model.edid_primaries('HDMI-A-9') == wanted
    assert display_model.edid_primaries('desk-1') is None           # a headless output has none
