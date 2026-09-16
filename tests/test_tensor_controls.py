"""Dimension controls coordinate reusable local groups, without an app model."""
import inspect
from types import SimpleNamespace

import pytest

from meltygui.core.graphics.gl_state import GLState
from meltygui.model.tensor_model import TensorDim, TensorDims
from meltygui.view import tensor_view


class LocalPanel:
    def __init__(self):
        self._view_children = {}

    @property
    def _parent(self):
        raise AssertionError('A picker must not look beyond its local panel')


class Parameters(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.writes = []

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        super().__setitem__(key, value)


def panel():
    parent = LocalPanel()
    values = Parameters(dim_names=('batch', 'token', 'feature'),
                        x_dim=TensorDim(0), y_dim=TensorDim(1), z_dim=TensorDim(2),
                        sort_dim=TensorDim(1), mean_dims=TensorDims((0,)))
    for key in ('x_dim', 'y_dim', 'z_dim', 'sort_dim', 'mean_dims'):
        parent._view_children[key] = SimpleNamespace(
            _parent=parent, _view_func=tensor_view.draw_tensor_dim,
            _kwargs={'key': key}, _collection=values)
    return parent, values


def test_selection_swaps_only_a_direct_sibling_through_the_collection(monkeypatch):
    first, values = panel()
    second, other_values = panel()
    # A stale child-index entry from another parent must not join this group.
    first._view_children['other_group'] = second._view_children['y_dim']
    style = object()
    received = {}

    def pick(draw_state, options, labels, selected, multi, **context):
        received.update(labels=labels, context=context)
        return True, [1]

    monkeypatch.setattr(tensor_view, '_draw_dim_tabs', pick)
    changed, value = inspect.unwrap(tensor_view.draw_tensor_dim)(
        values['x_dim'], draw_state=first._view_children['x_dim'],
        ui_scale=1.75, style_manager=style)
    assert changed and type(value) is TensorDim and value == 1
    assert values.writes == [('y_dim', TensorDim(0))]
    assert type(values['y_dim']) is TensorDim
    assert values['z_dim'] == 2 and values['sort_dim'] == 1
    assert not other_values.writes
    assert received == dict(labels=['off', 'batch', 'token', 'feature'],
                            context=dict(ui_scale=1.75, style_manager=style))
    # The collection renderer applies the returned value normally.
    values['x_dim'] = value
    assert tuple(values[key] for key in ('x_dim', 'y_dim', 'z_dim')) == (1, 0, 2)


@pytest.mark.parametrize('key,selection', [('x_dim', -1), ('sort_dim', 0)])
def test_off_and_non_axis_pickers_do_not_swap_siblings(key, selection, monkeypatch):
    parent, values = panel()
    monkeypatch.setattr(tensor_view, '_draw_dim_tabs', lambda *args, **kwargs: (True, [selection]))
    changed, value = inspect.unwrap(tensor_view.draw_tensor_dim)(
        values[key], draw_state=parent._view_children[key], collection=values)
    assert changed and value == selection and type(value) is TensorDim
    assert not values.writes


def test_multi_selection_preserves_its_type_and_local_group(monkeypatch):
    parent, values = panel()
    monkeypatch.setattr(tensor_view, '_draw_dim_tabs', lambda *args, **kwargs: (True, [2, 0]))
    changed, value = inspect.unwrap(tensor_view.draw_tensor_dim)(
        values['mean_dims'], draw_state=parent._view_children['mean_dims'])
    assert changed and value == (0, 2) and type(value) is TensorDims
    assert not values.writes


def test_tab_geometry_uses_supplied_scale_style_and_local_depth(monkeypatch):
    buttons, backgrounds, extents = [], [], []
    monkeypatch.setattr(tensor_view, 'imgui', SimpleNamespace(
        get_cursor_screen_pos=lambda: (10, 20),
        set_cursor_screen_pos=lambda position: None,
        calc_text_size=lambda label: SimpleNamespace(x=30),
        dummy=lambda width, height: extents.append((width, height))))
    monkeypatch.setattr(tensor_view, 'flat_button',
                        lambda *args, **kwargs: buttons.append(kwargs) or False)
    monkeypatch.setattr(tensor_view, 'draw_bg', lambda **kwargs: backgrounds.append(kwargs))
    draw_state = SimpleNamespace(content_width=500, depth_and_layer=(7, 2))
    style = object()
    for scale in (1.0, 2.0):
        assert tensor_view._draw_dim_tabs(draw_state, [0], ['batch'], [0], False,
                                          ui_scale=scale, style_manager=style) == (False, [0])
    assert buttons[1]['height'] == buttons[0]['height'] * 2
    assert extents[1][1] == extents[0][1] * 2
    assert all(background['depth'] == 7 and background['style_manager'] is style
               for background in backgrounds)


def test_label_atlas_keeps_resources_until_text_or_supplied_font_changes(monkeypatch):
    baked, deleted = [], []

    def bake(texts, font=None):
        baked.append((texts, font))
        return SimpleNamespace(texture_id=len(baked)), {}

    monkeypatch.setattr(tensor_view, 'bake_texts', bake)
    monkeypatch.setattr('meltygui.core.graphics.gl_state.current_context', lambda: 42)
    monkeypatch.setattr(tensor_view.gl, 'glDeleteTextures', lambda ids: deleted.extend(ids))
    state = GLState()
    first_font, second_font = object(), object()
    first = tensor_view._label_atlas(state, ('batch',), font=first_font)
    assert tensor_view._label_atlas(state, ('batch',), font=first_font) is first
    second = tensor_view._label_atlas(state, ('batch',), font=second_font)
    assert second is not first
    tensor_view._label_atlas(state, ('token',), font=second_font)
    assert baked == [(('batch',), first_font), (('batch',), second_font), (('token',), second_font)]
    state.release()
    GLState.flush_deletes()
    assert sorted(deleted) == [1, 2, 3]
