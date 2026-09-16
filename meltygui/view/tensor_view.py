"""Tensor view functions and supporting definitions."""
from meltygui.hdr_color import pack_color
from meltygui.state.tensor_state import TensorErrorState
from meltygui.model.tensor_model import TensorDim
from meltygui.model.tensor_model import TensorDims
from meltygui.core.core_render import render_func
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.view.header_view import draw_header
import math
import meltygui_imgui as imgui
from meltygui.view.header_view import flat_button
from meltygui.view.decoration_view import draw_bg


@render_func(tint=(0.23, 0.49, 0.62), use_cache=True, show_bg=False,
             with_header=None, show_header=False, shadow=False, auto_resize=True, wrap=False)
def draw_tensor_slices(input_value: tuple, slider_dims=(), dim_names=(),
                       source_shape=(), draw_state=None):
    """Edit unmapped tensor dimensions through ordinary integer controls."""
    from meltygui.view.control_view import draw_int_slider
    values = list(input_value) + [0] * max(0, len(source_shape) - len(input_value))
    changed = False
    for dimension in slider_dims:
        label = dim_names[dimension] if dimension < len(dim_names) else f"dim{dimension}"
        upper_bound = max(0, source_shape[dimension] - 1)
        current = max(0, min(int(values[dimension]), upper_bound))
        edited, value = draw_int_slider(current, name=f"{label}##slice{dimension}",
                                         min_value=0, max_value=upper_bound,
                                         width=draw_state.content_width, height=22)
        if edited and value != current:
            values[dimension] = value
            changed = True
    return changed, tuple(values) if changed else input_value


def _draw_slice_sliders(draw_state, slider_dims, dim_names, slices, source_shape, width):
    changed, value = draw_tensor_slices(slices, slider_dims=slider_dims,
                                        dim_names=dim_names, source_shape=source_shape,
                                        name="Slice positions", width=width)
    if changed:
        draw_state.locate_slices = value
    return changed, value


@render_func(tint=(0.68, 0.22, 0.20), use_cache=True, show_bg=False,
             with_header=None, show_header=False, shadow=False, auto_resize=False)
def draw_tensor_error(input_value: str, draw_state=None,
                       error_state: TensorErrorState = None, who="draw_voxels"):
    """An error card with the same footprint as a tensor or graph view."""
    import textwrap
    left, top, right, bottom = draw_state.get_content_rect()
    width, height = right - left, bottom - top
    padding = 12.0
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(left, top, left + width, top + height,
                              pack_color(0.09, 0.05, 0.05, 1.0), 6.0)
    draw_list.add_rect(left, top, left + width, top + height,
                       pack_color(0.75, 0.25, 0.25, 0.9), 6.0, thickness=1.5)
    draw_list.add_text(left + padding, top + padding,
                       pack_color(1.0, 0.55, 0.55, 1.0),
                       f"{who} can't display this tensor")
    line_height = imgui.get_font_size() + 2
    line_top = top + padding + line_height + 4
    columns = max(1, int((width - padding * 2) / max(1, imgui.calc_text_size("M")[0])))
    draw_list.push_clip_rect(left + padding, top + padding, right - padding, bottom - padding)
    for paragraph in input_value.splitlines():
        for line in textwrap.wrap(paragraph, width=columns) or ['']:
            draw_list.add_text(left + padding, line_top, pack_color(1.0, 0.85, 0.85, 1.0), line)
            line_top += line_height
    draw_list.pop_clip_rect()
    imgui.dummy(width, height)
    if error_state.last_error != input_value:
        error_state.last_error = input_value
        print(f"[{who}] {draw_state.name}: {input_value}")
    return False, input_value


def _draw_voxel_error(draw_state, message, who="draw_voxels"):
    width, height = _view_size(draw_state)
    return draw_tensor_error(message, name=f"{who} error", who=who,
                              width=width, height=height)


@render_func(is_default_for=("TensorDim", "TensorDims"), show_bg=False, is_tree=False,
             header_same_line=True, with_header=draw_header)
