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


def draw_tile_picker_label(input_value, draw_state, draw_list):
    """Paint live footer text outside cached pixels, including resize replay."""
    from meltygui.core.runtime.toggles import Tint
    from meltygui.core.melty import Melty
    from meltygui.hdr_color import pack_color
    from meltygui.view.dropdown_view import TRIGGER_TEXT_INSET

    label = "Empty" if input_value is None else renderer_label(input_value)
    # Match draw_dropdown's trigger and flat_button's optical text offsets,
    # rather than centering against the taller footer allocation.
    width = max(18, draw_state.width)
    height = max(25, (draw_state._kwargs or {}).get("trigger_height", 25))
    if imgui.calc_text_size(label).x + 30 > width:
        label = (renderer_decoration(input_value).get("icon")
                 if input_value is not None else None) or "\uf009"
        x = draw_state.abs_left + (width - imgui.calc_text_size(label).x) / 2
    else:
        x = draw_state.abs_left + TRIGGER_TEXT_INSET
    size = imgui.calc_text_size(label)
    x += 2 * Melty.ui_scale
    y = draw_state.abs_top + (height - size.y) / 2 - Melty.ui_scale
    color = pack_color(*Tint.dd_text(requested_tint=draw_state.current_tint)[:3], 1)
    draw_list.push_clip_rect(draw_state.abs_left, draw_state.abs_top,
                             draw_state.abs_left + width,
                             draw_state.abs_top + draw_state.height, True)
    try:
        draw_list.add_text(x, y, color, label)
    finally:
        draw_list.pop_clip_rect()


def parameter_label(endpoint, parameter):
    """Show the exact parameter and required state type in the open picker."""
    from meltygui.state.view_reference import DrawStateSource
    annotation = endpoint.parameters[parameter]
    if isinstance(annotation, DrawStateSource):
        required_type = f"DrawState[{annotation.view.__name__}]"
    else:
        required_type = annotation.__name__
    return f"{parameter}: {required_type}"


def source_icon_path(endpoint):
    """An injected state may expose icon_path for its represented filesystem value."""
    return next((state.icon_path for state in endpoint.states.values()
                 if getattr(state, 'icon_path', None)), None)


def source_display_name(endpoint):
    """The source's own label and its renderer icon, independent of identity."""
    decoration = renderer_decoration(endpoint.renderer)
    label = (endpoint.draw_state.nickname or endpoint.tile.name
             or decoration.get("display_name") or endpoint.renderer.__name__)
    icon = decoration.get("icon")
    return f"{icon} {label}" if icon else label


def source_label(identity, endpoints, available):
    source = endpoints[identity[0]]
    label = source_display_name(source)
    # Count instances, not state parameters offered by the same instance.
    duplicates = sum(source_display_name(endpoints[tile_id]) == label
                     for tile_id in {item[0] for item, _value in available})
    if duplicates > 1:
        label += f" · {source.tile.id}"
    if identity[2] is not None and sum(item[0] == identity[0] for item, _value in available) > 1:
        label += f" / {identity[2]}"
    return label


def source_tint(endpoint):
    """Prefer the live instance's colour to its renderer's default."""
    tint = endpoint.draw_state.current_tint
    if tint is None:
        tint = renderer_tint(endpoint.renderer) or endpoint.tile.tint
    return tuple(tint[:3]) if tint is not None else None


