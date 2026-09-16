"""Graph view functions and supporting definitions."""
from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.shader_func import shader_func
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.model.lut_model import Lut, LutPalette
from meltygui.model.tensor_model import TensorDim
from meltygui.model.tensor_model import TensorDims
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.shaped import Shaped
from meltygui.state.graph_state import GraphViewState
from meltygui.core.runtime.toggles import SwooshMode
from meltygui.core.runtime.toggles import Toggles
from meltygui.view.header_view import draw_header
from meltygui.view.tensor_view import _tick_values
from meltygui.state.file_state import ROOT
from pathlib import Path
import OpenGL.GL as gl
import colorsys
import math
import meltygui_imgui as imgui
import numpy as np


@render_func(
    # Shape-routed: 1-D and 2-D tensors come here, 3-D+ go to draw_voxels
    # (via `Shaped("Tensor", (None, None, None, None))`). The plain "Tensor"
    # name entry on draw_voxels stays as fallback for anything unshaped.
    is_default_for=(Shaped("Tensor", (None,)), Shaped("Tensor", (None, None)),
                    Shaped("ndarray", (None,)), Shaped("ndarray", (None, None))),
    show_bg=True, selectable=True, auto_resize=False, min_width=269,
    with_header=draw_header, bg_offset=0, min_height=293,
    disable_scroll=True, use_cache=True)
