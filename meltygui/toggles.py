from enum import Enum

from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core, defaults
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager


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
    if minc == maxc:
        return 0.0, 0.0, v
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
    max_saturation = 3.0
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
                      min(max(active_hsv[2] * value_factor, -1), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.191, 0.428, 0.157))
    def checkbox_text_true():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 1.1
        value_factor = 2.125

        active_hsv = ((active_hsv[0] + hue_delta),
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.54, 0.54, 0.54))
    def cursor_tint():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.958
        value_factor = 2.899

        active_hsv = ((active_hsv[0] + hue_delta),
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.45, 0.45, 0.45))
    def line_number_tint():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.75
        value_factor = 4.10

        active_hsv = ((active_hsv[0] + hue_delta),
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
                      min(max(active_hsv[2] * value_factor, 0), Tint.max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.1, 0.12, 0.14))
    def line_number_bg():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 1.6
        value_factor = 0.35

        active_hsv = ((active_hsv[0] + hue_delta),
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
                      min(max(active_hsv[2] * value_factor, 0), max_value))
        return hsv_to_rgb(*active_hsv)

    @staticmethod
    @defaults(tint=(0.9, 0.0, 0))
    def subtle_text():
        style_manager: ImGuiStyleManager = Core.melty.style_manager
        active_hsv = style_manager.hsv

        hue_delta = 0.00
        saturation_factor = 0.7
        value_factor = 0.668

        active_hsv = ((active_hsv[0] + hue_delta),
                      min(max(active_hsv[1] * saturation_factor, 0), Tint.max_saturation),
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
    tint = (0.93, 0.8545676, 0.69)   # fallback color if no style manager is attache
    value = 1.023              # intensity of the highlight (super-bright yellow)
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
    ribbon_alpha = 0.07         # fill opacity of the band (inside the fade area)
    ribbon_fade_size = 328.2    # px: the fill starts thinning once the band's AREA
                                # exceeds fade_size x fade_size; alpha then scales
                                # inversely with area (constant total ink, 0 = off)
    ribbon_edge_alpha = 0.05    # opacity of the band's two boundary strokes
    ribbon_edge_fade_length = 7.4  # px: a boundary stroke starts thinning once its
                                # own arc length exceeds this; alpha scales inversely
                                # with length, per stroke (0 = off)
    ribbon_edge_thickness = 2.7 # boundary stroke thickness in px (0 = no stroke/lines)

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
    # its OWN distance scale (parent vs child), and the connector is whichever
    # side is brighter - so the parent end can dim faster than the child end.
    # Distance is measured from each rect (0 when the mouse is inside it). Applies
    # to both the line and ribbon mode.
    mouse_falloff = True               # enable the mouse-based opacity fade
    mouse_falloff_dist_parent = 25.8   # px: parent-end falloff distance (lower =
                                       # the parent side dims sooner as you leave it)
    mouse_falloff_dist_child = 638.1   # px: child-end falloff distance
    mouse_falloff_floor = 0.07         # opacity multiplier when far away (0 = invisible)
    mouse_falloff_exp = 2.2            # falloff curve exponent (>1 = stay bright near
                                       # the rect, then drop off; 1 = linear)


@window(tint=(0.563, 0.58, 0.60))
class Toggles:

    @defaults(tint=(0.456, 0.611, 0.767, 1.00))
    class TextEditor:
        enable_spell_check = False
        text_focus_stack_trace = False
        token_match_tint = (0.277, 0.50, 0.50, 0.22)

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

        # When the caret rests on an identifier, every OTHER place that exact
        # token appears in the visible buffer gets this background wash. A dumb,
        # identifier-bounds character match - no CST / symbol-usage metadata is
        # involved, so it works in any text, mid-edit or unparseable. Flip the
        # flag to disable; the (r, g, b, a) tuple is read live.
        highlight_token_matches = True
        # Definition tints: a class/def whose definition carries a tint
        # (@defaults(tint=...), a '# [tint=...]' override comment, or a
        # class-body tint=...) gets a full-body background wash in the editor,
        # and every occurrence of that symbol - even when its definition lives
        # in another file - gets a small wash of the same color. The washes take
        # the tint's rgb with these alphas (the tint's own alpha is a view-
        # wide opacity, not meant for text washes). All read live

        # [tint=(0.047, 0.492, 0.939, 1.00), show_tint=True]
        definition_tints = True
        def_block_alpha = 0.148
        def_symbol_alpha = 0.616
        def_line_alpha = 0.089
    
        # Assignment propagation: a local defined FROM tinted symbols takes a
        # faded blend of their colors (single-symbol assignment averages the distinct
        # tints), fading a further step per hop so a value's color trail
        # weakens as it flows. Fade multiplies the wash alpha per hop -
        # LOWER = colors die out faster along assignment chains (0.55 puts
        # hop 1 at 55%, hop 2 at 30%; chains below 20% stop washing at all).
        def_tint_propagation = True
        def_propagation_fade = 0.705


        # When enabled the line tint rect above fills the whole line -
        # gutter edge to the view's right edge - instead of hugging the
        # line's text extent (indent → last non-ws column).
        def_line_full_width = True

        # Glyphs inside a symbol wash lean this fraction toward the wash
        # color (syntax color stays the base) — the slight text tinting used
        # app-wide so text reads as part of its panel. 0 disables.
        # [tint=(0.278, 0.076, 0.126, 1.0)]
        def_text_tint_mix = 0.293

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
        # [tint=(0.033, 1.0, 0.0, 1.0)]
        comment_tint_value = 0.160
        # Legibility floor for tinted COMMENT TEXT - independent of the
        # washes' bg_min_brightness (text needs a higher floor than a
        # background does); the brightness clamp still shares bg_max_brightness.
        comment_min_brightness = 0.170

        # Background wash color adjustment - applies to ALL def-tint
        # backgrounds (symbol washes, line bands, block washes, number
        # boxes, the glyph-mix target) AND, sans the brightness clamp, to
        # tinted comment text: hsv factors plus a clamp on PERCEIVED
        # brightness (0.299r+0.587g+0.114b) so text stays visible even when the
        # tint is very bright (scaled down to max) or very dark (lifted to
        # min, hue kept). Neutral = 1 / 1 / 0 / 1.
        bg_tint_saturation = 1.0
        bg_tint_value = 0.432
        bg_min_brightness = 0.18
        bg_max_brightness = 0.41

    @defaults(tint=(0.427, 0.541, 0.616))
    class ContextMenu:
        # The tab a freshly opened context menu selects, as an index into its
        # tab bar: 0 Info, 1 Config, 2 view function, 3 Eval, 4 Help, 5 Tint.
        default_tab = 2

    @defaults(tint=(0.631, 0.474, 0.861))
    class InputHandlerToggles:
        show_debug = False


    @defaults(tint=(0.652, 0.672, 0.733))
    class TerminalSettings:
        # Minimum console terminal size, in pixels - independent of the window size.
        min_height = 506.5
        min_width = 94.154


    @defaults(tint=(0.315, 0.489, 0.322))
    class LoadSave:
        # When True, save() ALSO writes the legacy custom.ini (root_new eval blob)
        # as a backout alongside the native-pickle custom.pkl. Set False to go
        # pickle-only (skip the .ini dual-write). NOTE: model_server still treats
        # custom.ini as the app's identity / hot-reload cache anchor, so leave
        # this True until the .ini is fully retired.
        ini_save = False

    @defaults(tint=(0.189, 0.486, 0.944))
    class WindowSettings:
        # Sticky resize: reamp the window top to the drag-start position each
        # frame so only the bottom-on-display clamp displaces it.
        sticky_drag = True

    @defaults(tint=(0.358, 0.444, 0.348))
    class InvalidateTracker:
        keep_for_frames = 26
        enable = False
        draw_rect = True
        invalidate_stack_trace = False
        attrib_change_stack_trace = False
        draw_bvh = False

    @defaults(tint=(0.57, 0.4161, 0.057))
    class ScrollSettings:
        scroll_speed = 611
        max_increment_fraction = 0.169
        acceleration_threshold = 0.036  # px
        bg_offset = 30
        debug_scroll = False

    @defaults(tint=(0.65, 0.385, 0.069, 1.0))
    class SearchSettings:
        # Search matches are highlighted with a radial gradient "glow" that
        # radiates out from the matched rectangle (rounded-rect cutout), with the
        # rect itself blurred out so the matched text stays readable. The CURRENT
        # match uses ActiveElement; every other match uses InactiveElements, so
        # the two can be tuned (color/falloff/opacity/...) independently. Glows
        # combine where they overlap. Applies in both the text editor (draw_text)
        # and collections (draw_collection_line). See view/core_views/search_glow.py.

        @defaults(tint=(0.965, 0.6, 0.149))
        class ActiveElement:
            gradient_color = (0.86, 0.67, 0.23)   # RGB of the halo
            outline_color = (0.88, 0.56, 0.15)   # RGB of the optional cutout outline
            falloff = 26.696          # px the glow radiates out past the match edge
            opacity = 0.176           # total opacity, reached at the cutout edge
            falloff_exp = 2.105       # >1 = bright at the word, then drops off fast
            inner_pad = 0.00         # px the cutout is grown beyond the match rect
            cutout_radius = 5.0       # corner radius of the rounded cutout
            rings = 86                # radial tessellation steps (higher = smoother)
            corner_segments = 15       # arc subdivisions at each rounded cutout corner
            outline_alpha = 1.00       # 0 = rely on the glow's bright inner rim alone
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

    # Global Feature Toggles
    @defaults(tint=(0.378, 0.286, 0.201))
    class Collection:
        pre_load_items = 26
        placeholder_height = 30.0
        drop_tail_height = 8

        max_preferred_header_width = 70
        preferred_header_width = 132

    @defaults(tint=(0.27, 0.7, 0.52))
    class HostLifecycle:
        # Deregister a RenderHost from Melty.rendering (stops per-frame
        # draw/parse) once none of its consumer windows are active. Host + parse
        # stay cached; reopening re-registers (close_on_change → register).
        deregister_idle = True
        # A consumer is gone when its window is abs_closed, or it hasn't
        # re-registered within this many frames (safety net for any abs_closed
        # misses). Also the birth grace before a new host can be swept.
        idle_frames = 120

    show_filled_tiles = False
    gl_check_error = False
    enable_jedi = True
    jedi_correctness = False
    # Auto-compute symbol usages after every editor parse (recommended, fast path
    # only); the Index button stays as a force refresh.
    auto_index = True

    # Incremental symbol-usage refresh on live edits: cache the prior compute's
    # expensive half (cross-file callers/ces, ~80% of cost) and rescan only the
    # edited file + new names. Off = full recompute. Fast path only.
    incremental_symbol_index = True

    # Position-only fast path: when an edit only added/removed blank lines, remap
    # the cached line numbers by the delta (~5ms vs ~42ms) instead of recomputing.
    offset_symbol_positions = True

    # Add function-local variables (params + in-function bindings) to the symbol
    # usage graph. to hover + double-click to their uses like any symbol. Cheap
    # now that the editor's line<->index helpers are O(log n) (see _line_starts);
    # toggle off to drop locals from the graph if ever needed.
    local_symbol_usages = True

    # Build the node→span map from Python's `ast` (C code) instead of libcst's
    # PositionProvider (whole-tree codegen, ~64% of cst→dict cost).
    new_position_map = True

    # While typing, pause the background cst→dict parse on statement boundaries so
    # the render thread gets the GIL uncontended. Never sleeps render.

    # [tint=(0.7555555701255798, 0.46218958497047424, 0.00)]
    yield_to_ui = True

    # Timeline logging of the symbol-index / code-host load path: every
    # meaningful unit of work (parse, graph compute, warmer pass, drag wait,
    # attach) writes a timestamped, thread-labeled line to
    # /tmp/lsd_symbol_perf.log (perf_trace.py). Near-zero cost when off.
    symbol_perf_log = True   # TEMP: on while debugging redundant symbol computes / convert.py hang
    attrib_churn_log = False
    debug_threads = False

    slow_down_threads = False

    # [tint=(0.025, 0.372, 0.326)]
    profile_mode = ProfileMode.LIGHT
    debug_stale_tint = False

    # View Settings

    # [tint=(0.418, 0.656, 0.744)]
    brightness = 0.530
    # [tint=(0.025, 0.032, 0.044)]
    contrast = 2.061

    debug_z_depth = False
    filters = True
    show_excluded = True
    layer_stack_trace = False
    show_line_breaks = False
    ignore_call_from = ()

    # Shadow settings
    shadow_downscale = 2
    shadow_edge_sharpness = 49.833

    caller_walk_steps = 7
    draw_legacy = False
    show_full_call_stack = False

    # Screenshot output dir (screenshot.py / context menu capture)
    screenshots = "/home/lukas/melty/screenshots"


    debug_set_anywhere = False

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