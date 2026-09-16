"""Contracts for the independently installable public package."""
import io
from pathlib import Path
import meltygui
from meltygui.state.load_save_v2 import LSDUnpickler
from meltygui.state.module_names import canonical_name
from meltygui.models.orchestration import Orchestration


def test_public_legacy_class_reference():
    assert LSDUnpickler(io.BytesIO(b'csrc.lsd.gl_gui.model.app_model\nOrchestration\n.')).load() is Orchestration
    assert canonical_name('numpy.ndarray') == 'numpy.ndarray'


def test_packaged_runtime_assets():
    root = Path(meltygui.__file__).parent
    for path in ('png_unfilter.c', 'resources/dejavu/DejaVuSans.ttf',
                 'resources/dejavu/LICENSE.txt', 'resources/JetBrainsMono-Regular.ttf',
                 'resources/fontawesome-webfont.ttf'):
        assert (root / path).is_file(), path


def test_native_namespace():
    assert meltygui.imgui.__name__ == 'meltygui_imgui'