def draw_line_graph(input_value=None, gl_state: GLState = None, selectable=False,
                    draw_state=None,
                    # ── camera: zoom/pan in the shader. cam_* and zoom_* names
                    # dodge the legacy DrawState zoom field (name-colling
                    # args are excluded from auto-state). ──
                    zoom_x=1.0, zoom_y=1.0, pan_x=0.0, pan_y=0.0, fit_margin=0.92,
                    # auto_scale=False (default): x and y share ONE pixel scale
                    # and ONE zoom - the plot keeps its aspect however the
                    # window is shaped or the wheel is rolled, like the voxel
                    # box. True: each axis fits the image and zooms on its own
                    # (shift/ctrl-wheel for x/y only).
                    auto_scale=False,
                    # ── line styling ──
                    line_width=1.5, line_opacity=1.0, lut=Lut("jet"),
                    single_color=(0.35, 0.75, 1.0), max_lines=1024,
                    show_axes=True,
                    # ── data mapping: dims by index or NAME; -1 = derive
                    # (x = second-to-last, lines = last). ──
                    dim_names=("layer", "batch", "token", "feature"),
                    x_dim=TensorDim(-1), line_dim=TensorDim(-1),
                    slices=(), mean_dims=TensorDims(()), normalize=False,
                    # ── events (hover-routed wrapper kwargs) ──
                    middle_mouse_drag=None, scroll_y_changed=None,
                    left_mouse_double_clicked=None, slash_pressed=None,
                    kp_divide_pressed=None, kp_decimal_pressed=None,
                    luts: LutPalette = None, keyboard_available=True, **kwargs):
    """The line-graph renderer — draw_voxels' sibling (see the module doc).
    CUDA tensors are sampled in place; CPU tensors/ndarrays are uploaded.
    Input can also be an already-packed series GLTexture (rendered as-is; needs n_samples/tex_w/y_range stamped
    on it)."""
    from meltygui.core.graphics.gl_state import GLTexture
    from meltygui.core.graphics.gl_state import gl_limits
    from meltygui.core.graphics.gl_state import texture3d_fit
    from meltygui.model.texture_model import _cached_volume_texture
    from meltygui.model.tensor_model import _clean_dim_name
    from meltygui.view.tensor_view import _describe_tensor
    from meltygui.view.tensor_view import _draw_image_notice
    from meltygui.view.tensor_view import _draw_voxel_error
    from meltygui.view.tensor_view import _draw_slice_sliders
    from meltygui.view.tensor_view import _view_size
    from meltygui.core.graphics.tensor_core import source_identity
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.model.graph_model import _finite_range
    from meltygui.model.graph_model import pack_series
    from meltygui.model.graph_model import slice_lines
    from meltygui.core.rendering.render_dispatch import draw_any

    import torch
    
    src = input_value
    cuda_lines = None
    img_origin = imgui.get_cursor_screen_pos()   # the image draws here below
    dim_names = tuple(_clean_dim_name(x, i) for i, x in enumerate(dim_names or ()))
    slices = tuple(int(v) for v in (slices or ()))
    mean_dims = tuple(int(v) for v in (mean_dims or ()))
    margin = max(0.1, min(1.0, float(fit_margin)))
    auto_scale = bool(auto_scale)

    # ── src → (lines, samples) → packed 3-D texture, parameter-driven and
    # stateless: slice_lines is a pure function of these params, the upload
    # re-runs exactly when its deps change; metadata rides the texture. ──
    if isinstance(src, GLTexture):
        tex, mapping = src, None
        source_shape = tuple(getattr(src, "source_shape", src.shape))
        n_samples = int(getattr(src, "n_samples", src.shape[2]))
        tex_w = int(getattr(src, "tex_w", src.shape[2]))
        y_range = tuple(getattr(src, "y_range", (0.0, 1.0)))
        n_lines = int(src.shape[0])
        clamp_note = getattr(src, "clamp_note", None)
    else:
        try:
            t = src if isinstance(src, torch.Tensor) else torch.from_numpy(np.asarray(src))
        except (TypeError, ValueError, RuntimeError) as e:
            _draw_voxel_error(draw_state, f"{type(src).__name__} is not tensor-shaped:\n{e}",
                              who="draw_line_graph")
            gl_state.drop("series"); gl_state.drop("series_cuda")
            return False, None
        # Cache gate BEFORE any tensor work (same fix as draw_voxels): the
        # slice / finite-range / pack are whole-tensor passes; on a hit the
        # metadata rides the cached texture exactly like a GLTexture input.
        vol_key = (source_identity(src), dim_names,
                   str(x_dim), str(line_dim), slices, mean_dims,
                   bool(normalize), int(max_lines))
        if t.is_cuda:
            from types import SimpleNamespace
            from meltygui.tensor import cuda_march as kernels, line_kernels
            if not kernels.available():
                _draw_voxel_error(draw_state, "CUDA line rendering requires meltygui[tensor]; "
                                  "the tensor was not copied to the CPU.", who="draw_line_graph")
                return False, None
            try:
                cuda_lines, mapping, source_shape = slice_lines(
                    t, dim_names, x_dim, line_dim, slices, mean_dims, False, materialize=False)
                cuda_lines = cuda_lines[:max(1, int(max_lines))]
                n_lines, n_samples = map(int, cuda_lines.shape)
                stats = gl_state.get('line_ranges', lambda: line_kernels.ranges(cuda_lines), deps=vol_key)
                if normalize:
                    y_range = (0., 1.)
                else:
                    lo, hi = float(stats[:, 0].min()), float(stats[:, 1].max())
                    if not math.isfinite(lo) or not math.isfinite(hi):
                        lo, hi = 0., 1.
                    if hi - lo < 1e-12:
                        pad = abs(lo) * .5 or .5
                        lo, hi = lo - pad, hi + pad
                    y_range = (lo, hi)
                tex_w, clamp_note = 0, None
                tex = SimpleNamespace(clamp_note=None)
                gl_state.drop('series'); gl_state.drop('series_cuda')
            except (ValueError, TypeError, RuntimeError) as error:
                _draw_voxel_error(draw_state, str(error), who="draw_line_graph")
                return False, None
        else:
            tex = _cached_volume_texture(gl_state, vol_key, keys=("series_cuda", "series"))
            if tex is not None:
                mapping, source_shape = tex.mapping, tex.source_shape
                n_samples, tex_w, y_range = tex.n_samples, tex.tex_w, tex.y_range
                n_lines, clamp_note = int(tex.shape[0]), tex.clamp_note
    if not isinstance(src, GLTexture) and tex is None:
        try:
            lines, mapping, source_shape = slice_lines(
                t, dim_names, x_dim, line_dim, slices, mean_dims, normalize)
        except (ValueError, TypeError, RuntimeError, IndexError) as e:
            _draw_voxel_error(draw_state, f"can't build lines from "
                              f"{_describe_tensor(t)}:\n{e}", who="draw_line_graph")
            gl_state.drop("series"); gl_state.drop("series_cuda")
            return False, None
        n_lines, n_samples = (int(s) for s in lines.shape)
        max_3d = int(gl_limits()["max_3d"])
        notes = []
        cap = max(1, min(int(max_lines), max_3d))
        if n_lines > cap:
            notes.append(f"{n_lines} lines — showing the first {cap} "
                         f"(max_lines={int(max_lines)}, GL depth limit {max_3d})")
            lines = lines[:cap]
            n_lines = cap
        y_range = _finite_range(lines)
        vol, tex_w = pack_series(lines, max_3d)
        clamped_shape, problems = texture3d_fit(vol.shape, vol.element_size(),
                                                max_bytes=float("inf"))
        if problems:
            # Rows past the GL limit: the sample axis longer than max_3d² -
            # show the displayable prefix of samples.
            if any("GL_MAX_3D_TEXTURE_SIZE" not in p for p in problems):
                _draw_voxel_error(draw_state, f"{_describe_tensor(t)} → series "
                                  f"{tuple(int(s) for s in vol.shape)}:\n"
                                  + "\n".join(problems), who="draw_line_graph")
                gl_state.drop("series"); gl_state.drop("series_cuda")
                return False, None
            d3, h3, w3 = clamped_shape
            vol = vol[:d3, :h3, :w3].contiguous()
            n_samples = min(n_samples, h3 * w3)
            notes.append(f"{n_samples}+ samples exceed the GL texture budget; "
                         f"showing the first {h3 * w3}")
        clamp_note = "; ".join(notes) if notes else None
        version = (source_identity(src), mapping, slices,
                   mean_dims, bool(normalize), n_lines, tuple(vol.shape))
        tex = None
        try:
            if vol.is_cuda:
                from meltygui.model.cuda_texture_model import tensor_to_texture
                tex = tensor_to_texture(gl_state, "series_cuda", vol,
                                                     version=version)
            if tex is None:
                tex = gl_state.texture3d("series", vol.cpu().numpy(), version=version)
                gl_state.drop("series_cuda")
            else:
                gl_state.drop("series")
        except Exception as e:
            _draw_voxel_error(draw_state, f"GPU upload failed for series "
                              f"{tuple(int(s) for s in vol.shape)} ({vol.dtype}):\n{e}",
                              who="draw_line_graph")
            gl_state.drop("series"); gl_state.drop("series_cuda")
            return False, None
        tex.source_shape = source_shape
        tex.source_ndim = len(source_shape)
        tex.n_samples, tex.tex_w, tex.y_range = n_samples, tex_w, y_range
        tex.clamp_note = clamp_note
        tex.mapping = mapping
        tex._vol_key = vol_key

    # ── labels: the x dim's name + the series caption ──
    x_label, caption = "", ""
    if mapping is not None:
        xd, ld = mapping
        x_label = dim_names[xd] if xd < len(dim_names) else f"dim{xd}"
        if ld is not None:
            ln = dim_names[ld] if ld < len(dim_names) else f"dim{ld}"
            caption = f"{ln} × {n_lines} lines"
    else:
        caption = f"{n_lines} lines" if n_lines > 1 else ""

    # ── slice sliders: one per UNMAPPED dim with extent > 1 (not plotted,
    # not averaged) - exactly draw_voxels' scrubbers. ──
    slider_dims = []
    if mapping is not None and len(source_shape) > 2:
        used = {d for d in mapping if d is not None}
        slider_dims = [d for d in range(len(source_shape))
                       if d not in used and d not in mean_dims
                       and source_shape[d] > 1]

    # ── size from the OWNING WINDOW (see _view_size) ──
    win = draw_state if draw_state.closable else (draw_state.parent_window or draw_state)
    width, height = _view_size(draw_state)
    if slider_dims:
        height = max(100, height - int(imgui.get_frame_height_with_spacing())
                     * len(slider_dims))

    # ── in-flight state values (deferred slow-source writes): re-read any
    # camera param with a timer set so drags accumulate off those values.
    _pending = getattr(draw_state, "_sa_pending", None) or {}
    _precise = getattr(draw_state, "_sa_precise", None) or {}
    _in_flight = _pending.keys() | _precise.keys()
    if _in_flight:
        def _fly(n, cur):
            if n not in _in_flight:
                return cur
            v = getattr(draw_state, "locate_" + n)
            return cur if v is None else v
        zoom_x, zoom_y = _fly("zoom_x", zoom_x), _fly("zoom_y", zoom_y)
        pan_x, pan_y = _fly("pan_x", pan_x), _fly("pan_y", pan_y)

    # ── gestures → draw_state params (auto-state: the write diverges the
    # param so it persists; events are hover-routed wrapper kwargs) ──
    unit = _unit_px(width, height, auto_scale)
    if not auto_scale and zoom_y != zoom_x:
        # Locked aspect: one zoom. A stray divergence (auto_scale is on,
        # or a panel edit to one of them) collapses onto zoom_x.
        zoom_y = zoom_x
        draw_state.locate_zoom_y = zoom_y
    if middle_mouse_drag is not None:
        # Pan tracks the cursor 1:1 at any zoom: a pixel is 1/z graph
        # units, and pan lives in pre-zoom graph units.
        pan_x -= middle_mouse_drag.dx / (unit[0] * max(zoom_x, 1e-6))
        pan_y += middle_mouse_drag.dy / (unit[1] * max(zoom_y, 1e-6))
        draw_state.locate_pan_x = pan_x
        draw_state.locate_pan_y = pan_y
    if scroll_y_changed is not None:
        # Zoom about the cursor: the data point under the mouse stays put -
        # (v·2-1)·m = c/z + pan = c/z' + pan'  →  pan' = pan + c/z - c/z'
        # with c the cursor in graph units. Locked aspect: both axes get one
        # zoom; auto_scale: shift = x only, ctrl = y only, plain = both.
        factor = math.exp(0.23 * scroll_y_changed.value)
        mx, my = imgui.get_mouse_pos()
        cx = (mx - img_origin[0] - width * 0.5) / unit[0]
        cy = (img_origin[1] + height * 0.5 - my) / unit[1]
        lim_x, lim_y = width * 0.5 / unit[0], height * 0.5 / unit[1]
        cx, cy = max(-lim_x, min(lim_x, cx)), max(-lim_y, min(lim_y, cy))
        do_x = (not auto_scale) or not scroll_y_changed.ctrl
        do_y = (not auto_scale) or not scroll_y_changed.shift
        if do_x:
            nz = min(1e6, max(1e-3, zoom_x * factor))
            pan_x += cx / zoom_x - cx / nz
            zoom_x = nz
            draw_state.locate_zoom_x = zoom_x
            draw_state.locate_pan_x = pan_x
        if do_y:
            nz = min(1e6, max(1e-3, zoom_y * factor))
            pan_y += cy / zoom_y - cy / nz
            zoom_y = nz
            draw_state.locate_zoom_y = zoom_y
            draw_state.locate_pan_y = pan_y
    if keyboard_available and (slash_pressed is not None
                                          or kp_divide_pressed is not None
                                          or kp_decimal_pressed is not None):
        zoom_x = zoom_y = 1.0
        pan_x = pan_y = 0.0
        draw_state.locate_zoom_x = 1.0
        draw_state.locate_zoom_y = 1.0
        draw_state.locate_pan_x = 0.0
        draw_state.locate_pan_y = 0.0

    lut_tex = luts.texture(lut)

    # ── GL pass: every resource keyed + lifecycle-managed by gl_state ──
    fb = gl_state.fbo("target", width, height)
    depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        _blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA,
                               gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        if cuda_lines is not None:
            from meltygui.model.texture_model import _upload_cuda_image
            from meltygui.view.texture_view import image_blit_pass
            from meltygui.tensor import line_kernels
            out = gl_state.get('cuda_line_image',
                lambda: torch.empty((height, width, 4), device=cuda_lines.device, dtype=torch.float16),
                deps=(height, width, str(cuda_lines.device)))
            cuda_lut = lut_tex.cuda(cuda_lines.device)
            line_kernels.render(cuda_lines, stats, out, cuda_lut, normalize=normalize,
                zoom_x=zoom_x, zoom_y=zoom_y, pan_x=pan_x, pan_y=pan_y,
                y_range=y_range, margin=margin, unit=unit, line_width=max(.5, float(line_width)),
                line_opacity=max(0., min(1., float(line_opacity))), single_color=single_color)
            image_blit_pass(gl_state, image=_upload_cuda_image(gl_state, out))
        else:
            line_pass(gl_state, series=tex, lut=lut_tex,
                      n_samples=int(n_samples), tex_w=int(tex_w), n_lines=int(n_lines),
                      zoom_x=float(zoom_x), zoom_y=float(zoom_y),
                      pan_x=float(pan_x), pan_y=float(pan_y),
                      y_min=float(y_range[0]), y_max=float(y_range[1]),
                      margin=float(margin), viewport=(float(width), float(height)),
                      unit_px=(float(unit[0]), float(unit[1])),
                      line_width=max(0.5, float(line_width)),
                      line_opacity=max(0.0, min(1.0, float(line_opacity))),
                      single_color=tuple(float(c) for c in single_color)[:3])
        if not _blend_was:
            gl.glDisable(gl.GL_BLEND)
    if depth_was_on:
        gl.glEnable(gl.GL_DEPTH_TEST)

    img_pos = imgui.get_cursor_screen_pos()
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))
    if show_axes:
        _draw_axes_overlay(imgui.get_window_draw_list(), img_pos, width, height,
                           int(n_samples), y_range, zoom_x, zoom_y, pan_x, pan_y,
                           margin, unit, x_label, caption, imgui.get_font_size())
    clamp_note = getattr(tex, "clamp_note", None)
    if clamp_note:
        _draw_image_notice(img_pos, width, clamp_note)

    _draw_slice_sliders(draw_state, slider_dims, dim_names, slices, source_shape, width)

    # ── params panel: the function's OWN params, satellite to and right
    # of the window, double-click to show/hide (draw_voxels' panel verbatim
    # - see the comments there for the mode/cursor/closed= contracts). ──
    init = "params_panel" not in draw_state.misc
    toggled = False
    if left_mouse_double_clicked is not None:
        draw_state.misc["params_panel"] = not draw_state.misc.get("params_panel", False)
        toggled = True
        draw_state.invalidate()
        request_render()
    panel_open = bool(draw_state.misc.get("params_panel", False))
    panel_kwargs = {"closed": not panel_open} if (init or toggled) else {}
    if not middle_mouse_drag and scroll_y_changed is None:
        _flow_cursor = imgui.get_cursor_screen_pos()
        _anchor_y = win.abs_top if draw_state.closable else draw_state.abs_top
        imgui.set_cursor_screen_pos((win.abs_left + (win.width or width) + 12, _anchor_y))
        changed, _, panel_ds = draw_any(draw_state.locate_params,
                                        name=f"controls##{draw_state.name}",
                                        is_tree=False,
                                        use_cache=False,
                                        show_name=False,
                                        layer_offset=7,
                                        tint=draw_state._kwargs.get("tint", None),
                                        swoosh_mode=SwooshMode.LINE,
                                        mode=Modes.WINDOW_PARAMS, show_tint=False,
                                        parent_window=win, auto_resize=True,
                                        shadow=False, return_extras=True,
                                        initial={"expanded": True},
                                        **panel_kwargs)
        imgui.set_cursor_screen_pos(_flow_cursor)
        if panel_ds is not None:
            draw_state.misc["params_panel"] = not panel_ds.closed
            if not panel_ds.closed:
                if (not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1) and not
                        imgui.is_mouse_down(0) and scroll_y_changed is None) and changed:
                    panel_ds.invalidate_up()

        if line_pass.last_error:
            imgui.text_colored(line_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)

        if changed:
            draw_state.invalidate()
            request_render()
            return changed, input_value

    return False, None


