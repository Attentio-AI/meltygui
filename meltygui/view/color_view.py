"""Color view functions and supporting definitions."""
from meltygui.gl_state import GLState
from meltygui.rendering.core_render import render_func
from meltygui.state.new_core_model import ColorPickerState
from meltygui.state.new_core_model import DrawState
from meltygui.state.new_core_model import TabState
from meltygui.toggles import Toggles
import meltygui_imgui as imgui
import types
from meltygui.hdr_color import pack_color
from meltygui.toggles import Tint


def draw_style_policy_fast(owner, draw_state):
    """One shared code-host editor, only for the selected effective policy."""
    from meltygui.code.new_converters import code_hosts_for
    from meltygui.code.new_converters import host_code_state
    from meltygui.fonts import Font
    from meltygui.utils.glfw_utils import request_render
    from meltygui.view.text_view import draw_text
    from meltygui.core.color_core import _add_style_policy
    from meltygui.core.color_core import _style_policy_source

    fields = ("tint_fn", "font_size_fn", "font_weight_fn", "shadow_fn")
    selected = draw_state.misc.get("style_policy_index", 0)
    imgui.push_item_width(180)
    imgui.push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0.0, 0.0, 0.0, 0.0)
    changed, selected = imgui.combo("Parent policy", selected,
                                    ["Tint", "Font size", "Font weight", "Shadow"])
    imgui.pop_style_color()
    imgui.pop_item_width()
    if changed:
        draw_state.misc["style_policy_index"] = selected
        request_render()
    fn, source = _style_policy_source(owner, fields[selected])
    if source is not owner:
        from meltygui.view.header_view import flat_button
        if flat_button("+ Add policy here", draw_state,
                       view_id="add_style_policy_" + fields[selected], shadow=False):
            _add_style_policy(owner, fields[selected])
            fn, source = _style_policy_source(owner, fields[selected])
    origin = "default" if source is None else ("this view" if source is owner else str(source.name).split("##")[0])
    imgui.text("From: " + origin)
    imgui.text_disabled("Shared function: edits apply to its users.")
    if not isinstance(fn, types.FunctionType) or fn.__name__ == "<lambda>":
        imgui.text_wrapped("This policy has no standalone function definition. Edit its containing source in Inputs.")
        return
    # Identical shared editor to Inputs; no inspect/getsource/exec in this UI.
    str_host, dict_host = code_hosts_for(fn)
    str_host.notify_on_change(draw_state)
    dict_host.notify_on_change(draw_state)
    text = str_host.get(str_host.value_key)
    if not isinstance(text, str):
        imgui.text_disabled("Loading policy…")
        return
    code_state = host_code_state(str_host)
    changed, updated = draw_text(
        text, name="style_policy_" + fields[selected],
        width=480, height=180, font=Font.JETBRAINS_MONO_13,
        is_tree=False, show_header=False, editable=True,
        code_dict=dict_host._held(),
        jump_to=code_state.address if code_state is not None else None)
    if changed and updated != text:
        str_host[str_host.value_key] = updated


def draw_style_residuals_fast(owner, draw_state=None):
    """Popover-only controls; locate reads/writes the source driving the view."""
    from meltygui.utils.glfw_utils import request_render

    from meltygui.style import Style
    from meltygui.state.core_undo import UndoManager

    value = owner.locate_style
    attr = "locate_style" if value is not None else "locate_tint"
    if value is None:
        value = owner.locate_tint
    original = value
    if not isinstance(value, Style):
        value = Style(value or (0, 0, 0), absolute=value is not None)
    rgb = list(value[:3])
    metadata = value.__getnewargs_ex__()[1]
    alpha = value[3] if len(value) == 4 else 1.0
    imgui.text("Edit " + attr.removeprefix("locate_") + " at its source")
    imgui.push_style_var(imgui.STYLE_FRAME_BORDERSIZE, 1.0)
    changed, metadata['absolute'] = imgui.checkbox("Absolute values", value.absolute)
    imgui.text_disabled("Signed shifts follow parent policy.")
    imgui.separator()
    # Let the existing background context show through the native controls.
    for slot in (imgui.COLOR_FRAME_BACKGROUND, imgui.COLOR_FRAME_BACKGROUND_HOVERED,
                 imgui.COLOR_FRAME_BACKGROUND_ACTIVE):
        imgui.push_style_color(slot, 0.0, 0.0, 0.0, 0.0)
    imgui.push_item_width(120)
    for i, label in enumerate(("Red", "Green", "Blue")):
        edited, rgb[i] = imgui.drag_float(label, rgb[i], change_speed=0.005, format="%.3f")
        changed |= edited
    edited, alpha = imgui.slider_float("Opacity", alpha, 0.0, 1.0, format="%.2f")
    changed |= edited
    imgui.separator()
    for name, label, speed in (("font_size", "Size (px)", 0.1),
                                ("font_weight", "Weight", 5.0),
                                ("shadow_offset", "Shadow", 0.1)):
        current = metadata[name]
        edited, enabled = imgui.checkbox("##enable_" + name, current is not None)
        if edited:
            metadata[name] = 0.0 if enabled else None
            changed = True
        imgui.same_line()
        if enabled:
            edited, number = imgui.drag_float(label, metadata[name], change_speed=speed, format="%.2f")
            if edited:
                metadata[name] = number
                changed = True
        else:
            imgui.text_disabled(label + " (inherit)")
    imgui.pop_item_width()
    imgui.pop_style_color(3)
    imgui.pop_style_var()
    imgui.separator()
    imgui.text_disabled("Unchecked = inherit.")
    if changed:
        updated = Style(tuple(rgb) + ((alpha,) if len(value) == 4 or alpha != 1.0 else ()), **metadata)
        setter = lambda new: setattr(owner, attr, new)
        UndoManager.record(owner, original, updated, setter=setter,
                           key="header_style_residuals", label="Style residuals")
        setter(updated)
        owner.invalidate()
        request_render()
    if draw_state is not None:
        imgui.separator()
        draw_style_policy_fast(owner, draw_state)


