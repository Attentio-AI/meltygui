"""A tile's editor picker and the selected render function's content."""
from meltygui import imgui
from meltygui.model.tile_model import Tile
from meltygui.view.dropdown_view import draw_dropdown


def renderer_decoration(renderer):
    """The kwargs a ``@render_func(...)`` decoration stamped on ``renderer``
    (its ``__header_defaults__``; a plain callable may carry one for its picker
    row), else an empty dict."""
    decoration = getattr(renderer, "__header_defaults__", None)
    return decoration if isinstance(decoration, dict) else {}


def renderer_label(renderer):
    """The picker label for a renderer: its decoration's icon and
    display_name when available, else its function name."""
    decoration = renderer_decoration(renderer)
    name = decoration.get("display_name") or renderer.__name__
    icon = decoration.get("icon")
    return f"{icon} {name}" if icon else name


def renderer_tint(renderer):
    """The renderer's own ``@render_func(tint=...)`` (never the tint a caller
    passes when invoking it), or None."""
    tint = renderer_decoration(renderer).get("tint")
    if isinstance(tint, (tuple, list)) and len(tint) >= 3:
        return tuple(tint)
    return None


def draw_tile_links(endpoint, endpoints, width, height):
    """One compact picker, with a section of choices for each dependency."""
    from meltygui.core.layout.tile_links import bindings_for, candidates, set_binding
    from meltygui.state.view_reference import DrawStateSource
    bindings = bindings_for(endpoint)
    choices = {}
    linked = missing = 0
    for name, annotation in endpoint.parameters.items():
        fallback = "Unlinked" if isinstance(annotation, DrawStateSource) else "Local state"
        saved = bindings.get(name)
        prefix = "✓ " if saved is None else ""
        choices[f"{prefix}{name}: {fallback}"] = (name, None)
        found = False
        for identity, _value in candidates(endpoint, name, endpoints):
            source = endpoints[identity[0]]
            label = f"{source.tile.name or renderer_label(source.renderer)} · {source.tile.id}"
            if identity[2] is not None:
                label += f" / {identity[2]}"
            active = saved is not None and tuple(saved) == identity
            choices[f"{'✓ ' if active else ''}{name}: {label}"] = (name, identity)
            found |= active
        linked += found
        if saved is not None and not found:
            missing += 1
            # Keep the unavailable selection visible; never silently retarget it.
            choices[f"{name}: Source unavailable"] = (name, tuple(saved))
    label = f"Links: {linked}/{len(endpoint.parameters)}"
    if missing:
        label = f"Links: {missing} missing"
    changed, selection = draw_dropdown(
        label, collection=choices, name="Sibling links", key=f"{endpoint.tile.id}:links",
        show_name=False, show_header=False, display_label=label,
        width=width, height=height,
    )
    if changed and selection is not None:
        set_binding(endpoint, *selection)
    return changed


def draw_tile_content(input_value: Tile, width, height, multi_instance_renderers=(),
                      layout_frame=None, use_cache=False, endpoints=None):
    """The tile's editor picker and its selected renderer. ``use_cache`` puts
    the renderer on the blit cache (the wrapper's mark_start_offscreen /
    mark_end_offscreen around its body): a tile whose view was not invalidated
    draws its captured texture instead of running the renderer."""
    # Change the picker size here; it sits in the tile's bottom-left corner and
    # renderers may opt into the remaining row with tile_toolbar=True.
    picker_height = 28.0
    picker_width = 180.0
    tile = input_value
    endpoint = endpoints.get(tile.id) if endpoints is not None else None
    has_links = endpoint is not None and bool(endpoint.parameters)
    # Fixed allocation depends on the signature, never on dynamic labels.
    picker_width = min(picker_width, max(0.0, (width - 4.0) / 2)) if has_links else min(picker_width, width)
    choices = {"Empty": None}
    row_tints = {}
    for renderer in multi_instance_renderers:
        label = renderer_label(renderer)
        if label in choices:
            label = f"{label} ({renderer.__name__})"
        choices[label] = renderer
        tint = renderer_tint(renderer)
        if tint is not None:
            row_tints[renderer] = tint
    left, top = imgui.get_cursor_screen_pos()
    content_height = max(0.0, height - picker_height)
    imgui.set_cursor_screen_pos((left, top + content_height))
    selected, renderer = draw_dropdown(
        tile.render_func, collection=choices, name="Editor type", key=tile.id,
        show_name=False, width=min(picker_width, width), height=picker_height,
        show_header=False, row_tints=row_tints,
        display_label="Empty" if tile.render_func is None else renderer_label(tile.render_func),
    )
    if selected:
        tile.render_func = renderer

    changed = selected
    if selected and endpoints is not None:
        from meltygui.core.layout.tile_links import prepare_endpoint, retire_endpoints
        previous = {tile.id: endpoint} if endpoint is not None else {}
        endpoint = prepare_endpoint(tile)
        retire_endpoints(previous, {tile.id: endpoint} if endpoint is not None else {})
        if endpoint is None:
            endpoints.pop(tile.id, None)
        else:
            endpoints[tile.id] = endpoint
    toolbar_left = min(picker_width + 4.0, width)
    injected = {}
    if endpoint is not None:
        from meltygui.core.layout.tile_links import resolve_parameters
        if endpoint.parameters:
            link_width = min(200.0, max(0.0, width - toolbar_left))
            imgui.set_cursor_screen_pos((left + toolbar_left, top + content_height))
            changed |= draw_tile_links(endpoint, endpoints, link_width, picker_height)
            toolbar_left = min(toolbar_left + link_width + 4.0, width)
        injected = resolve_parameters(endpoint, endpoints)
        injected['draw_state'] = endpoint.draw_state
    if tile.render_func is not None:
        from meltygui.core.melty import Melty
        imgui.set_cursor_screen_pos((left, top))
        toolbar = renderer_decoration(tile.render_func).get("tile_toolbar", False)
        renderer_height = height if toolbar else content_height
        toolbar_kwargs = {}
        if toolbar:
            # Local geometry keeps view state and process ownership in the renderer.
            toolbar_kwargs["tile_toolbar_rect"] = (
                toolbar_left, content_height, max(0.0, width - toolbar_left), picker_height)
        # A renderer that lays out wider than its tile (a view's own minimums)
        # is cut at the tile, never drawn over its neighbours.
        Melty.push_clip((left, top, left + width, top + renderer_height))
        try:
            content_changed, value = tile.render_func(
                tile.input_value,
                unique_name=f"{tile.render_func.__module__}.{tile.render_func.__qualname__}",
                width=width,
                height=renderer_height,
                auto_resize=False, show_header=False, use_cache=use_cache,
                key=tile.id, instance=tile.id, layout_frame=layout_frame, **toolbar_kwargs, **injected,
            )
        finally:
            Melty.pop_clip()
        if content_changed:
            tile.input_value = value
            if endpoint is not None and Melty.cache is not None:
                dependencies = getattr(Melty.cache, 'parameter_dependencies', None)
                if dependencies is not None:
                    for source in (endpoint.draw_state, *endpoint.states.values()):
                        dependencies.invalidate(Melty.cache, source)
        changed |= content_changed
    imgui.set_cursor_screen_pos((left, top + height))
    return changed, tile