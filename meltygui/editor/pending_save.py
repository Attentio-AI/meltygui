import difflib
from collections import defaultdict
from typing import Any

import imgui

from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


@window(view_func=RenderFuncs.draw_type, disable_scroll=False)
class PendingSave:


    pending_saves = defaultdict(Any)
    originals = defaultdict(Any)

    @classmethod
    def mark_load(cls, address, data, **kwargs):
        cls.originals[address] = data

    @classmethod
    def queue_save(cls, address, codec, **kwargs):
        cls.pending_saves[address] = codec, kwargs

    @classmethod
    def apply_all_saves(cls):
        for address, (codec, kwargs) in cls.pending_saves.items():
            codec.save(address=address, **kwargs)

        cls.pending_saves.clear()


@window
@render_func()
def draw_pending_saves():
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    RenderFuncs.draw_type(PendingSave, name="Pending Saves")


    for address, (codec, kwargs) in PendingSave.pending_saves.items():
        if address in PendingSave.originals:
            original_data = PendingSave.originals[address]
            # get code diff using codec library (DO NOT USE CODEC) code.diff does not exist.
            # code_diff = codec.diff(address=address, **kwargs) ### WRONG
            new_data = kwargs.get("data")
            old_data = original_data
            new_lines = str(new_data).splitlines(keepends=True)
            old_lines = str(old_data).splitlines(keepends=True)
            diff = difflib.unified_diff(
                fromfile=str(address.path), tofile=str(address.path),
                a=old_lines, b=new_lines, n=0,
            )
            # Drop the unified-diff scaffolding (--- / +++ file headers, @@ hunk
            # headers, and difflib's "\ No newline" lines) so only the +/- content
            # lines remain - draw_text(is_diff=True) reads the leading +/- to color
            # added lines green and deleted lines yellow.
            diff_str = "".join(
                l for l in diff if not l.startswith(("---", "+++", "@@", "\\"))
            )
            RenderFuncs.draw_text(diff_str, is_diff=True)
        else:
            imgui.text("No original data to compare against for address: {}".format(address))