def draw_view_offsets_fast(owner, draw_state=None):
    """Popover-only rows under the colour tabs: the OWNER view's `bg_offset`
    and `z_offset` (VIEW_OFFSET_ROWS), read through `owner.locate_<param>`
    and written back the same way — set_anywhere picks the source that
    drives the param (a `@window(bg_offset=…)`, a caller kwarg, a `# [ ]`
    comment) and falls back to the owner's own draw_state. Every change is
    a SetterChange on the undo stack (like the colour chip's) and invalidates
    the owner; the wrapper reads both offsets on its next run."""
    from meltygui.utils.glfw_utils import request_render

    from meltygui.state.core_undo import UndoManager
    # [tint=(0.85, 0.75, 0.05)]
    row_width = 120
    imgui.separator()
    for slot in (imgui.COLOR_FRAME_BACKGROUND, imgui.COLOR_FRAME_BACKGROUND_HOVERED,
                 imgui.COLOR_FRAME_BACKGROUND_ACTIVE):
        imgui.push_style_color(slot, 0.0, 0.0, 0.0, 0.0)
    imgui.push_item_width(row_width)
    for param, label, speed in Toggles.ColorPicker.view_offset_rows:
        current = getattr(owner, "locate_" + param)
        current = int(current) if isinstance(current, (int, float)) else 0
        edited, value = imgui.drag_int(f"{label}##view_{param}", current, speed)
        if edited and value != current:
            def setter(new, _owner=owner, _attr="locate_" + param):
                setattr(_owner, _attr, new)
                _owner.invalidate()
            UndoManager.record(owner, current, value, setter=setter,
                               key="view_" + param, label=label)
            setter(value)
            request_render()
    imgui.pop_item_width()
    imgui.pop_style_color(3)


@render_func(use_cache=False, show_bg=True, shadow=False, selectable=False, with_header=None)
def draw_color_picker(input_value, wrap=True, draw_state=None, info=None,
                      picker_state: ColorPickerState = None, gl_state: GLState = None, owner=None, **kwargs):
    """The colour picker popover, three tabs: **Wide** (default) — a
    Display-P3 hue/saturation/value square whose value axis runs on above
    white (`Toggles.HDR.picker_max_stops`), rendered as an fp16 texture so
    every texel is the real HDR/P3 colour, with the sRGB-reachable region
    outlined — **sRGB**, the classic square — and **sRGB+**, the classic
    square kept at its size with a wide-gamut extension to its right
    (`_draw_extended_picker`). All take and return extended-sRGB tuples
    (hdr_color.py), so a colour picked on one tab reads back on the others
    (an out-of-sRGB value shows clipped on the sRGB tab)."""
    from meltygui.utils.glfw_utils import request_render

    # The tab strip: draw-list flat_buttons (no wrapper per tab), neutral
    # grey like draw_tabs' untinted strip — the active tab gets the filled
    # rect, the others draw label-only.
    # [tint=(0.85, 0.75, 0.05)]
    tab_color = (0.5, 0.5, 0.5)
    # [tint=(0.85, 0.75, 0.05)]
    tab_height = 21
    from meltygui.view.header_view import flat_button
    imgui.dummy(0, 2)
    tabs = [("Wide", "wide"), ("sRGB", "srgb"), ("sRGB+", "extended")]
    if owner is not None and Toggles.dynamic_styles:
        tabs.append(("Residuals", "residuals"))
    for label, key in tabs:
        selected = picker_state.tab == key
        if flat_button(label, draw_state, view_id=f"cp_tab_{key}", height=tab_height,
                       color=tab_color, factor=1.2, corner_radius=4,
                       tint_value=0.35 if selected else 0.15, saturation=0.3,
                       alpha=1.0 if selected else 0.0, text_value=1.0,
                       event="left_mouse_down"):
            picker_state.tab = key
            request_render()
        imgui.same_line(spacing=4)
    imgui.new_line()
    if picker_state.tab == "residuals" and owner is not None and Toggles.dynamic_styles:
        draw_style_residuals_fast(owner, draw_state)
        return False, input_value
    if picker_state.tab == "extended":
        result = _draw_extended_picker(input_value, draw_state, gl_state, info)
    else:
        # The band's room stays reserved: the square lands at the same pos on
        # every tab.
        imgui.dummy(0, Toggles.ColorPicker.exposure_band_height)
        if picker_state.tab == "srgb":
            result = _draw_srgb_picker(input_value, draw_state, info)
        else:
            result = _draw_wide_picker(input_value, draw_state, gl_state, info)
    if owner is not None:
        # Editing a VIEW's colour: its other paint knobs ride along under
        # the colour tabs (color_picker_height reserves two rows).
        draw_view_offsets_fast(owner, draw_state)
    return result


