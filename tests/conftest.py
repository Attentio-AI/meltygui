"""
Shared pytest fixtures for imgui/GLFW context management.

Ensures a single imgui context exists across all test files,
and provides frame begin/end helpers that recover from PushID
mismatches by creating fresh contexts.
"""

import os
import sys

_root = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(_root, 'src'))
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, 'server'))

import pytest
import imgui

# Track whether we have a GL context (GLFW window) or just a headless context
_has_gl = False
_window = None
_impl = None

# Create a headless context at import time so all test modules can import
# imgui-dependent code without errors
imgui.create_context()
io = imgui.get_io()
io.display_size = (800, 600)
io.delta_time = 1.0 / 60.0
io.fonts.get_tex_data_as_rgba32()


def _ensure_gl_context():
    """Create a GLFW window + imgui context. For integration tests."""
    global _has_gl, _window, _impl
    if _has_gl:
        return _window, _impl

    import glfw
    from imgui.integrations.glfw import GlfwRenderer

    # End any existing headless frame
    try:
        imgui.end_frame()
    except Exception:
        pass

    if not glfw.init():
        raise RuntimeError("Failed to init GLFW")

    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    _window = glfw.create_window(800, 600, "test", None, None)
    glfw.make_context_current(_window)

    # Create a fresh context for the GL window
    ctx = imgui.create_context()
    imgui.set_current_context(ctx)
    _impl = GlfwRenderer(_window)
    _has_gl = True

    return _window, _impl


def begin_frame():
    """Start an imgui frame, recovering from prior corruption."""
    global _impl
    if _has_gl:
        import glfw
        glfw.poll_events()
        _impl.process_inputs()
    try:
        imgui.new_frame()
    except Exception:
        # Previous frame not ended, create a fresh context
        ctx = imgui.create_context()
        imgui.set_current_context(ctx)
        if _has_gl:
            from imgui.integrations.glfw import GlfwRenderer
            _impl = GlfwRenderer(_window)
            _impl.process_inputs()
        else:
            io = imgui.get_io()
            io.display_size = (800, 600)
            io.fonts.get_tex_data_as_rgba32()
        imgui.new_frame()


def end_frame():
    """End frame, recovering from PushID/PopID mismatches."""
    try:
        imgui.end_frame()
    except Exception:
        # Corrupted frame - create fresh context for next frame
        ctx = imgui.create_context()
        imgui.set_current_context(ctx)
        if _has_gl:
            global _impl
            from imgui.integrations.glfw import GlfwRenderer
            _impl = GlfwRenderer(_window)
        else:
            io = imgui.get_io()
            io.display_size = (800, 600)
            io.fonts.get_tex_data_as_rgba32()


@pytest.fixture(autouse=True)
def _imgui_cleanup():
    """Clean up imgui state after each test."""
    yield
    # End any open frames after each test
    try:
        imgui.end_frame()
    except Exception:
        pass


@pytest.fixture
def gl_context():
    """Fixture for tests that need a real GL context (integration tests)."""
    window, impl = _ensure_gl_context()
    yield window, impl
