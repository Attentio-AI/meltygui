"""Tile-local dependency discovery and persisted sibling bindings."""
from dataclasses import dataclass
import inspect
from meltygui.core.rendering.injected_state import state_parameters, owned_state
from meltygui.core.rendering.view_identity import get_draw_state, view_unique
from meltygui.model.tile_model import Split
from meltygui.state.view_reference import DrawStateSource, view_identifier


@dataclass
class TileEndpoint:
    tile: object
    renderer: object
    draw_state: object
    parameters: dict
    states: dict
    path: tuple = ()


def prepare_endpoint(tile, path=()):
    renderer = tile.render_func
    if renderer is None:
        return None
    unique, _ = view_unique('', inspect.unwrap(renderer).__name__, key=tile.id,
                            unique_name=view_identifier(renderer))
    ds = get_draw_state(unique)
    parameters = getattr(renderer, '__state_parameters__', None)
    if parameters is None:
        parameters = state_parameters(renderer)
    states = {name: owned_state(ds, name, annotation)
              for name, annotation in parameters.items()
              if not isinstance(annotation, DrawStateSource)}
    return TileEndpoint(tile, renderer, ds, parameters, states, path)


def prepare_endpoints(tree):
    """Allocate all local state first, including sources later in draw order."""
    result = {}

    def visit(node, path=()):
        if isinstance(node, Split):
            for index, child in enumerate(node.children):
                visit(child, path + (index,))
        else:
            endpoint = prepare_endpoint(node, path)
            if endpoint is not None:
                result[node.id] = endpoint

    visit(tree)
    return result


def bindings_for(endpoint):
    # Per-renderer bindings survive switching away and back; no object references.
    return getattr(endpoint.tile, 'links', {}).get(view_identifier(endpoint.renderer), {})


def candidates(endpoint, parameter, endpoints):
    annotation = endpoint.parameters[parameter]
    for source in endpoints.values():
        if source.tile is endpoint.tile:
            continue
        if isinstance(annotation, DrawStateSource):
            if view_identifier(source.renderer) == view_identifier(annotation.view):
                yield (source.tile.id, view_identifier(source.renderer), None), source.draw_state
        else:
            for name, value in source.states.items():
                if isinstance(value, annotation):
                    yield (source.tile.id, view_identifier(source.renderer), name), value


AUTO = "auto"


def tree_distance(first, second):
    """Number of parent/child edges through the leaves' lowest common ancestor."""
    shared = 0
    for a, b in zip(first, second):
        if a != b:
            break
        shared += 1
    return len(first) + len(second) - 2 * shared


def selected_candidate(endpoint, parameter, endpoints, *, preview_auto=False):
    """Resolve one manual or Auto binding; never change a saved manual target."""
    saved = bindings_for(endpoint).get(parameter)
    available = list(candidates(endpoint, parameter, endpoints))
    if saved == AUTO or preview_auto:
        if not available:
            return None
        distances = {identity: tree_distance(endpoint.path, endpoints[identity[0]].path)
                     for identity, _value in available}
        minimum = min(distances.values())
        nearest = [(identity, value) for identity, value in available
                   if distances[identity] == minimum]
        remembered = getattr(endpoint.tile, '_auto_link_sources', {})
        key = (view_identifier(endpoint.renderer), parameter)
        previous = (tuple(saved) if preview_auto and isinstance(saved, (tuple, list))
                    else remembered.get(key))
        chosen = next((item for item in nearest if item[0] == previous), None)
        if chosen is None:
            chosen = min(nearest, key=lambda item: (str(item[0][0]), item[0][1], item[0][2] or ''))
        if not preview_auto and previous != chosen[0]:
            endpoint.tile._auto_link_sources = {**remembered, key: chosen[0]}
        return chosen
    if saved is not None:
        return next((item for item in available if item[0] == tuple(saved)), None)
    return None


def resolve_parameters(endpoint, endpoints):
    values = dict(endpoint.states)
    for name, annotation in endpoint.parameters.items():
        if isinstance(annotation, DrawStateSource):
            values[name] = None
        selected = selected_candidate(endpoint, name, endpoints)
        if selected is not None:
            values[name] = selected[1]
    return values


def set_binding(endpoint, parameter, identity):
    # Replace dictionaries so @live observes edits and the host cache refreshes.
    links = dict(getattr(endpoint.tile, 'links', {}))
    bindings = dict(bindings_for(endpoint))
    previous = bindings.get(parameter)
    if identity == AUTO and isinstance(previous, (tuple, list)):
        remembered = getattr(endpoint.tile, '_auto_link_sources', {})
        key = (view_identifier(endpoint.renderer), parameter)
        endpoint.tile._auto_link_sources = {**remembered, key: tuple(previous)}
    if identity is None:
        bindings.pop(parameter, None)
    else:
        bindings[parameter] = identity
    links[view_identifier(endpoint.renderer)] = bindings
    endpoint.tile.links = links


def retire_endpoints(previous, current):
    """Stop watching inputs of removed views, even if saved draw states survive."""
    from meltygui.core.melty import Melty
    dependencies = getattr(Melty.cache, 'parameter_dependencies', None)
    if dependencies is None:
        return
    for identity, old in previous.items():
        new = current.get(identity)
        if new is None or new.draw_state is not old.draw_state:
            dependencies.bind(old.draw_state, {})
