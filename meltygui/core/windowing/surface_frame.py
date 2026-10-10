"""One Melty surface frame, shared by desktop and externally paced native hosts.

Hosts activate their context, supply input and begin their graphics target before
calling draw_surface_frame. They submit/present afterwards. Layout, chrome,
render-host updates and the Melty frame lifecycle have a single owner here.
"""
from meltygui import imgui
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing import titlebar, os_frame
from meltygui.utils import render_utils as views


def draw_render_hosts():
    """The studio's draw_main draws every registered RenderHost once a
    frame (RenderHost.draw_all); that is what loads a code_file_io host's
    file, reparses it after an edit and auto-saves it. A meltygui app's body
    is a plain draw function, so the surface runs the pump for it, after
    the body and inside the imgui frame. Hosts are process-global: once
    per app tick, however many surfaces draw in it."""
    if not Melty.render_hosts or Melty.render_hosts_tick == Melty.app_tick:
        return
    Melty.render_hosts_tick = Melty.app_tick
    from meltygui.core.conversion.render_host import RenderHost
    RenderHost.draw_all()


def root_background(surface, w, h, radius):
    """The window's ground: what the studio's Main Window paints under
    its windows (draw_main's show_bg — draw_bg at depth 0 under the root
    tint, capped at the same max_bg_value), edge to edge with the alpha
    cut's corner radius and no outline stroke. Sets the style tint for
    the body; returns (the tint to restore, the ground's colour)."""
    from meltygui.view.decoration_view import draw_bg
    style_manager = Melty.style_manager
    previous_tint = style_manager.get_tint()
    tint = surface.tint if surface.tint is not None else Toggles.Melty.app_root_tint
    style_manager.set_imgui_tint(*tint[:4])
    _, bg_color = draw_bg(bypass=True, left=0, top=0, width=w, height=h, rounding=radius,
                          outline=False, opacity=1.0, max_bg_value=0.130,
                          depth=Melty.shadow_depth, style_manager=style_manager)
    return previous_tint, bg_color


def root_view_kwargs(name, /, **kwargs):
    """The kwargs that draw a render func as the active surface's ROOT meltygui
    window: a closable window pinned to the OS window (layouts — draw_rows /
    draw_columns — register their edges on the enclosing WINDOW, so the
    root must be one), sized to it, never dragged, its own close and shadow
    off (the OS window has the chrome). Shared by app._draw_root (a
    `@glfw_window` over a render func) and Melty.draw_surface_root (a
    `glfw_window=True` child).

    ``with_header=`` (the decorator's, the call's) puts the MELTY HEADER in
    the chrome row — the same header a studio window wears, beside the OS
    window's controls: `show_name` names it after the OS window's title
    (`display_name`, live through a retitle), the tint chip edits the
    view's tint, and the geometry keeps clear of the controls on both sides
    — `header_indent` past a left-side group, and a `with_header_end` that
    only claims the right group's width (titlebar.draw_header_controls), so
    the wrapper right-aligns and clips the header exactly as it does
    around a header's close button. No collapse arrow (`is_tree=False`): an
    OS window does not fold to its header. Without a header the body
    starts under the control row (titlebar.top_inset). OS-decorated surfaces
    use the same row for the app settings cog, without custom window controls.

    Caller kwargs win over every pinned value (``show_header=False`` hides
    a passed header, ``disable_scroll=False`` scrolls the root, a ``name=``
    the fallback ``name`` — positional-only, so a child's own kwargs pass)."""
    width, height, top = Melty.root_fill
    header = kwargs.get('with_header') is not None
    if header:
        surface = Melty.current_surface()
        left_inset, right_inset = titlebar.chrome_insets()
        # The header row IS the chrome row: the view starts at the very
        # top and the body is immediately under the header.
        height, top = height + top, 0.0
        kwargs.setdefault('is_tree', False)
        kwargs.setdefault('show_tint', True)
        kwargs.setdefault('display_name', surface.title if surface is not None else name)
        kwargs.setdefault('header_indent', left_inset)
        kwargs.setdefault('with_header_end', titlebar.draw_header_controls if right_inset > 0 else None)
    else:
        kwargs.setdefault('with_header_end', None)
    # OS bodies orchestrate render calls and shortcuts every requested frame.
    # Descendant bodies have their own caches; the app still sleeps when idle.
    pinned = dict(name=name, closable=True, draggable=False, frame_pinned=True,
                  window_pos=(0, top), width=width, height=height,
                  auto_resize=False, show_header=header, with_footer=None, shadow=False, show_bg=True,
                  selectable=False, use_cache=False, disable_scroll=True, indent_size=5,
                  initial={'width': width, 'height': height, 'window_pos': (0, top)})
    return pinned | kwargs