@render_func(tint=(0.42, 0.36, 0.54), auto_resize=False, selectable=False)
def render_import_graph(input_value=None, draw_state=None,
                        graph_view_state: GraphViewState = None, root=ROOT,
                        left_mouse_down=False, left_mouse_double_clicked=False,
                        middle_mouse_drag=None, scroll_y_changed=None,
                        escape_key_pressed=False, slash_key_pressed=False,
                        **kwargs):
    # ── knobs ────────────────────────────────────────────────────────────────
    # Box geometry (px at ui_scale 1): padding around the name, corner
    # radius, the gap between rows and between columns; the name's font
    # scale grows with usage (the file's importer count on the graph's log
    # scale, 0..1), so a hub's box is bigger.
    # [tint=(0.65, 0.55, 0.95)]
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.header_view import flat_button
    from meltygui.core.layout.header_runtime import _brightness_clamp_fn
    from meltygui.model.import_graph_model import start_build
    from meltygui.core.files.file_tree_core import _meta
    from meltygui.core.files.file_tree_core import _tint_of
    from meltygui.core.files.file_tree_core import open_file
    import meltygui.model.import_graph_model as file_graph

    box_pad_x = 8.0
    # [tint=(0.65, 0.55, 0.95)]
    box_pad_y = 4.0
    # [tint=(0.65, 0.55, 0.95)]
    box_radius = 6.0
    # [tint=(0.65, 0.55, 0.95)]
    row_gap = 6.0
    # [tint=(0.65, 0.55, 0.95)]
    column_gap = 60.0
    # [tint=(0.65, 0.55, 0.95)]
    usage_font_scale_max = 1.5
    # [tint=(0.65, 0.55, 0.95)]
    edge_alpha = 0.10
    # [tint=(0.65, 0.55, 0.95)]
    faded_alpha = 0.18            # everything unrelated while a node is selected
    # [tint=(0.65, 0.55, 0.95)]
    highlight_fallback_tint = (0.55, 0.72, 0.95)
    # [tint=(0.65, 0.55, 0.95)]
    importer_value_boost = 0.35
    # [tint=(0.65, 0.55, 0.95)]
    importer_saturation = 0.45
    # [tint=(0.65, 0.55, 0.95)]
    bg_theme_factor = 0.1
    # [tint=(0.65, 0.55, 0.95)]
    toolbar_height = 26.0
    # [tint=(0.65, 0.55, 0.95)]
    unpainted_node = (0.55, 0.57, 0.62)

    state = graph_view_state
    px = Melty.px
    dl = imgui.get_window_draw_list()
    cw = draw_state.content_width or (draw_state.width or 400)

    # ── toolbar: build button + status (same as the tree's) ─────────────────
    build = state._build
    if build is not None and not build.running:
        if build.result is not None:
            state._graph = build.result
        state._build = None
        draw_state.invalidate()
    if state._graph is None and file_graph.current() is not None:
        state._graph = file_graph.current()
        draw_state.invalidate()
    if flat_button("Build import graph##import_graph", draw_state,
                   view_id="import_graph_build", height=px(toolbar_height) - px(4),
                   event="left_mouse_down"):
        if state._build is None:
            state._build = start_build(Path(root))
            request_render()
    status_x, status_y = imgui.get_item_rect_max().x + px(8), imgui.get_item_rect_min().y + px(4)
    graph = state._graph
    if build is not None and build.running:
        status = "building…"
    elif build is not None and build.error:
        status = build.error
    elif graph is not None:
        status = f"{graph.file_count} files, {graph.edge_count} imports"
        if state.selected:
            status += f"  ·  {Path(state.selected).name}"
    else:
        status = "no graph yet — build one"
    dl.add_text(status_x, status_y, pack_color(0.75, 0.78, 0.82, 1.0), status)
    imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + px(4))
    x0, y0 = imgui.get_cursor_screen_pos()
    height = max(1.0, (draw_state.height or 400) - (y0 - draw_state.abs_top) - px(4))
    imgui.dummy(cw, height)
    if graph is None or not getattr(graph, "layers", None):
        return False, None

    # ── pixel layout from the graph in column/row order ───────────────────────
    # Box sizes memoized per (name, scale) on the state (underscored, not
    # saved); a column is its widest box, rows share one pitch = the
    # tallest box + row gap, so boxes never overlap.
    size_memo = state.__dict__.setdefault("_size_memo", {})
    font_size = imgui.get_font_size()

    def font_scale_of(p):
        return 1.0 + (usage_font_scale_max - 1.0) * graph.usage(p)

    def box_size(p):
        scale = font_scale_of(p)
        key = (p.name, round(scale, 3))
        size = size_memo.get(key)
        if size is None:
            text = imgui.calc_text_size(p.name)
            size = size_memo[key] = (text.x * scale + 2 * px(box_pad_x),
                                     font_size * scale + 2 * px(box_pad_y))
        return size

    sizes = {p: box_size(p) for layer in graph.layers for p in layer}
    pitch = max(h for _w, h in sizes.values()) + px(row_gap)
    boxes = {}                      # p → (x, y, w, h) in layout px (unzoomed)
    column_x = 0.0
    for layer in graph.layers:
        widest = max(sizes[p][0] for p in layer)
        total_h = len(layer) * pitch - px(row_gap)
        y = -total_h * 0.5
        for p in layer:
            w, h = sizes[p]
            boxes[p] = (column_x + (widest - w) * 0.5, y + (pitch - px(row_gap) - h) * 0.5, w, h)
            y += pitch
        column_x += widest + px(column_gap)
    total_w = column_x - px(column_gap)
    tallest = max(len(layer) for layer in graph.layers) * pitch
    fit = min(cw / max(total_w, 1.0), height / max(tallest, 1.0), 1.0)

    # ── camera ───────────────────────────────────────────────────────────────
    if slash_key_pressed:
        state.zoom, state.pan_x, state.pan_y = 1.0, 0.0, 0.0
    if middle_mouse_drag is not None:
        state.pan_x += middle_mouse_drag.dx
        state.pan_y += middle_mouse_drag.dy
    origin_x, origin_y = x0 + cw * 0.5, y0 + height * 0.5
    if scroll_y_changed is not None:
        # Zoom about the cursor: the box under the mouse stays put.
        factor = math.exp(0.23 * scroll_y_changed.value)
        new_zoom = min(50.0, max(0.1, state.zoom * factor))
        mx, my = imgui.get_mouse_pos()
        ratio = new_zoom / state.zoom
        state.pan_x = (mx - origin_x) - ((mx - origin_x) - state.pan_x) * ratio
        state.pan_y = (my - origin_y) - ((my - origin_y) - state.pan_y) * ratio
        state.zoom = new_zoom
    scale = fit * state.zoom
    centre_x = total_w * 0.5

    def to_screen(box):
        x, y, w, h = box
        return (origin_x + (x - centre_x) * scale + state.pan_x,
                origin_y + y * scale + state.pan_y, w * scale, h * scale)

    screen = {p: to_screen(box) for p, box in boxes.items()}
    meta = _meta()

    # ── selection / hover ────────────────────────────────────────────────────
    if escape_key_pressed and state.selected is not None:
        state.select(None)
        request_render()
    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered

    def node_at(pt):
        if pt is None:
            return None
        for p, (sx, sy, sw, sh) in screen.items():
            if sx <= pt[0] <= sx + sw and sy <= pt[1] <= sy + sh:
                return p
        return None

    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    if click is not None:
        state.select(node_at(click))
        request_render()
    dclick = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
              if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    if dclick is not None:
        hit = node_at(dclick)
        if hit is not None:
            open_file(hit)
    hovered = node_at((mx, my)) if hover_ok else None

    selected = state.selected_path
    if selected is not None and selected not in screen:
        selected = None
    imports = graph.imports_of(selected) if selected is not None else set()
    importers = graph.importers_of(selected) if selected is not None else set()
    related = imports | importers | ({selected} if selected is not None else set())

    # ── colours ──────────────────────────────────────────────────────────────
    style_manager = Melty.style_manager
    brightness_clamp = _brightness_clamp_fn()
    fill_memo = {}

    def node_fill(tint, alpha):
        """The tab bar's mix of the file tint (same knobs as the tree rows)."""
        key = (tint, alpha)
        col = fill_memo.get(key)
        if col is None:
            mixed = style_manager.make_color_rgb(
                tint[0], tint[1], tint[2],
                value=Toggles.CodeEditor.tab_active_bg_brightness,
                factor=bg_theme_factor,
                saturation_scale=Toggles.CodeEditor.tab_active_bg_saturation,
                alpha=1.0)
            mixed = brightness_clamp(mixed[0], mixed[1], mixed[2], 0.0,
                                     Toggles.CodeEditor.tab_active_bg_max_brightness)
            col = fill_memo[key] = pack_color(mixed[0], mixed[1], mixed[2], alpha)
        return col

    selected_tint = (_tint_of(meta, selected) if selected is not None else None) or highlight_fallback_tint
    hue, saturation, value = colorsys.rgb_to_hsv(*selected_tint)
    importers_tint = colorsys.hsv_to_rgb(hue, saturation * importer_saturation,
                                         min(1.0, value + importer_value_boost))
    imports_col = pack_color(*selected_tint, 0.9)
    importers_col = pack_color(*importers_tint, 0.9)
    edge_col = pack_color(0.8, 0.85, 0.95, edge_alpha)
    faded_edge_col = pack_color(0.8, 0.85, 0.95, edge_alpha * faded_alpha)
    text_col = pack_color(0.92, 0.92, 0.92, 1.0)
    faded_text_col = pack_color(0.92, 0.92, 0.92, faded_alpha)
    outline_col = pack_color(1.0, 1.0, 1.0, 0.85)

    # ── edges (under the boxes): importer → imported, right edge → left edge
    # when the line runs left → right (the layered case), centre → centre
    # for a cycle's back edge ────────────────────────────────────────────────
    clip = getattr(draw_state, "abs_clip_rect", None)

    def visible(box):
        sx, sy, sw, sh = box
        return clip is None or (sx + sw >= clip[0] and sx <= clip[2] and sy + sh >= clip[1] and sy <= clip[3])

    def anchors(box_a, box_b):
        ax, ay, aw, ah = box_a
        bx, by, bw, bh = box_b
        if ax + aw <= bx:
            return (ax + aw, ay + ah * 0.5), (bx, by + bh * 0.5)
        return (ax + aw * 0.5, ay + ah * 0.5), (bx + bw * 0.5, by + bh * 0.5)

    for importer, targets in graph.imports.items():
        box_a = screen.get(importer)
        if box_a is None:
            continue
        for imported in targets:
            box_b = screen.get(imported)
            if box_b is None or not (visible(box_a) or visible(box_b)):
                continue
            a, b = anchors(box_a, box_b)
            if selected is None:
                col, thickness = edge_col, 1.0
            elif importer == selected:
                col, thickness = imports_col, 1.5          # what the selection imports
            elif imported == selected:
                col, thickness = importers_col, 1.5        # who imports the selection
            else:
                col, thickness = faded_edge_col, 1.0
            dl.add_line(a[0], a[1], b[0], b[1], col, thickness)

    # ── boxes: rounded rect in the file's tint, the name inside ──────────────
    radius = px(box_radius) * scale
    for p, (sx, sy, sw, sh) in screen.items():
        if not visible((sx, sy, sw, sh)):
            continue
        tint = _tint_of(meta, p) or unpainted_node
        faded = selected is not None and p not in related
        alpha = faded_alpha if faded else 1.0
        dl.add_rect_filled(sx, sy, sx + sw, sy + sh, node_fill(tint, alpha), rounding=radius)
        if p == selected:
            dl.add_rect(sx - px(1), sy - px(1), sx + sw + px(1), sy + sh + px(1), outline_col, rounding=radius, thickness=2.0)
        elif p in importers:
            dl.add_rect(sx, sy, sx + sw, sy + sh, importers_col, rounding=radius, thickness=1.5)
        elif p in imports:
            dl.add_rect(sx, sy, sx + sw, sy + sh, imports_col, rounding=radius, thickness=1.5)
        elif p == hovered:
            dl.add_rect(sx, sy, sx + sw, sy + sh, outline_col, rounding=radius, thickness=1.0)
        # The name at the box's own font scale × the camera scale (window
        # font scale retargets the entire list's font size; reset right after).
        text_scale = font_scale_of(p) * scale
        if text_scale * font_size >= 4.0:            # unreadable below this: skip
            imgui.set_window_font_scale(text_scale)
            dl.add_text(sx + px(box_pad_x) * scale, sy + px(box_pad_y) * scale,
                        faded_text_col if faded else text_col, p.name)
            imgui.set_window_font_scale(1.0)

    return False, None