@render_func(use_cache=True, show_bg=True, selectable=False)
def draw_tint_context(input_value: DrawState, tab_state: TabState = None, **kwargs):
    from meltygui.view.collection_view import draw_tuple
    from meltygui.core.parameter_core import anywhere_value
    from meltygui.core.parameter_core import get_source_for
    from meltygui.core.parameter_core import set_anywhere

    if Toggles.debug_set_anywhere:
        source_name = get_source_for("tint", input_value)
        imgui.text(f"Tint source: {source_name}")

    # The live framework-resolved tint - except mid-round-trip, when the
    # pending UI value shows so the widget doesn't snap back while the
    # write→save→hotswap frames play out (anywhere_value).
    tint_changed, tint_value = draw_tuple(anywhere_value("tint", input_value), name="Tint: set anywhere", height=30,
                                          width=40,
                                          show_name=False, show_header=False)
    if tint_changed:
        set_anywhere("tint", tint_value, input_value)

    return False, None



def color_picker_height(n_channels: int, has_info: bool = False,
                        has_owner: bool = False) -> int:
    """Height of draw_color_picker's popover: tab row + exposure band +
    square + the channel rows + the readout line (+ the info caption) (+ the
    view-offset rows an owner draw_state adds, `draw_view_offsets_fast`)."""

    return (Toggles.ColorPicker.tabs_height + Toggles.ColorPicker.exposure_band_height + Toggles.ColorPicker.square_size + 14
            + n_channels * 26 + 26 + (22 if has_info else 0)
            + (Toggles.ColorPicker.offsets_height if has_owner else 0))


def color_picker_top_offset(gap: int = Toggles.ColorPicker.anchor_gap) -> int:
    """The popover's y offset below its anchor (the cursor under the
    swatch): the popover HANGS under the swatch, tabs first, and the
    exposure band pushes the square down. It used to return `gap` less the
    band so the window grew upward and the square kept its band-less spot —
    that parked the tab row above the swatch, over the host's header row
    (Lukas 09-10: too high)."""
    return gap


def color_picker_width() -> int:
    """Width of draw_color_picker's popover: the square + hue bar row of
    the widest tab (sRGB+, whose extension sits between square and hue bar)
    plus the window margins. One size for every tab — the popover is a
    fixed-size closable window and the tab is the picker's own state."""

    return 216 + Toggles.ColorPicker.extension_width


