from dataclasses import dataclass
from enum import Enum

from meltygui.state.core_enums import ProfileMode
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.core_decoration import defaults
from meltygui.core.rendering.window_decoration import window
from meltygui.core.styling.style_core import ImGuiStyleManager
import json
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import scale_saturation


@dataclass(frozen=True)
class Snippet:
    """One code-suggestion snippet row (see Toggles.TextEditor.AC_SNIPPETS):
    `label` is the popup row text, `insert` REPLACES the typed trigger on
    accept — put `$0` where the caret should land (defaults to the end) —
    `detail` is the dim right-hand preview (falls back to `insert`), and
    `tint` optionally colors the row like a tinted symbol (rgb or rgba)."""
    label: str
    insert: str
    detail: str = ""
    tint: tuple = None


class SwooshMode(Enum):
    """Connector style for the nested-window swoosh. Pass per window as a
    kwarg — swoosh=True, swoosh_mode=SwooshMode.RIBBON — to override the
    global default (the Swoosh.ribbon toggle) for just that window."""
    LINE = "line"
    RIBBON = "ribbon"

    
    
class Counters:
    # Nested window
    nested_window_count = 18
    some_dict = [1,1,1]
    some_dict2 = {1:1}

def rgb_to_hsv(r, g, b):
    maxc = max(r, g, b)
    minc = min(r, g, b)
    rangec = (maxc - minc)
    v = maxc
    if rangec == 0 or maxc <= 0:
        # Grey, and a colour with no positive channel: black. A tint mix can
        # undershoot zero by a hair (line_number_bg gave (0, -0.013, -0.019)).
        return 0.0, 0.0, max(v, 0.0)
    s = rangec / maxc
    rc = (maxc - r) / rangec
    gc = (maxc - g) / rangec
    bc = (maxc - b) / rangec
    if r == maxc:
        h = bc - gc
    elif g == maxc:
        h = 2.0 + rc - bc
    else:
        h = 4.0 + gc - rc
    h = (h / 6.0) % 1.0
    return h, s, v

def hsv_to_rgb(h, s, v):
    if s == 0.0:
        return v, v, v
    i = int(h * 6.0)  # XXX assume int() truncates!
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    if i == 0:
        return v, t, p
    if i == 1:
        return q, v, p
    if i == 2:
        return p, v, t
    if i == 3:
        return p, q, v
    if i == 4:
        return t, p, v
    if i == 5:
        return v, p, q

def mix(r1, g1, b1, r2, g2, b2, alpha):
    """Mix two colors with alpha blending"""
    return (
        r1 * (1 - alpha) + r2 * alpha,
        g1 * (1 - alpha) + g2 * alpha,
        b1 * (1 - alpha) + b2 * alpha
    )