def draw_surface_frame(surface, *, transparent=False, chrome=False, request=None, fps_counter=None):
    from meltygui.core.rendering.parameter_core import flush_deferred_writes
    root_flags = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE
                  | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS
                  | imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS | imgui.WINDOW_NO_NAV_INPUTS
                  | imgui.WINDOW_NO_NAV | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS
                  | imgui.WINDOW_NO_SCROLL_WITH_MOUSE)
    views.new_frame()
    io = imgui.get_io()
    disp_w, disp_h = io.display_size
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(disp_w, disp_h)
    imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (0.0, 0.0))
    imgui.push_style_var(imgui.STYLE_WINDOW_BORDERSIZE, 0.0)
    views.begin('main##window_melty', closable=False, flags=root_flags)
    imgui.pop_style_var(2)
    # meltygui only routes pointer events to apps while the root imgui
    # window is hovered (draw_state.hover_eligible).
    Melty.imgui_main_window_hovered = imgui.is_window_hovered()
    Melty.begin_frame()
    # Standalone apps do not run the studio's draw_main. Commit source
    # edits deferred while a picker/slider held the pointer here too.
    flush_deferred_writes()
    # Collision bounds belong to the surface, including when the OS
    # supplies its title bar and cannot accept collision-driven moves.
    os_frame.begin_frame()
    if chrome:
        titlebar.poll_os_window_drag()
    prototype = getattr(surface, '_gui_prototype', None)
    if prototype is None:
        os_frame.solve()
    else:
        prototype.solve_native(os_frame)
    imgui.set_cursor_screen_pos((0, 0))
    imgui.set_item_allow_overlap()
    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    Melty.window_stack.append((surface.name, True))

    radius = titlebar.frame_corner_radius() if transparent else 0.0
    previous_tint, bg_color = root_background(surface, disp_w, disp_h, radius)
    if transparent and Toggles.Melty.window_shadow_lift > 0:
        from meltygui.core.cache.tile_marks import add_shadow
        add_shadow((0, 0, disp_w, disp_h), offset=0.5, corner_radius=radius, clip=False)

    top = titlebar.top_inset()
    imgui.set_cursor_screen_pos((0, top))
    # The root fills the OS MODEL's size (os_frame.content_size): equal
    # to the display except while our own resize is still landing.
    fill_w, fill_h = os_frame.content_size((disp_w, disp_h))
    Melty.root_fill = (float(fill_w), float(fill_h) - top, float(top))   # (w, h below the chrome, top y)
    Melty.root_fill_used = False
    # The body runs INSIDE the ground, as the studio's windows run inside
    # draw_main's show_bg: one bg depth down, the ground's tint and colour
    # on the stacks draw_bg reads for the bleed (the wrapper's own fill).
    Melty.bg_depth += 1
    Melty.bg_stack.append(Melty.style_manager.get_tint())
    Melty.bg_color_stack.append(bg_color)
    try:
        surface.body(surface)
        draw_render_hosts()
    finally:
        Melty.root_fill = None
        Melty.bg_depth -= 1
        Melty.bg_stack.pop()
        Melty.bg_color_stack.pop()
        Melty.style_manager.set_imgui_tint(*previous_tint)
    titlebar.paint_window_controls(draw_list)
    Melty.end_frame()
    if request is not None:
        Melty.finish_surface_root(request, surface)
    os_frame.flush()
    Melty.window_stack.pop()
    draw_list.channels_merge()
    Melty.channels_split = False
    views.end()
    titlebar.draw_titlebar(surface.window)
    if Toggles.show_fps and fps_counter is not None:
        # Here in the root loop, outside every view and Melty.end_frame:
        # no cache, tile or invalidation decides whether it is current.
        titlebar.paint_fps(Melty.overlay_top_channel(), chrome, fps_counter.frame_ms, fps_counter.fps)
    views.end_frame()
