"""Ownership helpers for drawing captured within the host ImGui frame."""


def release_captured_references(capture, start):
    """Packets own captured texture IDs; preserve uncaptured host/overlay IDs."""
    import meltygui_imgui as imgui
    references = imgui.get_current_context()._keepalive_cache
    if len(references) <= start:
        return
    captured = {id(command.texture_id) for draw in capture.draw_lists
                for command in draw.commands if command.elem_count}
    references[start:] = [value for value in references[start:] if id(value) not in captured]