def _draw_wide_picker(input_value, draw_state, gl_state, info):
    """The Wide tab: P3 HSV + exposure (hdr_color.p3_hsv_from_extended). The
    square's X is P3 saturation, its top `picker_top_fraction` is exposure
    (2^max_stops at the top, white at the seam), the rest the classic value
    axis. The sRGB outline is the gamut edge for the current hue
    (hdr_color.srgb_region_outline). `draw_state._cpw_precise` echoes our own
    edits back like the sRGB tab's `_cp_precise`."""
    from meltygui.model.color_model import _wide_marker
    from meltygui.model.color_model import _wide_pick
    from meltygui.utils.glfw_utils import request_render
    from meltygui.view.control_view import button

    import meltygui.hdr_color as hdr_color
    # [tint=(0.85, 0.75, 0.05)]
    outline_color = (1.0, 1.0, 1.0, 0.8)
    # [tint=(0.85, 0.75, 0.05)]
    outline_shadow = (0.0, 0.0, 0.0, 0.6)
    SQ, BAR_W, GAP = Toggles.ColorPicker.square_size, 18, 8
    max_stops = float(Toggles.HDR.picker_max_stops)
    top_fraction = float(Toggles.HDR.picker_top_fraction)
    imgui.dummy(0, 3)
    vals = list(input_value)
    has_alpha = len(vals) >= 4
    r, g, b = float(vals[0]), float(vals[1]), float(vals[2])
    a = float(vals[3]) if has_alpha else 1.0
    in_r, in_g, in_b, in_a = r, g, b, a

    ECHO_TOL = 0.002
    _prec = getattr(draw_state, '_cpw_precise', None)
    is_echo = _prec is not None and all(abs(pc - c) <= ECHO_TOL for pc, c in zip(_prec[0], (r, g, b, a)))
    if is_echo:
        r, g, b, a = _prec[0]
        h, s, v, exposure = _prec[1]
    else:
        h, s, v, exposure = hdr_color.p3_hsv_from_extended(r, g, b)
        if _prec is not None:
            if s <= 0.0 or v <= 0.0:
                h = _prec[1][0]
            if v <= 0.0:
                s = _prec[1][1]

    dl = imgui.get_window_draw_list()
    white = pack_color(1, 1, 1, 1)
    black = pack_color(0, 0, 0, 1)
    changed = False
    hsv_changed = False

    # --- the square: an fp16 texture of the hue's slice (re-baked per hue) ---
    sx0, sy0 = imgui.get_cursor_screen_pos()
    tex = _wide_square_texture(gl_state, h, SQ, top_fraction, max_stops)
    if tex is not None:
        dl.add_image(tex.texture_id, (sx0, sy0), (sx0 + SQ, sy0 + SQ))
    imgui.invisible_button("##wsv", SQ, SQ)
    if imgui.is_item_active():
        mx, my = imgui.get_mouse_pos()
        s, v, exposure = _wide_pick((mx - sx0) / SQ, (my - sy0) / SQ, top_fraction, max_stops)
        hsv_changed = True

    # --- hue bar: P3 hues at full saturation ---
    imgui.same_line(spacing=GAP)
    hx0, hy0 = imgui.get_cursor_screen_pos()
    for i in range(12):
        t0, t1 = i / 12.0, (i + 1) / 12.0
        c0 = pack_color(*hdr_color.p3(*imgui.color_convert_hsv_to_rgb(t0, 1, 1)), 1)
        c1 = pack_color(*hdr_color.p3(*imgui.color_convert_hsv_to_rgb(t1, 1, 1)), 1)
        dl.add_rect_filled_multicolor(hx0, hy0 + SQ * t0, hx0 + BAR_W, hy0 + SQ * t1, c0, c0, c1, c1)
    imgui.invisible_button("##whue", BAR_W, SQ)
    if imgui.is_item_active():
        h = min(max((imgui.get_mouse_pos()[1] - hy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- sRGB region outline + the white seam ---
    pts = [(sx0 + px * SQ, sy0 + py * SQ) for px, py in hdr_color.srgb_region_outline(h, top_fraction)]
    dl.add_polyline(pts, pack_color(*outline_shadow), False, 3.0)
    dl.add_polyline(pts, pack_color(*outline_color), False, 1.0)
    seam_y = sy0 + top_fraction * SQ
    dl.add_line(sx0, seam_y, sx0 + SQ, seam_y, pack_color(1, 1, 1, 0.25), 1.0)

    # --- markers ---
    mx_, my_ = _wide_marker(s, v, exposure, top_fraction, max_stops)
    cx, cy = sx0 + mx_ * SQ, sy0 + my_ * SQ
    dl.add_circle(cx, cy, 6, black, thickness=1.0)
    dl.add_circle(cx, cy, 5, white, thickness=1.5)
    hmy = hy0 + h * SQ
    dl.add_rect(hx0 - 1, hmy - 2, hx0 + BAR_W + 1, hmy + 2, white, thickness=1.5)

    if hsv_changed:
        r, g, b = hdr_color.extended_from_p3_hsv(h, s, v, exposure)
        changed = True

    # --- channel rows: extended sRGB, so they read past 1 and below 0 ---
    imgui.dummy(0, 4)
    imgui.push_item_width(SQ + GAP + BAR_W)
    out, edited = [], []
    lo, hi = -1.0, hdr_color.linear_to_srgb(2.0 ** max_stops)
    for lbl, cur in ([("R", r), ("G", g), ("B", b)] + ([("A", a)] if has_alpha else [])):
        imgui.set_next_item_width(draw_state.content_width - 30)
        if lbl == "A":
            ch, nv = imgui.drag_float(f"{lbl}##cpw_{lbl}", cur, 0.004, 0.0, 1.0, "%.3f")
        else:
            ch, nv = imgui.drag_float(f"{lbl}##cpw_{lbl}", cur, 0.006, lo, hi, "%.3f")
        if ch:
            changed = True
        edited.append(ch)
        out.append(nv if ch else cur)
        imgui.dummy(0, 1)
    imgui.pop_item_width()
    r, g, b = out[0], out[1], out[2]
    if has_alpha:
        a = out[3]

    # --- readout: exposure + gamut ---
    # Remove the colour: returns None (the tuple popover unsets its tuple).
    if button("\uf1f8", tint=(1, 0, 0, 0.5), height=21, shadow=True, use_cache=True,
              name="delete_color##", text_value=1.6)[0]:
        request_render()
        return True, None
    imgui.same_line()
    gamut = "sRGB" if all(0.0 <= c <= 1.0 for c in (r, g, b)) else ("P3" if exposure <= 1.0 else "P3 HDR")
    imgui.text_colored(f"{exposure:.2f}× white · {gamut}", *Tint.subtle_text())
    if info:
        imgui.dummy(0, 2)
        imgui.text_colored(str(info), 1.0, 1.0, 1.0, 0.45)
    if changed:
        if has_alpha and not edited[3]:
            a = in_a
        if not hsv_changed:
            if not edited[0]:
                r = in_r
            if not edited[1]:
                g = in_g
            if not edited[2]:
                b = in_b
            nh, ns, nv, ne = hdr_color.p3_hsv_from_extended(r, g, b)
            if ns <= 0.0 or nv <= 0.0:
                nh = h
            if nv <= 0.0:
                ns = s
            h, s, v, exposure = nh, ns, nv, ne
        draw_state._cpw_precise = ((r, g, b, a), (h, s, v, exposure))
        request_render()
        return True, ((r, g, b, a) if has_alpha else (r, g, b))
    return False, input_value


def _wide_square_texture(gl_state, hue, size, top_fraction, max_stops):
    """The hue slice as an RGBA16F GLTexture, cached on the picker's GLState
    and re-baked when the hue (or the layout toggles) change."""
    if gl_state is None:
        return None
    import meltygui.hdr_color as hdr_color
    import OpenGL.GL as gl
    from meltygui.gl_state import GLTexture
    from meltygui.gl_state import _scalar

    def create():
        data = hdr_color.wide_square_linear(hue, size, top_fraction, max_stops)
        tex_id = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, size, size, 0, gl.GL_RGBA, gl.GL_FLOAT, data)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return GLTexture(tex_id, gl.GL_TEXTURE_2D, (size, size), gl.GL_RGBA16F)

    return gl_state.get("wide_square", create, lambda t: gl.glDeleteTextures([t.texture_id]),
                        deps=(round(float(hue), 4), int(size), round(top_fraction, 4), round(max_stops, 4)))


def _draw_extended_picker(input_value, draw_state, gl_state, info):
    """The sRGB+ tab: the classic sRGB HSV square at its usual size and
    place, extended on two sides — to its RIGHT a PICKER_EXTENSION-wide
    strip that carries every row on past the sRGB gamut edge into Display
    P3 (colour-continuous across the seam, the top row ending in the pure
    P3 primary), and ABOVE square and strip a PICKER_EXPOSURE_BAND-tall
    exposure band that lifts the top row from white at the seam to
    2^Toggles.HDR.picker_max_stops at the top. The whole area is one fp16
    texture per hue (hdr_color.srgb_plus_linear) so every texel is the real
    wide / HDR colour. One drag runs across all of it (_srgb_plus_pick):
    inside the square the classic (s, v); past the right seam s = 1 and x
    is the depth into P3; above the top seam v = 1 and the height is
    exposure. The hue bar is sRGB hue (the square's). Below black there is
    nothing to extend into, so the square's bottom stays the bottom.
    `draw_state._cpx_precise` echoes our own edits back like the other
    tabs' caches; `_cpx_coords` memoizes the inverse for an external value
    (a bisection, not a per-frame cost)."""
    from meltygui.model.color_model import _srgb_plus_marker
    from meltygui.model.color_model import _srgb_plus_pick
    from meltygui.utils.glfw_utils import request_render
    from meltygui.view.control_view import button

    import meltygui.hdr_color as hdr_color
    # [tint=(0.85, 0.75, 0.05)]
    seam_color = (1.0, 1.0, 1.0, 0.35)
    SQ, EXT, BAND, BAR_W, GAP = Toggles.ColorPicker.square_size, Toggles.ColorPicker.extension_width, Toggles.ColorPicker.exposure_band_height, 18, 8
    max_stops = float(Toggles.HDR.picker_max_stops)
    imgui.dummy(0, 3)
    vals = list(input_value)
    has_alpha = len(vals) >= 4
    r, g, b = float(vals[0]), float(vals[1]), float(vals[2])
    a = float(vals[3]) if has_alpha else 1.0
    in_r, in_g, in_b, in_a = r, g, b, a

    ECHO_TOL = 0.002
    _prec = getattr(draw_state, '_cpx_precise', None)
    is_echo = _prec is not None and all(abs(pc - c) <= ECHO_TOL for pc, c in zip(_prec[0], (r, g, b, a)))
    if is_echo:
        r, g, b, a = _prec[0]
        h, s, v, x, exposure = _prec[1]
    else:
        memo = getattr(draw_state, '_cpx_coords', None)
        if memo is not None and memo[0] == (r, g, b):
            h, s, v, x, exposure = memo[1]
        else:
            hint = _prec[1][0] if _prec is not None else None
            h, s, v, x, exposure = hdr_color.srgb_extension_coords(r, g, b, hue_hint=hint)
            if _prec is not None:
                if s <= 0.0 or v <= 0.0:
                    h = _prec[1][0]
                if v <= 0.0:
                    s = _prec[1][1]
            draw_state._cpx_coords = ((r, g, b), (h, s, v, x, exposure))

    dl = imgui.get_window_draw_list()
    white = pack_color(1, 1, 1, 1)
    black = pack_color(0, 0, 0, 1)
    changed = False
    hsv_changed = False

    # --- the picking area: band over square + strip, the texture ---
    ax0, ay0 = imgui.get_cursor_screen_pos()          # the area's top-left
    sx0, sy0 = ax0, ay0 + BAND                        # the square's top-left
    tex = _srgb_plus_texture(gl_state, h, SQ, EXT, BAND, max_stops)
    if tex is not None:
        dl.add_image(tex.texture_id, (ax0, ay0), (ax0 + SQ + EXT, ay0 + BAND + SQ))
    seam = pack_color(*seam_color)
    dl.add_line(sx0 + SQ, ay0, sx0 + SQ, sy0 + SQ, seam, 1.0)        # sRGB | P3
    dl.add_line(ax0, sy0, ax0 + SQ + EXT, sy0, seam, 1.0)            # exposure | SDR
    imgui.invisible_button("##xsv", SQ + EXT, BAND + SQ)
    if imgui.is_item_active():
        mx, my = imgui.get_mouse_pos()
        s, x, v, exposure = _srgb_plus_pick(mx - sx0, my - sy0, SQ, EXT, BAND, max_stops)
        hsv_changed = True

    # --- hue bar: sRGB hue (the square's axis), the full height ---
    imgui.same_line(spacing=GAP)
    hx0, hy0 = imgui.get_cursor_screen_pos()
    bar_h = BAND + SQ
    for i in range(6):
        t0, t1 = i / 6.0, (i + 1) / 6.0
        c0 = pack_color(*imgui.color_convert_hsv_to_rgb(t0, 1, 1), 1)
        c1 = pack_color(*imgui.color_convert_hsv_to_rgb(t1, 1, 1), 1)
        dl.add_rect_filled_multicolor(hx0, hy0 + bar_h * t0, hx0 + BAR_W, hy0 + bar_h * t1, c0, c0, c1, c1)
    imgui.invisible_button("##xhue", BAR_W, bar_h)
    if imgui.is_item_active():
        h = min(max((imgui.get_mouse_pos()[1] - hy0) / bar_h, 0.0), 1.0)
        hsv_changed = True

    # --- markers ---
    px, py = _srgb_plus_marker(s, x, v, exposure, SQ, EXT, BAND, max_stops)
    cx, cy = sx0 + px, sy0 + py
    dl.add_circle(cx, cy, 6, black, thickness=1.0)
    dl.add_circle(cx, cy, 5, white, thickness=1.5)
    hmy = hy0 + h * bar_h
    dl.add_rect(hx0 - 1, hmy - 2, hx0 + BAR_W + 1, hmy + 2, white, thickness=1.5)

    if hsv_changed:
        r, g, b = hdr_color.extended_from_srgb_extension(h, s, v, x, exposure)
        changed = True

    # --- channel rows: extended sRGB, so they read past 1 and below 0 ---
    imgui.dummy(0, 4)
    imgui.push_item_width(SQ + EXT + GAP + BAR_W)
    out, edited = [], []
    lo, hi = -1.0, hdr_color.linear_to_srgb(2.0 ** max_stops)
    for lbl, cur in ([("R", r), ("G", g), ("B", b)] + ([("A", a)] if has_alpha else [])):
        imgui.set_next_item_width(draw_state.content_width - 30)
        if lbl == "A":
            ch, nv = imgui.drag_float(f"{lbl}##cpx_{lbl}", cur, 0.004, 0.0, 1.0, "%.3f")
        else:
            ch, nv = imgui.drag_float(f"{lbl}##cpx_{lbl}", cur, 0.006, lo, hi, "%.3f")
        if ch:
            changed = True
        edited.append(ch)
        out.append(nv if ch else cur)
        imgui.dummy(0, 1)
    imgui.pop_item_width()
    r, g, b = out[0], out[1], out[2]
    if has_alpha:
        a = out[3]

    # --- readout: hex inside sRGB, else the gamut + exposure ---
    # Remove the colour: return None (the tuple popover unsets the value).
    if button("\uf1f8", tint=(1, 0, 0, 0.5), height=21, shadow=True, use_cache=True,
              name="delete_color##", text_value=1.6)[0]:
        request_render()
        return True, None
    imgui.same_line()
    if all(0.0 <= c <= 1.0 for c in (r, g, b)):
        ri, gi, bi = (int(round(c * 255)) for c in (r, g, b))
        readout = (f"#{ri:02x}{gi:02x}{bi:02x}{int(round(a * 255)):02x}"
                   if has_alpha else f"#{ri:02x}{gi:02x}{bi:02x}")
    else:
        gamut = "sRGB" if x <= 0.0 else "P3"
        readout = f"{exposure:.2f}× white · " + (gamut if exposure <= 1.0 else gamut + " HDR")
    imgui.text_colored(readout, *Tint.subtle_text())
    if info:
        imgui.dummy(0, 2)
        imgui.text_colored(str(info), 1.0, 1.0, 1.0, 0.45)
    if changed:
        if has_alpha and not edited[3]:
            a = in_a
        if not hsv_changed:
            if not edited[0]:
                r = in_r
            if not edited[1]:
                g = in_g
            if not edited[2]:
                b = in_b
            nh, ns, nv, nx, ne = hdr_color.srgb_extension_coords(r, g, b, hue_hint=h)
            if ns <= 0.0 or nv <= 0.0:
                nh = h
            if nv <= 0.0:
                ns = s
            h, s, v, x, exposure = nh, ns, nv, nx, ne
        draw_state._cpx_precise = ((r, g, b, a), (h, s, v, x, exposure))
        request_render()
        return True, ((r, g, b, a) if has_alpha else (r, g, b))
    return False, input_value


def _srgb_plus_texture(gl_state, hue, square, ext, band, max_stops):
    """The sRGB+ picking area (band + square + strip) as an RGBA16F
    GLTexture, cached on the picker's GLState and re-baked when the hue (or
    the layout) changes."""
    if gl_state is None:
        return None
    import meltygui.hdr_color as hdr_color
    import OpenGL.GL as gl
    from meltygui.gl_state import GLTexture
    from meltygui.gl_state import _scalar
    cols, rows = int(square + ext), int(band + square)

    def create():
        data = hdr_color.srgb_plus_linear(hue, square, ext, band, max_stops)
        tex_id = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, cols, rows, 0, gl.GL_RGBA, gl.GL_FLOAT, data)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return GLTexture(tex_id, gl.GL_TEXTURE_2D, (cols, rows), gl.GL_RGBA16F)

    return gl_state.get("srgb_plus", create, lambda t: gl.glDeleteTextures([t.texture_id]),
                        deps=(round(float(hue), 4), int(square), int(ext), int(band), round(max_stops, 4)))


def _draw_srgb_picker(input_value, draw_state, info):
    """The sRGB tab: the classic HSV picker (a saturation/value square plus a
    hue bar. `input_value` is a 3- or 4-float RGB(A) tuple in 0..1; returns
    (changed, new_tuple). HSV is derived from the value each frame and the edit
    written straight back (imgui's own is_item_active tracks the drag), so it
    can be dropped anywhere; draw_tuple wraps it in Mode.POPOVER. The one piece
    of state is `draw_state._cp_precise`: the last full-precision RGBA we
    emitted plus its HSV. RGB→HSV loses hue at black/gray (h collapses to 0)
    and a driven value can echo back quantized (save/parse round trip, %.3f
    drag rounding) — so when the incoming value is just an echo of our own
    edit, we resume from the cache instead of re-deriving."""
    from meltygui.utils.glfw_utils import request_render
    from meltygui.view.control_view import button

    imgui.dummy(0, 3)
    vals = list(input_value)
    has_alpha = len(vals) >= 4
    r, g, b = float(vals[0]), float(vals[1]), float(vals[2])
    a = float(vals[3]) if has_alpha else 1.0
    # Exact incoming channels - untouched channels are emitted as these, so
    # an edit to one channel doesn't override the others' cached/rounded
    # working copies.
    in_r, in_g, in_b, in_a = r, g, b, a

    # Tolerance for "this is our own value coming back"; covers %.3f drag
    # rounding (±0.0005) and 1/255 hex quantization (±0.002).
    ECHO_TOL = 0.002
    _prec = getattr(draw_state, '_cp_precise', None)
    is_echo = False
    if _prec is not None:
        p_rgba, p_hsv = _prec  # p_rgba is always stored as a 4-tuple
        is_echo = (abs(p_rgba[0] - r) <= ECHO_TOL and
                   abs(p_rgba[1] - g) <= ECHO_TOL and
                   abs(p_rgba[2] - b) <= ECHO_TOL and
                   abs(p_rgba[3] - a) <= ECHO_TOL)

    if is_echo:
        # Echo of our own edit - resume the full-precision working value so
        # our SV/hue markers don't jump on quantization noise, and the cached
        # hue survives even when the colour is currently black/gray.
        r, g, b = p_rgba[0], p_rgba[1], p_rgba[2]
        if has_alpha:
            a = p_rgba[3]
        h, s, v = p_hsv
    else:
        h, s, v = imgui.color_convert_rgb_to_hsv(r, g, b)
        if _prec is not None:
            # External change to black/gray: hue (and at black, saturation)
            # are undefined in the new value - keep the cached ones for
            # continuity rather than snapping the markers to red/top-left.
            if s <= 0.0 or v <= 0.0:
                h = _prec[1][0]
            if v <= 0.0:
                s = _prec[1][1]

    SQ, BAR_W, GAP = 180, 18, 8
    dl = imgui.get_window_draw_list()
    white = pack_color(1, 1, 1, 1)
    black = pack_color(0, 0, 0, 1)
    trans = pack_color(0, 0, 0, 0)
    changed = False
    hsv_changed = False  # only convert HSV->RGB when the square/hue is actually changed

    # Everything renders in NATURAL imgui flow (invisible_button advances the
    # cursor, same_line for the hue bar, plain widgets below). That keeps imgui's
    # content-size tracking honest so the auto-resized popover grows to match the
    # square + channel rows + hex.

    # --- SV square: white->hue across, transparent->black down ---
    sx0, sy0 = imgui.get_cursor_screen_pos()
    hr, hg, hb = imgui.color_convert_hsv_to_rgb(h, 1.0, 1.0)
    hue = pack_color(hr, hg, hb, 1)
    dl.add_rect_filled_multicolor(sx0, sy0, sx0 + SQ, sy0 + SQ, white, hue, hue, white)
    dl.add_rect_filled_multicolor(sx0, sy0, sx0 + SQ, sy0 + SQ, trans, trans, black, black)
    imgui.invisible_button("##sv", SQ, SQ)
    if imgui.is_item_active():
        mx, my = imgui.get_mouse_pos()
        s = min(max((mx - sx0) / SQ, 0.0), 1.0)
        v = 1.0 - min(max((my - sy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- Hue bar to its right, 6 gradient segments ---
    imgui.same_line(spacing=GAP)
    hx0, hy0 = imgui.get_cursor_screen_pos()
    for i in range(6):
        t0, t1 = i / 6.0, (i + 1) / 6.0
        r0, g0, b0 = imgui.color_convert_hsv_to_rgb(t0, 1, 1)
        r1, g1, b1 = imgui.color_convert_hsv_to_rgb(t1, 1, 1)
        c0 = pack_color(r0, g0, b0, 1)
        c1 = pack_color(r1, g1, b1, 1)
        dl.add_rect_filled_multicolor(hx0, hy0 + SQ * t0, hx0 + BAR_W, hy0 + SQ * t1, c0, c0, c1, c1)
    imgui.invisible_button("##hue", BAR_W, SQ)
    if imgui.is_item_active():
        h = min(max((imgui.get_mouse_pos()[1] - hy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- Markers (after input, at the current position) ---
    cx, cy = sx0 + s * SQ, sy0 + (1.0 - v) * SQ
    dl.add_circle(cx, cy, 6, black, thickness=1.0)
    dl.add_circle(cx, cy, 5, white, thickness=1.5)
    hmy = hy0 + h * SQ
    dl.add_rect(hx0 - 1, hmy - 2, hx0 + BAR_W + 1, hmy + 2, white, thickness=1.5)

    # Fold the SV/hue edit back to RGB only when the square or hue bar was just
    # dragged. Otherwise keep the original input RGB untouched - round-tripping
    # RGB->HSV->RGB every frame accumulates conversion error and feeds it back as
    # next frame's input, which is what made the RGB sliders jitter.
    if hsv_changed:
        r, g, b = imgui.color_convert_hsv_to_rgb(h, s, v)
        changed = True

    # --- RGBA drag floats (natural flow, below the square row). Dragging a
    # channel edits RGB directly, overriding the HSV-derived value this frame. ---
    imgui.dummy(0, 4)
    imgui.push_item_width(SQ + GAP + BAR_W)
    out, edited = [], []
    for lbl, cur in ([("R", r), ("G", g), ("B", b)] + ([("A", a)] if has_alpha else [])):
        imgui.set_next_item_width(draw_state.content_width - 30)
        ch, nv = imgui.drag_float(f"{lbl}##cp_{lbl}", cur, 0.004, 0.0, 1.0, "%.3f")
        if ch:
            changed = True
        edited.append(ch)
        # Keep `cur` verbatim unless this row was actually dragged - the
        # returned value can differ from format rounding even if untouched.
        out.append(min(max(nv, 0.0), 1.0) if ch else cur)
        imgui.dummy(0, 1)
    imgui.pop_item_width()
    r, g, b = out[0], out[1], out[2]
    if has_alpha:
        a = out[3]

    # --- Hex code label (#rrggbb, +a with alpha) ---
    ri, gi, bi = (int(round(c * 255)) for c in (r, g, b))
    hex_str = (f"#{ri:02x}{gi:02x}{bi:02x}{int(round(a * 255)):02x}"
               if has_alpha else f"#{ri:02x}{gi:02x}{bi:02x}")


    if button("\uf1f8", tint=(1, 0, 0, 0.5), height=21, shadow=True, use_cache=True,
              name="delete_color##", text_value=1.6)[0]:
        request_render()
        return True, None
    imgui.same_line()
    imgui.text_colored(hex_str, *Tint.subtle_text())
    if info:
        # General-purpose caption (the tuple widget's info callback) - bottom
        # line, under the hex readout.
        imgui.dummy(0, 2)
        imgui.text_colored(str(info), 1.0, 1.0, 1.0, 0.45)
    if changed:
        # Untouched channels emit the EXACT incoming value - the working
        # copies may be truncated/rounded and must never overwrite precise
        # channels the user didn't edit. An SV/hue drag rewrites RGB
        # wholesale (that edit scope is all three channels); alpha only
        # changes when its own row was dragged.
        if has_alpha and not edited[3]:
            a = in_a
        if not hsv_changed:
            if not edited[0]:
                r = in_r
            if not edited[1]:
                g = in_g
            if not edited[2]:
                b = in_b
            # RGB drags edit the colour directly - refresh the cached HSV,
            # keeping hue/saturation where the new value leaves them undefined.
            nh, ns, nv = imgui.color_convert_rgb_to_hsv(r, g, b)
            if ns <= 0.0 or nv <= 0.0:
                nh = h
            if nv <= 0.0:
                ns = s
            h, s, v = nh, ns, nv
        draw_state._cp_precise = ((r, g, b, a), (h, s, v))
        request_render()
        return True, ((r, g, b, a) if has_alpha else (r, g, b))
    return False, input_value


def _popover_anchor(draw_state):
    """The window a header-row popover (draw_tuple_fast's picker) is nested
    under: the host itself when it IS a meltygui window, else the host's
    enclosing window. A popover parented to a plain draw_state inherits its
    `expanded` through abs_closed, so collapsing the host discarded it."""
    if draw_state.closable or draw_state.parent_window is None:
        return draw_state
    return draw_state.parent_window
