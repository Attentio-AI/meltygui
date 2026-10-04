"""The standalone runner is inert on import and owns only its UI lifetime."""
from types import SimpleNamespace
from pathlib import Path

import pytest

import meltygui.chat.main as app


def test_help_does_not_open_a_window(monkeypatch):
    monkeypatch.setattr(app, "glfw_window", lambda **kw: pytest.fail("opened window"))
    with pytest.raises(SystemExit) as stopped:
        app.main(["--help"])
    assert stopped.value.code == 0


def test_invalid_project_does_not_start_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "serve_instance", lambda path: pytest.fail("started server"))
    with pytest.raises(SystemExit) as stopped:
        app.main([str(tmp_path / "missing")])
    assert stopped.value.code == 2


def test_existing_instance_does_not_register_or_disconnect(monkeypatch):
    monkeypatch.setattr(app, "hand_over_to_running_instance", lambda path: True)
    monkeypatch.setattr(app, "glfw_window", lambda **kw: pytest.fail("opened window"))
    monkeypatch.setattr(app, "disconnect_chats", lambda: pytest.fail("disconnected"))
    assert app.main([]) == 0


@pytest.mark.parametrize("fails", [False, True])
def test_runner_owns_cleanup_and_project_context(monkeypatch, tmp_path, fails):
    calls, roots = [], []
    monkeypatch.setattr(app, "hand_over_to_running_instance", lambda path: False)
    monkeypatch.setattr(app, "serve_instance", lambda path: lambda: calls.append("closed"))
    monkeypatch.setattr(app, "disconnect_chats", lambda: calls.append("disconnected"))
    monkeypatch.setattr(app, "glfw_window", lambda **kw: lambda fn: roots.append(fn) or fn)
    monkeypatch.setattr(app, "draw_claude_chat", lambda value, **kw: calls.append(kw))
    monkeypatch.setattr(app, "pressed", lambda chord: False)
    geometry = SimpleNamespace(get_content_rect=lambda: (0, 0, 800, 600))
    monkeypatch.setattr(app, "_draw_root", lambda fn, name, value, **kw:
                        fn.__wrapped__(value, draw_state=geometry))
    def run():
        roots[0]()
        if fails:
            raise RuntimeError("render failed")
    monkeypatch.setattr(app.meltygui, "run", run)
    if fails:
        with pytest.raises(RuntimeError, match="render failed"):
            app.main([str(tmp_path)])
    else:
        assert app.main([str(tmp_path)]) == 0
    assert calls[0]["new_project"] == str(tmp_path)
    assert calls[-2:] == ["closed", "disconnected"]


def test_instance_socket_is_released(monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(app, "show_window", lambda: shown.append(True))
    # macOS's default temporary directory can exceed sockaddr_un's path limit.
    monkeypatch.chdir(tmp_path)
    path = Path("instance.sock")
    close = app.serve_instance(path)
    try:
        assert app.hand_over_to_running_instance(path)
        assert shown == [True]
    finally:
        close()
    assert not path.exists()
