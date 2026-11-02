"""Guard the native screenshot feature's zero-work normal frame path.

Pixel correctness still requires a device; these check the performance contract.
"""
from pathlib import Path


HOST = Path(__file__).resolve().parents[1] / "meltygui/platforms/ios/Host/MeltyHost.mm"


def test_screenshot_does_not_retain_drawables_or_run_in_display_callback():
    source = HOST.read_text()
    display = source.split("- (void)metalDisplayLink:", 1)[1]
    assert "HubScreenCapture" not in display
    assert "captureScreenOnRenderThread" not in display
    assert "_layer.framebufferOnly = YES;" in source
    assert "HubScreenCaptureFrame" not in source


def test_screenshot_schedules_one_off_work_on_idle_render_run_loop():
    source = HOST.read_text()
    assert "performSelector:@selector(captureScreenOnRenderThread:) onThread:host->_thread" in source
    capture = source.split("- (void)captureScreenOnRenderThread:", 1)[1].split("#endif", 1)[0]
    assert "encodeFrame:commands target:target" in capture
    assert "nextDrawable" not in capture
    assert "HubScreenCaptureTexture(target, commands" in capture
