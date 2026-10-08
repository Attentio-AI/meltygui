"""Controls for the codec and pending-change lifecycle, shared by file views."""
from meltygui import imgui
from meltygui.editor.pending_save import PendingSave
from meltygui.view.header_view import flat_button


def draw_file_status(address, codec, draw_state):
    state = PendingSave.file_status(address, codec, draw_state)
    if not state:
        return 0
    if flat_button('Save', draw_state, view_id='file-save', width=65, height=26):
        PendingSave.save_file(address)
    imgui.same_line()
    if flat_button('Refresh', draw_state, view_id='file-refresh', width=85, height=26):
        PendingSave.refresh_file(address, codec)
    imgui.same_line()
    status = ('Saving…' if state.get('saving') else state.get('save_error')
              or state.get('error') or ('File changed — refresh to reload.' if state.get('stale') else ''))
    imgui.text_unformatted(str(status))
    height = 30
    if state.get('save_error') and PendingSave.entry_for(address) is not None and not state.get('saving'):
        if flat_button('Keep my edits', draw_state, view_id='file-keep-mine', width=140, height=26):
            PendingSave.save_file(address, force=True)
        imgui.same_line()
        if flat_button('Load file and discard my edits', draw_state, view_id='file-take-theirs', width=240, height=26):
            PendingSave.refresh_file(address, codec, discard=True)
        height += 30
    return height
