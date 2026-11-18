"""What the display shows white as right now, in nits.

    display = Display()
    display.read()                              # one reading, on the calling thread
    display.watch(lambda: cache.wake(node))     # then a thread follows it and says when it changed
    display.emitted                             # nits of a colour of 1.0 as the panel shows it
    display.close()

Two numbers make it up: `white`, the nits of SDR white at full brightness (the compositor's
`sdr_max_luminance`), and `gain`, the brightness slider's factor (its `exposure`). Only the
Hyprland HDR fork reports them, over its request socket (`j/monitors`); anywhere else `read` is
False, `white` stays 0.0 and the caller falls back to what it knows.

That compositor also reshapes the finished frame on its way to the panel (its shadow stages,
cm_helpers.glsl `shadowToneMap`): below the emitted SDR white each channel is shown through a
pure power curve (`shadow_gamma`), and a few nits are taken off with a soft toe (`black_offset`).
`display.tone()` is those settings, `emit(nits, tone)` what the panel emits for the light it is
sent, and `ask(nits, tone)` the light to send for it to emit `nits`: the two undo each other.
Both work on arrays of linear light in the PANEL's own channels (its primaries), in nits.

Those stages run where the frame goes out to the PANEL, in the panel's own channels. A window
is usually on a headless desktop that the panel mirrors, and the description the compositor gives
that window is the desktop's (BT.2020), not the panel's. So `read` takes the settings from the
monitor that is a real output (it has an EDID under /sys/class/drm) and keeps that panel's
`primaries`: the EDID's under the compositor's `hdredid` preset, BT.2020 under `hdr`.

Everything here is that one compositor's: docs/HDR_PORTABILITY.md lists what is assumed and what
another system would need.

The compositor sends no event for a brightness change, so `watch` asks from a thread of its own:
every `PAUSE` seconds, and every `QUICK` seconds for `LINGER` seconds after a reading that
differed, so a slider being dragged is followed closely. One request is about 0.1 ms and never
touches the render thread. `close` ends the thread; whoever calls `watch` calls it (a view:
`cache.on_retire`).
"""
import json
import socket
import threading
import time
from collections import namedtuple

PAUSE = 0.1             # seconds between two readings while watched and nothing changes
QUICK = 0.008           # the same while the brightness is being changed: a frame at 120 Hz
LINGER = 1.0            # seconds the quick pace is kept after the last change
PATIENCE = 0.25         # seconds a request may take
BT2020 = (0.708, 0.292, 0.170, 0.797, 0.131, 0.046, 0.3127, 0.3290)    # red, green, blue, white as CIE xy
EDIDS = {}              # output name -> its EDID's primaries as xy, or None: read once each
TOE = []                # the black offset undone, as a table `toe()` makes once
TOE_SPAN = 20.0         # offsets past which the toe is over: sent = emitted + offset
TOE_STEPS = 4096        # samples of the table

# gamma: the shadow stage's exponent (0.0: off). white: nits under which it applies, the emitted
# SDR white. offset: nits of the black offset (0.0: off).
Tone = namedtuple('Tone', 'gamma white offset')


def emit(nits, tone):
    """What the panel emits for `nits` sent to it: the compositor's shadow stages, as its shader has them."""
    import numpy as np
    light = np.maximum(np.asarray(nits, dtype=np.float64), 0.0)
    if tone.gamma > 0.0 and tone.white > 0.0:
        share = light / tone.white
        encoded = np.where(share < 0.0031308, 12.92 * share, 1.055 * share ** (1.0 / 2.4) - 0.055)
        light = np.where(share < 1.0, encoded ** tone.gamma * tone.white, light)
    if tone.offset > 0.0:
        light = light - tone.offset * (1.0 - np.exp(-light / tone.offset))
    return light


def edid_primaries(name):
    """The chromaticities in the EDID of the output `name` (HDMI-A-2): (red x, y, green x, y,
    blue x, y, white x, y), or None when it is no real output. Ten bits each, in bytes 25 to 34
    of the base block: the low two bits of all eight in the first two, then the high eight of each."""
    if name not in EDIDS:
        import glob
        found = None
        for path in glob.glob(f'/sys/class/drm/card*-{name}/edid'):
            try:
                with open(path, 'rb') as handle:
                    block = handle.read(128)
            except OSError:
                continue
            if len(block) < 35:
                continue
            low = (block[25] << 8) | block[26]
            found = tuple(((block[27 + at] << 2) | ((low >> (14 - 2 * at)) & 3)) / 1024.0 for at in range(8))
            if min(found) <= 0.0:           # an EDID that leaves them out
                found = None
        EDIDS[name] = found
    return EDIDS[name]


