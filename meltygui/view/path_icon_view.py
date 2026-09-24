"""Compact path artwork shared by lists, selectors and menus.

Repeated rows pass their view's batched ``icon_state`` to ``path_icon``;
``with_path_icons`` handles results, subscriptions and dispatch once per view.
``draw_path_icon`` remains the standalone control for an isolated icon.
"""
from pathlib import Path
from meltygui import imgui
from meltygui.core.core_render import render_func
from meltygui.core.files.path_icons import cleanup_path_icon, sync_icon_watches, dispatch_icon_loads
from meltygui.hdr_color import pack_color
from meltygui.state.path_icon_state import PathIconState
from meltygui.view.file_icon_view import draw_file_badge
from meltygui.model.folder_icon_model import PathIcon


@render_func(show_header=False, show_bg=False, shadow=False, selectable=False,
             disable_scroll=True, on_cleanup=cleanup_path_icon)
def draw_path_icon(input_value: str, draw_state, icon_state: PathIconState = None,
                   size=18.0, color=None, fallback=None, is_dir=True,
                   custom_icon=None, alpha=1.0, paint=True):
    """Desktop artwork takes the same slot as a glyph; explicit metadata wins."""
    path = Path(input_value).expanduser()
    folders = {path} if is_dir and not custom_icon else set()
    icon_state.folders.consume(folders)
    sync_icon_watches(draw_state, icon_state, icon_state.folders.watch_directories())
    texture = icon_state.folders.get(path)
    x, y = imgui.get_cursor_screen_pos()
    dl = imgui.get_window_draw_list()
    color = pack_color(0.9, 0.92, 0.96, alpha) if color is None else color
    if paint:
        paint_path_icon(dl, path, texture, x, y, size, color, is_dir, fallback, custom_icon, alpha)
    imgui.dummy(size, size)
    dispatch_icon_loads(draw_state, icon_state)
    return False, input_value


def paint_path_icon(dl, path, texture, x, y, size, color, is_dir, fallback, custom_icon=None, alpha=1.0):
    """Paint a resolved icon on a normal or drag-overlay draw list."""
    fallback = fallback or (f"\uf07b" if is_dir else f"\uf15b")
    if texture is not None:
        pixels = texture.pixels
        scale = size / max(pixels.width, pixels.height)
        w, h = pixels.width * scale, pixels.height * scale
        left, top = x + (size - w) / 2, y + (size - h) / 2
        dl.add_image(int(texture), (left, top), (left + w, top + h),
                     col=pack_color(1, 1, 1, alpha))
        dl.add_line(x + 1, y + size, x + size - 1, y + size, color, 1.5)
    elif is_dir or custom_icon:
        dl.add_text(x, y + (size - imgui.get_font_size()) / 2, color, custom_icon or fallback)
    else:
        from meltygui.code.codec_registry import file_badge_for_path, file_icon_for_path
        badge = file_badge_for_path(path) if not is_dir and not custom_icon else None
        if badge:
            draw_file_badge(dl, x, y, size, color, badge, alpha)
        else:
            glyph = custom_icon or (fallback if is_dir else file_icon_for_path(path) or fallback)
            dl.add_text(x, y + (size - imgui.get_font_size()) / 2, color, glyph)

def path_icon(path, x, y, size, color, *, is_dir=True, fallback=None, custom_icon=None, key=None, draw_list=None, icons=None):
    """Place an icon in an existing row without changing its layout or hit target."""
    if isinstance(path, PathIcon):
        path, is_dir, custom_icon = path.path, path.is_dir, path.custom_icon
    if icons is not None:
        path = path if isinstance(path, Path) else Path(path)
        if is_dir and not custom_icon:
            icons.requested.add(path)
            texture = icons.folders.get(path)
        else:
            texture = None
        paint_path_icon(draw_list if draw_list is not None else imgui.get_window_draw_list(),
                        path, texture, x, y, size, color, is_dir, fallback, custom_icon)
        return
    cursor = imgui.get_cursor_screen_pos()
    if draw_list is None:
        imgui.set_cursor_screen_pos((x, y))
    try:
        result = draw_path_icon(str(path), name=f'path-icon:{path if key is None else key}', size=size, color=color,
                       is_dir=is_dir, fallback=fallback, custom_icon=custom_icon,
                       width=size, height=size, paint=draw_list is None, return_extras=True)
        if draw_list is not None:
            state = result[2].misc.get('icon_state')
            texture = state.folders.get(Path(path)) if state is not None else None
            paint_path_icon(draw_list, Path(path), texture, x, y, size, color,
                            is_dir, fallback, custom_icon)
    finally:
        imgui.set_cursor_screen_pos(cursor)


def path_label(draw_list, label, x, y, color, paths, icons=None):
    """Replace indexed glyphs in a label without changing its text or spacing."""
    start = 0
    for index, path in sorted(paths.items()):
        if not 0 <= index < len(label):
            continue
        prefix = label[start:index]
        draw_list.add_text(x, y, color, prefix)
        x += imgui.calc_text_size(prefix)[0]
        width = imgui.calc_text_size(label[index])[0]
        size = min(18.0, width)
        path_icon(path, x, y + (imgui.get_font_size() - size) / 2, size, color, key=f"label-icon:{index}", icons=icons)
        x += width
        start = index + 1
    draw_list.add_text(x, y, color, label[start:])
