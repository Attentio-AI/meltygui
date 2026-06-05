from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import tint, Core, defaults
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager


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
    @defaults(tint=(0.1,0.1,0))
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



    # Context menu tints
    context_select_tint = (1.0, 0.7, 0.2)
    context_select_outline_alpha = -0.12
    context_select_bg_alpha = 0.592
    context_select_rounding = 4.988
    
    # Background constants
    context_menu_bg_offset = 4.0

    # Highlight outline boxes (parent + child views)
    highlight_outline_thickness = 3.0   # outline line thickness
    highlight_outline_rounding = 7.868    # corner radius of the outline boxes
    highlight_outline_alpha = 0.472      # outline opacity
    highlight_bg_alpha = 0.114           # parent fill opacity

    # Selection rect (child views)
    select_outline_thickness = 2.0       # selection outline line thickness
    select_outline_alpha = 0.65          # selection outline opacity
    select_bg_alpha = 0.08               # selection fill opacity



@window
class Swoosh:
    # Nested-window "swoosh" connector (parent outline -> nested view)
    tint = (1.0, 0.7, 0.2)   # fallback color if no style manager is available
    value = 1.023              # intensity of the highlight (super-bright yellow)
    saturation = 0.791        # saturation scale applied to the current tint
    alpha = 1.0             # opacity of the swoosh
    end_thickness = 2.304      # half-width at the two endpoints (thick)
    cap_scale = 0.962          # end-cap dot radius as a multiple of end thickness
    mid_thickness = 0.441      # half-width at the middle (thin)
    curve = 0.029             # max curve bow as a fraction of endpoint distance
    curve_ramp = 2.0        # how the curve eases in with slope (>1 stays straighter longer)
    edge_softness = 1.138     # px smoothing window for the shared-edge anchor (0 = hard)
    segments = 31            # tessellation count (higher = smoother)
    taper = 10.0              # slope of the end->middle thickness falloff
    aa_width = 1.5           # antialiased edge-stroke width in px (0 = none)

    # When the child overlaps the parent, slide both endpoints along their own
    # rect edge out of the intersection area to flank the reentrant corner of the
    # union, then bow the curve smoothly towards that corner so the connector hugs the
    # outside of the overlap instead of crossing either view. See
    # Melty._closest_perimeter_points.
    avoid_intersection = True   # slide the endpoints out of the overlap, avoid the corner
    intersect_hook = 24.0       # px the endpoints slide along the edge past the overlap
    intersect_soft = 50.0       # px of overlap depth over which to ease in from the plain cur
    overlap_padding = 20.0      # grow each rect by this so the transition starts before they touch
    envelop_tie = 0.048          # enveloped child: near-tie window for the corner edge blend
                                #   (0 = always flat edges, hard switch; 0.5 = always blending)
    envelop_corner = 0.25       # enveloped child: only round the corner when the nearer gap is
                                # within this fraction of the parent's shorter side (else stay flat)


@window(tint=(0.8, 0.5, 0.2))
class Toggles:
    debug_scroll = False
    myval = 86

    @defaults(tint=(0.2, 0.45, 0.7))
    class TextEditor:
        # Red wavy underline under unknown words in the text editor. Detection +
        # suggestion are provided by symspellpy; results are cached on the editor's
        # draw_state and only recomputed when the buffer text changes.
        enable_spell_check = False
    @defaults(tint=(0.9,0.7,0.1))
    class InvalidateTracker:
        keep_for_frames = 15
        enable = False
        draw_bvh = False

    @defaults(tint=(0.16452135145664215, 0.1824345,0.2093023))
    class Debug:
        slow_frame_rate = False

    show_filled_tiles = False
    gl_check_error = False
    enable_jedi = True
    # [tint(0.9, 0.5, 0.0)]
    jedi_correctness = False

    # Invalidation settings
    invalidate_stack_trace = False
    text_focus_stack_trace = False
    attrib_change_stack_trace = False
    attrib_churn_log = False
    debug_threads = False
    slow_down_threads = False
    render_depth = False
    ds_invalidate_stack = False
    profile_mode = ProfileMode.LIGHT
    debug_stale_tint = False
    show_line_break = False

    # Filter SettingS
    brightness = 0.371
    contrast = 1.645
    saturation = -0.4
    prefered_header_width = 17
    max_preferred_header_width = 153
    debug_z_depth = False
    filters = True
    show_excluded = True
    layer_stack_trace = False
    show_line_breaks = False

     # Shadow Settings
    shadow_downscale = 2

    shadow_edge_sharpness = 50.0
    draw_legacy = False

    # Scroll settings
    @defaults(tint=(0.2, 0.6, 0.55))
    class ScrollSettings:
        # Base speed (px per wheel-tick unit) when the view is large enough that
        # the dynamic cap doesn't bind.
        scroll_speed = 2e+03
        # A single wheel tick never jumps more than this fraction of the visible
        # (clipped) height, so small views don't overshoot.
        max_increment_fraction = 0.5
        # Reserved for time-based scroll acceleration: wheel ticks arriving within
        # this window count as a continuous gesture. Not yet wired into the code.
        acceleration_threshold = 200  # ms

    show_full_call_stack = False
    ignore_call_from = ("draw", "_run_visualization", "run", "_bootstrap",
                        "_bootstrap_inner", "convert_in_and_out", "draw_melty_windows",
                        "draw_main", "end_frame", "render", "draw_inner",
                        "draw_inner_main", "__call__", "draw_collection", "draw_with_view_funcs")
    # Screenshot output directory (used by screenshot.py / context menu capture)
    screenshots = "/home/lukas/melty/screenshots"

@window
class LegacyToggles:
    # All the padding settings from imgui style
    item_spacing = (5, 2)
    window_padding = (6, 6)
    frame_padding = (4, 1)
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