def toe():
    """The black offset undone, for an offset of 1: (roots, sent). emit's last step,
    c - b * (1 - exp(-c / b)), has no closed inverse, but in units of b it is one curve for every
    b. It starts like a square root, so the table is spaced in the root of the emitted light."""
    if not TOE:
        import numpy as np
        roots = np.linspace(0.0, TOE_SPAN ** 0.5, TOE_STEPS)
        target = roots * roots
        sent = target + 1.0
        for _ in range(80):             # Newton from above; each step halves the error near black
            fade = np.exp(-sent)
            sent = np.maximum(sent - (sent - (1.0 - fade) - target) / np.maximum(1.0 - fade, 1e-12), 1e-9)
        sent[0] = 0.0
        TOE.extend((roots, sent))
    return TOE


def ask(nits, tone):
    """The light to send for the panel to emit `nits`: `emit` undone. A negative channel, which
    no panel emits, is passed through."""
    import numpy as np
    wanted = np.asarray(nits, dtype=np.float64)
    light = np.maximum(wanted, 0.0)
    if tone.offset > 0.0:
        roots, sent = toe()
        scaled = light / tone.offset
        light = np.where(scaled < TOE_SPAN, np.interp(np.sqrt(scaled), roots, sent), scaled + 1.0) * tone.offset
    if tone.gamma > 0.0 and tone.white > 0.0:
        encoded = (light / tone.white) ** (1.0 / tone.gamma)
        share = np.where(encoded < 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)
        light = np.where(light < tone.white, share * tone.white, light)
    return np.where(wanted < 0.0, wanted, light)


class Display:
    def __init__(self):
        self.white = 0.0            # nits of SDR white at full brightness; 0.0: nobody says
        self.gain = 1.0             # the brightness slider's factor
        self.lift = 1.0             # the compositor's extra gain on SDR windows (`sdrbrightness`)
        self.gamma = 0.0            # its shadow gamma; 0.0: off
        self.offset = 0.0           # its black offset in nits; 0.0: off
        self.primaries = None       # the panel's primaries as xy, where a real panel was found
        self.thread = None
        self.stop = threading.Event()

    @property
    def emitted(self):
        """Nits of a colour of 1.0 as the panel shows it now; 0.0 while `white` is unknown."""
        return self.white * self.gain

    def tone(self):
        """The compositor's shadow stages as they apply now, or None when it has none on."""
        if self.gamma <= 0.0 and self.offset <= 0.0:
            return None
        return Tone(self.gamma, self.white * self.lift * self.gain, self.offset)

    def state(self):
        return self.white, self.gain, self.lift, self.gamma, self.offset, self.primaries

    def read(self):
        """One reading from the compositor. True when it gave one."""
        from meltygui.core.windowing.geometry_feed import hyprland_socket_path
        path = hyprland_socket_path()
        if path is None:
            return False
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(PATIENCE)
        try:
            connection.connect(path)
            connection.sendall(b'j/monitors all')        # all: an output that mirrors another is left out otherwise
            parts = []
            while True:
                part = connection.recv(65536)
                if not part:
                    break
                parts.append(part)
            monitors = json.loads(b''.join(parts))
        except (OSError, ValueError):
            return False
        finally:
            connection.close()
        # The real panel first: its rule is the one that shapes what is seen. Else any output.
        told = [monitor for monitor in monitors if monitor.get('sdrMaxLuminance') and monitor.get('exposure')]
        told.sort(key=lambda monitor: edid_primaries(str(monitor.get('name'))) is None)
        for monitor in told:
            self.white = float(monitor['sdrMaxLuminance'])
            self.gain = float(monitor['exposure'])
            self.lift = float(monitor.get('sdrBrightness') or 1.0)
            self.gamma = max(float(monitor.get('shadowGamma') or 0.0), 0.0)
            self.offset = max(float(monitor.get('blackOffset') or 0.0), 0.0)
            preset = monitor.get('colorManagementPreset')
            self.primaries = None
            if preset == 'hdredid':
                self.primaries = edid_primaries(str(monitor.get('name'))) or BT2020
            elif preset == 'hdr':
                self.primaries = BT2020
            return True
        return False

    def watch(self, changed):
        """Follow the display from a thread and call `changed()` after each reading that differs."""
        if self.thread is not None:
            return
        self.stop.clear()

        def follow():
            pace = PAUSE
            moved = 0.0
            while not self.stop.wait(pace):
                before = self.state()
                self.read()
                if self.state() != before:
                    moved = time.monotonic()
                    changed()
                pace = PAUSE
                if time.monotonic() - moved < LINGER:
                    pace = QUICK
        self.thread = threading.Thread(target=follow, name='display-watch', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread = None
