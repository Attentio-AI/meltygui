"""Production voxel views have no demo side effects or shared error state."""
import subprocess
import sys
from types import SimpleNamespace

from meltygui.state.voxel_state import VoxelState


def test_importing_tensor_views_creates_no_demo_hosts_or_windows():
    result = subprocess.run([sys.executable, '-c', '''
import sys
from meltygui.core.melty import Melty
before = (dict(Melty.render_hosts), dict(Melty.registered_windows))
from meltygui import draw_voxels, draw_line_graph
assert (Melty.render_hosts, Melty.registered_windows) == before
assert draw_voxels.__module__ == 'meltygui.view.voxel_view'
assert 'meltygui.tensor.voxel_playground' not in sys.modules
assert 'meltygui.core.graphics.graph_core' not in sys.modules
'''], close_fds=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cuda_errors_belong_to_each_view_and_clear_on_recovery(monkeypatch):
    import torch
    from meltygui.view import voxel_view
    from meltygui.tensor import cuda_march

    class Resources:
        error = None

        def get(self, key, create, *args, **kwargs):
            if self.error:
                raise RuntimeError(self.error)
            return create()

    resources = Resources()
    first, second = VoxelState(), VoxelState()
    volume = SimpleNamespace(view=torch.zeros(2, 3, 4), shape=(2, 3, 4),
                             nf=None, norm=None, _vol_key=1)
    texture = SimpleNamespace(cuda=lambda device: object())
    camera = dict(threshold=0.3, density=0.7, brightness=1.0, contrast=1.0,
                  centered=False, volume_scale=(1.0, 1.0, 1.0))
    image = object()
    monkeypatch.setattr(voxel_view, 'print_stack_trace', lambda: None)
    monkeypatch.setattr(voxel_view, '_upload_cuda_image', lambda *args: image)
    monkeypatch.setattr(cuda_march, 'build_mip', lambda *args, **kwargs: object())
    monkeypatch.setattr(cuda_march, 'march', lambda *args, **kwargs: None)

    def render(state):
        return voxel_view._cuda_render(resources, volume, 8, 8, state,
            lut_texture=texture, shade=[0.0] * 16, **camera)

    resources.error = 'first volume failed'
    assert render(first) is None
    assert first.cuda_error == 'cuda_march failed: first volume failed'
    assert second.cuda_error is None
    resources.error = None
    assert render(second) is image
    assert first.cuda_error == 'cuda_march failed: first volume failed'
    assert render(first) is image
    assert first.cuda_error is None
