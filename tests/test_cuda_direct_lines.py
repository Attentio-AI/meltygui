"""The CUDA line renderer must consume the original strided allocation."""
import pytest
import torch

from meltygui.tensor import cuda_march as kernels, line_kernels
from meltygui.core.graph_core import slice_lines

pytestmark = pytest.mark.skipif(not torch.cuda.is_available() or not kernels.available(),
                                reason='CUDA and meltygui-pycuda required')


@pytest.mark.parametrize('dtype', [torch.float32, torch.bfloat16, torch.int64])
def test_strided_cuda_lines_are_read_in_place(dtype, monkeypatch):
    base = torch.arange(120, device='cuda').reshape(4, 30).to(dtype)
    source = base[:, 1::2]
    def forbid(*args, **kwargs):
        raise AssertionError('source tensor was copied')
    with monkeypatch.context() as no_copy:
        for name in ('cpu', 'numpy', 'clone', 'contiguous', 'to'):
            no_copy.setattr(torch.Tensor, name, forbid)
        lines, _, _ = slice_lines(source, x_dim=1, line_dim=0, materialize=False)
        assert lines.untyped_storage().data_ptr() == source.untyped_storage().data_ptr()
        assert lines.dtype == source.dtype and lines.stride() == source.stride()
        stats = line_kernels.ranges(lines)
        out = torch.empty((64, 80, 4), device='cuda', dtype=torch.float16)
        lut = torch.ones((2, 3), device='cuda')
        line_kernels.render(lines, stats, out, lut, y_range=(0., 120.), unit=(40., 32.))
    assert (out[..., 3] > 0).any().item()
    torch.testing.assert_close(stats[:, 0], source[:, 0].float())
    torch.testing.assert_close(stats[:, 1], source[:, -1].float())
    # No re-pack/re-upload: mutate the same allocation and render it again.
    old_pixels = out.clone()
    source.fill_(60)
    line_kernels.render(lines, stats, out, lut, y_range=(0., 120.), unit=(40., 32.))
    assert not torch.equal(old_pixels, out)


@pytest.mark.parametrize('dtype', [torch.float32, torch.bfloat16, torch.int64])
def test_volume_mapping_keeps_original_cuda_storage(dtype, monkeypatch):
    from meltygui.tensor.voxel_playground import slice_volume_view
    source = torch.arange(240, device='cuda').to(dtype).reshape(4, 5, 12)[:, :, 1::2]
    def forbid(*args, **kwargs):
        raise AssertionError('source tensor was copied')
    with monkeypatch.context() as no_copy:
        for name in ('cpu', 'numpy', 'clone', 'contiguous', 'to'):
            no_copy.setattr(torch.Tensor, name, forbid)
        volume = slice_volume_view(source)
        assert volume.view.untyped_storage().data_ptr() == source.untyped_storage().data_ptr()
        assert volume.view.dtype == dtype
        assert volume.view.storage_offset() == source.storage_offset()


def test_cuda_complex_is_not_silently_materialized():
    from meltygui.tensor.voxel_playground import slice_volume_view
    source = torch.ones((2, 3, 4), dtype=torch.complex64, device='cuda')
    with pytest.raises(ValueError, match='convert explicitly'):
        slice_volume_view(source)
