"""Native host density is available before point-sized font atlases build."""
import importlib.util
import os
from pathlib import Path

import pytest


def bootstrap():
    path = Path(__file__).resolve().parents[1] / 'meltygui/platforms/ios/Python/melty_ios_bootstrap.py'
    spec = importlib.util.spec_from_file_location('retina_bootstrap_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('density', [1, 2, 3])
def test_host_density_replaces_previous_run_before_font_build(monkeypatch, density):
    monkeypatch.setenv('HUB_IMGUI_PIXEL_DENSITY', '1')
    host = bootstrap()
    host.configure_pixel_density({'pixel_density': density})
    # The runtime binding rasterizes point-sized fonts using this value,
    # independently of the first frame's later display_fb_scale update.
    assert float(os.environ['HUB_IMGUI_PIXEL_DENSITY']) == density
    host.configure_pixel_density({'pixel_density': 2})
    assert float(os.environ['HUB_IMGUI_PIXEL_DENSITY']) == 2


@pytest.mark.parametrize('density', [0, -1, 9, float('nan'), float('inf')])
def test_invalid_density_fails_before_native_or_app_imports(monkeypatch, density):
    monkeypatch.delenv('HUB_IMGUI_PIXEL_DENSITY', raising=False)
    with pytest.raises(ValueError, match='display pixel density'):
        bootstrap().initialize({'pixel_density': density})
    assert 'HUB_IMGUI_PIXEL_DENSITY' not in os.environ