def draw_tensor_dim(input_value: TensorDim | TensorDims = None, draw_state=None,
                    unique=0, ui_scale=1.0, style_manager=None, **kwargs):
    """THE dim picker — one TAB per dim NAME instead of a bare number field,
    shared by every dim-typed param. A TensorDim renders single-select with
    a leading "off" tab that maps to -1 (unset: sort disabled, nf/axis dims
    derived), so sort_dim and nf_chop/nf_along reuse it as-is. A TensorDims
    renders the same tabs multi-select (mean_dims). The tabs are this view's
    own flat_buttons (`_draw_dim_tabs`), not a nested draw_tab_bar. Picking
    a dim a sibling AXIS row already holds swaps the two (`_swap_sibling_dim`,
    SWAP_DIM_KEYS). The names
    come from the sibling `dim_names` entry of the collection this row
    renders in (the params panel's locate_params proxy); with no names in
    reach it falls back to a plain int edit. Returns the SAME type it was
    given so the value keeps routing here (a plain int/tuple would drop back
    to the generic renderer next frame)."""
    from meltygui.model.tensor_model import TensorDim
    from meltygui.model.tensor_model import TensorDims
    from meltygui.model.tensor_model import _collection_dim_labels

    multi = isinstance(input_value, tuple)
    col = _row_collection(draw_state, kwargs)
    labels = _collection_dim_labels(col)
    if not labels:
        if multi:
            imgui.text(f"dims: {tuple(int(v) for v in input_value)}")
            return False, input_value
        changed, v = RenderFuncs.draw_int(
            0 if input_value is None else int(input_value),
            name=f"dim##{unique}")
        if changed and v is not None:
            return True, TensorDim(int(v))
        return False, input_value
    n = len(labels)
    if multi:
        cur = [int(v) for v in input_value if isinstance(v, int)]
        changed, selected = _draw_dim_tabs(
            draw_state, list(range(n)), labels,
            [d for d in cur if 0 <= d < n], multi=True,
            ui_scale=ui_scale, style_manager=style_manager)
        if changed:
            return True, TensorDims(sorted(int(s) for s in selected))
        return False, input_value
    # Single-select: a leading "off" tab maps to -1 (unset - sort disabled,
    # nf/axis dims derived), so unsetting doesn't rely on double-click.
    cur = int(input_value) if input_value is not None else -1
    if not (0 <= cur < n):
        cur = -1
    changed, selected = _draw_dim_tabs(
        draw_state, [-1] + list(range(n)), ["off"] + labels, [cur], multi=False,
        ui_scale=ui_scale, style_manager=style_manager)
    if changed:
        new = int(selected[0]) if selected else -1
        _swap_sibling_dim(draw_state, kwargs, cur, new)
        return True, TensorDim(new)
    return False, input_value


def _describe_tensor(t):
    """'Tensor[32, 1, 570, 4096] float16 cuda:0' — for error cards."""
    try:
        dev = getattr(t, "device", None)
        dev = f" {dev}" if dev is not None and str(dev) != "cpu" else ""
        return f"{type(t).__name__}{list(t.shape)} {str(t.dtype).replace('torch.', '')}{dev}"
    except Exception:
        return repr(t)[:80]


