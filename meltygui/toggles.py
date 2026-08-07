from dataclasses import dataclass
from enum import Enum

from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core, defaults
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager
import torch
import json


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
    tint = (0.54, 0.36911, 0.00)   # fallback color if no style manager is available
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


    # [tint=(0.739, 0.111, 0.111, 1.0), show_tint=True]
    ribbon_alpha = 0.10         # fill opacity of the band (below the fade area)
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
    # connector at mouse_falloff_floor and light one to full opacity only when
    # it is in play - its child window is being dragged/resized, the parent
    # window it hangs off is, or the parent view (not the window itself) is
    # focused. Dragging a PARENT window lights every connector hanging off it;
    # dragging a CHILD window lights only that parent's own connector. False
    # keeps the original behavior: distance fade + hover override.
    drag_focus = True

    mouse_falloff = False               # enable the distance-based opacity fade
    mouse_falloff_dist_parent = 49.576   # px: parent-end falloff distance (lower =
                                       # the parent side dims sooner as you leave it)
    mouse_falloff_dist_child = 638.1   # px: child-end falloff distance
    mouse_falloff_floor = 0.07         # opacity multiplier when far away (0 = invisible)
    mouse_falloff_exp = 2.2            # falloff curve exponent (>1 = stay bright near
    selectable = False
                                       # the rect, then drop off; 1 = linear)


