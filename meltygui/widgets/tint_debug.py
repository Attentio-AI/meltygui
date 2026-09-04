"""Tint debug window — a freeform external-edit rig for the manual merge
system. It manages its OWN file IO on `sample.py` (raw read_text/write_text,
no PendingSave, no code cache, no self-write hash), so its Save is
indistinguishable from an outside program writing the file: the watcher
tracks it as external drift, editors park on the merge banner, and the merge
window shows the incoming diff. Load/Save are manual buttons only — nothing
here is automatic."""

from datetime import datetime
from pathlib import Path

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.text_editor import draw_text

# Anchored at src/sample.py regardless of cwd (this file lives at
# src/lsd/gl_gui/view/playground/) - inside the watched project tree, where
# MelWatch tracks external writes.
SAMPLE_PATH = (Path(__file__).resolve().parents[4] / "sample.py")

_DEFAULT_SAMPLE = '''"""Freeform scratch file for external-edit testing."""


class SampleTints:
    tint = (0.5, 0.2, 0.8)
    outline = (1.0, 0.5, 0.0)


def sample_function():
    return "hello"
'''


@window(disable_scroll=False, icon="", display_name="External Editor",
        tint=(0.13, 0.08, 0.22))
@render_func(tint=(0.35, 0.18, 0.42))
def external_editor(draw_state=None):
    misc = draw_state.misc
    buffer = misc.get("_sample_buffer")

    # First open: seed the buffer from disk (creating the file if needed) -
    # the one implicit load; everything after is button-driven.
    if buffer is None:
        if not SAMPLE_PATH.exists():
            SAMPLE_PATH.write_text(_DEFAULT_SAMPLE)
        buffer = misc["_sample_buffer"] = SAMPLE_PATH.read_text()

    row_h = 26
    if RenderFuncs.button(" Load", width=90, height=row_h,
                          name="tint_debug_load")[0]:
        # Raw read, on purpose: NOT Melty.read_code - this window's view of
        # the file must be its own, independent of the studio's cache.
        try:
            misc["_sample_buffer"] = buffer = SAMPLE_PATH.read_text()
            misc["_sample_status"] = f"loaded {datetime.now():%H:%M:%S}"
        except OSError as e:
            misc["_sample_status"] = f"load failed: {e}"
        draw_state.invalidate_up(max_depth=4)
        request_render()
    imgui.same_line(spacing=8)
    if RenderFuncs.button(" Save", width=90, height=row_h,
                          name="tint_debug_save")[0]:
        # Raw write, on purpose: no codec, no PendingSave, no self-write
        # hash - the watcher sees this as EXTERNAL edit.
        try:
            SAMPLE_PATH.write_text(buffer)
            misc["_sample_status"] = f"saved {datetime.now():%H:%M:%S} (external write)"
        except OSError as e:
            misc["_sample_status"] = f"save failed: {e}"
        draw_state.invalidate_up(max_depth=4)
        request_render()
    imgui.same_line(spacing=12)
    imgui.align_text_to_frame_padding()
    imgui.text_colored(str(SAMPLE_PATH), 0.6, 0.65, 0.7, 1.0)

    # ── Canned edits: mutate the buffer without typing ────────────────────
    # Each returns the new buffer text; None means "no change". The counter
    # keeps generated names unique across clicks.
    n = misc.get("_sample_counter", 0)

    def _add_def(text):
        return (text.rstrip("\n")
                + f"\n\n\ndef generated_{n}():\n    return {n}\n")

    def _add_comment_top(text):
        # Insert below the module docstring if it leads the file, else at
        # the very top - shifts every line down (good for rebase testing).
        lines = text.split("\n")
        at = 0
        if lines and lines[0].lstrip().startswith(('"""', "'''")):
            q = lines[0].lstrip()[:3]
            at = 1 if lines[0].count(q) >= 2 else next(
                (i + 1 for i in range(1, len(lines)) if q in lines[i]), 0)
        lines.insert(at, f"# external comment {n} {datetime.now():%H:%M:%S}")
        return "\n".join(lines)

    def _bump_tint(text):
        # Rewrite the first tuple line mentioning 'tint' with fresh values.
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if "tint" in line and "(" in line:
                indent = line[:len(line) - len(line.lstrip())]
                v = (n % 9 + 1) / 10.0
                lines[i] = f"{indent}tint = ({v}, {v / 2:.2f}, {1.0 - v:.2f})"
                return "\n".join(lines)
        return None

    def _edit_return(text):
        # Change every `return "..."` / `return N` payload in place - an
        # in-span body edit that does NOT shift line counts.
        lines = text.split("\n")
        hit = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("return "):
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f'{indent}return "edit {n}"'
                hit = True
        return "\n".join(lines) if hit else None

    for label, fn in (("+ def", _add_def), ("+ top comment", _add_comment_top),
                      ("~ tint", _bump_tint), ("~ returns", _edit_return)):
        if RenderFuncs.button(f" {label}", height=row_h,
                              name=f"tint_debug_{label}")[0]:
            new = fn(buffer)
            if new is not None:
                misc["_sample_buffer"] = buffer = new
                misc["_sample_counter"] = n + 1
                misc["_sample_status"] = f"buffer edited ({label.strip()})"
                draw_state.invalidate_up(max_depth=4)
                request_render()
        imgui.same_line(spacing=6)
    imgui.new_line()

    # Debug readout: does the studio currently see this file as drifted?
    from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    key = str(SAMPLE_PATH)
    tracked = key in ExternalChanges.originals
    unmerged = tracked and key in PendingSave.unmerged_drift_paths()
    pending = any(a.path == SAMPLE_PATH for a in PendingSave.pending_saves)
    status = misc.get("_sample_status", "")
    imgui.text_colored(
        f"tracked drift: {'yes' if tracked else 'no'}   "
        f"unmerged: {'yes' if unmerged else 'no'}   "
        f"pending edits: {'yes' if pending else 'no'}   {status}",
        0.75, 0.7, 0.5, 1.0)

    edited, new_text = draw_text(buffer, name="tint_debug_editor",
                                 show_header=False, use_cache=True,
                                 bg_offset=-4, shadow=True)
    if edited and new_text != buffer:
        misc["_sample_buffer"] = new_text

    return False, None