def _unit_px(width, height, auto_scale):
    """Pixels per graph unit per axis — the one place the scaling policy
    lives (the shader's unit_px). auto_scale stretches the fitted graph to
    the image; otherwise both axes share min(w, h)/2 so the plot keeps its
    aspect whatever the window's shape (draw_voxels never distorts its box
    either)."""
    if auto_scale:
        return width * 0.5, height * 0.5
    u = min(width, height) * 0.5
    return u, u


def _graph_to_norm(g, zoom, pan, margin):
    """graph units (the shader's g) → normalized data coordinate [0, 1]."""
    return ((g / zoom + pan) / margin + 1.0) * 0.5


def _norm_to_px(v, zoom, pan, margin, px, unit, flip=False):
    """normalized data coordinate → pixel offset inside the image (one axis)."""
    g = ((v * 2.0 - 1.0) * margin - pan) * zoom
    return px * 0.5 - g * unit if flip else px * 0.5 + g * unit


def _nice_step(span, target_ticks):
    """1-2-5·10ᵏ step giving about `target_ticks` over `span`."""
    if span <= 0 or target_ticks <= 0:
        return 1.0
    raw = span / target_ticks
    k = 10.0 ** math.floor(math.log10(raw))
    for s in (1.0, 2.0, 5.0, 10.0):
        if s * k >= raw:
            return s * k
    return 10.0 * k


