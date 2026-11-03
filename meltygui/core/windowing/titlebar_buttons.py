"""Which window controls the desktop puts in a title bar, and on which side.

Every desktop lets the user choose the title-bar buttons (drop the maximize
button, put the controls on the left, ...), and GTK / Qt apps that draw
their own header bars follow that setting. meltygui's frameless windows draw
their own controls too (titlebar.py), so they read the same setting:

    Hyprland with hyprbars-buttons v1:
        the running plugin's ordered icons, actions and per-window state;
        SVG/PNG artwork is decoded off-thread and cached by mtime/size.
    GNOME, Budgie, stock Hyprland / sway / ... with a dconf profile:
        gsettings get org.gnome.desktop.wm.preferences button-layout
        -> 'appmenu:minimize,maximize,close'   (what GTK's header bars read)
    Cinnamon:   org.cinnamon.desktop.wm.preferences button-layout
    MATE:       org.mate.Marco.general button-layout
    KDE:        ~/.config/kwinrc  [org.kde.kdecoration2]  ButtonsOnLeft / ButtonsOnRight
                (letters: I minimize, A maximize, X close; the rest are ignored)
    XFCE:       ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml  button_layout
                ("O|SHMC": H hide = minimize, M maximize, C close, | splits the sides)
    fallback:   the settings portal (org.freedesktop.portal.Settings.Read of
                the GNOME key, what a sandboxed GTK app gets), then DEFAULT.

The GNOME syntax is the canonical form here — `parse_gnome` is also what
Toggles.Melty.titlebar_button_layout takes to pin a layout: "left:right",
comma-separated `minimize` / `maximize` / `close`; a string without a colon
is all LEFT buttons (mutter's and GTK's rule); other tokens (appmenu, menu,
spacer, icon) are dropped.

Stdlib only, so app.py's boot can start the probe (`start_probe`) before
meltygui's heavy imports land: the reads run on a daemon thread (a gsettings
call is a ~10 ms subprocess), `system_layout()` answers DEFAULT until the
first probe lands, and a layout change asks for a frame. `refresh()` re-reads
after a focus gain, `refresh_if_stale()` per frame at
Toggles.Melty.titlebar_button_refresh_s, so a setting changed while the app
runs is picked up without a restart.

Subprocesses go through posix_spawn (absolute executable, close_fds=False,
no cwd / preexec_fn) — the studio must never fork (CLAUDE.md).
"""
from __future__ import annotations

import configparser
import json
import sys
import hashlib
import logging
from pathlib import Path
import os
import re
import shutil
import subprocess
import threading
import time
from collections import namedtuple

ButtonLayout = namedtuple("ButtonLayout", "left right")
KINDS = ("minimize", "maximize", "close")
DEFAULT = ButtonLayout((), ("minimize", "maximize", "close"))

_GSETTINGS = shutil.which("gsettings") or "/usr/bin/gsettings"
_GDBUS = shutil.which("gdbus") or "/usr/bin/gdbus"

_GNOME_SCHEMA = "org.gnome.desktop.wm.preferences"
_CINNAMON_SCHEMA = "org.cinnamon.desktop.wm.preferences"
_MATE_SCHEMA = "org.mate.Marco.general"

# Survives hotswap re-exec (module globals are reused): the last layout read,
# where it came from, when, and the probe thread.
_state = globals().get("_state") or {"layout": None, "source": None, "probed_at": 0.0, "thread": None}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _kinds(tokens, seen):
    out = []
    for token in tokens:
        token = token.strip().lower()
        if token in KINDS and token not in seen:
            seen.add(token)
            out.append(token)
    return tuple(out)


def parse_gnome(text):
    """'appmenu:minimize,maximize,close' → ButtonLayout. No colon = all on
    the left (mutter / GTK); unknown tokens dropped; a kind appears once."""
    text = (text or "").strip().strip("'\"").strip()
    if ":" in text:
        left_text, right_text = text.split(":", 1)
    else:
        left_text, right_text = text, ""
    seen = set()
    left = _kinds(left_text.split(","), seen)
    right = _kinds(right_text.split(","), seen)
    return ButtonLayout(left, right)