def _view_size(draw_state):
    """(width, height) the 3-D image occupies — shared by the render path
    and the error card so the card holds the view's footprint (no layout
    jump between a good frame and a failed one). Sized from the OWNING
    WINDOW, not this view's own content rect — a nested view's rect derives
    from what it rendered last frame (self-referential), while the window's
    height is the user-dragged size. The owning window IS the draw_state
    when draw_voxels is itself a window (closable); parent_window for a
    closable voxel grabbed an ANCESTOR that doesn't move with it."""
    win = draw_state if draw_state.closable else (draw_state.parent_window or draw_state)
    width = max(64, int(draw_state.content_width or win.content_width or 0))
    # Vertical reserve: the actual header band (0 if hidden) plus a little
    # slack for the footer/status margin (the old hardcoded 30 was the
    # 23px header + this slack).
    _reserve = int(draw_state.header_height or 0) + 7
    if draw_state.closable:
        height = max(100, draw_state.height - _reserve)
    else:
        # When in a parent's flow, draw_state.height is only trustworthy
        # when something explicit wrote it - a user drag ("initial
        # window size"), a passed height kwarg, fill_height. The auto_resize
        # measurement path ("... item_rect[1]") is what this view drew last
        # frame - sizing the image from it is a feedback loop that sustains
        # any spike forever (image = height-30 → measures back ≈ height →
        # committed again); fall back to the design height (min_height,
        # overridable per call site) for that case, and a bad committed
        # height self-heals on the next live render.
        _h_src = str(draw_state._source.get("height", ""))
        if draw_state.height and "item_rect" not in _h_src:
            height = max(100, int(draw_state.height) - _reserve)
        else:
            height = max(100, int(draw_state.min_height or 293) - _reserve)
    return width, height


def _draw_image_notice(img_pos, width, text):
    """Small wrapped caption on the image's top-left (clamp notices):
    a translucent plate under wrapped imgui text, cursor restored after."""
    x, y = img_pos
    pad = 6.0
    wrap_w = max(40.0, width - 2 * pad - 4)
    tw, th = imgui.calc_text_size(text, wrap_width=wrap_w)
    imgui.get_window_draw_list().add_rect_filled(
        x + 2, y + 2, x + min(tw, wrap_w) + 2 * pad + 2, y + th + 2 * pad + 2,
        pack_color(0.0, 0.0, 0.0, 0.6), 4.0)
    cur = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((x + pad + 2, y + pad + 2))
    imgui.push_text_wrap_pos(x + pad + 2 + wrap_w)
    imgui.push_style_color(imgui.COLOR_TEXT, 1.0, 0.8, 0.4, 1.0)
    imgui.text_wrapped(text)
    imgui.pop_style_color()
    imgui.pop_text_wrap_pos()
    imgui.set_cursor_screen_pos(cur)


def format_bytes(n):
    """Tensor byte count → 'KB' / 'MB' / 'GB' string (the old volume
    renderer's 2-dp formatting) plus its size tint: green ≤10 MB, yellow
    ≤100 MB, red above."""
    n = int(n or 0)
    if n > 1024 ** 3:
        txt = f"{n / 1024 ** 3:.2f} GB"
    elif n > 1024 ** 2:
        txt = f"{n / 1024 ** 2:.2f} MB"
    else:
        txt = f"{n / 1024:.2f} KB"
    if n > 100 * 1024 ** 2:
        tint = (1.0, 0.45, 0.4, 1.0)
    elif n > 10 * 1024 ** 2:
        tint = (1.0, 0.85, 0.35, 1.0)
    else:
        tint = (0.5, 0.9, 0.5, 1.0)
    return txt, tint


def _draw_tensor_meta(img_pos, height, t):
    """Bottom-left caption over the image: shape · dtype · device · bytes
    (size tinted by magnitude), read straight off the source tensor."""
    try:
        shape = "×".join(str(int(d)) for d in t.shape)
        dtype = str(t.dtype).replace("torch.", "")
        nbytes = int(t.numel()) * int(t.element_size())
    except Exception:
        return
    dev = str(getattr(t, "device", "cpu"))
    head = "  ".join(p for p in (shape, dtype, dev if dev != "cpu" else "") if p)
    size_txt, size_tint = format_bytes(nbytes)
    pad, gap = 5.0, 8.0
    hw, hh = imgui.calc_text_size(head)
    sw, sh = imgui.calc_text_size(size_txt)
    tw, th = hw + gap + sw, max(hh, sh)
    x, y = img_pos
    x0, y0 = x + 2, y + height - th - 2 * pad - 2
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(x0, y0, x0 + tw + 2 * pad, y0 + th + 2 * pad,
                       pack_color(0.0, 0.0, 0.0, 0.55), 4.0)
    dl.add_text(x0 + pad, y0 + pad, pack_color(0.85, 0.85, 0.85, 1.0), head)
    dl.add_text(x0 + pad + hw + gap, y0 + pad, pack_color(*size_tint), size_txt)