@window(tint=(0.25, 0.29, 0.31))
class Toggles:

    @defaults(tint=(0.15, 0.135, 0.117, 1.0))
    class TextEditor:

        enable_spell_check = False
        text_focus_stack_trace = False
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
        token_match_tint = (0.277, 0.50, 0.50, 0.22)
        # [tint=(0.55, 0.496, 0.147, 1.0), show_tint=True]
        check_syntax_errors = True

        # [tint=(0.152, 0.143, 0.628), show_tint=True]
        freeze_cst_dict = False
        # Fast-path syntax check: re-run a bare compile() over the buffer
        # INLINE on every edit and swap the red marker immediately, instead of
        # hiding it until the debounced background reparse lands (~300ms after
        # typing goes idle). compile() is a C parser - no libcst - so a
        # typical span buffer takes well under 1ms; buffers over
        # fast_check_max_chars skip it and keep the debounced-only behavior.
        # No effect with check_syntax_errors off. Read live.
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
        fast_check_changed_region = True

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
        parse_debounce_ms = 833

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

        # Incremental cst→dict conversion: when a previous good parse exists,
        # re-convert only the changed top-level statements and splice them
        # into the held parse + module cst (cst_dict_incremental_update) -
        # O(edited statements) instead of the 150-550ms whole-buffer parse.
        # Falls back to the full conversion on any doubt. Read live.
        incremental_cst_parse = True

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
            auto_index = True

            # Incremental symbol-usage refresh on live edits: reuse the prior
            # compute's expensive half (cross-file callers + defs, ~80% of cost)
            # and rescan only the changed file + new names. Off = full recompute.
            # Fast path only.
            incremental_symbol_index = True

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

            ("white", "("): [
                Snippet("", "(1.0, 1.0, 1.0, 1.0)", ""),
            ],
            ("black", "("): [
                Snippet("", "(0.0, 0.0, 0.0, 1.0)", "", tint=(0.05, 0.05, 0.05)),
            ],
            ("blue", "("): [
                Snippet("", "(0.071, 0.354, 0.511)", "", tint=(0.071, 0.354, 0.511)),

            ],
            ("red", "("): [
                Snippet("", "(0.72, 0.11, 0.11)", "", tint=(0.72, 0.11, 0.11)),
            ],
            ("green", "("): [
                Snippet("", "(0.13, 0.55, 0.13)", "", tint=(0.13, 0.55, 0.13)),
            ],
            ("orange", "("): [
                Snippet("", "(0.85, 0.45, 0.05)", "", tint=(0.85, 0.45, 0.05)),
            ],
            ("yellow", "("): [
                Snippet("", "(0.85, 0.75, 0.05)", "", tint=(0.85, 0.75, 0.05)),
            ],
            ("purple", "("): [
                Snippet("", "(0.45, 0.15, 0.60)", "", tint=(0.45, 0.15, 0.60)),
            ],
            ("teal", "("): [
                Snippet("", "(0.05, 0.55, 0.55)", "", tint=(0.05, 0.55, 0.55)),
            ],
            ("pink", "("): [
                Snippet("", "(0.90, 0.40, 0.60)", "", tint=(0.90, 0.40, 0.60)),
            ],
            ("gray", "("): [
                Snippet("", "(0.5, 0.5, 0.5)", "", tint=(0.5, 0.5, 0.5)),
            ],
            ("cyan", "("): [
                Snippet("", "(0.05, 0.70, 0.85)", "", tint=(0.05, 0.70, 0.85)),
            ],
            ("magenta", "("): [
                Snippet("", "(0.80, 0.10, 0.80)", "", tint=(0.80, 0.10, 0.80)),
            ],
            ("brown", "("): [
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
                Snippet("", "dl.add_rect_filled(x, y, x + w, y + h, imgui.get_color_u32_rgba($0), "
                            "rounding=getattr(draw_state, 'corner_radius', 6))",
                        ""),
            ],
            ("pos",): [
                Snippet("", "pos = imgui.get_cursor_screen_pos()", ""),
            ],
            ("u32",): [
                Snippet("", "imgui.get_color_u32_rgba($0)", ""),
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

        # When the caret rests on an identifier, every OTHER place that exact
        # token appears in the visible buffer gets this background wash. A dumb,
        # identifier-bounded character match - no CST / symbol-DB metadata is
        # involved, so it works in any text, mid-edit or unparseable. Flip the
        # toggle to disable; the (r, g, b, a) tint is read live.
        highlight_token_matches = True
        def_block_alpha = 0.148
        def_symbol_alpha = 0.616
        def_line_alpha = 0.089
        # Outline drawn around each def-tint wash rect (blocks, line bands,
        # symbol washes) - makes the highlight edges read crisply against
        # the background. The outline color is the wash color BRIGHTENED by
        # def_outline_brightness (multiplied after the bg brightness clamp,
        # so it pops where the fill stays muted). 0 alpha disables.
        def_outline_alpha = 1.0
        def_outline_brightness = 0.8
        def_outline_thickness = 1.5


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

        def_line_blur_radius = 460
        # Alpha multiplier for the blurred band only - feathering spreads
        # the color thin, so the blur usually wants MORE alpha than the
        # hard rect's def_line_alpha. 1.0 = same as the hard band.
        def_line_blur_alpha = 1.622
        # Falloff hardness for the blur's inverse-square profile - how
        # concentrated the "lightsource" is. Higher = tighter core with a
        # longer radial tail; 0 falls back to the default linear feather.
        def_line_blur_falloff = 4.162
        # Layer count for the feather stack. More samples = smoother
        # gradient (fewer visible bands) at the cost of overdraw - large
        # radii need more; ~1 sample per 3-4px of radius reads smooth.
        def_line_blur_samples = 103

        # Glyphs inside a symbol wash lean this fraction toward the wash
        # color (syntax color stays the base) — the slight text tinting used
        # app-wide so text reads as part of its panel. 0 disables.
        # [tint=(0.278, 0.076, 0.126, 1.0)]
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

        # Background wash color adjustment - applies to ALL def-tint
        # backgrounds (symbol washes, line bands, block washes, number
        # boxes, the glyph-mix target) AND, sans the brightness clamp, to
        # tinted comment text: hsv factors plus a clamp on PERCEIVED
        # brightness (0.299r+0.587g+0.114b) so text stays visible even when the
        # tint is very bright (scaled down to max) or very dark (lifted to
        # min, hue kept). Neutral = 1 / 1 / 0 / 1.
        bg_tint_saturation = 1.15
        bg_tint_value = 0.48
        bg_min_brightness = 0.18
        bg_max_brightness = 0.349

    class Voxels:
        # Output gamma on the finished voxel image, folded into the raymarch
        # shader's final sRGB encode: 1.0 = pure sRGB encode (brightest,
        # colorimetrically "correct"); 2.2 = raw linear out (darkest). Read
        # live per frame by draw_voxels.
        gamma = 2.1

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
        scale = 0.80

    # [icon=""]
    @defaults(tint=(0.36, 0.42, 0.52))
    class Melty:
        # Custom client-side titlebar: undecorated OS window so the UI sticks
        # to the top of the display, with min/max/close drawn to the overlay
        # drawlist top-right and drag/edge-resize handed to the WM via
        # _NET_WM_MOVERESIZE (titlebar.py). X11/XWayland only - on native
        # Wayland the toggle is ignored and server decorations stay. Applied
        # live each frame (glfw.set_window_attrib) and read at boot from the
        # DECORATED window hint.
        enhanced_titlebar = True

        # px height of the invisible drag strip along the top edge - a drag
        # outside inside it (a few px of travel past the press) moves the OS
        # window; a clean click there falls through to whatever view is under
        # the cursor. Double-click toggles maximize.
        drag_strip_height = 50

        # px hit zones for edge/corner resize on the undecorated window.
        resize_border = 6
        resize_corner = 18

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

    # [icon=""]
    @defaults(tint=(0.427, 0.541, 0.616))
    class ContextMenu:
        # Which tab a newly opened context menu selects, as an index into its
        # tab bar: 0 Info, 1 Config, 2 view type, 3 Eval, 4 Input, 5 Tint.
        default_tab = 3

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

    @defaults(tint=(0.47, 0.463, 0.417))
    class ScrollSettings:
        scroll_speed = 214
        max_increment_fraction = 0.169
        acceleration_threshold = 0.036  # ms
        bg_offset = 30
        debug_scroll = False

    # [icon=""]
    @defaults(tint=(0.315, 0.489, 0.322))
    class LoadSave:
        # When True, save() ALSO writes the raw custom.ini (root_new eval blob)
        # as a backout alongside the new-pickle custom.pkl. Set False to go
        # pickle-only (skip the .ini dual-write). NOTE: model_server still treats
        # custom.ini as the main host identity / hot-reload cache anchor, so kee
        # this off until the .ini is fully deprecated.
        ini_save = False

    @defaults(tint=(0.72, 0.35, 0.3))
    class FileSafety:
        # Kill switch for folder_io's reconcile deletes: while True, a key
        # removed from a held folder tree never unlinks/rmtrees on disk (the
        # poller re-discovers the file and the key comes back). Flip off when
        # the file machinery has earned trust.
        block_file_delete = True

    @defaults(tint=(0.27, 0.7, 0.52))
    class HostLifecycle:
        # Deregister a RenderHost from Melty.render_hosts (stops its background
        # draw/parse) once none of its consumer windows are active. Host + parse
        # stay cached; reopening re-registers (notify_on_change → register).
        deregister_idle = True
        # A consumer is gone when its window is abs_closed, or it hasn't
        # re-registered within this many frames (safety net for closes abs_closed
        # misses). Also the birth grace before a new host can be swept.
        idle_frames = 120

    @defaults(tint=(0.181, 0.119, 0.294))
    class InputHandlerToggles:
        show_debug = False

    @defaults(tint=(0.652, 0.672, 0.733))
    class TerminalSettings:
        # Minimum logical terminal size, in pixels - independent of the window size.
        min_height = 506.5
        min_width = 94.154

    @defaults(tint=(0.478, 0.265, 0.265))
    class InvalidateTracker:
        keep_for_frames = 100
        enable = False
        draw_rect = True
        invalidate_stack_trace = False

        # [tint=(0.128, 0.25, 0.148), show_tint=True]
        invalidate_request_render = False
        attrib_change_stack_trace = False
        draw_bvh = False

    # Global App Toggles
    @defaults(tint=(0.63, 0.44, 0.2))
    class GC:
        # Deliberate collector scheduling (gc_manager.tick in Melty.end_frame):
        # gen2's auto-trigger is deferred and full collects run at
        # input-idle instead of landing mid-keystroke (the observed 3.3s
        # gen2 stall in the render thread). Off = stock collector.
        manage = True
        # Auto gen2 threshold while managed - effectively "manual threshold";
        # the idle collector below is what actually runs full passes.
        gen2_threshold = 1000000
        # Seconds of input quiet before the boot freeze / an idle collect.
        idle_seconds = 15.0
        # Minimum spacing between idle collects.
        idle_collect_s = 120.0
        # Never freeze/collect before the app has been up this long (caches
        # still filling - freezing mid-load would pin a half-built graph).
        boot_delay_s = 30.0

    @defaults(tint=(0.378, 0.286, 0.201))
    class Collection:
        pre_load_items = 26
        placeholder_height = 30.0
        drop_tail_height = 8

        max_preferred_header_width = 70
        preferred_header_width = 132
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
    # loop) and the notification / "Live" value columns in the top-right
    # (notifications.draw_notifications, gated in Melty.draw). Off = a clean
    # screen for demos and screenshots; notify()/display() keep recording, so
    # flipping it back shows the history. The GPU readout is unaffected.
    # also live.
    developer_mode = False

    show_filled_tiles = False
    gl_check_error = False

    # [tint=(0.04, 0.286, 0.422), show_tint=True]
    enable_jedi = True
    jedi_correctness = False

    # Build the node→span map from Python's `ast` (C code) instead of libcst's
    # PositionProvider (whole-tree codegen, ~64% of cst→dict cost).
    new_position_map = True

    # While typing, pause the background cst→dict parse at statement boundaries so
    # the render thread gets the GIL uncontended. Never sleeps render
    # [tint=(0.75, 0.46218, 0.00)]
    yield_to_ui = True

    # Automatic 3-way merge of external file changes against pending/live edits
    # (code_file_io's automerge + PendingSave's recompile-time absorb). Off =
    # fall back to the manual Merge window / Merge / Keep-mine buttons.
    auto_merge = False

    # Timeline logging of the symbol-index / code-host load path: every
    # meaningful unit of work (parse, graph compute, warmer pass, drag wait,
    # attach) writes a timestamped, thread-labeled line to
    # /tmp/lsd_symbol_perf.log (perf_trace.py). Near-zero cost when off.
    symbol_perf_log = False   # TEMP: on while debugging redundant symbol computes / convert_outlines
    attrib_churn_log = False
    debug_threads = False

    slow_down_threads = False

    # [tint=(0.025, 0.372, 0.326)]
    profile_mode = ProfileMode.LIGHT
    debug_stale_tint = False

    # Filter Settings
    # [tint=(0.418, 0.656, 0.744)]
    brightness = 0.762
    # [tint=(0.025, 0.032, 0.044)]
    contrast = 2.518

    debug_z_depth = False
    filters = True
    show_excluded = True
    layer_stack_trace = False
    show_line_breaks = False

    # Shadow settings
    shadow_downscale = 2
    shadow_edge_sharpness = 49.833

    caller_walk_steps = 7
    draw_legacy = False
    show_full_call_stack = False

    # Screenshot output dir (screenshot.py / context menu capture)
    screenshots = "/home/lukas/melty/screenshots"



    debug_set_anywhere = False
    ignore_call_from = ()

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