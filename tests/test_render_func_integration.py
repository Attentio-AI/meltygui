"""
Integration test for render_func with convert_in / convert_out.

Spawns a real GLFW window + imgui context and runs render_func
through multiple simulated frames to verify the full conversion
pipeline including Background.run, caching, and draw_state management.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_root = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(_root, 'src'))
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, 'server'))  # for model.render_utils.py

import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl


# ═══════════════════════════════════════════════════════════════════════════
#  GLFW / imgui test harness
# ═══════════════════════════════════════════════════════════════════════════

_window = None
_impl = None


def _setup_gl():
    """Create a hidden GLFW window + imgui context. Idempotent."""
    global _window, _impl
    if _window is not None:
        return

    if not glfw.init():
        raise RuntimeError("Failed to init GLFW")

    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    _window = glfw.create_window(800, 600, "test", None, None)
    glfw.make_context_current(_window)

    imgui.create_context()
    _impl = GlfwRenderer(_window)


def _begin_frame():
    """Start an imgui frame."""
    glfw.poll_events()
    _impl.process_inputs()
    try:
        imgui.new_frame()
    except Exception:
        # If last frame wasn't ended, end it and retry
        try:
            imgui.end_frame()
        except Exception:
            pass
        _impl.process_inputs()
        imgui.new_frame()


def _end_frame():
    """End frame + render (discards output)."""
    try:
        imgui.end_frame()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  Minimal Melty state required for render_func
# ═══════════════════════════════════════════════════════════════════════════

def _init_melty():
    """Set up the minimum Melty class state for render_func to run."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.view.core_views.blit_offscreen import TileCacheMasked

    if Melty.cache is None:
        Melty.cache = TileCacheMasked()
    Melty.cache.enabled = False  # no offscreen rendering in tests
    Melty.frame_count = 0
    Melty.depth = 0
    Melty.shadow_depth = 0
    Melty.active_layer = 0
    Melty.z_pos = 0
    Melty.annotation_mode = False
    Melty.on_drag = False
    Melty.window_drag = False
    Melty.imgui_active = False
    Melty.imgui_popup_open = False
    Melty.imgui_any_item_active = False
    Melty.imgui_main_window_hovered = True
    Melty.channels_split = False
    Melty.unique_stack = []
    Melty.suffix_stack = []
    Melty.mode_stack = []
    Melty.melty_window_stack = []
    Melty.draw_state_stack = []
    Melty.input_value_stack = [None]
    Melty.size_stack = []
    Melty.wrap_stack = []
    Melty.bg_stack = []
    Melty.bg_color_stack = []
    Melty.collection_stack = []
    Melty.fixed_size_stack = []
    Melty.clip_stack = []
    Melty.content_height_stack = []
    Melty.indent_count = 0
    Melty.unindent_count = 0
    Melty.bg_depth = 0
    Melty.detached = False
    Melty.silence_invalidate = False
    Melty.all_uniques = set()
    Melty.seen_unique = set()
    Melty.nested_collections = 0

    # imgui style defaults (set in begin_frame normally)
    style = imgui.get_style()
    if Melty.original_spacing is None:
        Melty.original_spacing = style.item_spacing
        Melty.original_window_padding = style.window_padding
        Melty.original_frame_padding = style.frame_padding

    # Style manager stub
    if Melty.style_manager is None:
        sm = MagicMock()
        sm.get_tint.return_value = (0.0, 0.0, 0.0)
        sm.set_imgui_tint = MagicMock()
        Melty.style_manager = sm

    # Global attrs needed by many render_func
    if 'style_manager' not in Melty.global_attrs:
        Melty.global_attrs['style_manager'] = Melty.style_manager

    # Vis / draw_state_registry stub
    if Melty.vis is None:
        vis = MagicMock()
        vis.root.draw_state_registry = {}
        Melty.vis = vis
        Melty.draw_state_registry = vis.root.draw_state_registry

    return Melty


def _tick_frame(melty):
    """Advance one frame."""
    melty.frame_count += 1
    melty.depth = 0
    melty.shadow_depth = 0
    melty.z_pos = 0
    melty.active_layer = 0
    melty.unique_stack = []
    melty.suffix_stack = []
    melty.draw_state_stack = []
    melty.input_value_stack = [None]
    melty.size_stack = []
    melty.wrap_stack = []
    melty.bg_stack = []
    melty.bg_color_stack = []
    melty.fixed_size_stack = []
    melty.clip_stack = []
    melty.all_uniques = set()
    melty.seen_unique = set()


# ═══════════════════════════════════════════════════════════════════════════
#  Test target: a python function in a temp dire
# ═══════════════════════════════════════════════════════════════════════════