def _fmt_value(v, step):
    if step >= 1.0:
        return f"{v:.0f}"
    decimals = min(9, max(0, int(math.ceil(-math.log10(step)))))
    return f"{v:.{decimals}f}"


def _draw_axes_overlay(draw_list, img_pos, width, height, n_samples, y_range,
                       zoom_x, zoom_y, pan_x, pan_y, margin, unit, x_label,
                       caption, font_px):
    """2-D axis furniture over the image: faint grid, sample-index ticks
    along the bottom, value ticks along the left, the x dim's name, and a
    caption (series dim × line count) top-right. Pure imgui draw-list
    text, recomputed per frame from the camera params."""
    x0, y0 = img_pos
    grid_col = pack_color(1.0, 1.0, 1.0, 0.07)
    tick_col = pack_color(0.85, 0.85, 0.85, 0.8)
    dim_col = pack_color(0.85, 0.85, 0.85, 0.55)
    # x: visible index span from the inverse camera at the image edges.
    ux, uy = unit
    if n_samples > 1:
        # visible index span: the image edges are g = ±(half-extent / unit)
        gx = width * 0.5 / ux
        lo = _graph_to_norm(-gx, zoom_x, pan_x, margin) * (n_samples - 1)
        hi = _graph_to_norm(gx, zoom_x, pan_x, margin) * (n_samples - 1)
        lo, hi = max(0.0, lo), min(float(n_samples - 1), hi)
        px_per_idx = 2.0 * ux * zoom_x * margin / (n_samples - 1)
        for i in _tick_values(lo, hi, px_per_idx, font_px):
            px = x0 + _norm_to_px(i / (n_samples - 1), zoom_x, pan_x, margin, width, ux)
            draw_list.add_line(px, y0, px, y0 + height, grid_col)
            label = str(i)
            tw, th = imgui.calc_text_size(label)
            draw_list.add_text(px - tw * 0.5, y0 + height - th - 2, tick_col, label)
    # y: value ticks at a nice step over the visible value span.
    ymin, ymax = y_range
    gy = height * 0.5 / uy
    v_lo = ymin + _graph_to_norm(-gy, zoom_y, pan_y, margin) * (ymax - ymin)
    v_hi = ymin + _graph_to_norm(gy, zoom_y, pan_y, margin) * (ymax - ymin)
    step = _nice_step(v_hi - v_lo, max(2, height / 70.0))
    v = math.ceil(v_lo / step) * step
    guard = 0
    while v <= v_hi and guard < 200:
        guard += 1
        vn = (v - ymin) / (ymax - ymin)
        py = y0 + _norm_to_px(vn, zoom_y, pan_y, margin, height, uy, flip=True)
        draw_list.add_line(x0, py, x0 + width, py, grid_col)
        label = _fmt_value(v, step)
        tw, th = imgui.calc_text_size(label)
        draw_list.add_text(x0 + 4, py - th * 0.5, tick_col, label)
        v += step
    if x_label:
        tw, th = imgui.calc_text_size(x_label)
        draw_list.add_text(x0 + width - tw - 6, y0 + height - th - 2, dim_col, x_label)
    if caption:
        tw, th = imgui.calc_text_size(caption)
        draw_list.add_text(x0 + width - tw - 6, y0 + 4, dim_col, caption)