DIM_TAB_HEIGHT = 26


DIM_TAB_TEXT_PAD = 11


DIM_TAB_GAP = 3


DIM_TAB_BAND_PAD = 3


DIM_TAB_COLOR = (0.5, 0.5, 0.5)


SWAP_DIM_KEYS = frozenset({"x_dim", "y_dim", "z_dim", "line_dim"})


def _row_collection(draw_state, kwargs):
    """The collection this row renders in (the params panel's locate_params
    proxy) — sibling params like dim_names / x_dim live there."""
    col = kwargs.get("collection")
    if not isinstance(col, dict):
        col = getattr(draw_state, "_collection", None)
    return col if isinstance(col, dict) else None


def _draw_dim_tabs(draw_state, options, labels, selected, multi, *,
                   ui_scale=1.0, style_manager=None):
    """The dim tab strip: one flat_button per option, laid out by hand
    (wrapping at draw_state.content_width) over the same draw_bg band
    draw_tab_bar's wrapper painted (bg_offset=-3, rounding 5), with the tab
    bar's look — active = filled rect + shadow, inactive = label only, hover
    brightens — but DIM_TAB_* geometry. No nested render_func: the buttons
    paint straight into this view's draw list and claim their clicks through
    this view's draw_state.on_action, so the row costs one wrapper instead
    of two. Returns (changed, selected) with `selected` a list of option
    values; multi toggles, single replaces."""
    tab_h = DIM_TAB_HEIGHT * ui_scale
    pad = DIM_TAB_TEXT_PAD * ui_scale
    gap = DIM_TAB_GAP * ui_scale
    band_pad = DIM_TAB_BAND_PAD * ui_scale
    x0, y0 = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width if draw_state is not None else 0
    x_limit = x0 + content_width if content_width > 0 else None
    # Layout first (pure math over the label widths) so the band can be
    # painted UNDER the buttons without carrying last frame's extent on the
    # draw_state the way the tab / show_bg path does.
    rects = []
    x, y = x0 + band_pad, y0 + band_pad
    for label in labels:
        w = imgui.calc_text_size(label).x + pad
        if rects and x_limit is not None and x + w + band_pad > x_limit:
            x, y = x0 + band_pad, y + tab_h + gap
        rects.append((x, y, w))
        x += w + gap
    right = max(rx + rw for rx, _, rw in rects) + band_pad
    bottom = rects[-1][1] + tab_h + band_pad
    draw_bg(left=x0, top=y0, width=right - x0, height=bottom - y0,
            rounding=5, bg_offset=-3, depth=draw_state.depth_and_layer[0], opacity=1.0,
            selected=False, pressed=False, nested_bg=False,
            style_manager=style_manager)
    changed = False
    selected = list(selected)
    for i, (opt, label, (x, y, w)) in enumerate(zip(options, labels, rects)):
        imgui.set_cursor_screen_pos((x, y))
        active = opt in selected
        # Same params draw_tab_bar hands flat_button for an untinted tab
        # (new_value=0.15, factor=1.2): active keeps flat_button's full
        # text/saturation, inactive is alpha=0 with the muted text.
        if active:
            clicked = flat_button(label, draw_state, view_id=f"dim_tab_{i}",
                                  width=w, height=tab_h, color=DIM_TAB_COLOR,
                                  factor=1.2, tint_value=0.15 + 0.23 - 0.03)
        else:
            clicked = flat_button(label, draw_state, view_id=f"dim_tab_{i}",
                                  width=w, height=tab_h, color=DIM_TAB_COLOR,
                                  factor=1.2, alpha=0.0, tint_value=0.15,
                                  saturation=0.3, text_value=1.0)
        if clicked:
            changed = True
            if multi:
                if active:
                    selected.remove(opt)
                else:
                    selected.append(opt)
            else:
                selected = [opt]
    # Register the whole band (pads included) with the layout so the row's
    # measured extent covers it, and leave the cursor below it.
    imgui.set_cursor_screen_pos((x0, y0))
    imgui.dummy(right - x0, bottom - y0)
    return changed, selected


