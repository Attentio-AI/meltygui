import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import unpack_color
from meltygui.hdr_color import style_color
from meltygui.hdr_color import scale_saturation
import colorsys

from meltygui.core.styling.global_style import GlobalStyle


def cap_brightness(r, g, b, max_b):
    """Cap PERCEIVED brightness (0.299r + 0.587g + 0.114b) at max_b — the
    legibility guard for widget fills that carry light text. Scales the
    channels, so hue and saturation survive (the max-side of the editor's
    _brightness_clamp; kept local because toggles.py imports this module).
    max_b <= 0 disables."""
    if max_b <= 0:
        return r, g, b
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum > max_b:
        k = max_b / lum
        return r * k, g * k, b * k
    return r, g, b


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

    @staticmethod
    def _safe_rgb_to_hsv(r, g, b):
        # EXTENDED sRGB HSV: colours are never clamped into [0, 1] anymore.
        # colorsys is exact on extended values whenever max(r, g, b) > 0 -
        # a channel above 1 lands in v (> 1 = brighter than SDR white), a
        # negative channel in s (> 1 = outside the sRGB gamut, the P3 tints
        # from hdr_color.p3), and hsv_to_rgb hands both straight back. The
        # old per-channel clamp is why draw_bg never showed a tint's HDR
        # headroom or wide gamut (09-07). The one hazard is a max channel
        # ≤ 0 (a black tint like (0.0, -0.005, -0.009), or all-negative):
        # colorsys divides by it, so the only sensible result is black.
        def clean(c):
            try:
                c = float(c)
            except (TypeError, ValueError):
                return 0.0
            return 0.0 if c != c else c  # NaN → 0

        r, g, b = clean(r), clean(g), clean(b)
        if max(r, g, b) <= 0.0:
            return 0.0, 0.0, 0.0
        return colorsys.rgb_to_hsv(r, g, b)

    def make_custom_styled(self, r, g, b, input, alpha=1.0, value=0.5, saturation=None):
        h, s, v = self._safe_rgb_to_hsv(r, g, b)
        value = input["value"] + value

        if saturation is not None:
            saturation_scale = saturation
        else:
            saturation_scale = input["saturation"]
        value = (v * GlobalStyle.base_value) + value
        if 'max_value' in input:
            value = min(value, input["max_value"])

        modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)
        return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

    def make_custom(self, r, g, b, value, saturation_scale=1.0, alpha=1.0):
        h, s, v = self._safe_rgb_to_hsv(r, g, b)
        modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)
        imgui_color = pack_color(modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

        return unpack_color(imgui_color)[:3]

    def make_color(self, value, saturation_scale=1.0, alpha=1.0):
        h, s, v = self.hsv
        value = (v * GlobalStyle.secondary_value) + value

        modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)
        imgui_color = pack_color(modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)
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

        # Pure function of the current tint and the arguments - memoize:
        # flat_button / tabs / dock rows call this twice per widget per
        # frame with a handful of distinct values (two hsv round trips each).
        key = (self.current_rgb, r, g, b, saturation_scale, alpha, factor, value)
        hit = _MAKE_COLOR_RGB_MEMO.get(key)
        if hit is not None:
            return hit

        modified_rgb = self.make_custom(*self.current_rgb, value, saturation_scale=saturation_scale, alpha=alpha)
        # Apply alpha blending with the original color
        modified_rgb = mix(r, g, b, modified_rgb[0], modified_rgb[1], modified_rgb[2], factor)

        result = (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)
        if len(_MAKE_COLOR_RGB_MEMO) > 4096:
            _MAKE_COLOR_RGB_MEMO.clear()      # bounded: drags mint many times
        _MAKE_COLOR_RGB_MEMO[key] = result
        return result

    def make_color_style_imgui(self, input, alpha=1.0):
        """
        Create an ImGui color from a dictionary input with saturation and alpha adjustments.
        Args:
            input: Dictionary containing 'value', 'saturation', 'alpha', and optional 'max_value'
        Returns:
            Packed u32 color value
        """
        color = self.make_color_style(input, alpha)
        return pack_color(color[0], color[1], color[2], alpha)

    def make_color_style_value_imgui(self, input, saturation=None, alpha=1.0, value=0.0):
        """
        Create an ImGui color from a dictionary input with saturation and alpha adjustments.
        Args:
            input: Dictionary containing 'value', 'saturation', 'alpha', and optional 'max_value'
        Returns:
            Packed u32 color value
        """
        color = self.make_color_style_value(input, alpha, saturation=saturation, value=value)
        return pack_color(color[0], color[1], color[2], alpha)


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
        value = (v * GlobalStyle.base_value) + value
        if 'max_value' in input:
            value = min(value, input["max_value"])
        alpha = input["alpha"]

        modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)

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

    def make_color_style_value(self, input, alpha=1.0, value=0.5, saturation=None):
        h, s, v = self.hsv

        value = input["value"] + value

        if saturation is not None:
            saturation_scale = saturation
        else:
            saturation_scale = input["saturation"]
        value = (v * GlobalStyle.base_value) + value
        if 'max_value' in input:
            value = min(value, input["max_value"])

        modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)
        return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

    def make_color_style(self, input, alpha=1.0):
        h, s, v = self.hsv

        value = input["value"]

        saturation_scale = input["saturation"]
        value = (v * GlobalStyle.base_value) + value
        if 'max_value' in input:
            value = min(value, input["max_value"])
        alpha = input["alpha"]

        modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)
        return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

    def make(self, value, saturation_scale=1.0, alpha=1.0):
        _unpack_color = unpack_color

        color = self.make_color(value, saturation_scale, alpha)
        unpacked = _unpack_color(color)
        return (unpacked[0], unpacked[1], unpacked[2])

    def make_color_unpacked(self, value, saturation_scale=1.0, alpha=1.0):
        _unpack_color = unpack_color
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

    def push_tint_fields(self, r, g, b, a=1.0):
        """set_imgui_tint's COLOUR MATH without the imgui table apply: sets
        `current_rgb` / `hsv` (what make_color_* and draw_bg read) and
        returns the previous pair for pop_tint_fields. For draw-list code
        that only needs draw_bg to colour from a tint (the editor's inline
        widgets, ~40 a frame) — the 35-entry imgui table isn't consulted
        there, and applying it twice per widget was half the widget's cost."""
        prev = (self.current_rgb, self.hsv)
        if a < 1.0 and self.current_rgb is not None:
            pr, pg, pb = self.current_rgb
            r = r * a + pr * (1.0 - a)
            g = g * a + pg * (1.0 - a)
            b = b * a + pb * (1.0 - a)
        self.current_rgb = (r, g, b)
        self.hsv = self._safe_rgb_to_hsv(r, g, b)
        return prev

    def pop_tint_fields(self, prev):
        self.current_rgb, self.hsv = prev

    def set_imgui_tint(self, r, g, b, a=1.0):
        """
        Sets a global tint color for ImGui by adjusting all style colors based on a single RGB color.
        Args:
            r, g, b: RGB values between 0 and 1
        """

        # No boot gate here: the studio's `set_root` used to be the first
        # thing that armed this, and a @glfw_window app never called that, so
        # setting tint was a no-op and the whole app painted from the black
        # default (09-12). The colour math needs nothing; only the imgui
        # style table below needs a check.

        # A 4-component tint carries an alpha that controls how much of the
        # current (parent) tint bleeds through. a=1.0 -> use the new color
        # outright; a=0.0 -> keep the parent color in full force. Because every
        # nested layer re-applies its tint through here, a low alpha lets the
        # parent tint accumulate down the layer stack instead of washing out.
        if a < 1.0 and self.current_rgb is not None:
            pr, pg, pb = self.current_rgb
            r = r * a + pr * (1.0 - a)
            g = g * a + pg * (1.0 - a)
            b = b * a + pb * (1.0 - a)

        self.current_rgb = (r, g, b)
        h, s, v = self._safe_rgb_to_hsv(r, g, b)
        self.hsv = (h, s, v)
        if imgui.get_current_context() is None:
            return
        style = imgui.get_style()
        # The 35-entry table below is a pure function of the final (r, g, b)
        # and the widget brightness cap, so it's memoized per tint: the
        # editor's inline widgets push + restore a tint each (60+ widgets a
        # frame on a Toggles-like file = ~2.8 ms of hsv conversion per frame).
        from meltygui.core.runtime.toggles import Toggles
        widget_max_b = Toggles.Style.widget_max_brightness
        real_colors = style.colors
        cache_key = (round(r, 5), round(g, 5), round(b, 5), widget_max_b)
        cached = _TINT_TABLE_CACHE.get(cache_key)
        if cached is not None:
            for idx, col in cached:
                real_colors[idx] = col
            return
        # Entries are recorded as the packer's bytes / 255 (hdr_color.style_color):
        # imgui converts a style float4 to u32 by itself, and that conversion
        # must reproduce pack_color's layout (the SDR bit, the 7-bit alpha).
        colors = _TintTableRecorder()

        def make_color(input, alpha=1.0):
            value = input["value"]
            saturation_scale = input["saturation"]
            GlobalStyle.base_value = 0.3
            value = (v * GlobalStyle.base_value) + value
            alpha = input["alpha"]

            modified_rgb = colorsys.hsv_to_rgb(h, scale_saturation(s, saturation_scale), value)
            return (modified_rgb[0], modified_rgb[1], modified_rgb[2], alpha)

        # Widget fills (buttons, frame backgrounds = text edits + drag/slider
        # tracks, check.)) sit under the light COLOR_TEXT, so their
        # brightness is capped (Toggles.Style.widget_max_brightness): a bright
        # tint otherwise lifts them to the text's brightness and the widgets
        # go blank. Function-level import: toggles.py imports this module.
        from meltygui.core.runtime.toggles import Toggles
        widget_max_b = Toggles.Style.widget_max_brightness

        def make_widget_color(input):
            cr, cg, cb, ca = make_color(input)
            cr, cg, cb = cap_brightness(cr, cg, cb, widget_max_b)
            return (cr, cg, cb, ca)

        glb_cst = GlobalStyle.main_const

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
        colors[imgui.COLOR_BUTTON] = make_widget_color(glb_cst["widget"]["button"])
        colors[imgui.COLOR_BUTTON_HOVERED] = make_widget_color(glb_cst["widget"]["button_hovered"])
        colors[imgui.COLOR_BUTTON_ACTIVE] = make_widget_color(glb_cst["widget"]["button_active"])

        # Frame backgrounds
        colors[imgui.COLOR_FRAME_BACKGROUND] = make_widget_color(glb_cst["frame"]["frame_bg"])
        colors[imgui.COLOR_FRAME_BACKGROUND_HOVERED] = make_widget_color(glb_cst["frame"]["frame_hovered"])
        colors[imgui.COLOR_FRAME_BACKGROUND_ACTIVE] = make_widget_color(glb_cst["frame"]["frame_active"])

        colors[imgui.COLOR_CHECK_MARK] = make_color(glb_cst["widget"]["check_mark"])
        colors[imgui.COLOR_TEXT_SELECTED_BACKGROUND] = make_color(glb_cst["widget"]["text_selected_bg"])
        # Tabs
        colors[imgui.COLOR_TAB] = make_color(glb_cst["tab"]["tab"])
        colors[imgui.COLOR_TAB_HOVERED] = make_color(glb_cst["tab"]["tab_hovered"])
        colors[imgui.COLOR_TAB_ACTIVE] = make_color(glb_cst["tab"]["tab_active"])

        # Borders and separators
        colors[imgui.COLOR_SEPARATOR] = make_color(glb_cst["frame"]["separator"])

        # Sliders, scrollbars
        colors[imgui.COLOR_SLIDER_GRAB] = make_widget_color(glb_cst["widget"]["slider_grab"])
        colors[imgui.COLOR_SLIDER_GRAB_ACTIVE] = make_widget_color(glb_cst["widget"]["slider_grab_active"])
        colors[imgui.COLOR_SCROLLBAR_GRAB] = make_color(glb_cst["widget"]["scrollbar_grab"])
        colors[imgui.COLOR_SCROLLBAR_GRAB_HOVERED] = make_color(glb_cst["widget"]["scrollbar_grab_hovered"])
        colors[imgui.COLOR_SCROLLBAR_GRAB_ACTIVE] = make_color(glb_cst["widget"]["scrollbar_grab_active"])

        table = colors.entries
        for idx, col in table:
            real_colors[idx] = col
        if len(_TINT_TABLE_CACHE) > 512:
            _TINT_TABLE_CACHE.clear()      # bounded: a slider will muck many tints
        _TINT_TABLE_CACHE[cache_key] = table


class _TintTableRecorder:
    """Stand-in for style.colors while set_imgui_tint computes a table:
    records (index, color) so the result can be memoized and replayed."""
    __slots__ = ("entries",)

    def __init__(self):
        self.entries = []

    def __setitem__(self, idx, col):
        self.entries.append((idx, style_color(*col)))


_TINT_TABLE_CACHE = globals().get("_TINT_TABLE_CACHE", {})   # (r, g, b, cap) → [(idx, color)]
# (current tint, r, g, b, sat, alpha, factor, cap) → rgba - make_color_rgb
_MAKE_COLOR_RGB_MEMO = globals().get("_MAKE_COLOR_RGB_MEMO", {})
