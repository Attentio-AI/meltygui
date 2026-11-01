"""Boolean backgrounds and hit targets follow their host window."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import meltygui_imgui as imgui
from meltygui.view.control_view import draw_bool


class BoolGeometryTests(unittest.TestCase):
    def test_background_contains_label_in_offset_windows(self):
        context = imgui.create_context()
        self.addCleanup(imgui.destroy_context, context)
        from meltygui.core.melty import Melty
        from meltygui.core.styling.style_core import ImGuiStyleManager
        self.enterContext(patch.object(Melty, 'style_manager', ImGuiStyleManager()))
        io = imgui.get_io()
        io.ini_file_name = None
        io.display_size = (1400, 900)
        io.fonts.get_tex_data_as_rgba32()
        for x, width, value in ((0, 300, False), (400, 300, False),
                                (650, 200, True), (400, 45, True)):
            with self.subTest(x=x, width=width, value=value):
                imgui.new_frame()
                imgui.set_next_window_position(x, 80, imgui.ALWAYS)
                imgui.set_next_window_size(500, 300, imgui.ALWAYS)
                imgui.begin('control', flags=imgui.WINDOW_NO_TITLE_BAR)
                imgui.set_cursor_pos((25, 35))
                imgui.begin_group()
                origin = imgui.get_cursor_screen_pos()
                state = SimpleNamespace(abs_left=origin.x, abs_top=origin.y,
                                        width=width, content_width=width,
                                        content_height=21, _bounding_hovered=True)
                draws = Mock()
                label = []
                text_colored = imgui.text_colored

                def record_text(*args):
                    text_colored(*args)
                    label.append((*imgui.get_item_rect_min(), *imgui.get_item_rect_max()))

                with patch.object(imgui, 'get_window_draw_list', return_value=draws), \
                     patch.object(imgui, 'text_colored', record_text), \
                     patch.object(imgui, 'is_mouse_hovering_rect', return_value=True) as hit:
                    changed, result = inspect.unwrap(draw_bool)(value, state, left_mouse_clicked=True)
                imgui.end_group()
                imgui.end()
                imgui.render()
                box = draws.add_rect_filled.call_args_list[0].args[:4]
                self.assertEqual(hit.call_args.args, box)
                self.assertTrue(changed)
                self.assertEqual(result, not value)
                self.assertLessEqual(box[0], label[0][0])
                self.assertGreaterEqual(box[2], label[0][2])
                self.assertLessEqual(box[1], label[0][1])
                self.assertGreaterEqual(box[3], label[0][3])