_KDE_LETTERS = {"I": "minimize", "A": "maximize", "X": "close"}
_XFWM_LETTERS = {"H": "minimize", "M": "maximize", "C": "close"}


def _letters(text, table, seen):
    out = []
    for letter in (text or "").strip():
        kind = table.get(letter.upper())
        if kind is not None and kind not in seen:
            seen.add(kind)
            out.append(kind)
    return tuple(out)


def parse_kde(buttons_on_left, buttons_on_right):
    """kwinrc's ButtonsOnLeft / ButtonsOnRight letter strings."""
    seen = set()
    return ButtonLayout(_letters(buttons_on_left, _KDE_LETTERS, seen),
                        _letters(buttons_on_right, _KDE_LETTERS, seen))


def parse_xfwm(text):
    """xfwm4's button_layout ("O|SHMC"): the bar splits the sides; without
    one everything is on the left, like GNOME."""
    text = (text or "").strip()
    left_text, right_text = (text.split("|", 1) + [""])[:2] if "|" in text else (text, "")
    seen = set()
    return ButtonLayout(_letters(left_text, _XFWM_LETTERS, seen),
                        _letters(right_text, _XFWM_LETTERS, seen))


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _run(args, timeout=3.0):
    """stdout of a command, or None when it can't run / fails. Absolute
    executable + close_fds=False → posix_spawn, never fork."""
    if not args or not os.path.exists(args[0]):
        return None
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _config_home(environ):
    return environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def _read_gsettings(schema, run=_run):
    out = run([_GSETTINGS, "get", schema, "button-layout"])
    if out is None or not out.strip():
        return None
    return parse_gnome(out), f"gsettings {schema}"


def _read_portal(run=_run):
    """org.freedesktop.portal.Settings.Read — the answer prints as
    `(<<'appmenu:minimize,maximize,close'>>,)`."""
    out = run([_GDBUS, "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
               "--object-path", "/org/freedesktop/portal/desktop",
               "--method", "org.freedesktop.portal.Settings.Read", _GNOME_SCHEMA, "button-layout"])
    if out is None:
        return None
    match = re.search(r"'([^']*)'", out)
    if match is None:
        return None
    return parse_gnome(match.group(1)), "settings portal"


def _read_kde(environ):
    path = os.path.join(_config_home(environ), "kwinrc")
    if not os.path.isfile(path):
        return None
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    section = "org.kde.kdecoration2"
    # KWin's own defaults when the keys are absent: menu + on-all-desktops
    # left, help / minimize / maximize / close right.
    left = parser.get(section, "ButtonsOnLeft", fallback="MS") if parser.has_section(section) else "MS"
    right = parser.get(section, "ButtonsOnRight", fallback="HIAX") if parser.has_section(section) else "HIAX"
    return parse_kde(left, right), "kwinrc"


def _read_xfwm(environ):
    path = os.path.join(_config_home(environ), "xfce4", "xfconf", "xfce-perchannel-xml", "xfwm4.xml")
    if not os.path.isfile(path):
        return None
    try:
        import xml.etree.ElementTree as ElementTree
        root = ElementTree.parse(path).getroot()
    except Exception:
        return None
    for node in root.iter("property"):
        if node.get("name") == "button_layout":
            return parse_xfwm(node.get("value", "")), "xfwm4"
    return parse_xfwm("O|SHMC"), "xfwm4 default"


def detect(environ=None, run=_run):
    """(ButtonLayout, source) for this desktop: the desktop's own store
    first (XDG_CURRENT_DESKTOP), then the GNOME key via gsettings (most
    desktops carry a dconf profile), the settings portal, and DEFAULT."""
    environ = os.environ if environ is None else environ
    desktops = (environ.get("XDG_CURRENT_DESKTOP") or "").lower().split(":")
    probes = []
    if "kde" in desktops:
        probes.append(lambda: _read_kde(environ))
    if "xfce" in desktops:
        probes.append(lambda: _read_xfwm(environ))
    if "x-cinnamon" in desktops or "cinnamon" in desktops:
        probes.append(lambda: _read_gsettings(_CINNAMON_SCHEMA, run))
    if "mate" in desktops:
        probes.append(lambda: _read_gsettings(_MATE_SCHEMA, run))
    probes.append(lambda: _read_gsettings(_GNOME_SCHEMA, run))
    probes.append(lambda: _read_portal(run))
    for probe in probes:
        found = probe()
        if found is not None:
            return found
    return DEFAULT, "default"


