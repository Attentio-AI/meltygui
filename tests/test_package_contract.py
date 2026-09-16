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
                 'resources/fontawesome-webfont.ttf', 'core/legacy_modules.json',
                 'state/module_map.json'):
        assert (root / path).is_file(), path


def test_native_namespace():
    assert meltygui.imgui.__name__ == 'meltygui_imgui'


def test_moved_view_and_widget_classes_restore_from_saved_names():
    import pickle
    from meltygui.state.annotation_state import AnnotationOverride
    from meltygui.model.import_graph_model import ImportGraph

    for module, name, expected in (
        ('meltygui.views.core_meta', 'AnnotationOverride', AnnotationOverride),
        ('meltygui.widgets.file_graph', 'ImportGraph', ImportGraph),
    ):
        saved_global = f'c{module}\n{name}\n.'.encode()
        assert pickle.loads(saved_global) is expected
        assert LSDUnpickler(io.BytesIO(saved_global)).load() is expected
    historical = b'csrc.lsd.gl_gui.view.core_views.core_meta\nAnnotationOverride\n.'
    assert LSDUnpickler(io.BytesIO(historical)).load() is AnnotationOverride
