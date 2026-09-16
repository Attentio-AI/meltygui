"""Tensor data operations work without loading a viewer or a GPU backend."""
import subprocess
import sys

import numpy as np
import pytest

from meltygui.model import camera_model
from meltygui.model import tensor_model


def test_models_do_not_load_viewer_or_cuda_backend():
    pytest.importorskip('torch')
    result = subprocess.run(
        [sys.executable, '-c', '''
import sys
import torch
from meltygui.model.tensor_model import slice_volume_view
from meltygui.model.camera_model import basis
volume = slice_volume_view(torch.arange(24).reshape(2, 3, 4),
                           nf_on=True, nf_chunk=2)
assert volume.shape == (4, 3, 2)
basis(0.2, 0.4, 0.6)
for module in ('meltygui.tensor.voxel_playground', 'meltygui.tensor.cuda_march',
               'meltygui.view.tensor_view', 'meltygui.view.graph_view'):
    assert module not in sys.modules, module
'''],
        close_fds=False, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_named_axes_and_pinned_slices_preserve_source_storage():
    torch = pytest.importorskip('torch')
    source = torch.arange(120, dtype=torch.int32).reshape(2, 3, 4, 5)
    options = dict(dim_names=('batch', 'channel', 'row', 'column'),
                   z_dim='row', y_dim='channel', x_dim='column', slices=(99,))
    view = tensor_model.slice_volume_view(source, **options)
    expected = source[1].permute(1, 0, 2)
    assert view.view.data_ptr() == expected.data_ptr()
    assert view.view.stride() == expected.stride()
    assert view.view.dtype == source.dtype
    assert view.mapping == (2, 1, 3)
    assert view.source_shape == (2, 3, 4, 5)
    torch.testing.assert_close(view.view, expected)
    materialized, mapping, shape = tensor_model.slice_volume(source, **options)
    torch.testing.assert_close(materialized, expected.float())
    assert materialized.is_contiguous()
    assert (mapping, shape) == (view.mapping, view.source_shape)


@pytest.mark.parametrize('dtype', ['float16', 'float32', 'float64', 'int32', 'bool'])
def test_display_dtype_and_no_copy_contract(dtype):
    torch = pytest.importorskip('torch')
    source = torch.arange(24).reshape(2, 3, 4).to(getattr(torch, dtype))
    view = tensor_model.slice_volume_view(source)
    assert view.view.data_ptr() == source.data_ptr()
    assert view.view.dtype == source.dtype
    materialized, _, _ = tensor_model.slice_volume(source)
    expected = source if dtype in ('float16', 'float32') else source.float()
    torch.testing.assert_close(materialized, expected)


def test_mean_sort_and_signed_normalization():
    torch = pytest.importorskip('torch')
    source = torch.arange(120).reshape(2, 3, 4, 5) - 80
    options = dict(mean_dims=(0, 2), sort_dim=3)
    expected = source.float().sort(dim=3, descending=True).values
    expected = expected.mean(dim=0, keepdim=True)
    expected = expected.mean(dim=2, keepdim=True).expand(1, 3, 4, 5)[0]
    view = tensor_model.slice_volume_view(source, normalize=True, **options)
    torch.testing.assert_close(view.view, expected)
    assert view.norm == (float(expected.min()), float(expected.abs().max()), 2)
    result, _, _ = tensor_model.slice_volume(source, normalize=True, **options)
    torch.testing.assert_close(result, expected / expected.abs().max())


@pytest.mark.parametrize('pad', [False, True])
def test_neural_flow_shape_and_values(pad):
    torch = pytest.importorskip('torch')
    source = torch.arange(30).reshape(2, 3, 5)
    options = dict(nf_on=True, nf_chop=2, nf_along=0, nf_chunk=2, nf_pad=pad)
    view = tensor_model.slice_volume_view(source, **options)
    result, _, _ = tensor_model.slice_volume(source, **options)
    expected = source.float()
    if pad:
        padded = torch.nn.functional.pad(expected, (0, 1))
        expected = torch.cat((padded[:, :, :2], padded[:, :, 2:4], padded[:, :, 4:]), dim=0)
    torch.testing.assert_close(result, expected)
    assert view.shape == tuple(expected.shape)
    assert view.nf == ((2, 0, 2) if pad else (-1, -1, 0))
    assert view.view.data_ptr() == source.data_ptr()


@pytest.mark.parametrize('operation', [tensor_model.slice_volume, tensor_model.slice_volume_view])
def test_empty_tensor_has_readable_error(operation):
    torch = pytest.importorskip('torch')
    with pytest.raises(ValueError, match='empty tensor'):
        operation(torch.empty(2, 0, 3))


def test_camera_round_trip_preserves_view_frame():
    frame = camera_model.basis(0.3, -0.8, 0.6)
    angles = camera_model.decompose(*frame[:2], tilt_hint=0.3, spin_hint=-0.8)
    np.testing.assert_allclose(camera_model.basis(*angles), frame, atol=1e-12)
    matrix = np.asarray(frame)
    np.testing.assert_allclose(matrix @ matrix.T, np.eye(3), atol=1e-12)