def tile_link_column(endpoint, parameter, endpoints):
    """Describe one parameter column without changing its binding."""
    from meltygui.core.layout.tile_links import AUTO, bindings_for, candidates, selected_candidate
    from meltygui.state.view_reference import DrawStateSource
    annotation = endpoint.parameters[parameter]
    saved = bindings_for(endpoint).get(parameter, AUTO)
    available = list(candidates(endpoint, parameter, endpoints))
    selected = selected_candidate(endpoint, parameter, endpoints)
    fallback = f"\uf1c0 Self contained"
    auto = selected_candidate(endpoint, parameter, endpoints, preview_auto=True)
    auto_label = f"\uf0d0 Auto"
    if auto is not None:
        auto_label += f" → {source_label(auto[0], endpoints, available)}"
    choices = {auto_label: AUTO}
    for identity, _value in available:
        label = source_label(identity, endpoints, available)
        if label in choices or label in ("Source unavailable", fallback):
            label += f" · {identity[0]}"
        choices[label] = identity
    if selected is None and saved is not None and saved != AUTO:
        choices["Source unavailable"] = tuple(saved)
    if selected is not None and selected[0] not in {item[0] for item in available}:
        from meltygui.core.layout.tile_links import endpoint_for_binding
        retained = endpoint_for_binding(endpoints, selected[0])
        choices[f"{source_display_name(retained)} (retained)"] = selected[0]
    choices[fallback] = None
    normalized = tuple(saved) if isinstance(saved, (tuple, list)) else saved
    previews = {identity: endpoints[identity[0]].draw_state
                for identity, _value in available}
    if auto is not None:
        previews[AUTO] = endpoints[auto[0][0]].draw_state
    if not isinstance(annotation, DrawStateSource):
        previews[None] = endpoint.draw_state
    row_tints = {identity: source_tint(endpoints[identity[0]])
                 for identity, _value in available}
    if auto is not None:
        row_tints[AUTO] = source_tint(endpoints[auto[0][0]])
    if not isinstance(annotation, DrawStateSource):
        row_tints[None] = source_tint(endpoint)
    title, subtitle = parameter_label(endpoint, parameter).split(": ", 1)
    return dict(title=title, subtitle=subtitle, choices=choices, selected=normalized,
                previews=previews, tints=row_tints)


def link_trigger_label(endpoint, endpoints):
    """A single chain glyph, replaced by a source's custom icon when present."""
    return "\uf0c1"


def draw_tile_links(endpoint, endpoints, width, height, resize_record=None):
    """One trigger opens independently selectable parameter columns."""
    from meltygui.core.layout.tile_links import set_binding, AUTO, selected_candidate, endpoint_for_binding
    columns, choices, previews, tints, paths = [], {}, {}, {}, {}
    for parameter in endpoint.parameters:
        column = tile_link_column(endpoint, parameter, endpoints)
        rows = []
        for label, binding in column['choices'].items():
            key = f"{parameter}: {label}"
            value = (parameter, binding)
            choices[key] = value
            rows.append((key, label))
            if binding in column['previews']:
                previews[value] = column['previews'][binding]
            tints[value] = column['tints'].get(binding)
            identity = (selected_candidate(endpoint, parameter, endpoints, preview_auto=True)
                        if binding == AUTO else None)
            source_id = identity[0][0] if identity else (binding[0] if isinstance(binding, tuple) else None)
            if source_id in endpoints:
                path = source_icon_path(endpoints[source_id])
                if path:
                    paths[value] = path
        columns.append(dict(title=column['title'], subtitle=column['subtitle'],
                            rows=rows, selected=(parameter, column['selected'])))
    presentation = tuple((column['title'], column['subtitle'], column['selected'],
                          tuple((key, label, tints[choices[key]], paths.get(choices[key]))
                                for key, label in column['rows']))
                         for column in columns)
    trigger_label = link_trigger_label(endpoint, endpoints)
    trigger_paths = {}
    for parameter in endpoint.parameters:
        candidate = selected_candidate(endpoint, parameter, endpoints)
        if candidate:
            path = source_icon_path(endpoint_for_binding(endpoints, candidate[0]))
            if path:
                trigger_paths[0] = path
                break
    result = draw_dropdown(
        (presentation, trigger_label), collection=choices, name="Links", key=f"{endpoint.tile.id}:links",
        show_name=False, show_header=False, display_label=trigger_label,
        trigger_caret=("", ""), text_align="center", text_pad=3,
        menu_columns=columns, menu_revision=presentation, keep_open_on_select=True,
        row_previews=previews, row_tints=tints, row_paths=paths, display_paths=trigger_paths,
        width=width, height=height, return_extras=resize_record is not None)
    changed, selection = result[:2]
    if resize_record is not None and len(result) > 2:
        resize_record.controls.append(result[2])
    if changed:
        parameter, binding = selection
        set_binding(endpoint, parameter, binding)
    return changed


