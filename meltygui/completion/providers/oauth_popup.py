"""Placed sign-in popups — Wayland refuses window positioning, so auth pages
open as a chromeless Chromium `--app` window forced onto XWAYLAND
(`--ozone-platform=x11`, where placement IS allowed) and a placement thread
parks it beside the pointer — i.e. over the button that opened it — with
xdotool, sized for a login form.

A dedicated profile (`~/.lsd/oauth-browser`) does two jobs: it guarantees a
FRESH browser instance (flags would be swallowed by an already-running
Wayland Chrome otherwise) and it keeps its own Google session between
sign-ins — the first one asks for credentials, later ones are one click.

`shim_env` covers the flow the studio doesn't open itself: `claude auth
login` calls xdg-open on its own, so its PATH gets a shim dir whose
xdg-open launches the same placed popup (`place_async` watches for it).

Fallback at every step — popups disabled (Toggles.InternetAccounts.
use_oauth_popup), no Chromium-family browser, no DISPLAY, the browser
dying before a window appears — is plain copilot.open_url. Every spawn is
posix_spawn-style (absolute paths, close_fds=False — never fork the
studio's CUDA/GL address space).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

# Protocol constants (not knobs): the WM_CLASS the popup is found/closed by,
# and where the shim + browser profile go.
WINDOW_CLASS = "lsd-oauth"
PROFILE_DIR = Path.home() / ".lsd" / "oauth-browser"
SHIM_DIR = Path.home() / ".lsd" / "oauth-shim"


def find_browser(explicit=""):
    """A Chromium-family browser (only they have --app / --ozone-platform)."""
    if explicit:
        found = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        return found
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def popup_available() -> bool:
    from meltygui.core.runtime.toggles import Toggles
    return (bool(Toggles.InternetAccounts.use_oauth_popup)
            and bool(os.environ.get("DISPLAY"))
            and find_browser(Toggles.InternetAccounts.oauth_popup_browser) is not None
            and shutil.which("xdotool") is not None)


class PopupHandle:
    def __init__(self, process):
        self.process = process
        self.placed = False

    def close(self):
        """End of the flow: the popup's job is done — close it."""
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass


def open_auth_popup(url, browser=None, xdotool=None, size=None, place_timeout_s=15.0):
    """Open `url` as a placed popup; falls back to copilot.open_url and
    returns None when it can't. Returns a PopupHandle (close() on flow end)
    when the popup browser was launched."""
    from meltygui.core.runtime.toggles import Toggles
    browser = browser or (find_browser(Toggles.InternetAccounts.oauth_popup_browser)
                          if Toggles.InternetAccounts.use_oauth_popup else None)
    xdotool = xdotool or shutil.which("xdotool")
    if not browser or not xdotool or not os.environ.get("DISPLAY"):
        _fallback(url)
        return None
    width, height = size or Toggles.InternetAccounts.oauth_popup_size
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(
            [browser, f"--app={url}", "--ozone-platform=x11", f"--class={WINDOW_CLASS}",
             f"--window-size={width},{height}", f"--user-data-dir={PROFILE_DIR}",
             "--no-first-run", "--no-default-browser-check"],
            close_fds=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        _fallback(url)
        return None
    handle = PopupHandle(process)
    threading.Thread(target=_place, args=(handle, url, xdotool, (width, height), place_timeout_s),
                     daemon=True, name="oauth-popup-place").start()
    return handle


def place_async(xdotool=None, size=None, place_timeout_s=20.0):
    """Watch for a popup some OTHER process launches (the xdg-open shim under
    `claude auth login`) and park it like open_auth_popup does."""
    from meltygui.core.runtime.toggles import Toggles
    xdotool = xdotool or shutil.which("xdotool")
    if xdotool is None:
        return
    handle = PopupHandle(None)
    threading.Thread(target=_place,
                     args=(handle, None, xdotool, size or Toggles.InternetAccounts.oauth_popup_size,
                           place_timeout_s),
                     daemon=True, name="oauth-popup-place").start()


def close_popups(xdotool=None):
    """Close every lsd-oauth window (flows we don't hold a Popen for)."""
    xdotool = xdotool or shutil.which("xdotool")
    if xdotool is None:
        return
    try:
        found = subprocess.run([xdotool, "search", "--class", WINDOW_CLASS],
                               capture_output=True, text=True, timeout=5, close_fds=False)
        for window_id in found.stdout.split():
            subprocess.run([xdotool, "windowclose", window_id],
                           timeout=5, close_fds=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ── the xdg-open shim for `claude auth login` ─────────────────────────────

def write_shim(browser=None):
    """`~/.lsd/oauth-shim/xdg-open`: launches the placed popup for whatever
    URL Claude Code opens. Regenerated per use so browser/size changes land."""
    from meltygui.core.runtime.toggles import Toggles
    browser = browser or find_browser(Toggles.InternetAccounts.oauth_popup_browser)
    if browser is None:
        return None
    width, height = Toggles.InternetAccounts.oauth_popup_size
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    shim = SHIM_DIR / "xdg-open"
    shim.write_text(
        "#!/bin/sh\n"
        "# latent-descent oauth shim: Claude Code's browser-open, as a placed popup\n"
        f'exec "{browser}" "--app=$1" --ozone-platform=x11 --class={WINDOW_CLASS} '
        f"--window-size={width},{height} \"--user-data-dir={PROFILE_DIR}\" "
        "--no-first-run --no-default-browser-check "
        ">/dev/null 2>&1 &\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def shim_env(base_env):
    """`base_env` with the shim first on PATH (and as $BROWSER) — pass to the
    `claude auth login` subprocess; unchanged when popups can't happen."""
    if not popup_available() or write_shim() is None:
        return dict(base_env)
    env = dict(base_env)
    env["PATH"] = f"{SHIM_DIR}:{env.get('PATH', '')}"
    env["BROWSER"] = str(SHIM_DIR / "xdg-open")
    return env


# ── placement ─────────────────────────────────────────────────────────────

def _place(handle, url, xdotool, size, timeout_s):
    """Wait for the popup's X window, then park it beside the pointer
    (clamped to the display span) and raise it. If the browser died before
    a window appeared (bad flags, broken profile) fall back to xdg-open."""
    width, height = size
    # Within this margin of any display edge the popup is pushed inward.
    margin = 16
    # The popup is this far above the pointer so the title area isn't under it.
    pointer_lift = 60
    deadline = time.monotonic() + timeout_s

    def run(*args):
        return subprocess.run([xdotool, *args], capture_output=True, text=True,
                              timeout=10, close_fds=False)

    window_id = None
    while time.monotonic() < deadline and window_id is None:
        try:
            found = run("search", "--onlyvisible", "--class", WINDOW_CLASS)
            ids = found.stdout.split()
            if ids:
                window_id = ids[-1]
                break
        except (OSError, subprocess.TimeoutExpired):
            return
        if (handle.process is not None and handle.process.poll() not in (None, 0)
                and url is not None):
            _fallback(url)          # the popup was but not showing anything
            return
        time.sleep(0.25)
    if window_id is None:
        return
    try:
        mouse = run("getmouselocation", "--shell").stdout
        position = dict(line.split("=", 1) for line in mouse.split() if "=" in line)
        screen = run("getdisplaygeometry").stdout.split()
        screen_width, screen_height = int(screen[0]), int(screen[1])
        x = max(margin, min(int(position.get("X", 0)) - width // 2, screen_width - width - margin))
        y = max(margin, min(int(position.get("Y", 0)) - pointer_lift, screen_height - height - margin))
        run("windowmove", window_id, str(x), str(y))
        run("windowactivate", window_id)
        handle.placed = True
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass


def _fallback(url):
    try:
        from meltygui.completion.providers.copilot import open_url
        open_url(url)
    except Exception:
        pass