# ---------------------------------------------------------------------------
# A probe thread + the cached answer
# ---------------------------------------------------------------------------

def parse_hyprbars(reply):
    """Validate the compositor's versioned button manifest, in visual order."""
    data = json.loads(reply)
    if data.get("version") != 1 or data.get("side") not in ("left", "right"):
        raise ValueError("Unsupported titlebar button manifest")
    buttons = {}
    order = []
    for item in data["buttons"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in
                                                ("icon", "alt_icon", "action", "state", "role")):
            raise ValueError("Invalid titlebar button")
        key = "system:" + hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:20]
        buttons[key] = item
        order.append(key)
    if data["side"] == "right":
        order.reverse()  # Hyprbars stores buttons from the outer edge inward.
    layout = ButtonLayout(tuple(order), ()) if data["side"] == "left" else ButtonLayout((), tuple(order))
    return layout, buttons


def _read_hyprbars():
    if not sys.platform.startswith("linux"):
        return None
    from meltygui.core.windowing.geometry_feed import hypr_request
    try:
        return parse_hyprbars(hypr_request("j/hyprbars-buttons"))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        return None


def _decode_artwork(buttons, previous):
    """CPU/file work belongs to the existing background layout probe."""
    from meltygui.model.folder_icon_model import decode_icon
    result = {}
    for item in buttons.values():
        for icon in (item["icon"], item["alt_icon"]):
            path = Path(icon)
            if not path.is_absolute() or path.suffix.lower() not in (".svg", ".png"):
                continue
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if icon in previous and previous[icon][0] == stamp:
                    result[icon] = previous[icon]
                    continue
                pixels = decode_icon(path, size=48)
                if "-symbolic" in path.stem and pixels.format == "rgba8srgb":
                    rgba = bytearray(pixels.data)
                    for offset in range(0, len(rgba), 4):
                        rgba[offset:offset + 3] = b"\xff\xff\xff"
                    pixels.data = bytes(rgba)
                result[icon] = (stamp, pixels)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
    return result


def system_button(kind):
    return _state.get("buttons", {}).get(kind)


def uses_system_buttons():
    return _state.get("source") == "hyprbars"


