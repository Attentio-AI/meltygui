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


def parameter_label(endpoint, parameter):
    """Show the exact parameter and required state type in the open picker."""
    from meltygui.state.view_reference import DrawStateSource
    annotation = endpoint.parameters[parameter]
    if isinstance(annotation, DrawStateSource):
        required_type = f"DrawState[{annotation.view.__name__}]"
    else:
        required_type = annotation.__name__
    return f"{parameter}: {required_type}"


def source_label(identity, endpoints, available):
    source = endpoints[identity[0]]
    label = source.tile.name or renderer_label(source.renderer)
    # Keep ordinary labels short, but distinguish duplicate names/parameters.
    duplicates = sum((endpoints[item[0]].tile.name or renderer_label(endpoints[item[0]].renderer)) == label
                     for item, _value in available)
    if duplicates > 1:
        label += f" · {source.tile.id}"
    if identity[2] is not None and sum(item[0] == identity[0] for item, _value in available) > 1:
        label += f" / {identity[2]}"
    return label


def draw_tile_link(endpoint, parameter, endpoints, width, height):
    """A consumer-owned picker for exactly one signature parameter."""
    from meltygui.core.layout.tile_links import AUTO, bindings_for, candidates, set_binding, selected_candidate
    from meltygui.state.view_reference import DrawStateSource
    annotation = endpoint.parameters[parameter]
    saved = bindings_for(endpoint).get(parameter)
    available = list(candidates(endpoint, parameter, endpoints))
    selected = selected_candidate(endpoint, parameter, endpoints)
    fallback = "Unlinked" if isinstance(annotation, DrawStateSource) else "Local state"
    auto_label = "Auto"
    if saved == AUTO and selected is not None:
        auto_label += f" → {source_label(selected[0], endpoints, available)}"
    choices = {auto_label: AUTO, fallback: None}
    for identity, _value in available:
        label = source_label(identity, endpoints, available)
        if label in choices or label == "Source unavailable":
            label += f" · {identity[0]}"
        choices[label] = identity
    if saved == AUTO:
        current = "Auto"
        current += f" → {source_label(selected[0], endpoints, available)}" if selected else " (no match)"
    elif selected is not None:
        current = source_label(selected[0], endpoints, available)
    elif saved is not None:
        current = "Source unavailable"
        choices[current] = tuple(saved)
    else:
        current = fallback
    normalized = tuple(saved) if isinstance(saved, (tuple, list)) else saved
    choices = {(f"✓ {label}" if value == normalized else label): value
               for label, value in choices.items()}
    previews = {identity: endpoints[identity[0]].draw_state
                for identity, _value in available}
    auto = selected_candidate(endpoint, parameter, endpoints, preview_auto=True)
    if auto is not None:
        previews[AUTO] = endpoints[auto[0][0]].draw_state
    if not isinstance(annotation, DrawStateSource):
        previews[None] = endpoint.draw_state
    title = parameter_label(endpoint, parameter)
    # Status participates in the input so cached triggers follow Auto/topology.
    changed, selection = draw_dropdown(
        (saved, current), collection=choices, name=title,
        key=f"{endpoint.tile.id}:link:{parameter}",
        show_name=False, show_header=False, display_label=f"\uf0c1",
        trigger_caret=("", ""), text_align="center", text_pad=3,
        menu_title=title, menu_min_width=260, row_previews=previews,
        width=width, height=height,
    )
    if changed:
        set_binding(endpoint, parameter, selection)
    return changed


def tile_control_layout(width, height, parameter_count):
    """One compact footer: tile switcher followed by fixed-size link buttons."""
    row_height = 28.0
    gap = min(4.0, width / (2 * max(1, parameter_count)))
    if not parameter_count:
        return max(0.0, height - row_height), min(180.0, width), []
    available = max(0.0, width - gap * parameter_count)
    # Preserve 28px icons while allowing the switcher to shrink on small tiles.
    link_width = min(28.0, available / (parameter_count + 1))
    editor_width = min(180.0, max(0.0, available - parameter_count * link_width))
    slots = [(editor_width + gap + index * (link_width + gap), 0.0, link_width)
             for index in range(parameter_count)]
    return max(0.0, height - row_height), editor_width, slots


def draw_tile_content(input_value: Tile, width, height, multi_instance_renderers=(),
                      layout_frame=None, use_cache=False, endpoints=None, tile_path=()):
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
    parameter_count = len(endpoint.parameters) if endpoint is not None else 0
    content_height, picker_width, link_slots = tile_control_layout(width, height, parameter_count)
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
        endpoint = prepare_endpoint(tile, endpoint.path if endpoint is not None else tile_path)
        retire_endpoints(previous, {tile.id: endpoint} if endpoint is not None else {})
        if endpoint is None:
            endpoints.pop(tile.id, None)
        else:
            endpoints[tile.id] = endpoint
    toolbar_left = min(picker_width + 4.0, width)
    injected = {}
    if endpoint is not None:
        from meltygui.core.layout.tile_links import resolve_parameters
        # A renderer change gets its new footer layout on the next frame.
        if not selected:
            for parameter, (slot_left, slot_top, slot_width) in zip(endpoint.parameters, link_slots):
                imgui.set_cursor_screen_pos((left + slot_left, top + content_height + slot_top))
                changed |= draw_tile_link(endpoint, parameter, endpoints, slot_width, picker_height)
            if link_slots:
                last_left, _last_top, last_width = link_slots[-1]
                toolbar_left = min(width, last_left + last_width + 4.0)
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