LINE_VERT = """
#version 330 core
uniform sampler3D series;   // texel (i % tex_w, i / tex_w, line) = sample i of line
out float v_edge;            // signed pixel distance from the line's centerline
flat out int v_line;

// data (sample index, value) → pixel. Zoom/pan ARE the camera: a fitted
// graph spans [-margin, margin] graph units at zoom 1, pan shifts in that
// space, and unit_px (pixels per graph unit, per axis) places it in the
// image — equal components keep the plot's aspect (the default), the
// image's half-extents stretch it to fill (auto_scale).
vec2 toPix(int i, float y) {
    float xn = n_samples > 1 ? float(i) / float(n_samples - 1) : 0.5;
    float yn = (y - y_min) / max(y_max - y_min, 1e-30);
    vec2 g = vec2(((xn * 2.0 - 1.0) * margin - pan_x) * zoom_x,
                  ((yn * 2.0 - 1.0) * margin - pan_y) * zoom_y);
    return viewport * 0.5 + g * unit_px;
}

float sampleAt(int i, int line) {
    return texelFetch(series, ivec3(i % tex_w, i / tex_w, line), 0).r;
}

void main() {
    int seg = gl_VertexID / 6;
    int corner = gl_VertexID % 6;
    int line = gl_InstanceID;
    v_line = line;
    float y0 = sampleAt(seg, line);
    float y1 = sampleAt(seg + 1, line);
    if (isnan(y0) || isnan(y1) || isinf(y0) || isinf(y1)) {
        // A gap in the data: park the vertex outside the clip volume.
        v_edge = 0.0;
        gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
        return;
    }
    vec2 s0 = toPix(seg, y0);
    vec2 s1 = toPix(seg + 1, y1);
    vec2 dir = s1 - s0;
    float len = length(dir);
    vec2 ext = len > 1e-6 ? dir / len : vec2(1.0, 0.0);
    vec2 nrm = vec2(-ext.y, ext.x);
    float hw = line_width * 0.5 + 1.0;       // +1px skirt for the AA ramp
    // two triangles: (s0,-)(s1,-)(s1,+) and (s0,-)(s1,+)(s0,+); the ends
    // extend by hw along the segment so consecutive segments overlap at
    // joins instead of leaving wedge gaps on sharp turns.
    bool at1 = (corner == 1 || corner == 2 || corner == 4);
    float side = (corner == 2 || corner == 4 || corner == 5) ? 1.0 : -1.0;
    vec2 p = (at1 ? s1 + ext * hw : s0 - ext * hw) + nrm * side * hw;
    v_edge = side * hw;
    gl_Position = vec4(p / viewport * 2.0 - 1.0, 0.0, 1.0);
}
"""