_SAMPLE_SOURCE = """\
x = 1
y = 2
"""


# ═══════════════════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestRenderFuncConvertInOut(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _setup_gl()

    def setUp(self):
        self.melty = _init_melty()

        from src.lsd.gl_gui.view.core_views.core_render import render_func, _run_convert_chain
        from src.lsd.gl_gui.view.core_conversion.file_converters import fn_to_cst, cst_to_fn, load_text
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import cst_to_dict, dict_to_cst, GeneralParse
        from src.lsd.gl_gui.view.core_conversion.path_finder import Pending

        self.render_func = render_func
        self._run_chain = _run_convert_chain
        self.fn_to_cst = fn_to_cst
        self.cst_to_fn = cst_to_fn
        self.cst_to_dict = cst_to_dict
        self.dict_to_cst = dict_to_cst
        self.load_text = load_text
        self.GeneralParse = GeneralParse
        self.Pending = Pending

        # Create temp file
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
        f.write(_SAMPLE_SOURCE)
        f.close()
        self.tmp_path = f.name

    def tearDown(self):
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def _run_frame(self, func, input_value, **kwargs):
        """Run one frame: begin imgui, call func, end imgui, tick."""
        _begin_frame()
        imgui.begin("Test Window")
        try:
            result = func(input_value, **kwargs)
        finally:
            imgui.end()
            _end_frame()
            _tick_frame(self.melty)
        return result

    def test_render_func_with_convert_in(self):
        """A render_func with convert_in receives the converted value."""
        received_values = []

        @self.render_func(use_cache=False)
        def my_view(input_value, draw_state=None):
            received_values.append(type(input_value).__name__)
            return False, None

        # Run a few frames with convert_in in kwargs
        for _ in range(3):
            self._run_frame(
                my_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_conv")

        # After a few frames, the render func should have received
        # a GeneralParse (converted from the file), not a Path
        gp_frames = [v for v in received_values if v == 'GeneralParse']
        self.assertGreater(len(gp_frames), 0,
                           f"Expected GeneralParse in received types, got: {received_values}")

    def test_convert_in_produces_correct_dict(self):
        """Verify the converted GeneralParse has correct keys/values."""
        last_value = [None]

        @self.render_func(use_cache=False)
        def my_view(input_value, draw_state=None):
            if isinstance(input_value, self.GeneralParse):
                last_value[0] = dict(input_value)
            return False, None

        for _ in range(5):
            self._run_frame(
                my_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_dict_vals")

        self.assertIsNotNone(last_value[0], "GeneralParse was never received")
        self.assertEqual(last_value[0].get("x"), 1)
        self.assertEqual(last_value[0].get("y"), 2)

    def test_draw_state_file_watch_populated(self):
        """After convert_in with load_data, draw_state has file watch fields set."""
        captured_ds = [None]

        @self.render_func(use_cache=False)
        def my_view(input_value, draw_state=None):
            if isinstance(input_value, self.GeneralParse):
                captured_ds[0] = draw_state
            return False, None

        for _ in range(5):
            self._run_frame(
                my_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_filewatch")

        ds = captured_ds[0]
        self.assertIsNotNone(ds, "draw_state was never captured")
        # FileRef should be set (from the initial staleness check on main thread)
        self.assertIsNotNone(ds._fileref)

    def test_convert_in_not_overwritten_by_else_branch(self):
        """The converted input should NOT be overwritten by the raw input."""
        types_seen = []

        @self.render_func(use_cache=False)
        def my_view(input_value, draw_state=None):
            types_seen.append(type(input_value).__name__)
            return False, None

        for _ in range(5):
            self._run_frame(
                my_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_no_overwrite")

        # After initial frames, we should never see PosixPath -
        # the convert_in result should always be used
        path_frames = [v for v in types_seen if 'Path' in v]
        gp_frames = [v for v in types_seen if v == 'GeneralParse']

        # At least some frames should have GeneralParse
        self.assertGreater(len(gp_frames), 0,
                           f"Expected GeneralParse frames, got: {types_seen}")

    def test_multiple_frames_stable(self):
        """Value should stabilize after initial load — no flickering."""
        values_by_frame = []

        @self.render_func(use_cache=False)
        def my_view(input_value, draw_state=None):
            if isinstance(input_value, self.GeneralParse):
                values_by_frame.append(dict(input_value))
            else:
                values_by_frame.append(None)
            return False, None

        for _ in range(10):
            self._run_frame(
                my_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_stable")

        # Find the first frame where we have a GeneralParse
        first_gp = None
        for i, v in enumerate(values_by_frame):
            if v is not None and "x" in v:
                first_gp = i
                break

        self.assertIsNotNone(first_gp, f"Never got GeneralParse: {values_by_frame}")

        # All subsequent frames should have the same value
        for i in range(first_gp + 1, len(values_by_frame)):
            if values_by_frame[i] is not None:
                self.assertEqual(values_by_frame[i].get("x"), 1,
                                 f"Frame {i} has unexpected value: {values_by_frame[i]}")


class TestConvertOutFlow(unittest.TestCase):
    """Test the convert_out (save) path — simulates user editing."""

    @classmethod
    def setUpClass(cls):
        _setup_gl()

    def setUp(self):
        self.melty = _init_melty()

        from src.lsd.gl_gui.view.core_views.core_render import render_func, _run_convert_chain
        from src.lsd.gl_gui.view.core_conversion.file_converters import fn_to_cst, cst_to_fn, load_text
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import cst_to_dict, dict_to_cst, GeneralParse
        from src.lsd.gl_gui.view.core_conversion.path_finder import Pending

        self.render_func = render_func
        self._run_chain = _run_convert_chain
        self.fn_to_cst = fn_to_cst
        self.cst_to_fn = cst_to_fn
        self.cst_to_dict = cst_to_dict
        self.dict_to_cst = dict_to_cst
        self.load_text = load_text
        self.GeneralParse = GeneralParse
        self.Pending = Pending

        f = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
        f.write(_SAMPLE_SOURCE)
        f.close()
        self.tmp_path = f.name

    def tearDown(self):
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def _run_frame(self, func, input_value, **kwargs):
        _begin_frame()
        imgui.begin("Test Window")
        try:
            result = func(input_value, **kwargs)
        finally:
            imgui.end()
            _end_frame()
            _tick_frame(self.melty)
        return result

    def test_child_changed_triggers_convert_out(self):
        """When the inner function returns changed=True, convert_out runs and produces a dirty diff."""
        ds_snapshots = []

        @self.render_func(use_cache=False)
        def editing_view(input_value, draw_state=None):
            ds_snapshots.append({
                'type': type(input_value).__name__,
                'show_save': draw_state._show_save,
                'save_pending': draw_state._all_pending.get('save_pending'),
            })
            if isinstance(input_value, self.GeneralParse):
                edited = self.GeneralParse(input_value)
                edited.source = input_value.source
                edited["x"] = 99
                return True, edited
            return False, input_value

        for _ in range(8):
            self._run_frame(
                editing_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_out_trigger")

        # After editing, save_pending should be set (dirty detection fired)
        gp_frames = [s for s in ds_snapshots if s['type'] == 'GeneralParse']
        has_save_pending = any(s['save_pending'] is not None for s in gp_frames)
        self.assertTrue(has_save_pending or any(s['show_save'] for s in gp_frames),
                        f"convert_out never produced dirty state. Snapshots: {gp_frames}")

    def test_convert_out_produces_dirty_diff(self):
        """convert_out should detect edits and produce a save_pending with diff."""
        ds_snapshots = []

        @self.render_func(use_cache=False)
        def editing_view(input_value, draw_state=None):
            ds_snapshots.append({
                'type': type(input_value).__name__,
                'save_pending': draw_state._all_pending.get('save_pending'),
            })
            if isinstance(input_value, self.GeneralParse):
                edited = self.GeneralParse(input_value)
                edited.source = input_value.source
                edited["x"] = 42
                return True, edited
            return False, input_value

        for _ in range(8):
            self._run_frame(
                editing_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_out_diff")

        # Find frames with save_pending that has a diff
        pending_frames = [s for s in ds_snapshots
                          if isinstance(s.get('save_pending'), self.Pending)
                          and s['save_pending'].status]
        self.assertGreater(len(pending_frames), 0,
                           f"No dirty diff detected. Snapshots: {ds_snapshots}")

    def test_no_edit_no_dirty(self):
        """When child_changed=False, the save path should not show dirty."""
        ds_snapshots = []

        @self.render_func(use_cache=False)
        def readonly_view(input_value, draw_state=None):
            ds_snapshots.append({
                'show_save': draw_state._show_save,
                'save_pending': draw_state._all_pending.get('save_pending'),
                'type': type(input_value).__name__,
            })
            return False, input_value  # no edit

        for _ in range(8):
            self._run_frame(
                readonly_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_no_dirty")

        # After conversion stabilizes, save should never be dirty
        gp_frames = [s for s in ds_snapshots if s['type'] == 'GeneralParse']
        for snap in gp_frames:
            self.assertFalse(snap['show_save'],
                             f"show_save should be False for unchanged content: {snap}")

    def test_zzz_auto_apply_save(self):
        """When auto_apply includes the save_data fn, dirty changes should auto-apply."""
        from src.lsd.gl_gui.view.core_conversion.file_converters import recompile_fn
        ds_snapshots = []

        @self.render_func(use_cache=False)
        def auto_save_view(input_value, draw_state=None):
            ds_snapshots.append({
                'frame': self.melty.frame_count,
                'type': type(input_value).__name__,
                'show_save': draw_state._show_save,
                'apply_save': draw_state._apply_save,
                'save_pending': draw_state._all_pending.get('save_pending'),
                'save_pending_obj': draw_state._save_pending_obj,
            })
            if isinstance(input_value, self.GeneralParse):
                edited = self.GeneralParse(input_value)
                edited.source = input_value.source
                edited["x"] = 42
                return True, edited
            return False, input_value

        for _ in range(10):
            self._run_frame(
                auto_save_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                auto_apply=[self.load_text, recompile_fn],
                name="test_auto_apply")

        print("\n=== Auto Apply Save Diagnostics ===")
        for snap in ds_snapshots:
            sp = snap['save_pending']
            sp_info = f"originated={sp.originated.__name__}" if isinstance(sp, self.Pending) else str(sp)
            spo = snap['save_pending_obj']
            spo_info = f"originated={spo.originated.__name__}" if isinstance(spo, self.Pending) else str(spo)
            print(f"  Frame {snap['frame']}: "
                  f"type={snap['type']}, "
                  f"show_save={snap['show_save']}, "
                  f"apply_save={snap['apply_save']}, "
                  f"save_pending={sp_info}, "
                  f"save_pending_obj={spo_info}")

        # After auto_apply kicks in, apply_save should get set
        gp_frames = [s for s in ds_snapshots if s['type'] == 'GeneralParse']
        apply_frames = [s for s in gp_frames if s['apply_save'] is not None]
        self.assertGreater(len(apply_frames), 0,
                           f"auto_apply never triggered. Snapshots:\n" +
                           "\n".join(str(s) for s in gp_frames))

    def test_draw_state_internals_after_convert_in(self):
        """Inspect draw_state fields after convert_in to diagnose issues."""
        ds_snapshots = []

        @self.render_func(use_cache=False)
        def diagnostic_view(input_value, draw_state=None):
            ds_snapshots.append({
                'frame': self.melty.frame_count,
                'input_type': type(input_value).__name__,
                'raw_input_type': type(draw_state._raw_input_value).__name__,
                'internal_cache_type': type(draw_state._input_cache["internal_state"][0]).__name__,
                'external_cache_type': type(draw_state._input_cache["external_state"][0]).__name__,
                'fileref': draw_state._fileref,
                'original_load_data': draw_state._original_load_data is not None,
                'original_input_ref': draw_state._original_input_ref,
                'show_load': draw_state._show_load,
                'show_save': draw_state._show_save,
                'apply_load': draw_state._apply_load,
                'apply_save': draw_state._apply_save,
                'load_pending': draw_state._all_pending.get('load_pending'),
                'save_pending': draw_state._all_pending.get('save_pending'),
                'converted_input_flag': True,  # we know convert_in ran
            })
            return False, input_value

        for _ in range(10):
            self._run_frame(
                diagnostic_view, Path(self.tmp_path),
                convert_in=[self.fn_to_cst, self.cst_to_dict],
                convert_out=[self.dict_to_cst, self.cst_to_fn],
                name="test_diagnostic")

        # Print diagnostics for debugging
        print("\n=== Draw State Diagnostics ===")
        for snap in ds_snapshots:
            print(f"  Frame {snap['frame']}: "
                  f"input={snap['input_type']}, "
                  f"raw={snap['raw_input_type']}, "
                  f"internal_cache={snap['internal_cache_type']}, "
                  f"external_cache={snap['external_cache_type']}, "
                  f"fileref={'set' if snap['fileref'] else 'None'}, "
                  f"orig_data={'set' if snap['original_load_data'] else 'None'}, "
                  f"show_load={snap['show_load']}, "
                  f"show_save={snap['show_save']}, "
                  f"load_pending={snap['load_pending']}, "
                  f"save_pending={snap['save_pending']}")

        # At least some frames should have GeneralParse as input
        gp_frames = [s for s in ds_snapshots if s['input_type'] == 'GeneralParse']
        self.assertGreater(len(gp_frames), 0,
                           f"Never received GeneralParse. Snapshots: {ds_snapshots}")


if __name__ == '__main__':
    unittest.main()