def activate_system_button(kind, address):
    """Run exactly the configured action, scoped to the clicked window.

    No focus manipulation, shell interpolation of window metadata, or main-seat
    input. Helpers may use HYPRLAND_WINDOW_ADDRESS instead of activewindow.
    """
    item = system_button(kind)
    if item is None or not item["action"] or not re.fullmatch(r"0x[0-9a-fA-F]+", address or ""):
        return False
    env = dict(os.environ, HYPRLAND_WINDOW_ADDRESS=address)
    # Reap even quick actions; shell startup uses posix_spawn like other probes.
    def run_action():
        try:
            result = subprocess.run(["/bin/sh", "-c", item["action"]], env=env, close_fds=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                logging.getLogger(__name__).warning("System titlebar action %s exited with %s", kind, result.returncode)
        except OSError:
            logging.getLogger(__name__).exception("Could not launch system titlebar action %s", kind)
    threading.Thread(target=run_action, name="titlebar-action", daemon=True).start()
    return True


# CPU snapshots are published by the worker; texture ownership stays on the
# render thread, and TextureId handles GL/native context-specific cleanup.
_artwork = globals().get("_artwork") or {"snapshot": None, "textures": {}}


def prepare_client_decorations(app_id):
    """Before mapping a Melty window, let its CSD request win over force_bar.

    This one startup request uses the real app ID, not a hardcoded app list.
    Standard decorated windows still receive a server bar. Per-window tags
    handle live chrome toggles after mapping without changing other apps.
    """
    if not sys.platform.startswith("linux") or not app_id:
        return
    from meltygui.core.windowing.geometry_feed import hypr_request
    name = "melty-csd-" + hashlib.sha256(app_id.encode()).hexdigest()[:20]
    rule = ('{name=' + json.dumps(name) + ',match={class=' +
            json.dumps(re.escape(app_id), ensure_ascii=False) + '},["hyprbars:force_bar"]=false}')
    try:
        hypr_request('eval if hl.plugin.hyprbars then hl.window_rule(' + rule + ') end')
    except (OSError, RuntimeError):
        pass  # Other compositors already honor the normal CSD protocol.


_claims_lock = globals().get("_claims_lock") or threading.Lock()


def sync_client_controls(address, enabled):
    """Announce custom chrome once per window/state/probe generation.

    The normal frame path only checks cached values. IPC runs on a worker;
    periodic/focus probes reassert ownership after a compositor plugin reload.
    """
    if not uses_system_buttons() or not re.fullmatch(r"0x[0-9a-fA-F]+", address or ""):
        return
    claims = _state.setdefault("client_controls", {})
    value = (bool(enabled), _state["probed_at"])
    if claims.get(address) == value:
        return
    claims[address] = value
    def publish():
        from meltygui.core.windowing.geometry_feed import hypr_request
        try:
            with _claims_lock:
                if claims.get(address) != value:
                    return
                tag = ("+" if enabled else "-") + "melty-client-controls"
                hypr_request(f'eval local w=hl.get_window("address:{address}"); '
                             f'if w and w.pid=={os.getpid()} then '
                             f'hl.dispatch(hl.dsp.window.tag({{window=w,tag="{tag}"}})) end')
        except (OSError, RuntimeError):
            pass  # Retry on the next existing focus/periodic probe, never per frame.
    threading.Thread(target=publish, name="titlebar-decoration", daemon=True).start()


def artwork_texture(icon):
    snapshot = _state.get("artwork", {})
    if snapshot is not _artwork["snapshot"]:
        for path, (pixels, texture) in list(_artwork["textures"].items()):
            if path not in snapshot or snapshot[path][1] is not pixels:
                texture.release()
                del _artwork["textures"][path]
        _artwork["snapshot"] = snapshot
    if icon not in snapshot:
        return None
    if icon not in _artwork["textures"]:
        from meltygui.model.folder_icon_model import FolderIconTexture
        pixels = snapshot[icon][1]
        _artwork["textures"][icon] = (pixels, FolderIconTexture(pixels))
    return _artwork["textures"][icon][1]


def _probe():
    buttons, artwork = {}, {}
    try:
        found = _read_hyprbars()
        if found is not None:
            layout, buttons = found
            source = "hyprbars"
            artwork = _decode_artwork(buttons, _state.get("artwork", {}))
        else:
            layout, source = detect()
    except Exception:
        layout, source = DEFAULT, "error"
    changed = layout != _state["layout"] or artwork != _state.get("artwork", {})
    _state.update(layout=layout, source=source, buttons=buttons, artwork=artwork, probed_at=time.monotonic())
    if changed:
        try:
            from meltygui.core.windowing.glfw_utils import request_render
            request_render()
        except Exception:
            pass


def start_probe(force=False):
    """Read the layout on a daemon thread (idempotent while one runs; a
    known layout is kept unless ``force``). Returns the thread, or None."""
    thread = _state["thread"]
    if thread is not None and thread.is_alive():
        return thread
    if _state["layout"] is not None and not force:
        return None
    thread = threading.Thread(target=_probe, name="titlebar-buttons", daemon=True)
    thread.start()
    _state["thread"] = thread
    return thread


def system_layout():
    """The desktop's layout, DEFAULT until the first probe lands (kicked
    here if nobody started it)."""
    layout = _state["layout"]
    if layout is None:
        start_probe()
        return DEFAULT
    return layout


def source():
    """Where the current layout came from ("gsettings …", "kwinrc", …)."""
    return _state["source"]


def refresh(min_interval=2.0):
    """Re-read (a focus gain: the user may have changed the setting), at
    most once per ``min_interval`` seconds."""
    if time.monotonic() - _state["probed_at"] >= min_interval:
        start_probe(force=True)


def refresh_if_stale(interval):
    """Per-frame: re-read once the last probe is ``interval`` seconds old."""
    if interval and interval > 0:
        refresh(min_interval=float(interval))
