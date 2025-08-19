import imgui
import colorsys


class ImGuiStyleManager:
    def __init__(self):
        self.saved_colors = None
        self.saved_style = None
        self.saved_rgb = None
        self.current_rgb = (0.0, 0.0, 0.0)
        self.hsv = (0.0, 0.0, 0.0)
        self.root = None

        # List of all color indices we need to save/restore
        self.color_indices = [
            imgui.COLOR_TEXT,
            imgui.COLOR_TEXT_DISABLED,
            imgui.COLOR_WINDOW_BACKGROUND,
            imgui.COLOR_CHILD_BACKGROUND,
            imgui.COLOR_POPUP_BACKGROUND,
            imgui.COLOR_BORDER,
            imgui.COLOR_BORDER_SHADOW,
            imgui.COLOR_FRAME_BACKGROUND,
            imgui.COLOR_FRAME_BACKGROUND_HOVERED,
            imgui.COLOR_FRAME_BACKGROUND_ACTIVE,
            imgui.COLOR_TITLE_BACKGROUND,
            imgui.COLOR_TITLE_BACKGROUND_ACTIVE,
            imgui.COLOR_TITLE_BACKGROUND_COLLAPSED,
            imgui.COLOR_SCROLLBAR_BACKGROUND,
            imgui.COLOR_SCROLLBAR_GRAB,
            imgui.COLOR_SCROLLBAR_GRAB_HOVERED,
            imgui.COLOR_SCROLLBAR_GRAB_ACTIVE,
            imgui.COLOR_CHECK_MARK,
            imgui.COLOR_SLIDER_GRAB,
            imgui.COLOR_SLIDER_GRAB_ACTIVE,
            imgui.COLOR_BUTTON,
            imgui.COLOR_BUTTON_HOVERED,
            imgui.COLOR_BUTTON_ACTIVE,
            imgui.COLOR_HEADER,
            imgui.COLOR_HEADER_HOVERED,
            imgui.COLOR_HEADER_ACTIVE,
            imgui.COLOR_SEPARATOR,
            imgui.COLOR_SEPARATOR_HOVERED,
            imgui.COLOR_SEPARATOR_ACTIVE,
            imgui.COLOR_RESIZE_GRIP,
            imgui.COLOR_RESIZE_GRIP_HOVERED,
            imgui.COLOR_RESIZE_GRIP_ACTIVE,
            imgui.COLOR_TAB,
            imgui.COLOR_TAB_HOVERED,
            imgui.COLOR_TAB_ACTIVE,
            imgui.COLOR_TAB_UNFOCUSED,
            imgui.COLOR_TAB_UNFOCUSED_ACTIVE,
            imgui.COLOR_PLOT_LINES,
            imgui.COLOR_PLOT_LINES_HOVERED,
            imgui.COLOR_PLOT_HISTOGRAM,
            imgui.COLOR_PLOT_HISTOGRAM_HOVERED,
            imgui.COLOR_TEXT_SELECTED_BACKGROUND,
            imgui.COLOR_DRAG_DROP_TARGET,
            imgui.COLOR_NAV_HIGHLIGHT,
            imgui.COLOR_NAV_WINDOWING_HIGHLIGHT,
            imgui.COLOR_NAV_WINDOWING_DIM_BACKGROUND,
        ]

    def set_root(self, root):
        self.root = root

    def make_custom(self, r, g, b, value, saturation_scale=1.0, alpha=1.0):
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        modified_rgb = colorsys.hsv_to_rgb(h, s * saturation_scale, value)
        imgui_color = imgui.get_color_u32_rgba(modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

        def _unpack_color(packed_color):
            """Convert a packed u32 color to RGBA components (0-1 range)"""
            return (
                ((packed_color >> 0) & 0xFF) / 255.0,  # R
                ((packed_color >> 8) & 0xFF) / 255.0,  # G
                ((packed_color >> 16) & 0xFF) / 255.0,  # B
            )
        return _unpack_color(imgui_color)

    def make_color(self, value, saturation_scale=1.0, alpha=1.0):
        h, s, v = self.hsv
        value = (v * self.root.global_style.secondary_value) + value

        modified_rgb = colorsys.hsv_to_rgb(h, s * saturation_scale, value)
        imgui_color = imgui.get_color_u32_rgba(modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)
        return imgui_color

    def make_color_rgb(self, r, g, b, saturation_scale=0.4, alpha=1.0, factor=0.3, value=0.5):
        """
        Create an ImGui color from RGB values with optional saturation and alpha adjustments.
        Args:
            r, g, b: RGB values between 0 and 1
            saturation_scale: Scale for saturation (default is 1.0)
            alpha: Alpha value (default is 1.0)
        Returns:
            Packed u32 color value
        """

        def mix(r1, g1, b1, r2, g2, b2, alpha):
            """Mix two colors with alpha blending"""
            return (
                r1 * (1 - alpha) + r2 * alpha,
                g1 * (1 - alpha) + g2 * alpha,
                b1 * (1 - alpha) + b2 * alpha
            )

        hn, sn, vn = colorsys.rgb_to_hsv(r, g, b)
        h, s, v = self.hsv

        modified_rgb = colorsys.hsv_to_rgb(h, saturation_scale, value)

        # Apply alpha blending with the original color
        modified_rgb = mix(r, g, b, modified_rgb[0], modified_rgb[1], modified_rgb[2], factor)

        return modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha

    def make_color_style_imgui(self, input, alpha=1.0):
        """
        Create an ImGui color from a dictionary input with saturation and alpha adjustments.
        Args:
            input: Dictionary containing 'value', 'saturation', 'alpha', and optional 'max_value'
        Returns:
            Packed u32 color value
        """
        color = self.make_color_style(input, alpha)
        return imgui.get_color_u32_rgba(color[0], color[1], color[2], alpha)

    def make_color_style_rgb(self, r, g, b, input,factor=0.6):
        """
        Create an ImGui color from RGB values with optional saturation and alpha adjustments.
        Args:
            r, g, b: RGB values between 0 and 1
            saturation_scale: Scale for saturation (default is 1.0)
            alpha: Alpha value (default is 1.0)
        Returns:
            Packed u32 color value
        """
        h, s, v = self.hsv

        value = input["value"]

        saturation_scale = input["saturation"]
        value = (v * self.root.global_style.base_value) + value
        if 'max_value' in input:
            value = min(value, input["max_value"])
        alpha = input["alpha"]

        modified_rgb = colorsys.hsv_to_rgb(h, s * saturation_scale, value)

        def mix(r1, g1, b1, r2, g2, b2, alpha):
            """Mix two colors with alpha blending"""
            return (
                r1 * (1 - alpha) + r2 * alpha,
                g1 * (1 - alpha) + g2 * alpha,
                b1 * (1 - alpha) + b2 * alpha
            )

        # Apply alpha blending with the original color
        modified_rgb = mix(r, g, b, modified_rgb[0], modified_rgb[1], modified_rgb[2], factor)

        return modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha

    def make_color_style(self, input, alpha=1.0):
        h, s, v = self.hsv

        value = input["value"]

        saturation_scale = input["saturation"]
        value = (v * self.root.global_style.base_value) + value
        if 'max_value' in input:
            value = min(value, input["max_value"])
        alpha = input["alpha"]

        modified_rgb = colorsys.hsv_to_rgb(h, s * saturation_scale, value)
        return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

    def make(self, value, saturation_scale=1.0, alpha=1.0):
        def _unpack_color(packed_color):
            """Convert a packed u32 color to RGBA components (0-1 range)"""
            return (
                ((packed_color >> 0) & 0xFF) / 255.0,  # R
                ((packed_color >> 8) & 0xFF) / 255.0,  # G
                ((packed_color >> 16) & 0xFF) / 255.0,  # B
                ((packed_color >> 24) & 0xFF) / 255.0  # A
            )

        color = self.make_color(value, saturation_scale, alpha)
        unpacked = _unpack_color(color)
        return (unpacked[0], unpacked[1], unpacked[2])

    def make_color_unpacked(self, value, saturation_scale=1.0, alpha=1.0):
        def _unpack_color(packed_color):
            """Convert a packed u32 color to RGBA components (0-1 range)"""
            return (
                ((packed_color >> 0) & 0xFF) / 255.0,  # R
                ((packed_color >> 8) & 0xFF) / 255.0,  # G
                ((packed_color >> 16) & 0xFF) / 255.0,  # B
                ((packed_color >> 24) & 0xFF) / 255.0  # A
            )
        color = self.make_color(value, saturation_scale, alpha)
        return _unpack_color(color)

    def save_style(self):
        """Save the current ImGui style and colors"""
        # style = imgui.get_style()
        self.saved_rgb = self.current_rgb

        return self.saved_rgb
        # # Save colors by their indices
        # self.saved_colors = {i: tuple(style.colors[i]) for i in self.color_indices}
        #
        # # Save other style variables
        # self.saved_style = {
        #     'alpha': style.alpha,
        #     'window_padding': style.window_padding,
        #     'window_rounding': style.window_rounding,
        #     'frame_padding': style.frame_padding,
        #     'frame_rounding': style.frame_rounding,
        #     'item_spacing': style.item_spacing,
        #     'item_inner_spacing': style.item_inner_spacing,
        #     'touch_extra_padding': style.touch_extra_padding,
        #     'indent_spacing': style.indent_spacing,
        #     'scrollbar_size': style.scrollbar_size,
        #     'grab_min_size': style.grab_min_size
        # }

    def restore(self, saved_rgb=None):
        if saved_rgb is not None:
            self.set_imgui_tint(*saved_rgb)
        else:
            self.set_imgui_tint(*self.saved_rgb)

    def restore_style(self, saved_rgb=None):
        """Restore the previously saved style and colors"""

        if saved_rgb is not None:
            self.set_imgui_tint(*saved_rgb)
        else:
            self.set_imgui_tint(*self.saved_rgb)
        # if self.saved_colors is None or self.saved_style is None:
        #     print("Warning: No style saved to restore")
        #     # Print stack trace
        #     import traceback
        #     traceback.print_stack()
        #     return False
        #
        # style = imgui.get_style()
        # # Restore colors
        # for i, color in self.saved_colors.items():
        #     style.colors[i] = color
        #
        # # Restore other style variables
        # style.alpha = self.saved_style['alpha']
        # style.window_padding = self.saved_style['window_padding']
        # style.window_rounding = self.saved_style['window_rounding']
        # style.frame_padding = self.saved_style['frame_padding']
        # style.frame_rounding = self.saved_style['frame_rounding']
        # style.item_spacing = self.saved_style['item_spacing']
        # style.item_inner_spacing = self.saved_style['item_inner_spacing']
        # style.touch_extra_padding = self.saved_style['touch_extra_padding']
        # style.indent_spacing = self.saved_style['indent_spacing']
        # style.scrollbar_size = self.saved_style['scrollbar_size']
        # style.grab_min_size = self.saved_style['grab_min_size']
        # return True

    def tint_gold(self):
        self.save_style()
        self.set_imgui_tint(0.6, 0.5, 0.3)

    def tint_default(self):
        self.save_style()
        self.set_imgui_tint(0.3, 0.6, 0.7)


    def get_tint(self):
        """
        Returns the current tint color as an RGB tuple.
        Returns:
            Tuple of (r, g, b) values in the range [0.0, 1.0]
        """
        return self.current_rgb

    def set_imgui_tint(self, r, g, b, a=1.0):
        """
        Sets a global tint color for ImGui by adjusting all style colors based on a single RGB color.
        Args:
            r, g, b: RGB values between 0 and 1
        """

        if self.root is None:
            return

        self.current_rgb = (r, g, b)
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        self.hsv = (h, s, v)
        style = imgui.get_style()
        colors = style.colors

        def make_color(input, alpha=1.0):
            value = input["value"]
            saturation_scale = input["saturation"]
            value = (v * self.root.global_style.base_value) + value
            alpha = input["alpha"]

            modified_rgb = colorsys.hsv_to_rgb(h, s * saturation_scale, value)
            return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)
        # def make_color(value, saturation_scale=1.0, alpha=1.0):
        #     value = (v * self.root.global_style.base_value) + value
        #
        #     modified_rgb = colorsys.hsv_to_rgb(h, s * saturation_scale, value)
        #     return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

        glb_cst = self.root.global_style.main_const

        colors[imgui.COLOR_TEXT] = make_color(glb_cst["widget"]["text"])  # Nearly white text
        colors[imgui.COLOR_TEXT_DISABLED] = make_color(glb_cst["widget"]["text_disabled"])  # Grayed out text

        colors[imgui.COLOR_WINDOW_BACKGROUND] = make_color(glb_cst["window"]["background"])  # Dark background
        colors[imgui.COLOR_CHILD_BACKGROUND] = make_color(glb_cst["window"]["child_bg"])
        colors[imgui.COLOR_POPUP_BACKGROUND] = make_color(glb_cst["window"]["popup_bg"])
        colors[imgui.COLOR_BORDER] = make_color(glb_cst["window"]["border"])

        # Title
        colors[imgui.COLOR_TITLE_BACKGROUND] = make_color(glb_cst["window"]["title_bg"])
        colors[imgui.COLOR_TITLE_BACKGROUND_ACTIVE] = make_color(glb_cst["window"]["title_bg_active"])
        colors[imgui.COLOR_TITLE_BACKGROUND_COLLAPSED] = make_color(glb_cst["window"]["title_bg_collapsed"])

        # Headers
        colors[imgui.COLOR_HEADER] = make_color(glb_cst["window"]["header"])
        colors[imgui.COLOR_HEADER_HOVERED] = make_color(glb_cst["window"]["header_hovered"])
        colors[imgui.COLOR_HEADER_ACTIVE] = make_color(glb_cst["window"]["header_active"])

        colors[imgui.COLOR_RESIZE_GRIP] = make_color(glb_cst["widget"]["resize_grip"])
        colors[imgui.COLOR_RESIZE_GRIP_HOVERED] = make_color(glb_cst["widget"]["resize_hovered"])
        colors[imgui.COLOR_RESIZE_GRIP_ACTIVE] = make_color(glb_cst["widget"]["resize_active"])

        # Buttons
        colors[imgui.COLOR_BUTTON] = make_color(glb_cst["widget"]["button"])
        colors[imgui.COLOR_BUTTON_HOVERED] = make_color(glb_cst["widget"]["button_hovered"])
        colors[imgui.COLOR_BUTTON_ACTIVE] = make_color(glb_cst["widget"]["button_active"])

        # Frame backgrounds
        colors[imgui.COLOR_FRAME_BACKGROUND] = make_color(glb_cst["frame"]["frame_bg"])
        colors[imgui.COLOR_FRAME_BACKGROUND_HOVERED] = make_color(glb_cst["frame"]["frame_hovered"])
        colors[imgui.COLOR_FRAME_BACKGROUND_ACTIVE] = make_color(glb_cst["frame"]["frame_active"])

        colors[imgui.COLOR_CHECK_MARK] = make_color(glb_cst["widget"]["check_mark"])
        colors[imgui.COLOR_TEXT_SELECTED_BACKGROUND] = make_color(glb_cst["widget"]["text_selected_bg"])
        # Tabs
        colors[imgui.COLOR_TAB] = make_color(glb_cst["tab"]["tab"])
        colors[imgui.COLOR_TAB_HOVERED] = make_color(glb_cst["tab"]["tab_hovered"])
        colors[imgui.COLOR_TAB_ACTIVE] = make_color(glb_cst["tab"]["tab_active"])



        # Borders and separators
        colors[imgui.COLOR_SEPARATOR] = make_color(glb_cst["frame"]["separator"])

        # Sliders, scrollbars
        colors[imgui.COLOR_SLIDER_GRAB] = make_color(glb_cst["widget"]["slider_grab"])
        colors[imgui.COLOR_SLIDER_GRAB_ACTIVE] = make_color(glb_cst["widget"]["slider_grab_active"])
        colors[imgui.COLOR_SCROLLBAR_GRAB] = make_color(glb_cst["widget"]["scrollbar_grab"])
        colors[imgui.COLOR_SCROLLBAR_GRAB_HOVERED] = make_color(glb_cst["widget"]["scrollbar_grab_hovered"])
        colors[imgui.COLOR_SCROLLBAR_GRAB_ACTIVE] = make_color(glb_cst["widget"]["scrollbar_grab_active"])