def tile_control_layout(width, height, parameter_count, picker_width=180.0):
    """One compact footer: tile switcher followed by a single link button."""
    parameter_count = int(bool(parameter_count))
    row_height = 28.0
    gap = min(4.0, width / (2 * max(1, parameter_count)))
    if not parameter_count:
        return max(0.0, height - row_height), min(picker_width, width), []
    available = max(0.0, width - gap * parameter_count)
    # Fixed slots keep neighboring controls stationary when a link changes.
    link_width = min(28.0, available / (parameter_count + 1))
    editor_width = min(picker_width, max(0.0, available - parameter_count * link_width))
    slots = [(editor_width + gap + index * (link_width + gap), 0.0, link_width)
             for index in range(parameter_count)]
    return max(0.0, height - row_height), editor_width, slots


def draw_tile_content(input_value: Tile, width, height, multi_instance_renderers=(),
                      layout_frame=None, use_cache=False, endpoints=None, tile_path=(), resize_record=None):
    """The tile's editor picker and its selected renderer. ``use_cache`` puts
    the renderer on the blit cache (the wrapper's mark_start_offscreen /
    mark_end_offscreen around its body): a tile whose view was not invalidated
    draws its captured texture instead of running the renderer."""
    # Change the picker size here; it sits in the tile's bottom-left corner and
    # renderers may opt into the remaining row with tile_toolbar=True.
    picker_height = 28.0
    tile = input_value
    picker_label = "Empty" if tile.render_func is None else renderer_label(tile.render_func)
    picker_width = imgui.calc_text_size(picker_label).x + 30
    endpoint = endpoints.get(tile.id) if endpoints is not None else None
    parameter_count = len(endpoint.parameters) if endpoint is not None else 0
    if resize_record is not None:
        resize_record.link_parameter_count = parameter_count
        resize_record.picker_width = picker_width
    content_height, picker_width, link_slots = tile_control_layout(width, height, parameter_count, picker_width)
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
    changed = False
    toolbar_left = min(picker_width + 4.0, width)
    injected = {}
    if endpoint is not None:
        from meltygui.core.layout.tile_links import resolve_parameters
        if link_slots:
            last_left, _last_top, last_width = link_slots[-1]
            toolbar_left = min(width, last_left + last_width + 4.0)
        injected = resolve_parameters(endpoint, endpoints)
        injected['draw_state'] = endpoint.draw_state
    if tile.render_func is not None:
        from meltygui.core.melty import Melty
        imgui.set_cursor_screen_pos((left, top))
        toolbar = renderer_decoration(tile.render_func).get("tile_toolbar", False)
        if resize_record is not None and endpoint is not None:
            resize_record.body = endpoint.draw_state
            resize_record.toolbar = toolbar
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
                auto_resize=False, show_header=False, use_cache=use_cache, freeze_resize=use_cache,
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
    # A toolbar renderer spans the footer too. Paint the tile controls last
    # so its cached mask cannot flatten their shadows. Fresh control bodies
    # emit standalone shadow marks, which otherwise hide the overlap until
    # those controls are cache-served (e.g. when Tasks loses hover).
    imgui.set_cursor_screen_pos((left, top + content_height))
    result = draw_dropdown(
        tile.render_func, collection=choices, name="Editor type", key=tile.id,
        show_name=False, width=min(picker_width, width), height=picker_height,
        show_header=False, row_tints=row_tints,
        display_label="", trigger_caret=("", ""),
        draw_overlay=draw_tile_picker_label,
        return_extras=resize_record is not None,
    )
    selected, renderer = result[:2]
    if resize_record is not None and len(result) > 2:
        resize_record.controls.append(result[2])
    if selected:
        tile.render_func = renderer

    changed |= selected
    if selected and endpoints is not None:
        from meltygui.core.layout.tile_links import prepare_endpoint, retire_endpoints
        previous = {tile.id: endpoint} if endpoint is not None else {}
        endpoint = prepare_endpoint(tile, endpoint.path if endpoint is not None else tile_path)
        retire_endpoints(previous, {tile.id: endpoint} if endpoint is not None else {},
                         getattr(endpoints, "retained", None))
        if endpoint is None:
            endpoints.pop(tile.id, None)
        else:
            endpoints[tile.id] = endpoint
    if endpoint is not None and not selected:
        for slot_left, slot_top, slot_width in link_slots:
            imgui.set_cursor_screen_pos((left + slot_left, top + content_height + slot_top))
            changed |= draw_tile_links(endpoint, endpoints, slot_width, picker_height, resize_record)
    imgui.set_cursor_screen_pos((left, top + height))
    return changed, tile