def _sibling_dim_keys(draw_state):
    """Keys of the OTHER draw_tensor_dim rows in the panel this row renders
    in, found by walking the render tree (the parent's _view_children index,
    where every rendered child self-registers) rather than the collection:
    what is actually rendering as a dim picker right now, whatever the
    collection stores. Read-only over the siblings — no value ever moves
    through another row's draw_state."""
    parent = draw_state._parent if draw_state is not None else None
    if parent is None or parent is draw_state:      # a root ds parents itself
        return []
    keys = []
    for sib in parent._view_children.values():
        if sib is None or sib is draw_state or sib._parent is not parent:
            continue
        # Recognize the renderer by its stable name, including rows restored
        # from older sessions or decorated through another call path.
        if getattr(sib._view_func, "__name__", None) != "draw_tensor_dim":
            continue
        key = (sib._kwargs or {}).get("key")
        if key is not None:
            keys.append(key)
    return keys


def _swap_sibling_dim(draw_state, kwargs, old, new):
    """This row just moved from `old` to `new`: if a sibling AXIS row holds
    `new`, hand it `old` so the two axes swap. The hand-off is the same
    write draw_collection performs when that row returns changed —
    `collection[key] = value` on the panel's ParamProxy (→ set_anywhere) —
    so the sibling's value takes the normal route whether or not that row
    renders this frame, and nothing is posted on its draw_state."""
    if new < 0:
        return                              # "off" can be shared freely
    key = (draw_state._kwargs or {}).get("key") if draw_state is not None else None
    if key not in SWAP_DIM_KEYS:
        return
    col = _row_collection(draw_state, kwargs)
    if col is None:
        return
    for sib_key in _sibling_dim_keys(draw_state):
        if sib_key not in SWAP_DIM_KEYS or sib_key not in col:
            continue
        v = col.get(sib_key)
        if isinstance(v, int) and not isinstance(v, bool) and int(v) == new:
            col[sib_key] = TensorDim(old)
            return


def _tick_values(lo, hi, px_per_idx, num_px, spacing=1.6):
    """Integer tick positions for the VISIBLE [lo, hi] index span of one
    edge: EVERY integer when the labels fit, else the smallest 1-2-5·10ᵏ
    step whose rotated labels keep clear of each other (footprint ≈ the
    widest label's text width along the edge, in projected PIXELS — so
    zooming in fits more ticks). `spacing` is the minimum gap between tick
    centers in widest-label widths. The span's end values always show —
    0/max on an unclipped edge, the boundary indices (a scrollbar-like
    readout of where you are along the axis) on a clipped one; interior
    step multiples stay GLOBAL multiples (they don't jitter as the clip
    end moves) and yield when they would crowd an end."""
    e0, e1 = int(math.ceil(lo - 1e-9)), int(math.floor(hi + 1e-9))
    if e1 < e0:
        return []
    if e1 == e0:
        return [e0]
    widest = max(1, len(str(e1))) * 0.62 * num_px  # ~max glyph aspect
    min_px = widest * spacing
    step, k = None, 1
    while step is None and k <= 10 ** 9:
        for s in (1, 2, 5):
            if s * k * px_per_idx >= min_px:
                step = s * k
                break
        else:
            k *= 10
    if step is None or step > e1 - e0:
        return [e0, e1]
    ticks = [e0]
    m = int(math.ceil((e0 + 0.6 * step) / step)) * step
    while m <= e1 - 0.6 * step:
        ticks.append(m)
        m += step
    ticks.append(e1)
    return ticks
