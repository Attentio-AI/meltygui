"""Retained drawing shares the host context and its single ImGui frame."""
import pytest
import meltygui_imgui as imgui
from conftest import _ensure_gl_context
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


@pytest.mark.parametrize('height', [64., 32.6, 8192.])
def test_nested_capture_never_creates_or_switches_context(height, monkeypatch):
    _ensure_gl_context()
    host = imgui.get_current_context()
    cache = RetainedGui()
    def forbidden(*args, **kwargs):
        raise AssertionError('A retained view must use the host ImGui context')
    monkeypatch.setattr(imgui, 'create_context', forbidden)
    monkeypatch.setattr(imgui, 'set_current_context', forbidden)
    monkeypatch.setattr(imgui, 'destroy_context', forbidden)
    @cache.gui(width=128, height=height)
    def child():
        assert imgui.get_current_context() is host
        imgui.text('Shared context')
    @cache.gui(width=128, height=height)
    def parent():
        imgui.text('Parent before')
        child()
        assert imgui.get_current_context() is host
        imgui.text('Parent after')
    try:
        parent()
        assert len(cache.captures) == 2
        cache.invalidate(child)
        cache.flush()
        assert imgui.get_current_context() is host
    finally:
        cache.close()


def test_capture_does_not_start_or_finish_the_host_frame(monkeypatch):
    _ensure_gl_context()
    cache = RetainedGui()
    @cache.gui(width=128, height=64)
    def body():
        imgui.button('Button', width=40, height=20)
    imgui.new_frame()
    imgui.begin('single frame host')
    def forbidden(*args, **kwargs):
        raise AssertionError('The host owns the frame lifecycle')
    try:
        with monkeypatch.context() as scope:
            scope.setattr(imgui, 'new_frame', forbidden)
            scope.setattr(imgui, 'render', forbidden)
            body()
            cache.invalidate(body)
            cache.flush()
            assert imgui.is_frame_active()
        imgui.end()
        imgui.render()
    finally:
        cache.close()


def test_capture_scope_restores_mouse_and_parent_cursor():
    _ensure_gl_context()
    imgui.new_frame()
    imgui.begin('scope host')
    io = imgui.get_io()
    io.mouse_pos = (310, 155)
    imgui.set_cursor_screen_pos((40, 60))
    capture = imgui.DrawCapture()
    try:
        capture.begin(128, 64, 300, 150)
        assert tuple(io.mouse_pos) == (10, 5)
        assert tuple(imgui.get_cursor_screen_pos()) == (0, 0)
        imgui.text('Local capture')
        capture.end()
        assert tuple(io.mouse_pos) == (310, 155)
        assert tuple(imgui.get_cursor_screen_pos()) == (40, 60)
        assert capture.draw_list.vtx_buffer_size > 0
    finally:
        capture.close()
        imgui.end()
        imgui.render()


def test_failed_child_restores_shared_style_and_window_stacks():
    _ensure_gl_context()
    cache = RetainedGui()
    @cache.gui(width=128, height=64)
    def failing():
        imgui.begin_child('unfinished child', 100, 50)
        imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (17, 19))
        imgui.push_style_color(imgui.COLOR_TEXT, 1, 0, 0, 1)
        imgui.text('Failure inside child')
        raise ValueError('view failed')
    imgui.new_frame()
    imgui.begin('error recovery host')
    padding = tuple(imgui.get_style().frame_padding)
    try:
        with pytest.raises(ValueError, match='view failed'):
            failing()
        assert tuple(imgui.get_style().frame_padding) == padding
        imgui.text('Host remains usable')
        assert imgui.is_frame_active()
    finally:
        cache.close()
        imgui.end()
        imgui.render()


def _draw_bytes(buffers):
    import ctypes
    return [(ctypes.string_at(vertices, vertex_count * 20), ctypes.string_at(indices, index_count * 4), command_count)
            for _, command_count, vertices, vertex_count, indices, index_count in buffers]


def test_fused_view_scope_matches_the_separate_calls():
    """begin_view/end_view are begin, push_id, begin_group and their closing
    calls in one crossing: same measurement, same captured geometry."""
    _ensure_gl_context()
    def content():
        imgui.text('Measured text')
        imgui.button('Button', width=40, height=20)
    imgui.new_frame()
    imgui.begin('fused scope host')
    try:
        separate, fused = imgui.DrawCapture('parity-separate'), imgui.DrawCapture('parity-fused')
        try:
            separate.begin(128, 64, 10, 20)
            imgui.push_id('view-label')
            imgui.begin_group()
            content()
            imgui.end_group()
            imgui.pop_id()
            expected_size = tuple(imgui.get_item_rect_size())
            expected_live = separate.active_widget
            separate.end()
            expected = _draw_bytes(separate.buffers())
            assert len(expected) == len(separate.draw_lists) == 1 and expected[0][0]

            fused.begin_view(128, 64, 10, 20, b'view-label')
            content()
            width, height, live, buffers = fused.end_view(True)
            assert (width, height) == expected_size and live == expected_live is False
            # An idle view ends with its measurement and reports its buffers.
            assert buffers is not None and _draw_bytes(buffers) == expected == _draw_bytes(fused.buffers())

            # Asked to stay open, the capture still answers item queries until end().
            fused.begin_view(128, 64, 10, 20, b'view-label')
            content()
            width, height, live, buffers = fused.end_view(False)
            assert buffers is None and (width, height) == expected_size
            assert fused.active_widget is False
            fused.end()
            assert _draw_bytes(fused.buffers()) == expected
            with pytest.raises(RuntimeError, match='Unbalanced DrawCapture scope'):
                fused.end_view(True)
        finally:
            separate.close()
            fused.close()
    finally:
        imgui.end()
        imgui.render()


def test_failing_body_unwinds_its_view_scope_and_can_run_again():
    _ensure_gl_context()
    cache = RetainedGui()
    fail = [True]

    @cache.gui(width=128, height=64)
    def body():
        imgui.begin_group()  # left open on purpose when the body fails
        imgui.text('Before the failure')
        if fail[0]:
            raise ValueError('body failed')
        imgui.end_group()

    try:
        with pytest.raises(ValueError, match='body failed'):
            body()
        assert not cache.stack and not cache.backgrounds
        fail[0] = False
        body()
        node = next(iter(cache.records))
        assert node in cache.backgrounds and cache.gpu.capture_bytes(node) > 0
    finally:
        cache.close()