@window
class Tint:
    # Saturation boosts go through hdr_color.scale_saturation: never past
    # the tint's own gamut edge (the sRGB edge for an SDR tint, its own
    # extended saturation for a P3 one), so the old 3.0 cap is gone.
    max_value = 10.0

    @staticmethod
    @defaults(tint=(1.00,0.90,0.7944444417953491))
    def icon_tint():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv
        hue_delta = -0.03
        saturation_factor = 2.0
        value_factor = 1.648

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    def checkbox_outline():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 1.2
        value_factor = -0.002
        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, -1), Tint.max_value))
        return hsv_to_rgb(*active_hsv)
        
    @staticmethod
    def checkbox_bg():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 1.1
        value_factor = 0.068

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, -1), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.0, 0.0, 0))
    def checkbox_bg_selected():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.9
        value_factor = 0.108


        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, -1), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.0, 0.0, 0))
    def checkbox_bg_hovered():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.6
        value_factor = 0.258
        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, -1), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.127, 0.989, 0.0))
    def checkbox_text_true():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 1.1
        value_factor = 2.125

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.9, 0.0, 0))
    def checkbox_text():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.8
        value_factor = 1.018

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.478, 0.053, 0.053))
    def dd_text(requested_tint=None):
        if requested_tint is None:
            style_manager: ImGuiStyleManager = Core.melty.style_manager
            active_hsv = style_manager.hsv
        else:
            active_hsv = rgb_to_hsv(*requested_tint)

        hue_delta = 0.00
        saturation_factor = 0.4
        value_factor = 2.161

        active_hsv = ((active_hsv[0] + hue_delta),
                      min(0.25, scale_saturation(active_hsv[1], saturation_factor)),
                      min(max(active_hsv[2] * value_factor, 0.85), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.15, 0.95, 0.30))
    def change_count(added=True):
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        hue = 0.36 if added else 0.0
        saturation = 0.70
        value_factor = 2.161
        brightness = min(1.0, max(0.85, style_manager.hsv[2] * value_factor))
        return hsv_to_rgb(hue, saturation, brightness)

    @staticmethod
    @defaults(tint=(0.54, 0.54, 0.54))
    def cursor_tint():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.958
        value_factor = 2.899

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.54, 0.54, 0.54))
    def cursor_line_tint():
        # The caret ROW's wash in draw_text. Same hue as the caret, but the
        # brightness is capped at Toggles.TextEditor.cursor_line_max_brightness
        # rather than Tint.max_value: the caret may go too-bright on a bright
        # theme, and the same colour behind a whole row of text at any alpha
        # drowned the glyphs (09-13).
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.958
        value_factor = 2.899

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0),
                          Toggles.TextEditor.cursor_line_max_brightness))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.45, 0.45, 0.45))
    def line_number_tint(requested_tint=None):
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = (rgb_to_hsv(*requested_tint[:3]) if requested_tint is not None
                      else style_manager.hsv)

        hue_delta = 0.00
        saturation_factor = 0.75
        value_factor = 4.10
        # Live guards - see Toggles.TextEditor.gutter_text_min/max_brightness.
        min_value = Toggles.TextEditor.gutter_text_min_brightness
        max_value = max(Toggles.TextEditor.gutter_text_max_brightness, min_value)
        


        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, min_value), max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.17, 0.2, 0.228))
    def line_number_bg():
        # The gutter follows the editor BODY's painted background, not the main
        # theme tint: the body is a depth ramp capped at
        # Toggles.TextEditor.editor_value, but a theme-relative gutter sat a
        # different distance under that for every file tint (a dark tint left
        # the strip near black). bg_color_stack[-1] is the fill the editor
        # painted for the view whose body is running (draw_text is show_bg).
        hue_delta = 0.00
        # Custom knobs - see Toggles.TextEditor.gutter_saturation, gutter_value.
        saturation_factor = Toggles.TextEditor.gutter_saturation
        value_factor = Toggles.TextEditor.gutter_value

        bg_color_stack = Core.melty.bg_color_stack
        body_bg = bg_color_stack[-1] if isinstance(bg_color_stack, list) and bg_color_stack else None
        if isinstance(body_bg, (tuple, list)) and len(body_bg) >= 3 and (len(body_bg) < 4 or body_bg[3] > 0):
            active_hsv = rgb_to_hsv(*body_bg[:3])
        else:
            # No painted body (a bare call outside a show_bg run): fall back
            # to the theme tint, as before.
            style_manager: ImGuiStyleManager = Core.melty.style_manager
            active_hsv = style_manager.hsv

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.0, 0.325, 0.972))
    def text_selection():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 1.313
        value_factor = 1.714
        max_value = 0.6

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.30, 0.34, 0.40))
    def scope_guide():
        # The text editor's indent guides for the file with no tint: the
        # window background lifted a little, so the line reads as
        # "background, slightly brighter" whatever the theme (the
        # Toggles.TextEditor.scope_guide_* knobs adjust it later).
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.85
        value_factor = 2.2
        min_value = 0.12

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, min_value), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.367, 0.112, 0.112))
    def subtle_text():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.7
        value_factor = 0.668

        active_hsv = ((active_hsv[0] + hue_delta),
                      scale_saturation(active_hsv[1], saturation_factor),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    # Context menu tints
    context_select_tint = (1.0, 0.7, 0.2)
    context_select_outline_alpha = -0.242
    context_select_bg_alpha = 0.592
    context_select_rounding = 4.988

    # Background constants
    context_menu_bg_offset = -1.0

    # Highlight outline boxes (parent + child views)
    highlight_outline_thickness = 3.0   # outline line thickness
    highlight_outline_rounding = 7.868    # corner radius of the outline boxes
    highlight_outline_alpha = 0.472      # outline opacity
    highlight_bg_alpha = 0.0           # parent fill opacity

    # Selection rect (child views)
    select_outline_thickness = 2.0       # selection outline line thickness
    select_outline_alpha = 0.65          # selection outline opacity
    select_bg_alpha = 0.08               # selection fill opacity

@window
class Swoosh:
    # Nested-window "swoosh" connector (parent outline -> nested view)
    tint = (1.0, 0.683, 0.0)   # fallback color if no style tint is available
    value = 1.023              # brightness of the highlight (super-bright tint)
    saturation = 0.791        # saturation scale applied to the current tint
    alpha = 1.0             # opacity of the swoosh
    end_thickness = 1.433      # half-width at the anchor endpoints (thick)
    cap_scale = 0.962          # end-cap dot radius as a multiple of end thickness
    mid_thickness = 0.441      # half-width at the middle (thin)
    curve = 0.029             # max arc bow as a fraction of endpoint distance
    curve_ramp = 2.00        # how the bow eases in to slope (>1 stays straighter longer)
    edge_softness = 1.138     # px smoothing window for the shared-edge anchor (0 = hard)
    segments = 31            # tessellation count (higher = smoother)
    taper = 10.0              # slope of the end->middle thickness falloff
    aa_width = 1.5           # antialiased edge-stroke width in px (0 = none)

    # Ribbon mode: replace the thin connector line with a full band bridging the
    # two views' facing edges, s-curving between them when the views are offset
    # (see Melty._draw_ribbon). Each end is sized from ITS OWN edge length, so
    # a small child on a big parent gets a funnel. Per-window override:
    # swoosh_mode=SwooshMode.RIBBON / OUTLINE. Views with no facing gap
    # (overlapping) fall back to the thin line, which knows how to route
    # around the overlap.
    ribbon = True              # global default: ribbon instead of the thin line
    ribbon_axis_bias = 0.9      # which axis the band comes from: 0.5 picks the axis
                                # with the wider facing gap (current behavior); 1.0
                                # biases fully to the left/right (x) edges,
                                # 0.0 fully to the top/bottom (y) edges. An
                                # axis with no facing gap can't be bridged, so an
                                # extreme bias falls back to whichever axis has a gap.
    ribbon_coverage = 2.13      # each end's band width as a fraction of its own edge
                                # (clamped at the full edge, so >=1 spans the edge)
    ribbon_max_width = 0     # px cap on either end's band width (0 = uncapped)
    ribbon_curve = 0.33         # s-curve tangent reach as a fraction of the gap the
                                # ribbon spans (x for left-right, y for down)
    ribbon_curve_across = 0.00  # how much of a side's CROSS-axis travel adds to that
                                # reach - the offset matters less than the gap (0 = none at all)
    ribbon_bow = -0.02            # single-sided bow: how far the band bulges through
                                # the swerve, scaled by width/length so wide short
                                # ribbons arc as one C and long thin ones keep the
                                # S (negative = bow "in" against the swerve, 0 = off)
    ribbon_bow_shape = 2.0      # bow profile exponent: <1 broad arc, >1 mid bulge


    # [tint=(0.55, 0.073, 0.073, 1.0), show_tint=True]
    ribbon_alpha = 0.36         # fill opacity of the band (below the fade area)
    ribbon_fade_size = 328.2    # px: the fill starts thinning once the band's AREA
                                # exceeds fade_size x fade_size; alpha then scales
                                # inversely with area (constant total ink, 0 = off)
    ribbon_edge_alpha = 0.05    # opacity of the band's two boundary strokes
    ribbon_edge_fade_length = 7.4  # px: a boundary stroke starts thinning once its
                                # own arc length exceeds this; alpha scales inversely
                                # with length, per side (0 = off)
    ribbon_edge_thickness = 2.7 # boundary stroke thickness in px (0 = no stroke/AA)

    # When the child overlaps the parent, slide both endpoints along their own
    # rect edge out of the intersection area to flank the reentrant corner of the
    # union, then bow the curve smoothly towards that corner so the connector hugs the
    # outside of the overlap instead of crossing either view. See
    # Melty._closest_perimeter_points.
    avoid_intersection = True   # slide the endpoints out of the overlap, avoid the corner
    intersect_hook = 24.0       # px the endpoints slide along the edge past the overlap
    intersect_soft = 50.0       # px of overlap depth over which to ease in from the plain cur
    overlap_padding = 20.0      # grow each rect by this so the transition starts before they touch
    envelop_tie = 0.04          # enveloped child: near-tie window for the round corner blend
                                #   (0 = always flat edges, hard switch; 0.5 = always blending)
    envelop_corner = 0.25       # enveloped child: only round the corner when the nearer gap is
                                # within this fraction of the parent's shorter side (else stay flat)

    # Mouse-proximity fade: scale the whole connector's opacity by how close
    # the cursor is to the views it joins, so only the swooshes near the mouse
    # stay bright and a busy screen of connectors declutters. Each END fades on
    # its OWN distance scale (parent vs child), and the connector takes whichever
    # side is brighter — so the parent end can dim sooner than the child end.
    # Distance is measured to each rect (0 when the mouse is inside it). Applies
    # to both the line and ribbon styles.
    # Drag-focus opacity (alternative to the proximity fade below): hold EVERY
    # connector at rest_alpha and light one to drag_alpha only when it is in
    # play - its child window is being dragged/resized, the parent window it
    # hangs off is, or the parent view (or the window itself) is hovered.
    # Dragging a PARENT window lights every connector hanging off it; dragging
    # a CHILD window lights only that window's own connector. False = the
    # original behavior: distance fade + hover override.
    drag_focus = True
    drag_alpha = 0.6           # connector opacity while lit (dragging / parent hovered)
    rest_alpha = 0.29          # opacity of every other connector at rest (0 = invisible)

    mouse_falloff = False               # enable the distance-based opacity fade
    mouse_falloff_dist_parent = 49.576   # px: parent-end falloff distance (lower =
                                       # the parent side dims sooner as you leave it)
    mouse_falloff_dist_child = 638.1   # px: child-end falloff distance
    mouse_falloff_floor = 0.07         # opacity multiplier when far away (0 = invisible)
    mouse_falloff_exp = 2.2            # falloff curve exponent (>1 = stay bright near
    selectable = False
                                       # the rect, then drop off; 1 = linear)


@window(tint=(0.27, 0.19, 0.14))
class Toggles:
    @defaults(tint=(0.85, 0.75, 0.05))
    class ColorPicker:
        # [tint=(0.85, 0.75, 0.05)]
        square_size = 180
        # [tint=(0.85, 0.75, 0.05)]
        tabs_height = 30
        # [tint=(0.85, 0.75, 0.05)]
        extension_width = 34
        # [tint=(0.85, 0.75, 0.05)]
        exposure_band_height = 54
        # [tint=(0.85, 0.75, 0.05)]
        anchor_gap = 10
        # [tint=(0.85, 0.75, 0.05)]
        offsets_height = 8 + 2 * 20
        # [tint=(0.85, 0.75, 0.05)]
        view_offset_rows = (("bg_offset", "Bg offset", 0.05),
                            ("z_offset", "Z offset", 0.05))
        # The view's `icon` row under the offsets (draw_view_icon_fast):
        # its height in the popover and the glyph chip's square size.
        # [tint=(0.85, 0.75, 0.05)]
        icon_row_height = 28
        # [tint=(0.85, 0.75, 0.05)]
        icon_chip_size = 20

    @defaults(tint=(0.85, 0.75, 0.05))
    class Dropdown:
        # [tint=(0.85, 0.75, 0.05)]
        menu_width = 170
        # [tint=(0.85, 0.75, 0.05)]
        row_height = 24
        # [tint=(0.85, 0.75, 0.05)]
        min_width = 300
        # [tint=(0.85, 0.75, 0.05)]
        min_height = 33
        # [tint=(0.85, 0.75, 0.05)]
        max_height = 539
        # [tint=(0.85, 0.75, 0.05)]
        code_label_max_width = 150.0
        # [tint=(0.85, 0.75, 0.05)]
        tag_color = (0.55, 0.6, 0.72, 0.85)
        # [tint=(0.85, 0.75, 0.05)]
        tag_gap = 6.761
        # [tint=(0.85, 0.75, 0.05)]
        row_tint_alpha = 0.35

    @defaults(tint=(0.811, 0.59, 0.29))
    class UsagePicker:
        # [tint=(0.811, 0.59, 0.29)]
        row_height = 24.0
        row_gap = 2.0
        min_width = 680
        min_height = 33


    # Allow window gestures to edit caller/default source code as a GUI editor.
    # Comment overrides and local window state do not require this flag.
    dangerous_edit_mode = False

    @defaults(tint=(0.103, 0.341, 0.617))
    class windows:
        # Native EGL/Wayland for app windows; GLFW on other display systems.
        # Chosen when an application's window share group is created.
        # [tint=(0.103, 0.341, 0.617)]
        native_os_windows = True
        # An OS window nothing asked to redraw skips its frame and its swap
        # (surface.Surface.wants_frame); the compositor keeps showing its last
        # buffer. Off: every window draws on every requested frame.
        # [tint=(0.103, 0.341, 0.617)]
        skip_clean_os_windows = True

    # [tint=(0.811, 0.59, 0.29)]
    dynamic_styles = False
    # [tint=(0.811, 0.59, 0.29)]
    dynamic_style_root = (0.12, 0.13, 0.15)
    # [tint=(0.811, 0.59, 0.29)]
    dynamic_text_contrast = 4.5

    @defaults(tint=(0.811, 0.59, 0.29))
    class TextEditor:

        # Completion menus stay compact; more candidates scroll.
        completion_max_height = 202

        some_list = [122,-21,311]

        some_new_dict= {
            "key": ""
        }

        enable_spell_check = False
        # Long-line token clipping (_window_tokens band): a line longer
        # than long_line_cols chars is tokenized/drawn only over the visible
        # column span (+ margin), the rest riding as O(1) 'clipped' tokens.
        # long_line_band_cols is the band's quantum/margin in columns - the
        # window_tokens only misses when the x-scroll crosses a step.
        long_line_cols = 1500
        long_line_band_cols = 512
        text_focus_stack_trace = False
        # TEMP: dump a def-tint coordinate trace in /tmp/lsd_tint_flicker.log
        # while hunting the one-frame wash misplacement on edits - logs each
        # traced line's drawn wash column vs the indent the live text claims,
        # and the mismatch frame names which side (editor vs draw map) lied.
        tint_flicker_trace = False
        # Scope-derived code folds (_scope_fold_ranges): off = editors derive
        # no per-def/class fold ranges (no chevrons, no default-collapsed
        # scopes; explicit fold_ranges from callers still work). The O(buffer)
        # scan (~22ms on a large buffer) is debounced off the keystroke path
        # (input-quiet rescan on splice - the collapse state is stored as
        # line-independent fold KEYS) but this switch skips the layer entirely.
        # Read live.
        scope_fold_ranges = True
        # Compound-statement folds (if/elif/else/for/while/try/except/
        # finally/with/match/case) alongside the def/class scopes. Read on
        # the next fold rescan (text edit), not per frame.
        block_fold_ranges = True
        # Collapsed comment runs that carry a melongui `# [...]` metadata line
        # hide their HEADER line too: the whole run drops the display and
        # only the fold chevron remains, on the gutter of the line BELOW the
        # run (the line the metadata annotates; edits splice around the
        # seam at the end of the hidden run). Off = a collapsed run keeps
        # its first comment line visible like every other line. A run at
        # the top / end of the file, or one whose metadata below heads a fold
        # of its own (the gutter has one chevron per row), keeps its header
        # either way. Read live.
        hide_meta_comment_folds = True
        # Diff fold spans (draw_text's diff_fold_ranges — the compare
        # splits' unchanged gaps) wear THIS tint on their chevrons/badges
        # and, while collapsed, a thin separator band across the row — so
        # they read apart from the grey scope folds at a glance. 4th value
        # is the badge alpha at rest (hover lifts it). Read live.
        # [tint=(0.36, 0.62, 0.85)]
        diff_fold_tint = (0.36, 0.62, 0.85, 0.55)   # rgb = fallback for a buffer with no file; alpha = chevron / label alpha
        # Diff-gap PREVIEW: a collapsed gap keeps this many of its hidden
        # lines visible (faded) on each side — `below` = under the context
        # lines that follow the change ABOVE the gap (the fold badge sits on
        # the last of them), `above` = over the context lines that lead
        # into the change BELOW the gap — so a collapsed diff still hints
        # at what the gap holds. 0 = the bare fold on that side. Built
        # into the gap spans (open_files._diff_gap_folds) — takes effect on
        # the next re-derive.
        # [tint=(0.36, 0.62, 0.85)]
        diff_preview_lines_below = 5
        # [tint=(0.36, 0.62, 0.85)]
        diff_preview_lines_above = 5
        # Glyph alpha factor on those preview rows (1.0 = not faded). Read
        # live.
        # [tint=(0.36, 0.62, 0.85)]
        diff_preview_alpha = 0.45
        # Enter inside a single-quoted string literal closes it and reopens
        # it on the next line (implicit concatenation, parenthesised when
        # not already inside parentheses) instead of leaving an unterminated
        # string. Read live.
        enter_splits_strings = True
        # Master switch for the live-view pipeline: off = the editor draws no
        # live-view/snapshot markers (and drops the gutter toggle column), and
        # opening a context menu no longer collects - the menu-open stack
        # capture skips the frame-snapshot publish and the one-shot body-locals
        # too. Captured stores persist untouched and the markers come back
        # on re-enable. Read live.
        enable_live_view = True

        # Hovering a live-view marker shows its value window as a TEMPORARY
        # preview (closes on mouse-leave); double-click still latches it
        # open permanently. Read live per marker render.
        live_hover_preview = False

        # Auto-open the value window for 3-D+ tensors (typically voxel volumes)
        # the moment an instrumented run captures them. Off = every snapshot
        # marker starts closed (click-gutter to open) - with loop
        # accumulation stacking per-layer tensors into volumes, a run would
        # otherwise pop one window per captured tensor. A site can opt back
        # in with `# [auto_open=True]`. Read live per marker render.
        live_auto_open_volumes = False

        # Paint a captured SIMPLE value over all USAGES of its symbol too
        # (e x = 301, then `.view(1, 301, ...)` further down). Display-time
        # only: usages are resolved from the text (live_usage.py) and read
        # the binding's single store entry - no extra runs and nothing
        # is captured in the instrumented function. Read live per repaint.
        live_inline_usages = True
        # First-render budget for live markers per overlay pass: a diff
        # expand / a fresh snapshot can reveal thousands of markers at once,
        # each costing a render_func call + draw_state; the rest are
        # created over the following frames (their pills show at once).
        # [tint=(0.36, 0.62, 0.66)]
        live_marker_create_budget = 300
        token_match_tint = (0.277, 0.50, 0.50, 0.22)
        # The caret row's wash (Tint.cursor_line_tint, drawn under the text
        # by draw_text): the brightest the wash colour may get (HSV value,
        # 1.0 = SDR white) and its alpha. Raise max_brightness to make the
        # row pop on dark themes; lower it if glyphs get hard to read. Read
        # live.
        # [tint=(0.277, 0.50, 0.50)]
        cursor_line_max_brightness = 0.6
        # [tint=(0.277, 0.50, 0.50)]
        cursor_line_alpha = 0.08
        # [tint=(0.55, 0.496, 0.147, 1.0), show_tint=True]
        check_syntax_errors = True

        # [tint=(0.152, 0.143, 0.628), show_tint=True]
        freeze_cst_dict = False

        # Fast-path syntax check: re-run a bare compile() over the buffer
        # INLINE on every edit and swap the red marker immediately, instead of
        # hiding it until the debounced background reparse lands (~300ms after
        # typing goes quiet). compile() is the C parser — no libcst — so a
        # typical span buffer costs well under 1ms; buffers over
        # fast_check_max_chars skip it and keep the debounced-only behavior.
        # No effect with check_syntax_errors off. Read live.
        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        fast_syntax_check = True

        # Size cap for the fast path above: buffers larger than this skip the
        # inline per-keystroke compile() + import scan and stay on the
        # debounced background pass. compile() is O(buffer) on the render
        # thread — measured ~0.07ms @ 2KB, ~1.2ms @ 18KB, ~11ms @ 128KB — so
        # this bounds the worst-case frame hit. 0 disables the fast path on
        # every edit. Over-cap buffers fall back to the changed-region path
        # below (import suggestions stay debounced-only there). Read live.
        fast_check_max_chars = 128 * 1024

        # Changed-region fast check for buffers OVER fast_check_max_chars:
        # diff old vs new text (including prefix/suffix lines), expand the edit
        # to its enclosing top-level block(s), and compile just that snippet
        # (dedent + fake-functioning - the _compile_check machinery) -
        # real-time syntax markers on very large files at O(edited block)
        # cost. Differential: the whole region must compile clean for a new
        # failure to be reported, so a region cut mid-string or mid-bracket
        # can never false-flag. Read live.
        check_changed_region = True

        # Whole-buffer static lint cap: check_source (undefined names /
        # call-signature checks) and the relint's full import rescan are
        # O(buffer) GIL-held passes (~90ms + ~40ms on a 340KB file) that the
        # relint's re-runs per queued keystroke save - a recurring render-
        # thread convoy on big files. Buffers over this cap skip check_source
        # and downgrade the full import rescan to the incremental step; the
        # fast-path syntax markers and import popovers are unaffected.
        # Trade-off over the cap: an import REMOVED elsewhere in the file can
        # keep its name wrongly suppressed until a full pass runs again.
        # Read live (worker-side).
        lint_max_chars = 64 * 1024

        # Master switch for the static lint (check_source: undefined names /
        # call-signature checks) in both the chain_in and relint passes —
        # independent of incremental_lint / lint_max_chars, for isolating
        # other pipeline features while profiling. Off = lint never runs;
        # syntax-error markers and import suggestions are unaffected.
        # [tint=(0.256, 0.189, 0.244, 1.0), show_tint=True]
        enable_lint = True
        # Call-signature checks for SPAN buffers (a function/class edited on
        # its own): bare-name calls resolve through the enclosing module's
        # PENDING text (code_checks._signature_table), so a signature edited
        # in another view flags wrong call sites before any recompile. Off =
        # span buffers report missing imports only (the old behavior).
        # [tint=(0.256, 0.189, 0.244, 1.0), show_tint=True]
        lint_span_calls = True
        # Literal-argument TYPE checks in the call lint: a LITERAL argument
        # (False, 3, "x") against a DECLARED param type (a doc C type like
        # `float position`, or a float/int/str/bool annotation) — catches
        # imgui.same_line(False). Stricter than Python's coercions on
        # purpose: numeric params reject bool literals. Expressions, names
        # and None literals are never judged.
        # [tint=(0.256, 0.189, 0.244, 1.0), show_tint=True]
        lint_literal_types = True

        # Master switch for the import-suggestions scan (the Alt+Enter
        # quick-fix channel) in the chain_in / relint passes and the editor's
        # per-keystroke fast path - for isolating pipeline features while
        # profiling. Off = no scans run and the suggestion data dries up after
        # the next pass; error markers and lint are unaffected.
        enable_import_scan = True

        # Typing debounce (ms) used by the two O(buffer) passes that key off
        # keystrokes: the chain_in cst→dict reparse (deferred until input goes
        # idle, re-queued on every key) and the symbol-usage recompute
        # quiet-gate (serves the last-good graph while input is hotter than
        # this). In the inter-key gap of fast typing a burst coalesces into
        # ONE reparse; lower = fresher structure/usages but more mid-burst
        # GIL-held parse convoying the render thread. 0 = no debounce (every
        # keystroke reparses - pathological on big buffers). First parses are
        # exempt. Read live.
        parse_debounce_ms = 32

        # Alternative debounce (ms) for SMALL buffers: under
        # small_file_max_chars a full cst→dict parse + symbol pass costs a few
        # ms, not the 150-550ms that made the big-file debounce necessary - so
        # they can run almost per keystroke without convoying the render
        # thread. Applies to the same two passes as parse_debounce_ms above.
        # 0 = no debounce at all on small buffers. Set equal to
        # parse_debounce_ms to disable the split. Read live.
        small_file_debounce_ms = 119

        # Size gate for small_file_debounce_ms: buffers up to this many chars
        # take the fast debounce, larger ones use parse_debounce_ms. 0
        # disables the small-file path entirely (everything uses
        # parse_debounce_ms). Read live.
        small_file_max_chars = 16 * 1024

        # Trailing debounce (ms) on the def-widget's TEXT → params-panel
        # sync: typing a new default in the signature updates the open panel
        # only after this quiet interval, so a half-typed value ("5" on the
        # way to "50") never lands mid-keystroke — which matters with Auto
        # Execute on, where the synced value triggers a run. Re-armed per
        # keystroke. 0 = sync immediately. Read live.
        fnrun_text_sync_debounce_ms = 400

        # Trailing debounce (ms) on Auto Execute's CODE-EDIT trigger: with
        # a def and Auto Execute on, an edit to the def's text (body, params,
        # annotations - literal defaults excluded, the panel path already
        # runs those) recompiles + instrument-runs it once typing has been
        # quiet this long. Re-armed per keystroke; the trigger reads
        # PendingSave, so expiry waits (short retries) for the deferred
        # save channel to carry the edit. 0 = run on every buffer change
        # that reaches pending. Read live.
        fnrun_auto_exec_edit_debounce_ms = 700

        # Incremental cst→dict conversion: when a previous good parse exists,
        # re-convert only the changed top-level statements and splice them
        # into the held parse + module cst (cst_dict_incremental_update) -
        # O(edited statements) instead of the 150-550ms whole-buffer parse.
        # Falls back to the full conversion on any doubt. Read live.
        incremental_cst_parse = True

        # melty_syntax: parse code with core_syntax (Python's ast + a TEXT
        # residual, like libcst) instead of libcst - ~7x faster forward, ~50x
        # on the reverse, but the round-trip is text surgery-only so unchanged
        # code comes back byte-identical. Decides the parser for NEW parses
        # (and the next edit of a held one: the parser switch forces a full
        # reparse); every chain node dispatches on the parse it is handed
        # (__origin__ = core_syntax, __cst__ = libcst), so it flips live.
        # The cst-dict cache is keyed by parser. Read live.
        melty_syntax = True

        # melty_scanner: with melty_syntax on, parse with core_syntax's own
        # tokenize-based scanner (melty_scan.scan - the "cst-lite" replacement
        # for ast.parse; off = Python's ast, the oracle the scanner is tested
        # against). It is what makes the above knob possible. Read live.
        melty_scanner = True

        # Buffers at least this many chars parse in the scan WORKER: a 3.12
        # subinterpreter with its own GIL, so the parse runs truly in parallel
        # and never stalls the render thread; smaller buffers parse in-process
        # (their parse is a few ms). 0 = always the worker. Read live.
        melty_async_min_chars = 32 * 1024


        # Fidelity gate for the merge above: regenerate the spliced module's
        # code and require it to EQUAL the new buffer (one O(file) codegen,
        # ~a sixth of full-parse cost) - any comment/whitespace attribution
        # drift falls back to the full parse instead of corrupting the
        # round-trip. Turn off once trusted for the last bit of speed.
        verify_incremental_cst = True

        # Incremental static lint: per-path re-diff - findings outside the
        # edited top-level block are kept (line-shifted), only the block
        # itself is re-linted, and the live-module + pending-binds fallbacks
        # resolving cross-buffer names (the same way span lint already
        # works). ~1ms per edit instead of the ~90ms whole-buffer pass, after
        # a one-time full pass per path - so with this ON, big buffers lint
        # again (lint_max_chars stops skipping them). Trade-off: a binding
        # added/removed OUTSIDE the edited block doesn't re-verify findings
        # elsewhere until the next full pass. Read live (worker-side).
        incremental_lint = True

        # Chain_in no-mutation skip: a newline-only edit (blank lines added
        # or removed, or a byte-identical echo) can't change the parse
        # structure or introduce a syntax error, so the full reparse -
        # 150-550ms of GIL-held libcst + dict conversion + compile that
        # convoys the render thread - is skipped inline for it. The held
        # parse and its src_good baseline stay put, so the first CONTENT edit
        # afterwards is non-safe against it and pays the one full parse it
        # always would have. Read live (on the chain_in worker).
        skip_reparse_on_blank_edits = True

        @defaults(tint=(0.22, 0.429, 0.844))
        class SymbolUsages:
            # Auto-attach symbol usages to every editor parse (background, fast
            # path only); the Index button stays as a force refresh.
            auto_index = False

            # Incremental symbol-usage refresh on live edits: reuse the prior
            # compute's expensive half (cross-file callers + defs, ~80% of cost)
            # and rescan only the changed file + new names. Off = full recompute.
            # Fast path only.
            incremental_symbol_index = True

            # Also seed the incremental path from a STALE-generation cached
            # span (cross-session pickle restore, or a gen bump because some
            # OTHER file changed) instead of cold-recomputing (~50-100ms vs
            # 0.4-2s). Cross-file callers reused from the stale seed can drift;
            # the span is marked and replaced with one full pass at the next
            # generation bump. Off = a stale-gen miss recomputes cold.
            stale_gen_incremental = True

            # Per-edit incremental patching on top of the incremental path: re-derive
            # only the top-level-statement region around the edit (patched parse
            # artifacts + a region-restricted pass) and merge into the prior
            # graph - O(edit) per keystroke instead of O(file). A final pass
            # reconciles when typing goes idle. Off = each live edit runs the
            # incremental_symbol_index pass.
            live_incremental_usages = True

            # Position-only fast path: when an edit only added/removed blank
            # lines, remap the cached line numbers by the delta (~5ms vs ~42ms)
            # instead of recomputing.
            offset_symbol_positions = True

            # Add function-local variables (params + in-function bindings) to
            # the symbol usage graph so they wash + double-click to their users
            # like any symbol. Cheap now that the editor's line<->index helpers
            # are O(log n) (see _line_offsets); turn off to drop locals from
            # the graph if ever needed.
            local_symbol_usages = True

            # Ctrl+B always consults FRESH data: run the synchronous
            # single-line usage recheck (usage_data_for_line - full
            # cross-file caller walk, on the UI thread, timed to log +
            # /tmp/uj_debug.log) on EVERY Ctrl+B press and prefer its
            # result, falling back to the background graph only when the
            # recheck resolves nothing under the caret. Off = the recheck
            # runs only as the no-targets fallback before the red flash.
            ctrl_b_always_recheck = True

            # Ctrl+B through the symbol roster FIRST (roster_tints.ctrl_b_lookup):
            # name -> definition by forward resolution of the view's chain,
            # definition -> usages via the trigram index + resolve-name
            # filter. Pending/live-buffer included, nested defs and class
            # members included, milliseconds instead of the ~1s live-object
            # recheck. The usage graph / recheck below is the fallback
            # when the roster can't resolve the caret's symbol.
            ctrl_b_roster = True

            # Ctrl+B picker: rows listed before a selectable "+ N more" row
            # (Enter / click on it shows them all). The best-match row is
            # always kept above the cut.
            picker_max_rows = 24


        # --- Code-suggestion snippets ---------------------------------
        # trigger -> snippet rows offered when the text just typed ends
        # with the trigger. The key is one trigger string or a TUPLE of
        # alias triggers; the value is a list of Snippet rows. `insert`
        # replaces the whole trigger; `$0` marks the final caret. Adding
        # a shortcut = adding one entry here (read live, hotswap-safe).
        # [expanded=False]
        AC_SNIPPETS = {
            ("#"): [
                Snippet("", "# [$0]", "# [ ... ]", tint=(0.161, 0.027, 0.047)),
            ],
            ("#[", "# ["): [
                Snippet("", "# [tint=($0), show_tint=True]", tint=(0.756, 0.283, 0.08, 1.0)),
            ],
            ("t"): [
                Snippet("black", "tint=(0.0, 0.0, 0.0, 1.0)", "", tint=(0.05, 0.05, 0.05)),
                Snippet("white", "tint=(1.0, 1.0, 1.0, 1.0)", "", tint=(1.0, 1.0, 1.0)),
                Snippet("red", "tint=(0.72, 0.11, 0.11)", "", tint=(0.72, 0.11, 0.11)),
                Snippet("green", "tint=(0.13, 0.55, 0.13)", "", tint=(0.13, 0.55, 0.13)),
                Snippet("blue", "tint=(0.071, 0.354, 0.511)", "", tint=(0.071, 0.354, 0.511)),
                Snippet("orange", "tint=(0.85, 0.45, 0.05)", "", tint=(0.85, 0.45, 0.05)),
                Snippet("yellow", "tint=(0.85, 0.75, 0.05)", "", tint=(0.85, 0.75, 0.05)),
                Snippet("purple", "tint=(0.45, 0.15, 0.60)", "", tint=(0.45, 0.15, 0.60)),
                Snippet("teal", "tint=(0.05, 0.55, 0.55)", "", tint=(0.05, 0.55, 0.55)),
                Snippet("pink", "tint=(0.90, 0.40, 0.60)", "", tint=(0.90, 0.40, 0.60)),
                Snippet("gray", "tint=(0.5, 0.5, 0.5)", "", tint=(0.5, 0.5, 0.5)),
                Snippet("cyan", "tint=(0.05, 0.70, 0.85)", "", tint=(0.05, 0.70, 0.85)),
                Snippet("magenta", "tint=(0.80, 0.10, 0.80)", "", tint=(0.80, 0.10, 0.80)),
                Snippet("brown", "tint=(0.45, 0.28, 0.12)", "", tint=(0.45, 0.28, 0.12)),
            ],

            ("white", "(1"): [
                Snippet("", "(1.0, 1.0, 1.0, 1.0)", ""),
            ],
            ("black", "(0"): [
                Snippet("", "(0.0, 0.0, 0.0, 1.0)", "", tint=(0.05, 0.05, 0.05)),
            ],
            ("blue", "(0."): [
                Snippet("", "(0.071, 0.354, 0.511)", "", tint=(0.071, 0.354, 0.511)),

            ],
            ("red", "(0."): [
                Snippet("", "(0.72, 0.11, 0.11)", "", tint=(0.72, 0.11, 0.11)),
            ],
            ("green", "(0."): [
                Snippet("", "(0.13, 0.55, 0.13)", "", tint=(0.13, 0.55, 0.13)),
            ],
            ("orange", "(0."): [
                Snippet("", "(0.85, 0.45, 0.05)", "", tint=(0.85, 0.45, 0.05)),
            ],
            ("yellow", "(0."): [
                Snippet("", "(0.85, 0.75, 0.05)", "", tint=(0.85, 0.75, 0.05)),
            ],
            ("purple", "(0."): [
                Snippet("", "(0.45, 0.15, 0.60)", "", tint=(0.45, 0.15, 0.60)),
            ],
            ("teal", "(0."): [
                Snippet("", "(0.05, 0.55, 0.55)", "", tint=(0.05, 0.55, 0.55)),
            ],
            ("pink", "(0."): [
                Snippet("", "(0.90, 0.40, 0.60)", "", tint=(0.90, 0.40, 0.60)),
            ],
            ("gray", "(0."): [
                Snippet("", "(0.5, 0.5, 0.5)", "", tint=(0.5, 0.5, 0.5)),
            ],
            ("cyan", "(0."): [
                Snippet("", "(0.05, 0.70, 0.85)", "", tint=(0.05, 0.70, 0.85)),
            ],
            ("magenta", "(0."): [
                Snippet("", "(0.80, 0.10, 0.80)", "", tint=(0.80, 0.10, 0.80)),
            ],
            ("brown", "(0."): [
                Snippet("", "(0.45, 0.28, 0.12)", "", tint=(0.45, 0.28, 0.12)),
            ],


            # --- Melty view-authoring idioms (lifted from new_core_view.py) ---
            ("@r",): [
                Snippet("render_func view",
                        "@render_func(use_cache=True, show_bg=True, with_header=draw_header)\n"
                        "def draw_$0(input_value, draw_state=None, **kwargs):\n    $1\n    return False, None",
                        "view skeleton", tint=(0.93, 0.56, 0.23)),
                Snippet("render_func default-for",
                        "@render_func(is_default_for=$0, use_cache=True, show_bg=True)",
                        "typed renderer", tint=(0.209, 0.383, 0.181)),
            ],
            ("dl",): [
                Snippet("", "dl = imgui.get_window_draw_list()", ""),
            ],
            ("rect",): [
                Snippet("", "dl.add_rect_filled(x, y, x + w, y + h, pack_color($0), "
                            "rounding=getattr(draw_state, 'corner_radius', 6))",
                        ""),
            ],
            ("pos",): [
                Snippet("", "pos = imgui.get_cursor_screen_pos()", ""),
            ],
            ("u32",): [
                Snippet("", "pack_color($0)", ""),
            ],
            ("txc",): [
                Snippet("", "imgui.text_colored($0, 1.0, 1.0, 1.0, 1.0)", ""),
            ],
            ("ga",): [
                Snippet("", "getattr(draw_state, '$0', None)", ""),
            ],
            ("in",): [
                Snippet("invalidate_up",
                        "Melty.cache.invalidate_up(draw_state._tile_id, max_depth=$0)",
                        "", tint=(0.55, 0.20, 0.15)),

                Snippet("Melty.cache.invalidate",
                        "Melty.cache.invalidate(draw_state._tile_id)",
                        "", tint=(0.55, 0.20, 0.15)),
            ],
            ("r"): [
                Snippet("", "request_render()", ""),
            ],
        }

        @staticmethod
        def usage_tint(users):
            """Background-wash color for a symbol-usage span in the editor — a
            heat ramp on `users`, the number of jump targets that occurrence
            fans out to (the rows the usage-jump dropdown would show). One
            target is a faint washed-out steel blue; it climbs to a bright deep
            orange by ~six, so a click that fans out reads hot at a glance while
            a straight jump-to-definition stays cool. The hue walks the warm
            side of the wheel (blue → violet → red → orange) rather than lerping
            straight down through green.

            Returns (r, g, b, a) floats in 0..1 (a = opacity). Edit freely to
            restyle the wash — it is read live, so changes show immediately."""
            import colorsys
            t = min(max(users, 1) - 1, 5) / 5.2
            v = max(0.13, 0.88 * t + 0.3)           
            usage_tint = (0.204, 0.224, 0.239)
            usage_tint = (*usage_tint, v)
            return usage_tint
        # Definition tints: a class/def whose definition carries a tint
        # (@defaults(tint=...), a '# [tint=...]' override comment, or a
        # class-body tint=...) gets a full-body background wash in the editor,
        # and every occurrence of that symbol - even when its definition lives
        # in another file - gets a small wash of the same color. The washes take
        # the tint's rgb with these alphas (the tint's own alpha is a view-
        # wide opacity, not meant for text washes). All read live

        # [tint=(0.0875, 0.2815, 0.477, 1.00), show_tint=True]
        definition_tints = True

        # Definition tints from the text-derived SYMBOL ROSTER
        # (core_conversion/symbol_roster.py + core_views/roster_tints.py):
        # washes come from the live buffer + pending text of every file,
        # no cst-dict parse, no background usage graph, no live objects —
        # a tinted def typed anywhere paints its references on the next
        # rebuild without saving or hotswapping. Off = the legacy
        # _collect_def_tints path (cst-dict + __symbol_usages__).
        # [tint=(0.0875, 0.2815, 0.477, 1.00), show_tint=True]
        roster_def_tints = True

        # Definition block washes of ROOT symbols (blocks no other block in
        # the buffer contains — top-level classes/defs). Embeds that paint
        # the enclosing class's background themselves (global-search rows)
        # ask draw_text to skip these via show_root_backgrounds=False; with
        # this True that request is ignored and root symbols keep their
        # tints everywhere, False honours it.
        # [tint=(0.0875, 0.2815, 0.477, 1.00), show_tint=True]
        root_symbol_tints = True

        # When the caret rests on an identifier, every OTHER place that exact
        # token appears in the visible buffer gets this background wash. A dumb,
        # identifier-bounded character match - no CST / symbol-DB metadata is
        # involved, so it works in any text, mid-edit or unparseable. Flip the
        # toggle to disable; the (r, g, b, a) tint is read live.
        highlight_token_matches = True

        # Scope guides: a thin vertical line down the indent column of every
        # indented block. A tinted def/class draws its guide in its
        # definition tint, nested blocks inherit the nearest enclosing
        # tinted block's, and outside any tinted block the file's tint
        # (else Tint.scope_guide) applies. Colours run through the
        # same hsv adjustment as the washes with the four knobs below
        # (saturation / value multipliers, then a brightness clamp).
        # [tint=(0.0875, 0.2815, 0.477, 1.00), show_tint=True]
        scope_guides = True
        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        scope_guide_alpha = 0.5
        scope_guide_thickness = 1.0
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        scope_guide_saturation = 0.8
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        scope_guide_value = 1.6
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        scope_guide_min_value = 0.16
        scope_guide_max_value = 0.32
        # The guide of the block the caret sits in (focused editor): its
        # own alpha / value / brightness cap, so it stands out from the rest.
        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        scope_guide_active_alpha = 0.9
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        scope_guide_active_value = 2.6
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        scope_guide_active_max_value = 0.55

        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        def_block_alpha = 1.0

        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        def_symbol_alpha = 1.0

        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        def_line_alpha = 0.078

        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        bg_tint_saturation = 0.67
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        bg_tint_value = 0.12
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        bg_min_brightness = 0.01
        bg_max_brightness = 0.137

        # Per-occurrence symbol-wash color adjustment - same hsv factor
        # pattern as bg_tint_*, but independent of the block/line washes so
        # the chips can run hotter or duller than the surfaces under them.
        # The shared brightness clamp (bg_min/max) still applies after.
        symbol_tint_saturation = 1.02
        symbol_tint_value = 0.25

        # Line-band color adjustment - the third independent hsv pair
        # (blocks = bg_tint_*, symbols = symbol_tint_*). Applies to the
        # hard rect AND the blurred band alike. Shared clamp applies.
        line_tint_saturation = 0.72
        line_tint_value = 0.54

        # Outline drawn around the BG washes ( class/func block rects and
        # hard line bands) - so the highlight edges read crisply against
        # the background. The outline color is the wash color BRIGHTENED by
        # def_outline_brightness (multiplied after the bg brightness clamp,
        # so it pops where the fill stays muted). 0 alpha disables.
        def_outline_alpha = 1.0
        def_outline_brightness = 1.6
        def_outline_thickness = 0.3
        # Same three knobs for the per-occurrence symbol wash outlines,
        # independent of the bg wash above. 0 alpha disables.
        def_symbol_outline_alpha = 1.0
        def_symbol_outline_brightness = 1.1
        def_symbol_outline_thickness = 0.3
        # Compositor shadows under the def-tint washes (add_shadow depth
        # marks): the signed depth offset for class/func block rects and for
        # per-occurrence symbol washes. Symbols sit above blocks so the widget
        # chip casts onto its enclosing block wash; negative values recess
        # instead; 0 disables.
        def_block_shadow_offset = 0.05
        def_symbol_shadow_offset = 3.7
        # Line-number shadows: negative = recessed below the editor surface
        # (the body casts into the gutter along its edge); 0 disables.
        gutter_shadow_offset = 0.157
        # Master switch for the gutter usage-heat buttons: the per-line
        # summed-usage boxes behind the line numbers AND their click-to-open
        # usage picker. Off also skips the per-frame heat aggregation pass
        # (gutter clicks fall through to caret placement). Read live.
        # [tint=(0.0875, 0.2815, 0.477, 1.00), show_tint=True]
        usage_heat_gutter = True
        # Compositor shadow under the gutter usage-heat boxes: each use
        # counted on the line adds this much lift, so hotter lines float
        # higher off the gutter background. The magnitude is capped at
        # usage_heat_shadow_max (sign preserved - negative recesses);
        # 0 disables.
        usage_heat_shadow_offset = 0.362
        usage_heat_shadow_max = 1.092
        # Gutter background saturation/value — the hsv multipliers
        # Tint.line_number_bg applies to the editor BODY's painted background
        # (the show_bg fill under the text, already capped by editor_value),
        # so gutter_value is the strip's brightness as a FRACTION of the
        # body's: 1.0 = same brightness as the text background, lower =
        # darker strip. Lower saturation = greyer, calmer strip. (Before
        # 09-14 both applied to the raw theme tint, value 0.325.)
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        gutter_saturation = 1.046
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        gutter_value = 0.8
        # Gutter TEXT (line numbers) hsv-value guards, applied in
        # Tint.line_number_tint AFTER its value scale: the floor keeps the
        # numbers legible on a dark theme tint, the ceiling stops a bright
        # one from pushing them to full white over the body text. Hue and
        # saturation kept. The floor wins if they cross.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        gutter_text_min_brightness = 0.45
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        gutter_text_max_brightness = 0.8

        # Code editor body background — draw_code_editor forwards these into
        # its draw_text panes as the show_bg saturation multiplier and the
        # max_bg_value brightness cap (draw_text's decorator defaults are
        # saturation=0.9 / max_bg_value=0.05).
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        editor_saturation = 0.394
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        editor_value = 0.346


        # Assignment propagation: a local defined FROM tinted symbols takes a
        # faded blend of their colors (single-symbol assignment averages the distinct
        # tints), fading a further step per hop so a value's color trail
        # weakens as it flows. Fade multiplies the wash alpha per hop -
        # LOWER = colors die out faster along assignment chains (0.55 puts
        # hop 1 at 55%, hop 2 at 30%; chains below 20% stop washing at all).
        def_tint_propagation = True
        def_propagation_fade = 0.025

        # When enabled the line tint rect above fills the whole line -
        # gutter edge to the view's right edge - instead of hugging the
        # line's text extent (indent → last non-ws column).
        def_line_full_width = False

        # Soft-edged line band: instead of a solid rect the line tint draws
        # as a feathered stack of expanding translucent rects, fading the
        # color out over def_line_blur_radius pixels past the band's edge
        # (a cheap drawcall gaussian - no blur pass). The radius also
        # bleeds vertically into neighboring lines, which is the point.
        def_line_blur = True

        # Render the line band through the GL glow pipeline (add_glow →
        # low-res light buffer → shadow composite) instead of the draw-list
        # feather stack: the band becomes a real light source - it brightens
        # neighbors and pushes back compositor shadows - and costs one small
        # quad instead of def_line_blur_samples rects of overdraw. Falls back
        # to the draw-list stack when False (or Toggles.glow is off).
        def_line_glow = True
        # Intensity of the emitted light for line bands (on top of the band
        # alpha; Toggles.glow_intensity scales all glows globally).
        def_line_glow_intensity = 0.495
        # Emit light from each PER-OCCURRENCE chip (the per-token wash
        # rects) instead of / in addition to the line band - the glow then
        # highlights the individual token background's edges. Pair with
        # def_line_glow = False to make tokens the only light source.
        def_symbol_glow = True
        # Intensity of the emitted light per token chip (on top of
        # def_symbol_alpha and any per-hop propagation scale).
        def_symbol_glow_intensity = -0.04
        # Falloff skirt radius for token-chip light, px. Deliberately its
        # own knob - token halos want a far shorter throw than the
        # line-band def_line_blur_radius.
        def_symbol_glow_radius = 65.532

        def_line_blur_radius = 126
        # Alpha multiplier for the blurred band only - feathering spreads
        # the color thin, so the blur usually wants MORE alpha than the
        # hard rect's def_line_alpha. 1.0 = same as the hard band.
        def_line_blur_alpha = 1.985
        # Falloff hardness for the blur's inverse-square profile - how
        # concentrated the "lightsource" is. Higher = tighter core with a
        # longer radial tail; 0 falls back to the default linear feather.
        def_line_blur_falloff = 3.044
        # Perceived-brightness clamp on the BLURRED band's color only -
        # applied on top of the line_tint_* adjustment (which already ran
        # through bg_min/max), so the feathered glow can hold a different
        # brightness window than the hard band. 0.0/1.0 = no extra clamp.
        def_line_blur_min_value = 0.401
        def_line_blur_max_value = 6.338
        # Layer count for the feather stack. More samples = smoother
        # gradient (fewer visible bands) at the cost of overdraw - large
        # radii need more; ~1 sample per 3-4px of radius reads smooth.
        def_line_blur_samples = 7

        # Glyphs inside a symbol wash lean this fraction toward the wash
        # color (syntax color stays the base) — the slight text tinting used
        # app-wide so text reads as part of its panel. 0 disables.
        # [tint=(0.789, 0.18, 0.332, 1.0)]
        def_text_tint_mix = 0.293

        # Brightness multiplier for glyphs on NON-tinted lines while
        # Toggles.presentation_mode is on (lines with a def-tint line wash
        # keep full brightness). 1 = no dimming; 0 = black. Read live.
        presentation_text_brightness = 0.292

        # Compensation for the inline value widgets (bool/number) inside
        # tint comments: their text renders through the widget's own hsv
        # pipeline, so reads darker than the plain comment glyphs at the
        # same dimmed tint - multiply their dimming by this so both
        # land at the same visual level. 1 = no boost. Read live.
        presentation_widget_boost = 1.953

        # Background-chip brightness for the faded comment widgets, relative
        # to their (already boosted) dimmed text color - a step brighter so
        # the widget stands out as a spot on a dim line. Read live.
        presentation_widget_bg_boost = 0.115

        # Text alpha for the faded COLORED value widgets (ones whose
        # comment carries a tint) - saturated colors read brighter than the
        # grey at equal value, so they get a transparency cut on top of the
        # brightness boost. Untinted (grey) widgets stay opaque. 1 = opaque.
        # Read live.
        presentation_widget_alpha = 0.409

        # The number widget's chip paints brighter than the bool's at the
        # same tint (depth-clamped blur + drag-frame fill stack) - extra dim
        # factor on its background in the presentation mode, applied on top
        # of presentation_widget_bg_boost (also lowers the chip's max_bg_value
        # legibility cap). 1 = same as bool. Read live.
        presentation_number_bg_dim = 4.001

        # Glyph-mix TARGET color adjustment (which color text leans toward
        # inside a wash) - same hsv factor pattern as comment_tint_* /
        # bg_tint_*, independent of the wash's own factors; the shared
        # brightness clamp (bg_min/max) still applies after. 1/1 = raw tint.
        text_tint_saturation = 1.0
        text_tint_value = 1.0

        # Tint-comment TEXT color adjustment (hsv factors, the Tint-class
        # pattern): an override comment wears its own [tint=...] color,
        # desaturated and darkened by these so it reads as commentary, not
        # code. Both read live; 1.0/1.0 = the raw tint.
        # [tint=(1.0, 0.661, 0.0, 1.0)]
        comment_tint_saturation = 0.49

        # [tint=(0.3813193440437317, 0.7055555582046509, 0.14895063638687134), show_tint=True]
        comment_tint_value = 0.160
        # Legibility floor for tinted COMMENT TEXT - independent of the
        # washes' bg_min_brightness (text needs a higher floor than a
        # background does); the brightness clamp still shares bg_max_brightness.
        comment_min_brightness = 0.170


    # [tint=(0.013, 0.583, 0.013), show_tint=True]
    class Voxels:
        # Artistic curve on the finished volume image, applied to the LINEAR
        # light the raymarch hands the fp16 scene (the presentation pass
        # does the one sRGB encode): 1.0 = untouched (colorimetrically
        # "correct"), above 1 darkens the mids against the studio's darks.
        # Read live per frame by draw_voxels.
        gamma = 1.00

        # The CUDA raymarcher's finished image reaches the display GPU's
        # texture GPU to GPU (a registered pixel buffer; from another GPU a
        # peer copy the driver stages itself). Off, or where CUDA cannot
        # reach the display GPU: a pinned host buffer and glTexSubImage2D.
        cuda_image_interop = True

        # Auto neural flow: when nf_on is OFF and a DISPLAYED axis is longer
        # than this (or than GL_MAX_3D_TEXTURE_SIZE, whichever is smaller),
        # draw_voxels wraps it itself - chops it into ~sqrt-sized chunks spaced
        # along the shortest visible axis - so a (1, 32000)) row renders
        # as a readable slab instead of a hairline (or a clamped prefix).
        # 0 disables. Read live per render.
        auto_flow_extent = 8192

        # Navigation model of the MOUSE orbit (middle-drag) - the 3D mouse has
        # its own below (SpaceMouse.navigation), so each device can keep the
        # feel that suits it. "turntable": drag x spins about the world UP
        # axis, drag y tilts, the horizon never rolls. "trackball": the drag
        # is a rotation in VIEW space about the orbit center, roll included.
        # Read live per frame.
        mouse_navigation = "turntable"

        # What the volume's FLOOR shadow darkens toward (both the GL and the
        # cuda_march paths). Neutral grey-black; the UI's own window
        # shadows keep their blue-black Toggles.shadow_color. Read live
        # per render.
        floor_shadow_color = (0.03, 0.03, 0.03)

    @defaults(tint=(0.545, 0.451, 0.248))
    class UIScale:
        # Auto-pick the UI scale each frame from the resolution of the monitor
        # the OS window sits on: 4k-and-larger panels get 1.5, everything else
        # 1.0 (detect_auto_scale in fonts.py; rechecked every ~120 frames, so
        # dragging the window to another monitor retunes soon after). False =
        # use the manual scale below. Read live.
        auto_scale = False

        # The scaling dial, used when auto_scale is off. It reaches the screen
        # exactly two ways (Melty.apply_ui_scale / Melty.begin_frame): every
        # font is RE-BAKED at scale x its authored physical size, and imgui's
        # style metrics (padding, spacing, rounding, borders) are multiplied
        # by it. Coordinates are untouched - window sizes and hand-placed
        # pixel offsets in the code do NOT scale, so this is a text-and-chrome
        # scale, not a zoom. That's on purpose: the whole-interface zoom this
        # replaced (logical display + magnify at present time) forced every
        # offscreen tile to allocate and repaint at physical resolution and
        # put a resample between tile and screen - slow, and soft at any scale
        # but 1. Sanitized through glfw_utils.clamp_ui_scale: outside
        # 0.5..3.0 (or NaN/garbage) is read as 1.0, so a typo can't bake a
        # 20x font atlas. Changing it re-rasterizes all 18 fonts - a ~0.3s
        # operation and a ~64->128MB atlas at 1.25 - so it is a setting to
        # change deliberately, not to animate. 1.0 = the authored look.
        scale = 1.00

    @defaults(tint=(0.545, 0.451, 0.248))
    class Fonts:
        # Subpixel (LCD & "ClearType"-style) text anti-aliasing. The at
        # atlas is baked 3x oversampled horizontally, and each glyph quad
        # covers 3 atlas texels per screen pixel; the imgui renderer
        # (split_overlay_renderer.py) samples those as per-channel R/G/B
        # coverage and blends them with dual-source blending - tripling the
        # horizontal resolution of lines and curves the way IntelliJ and the
        # desktop do. Off = classic grayscale AA (same atlas, one tap).
        # Turn off on a ROTATED monitor (stripes run vertically there) or
        # when text must be fringe-free. Read live per frame.
        lcd_subpixel = True

        # Subpixel stripe order of the panel. Nearly every desktop panel is
        # RGB left-to-right; a BGR panel shows orange/blue fringes on the
        # wrong sides of every glyph - flip this. Read live per frame.
        lcd_bgr = False

        # Contrast curve on glyph coverage: coverage ** (1 / text_gamma).
        # > 1 darkens the anti-aliased mid-tones so strokes read thicker
        # (Java2D's "high contrast"); 1.0 = linear coverage, the unaltered
        # rasterizer output. Applies in both LCD and grayscale modes to glyphs
        # only - never to rects / lines / images. Read live per frame.
        text_gamma = 1.0

        # Replace stb_truetype's glyph bitmaps with FreeType light-hinted
        # LCD renders at atlas bake (FontManager.hint_atlas): baselines,
        # x-heights and crossbars snap to pixel rows instead of smearing
        # over two - the other half of the LCD look, grayscale being the
        # first. Needs freetype-py; glyphs the hinter grows past their
        # atlas rect keep stb's bitmap. Read at atlas bake: must under a
        # UI-scale change to apply.
        freetype_hinting = True

        # Style variants (FontManager.styled_font: a Font at another native
        # size / weight) stay in the atlas until they're not drawn for this
        # many flushes (one flush = one surface resize, Melty.apply_ui_scale)
        # AND a bake happens anyway. Too short and windows that draw in turns
        # evict each other's fonts, so every frame re-bakes the atlas (~0.6 s
        # each, text jumping to the fallback face in between; the 09-13
        # playground ran at 1.5 fps). Only a bake can evict.
        variant_idle_flushes = 600
        # Hard cap on retained variants past the idle window: the least
        # recently drawn go first. Bounds the atlas after a long style drag.
        max_font_variants = 96

    # [icon=""]

    @defaults(tint=(0.62, 0.36, 0.52))
    class HDR:
        # HDR / wide-gamut colour (gl_gui/hdr_color.py). Colour tuples are
        # EXTENDED sRGB: 1.0 = the panel's SDR reference white, above it is
        # brighter and negatives are outside the sRGB gamut (write them with
        # white(n) / p3(r, g, b)). The GPU works in linear scRGB fp16 and
        # the presentation pass (Melty.post_frame) encodes for the output.

        # Ceiling of the HDR byte curve, in multiples of reference white on
        # P3 primaries: the brightest fully saturated colour a vertex can
        # carry (white(64) on a 250-nit desktop is 16 000 nits — headroom
        # for a dimmed desktop, not a target). Raising it spreads the same
        # 255 codes over more octaves (see vertex_octaves). Read live by the
        # packer and the shaders.
        # [tint=(0.62, 0.36, 0.52)]
        vertex_range = 64.0
        # Octaves below vertex_range the 255 codes cover: code 1 sits at
        # vertex_range / 2^vertex_octaves, code 0 is exactly zero. 12 -> ~21
        # codes per doubling (a 3.3 % step), enough for accents; long
        # smooth HDR gradients want more codes, not more octaves.
        # [tint=(0.62, 0.36, 0.52)]
        vertex_octaves = 12.0
        # Presentation encode of the linear scRGB scene into the 8-bit
        # swapchain AND the surface's colour tag (wayland_color.py):
        # "auto" — PQ whenever the compositor offers colour management (the
        # Hyprland HDR session), sRGB otherwise (GNOME); "pq" — BT.2020 +
        # ST 2084, surface tagged PQ; "srgb" — SDR, untagged: everything
        # clips to the sRGB gamut and to white. Read live.
        # [tint=(0.62, 0.36, 0.52)]
        output = "auto"
        # Under the "pq" encode a colour of 1.0 is shown at the desktop's
        # SDR reference white — read from the compositor's preferred image
        # description for our surface (wayland_color.query_preferred;
        # Hyprland: the monitor's sdr_max_luminance) and re-read whenever
        # it changes, so the studio's white tracks the desktop's SDR white
        # setting like every untagged window. Off = pin pq_reference_nits.
        # [tint=(0.62, 0.36, 0.52)]
        follow_desktop_white = True
        # Nits of reference white (a colour of 1.0) under the "pq" encode
        # when the compositor doesn't say (no luminances in its preferred
        # description) or follow_desktop_white is off.
        # [tint=(0.62, 0.36, 0.52)]
        pq_reference_nits = 250.0
        # The colour picker's Wide tab: how far above white its square
        # reaches, in stops (4 = white(16)), and the fraction of the
        # square's height that exposure band takes (the rest is the classic
        # value axis). Read live.
        # [tint=(0.62, 0.36, 0.52)]
        picker_max_stops = 4.0
        # [tint=(0.62, 0.36, 0.52)]
        picker_top_fraction = 0.3
        # Emphasis flashes (Melty.emphasize / emphasize_click, the overlay
        # pass): how far above the desktop's white the flash is lifted, in
        # stops of linear light — the outline / ripple rings at
        # 2^emphasis_stops (3 = white(8): 2000 nits on a 250-nit desktop),
        # the rect's fill at 2^emphasis_fill_stops, the bloom halo around
        # the outline at emphasis_stops too but faint. Callers pass a hue
        # tint (SDR or P3) and the lift is applied at draw time, so an edit
        # here is live. Under the "srgb" output everything clips to white
        # and the flash just reads as a bright, saturated highlight.
        # [tint=(0.62, 0.36, 0.52)]
        emphasis_stops = 3.0
        # [tint=(0.62, 0.36, 0.52)]
        emphasis_fill_stops = 1.0
        # Ceiling on TEXT brightness, in stops above the desktop's white:
        # glyph pixels of the draw-list renderer and the text-texture bake
        # are scaled down (hue kept) so no channel exceeds 2^text_max_stops
        # — an HDR colour reaching a label through a style, a tint or an
        # emphasis lift read as blinding white on the PQ desktop (09-14).
        # Backgrounds, icons drawn as rects and images keep their full
        # headroom; 0 = clamp text at reference white. Read live.
        # [tint=(0.62, 0.36, 0.52)]
        text_max_stops = 1.0

    @defaults(tint=(0.36, 0.42, 0.52))
    class Melty:
        # Every tree / expand arrow (the header arrows, the chat's carets and
        # the sidebar's folder arrows) draws at this fraction of its recipe's
        # brightness and alpha; 1 = as designed.
        # [tint=(0.62, 0.36, 0.52), show_tint=True]
        arrow_brightness = 0.5
        # Custom client-side titlebar: undecorated OS window so the UI sticks
        # to the top of the display, with min/max/close drawn to the overlay
        # drawlist top-right and drag/edge-resize handed to the WM via
        # _NET_WM_MOVERESIZE (titlebar.py). X11/XWayland only: on native
        # Wayland this is NOT consulted - with wayland_native_frame (below)
        # the min/max/close buttons always draw (GLFW's fallback frame has
        # none of its own) while the OS window KEEPS that frame (its caption
        # strip + borders are the only move/resize surface there; no GLFW
        # route to xdg_toplevel.move). Applied live per frame
        # (glfw.set_window_attrib) and read at boot for the DECORATED hint.
        enhanced_titlebar = False

        # GNOME Wayland only. GNOME offers no server-side decorations, so
        # GLFW hands its OS window to libdecor, and its GNOME plugin
        # repaints the whole title bar + frame on the fly for EVERY resize
        # configure: 38 ms a step at a 4072×2136 window, 59 ms at 7000×2000,
        # inside glfw.wait_events before render() even starts, and the
        # configures queue up while the frame runs - the 2s OS-window
        # resize. On, GLFW skips libdecor (WAYLAND_DISABLE_LIBDECOR init
        # hint) and draws its own fallback frame: a plain caption strip +
        # 4 px borders that move/resize through the compositor at <1 ms a
        # step, with no buttons - titlebar.py draws min/max/close over it. Read
        # by the FIRST glfw.init() of the day (the launcher process,
        # glfw_utils.apply_wayland_frame_hint), so a change takes effect on
        # the next model_server restart, like the cursor size.
        wayland_native_frame = True

        # With wayland_native_frame: keep GLFW's fallback frame - a 24 px
        # caption strip (drag = move) + 4 px borders (drag = resize), sizes
        # GLFW hardcodes - or drop it and run frameless: the buttons are
        # titlebar.py's, resizing is the right-drag anywhere in the window
        # (app-driven glfw.set_window_size, bottom-right corner - Wayland
        # prefers the top-left; the edge zones ask the compositor:
        # xdg_toplevel.resize) and MOVING is xdg_toplevel.move from the
        # drag strip / drag-anywhere (gl_gui/wayland_move.py - Super+drag
        # semantics, driven by the compositor). Applied live
        # (sync_decoration).
        wayland_show_frame = False

        # px height of the invisible drag strip along the top edge - a drag
        # outside inside it (a few px of travel past the press) moves the OS
        # window; a clean click there falls through to whatever view is under
        # the cursor. Double-click toggles maximize (unless
        # disable_double_click_maximize below).
        drag_strip_height = 50

        # On (default): the drag strip never subscribes to double-click, so
        # it falls through to the view under the cursor like any other
        # click. Off: a double-click on the strip toggles the OS window's
        # maximize, the WM caption rule. Applied live (each frame).
        disable_double_click_maximize = True

        # Left-drag ANYWHERE in the window moves the OS window, the meltygui
        # way — as the worst-priority drag subscriber: a view that wants the
        # drag (window headers, sliders, text selection, dnd) always wins,
        # while an unclaimed drag on bare background moves the window. Off =
        # only the strip moves. Double-click-maximize stays strip-only.
        move_drag_anywhere = True

        # Show the desktop's "move" pointer (mouse_state.MOVE) wherever a
        # left drag would move a window: a meltygui window's bare areas (its
        # resize handle) and the OS window's drag strip / drag-anywhere
        # background. Off = the normal arrow there.
        window_move_cursor = True

        # Which window controls the frameless chrome shows, and on which
        # side. "" (the default) follows the DESKTOP's own title-bar button
        # setting, what GTK / Qt header bars follow (gl_gui/titlebar_buttons
        # .py: the GNOME key `org.gnome.desktop.wm.preferences button-layout`
        # via gsettings — GNOME, Budgie, and any desktop with a dconf
        # profile, Hyprland included — Cinnamon's and MATE's keys, KDE's
        # kwinrc, xfwm4's button_layout, then the settings portal), read on
        # a background thread at boot, on focus gain and every
        # titlebar_button_refresh_s. A GNOME-syntax string pins it instead:
        # "left:right", comma-separated minimize / maximize / close —
        # "close,minimize,maximize:" puts every control on the left,
        # ":minimize,close" drops the maximize button, "" + no colon is all
        # left (mutter's rule). Applied live (each frame).
        # [tint=(0.55, 0.75, 0.35)]
        titlebar_button_layout = ""
        # Seconds between re-reads of the desktop's button setting while
        # frames render (0 = only at boot and on focus gain).
        # [tint=(0.55, 0.75, 0.35)]
        titlebar_button_refresh_s = 30
        # On Lukas's patched Hyprland (the compositor has
        # general:left_drag_move) the chrome also shows the desktop's
        # window-gesture toggle — the hyprbars `state = "left_drag_move"`
        # button: lit while the COMPOSITOR moves this app's windows on a
        # plain left drag and resizes them on a right drag, faded while the
        # app is in left_drag_move_exclude + right_drag_resize_exclude and
        # does both itself (drag-anywhere xdg move, right-drag through the
        # edge physics, nested meltygui windows' own right-drag). A click flips
        # it through desktop/left-drag-toggle --gestures (persisted by the
        # desktop's Settings). Innermost of the right group.
        # Off = never shown. (gl_gui/hypr_left_drag.py)
        # [tint=(0.55, 0.75, 0.35)]
        titlebar_move_toggle = True

        # px hit zones for edge/corner resize on the undecorated window.
        resize_border = 6
        resize_corner = 18

        # The GLFW window's four edges are collision edges in the column
        # edge system, one level above the nested meltygui windows
        # (gl_gui/os_edges.py): a meltygui window edge or a column cascade
        # pushed into the OS edge moves it (the surface grows, or the
        # window moves), the screen's work area is the wall outside it (its
        # position from the GNOME extension's feed — installation_helper
        # — glfw on X11), and an edge blocked by a wall grows its window on
        # the other side, just like a meltygui window's edge against the
        # display. Off, or with no position feed: the display edges are
        # immovable walls and the old in-display pin-and-slide remains.
        push_os_window_edges = True
        # A window MOVED by hand (left-drag) pushes the OS window's edge it
        # runs into out of its way, like any collision — but the move
        # itself is not clamped: the OS edge stops at the screen and the
        # window keeps going, so a window can be dragged partly off the
        # display on purpose (Lukas 08-27) - except above the display's
        # TOP (window_top_hard_limit).
        window_move_pushes_os_edges = True
        # A meltygui window's TOP never passes the top of the DISPLAY (the
        # work area, os_frame.display_rect): a hand move - its own or a
        # parent's it rides with - first pushes the OS edge back to the
        # screen as usual, then the remainder is clamped, so the window's
        # header always stays reachable (Lukas 08-28). The other three
        # sides stay free.
        window_top_hard_limit = True
        # Console trace of the OS edge model (foreign moves / resizes seen,
        # OS edges pushed and the surface request per frame). Off: it prints
        # per push and per frame, a real cost at 120 fps.
        push_os_window_edges_trace = False
        # Feedback-loop guard (diagnostics/edge_motion_guard): while a mouse
        # button is held, an edge moving faster or farther than the pointer
        # is logged to the resize trace with the app's geometry state and the
        # solver stacks that moved it. Cheap (one pass over the edges per
        # frame); off only to silence a known report while investigating.
        edge_motion_guard = True
        # Hyprland has no window-geometry event, so the position feed
        # (geometry_feed's hyprland backend) POLLS its request intervals
        # this often on its own thread - a `j/clients` round trip is
        # ~0.03 ms, so 120 Hz is reasonable; lower it temporarily to reduce
        # the OS edge physics' reaction time on purpose.
        hyprland_feed_poll_hz = 120

        # Tint of the OS-window chrome: the minimize / maximize / close
        # controls titlebar.py draws top-right. Each is painted exactly like
        # a window header's close button (draw_header_end's flat_button),
        # with this tint set in the style manager the way the header runs
        # under its window's tint — copy a window's tint here to match it.
        # [tint=(0.55, 0.75, 0.35)]
        melty_window_tint = (0.653, 0.758, 0.806)

        # Tint of a meltygui APP's root surface (surface.Surface.frame): the
        # ground every @glfw_window body draws on, painted through draw_bg
        # exactly as the studio's Main Window paints its desktop, so a
        # window filling the surface sits at bg depth 1 like a studio window
        # (at depth 0 over a black bg stack every root came out black,
        # 09-12). A `@glfw_window(tint=...)` overrides it per window. The
        # studio's default draw_state tint, so the two look alike.
        # [tint=(0.55, 0.75, 0.35)]
        app_root_tint = (0.11, 0.12, 0.14)

        # Rounded corners on the frameless OS window (px; 0 = square). The
        # window is created with a transparent framebuffer (boot-time -
        # restart to change 0 ↔ >0) and titlebar.composite_window_frame runs
        # last in Melty.post_frame: a fullscreen pass premultiplying the
        # frame by the surface's rounded-rect coverage (anti-aliased mask),
        # which also clips the alpha imgui's blending leaves below 1 -
        # without it the desktop would bleed through every translucent
        # draw. The radius itself applies live; 0 while maximized.
        window_corner_radius = 14

        # Client-side window shadow (GNOME draws none for Wayland clients):
        # px of transparent margin the OS surface keeps around the content.
        # imgui sees only the content (SplitOverlayRenderer insets the
        # viewport and offsets the pointer); the masks cover the whole
        # surface, and the shadow pass (ShadowPass, frame_* uniforms)
        # writes whatever shadow lands outside the content with premultiplied
        # alpha - the inner windows' own shadow casts, continued beyond the
        # content. This is the FLOOR: the ceiling grows to the shadow's reach on
        # the current surface (titlebar.shadow_reach, from the cast
        # shader's terms - ~80 px at 4072 wide, ~144 at 7680), since a
        # margin shorter than the shadow cuts it mid-fall as a hard band.
        # Collapses to 0 while maximized / fullscreen. 0 = no margin.
        # Applies live.
        window_shadow_margin = 40

        # add_shadow lift of the whole content rect over the transparent
        # surroundings (draw_melty_windows) - how pronounced the OS window's
        # shadow is. Depth is at paint rank 0: keep it deep below the
        # windows' ranks (they start at layer 64) or it would flatten the
        # inner windows' own shadow. 0 = only the root's own mark casts.
        window_shadow_lift = 20
        # add_shadow() rect marks stamped as ONE instanced draw per blend
        # equation (blit_offscreen._stamp_shadow_marks_batched) instead of
        # ~10 GL calls per mark: 2.5 ms → ~0.3 ms a frame with ~280 marks.
        # Off = the per-mark path (compare if a shadow looks different).
        # [tint=(0.95, 0.55, 0.15)]
        batch_shadow_stamps = True
        # finalize_captures rebuilds the tile / full depth masks and the glow
        # buffer (passes 4–6) only when their INPUTS changed — mask rects,
        # shadow marks, retained emitters, window order — and keeps last
        # frame's textures otherwise (a keystroke or selection drag changes
        # pixels, not geometry). Off = the old rebuild-every-frame path.
        # [tint=(0.95, 0.55, 0.15)]
        mask_rebuild_on_change = True

    @defaults(tint=(0.635, 0.728, 0.725))
    class Style:
        # Ceiling on the PERCEIVED brightness (0.299r + 0.587g + 0.114b) of
        # the imgui widget fills the style manager derives from the window
        # tint — buttons, frame backgrounds (text edits, drag/slider tracks)
        # and slider grabs — applied in ImGuiStyleManager.set_imgui_tint
        # right after the hsv transform. Those fills sit under light text,
        # so a bright background tint (v -> 1) lifted them to the text's
        # brightness and the widgets read blank; the cap SCALES the channels
        # (hue and saturation kept) instead of washing toward gray. Text,
        # check-mark and window/header colors are not capped. Read live on
        # every set_imgui_tint call. 0 disables.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        widget_max_brightness = 0.3
        # How far a tint chip's edit (draw_tuple_fast: window-header tints,
        # editor tab tints, file-row tints) cascades through the blit cache.
        # Every LIVE change from the picker invalidates the chip's host and
        # its children to tint_edit_live_depth (1 = the host + its direct
        # children — the bg the tint paints and the rows/headers sitting
        # right on it); a full invalidate_up per frame walked the whole
        # subtree and was the drag's cost. CLOSING the picker (click away,
        # Esc, re-click) runs one deep cascade to tint_edit_close_depth so
        # every nested tile under the host settles on the final colour.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tint_edit_live_depth = 1
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tint_edit_close_depth = 10

    @defaults(tint=(0.103, 0.341, 0.617))
    class WindowSettings:
        # Sticky resize: re-anchor the window top at the drag-start point each
        # frame so only the min-on-display clamp displaces it.
        sticky_drag = True

        # Breathing room, in px, left at every display edge by
        # Melty.clamp_window_pos - a bound applied whenever a window is
        # PLACED programmatically (summoned by the dock / a search hit /
        # Ctrl+Shift+F, or shown for the first time), so it can't open half
        # off-screen or with its bottom below the bottom of the display.
        # Dragging is unaffected: a window you drag off the edge just
        # stays where you put it. A window taller/wider than the display
        # keeps its top-left in view and overflows the far edge. Read live.
        edge_margin = 20

    @defaults(tint=(0.719, 0.478, 0.208))
    class Orchestrator:
        # Replay pacing multiplier over the recorded timeline: 1.0 replays in
        # real time, 2.0 twice as fast. The event stream keeps its ordering
        # either way; cues still gate progress regardless of speed. Read live.
        # [tint=(0.939, 0.453, 0.245)]
        replay_speed = 1.0
        # How many frames a replay waits at a cue for the expected undo-stack
        # change to appear before trying its correction (re-aiming the click
        # at the cue target's LIVE rect), and again before aborting. At 120fps
        # this is ~0.75s — enough for background converters to land a value.
        # [tint=(0.939, 0.453, 0.245)]
        cue_wait_frames = 90
        # Cursor moves smaller than this (px, either axis) are not recorded —
        # keeps a long hover from bloating the event list with jitter. Replay
        # interpolates nothing, so keep it small or drags get steppy.
        # [tint=(0.939, 0.453, 0.245)]
        move_sample_min_px = 1.0
        # When True a cue only matches a live change whose recorded value repr
        # equals the recorded one - strict verification. Off by default:
        # value reprs drift for coalesced drags, the change type + target
        # name is the reliable part of the anchor.
        cue_match_values = False
        # change_value's drag servo: the first nudge (px) that measures the
        # live gain, the per-step travel cap (a wild residual/gain estimate
        # can't fling the cursor across the screen), and the step budget
        # before "didn't converge".
        # [tint=(0.939, 0.453, 0.245)]
        servo_probe_px = 8.0
        # [tint=(0.939, 0.453, 0.245)]
        servo_max_step_px = 400.0
        # [tint=(0.939, 0.453, 0.245)]
        servo_max_steps = 60
        # How many times change_value re-tries the SAME collapsed/closed gate
        # (it re-closed, or the fix missed) before giving up. Distinct gates
        # on the way in are passes, not attempts — a deep nest never counts
        # against this.
        # [tint=(0.939, 0.453, 0.245)]
        gate_attempts = 100
        # Precondition fixes, ranked by ONE number — how much of the user's
        # state each disturbs: the cheapest applicable fix runs first, the
        # predicate is re-checked, the next runs only when it did not help.
        # A command may cap what it is allowed to disturb (max_disturbance).
        # [tint=(0.62, 0.55, 0.85)]
        fix_costs = {"re_pick": 0, "raise": 1, "scroll": 2, "expand": 2,
                     "move": 3, "open": 4, "anchor_window_penalty": 1}
        # [tint=(0.62, 0.55, 0.85)]
        max_disturbance = 10
        # A fix's own press point can need fixing (an obscurer's header is
        # itself covered): sub-goals nest at most this deep. Expanding a
        # covered gate is already three levels (expand → raise its window →
        # the header's own re-pick / move), so 2 gave up on the common case.
        # [tint=(0.62, 0.55, 0.85)]
        fix_depth = 4
        # The orchestrator window re-lists each visible command's unmet
        # preconditions (closed / collapsed / covered …) this often, in
        # frames — a live view of what the solver would do next.
        # [tint=(0.62, 0.55, 0.85)]
        precondition_refresh_frames = 20
        # A window raise only records as a "raise" cue when the press that
        # caused it was an EXPLICIT one: on a window's header band, or
        # outside the raised window altogether (a fast-dock row, another
        # window's button). A click inside a window's body raises it too,
        # and those press-raises were landing in takes as stray raise
        # commands (Lukas 09-01). Off = every actual restack records.
        # [tint=(0.62, 0.55, 0.85)]
        raise_cue_needs_explicit_press = True
        # A gate's control (the expand chevron) is small: a re-pick for a
        # covered gate press stays within this many px of the demonstrated
        # offset, else the cover is answered by a raise / move.
        # [tint=(0.62, 0.55, 0.85)]
        gate_hit_radius_px = 6.0
        # A synthesized header press (raise / move) lands at the MIDDLE of
        # the header strip between these two margins: the left one clears
        # the collapse chevron, the right one the close / pin buttons.
        # [tint=(0.62, 0.55, 0.85)]
        header_safe_left_px = 40.0
        # [tint=(0.62, 0.55, 0.85)]
        header_safe_right_px = 80.0
        # Height of the strip a header press may land in.
        # [tint=(0.62, 0.55, 0.85)]
        header_height_px = 24.0
        # How far past the press point a moved window is pushed.
        # [tint=(0.62, 0.55, 0.85)]
        uncover_margin_px = 12.0
        # A replayed window move verifies by geometry: the window's corner
        # must land within this many px of the recorded delta.
        # [tint=(0.62, 0.55, 0.85)]
        move_tolerance_px = 4.0
        # Wheel steps a scroll fix may send before giving up.
        # [tint=(0.62, 0.55, 0.85)]
        scroll_attempts = 40
        # change_value presses only once the target's tile reports hover
        # (its wrapper ran live, so the imgui widget is submitted — a press
        # on a blit-cached tile reaches only the window's move handle). This
        # many pumps is the cap for a view that never reports hover.
        # [tint=(0.939, 0.453, 0.245)]
        settle_hover_pumps = 30
        # After a fix reflowed the UI (expand / scroll / move): how many pumps
        # to wait for the target to be laid out again and hold still before
        # its press point is read.
        # [tint=(0.939, 0.453, 0.245)]
        layout_settle_pumps = 30
        # The virtual cursor's travel speed for synthesized moves (an
        # approach to a target, a gate click, the continuous-mouse glide):
        # WALL-CLOCK paced — a per-frame step (60 px/pump) was ~7,000 px/s
        # at 120 fps, i.e. invisible. A glide lasts distance /
        # glide_px_per_second, clamped to [glide_min_s, glide_max_s].
        # [tint=(0.939, 0.453, 0.245)]
        glide_px_per_second = 1400.0
        # [tint=(0.939, 0.453, 0.245)]
        glide_min_s = 0.18
        # [tint=(0.939, 0.453, 0.245)]
        glide_max_s = 0.9
        # Continuous mouse for EVERY injection (not just change_value's own
        # approach): a move/press far from the virtual cursor glides there
        # first (glide_px_per_second / glide_min_s / glide_max_s), so reused takes —
        # gate fragments, offset clicks, a replay resuming after a remap —
        # travel instead of snapping. Off = the raw recorded/synthesized
        # positions.
        # [tint=(0.939, 0.453, 0.245)]
        continuous_mouse = True

    @defaults(tint=(0.85, 0.22, 0.25))
    class InputRecording:
        # Record every launch to a new file in ~/.cache/meltygui/input_recordings,
        # so the session that crashed is already on disk (crash reproduction,
        # usage analytics). MELTY_RECORD_INPUT=1 does the same for one launch.
        # Applies at the next launch.
        record_on_launch = False
        # Typed characters and unmodified keys are part of a recording. Turn
        # off to keep keystrokes out of analytics recordings: shortcuts, clicks,
        # drags and scrolls are still recorded, but text fields replay empty.
        # Applies to the next recording.
        record_typed_text = True
        # Cursor moves smaller than this (px, both axes) are not recorded.
        # A replayed drag follows at most 8 waypoints, so raising it mainly
        # shrinks the file. Applies to the next recording.
        # [tint=(0.939, 0.453, 0.245)]
        move_sample_min_px = 1.0
        # Starting a recording deletes the oldest files beyond this many.
        # [tint=(0.939, 0.453, 0.245)]
        keep_recordings = 50

    @defaults(tint=(0.103, 0.341, 0.617))
    class FastDock:
        # Floor on the hsv VALUE of an OPEN (active) row's name/icon text in
        # the Fast Dock, applied AFTER the theme mix (fast_dock.draw_fast_dock
        # -> _floor_value). The row text is the window's tint pushed through
        # make_color_rgb, so a dark window tint scaled toward black and the
        # open row read no brighter than a closed one; the floor lifts just
        # the value (hue and saturation kept) so every active row stays
        # legible. The summon button is untouched. 0 disables. Read live.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        active_text_min_brightness = 0.55
        # Same floor for CLOSED (inactive) rows. Their text mix is already
        # dim by design (closed_text_value in draw_fast_dock), so a dark
        # window tint took it below reading contrast against the dock
        # background. Keep this under active_text_min_brightness or open and
        # closed rows stop reading as different states. 0 disables. Read live.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        inactive_text_min_brightness = 0.45
        # How many of the most recently first-seen windows
        # (AppModel.render_windows) the Important tab lists under its
        # "Recently added" heading, newest first. 0 hides the section.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        recently_added_count = 3

    @defaults(tint=(0.478, 0.053, 0.053))
    class CrashReports:
        # Every trace print_stack_trace prints is also written, ANSI-stripped,
        # as a text file under `directory` (glfw_utils.save_crash_report);
        # the Crash Reports window (view/playground/crash_reports.py) lists
        # them. Off = print only.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        auto_save = True
        # Where the reports go — one file per trace, named by time + error.
        # [tint=(0.35, 0.85, 0.94), show_tint=True]
        directory = "~/.lsd/crash_reports"
        # The oldest reports are deleted once more than this many exist, so
        # a crash loop can't fill the disk. 0 = keep everything.
        # [tint=(0.994, 0.872, 0.0), show_tint=True]
        max_reports = 300

    # [icon=""]
    @defaults(tint=(0.427, 0.541, 0.616))
    class ContextMenu:
        # Which tab a newly opened context menu selects, as an index into its
        # tab bar: 0 Info, 1 Config, 2 view type, 3 Eval, 4 Input, 5 Tint.
        default_tab = 0

        # Code tab: hide render-dispatch stack frames (the render_func
        # dispatch / draw_inner_main, draw_any re-dispatch - the same
        # _is_dispatch_frame filter the func-stack stacks use) so the trace
        # reads caller → caller → view function.
        code_tab_hide_dispatch = True

        # ── Info tab source dropdown styling ────────────────────────────────
        # Row wash + trigger tint marking the source actively driving a param.
        active_source_tint = (0.9, 0.8, 0.2)
        # Row LABEL text pulled toward the menu background (0 = the stock
        # near/black dd text, 1 = invisible) so the active-source yellow
        # stands out against quiet rows.
        source_text_toward_bg = 0.55

    @defaults(tint=(0.378, 0.286, 0.201))
    class Collection:
        pre_load_items = 26
        placeholder_height = 30.0
        drop_tail_height = 8
        # Drag-and-drop chrome (slot lines, the home icon) starts INVISIBLE
        # at pickup and eases up to full opacity once the cursor has
        # travelled this many px (cumulative path length, never fading back
        # on a return trip) - nothing pops in on a short drag. 0 = instant.
        dnd_reveal_distance = 10.0

        max_preferred_header_width = 70
        preferred_header_width = 132
        # draw_any renders collections through fast_draw_collection (no
        # @render_func wrapper per nested collection). Off = every collection
        # goes back through the draw_collection wrapper, for comparing the
        # two paths' look and frame time.
        fast_draw_collection = False

    # [icon=""]
    @defaults(tint=(0.65, 0.385, 0.069, 1.0))
    class SearchSettings:
        # Auto-scroll to the current match while the search term is being
        # typed. When False, typing only recounts/highlights in place; the
        # view scrolls to the current match only on explicit navigation
        # (Enter / Shift+Enter / the find bar's arrows).
        scroll_while_typing = True

        # Search matches are highlighted with a circular gradient "glow" that
        # radiates out from the matched rectangle (rounded-rect cutout), with the
        # rect itself cut out so the matched text stays readable. The CURRENT
        # match uses ActiveElement; every other match uses InactiveElements, so
        # the two can be tuned (color/falloff/alpha/...) independently. Glows
        # combine where they overlap. Applies in both the text editor (draw_text)
        # and collections (draw_collection rows). See view/core_views/search_glow.py.

        @defaults(tint=(0.406, 0.30, 0.16))
        class ActiveElement:
            gradient_color = (0.86, 0.67, 0.23)   # RGB of the halo
            outline_color = (0.88, 0.56, 0.15)   # RGB of the optional cutout outline
            falloff = 26.696          # px the glow radiates out past the match edge
            opacity = 0.176           # peak opacity, % at the cutout edge
            falloff_exp = 2.105       # >1 = bright inside the word, then drops off fast
            inner_pad = 0.00         # px the cutout is grown beyond the match rect
            cutout_radius = 5.0       # corner radius of the rounded cutout
            rings = 86                # radial tessellation steps (higher = smoother)
            corner_segments = 15       # arc subdivisions in each rounded cutout corner
            outline_alpha = 1.00       # 0 = rely on the glow's bright inner halo alone
            outline_thickness = 1.626

        @defaults(tint=(0.36, 0.52, 0.93, 0.484))
        class InactiveElements:
            gradient_color = (0.73, 0.84, 0.91)  # cooler hue so the active match stands out
            outline_color = (0.6, 0.72, 1.0)
            falloff = 15.664
            opacity = 0.077
            falloff_exp = 2.491
            inner_pad = 0.00
            cutout_radius = 5.00
            rings = 16
            corner_segments = 5
            outline_alpha = 1.00
            outline_thickness = 1.366

    @defaults(tint=(0.58, 0.47, 0.24))
    class GlobalSearch:
        # Category order for the search selector and the All tab's interleave:
        # All shows each category's #1 hit first (in this order), then each
        # category's next-best hits up to all_tab_per_category per category.
        # Kinds not matching here fall in after these.
        search_priority = ("Toggles", "Actions", "Windows", "Code", "Text")
        # Hits each category contributes to the All tab (the #1 lands in the
        # top block; the rest sit under the category's own label).
        all_tab_per_category = 13
        # Files the Code tab lists at first - the rest fold into a trailing
        # "+ n more files" row that reveals this many more per pick. The
        # per-file row cap (FILE_ROW_CAP; "+ n more" inside a file) is
        # separate. 0 = list every file.
        code_files_per_page = 4
        # Lay the All tab out horizontally: one column per category (its
        # first on top, hits below), instead of the vertical Top-block
        # interleave.
        all_tab_horizontal = False
        # Per-category cap for the horizontal layout's columns (replaces
        # all_tab_per_category there - columns have the vertical room).
        all_tab_horizontal_per_category = 15
        # Where the window may reappear on Ctrl+Shift+S: its top edge is
        # pushed down to at least this fraction of the display height. The
        # window remembers its last spot otherwise - 0.5 = never above the
        # middle of the screen, 0.0 = reopen it where it was closed.
        summon_min_top_fraction = 0.5
        # Seconds a keystroke must sit unchanged before a search pass runs
        # (the exact pass and the full-text trigram pass share this; tab
        # switches and load-all skip this - no text changed). Typing pays
        # nothing on the render thread: the passes run on a background
        # worker (_kick_search) and repaint when their hits arrive.
        input_debounce_s = 0.15
        # Seconds the query must sit unchanged before the typo-tolerant
        # (fuzzy) Code pass runs (measured from the keystroke, so the worker
        # sleeps the remainder of input_debounce_s). Fuzzy hits arrive as
        # a trailing section, never moving the hits already shown.
        fuzzy_debounce_s = 0.2

    @defaults(tint=(0.47, 0.463, 0.417))
    class ScrollSettings:
        scroll_speed = 214
        max_increment_fraction = 0.169
        acceleration_threshold = 0.036  # ms
        bg_offset = 30
        debug_scroll = False
        # Compositor shadow under the scrollbar grab (add_shadow depth offset,
        # signed px from the view's surface; 0 disables). The grab gets
        # its own plane in the depth map, so it reads the same whether
        # it is drawn in the view list - lit by the composite, so it
        # otherwise inherits the view edge's specular rim and any
        # neighbour's cast shadow - or on the overlay list mid freeze-drag,
        # which renders after the composite.
        scrollbar_shadow_offset = 1.0

    # [icon=""]
    @defaults(tint=(0.315, 0.489, 0.322))
    class LoadSave:
        # When True, save() ALSO writes the raw custom.ini (root_new eval blob)
        # as a backout alongside the new-pickle custom.pkl. Set False to go
        # pickle-only (skip the .ini dual-write). NOTE: model_server still treats
        # custom.ini as the main host identity / hot-reload cache anchor, so kee
        # this off until the .ini is fully deprecated.
        ini_save = False


    @defaults(tint=(0.04, 0.05, 0.07))
    class CodeEditor:
        # Record navigation (file tab switches, jump-to, split open/close)
        # onto NavUndo's own stack - separate from the Ctrl+Z edit history.
        # Step back/forward with Ctrl+Shift+Left/Right or the Fast Dock's
        # arrow buttons.
        undo_navigation = True
        # Also record the text caret (NavUndo.poll_caret): moves inside a
        # draw_text (arrow keys, clicks) and tab focus hopping between
        # draw_texts. Consecutive moves in one view fold into a single step
        # while they come within nav_caret_coalesce_s seconds of each other
        # AND stay within nav_caret_step_lines lines of where the step
        # already ended (an arrow-key walk = one step; a far click = a new
        # one). Typing never records (the edit stack does that caret), and
        # jumps / tab switches keep recording as locations. Needs
        # undo_navigation.
        undo_navigation_caret = True
        nav_caret_coalesce_s = 0.6
        nav_caret_step_lines = 10

        # ── Text undo steps (UndoManager.record, IntelliJ-style) ──
        # Typing folds into one Ctrl+Z step while it stays in one place ( one
        # editor, keystrokes landing on the selection's caret edge, same
        # insert/delete direction) and, with undo_word_steps, until a new
        # word starts with a non-space typed character after whitespace: "hello
        # world" undoes as "world" then "hello"; a Backspace run mirrors this
        # (" world" then "hello"). No pause ever commits a step, and nothing
        # folds across an undo/redo. An edit that is not keystroke-sized -
        # inserts or removes a line (Enter + auto-indent), more than
        # undo_typing_max_chars characters at once (paste, tab, a completion,
        # a comment toggle) or replaces a selection - is always its own step.
        # Raise undo_typing_max_chars if fast typing on a slow frame splits
        # words too often (a frame's keystrokes arrive as one edit).
        undo_word_steps = True
        undo_typing_max_chars = 3

        # ── Compare-split ribbons (open_files._draw_compare_ribbons) ──
        # Block colors by kind. Read live per frame.
        ribbon_insert_tint = (0.315, 0.928, 0.294)   # lines only in the buffer
        ribbon_delete_tint = (0.737, 0.76, 0.767)   # lines only in the reference
        ribbon_replace_tint = (0.294, 0.675, 0.928) # changed in place
        # Merge mode (merge_files) colors: a CONFLICT region - a pending edit
        # and an external edit touch the same original row and disagree.
        # Red on purpose: the eye must land here first.
        ribbon_conflict_tint = (0.93, 0.25, 0.25)
        # A conflict region whose pending side already equals the external
        # side (taken with the arrow, or edited to match) - no longer red.
        ribbon_resolved_tint = (0.55, 0.85, 0.55)
        # A DECLINED side's band (its ✗ marker set): grey, and the 4th
        # component scales the band's fill/edge alpha down - a rejected
        # change should recede, not glow.
        ribbon_declined_tint = (0.55, 0.58, 0.62, 0.45)
        # Shared fill alpha for the block washes AND the seam band - same fill
        # so highlight → band → highlight reads as ONE continuous shape.
        ribbon_fill_alpha = 0.10
        # Boundary stroke around the whole shape (wash edges + S-curves).
        ribbon_edge_alpha = 0.00
        ribbon_edge_thickness = -0.35
        # Thin insertion line where a side has no rows (pure insert/delete).
        ribbon_insertion_alpha = 0.242
        ribbon_insertion_thickness = 3
        # Seam curve sampling (smoothstep slices).
        ribbon_curve_steps = 40
        # AA feather for the seam band's S-curve edges: the band fills with
        # aliased triangles (per-triangle AA reads as seams), so its two
        # boundary curves are stroked with an antialiased polyline in the
        # FILL_COLOR at this thickness - same trick as Swoosh.aa_width in
        # Melty._draw_ribbon. 0 disables.
        ribbon_aa_width = 1.0
        # Signed depth offset for the shadow cast behind the whole swoosh
        # (washes + seam band, add_shadow semantics: positive lifts it off
        # the editor surface, negative carves a hole). 0 disables.
        ribbon_shadow_offset = 0.397
        # Card shadow under each of the Merge Files column's five cells
        # (files spread over their whole band, panes below their chevron
        # row - add_shadow semantics: positive lifts the card off the
        # column, negative sinks it). 0 disables.
        merge_column_shadow_offset = 0.5
        # Take-arrow chips riding the swooshes (pull a block from the
        # reference pane into the buffer): flat_buttons colored by the
        # block's ribbon tint - hover boost and text color come from
        # flat_button's own pipeline.
        take_arrow_size = 20.6
        take_arrow_alpha = 1.0
        # ── Editor tab bar (open_files.draw_code_editor) ──
        # Styling knobs for the file tabs, read live per frame. The ACTIVE
        # tab draws a tinted bg + text; INACTIVE tabs draw a MUTED bg
        # (the tab_inactive_bg_* knobs below) plus their own text knobs.
        #
        # The bg pair feeds flat_button's theme-mix pipeline (value /
        # saturation_scale of make_color_rgb).
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_active_bg_brightness = 0.51
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        tab_active_bg_saturation = 1.184
        # Hard cap the active-tab bg is clamped to AFTER the hsv transform —
        # raise it along with tab_active_bg_brightness or the brightness
        # knob tops out here.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tab_active_bg_max_brightness = 0.31
        # Inactive-tab bg: the same theme-mix pipeline, muted — lower
        # brightness / saturation and a cap well under the active tab's so
        # the selected tab still reads first. Alpha 0 = no bg at all.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_inactive_bg_brightness = 0.30
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        tab_inactive_bg_saturation = 0.45
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tab_inactive_bg_max_brightness = 0.08
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tab_inactive_bg_alpha = 0.6
        # Shadow lift (add_shadow offset) of the tab bgs — keep the inactive
        # one under the active so the selected tab pops out.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tab_active_shadow_offset = 2.0
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tab_inactive_shadow_offset = 0.21
        # Closing a tab HOLDS the bar's layout (Safari-style: the next tab
        # slides into the closed slot at the closed tab's width) until the
        # mouse travels this many pixels, so repeated close × / middle
        # clicks on one spot close tab after tab. 0 = no hold.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        tab_close_hold_move_px = 8.0
        # The text pairs are FULL-RANGE hsv multipliers applied directly to
        # each tab's tint (open_files._tab_text_color → flat_button
        # text_color): brightness scales hsv value (0 = black, 1 = the
        # tint's own value, higher pushes toward full-bright), saturation
        # scales hsv saturation (0 = greyscale, 1 = the tint's own).
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_active_text_brightness = 1.111
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        tab_active_text_saturation = 0.420
        # Floor on the ACTIVE tab label's hsv value AFTER the brightness
        # scale (same floor as the inactive one below) — a dark file tint
        # otherwise scales the selected tab's text toward black against its
        # bright bg.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_active_text_min_brightness = 0.83
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_inactive_text_brightness = 0.390
        # [tint=(0.923, 0.989, 0.0), show_tint=True]
        tab_inactive_text_saturation = 0.522
        # Floor on the inactive-tab label's hsv value AFTER the brightness
        # scale — dark tints stay legible instead of scaling toward black.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_inactive_text_min_brightness = 0.387
        # Diff gap folds (open_files._diff_gap_folds): unchanged
        # context lines kept visible on each side of a change block; the
        # rest of the gap folds away, so collapse-all skims the changes
        # without scrolling.
        diff_fold_context = 2
        # Debug trace of the compare split's resolve, printed on every
        # CHANGE of its state (file, reference, loading, served/warm,
        # blocks, diff folds, switch, per-pane cursor offset + display
        # lines) - for pinning down a boot that shows the wrong fold
        # state. Read live; off = silent.
        trace_compare_boot = False
        # Tab tint for a file whose FileMeta carries none — the tab bar, the
        # compare files column, and the editor toolbar buttons all key off it.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        tab_tint_fallback = (0.485, 0.61, 0.76)
        # Editor toolbar back/forward buttons (open_files._draw_editor_toolbar):
        # hsv SCALES applied straight to the target file's tint (1.0 = the
        # tint as is) before it reaches flat_button — its own saturation /
        # tint_value knobs only shape the theme colour mixed in at `factor`,
        # which left these buttons unmoved. The disabled side is theme grey
        # and carries no file tint, so nothing here touches it.
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        nav_button_saturation = 2.5
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        nav_button_value = 0.34
        # Hard cap the button bg is clamped to AFTER the scale — raise it
        # along with nav_button_value or the value knob tops out here.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        nav_button_max_brightness = 0.25
        # The arrow glyph on an enabled button: hsv scales of the target
        # file's UNSCALED tint (not the bg above — dimming the bg leaves the
        # glyph alone), handed to flat_button as text_color the way the tab
        # labels are. brightness scales value (1.0 = the tint's own, higher
        # pushes toward full-bright), saturation scales hsv saturation.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        nav_button_text_brightness = 1.0
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        nav_button_text_saturation = 0.8
        # Floor on the glyph's hsv value AFTER the brightness scale — a dark
        # file tint stays legible instead of scaling toward black.
        # [tint=(0.13, 0.55, 0.13), show_tint=True]
        nav_button_text_min_brightness = 0.6
        # ColumnLayout padding the compare column renders with (cell content
        # is inset by this from its dividers on both sides). The layout
        # reframe math (open_files._cmp_layout_reframe) keys on the SAME
        # value - change them together by changing only this.
        compare_padding = 14.0
        # Compact file-column rows; shared by layout, paint and keyboard scroll.
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_row_height = 20.0
        # Nearest baked JetBrains Mono size, in pixels before UI scaling.
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_font_size = 16
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_file_max_width = 650.0
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_file_bg_value = 0.020
        # Unselected file labels in the files column (draw_changed_file_header):
        # the same hsv scale / floor as the tab_inactive_text_* pair, but
        # brighter — the column's unpainted rows draw no bg to read against.
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_file_text_brightness = 0.75
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_file_text_min_brightness = 0.66
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        compare_row_gap = 1.0

    @defaults(tint=(0.72, 0.35, 0.3))
    class FileSafety:
        # Kill switch for folder_io's reconcile deletes: while True, a key
        # removed from a held folder tree never unlinks/rmtrees on disk (the
        # poller re-discovers the file and the key comes back). Flip off when
        # the file machinery has earned trust.
        block_file_delete = True

    @defaults(tint=(0.103, 0.561, 0.145))
    class HostLifecycle:
        # Deregister a RenderHost from Melty.render_hosts (stops its background
        # draw/parse) once none of its consumer windows are active. Host + parse
        # stay cached; reopening re-registers (notify_on_change → register).
        deregister_idle = True
        # A consumer is gone when its window is abs_closed, or it hasn't
        # re-registered within this many frames (safety net for closes abs_closed
        # misses). Also the birth grace before a new host can be swept.
        idle_frames = 120
        # Trailing debounce (ms) on consumer-notify invalidations while the
        # host's value is being actively edited (typing). Each second editor
        # window over the same file is a consumer of the shared code_host -
        # without this it re-renders (and re-runs its background chain) on
        # every keystroke's finished reconvert. The notify is deferred and
        # re-armed per edit; it fires once, this long after the last local
        # edit. Notifies with no recent local edit (external file reload,
        # initial load) pass through live. 0 disables.
        consumer_notify_debounce_ms = 2000
        # Skip ALL RenderHost draws (the draw_main host loop) for this long
        # after each keypress while a draw_text editor is focused - the same
        # deferral the loop already applies during click/drag/scroll. Host
        # draws and deferrable background work (reconverts, saves) that
        # otherwise leak into typing frames; they catch up on a self-armed
        # wake once the window expires. 0 disables.
        host_typing_debounce_ms = 25

    @defaults(tint=(0.181, 0.119, 0.294))
    class InputHandlerToggles:
        show_debug = False

    # 3D mouse (3Dconnexion SpaceMouse). The device side is read by
    # events/space_mouse.py - the spacenavd socket client and the per-frame
    # pump that feeds the InputHandler a "space_mouse" axes event, each axis
    # the deflection integrated over the frame in full-deflection-seconds -
    # and the navigation knobs below are how draw_voxels turns those axes
    # into tilt / spin / roll / cam_zoom / pan (voxel_camera.apply_space_mouse),
    # so every sensitivity reads "per second at full push". All read live.
    @defaults(tint=(0.181, 0.119, 0.294))
    class SpaceMouse:
        # ── navigation (draw_voxels) ──

        # Rotation model, independent of the mouse's
        # (Toggles.Voxels.mouse_navigation). "trackball": the puck's full
        # rotation vector rotates the view freely about the orbit center in
        # VIEW space, roll included (Blender's "Free" default) - the
        # camera's roll param carries the third degree of freedom.
        # "turntable": the puck's yaw (ry) spins about the world UP, and
        # its pitch (rx) tilts; the horizon never rolls, roll (rz) is ignored.
        navigation = "trackball"

        # What a rotation turns about. "camera": the eye stays where it is
        # and the VIEW turns - looking around, the scene sweeps across the
        # screen (the orbit center rides along the new view direction).
        # "target": the camera orbits the volume's orbit target like the
        # mouse does - the volume turns in place on screen.
        pivot = "camera"

        # Orbit sensitivity: radians per second at full deflection of a
        # rotation axis. Higher = faster orbit.
        # [tint=(0.181, 0.119, 0.294)]
        orbit_sensitivity = 1.50

        # Translation sensitivity. pivot "camera": WORLD units per second
        # at full deflection on all three axes (right / up / forward — a
        # rigid flight, the volume is ~2 units across; nothing scales with
        # the camera distance). pivot "target": camera distances (cam_zoom)
        # per second in the screen plane, the mouse pan's rule. Higher =
        # faster.
        # [tint=(0.181, 0.119, 0.294)]
        pan_sensitivity = 1.0

        # Zoom sensitivity, pivot "target" only: e-folds of cam_zoom per
        # second at full push / pull (tz). With pivot "camera" push / pull
        # is flight along the view at pan_sensitivity. Higher = faster.
        # [tint=(0.181, 0.119, 0.294)]
        zoom_sensitivity = 1.0

        # Per-axis sensitivity, one float per degree of freedom, applied to
        # the normalized reading in space_mouse.normalize (so every view
        # sees it, on top of the orbit / pan / zoom sensitivities above).
        # 1.0 = as the device reports; 0 disables that axis. Translation:
        # tx right, ty up, tz push / pull. Rotation: rx pitch, ry yaw,
        # rz roll.
        # [tint=(0.181, 0.119, 0.294)]
        tx_sensitivity = 1.0
        # [tint=(0.181, 0.119, 0.294)]
        ty_sensitivity = 1.0
        # [tint=(0.181, 0.119, 0.294)]
        tz_sensitivity = 1.0
        # [tint=(0.181, 0.119, 0.294)]
        rx_sensitivity = 1.0
        # [tint=(0.181, 0.119, 0.294)]
        ry_sensitivity = 2.239
        # [tint=(0.181, 0.119, 0.294)]
        rz_sensitivity = 0.875

        # Environment lighting the puck in draw_space_mouse: an HDR photo
        # from pbr.HDRIS ("studio", resources/hdri) or a cube map from
        # pbr.ENVIRONMENTS ("room", "outdoor"). Read live.
        environment = "studio"

        # A real model of the device (OBJ / STL / GLB, absolute or relative
        # to the src tree) drawn instead of the procedural one. Parts whose
        # name contains "cap", "knob" or "puck" ride the reading (the
        # the cap); everything else is the base. Empty = procedural.
        # Model UP axis: "y" or "z" (CAD exports are usually Z-up).
        model_path = ""
        model_up = "z"

        # How much of the WINDOW's tint colours the environment light on the
        # puck (0 = white light, 1 = fully the tint's hue). Read live.
        # [tint=(0.181, 0.119, 0.294)]
        environment_tint_strength = 0.6

        # ── device (events/space_mouse.py) ──

        # Master switch: off = the reader parks (socket closed) and no view
        # receives space_mouse events. Read live.
        enabled = True

        # spacenavd's listen socket (protocol 0 stream). Debian/Ubuntu's
        # package installs here; /var/run/spnav.sock is the same path.
        socket_path = "/run/spnav.sock"

        # The units spacenavd reports at full push / twist. 3Dconnexion
        # devices saturate at ±350 through spacenavd's default sensitivity;
        # raise your /etc/spnavrc sensitivity and this stays the ceiling
        # (readings clamp to ±1.0).
        full_deflection = 350

        # Dead zone as a percentage of full deflection: readings below it are
        # zero (a resting cap never drifts the camera), the live range is
        # re-scaled so motion starts smoothly at the edge. spacenavd has its
        # own dead zone (spnavrc), this one stacks on top.
        dead_zone = 0.03

        # Axis convention. The reader assumes spacenavd's graphics-style
        # frame: tx right, ty UP, tz toward the viewer (pull = +), rx pitch
        # about right, ry yaw about up, rz roll about the view axis. If the
        # daemon is configured for the device's original Z-up frame, flip
        # this so y / z (and ry / rz) swap into the frame above.
        swap_yz = False

        # Flip the sign of individual axes: any of "tx", "ty", "tz", "rx",
        # "ry", "rz". The defaults match Blender (space_mouse.BLENDER_SIGNS,
        # measured); this is for a view that wants it otherwise. Applied
        # after swap_yz.
        invert_axes = ()

        # A deflection older than this (no motion frame since) reads as
        # zero - spacenavd dying mid-push or a lost release frame must never
        # keep the camera moving. Devices report at 60–100 Hz while held.
        stale_s = 0.25

        # Cap on the frame interval the pump integrates over: a stalled
        # frame (a load, a hotswap) hands the view at most this much motion
        # instead of a jump.
        max_frame_dt = 0.10

        # Seconds between connection attempts while spacenavd is away.
        retry_s = 3.0

    @defaults(tint=(0.652, 0.672, 0.733))
    class TerminalSettings:
        # Minimum logical terminal size, in pixels - independent of the window size.
        min_height = 605.7
        min_width = 94.154

    @defaults(tint=(0.239, 0.435, 0.408))
    class Thermostat:
        # The thermostat web server (Desktop/thermostat/server.py). The proxy in
        # playground/thermostat_data.py pulls the chart histories from HERE,
        # not the raw jsonl files.
        server_url = "http://127.0.0.1:8765"
        # The charts' window query: hours back from now, or "all" for the whole
        # log (the server stride-samples each answer to ~2000 points either way).
        history_hours = "all"
        # How often the poller re-pulls the data (seconds).
        poll_s = 60.0

    @defaults(tint=(0.478, 0.265, 0.265))
    class InvalidateTracker:
        keep_for_frames = 100
        enable = False
        draw_rect = True
        # A faint fill over every cached (use_cache) view that rendered
        # live, coloured by its own render time on the `hot` LUT (black →
        # red → yellow → white at render_time_max_ms and above), fading out
        # over keep_for_frames: which views cost the frame, at a glance.
        draw_render_time = True
        render_time_max_ms = 8.0
        # [tint=(0.85, 0.45, 0.05), show_tint=True]
        invalidate_stack_trace = False
        # [tint=(0.028, 0.561, 0.115), show_tint=True]
        invalidate_request_render = False
        attrib_change_stack_trace = False
        # [tint=(0.0, 0.56, 0.872), show_tint=True]
        draw_bvh = False


    @defaults(tint=(0.16, 0.132, 0.194))
    class Fim:
        # Fill-in-the-middle code completion (ghost text) in code editors -
        # fim.py. Providers register with @fim_provider; `profile` names a
        # provider or a fim_profile() variant (fim_providers/profiles.py);
        # an editor can override it with draw_text(fim="...").
        enabled = True
        profile = "ollama"

        # Idle time after the last keystroke before a request is sent.
        debounce_s = 0.25

        # Only generate when the caret is at the end of its line (nothing but
        # whitespace after it) - never mid-line. Off = complete anywhere.
        only_at_line_end = True

        # Ghost text is shown one CHUNK at a time: this many newline-
        # separated lines (a partial rest-of-line counts as one). Tab
        # accepts the chunk and the next one appears instantly from the
        # buffered completion - small chunks add more steering options.
        chunk_lines = 1

        # Length of ONE provider request (tokens). The buffer refills with
        # a continuation request when it runs low, so this is the fetch
        # granularity, not a cap on how far repeated Tabs can go.
        max_tokens = 256

        # Fetch the continuation while the current chunk is still showing
        # so the next Tab never waits.
        prefetch = True

        # Token budget for the context block (definitions, enclosing code,
        # enclosing types, last-run values). Split ~60/25/15 across the
        # stable / run / volatile tiers.
        context_tokens = 4000



        # A definition longer than this is truncated (its signature line
        # is kept as the budget-degrade form).
        definition_max_lines = 80

        # Lines above and below the caret the per-request scans look at
        # (referenced definitions, runtime values). Bounds context assembly
        # on large-file buffers - a 14k-line span is not scanned end to end.
        scan_lines = 120

        # Re-derive the stable context after this many seconds even when
        # its key (file, pending gen, enclosing def) hasn't moved.
        stable_refresh_s = 2.0

        # Most last-run values to annotate (nearest the caret first).
        live_values_max = 40
        # Also report min/mean/max for tensors - a GPU reduce per value.
        live_value_stats = False

        # Close a provider session (language client / Copilot LS process) that
        # no editor has used for this long.
        session_idle_s = 600.0

        # How long Ollama keeps a model resident after a request / a Load
        # from Internet Accounts (Ollama duration string; 0 = disable).
        ollama_keep_alive = "30m"

        # Print provider/context tracebacks.
        debug_print = False


    @defaults(tint=(0.85, 0.55, 0.35))
    class InternetAccounts:
        # Codex app-server CLI integration; empty executable searches PATH.
        codex_bin = ""
        codex_request_timeout_s = 30.0
        codex_login_timeout_s = 300.0
        # The Anthropic "Sign in" button (Internet Accounts.py →
        # fim_providers/anthropic_oauth.py): the same OAuth login that
        # `ant auth login` performs, written as an SDK profile the
        # anthropic client reads and refreshes for itself.
        #
        # OAuth client the login runs as - the official CLI's public id,
        # so the profile it mints is one `ant` and the SDKs share/refresh.
        anthropic_oauth_client_id = "41077d10-94b8-4194-be48-d251e9eb21b4"
        # Console that hosts the /oauth/authorize consent page.
        anthropic_console_url = "https://platform.claude.com"
        # Scopes requested at login (space separated).
        anthropic_oauth_scope = "user:profile user:inference user:developer"
        # SDK profile the sign-in writes: ~/.config/anthropic/{configs,
        # credentials}/<name>.json. Named - not "default" and never made the
        # active profile - so Claude Code / a bare Anthropic() elsewhere keep
        # their own login; the studio passes profile= explicitly. Extra
        # Anthropic accounts use "<name>-<account id>" unless their Login
        # profile field says otherwise.
        anthropic_profile = "lsd"
        # Give up waiting for the browser redirect after this long.
        anthropic_login_timeout_s = 300.0
        # Claude plan usage panel (Anthropic row): NO background poll (it
        # rate-limited the endpoint, 08-25). One GET /api/oauth/usage when
        # the panel opens, when a REPAINT finds the numbers older than
        # usage_refresh_s, and from the Refresh button. No panel = nothing.
        usage_refresh_s = 300.0
        # Every AUTOMATIC usage fetch (panel opened, numbers found stale by
        # a repaint, the persisted-open panel at boot) waits this long
        # before the request fires; last session's cached bars show
        # meanwhile (AccountsPanelState.usage), so a quick restart cycle
        # never reaches the endpoint. 0 = fire at once. Refresh is immediate.
        usage_fetch_delay_s = 60.0
        # Hard floor between two usage requests for one account, whatever
        # asks (a redraw, the poller, an identity change) - only the Refresh
        # button goes under it. A 429 backs off for its Retry-After, else
        # usage_backoff_s, doubling per repeat up to usage_backoff_max_s.
        usage_min_interval_s = 20.0
        usage_backoff_s = 300.0
        usage_backoff_max_s = 1800.0
        # The Claude Code executable the "Use in Claude Code" button runs
        # (`claude auth login --email ...`); empty = PATH / the usual installs.
        claude_code_bin = ""
        # Sign-in pages open as a PLACED popup: a chromeless Chromium --app
        # window forced into Xwayland (positioning works there) and parked
        # beside the pointer by xdotool (fim_providers/oauth_popup.py); a
        # dedicated profile ~/.lsd/oauth-browser keeps the Google session
        # between sign-ins. Off / no Chromium / no xdotool → plain xdg-open.
        use_oauth_popup = True
        oauth_popup_browser = ""
        oauth_popup_size = (520, 760)
        # A toast (tag "anthropic") + log line for EVERY Anthropic API
        # request the studio makes - usage fetches, FIM completions, Test,
        # sign-in token exchanges, retries (fim_providers/anthropic_requests.py).
        notify_requests = True


    @defaults(tint=(0.63, 0.44, 0.2))
    class GC:
        # Deliberate collector scheduling (gc_manager.tick in Melty.end_frame):
        # gen2's auto-trigger is deferred and full collects run at
        # input-idle instead of landing mid-keystroke (the observed 3.3s
        # gen2 stall in the render thread). Off = stock collector.
        enable = True
        manage = True
        # Auto gen2 threshold while managed - effectively "manual threshold";
        # the idle collector below is what actually runs full passes.
        gen2_threshold = 1000000
        # Seconds of input quiet before the boot freeze / an idle collect.
        idle_seconds = 15.0
        # Minimum spacing between idle collects.
        idle_collect_s = 120.0
        # Minimum spacing for a collect that at the moment the window
        # LOSES focus (alt-tab / minimize): the one frame nobody is watching.
        # Focus-gain restarts the idle clock, so returning never collects.
        unfocus_collect_s = 200.0
        # The focus-loss collect fires only after the window has stayed
        # unfocused, with no mouse presence, this long: the polled focus
        # edge is also what the first frame BACK reads (pointer over the
        # window, focus not regained yet), and collecting there froze the
        # return. Raise it if a loss still lands in your face; it costs
        # nothing while away.
        unfocus_confirm_s = 1.0
        # The BOOT pass (full-graph walk, seconds) needs a real absence when
        # it goes by idle rather than by focus loss: this long input quiet
        # with the window focused. Ordinary auto-freeze collects use
        # idle_seconds.
        boot_idle_seconds = 120.0
        # Minimum spacing between post-run collects (collect_after_run -
        # the live lab's per-run VRAM retirement). Auto Execute runs the
        # previewed function per mouse-drag tick; collecting after every
        # one was a ~120ms stall per frame, and even every few seconds the
        # accumulating garbage made each pass a ~400ms GIL stall. Within this
        # window runs coalesce onto a trailing one-shot collect that fires
        # once the burst rests, so the LAST run's garbage (the VRAM that
        # matters) always retires - at most one full pass per window. VRAM
        # from all generations inside the window stays pinned until then;
        # lower this if iterating on models that fill the card. 0 = collect
        # after every run.
        post_run_min_s = 120.0
        # Minimum spacing between post-run torch.cuda.empty_cache() calls -
        # separate from the collect above: freeing cached blocks is cheap
        # and is what makes freed activations actually leave VRAM (nvidia-
        # smi) while a typing burst runs the lab every keystroke. 0 = every run.
        post_run_cache_release_s = 2.0
        # Never freeze/collect before the app has been up this long (caches
        # still filling - freezing mid-load would pin a half-built graph).
        boot_delay_s = 30.0
        # On a CUDA out-of-memory, print gc_manager.report_vram_holders()
        # BEFORE the responder sweeps: the largest CUDA storages and who
        # references them (store key / draw / attr / frame / module).
        # A few seconds of gc walk, OOM-time only.
        oom_holder_report = True
        # Every scheduled collect writes a report of WHAT it reclaimed
        # (class / module / dict-signature / function, with sample
        # reprs) - one file per collect under report_dir - and its "lag"
        # toast names the top types; clicking the toast opens the report in
        # the code editor. Off = bare gc.collect(), no toast.
        reports = True
        report_dir = "~/meltygui/gc_reports"
        # Oldest reports pruned past this many.
        report_keep = 50
    cam_zoom = 1.5585

    # Presentation mode: dim the text editor's glyphs everywhere EXCEPT on
    # lines carrying a def-tint line band, so the tinted lines read as the
    # selected content for an audience. Requires TextEditor.definition_tints
    # for the exception lines to exist - with it off, every line dims. Dimmed
    # brightness comes from TextEditor.presentation_text_brightness. Also
    # hides the Fast Dock (draw_main skips it; summon windows via the global
    # search instead). Read live.
    presentation_mode = False

    # Master switch for the always-on debug chrome painted over the app: the
    # red/white texture-init tile counter in the top-left (LSDStudio's render
    # loop) and the notification / "Live" value bands along the right edge
    # (notifications.draw_notifications, gated in Melty.draw). Off = a clean
    # screen for demos and screenshots; notify()/display() keep recording, so
    # flipping it back shows the history. The GPU readout is unaffected.
    # also live.
    developer_mode = False
    # imgui's frame rate and the render-thread frame time, in a fixed-width
    # pill in the titlebar strip of every OS window, parents and children
    # alike. Painted by the root loop (Surface.frame -> titlebar.paint_fps),
    # outside every view. Read live; MELTY_SHOW_FPS=0 turns it off for one
    # app launch.
    show_fps = True

    # The notification overlay (notifications.draw_notifications, gated by
    # developer_mode above): categories stack vertically along the right
    # edge, each in its own fixed-height scrolling band. Read live.
    @defaults(tint=(0.85, 0.64, 0.13))
    class Notifications:
        # Vertical space one category's band gets (title included). Entries
        # beyond it scroll: wheel over the band, sticky at the newest end,
        # "N new" badge while scrolled back.
        # [tint=(0.85, 0.64, 0.13), show_tint=True]
        category_height = 300

    class Chat:
        # Centered transcript and right-aligned user bubble width limits.
        # [tint=(0.45, 0.48, 0.52)]
        transcript_max_width = 900.0
        # [tint=(0.72, 0.43, 0.28)]
        user_message_max_width = 680.0
        # Sidebar navigation stays neutral when the selected conversation changes.
        # [tint=(0.45, 0.48, 0.52), show_tint=True]
        navigation_tint = (0.45, 0.48, 0.52)
        # [tint=(0.58, 0.6, 0.63), show_tint=True]
        codex_tag_tint = (0.58, 0.6, 0.63)
        # [tint=(0.72, 0.43, 0.28), show_tint=True]
        claude_tag_tint = (0.72, 0.43, 0.28)
        # Selected sidebar chats rise above their folder plate.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        selected_chat_shadow_offset = 5.0
        # Drop-shadow offsets in the Chat window (cards, code blocks, buttons,
        # the transcript scrollbar thumb). 0 disables a shadow.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        shadow_offset = 1.5
        # A selected conversation card / pressed button lifts a little higher.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        selected_shadow_offset = 2.0
        # Bash / terminal blocks paint flat.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        bash_shadow_offset = 0.0
        # The file tags on write-file rows (the compare column's tab look), flat.
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        file_tag_shadow_offset = 0.0
        # Code blocks darken the window's painted fill by these factors:
        # bash a small step under the window, python darker.
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        bash_darken = 0.74
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        python_darken = 0.65
        # The dimmed first line of a collapsed command row (1 = full text tint).
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        command_text_brightness = 0.6
        # The row icons (terminal / pencil / brain) in their chips (1 = full text tint).
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        icon_brightness = 0.7
        # The Failed badge's plate / icon / text, scaled down from their design colours.
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        failed_badge_brightness = 0.7
        # Layout: the provider / account dropdowns' trigger height, the margin
        # under them before the sidebar and transcript, the gap between the
        # sidebar column and the transcript column, and the gap between the
        # conversation list and the New conversation button (all px).
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        dropdown_height = 30
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        header_margin = 8
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        column_gap = 14
        # [tint=(0.635, 0.728, 0.725, 1.0), show_tint=True]
        new_conversation_margin = 8
        # Tallest an inline picture (a pasted image, a Read of an image file)
        # draws in the transcript, in px; wider ones fit the span.
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        image_max_height = 360
        # Brightness cap (HSV value) of a user message's card; the sidebar's
        # conversation cards stay at draw_bg's 0.18.
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        user_message_bg_value = 0.3
        # Widest a command row (terminal block, file tags) or assistant prose
        # gets, in px; long lines clip / wrap at it.
        # [tint=(0.95, 0.6, 0.25), show_tint=True]
        terminal_max_width = 748

    show_filled_tiles = False
    gl_check_error = False

    # [tint=(0.0, 0.374, 0.744), show_tint=True]
    enable_jedi = True
    jedi_correctness = False

    # Build the node→span map from Python's `ast` (C code) instead of libcst's
    # PositionProvider (whole-tree codegen, ~64% of cst→dict cost).
    new_position_map = True

    # While typing, pause the background cst→dict parse at statement boundaries so
    # the render thread gets the GIL uncontended. Never sleeps render
    # [tint=(0.75, 0.46218, 0.00)]
    yield_to_ui = True

    # Timeline logging of the symbol-index / code-host load path: every
    # meaningful unit of work (parse, graph compute, warmer pass, drag wait,
    # attach) writes a timestamped, thread-labeled line to
    # /tmp/lsd_symbol_perf.log (perf_trace.py). Near-zero cost when off.
    symbol_perf_log = False    # TEMP: enabled to capture the 1300ms frame shortly after boot
    attrib_churn_log = False
    debug_threads = False

    slow_down_threads = False

    # [tint=(0.025, 0.372, 0.326)]
    profile_mode = ProfileMode.LIGHT
    debug_stale_tint = False

    # Filter Settings
    # [tint=(0.418, 0.656, 0.744)]
    brightness = 0.144
    # [tint=(0.458, 0.474, 0.5)]
    contrast = 1.213

    debug_z_depth = False
    filters = True
    filter_brightness = False
    show_excluded = True
    layer_stack_trace = False
    show_line_breaks = False

    memory_profile = False

    # Shadow Settings - the compositor shadow pass (meltygui.py post_frame:
    # ShadowCast at reduced res over the R16 rank mask, then
    # ShadowComposite's joint bilateral upsample onto the frame). Read live
    # per frame.
    shadow_downscale = 3
    # Per-view cap for add_shadow/add_glow marks: a view (draw_state)
    # can emit at most this many marks of each kind per frame; extra
    # emissions are dropped and the budget recycles next frame. Backstop
    # against a view leaking unbounded marks into the retained stores
    # (every retained mark re-stamps every finalize).
    shadow_cap = 100
    shadow_edge_sharpness = 0.021
    # Light direction the shadows are cast AWAY from, as a screen-space
    # vector (x right, y down in UV space). Only the direction matters -
    # the shader normalizes it; travel distance comes from
    # shadow_height_scale.
    shadow_light_dir = (-0.196, 0.265)
    # How far a shadow travels per unit of caster/receiver depth gap
    # (in units): higher = deeper stacks cast longer shadows.
    shadow_height_scale = 3.716
    # Penumbra widening per unit of depth gap: bigger = softer, more
    # diffuse shadows from tall casters.
    shadow_blur_scale = 0.16
    # Contact-hardening: curve of penumbra growth along the shadow's
    # LENGTH - 0 at the caster's silhouette edge, 1 at the shadow tip
    # (the shader measures the edge distance by bisecting along
    # light_dir). shadow_blur_scale stays the blur magnitude at the far
    # end; this shapes the ramp: < 1 blooms the blur rapidly just past
    # the contact edge (long shadows that go soft fast), 1 = linear
    # growth, > 1 stays crisp for most of the run and softens only the
    # tip. 0 = legacy uniform blur along the whole shadow.
    shadow_blur_exponent = -0.312
    # Blur samples in ShadowCast's penumbra ring (x3 radii per sample).
    # More = finer/less grainy penumbra, linearly more fragment work at
    # the shadow edge.
    shadow_blur_samples = 6
    # Occlusion each caster hit contributes before the depth-gap decay -
    # the base darkness of a shadow right under its caster.
    shadow_hit_strength = 0.503
    # How fast that contribution decays per unit of caster/receiver depth
    # gap: higher = deep stacks fade their shadows out sooner (clamped at
    # 0 in-shader, never lightens).
    shadow_hit_falloff = 69.627
    # Max fraction of light a deep stack of casters can block inside
    # ShadowCast (the light-transmission model's ceiling).
    shadow_strength = 0.683
    # Composite-time darkening: how far shadowed pixels mix toward
    # shadow_color (scales the ShadowCast intensity at the final blend).
    shadow_opacity = 0.684
    # What shadows mix TOWARD - a slightly blue gray by default.
    shadow_color = (0.0, 0.02, 0.05)
    # Specular highlight on the LIT edge of raised backgrounds — the edge
    # facing the light source (top-left when the shadow falls down-right;
    # direction derives from shadow_light_dir so the two always agree).
    # Value = bevel radius in px: the width of the highlight rim and the
    # apparent roundness of the edge. 0 disables the pass.
    # [tint=(0.85, 0.75, 0.05), show_tint=True]
    specular_bevel = 1.243

    # Global surface roughness for the specular rim, (0, 1]: low = tight
    # bright crest line at the edge, high = broad dim sheen at the bevel.
    specular_roughness = 0.094
    # Peak brightness of the highlight (white light added at composite).
    specular_opacity = 0.286
    # Fade of the highlight ALONG the lit edges, in px: brightest at the
    # lit corner (top-left when the shadow falls down-right), dying out
    # over this distance scanning down the left edge / across the top
    # edge. Distances come from edge walks (smooth-min over silhouette edge
    # tests on an absolute sample grid), so the gradient is smooth - no
    # dashes or stair steps. 0 = uniform rim, no fade.
    specular_fade = 3280.821
    # Size-relative cap on the fade: per axis the fade length becomes
    # min(specular_fade, rel * edge_extent), the extent being the soft
    # forward+backward distances. Large windows keep the fixed
    # specular_fade look; small widgets fade out within their own edge
    # instead of holding a uniform bright rim. 0 = pure fixed fade.
    specular_fade_rel = 0

    # Depth falloff: specular intensity decays as exp(-depth * rate), so
    # surfaces near the floor catch the full highlight and high-stacked
    # windows progressively lose it. One layer slot is ~0.3 depth steps
    # at the 64x32 layer/depth config; 0 = depth-independent.
    specular_depth_falloff = 0.0
    # Slope tolerance for the bevel edge march, in depth units per px: a
    # sample only counts as a silhouette edge when it drops more than
    # eps + slope*distance below the start depth. Backgrounds interpolate
    # depth across their quad, so without this a tilted surface can read
    # as a phantom edge.
    specular_slope_tol = 0.003

    # Glow Settings - add_glow() marks rendered as light sources in the
    # shadow composite (blit_offscreen PASS 6 stamps the low-res light
    # buffer; ShadowComposite adds it and cuts shadow under it).
    glow = True
    # Resolution divisor for the glow light buffer. The falloff is smooth by
    # construction, so it survives aggressive downscaling; the composite's
    # bilinear fetch upsamples for free.
    glow_downscale = 1
    # Master strength of the glow light at composite time.
    glow_strength = 0.091
    # How strongly glow luminance cancels shadow beneath it (0 = shadows
    # ignore glows, >1 = a full lit glow erases the shadow under it).
    # Keep MODEST: shadows are cast relative from the casters (light_dir),
    # so a strong cut brightens a band-shaped region DISPLACED from the
    # glow - it reads as a second copy of the glow drawn over itself, and
    # it shifts between live-rendered and blit-served frames because the
    # depth detail under the band differs subtly between those paths.
    # (Confirmed by glow_debug_log: dups=0 = one stamping, one composite
    # - the "double" is this cut, not a second glow rendering.)
    glow_shadow_cut = -0.291

    # Downward AREA-LIGHT glow mode. Off = the omnidirectional
    # inverse-square skirt. On = each glow rect reads as a downward-facing
    # area l
    # ight: no light above or beside the source, a lit trapezoid below
    # it that widens by glow_area_spread px per px of drop, brightness held
    # flat for the first glow_area_hold fraction of the falloff radius and
    # then cut off with a sharp smoothstep - a much harder transition than
    # the inverse-square tail.
    glow_area_light = False
    # Fraction of the falloff radius over which the area light holds full
    # brightness before the gradient cutoff begins (0 = fade from the edge,
    # 0.9 = bright almost all the way down, then a hard stop).
    glow_area_hold = -1.119
    # Lateral widening of the lit trapezoid, in px per px of drop below
    # the rect (tan of the light cone's half-angle; 0 = straight down).
    glow_area_spread = 0.647
    # Falloff curve exponent past the hold point: brightness falls as
    # smoothstep^exponent, smooth at BOTH ends so there is no hard edge at
    # the far extent. 1 = plain smoothstep; higher = the light dies faster
    # near the source and trails out longer - a more prominent gradient.
    glow_area_falloff = 2.25
    # Fan-edge penumbra: the side edges of the light cone blur by this many
    # px per px of drop (symmetric about the trapezoid center) - razor-sharp
    # at the source and progressively softer with distance, like a real
    # area-light penumbra. 0 = hard fan edges all the way down.
    glow_area_edge_blur = 9.113
    # Tilt of the light, in degrees from straight down (clamped to +/-80).
    # Positive shears the fan toward screen-right as it drops; the whole
    # fan (center, edges, penumbra) shifts by tan(angle) px per px of drop.
    glow_area_angle = 0.00
    # Which edge of the emitting rect the light hangs from. True = the TOP
    # edge: the fan starts there and washes down THROUGH the rect and past
    # it (the rect interior gets the gradient too). False = the BOTTOM
    # edge: the rect interior stays fully lit and the fan starts under it.
    glow_area_top_edge = False
    # Emit from the token background's LEFT, RIGHT and BOTTOM edges
    # instead of a single downward fan (ignore glow_area_top_edge while
    # on). The hold/falloff profile runs on the distance from the rounded
    # rect itself, sheared sideways by glow_area_angle as it drops below
    # the top edge, and the skirt tapers to nothing approaching the top
    # rect so the top edge remains dark. glow_area_spread /
    # glow_area_edge_blur are fan-only and ignored here.
    glow_area_edges = False

    # Band offsets for the glow receiver mask, applied live in PASS 6 (no
    # re-render needed to tune). Lower bound is relative to the emitter's
    # ROOT WINDOW surface (negative reaches below the window, positive
    # trims up into it); upper bound is relative to the EMITTER's own
    # surface (how far above it a receiver may sit and still catch light).
    # Units: one shallow depth step (~one Melty.shadow_depth increment
    # near depth 0). Applied LINEARLY in rank space - shadow_depth_at's
    # depth curve is non-monotone, so offsets never go through it, which
    # makes large values (+/-1000) genuinely open the whole band, same as
    # glow_debug_no_mask.
    glow_mask_lower_offset = 0.054
    glow_mask_upper_offset = 0.151

    # How many consecutive EMPTY body runs (cleared without re-emitting)
    # before an emitter's retained glow drops. 1 = AUTHORITATIVE: the
    # first empty run drops the glow - live edits shed stale glow
    # instantly. Raise it if async tint recomputes (all views rebuilding
    # their tree per publish, background symbol indexing) start reading as
    # random glow loss again: each unit is another body run of grace against
    # a transient stale tint state.
    glow_clear_hold_frames = 0

    # Automatic culling of retained glow/depth marks from views judged no
    # longer visible (territory-repaint kills: tab switches, jump-to
    # content swaps). OFF = retained marks only ever change by the
    # emitter's own re-emission or explicit clears - stale glow may linger
    # after tab switches, but if a mysterious glow LOSS stops happening
    # with this off, the culling misjudged it; if it persists, the
    # depth-mask gate or the stamp path is the culprit. A narrowing tool.
    glow_auto_cull = True

    # --- Glow debug ---
    # Bypass the glow-mask receiver gate: light falls on EVERY pixel under
    # the quad. Glows appearing only with this on = the rank maths is broken
    # (emitter/floor vs the mask's receiver ranks), not the stamping.
    glow_debug_no_mask = False
    # Stamp hard full-intensity rects instead of the falloff: solid inside
    # the emitting quad, 30% across the skirt - shows position + radius
    # extent through the real pipeline.
    glow_debug_rects = False
    # Draw the entire low-res glow buffer over the whole frame (replaces the
    # image). Buffer has content but the normal view doesn't = composite
    # hookup broken; buffer empty = stamping broken.
    glow_debug_view = False
    # ~1/sec console print of pipeline counts (frame marks, retained
    # emitters, stamped quads, first mark's rank/floor band).
    glow_debug_log = False

    caller_walk_steps = 7
    draw_legacy = False
    show_full_call_stack = False

    # Screenshot output dir (screenshot.py / context menu capture)
    screenshots = ""  # Default: the user's XDG cache directory.

    debug_set_anywhere = False
    ignore_call_from = ()


@window
class Actions:
    """General-purpose stash for app triggers ("New file", "New Render
    Function", ...). Rendered by draw_actions (actions_playground.py)."""

    @staticmethod
    def new_file(name: str):
        pass

    @defaults(icon="")
    @staticmethod
    def new_render_func(name="draw_other"):
        pass

    @defaults(icon="\uf030")
    @staticmethod
    def screenshot():
        """Arm the region screenshot tool (also Ctrl+Shift+3): a crosshair
        follows the cursor; click-drag a box; on release the framebuffer
        pixels inside it are saved as a PNG (Toggles.screenshots), opened in
        the code editor, and the file path is copied to the clipboard. Esc
        cancels."""
        from meltygui.core.diagnostics.screenshot_core import arm
        arm()

    @staticmethod
    def claude_terminal():
        """Open a gnome-terminal window running `claude-d` (Claude Code in a
        studio-discoverable tmux session — see ~/bin/claude-d). The script owns
        the session lifetime; closing the window kills it."""
        import subprocess
        # Full paths + close_fds=False -> posix_spawn, not fork (forking this
        # process stalls the render thread).
        import shutil
        terminal, command = shutil.which("gnome-terminal"), shutil.which("claude-d")
        if terminal is None or command is None:
            from meltygui.core.diagnostics.notifications import notify
            notify("Install gnome-terminal and claude-d to use this action")
            return
        subprocess.Popen([terminal, "--", command], close_fds=False)


@window
class LegacyToggles:
    # All the padding settings from imgui style
    item_spacing = (3, 2)
    frame_padding = (4, 1)
    window_padding = (6, 6)
    line_height = 16


# =========
def shadow_depth_at(depth, active_layer):
    scaling = 53.42
    cap = 5.975

    divisor = max(cap, depth - scaling)

    depth_and_layer = active_layer * Core.melty.max_depth + (depth * (scaling / (divisor)))
    depth_and_layer *= Core.melty.layer_inc
    return depth_and_layer


class WindowManager:
    excluded_windows = ["demo test", "Egg Time", "Layer 1"]