LINE_FRAG = """
#version 330 core
in float v_edge;
flat in int v_line;
out vec4 FragColor;
uniform sampler1D lut;

void main() {
    float d = abs(v_edge);
    float a = 1.0 - smoothstep(line_width * 0.5 - 0.5, line_width * 0.5 + 0.5, d);
    a *= line_opacity;
    vec3 c = n_lines > 1
        ? texture(lut, (float(v_line) + 0.5) / float(n_lines)).rgb
        : single_color;
    c = pow(max(c, 0.0), vec3(2.2));   // LUT / tint are display-referred sRGB; the FBO is linear (hdr_color.py)
    FragColor = vec4(c * a, a);     // premultiplied — the FBO composites ONE / 1-a
}
"""


@shader_func(fragment=LINE_FRAG, vertex=LINE_VERT)
def line_pass(gl_state: GLState = None, series=None, lut=None, n_samples=2,
              tex_w=1, n_lines=1, zoom_x=1.0, zoom_y=1.0, pan_x=0.0, pan_y=0.0,
              y_min=0.0, y_max=1.0, margin=0.92, viewport=(1.0, 1.0),
              unit_px=(0.5, 0.5), line_width=1.5, line_opacity=1.0, single_color=(0.35, 0.75, 1.0),
              **kwargs):
    # Program bound, uniforms set. Attribute-less instanced draw: the vertex
    # shader computes every segment quad from gl_VertexID / gl_InstanceID and
    # the series texture - 6 vertices per segment, one instance per line.
    if n_samples < 2 or n_lines < 1:
        return
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 6 * (int(n_samples) - 1), int(n_lines))
