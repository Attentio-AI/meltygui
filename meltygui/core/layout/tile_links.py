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


def prepare_endpoint(tile):
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
    return TileEndpoint(tile, renderer, ds, parameters, states)


def prepare_endpoints(tree):
    """Allocate all local state first, including sources later in draw order."""
    result = {}

    def visit(node):
        if isinstance(node, Split):
            for child in node.children:
                visit(child)
        else:
            endpoint = prepare_endpoint(node)
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


def resolve_parameters(endpoint, endpoints):
    values = dict(endpoint.states)
    bindings = bindings_for(endpoint)
    for name, annotation in endpoint.parameters.items():
        if isinstance(annotation, DrawStateSource):
            values[name] = None
        saved = bindings.get(name)
        if saved is not None:
            for identity, value in candidates(endpoint, name, endpoints):
                if tuple(saved) == identity:
                    values[name] = value
                    break
    return values


def set_binding(endpoint, parameter, identity):
    # Replace dictionaries so @live observes edits and the host cache refreshes.
    links = dict(getattr(endpoint.tile, 'links', {}))
    bindings = dict(bindings_for(endpoint))
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
