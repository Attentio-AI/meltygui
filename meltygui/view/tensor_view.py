"""Tensor view functions and supporting definitions."""
from meltygui.core.graphics.tensor_core import _voxels_cleanup
from meltygui.hdr_color import pack_color
from meltygui.core.graphics.gl_state import GLState
from meltygui.state.tensor_state import TensorErrorState
from meltygui.model.lut_model import Lut, LutPalette
from meltygui.model.tensor_model import TensorDim
from meltygui.model.tensor_model import TensorDims
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.rendering.shaped import Shaped
from meltygui.core.runtime.toggles import SwooshMode
from meltygui.core.runtime.toggles import Toggles
from meltygui.view.header_view import draw_header
import OpenGL.GL as gl
import math
import meltygui_imgui as imgui
import numpy as np
import ctypes
from meltygui.core.styling.fonts import Font
from meltygui.core.layout.header_runtime import flat_button
from meltygui.core.rendering.render_dispatch import draw_bg
from meltygui.core.graphics.text_texture import bake_texts
from meltygui.model.camera_model import basis as _cam_basis


from meltygui.view.lut_view import draw_lut


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


@render_func(is_default_for=("GLTexture",
                             # 3-D+ tensors by shape; 1-D/2-D route to
                             # draw_line_graph. The bare "Tensor" name stays
                             # as the fallback for 0-D / anything unmatched
                             # (the error card is the right place for those).
                             Shaped("Tensor", (None, None, None, ...)),
                             Shaped("ndarray", (None, None, None, ...)),
                             "Tensor", "ndarray"),
             show_bg=True, selectable=True,
             auto_resize=False, min_width=269, with_header=draw_header,
             bg_offset=0, min_height=293, disable_scroll=True, use_cache=True,
             on_cleanup=_voxels_cleanup)
def draw_voxels(input_value=None, gl_state: GLState = None, selectable=False,
                draw_state=None,
                # ── camera + shading: cam_* names dodge the legacy DrawState
                # zoom/brightness/contrast fields (name-colliding params are
                # excluded from auto-state). Gestures/panel write
                # draw_state.<name>; diverged values persist. ──
                tilt=0.283, spin=0.724, roll=0.0, cam_zoom=3.4,
                # [tint=(0.084, 0.472, 0.148, 1.0)]
                pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
                cam_brightness=1.332, cam_contrast=1.0,
                # density = the old densityScale (haze gain over the opacity
                # gate); threshold = the old opacityThreshold (higher → lower
                # gate → more opaque)
                density=0.7, threshold=0.297, centered=False,
                nearest=True, lut=Lut("jet"), step_size=0.0005, max_steps=4096,
                # ── shadow catcher: the invisible plane the box rests on -
                # it renders nothing but the volume's cast shadow (one-sided:
                # no shadow from below). shadow_opacity scales how dark the
                # caught shadow composites; shadow_softness scales the
                # screen-space penumbra blur (radius grows with occluder
                # height) - 0 = hard edge, bigger = wider penumbra. ──
                draw_plane=True, shadow_opacity=1.0, shadow_softness=0.15,
                # ── lighting: draw_shading lights the floor (per-s., the
                # raymarched cast shadow) and the volume (gradient-normal
                # Lambert); self_shading adds the per-sample transmittance
                # march inside the volume - the expensive tier. ambient_light
                # is the shadow floor: how much light survives everywhere.
                # shading_strength scales how much the volume's cast normal
                # may darken its LUT color (0 = shading off, plane still
                # catches). ──
                draw_shading=True, self_shading=True,
                light_pos=(50.0, -50.0, 200.0), light_tint=(1.0, 1.0, 1.0),
                light_brightness=1.622, ambient_light=0.3, shading_strength=0.7,
                # ── axis mapping: dims by index OR NAME. The first three dims
                # by default; None still means "derive" (last three → z/y/x)
                # for anything that clears one. ──
                dim_names=("layer", "batch", "token", "feature"),
                x_dim=TensorDim(0), y_dim=TensorDim(2), z_dim=TensorDim(2),
                slices=(),
                mean_dims=TensorDims(()), sort_dim=TensorDim(-1),
                normalize=False, nf_on=False, nf_chop=TensorDim(-1),
                nf_along=TensorDim(-1), nf_chunk=128,
                # ── experimental: raymarch IN CUDA on the value's own GPU,
                # reading the tensor's memory through its strides (no volume
                # copy, no 3-D texture, any device); only the 2-D image
                # crosses to the display GPU. Colour march is nearest-only;
                # shading taps (normals, light marches, floor shadow) read a
                # trilinear field like the GL version (see cuda_march.py). ──
                cuda_march=True,
                # ── volume furniture (screen px) ──
                name_size=17.0, name_padding=30.1, name_opacity=1.1,
                num_size=17.1, num_padding=5.5, num_opacity=0.8,
                num_spacing=1.0, num_angle=0.0, z_offset=1,
                middle_mouse_drag=None, double_right_mouse_drag=None,
                scroll_y_changed=None, space_mouse_changed=None,
                left_mouse_double_clicked=None,
                kp_7_pressed=None, kp_1_pressed=None, kp_3_pressed=None,
                kp_5_pressed=None, slash_pressed=None, kp_divide_pressed=None,
                kp_decimal_pressed=None, font_manager=None,
                luts: LutPalette = None, **kwargs):
    """The voxel renderer — owner of every render and mapping decision.
    Input is a tensor/ndarray (sliced + uploaded HERE, re-keyed by gl_state
    deps on source identity/_version/mapping) or an already-uploaded
    GLTexture (rendered as-is). Tensor METADATA — full shape, dim count —
    rides the uploaded buffer; EVERYTHING else is a parameter on this
    signature (auto draw_state params: gestures and the controls panel
    write draw_state.<name>, only diverged values persist/serialize)."""
    from meltygui.core.graphics.gl_state import GLTexture
    from meltygui.core.graphics.gl_state import gl_limits
    from meltygui.core.graphics.gl_state import texture3d_fit
    from meltygui.model.camera_model import apply_space_mouse
    from meltygui.model.tensor_model import CudaVolumeView
    from meltygui.tensor.voxel_playground import HALF_PI
    from meltygui.model.tensor_model import _AXIS_POS
    from meltygui.tensor.voxel_playground import _CUDA_LAST_ERROR
    from meltygui.tensor.voxel_playground import _cached_volume_texture
    from meltygui.model.tensor_model import _clean_dim_name
    from meltygui.tensor.voxel_playground import _cuda_march_ready
    from meltygui.tensor.voxel_playground import _cuda_render
    from meltygui.model.tensor_model import _resolve_dim
    from meltygui.model.tensor_model import _volume_scale
    from meltygui.model.tensor_model import auto_neural_flow
    from meltygui.tensor.voxel_playground import image_blit_pass
    from meltygui.model.tensor_model import slice_volume
    from meltygui.model.tensor_model import slice_volume_view
    from meltygui.tensor.voxel_playground import source_identity
    from meltygui.tensor.voxel_playground import voxel_pass
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.rendering.render_dispatch import draw_any
    import meltygui.tensor.voxel_playground

    src = input_value
    if src is None:
        # Between a live value being released (a new run's first publish
        # drops the previous generation) and the new data arriving, the
        # window renders with no value. Hold the LAST image - the FBO
        # survives that release - and keep the slice sliders up (their
        # geometry comes from the metadata still on the cached volume
        # entry) instead of flashing an error card; a window that never
        # rendered shows nothing.
        fb = gl_state.peek("target")
        if fb is not None:
            dim_names = tuple(_clean_dim_name(x, i) for i, x in enumerate(dim_names or ()))
            slices = tuple(int(v) for v in (slices or ()))
            mean_dims = tuple(int(v) for v in (mean_dims or ()))
            meta = None
            for key in ("volume_cuda", "volume", "cuda_view"):
                rec = gl_state.peek(key)
                if rec is not None:
                    meta = getattr(rec, "texture", rec)
                    break
            source_shape = tuple(getattr(meta, "source_shape", ()) or ())
            mapping = getattr(meta, "mapping", None)
            # the marker's dim_names merge skips a None value; use the names
            # the last valid frame stamped
            dim_names = tuple(getattr(meta, "dim_names", None) or dim_names)
            slider_dims = []
            if mapping is not None and len(source_shape) > 3:
                slider_dims = [d for d in range(len(source_shape))
                               if d not in mapping and d not in mean_dims
                               and source_shape[d] > 1]
            width, height = _view_size(draw_state)
            if slider_dims:
                height = max(100, height - int(imgui.get_frame_height_with_spacing())
                             * len(slider_dims))
            img_pos = imgui.get_cursor_screen_pos()
            imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))
            # the 2-D axis outline is drawn over the image each frame (not
            # rendered into the FBO) - recompute it from the last volume's
            # extents + the current camera so it doesn't blink either
            shape = tuple(getattr(meta, "shape", ()) or ())
            if len(shape) == 3:
                try:
                    edges = _axis_edges(tilt, spin, cam_zoom, width / height, width, height,
                                        scale=_volume_scale(shape),
                                        pan=(pan_x, pan_y, pan_z), ortho=ortho, roll=roll)
                    if edges:
                        _draw_axis_lines(imgui.get_window_draw_list(), img_pos, edges)
                except Exception:
                    pass
            _draw_slice_sliders(draw_state, slider_dims, dim_names, slices, source_shape, width)
        return False, None
    # A 1-D texture is a LUT, not a volume - don't try to raymarch it.
    if getattr(src, "target", None) == int(gl.GL_TEXTURE_1D):
        imgui.text(f"{src!r} — a LUT, not a volume")
        return False, None

    dim_names = tuple(_clean_dim_name(x, i) for i, x in enumerate(dim_names or ()))
    slices = tuple(int(v) for v in (slices or ()))
    mean_dims = tuple(int(v) for v in (mean_dims or ()))

    # ── source → display volume → GPU, parameter-driven and stateless:
    # slice_volume is a pure function of the params, the upload re-runs
    # exactly when its deps change, and tensor metadata rides the buffer.
    if isinstance(src, GLTexture):
        tex, mapping = src, None
        source_shape = tuple(getattr(src, "source_shape", src.shape))
    else:
        import torch
        try:
            t = src if isinstance(src, torch.Tensor) else torch.from_numpy(np.asarray(src))
        except (TypeError, ValueError, RuntimeError) as e:
            _draw_voxel_error(draw_state, f"{type(src).__name__} is not tensor-shaped:\n{e}")
            gl_state.drop("volume"); gl_state.drop("volume_cuda")
            return False, None
        # ── preflight: slicing + upload can fail on BAD DATA (odd dtypes,
        # empty tensors) or on the DRIVER (an extent past
        # GL_MAX_3D_TEXTURE_SIZE, a volume larger than VRAM). Decide here,
        # before any GL interaction: over-limit extents CLAMP to the
        # displayable prefix (with a notice over the image), anything
        # unrecoverable shows an error card in place of the 3-D view. The
        # error path also flushes stale textures so a bad frame never
        # keeps a previous volume alive under the message. ─────────────
        # ── auto neural flow: if nf OFF, a displayed axis longer than the
        # readability cap (Toggles.Voxels.auto_flow_extent) or the GL limit
        # is wrapped HERE - effective nf_* locals for this render only (the
        # user's params stay untouched; the labels below use the effective
        # values and show e.g. "vocab % 180" or "batch - vocab"). ───────────
        nf_pad = False
        # The CUDA path needs pycuda + a CUDA tensor; anything else (CPU
        # tensors, no pycuda) silently takes the GL path.
        # A CUDA input must stay in its allocation. Never silently switch to
        # the texture-upload path (which copies/reformats the whole tensor).
        use_cuda = bool(getattr(t, "is_cuda", False))
        if use_cuda and not _cuda_march_ready():
            _draw_voxel_error(draw_state, "CUDA tensor rendering requires meltygui[tensor]; "
                              "the tensor was not copied to the CPU.")
            return False, None
        if not nf_on:
            _cap = int(Toggles.Voxels.auto_flow_extent)
            if use_cuda:
                # no 3-D texture → no GL extent limit; only the readability cap
                _cap = _cap if _cap > 0 else 0
            else:
                _gl_max = int(gl_limits()["max_3d"])
                _cap = min(_cap, _gl_max) if _cap > 0 else _gl_max
            auto = auto_neural_flow(t.shape, dim_names, x_dim, y_dim, z_dim, _cap)
            if auto is not None:
                nf_on, nf_pad = True, True
                nf_chop, nf_along, nf_chunk = (TensorDim(auto[0]),
                                               TensorDim(auto[1]), auto[2])
        # ── cache gate BEFORE any tensor work: slice_volume is pure but not
        # free - on a multi-GB tensor its type coercion / permute-contiguous
        # / neural-flow repack / normalize min-max are whole-tensor GPU
        # passes, and running them every frame (the upload was already
        # version-gated, the slice that feeds it was not) pinned an orbit
        # at ~18 fps with the raymarcher all the way down. The key is
        # the params that decide the volume (the effective nf_* after
        # auto-flow); the volume-derived facts (mapping, source_shape,
        # clamp_note) ride the cached texture. ─────────────────────────
        vol_key = (source_identity(src), dim_names,
                   str(x_dim), str(y_dim), str(z_dim), slices, mean_dims,
                   int(sort_dim), bool(normalize), bool(nf_on), str(nf_chop),
                   str(nf_along), int(nf_chunk), nf_pad, use_cuda)
        tex = _cached_volume_texture(gl_state, vol_key)
        if tex is not None:
            mapping, source_shape = tex.mapping, tex.source_shape
            vol = None
    if not isinstance(src, GLTexture) and tex is None and use_cuda:
        try:
            cv = slice_volume_view(
                t, dim_names, x_dim, y_dim, z_dim, slices, mean_dims,
                sort_dim, normalize, nf_on, nf_chop, nf_along, nf_chunk,
                nf_pad=nf_pad)
        except (ValueError, TypeError, RuntimeError, IndexError) as e:
            _draw_voxel_error(draw_state, f"can't build a volume view from "
                              f"{_describe_tensor(t)}:\n{e}")
            gl_state.drop("cuda_view")
            return False, None
        cv._vol_key = vol_key
        cv.dim_names = dim_names
        # The view is the cached "volume": gl_state holds it by the same key
        # discipline as the textures (no GL - nothing to delete). The GL
        # volume (possibly GBs of display-GPU VRAM) is released on the
        # switch, and vice versa below.
        gl_state.drop("cuda_view")
        gl_state.get("cuda_view", lambda: cv, deps=vol_key)
        gl_state.drop("volume"); gl_state.drop("volume_cuda")
        tex, mapping, source_shape = cv, cv.mapping, cv.source_shape
    if not isinstance(src, GLTexture) and tex is None:
        gl_state.drop("cuda_view")
        try:
            vol, mapping, source_shape = slice_volume(
                t, dim_names, x_dim, y_dim, z_dim, slices, mean_dims,
                sort_dim, normalize, nf_on, nf_chop, nf_along, nf_chunk,
                nf_pad=nf_pad)
        except (ValueError, TypeError, RuntimeError, IndexError) as e:
            _draw_voxel_error(draw_state, f"can't build a volume from "
                              f"{_describe_tensor(t)}:\n{e}")
            gl_state.drop("volume"); gl_state.drop("volume_cuda")
            return False, None
        # Extents only here (max_bytes=inf): the VRAM budget is judged by
        # the upload on a cache MISS (texture3d / tensor_to_texture create),
        # since it's measured against FREE memory and a cached volume's GPU
        # allocation must not fail its next frame. That refusal surfaces
        # as the "GPU upload failed" card below.
        clamped_shape, problems = texture3d_fit(vol.shape, vol.element_size(),
                                                max_bytes=float("inf"))
        if any("GL_MAX_3D_TEXTURE_SIZE" not in p for p in problems):
            _draw_voxel_error(draw_state, f"{_describe_tensor(t)} → volume "
                              f"{tuple(int(s) for s in vol.shape)}:\n"
                              + "\n".join(problems))
            gl_state.drop("volume"); gl_state.drop("volume_cuda")
            return False, None
        clamp_note = None
        if problems:
            # Display the leading max-size block of each over-limit axis.
            d3, h3, w3 = clamped_shape
            vol = vol[:d3, :h3, :w3].contiguous()
            clamp_note = "clamped: " + "; ".join(problems)
        version = vol_key + (mapping, clamped_shape)
        try:
            if vol.is_cuda:
                import meltygui.tensor.cuda_interop as cuda_interop
                tex = cuda_interop.tensor_to_texture(gl_state, "volume_cuda", vol,
                                                     version=version)
            if tex is None:
                tex = gl_state.texture3d("volume", vol.cpu().numpy(), version=version)
                gl_state.drop("volume_cuda")
            else:
                gl_state.drop("volume")
        except Exception as e:
            # The driver refused something the pre-flight didn't predict
            # (out of memory, unsupported format...). Show it, don't crash
            # the render loop; the deps still ensure we retry only when the
            # source or mapping change.
            _draw_voxel_error(draw_state, f"GPU upload failed for volume "
                              f"{tuple(int(s) for s in vol.shape)} "
                              f"({vol.dtype}):\n{e}")
            gl_state.drop("volume"); gl_state.drop("volume_cuda")
            return False, None
        tex.source_shape = source_shape       # tensor metadata on the buffer
        tex.source_ndim = len(source_shape)
        tex.clamp_note = clamp_note
        tex.mapping = mapping
        tex.dim_names = dim_names
        tex._vol_key = vol_key                # the gate above uses this
        vol = None                            # don't hold the temp volume

    # Edge labels - the mapped dim's name (+ neural-flow decoration) and its
    # DISPLAYED size, recomputed per frame from the params.
    if mapping is not None:
        zd, yd, xd = mapping
        n = len(source_shape)
        chop_d = _resolve_dim(dim_names, nf_chop, n)
        along_d = _resolve_dim(dim_names, nf_along, n)
        chop_d = xd if chop_d is None else chop_d
        along_d = zd if along_d is None else along_d
        display = []
        for axis, dim in (("x", xd), ("y", yd), ("z", zd)):
            label = dim_names[dim] if dim < len(dim_names) else f"dim{dim}"
            if nf_on and dim == chop_d:
                label = f"{label} % {int(nf_chunk)}"   # chopped into chunks
            elif nf_on and dim == along_d:
                chop_name = (dim_names[chop_d]
                             if chop_d < len(dim_names) else f"dim{chop_d}")
                label = f"{label} · {chop_name}"        # along the blocks
            display.append((label, int(tex.shape[_AXIS_POS[axis]])))
        axis_display = tuple(display)
    else:
        d3, h3, w3 = (int(s) for s in tex.shape)
        axis_display = ((dim_names[2] if len(dim_names) > 2 else "x", w3),
                        (dim_names[1] if len(dim_names) > 1 else "y", h3),
                        (dim_names[0] if dim_names else "z", d3))

    # ── slice sliders: >3-dim tensors get one slider per UNMAPPED dim (not
    # displayed, not averaged, extent > 1) along the bottom of the view to
    # choose which slice is pinned. GLTexture inputs arrive pre-sliced
    # (mapping is None) - nothing to scrub. ─────────────────────────────
    slider_dims = []
    if mapping is not None and len(source_shape) > 3:
        slider_dims = [d for d in range(len(source_shape))
                       if d not in mapping and d not in mean_dims
                       and source_shape[d] > 1]

    # Size from the OWNING WINDOW, not this view's own draw() - a nested
    # view's height derives from what it rendered last frame (self-referential),
    # while the window's height is the user-dragged size. Reserve room for the
    # header + a line below the image.
    # The owning window IS this draw_state when draw_voxels is itself a window
    # (mode=WINDOW / closable); only fall back to the enclosing window for the
    # non-window child case. Using draw_state.parent_window for a closable voxel
    # grabbed an ANCESTOR (e.g. live_view_forward) that doesn't move with the
    # voxel window, so the controls panel - parented to `win` below - followed
    # the ancestor and stayed put while the voxel window was dragged.
    win = draw_state if draw_state.closable else (draw_state.parent_window or draw_state)
    width, height = _view_size(draw_state)
    if slider_dims:
        # The sliders live INSIDE the view's box - give them their rows by
        # shrinking the image, not by growing past the window.
        height = max(100, height - int(imgui.get_frame_height_with_spacing())
                     * len(slider_dims))

    # ── in-flight locate values: a locate_* write to a SLOW source (e.g. a
    # `# [cam_brightness=...]` comment) is deferred during drags and lands
    # multi-frame after; until then the injected kwarg is stale. Re-read any
    # camera param with a value set through locate_* (which also clears the
    # entry once the trip lands) so drags accumulate off the latest state.
    # _sa_precise rides the same way: a low-precision source has a 4dp
    # rounding, so locate_* serves the full-precision overlay over it. ──
    _pending = getattr(draw_state, "_sa_pending", None) or {}
    _precise = getattr(draw_state, "_sa_precise", None) or {}
    _in_flight = _pending.keys() | _precise.keys()
    if _in_flight:
        def _fly(n, cur):
            if n not in _in_flight:
                return cur
            v = getattr(draw_state, "locate_" + n)
            return cur if v is None else v
        tilt, spin, cam_zoom = _fly("tilt", tilt), _fly("spin", spin), _fly("cam_zoom", cam_zoom)
        roll = _fly("roll", roll)
        pan_x, pan_y, pan_z = _fly("pan_x", pan_x), _fly("pan_y", pan_y), _fly("pan_z", pan_z)
        cam_brightness = _fly("cam_brightness", cam_brightness)
        cam_contrast = _fly("cam_contrast", cam_contrast)
        ortho = _fly("ortho", ortho)
        # EVERY display-affecting panel param rides the same gate, not just
        # the camera: a panel edit whose driving source is slow (an override
        # comment's parse->copy->hotkey trip) otherwise renders the STALE
        # injected kwarg until the trip lands - "changes not always
        # reflected", especially visible on the cuda path where transfer params
        # also key the mip/floor bakes.
        density, threshold = _fly("density", density), _fly("threshold", threshold)
        step_size, max_steps = _fly("step_size", step_size), _fly("max_steps", max_steps)
        nearest, centered = _fly("nearest", nearest), _fly("centered", centered)
        draw_plane = _fly("draw_plane", draw_plane)
        shadow_opacity = _fly("shadow_opacity", shadow_opacity)
        shadow_softness = _fly("shadow_softness", shadow_softness)
        draw_shading = _fly("draw_shading", draw_shading)
        self_shading = _fly("self_shading", self_shading)
        light_pos, light_tint = _fly("light_pos", light_pos), _fly("light_tint", light_tint)
        light_brightness = _fly("light_brightness", light_brightness)
        ambient_light = _fly("ambient_light", ambient_light)
        shading_strength = _fly("shading_strength", shading_strength)
        # # (Data-shaping params - slices/dims/nf/scale/normalize - are consumed
        # # ABOVE this gate and they can't be re-read here, but the pump below
        # # re-renders until the trip lands and the new tex_key rebuilds.)
        # # And keep re-rendering until the slow write LANDS (locate_* clears
        # # the entry): the landing itself doesn't invalidate this frame, so
        # # without the pump the final value never rebuilds.
        # draw_state.invalidate()
        # request_render()

    # ── gestures → draw_state params (auto-state: the caller diverges the
    # param so it persists; events are hover-routed wrapper kwargs) ──────
    if middle_mouse_drag is not None:
        # Ortable chirality, latched per GESTURE: upside down (cos(tilt)<0,
        # world-up pointing down the screen) a rightward move must spin the
        # other way to keep tracking the cursor. Latching at drag start keeps
        # the direction stable when a drag tilts across the pole mid-gesture;
        # the latch clears on release so the next drag re-reads orientation.
        spin_sign = getattr(draw_state, "_orbit_spin_sign", None)
        if spin_sign is None:
            spin_sign = -1.0 if math.cos(tilt) < 0.0 else 1.0
            draw_state._orbit_spin_sign = spin_sign
        if middle_mouse_drag.shift:
            # Blender-style shift-d = pan: move the orbit target so the
            # content tracks the cursor 1:1 at the target plane (world units
            # per pixel at current cam_zoom, focal 1.7 - matches the ray gen).
            wpp = 2.0 * cam_zoom / (1.7 * height)
            st, ct = math.sin(tilt), math.cos(tilt)
            cs, ss = math.cos(spin), math.sin(spin)
            dx, dy = middle_mouse_drag.dx, middle_mouse_drag.dy
            pan_x += (ss * dx - cs * st * dy) * wpp
            pan_y += (-cs * dx - ss * st * dy) * wpp
            pan_z += ct * dy * wpp
            draw_state.locate_pan_x = pan_x
            draw_state.locate_pan_y = pan_y
            draw_state.locate_pan_z = pan_z
        elif middle_mouse_drag.ctrl:
            # the old viewer's ctrl-drag: vertical = dolly zoom, horizontal
            # still orbits.
            cam_zoom = min(137.6, max(0.0, cam_zoom * math.exp(0.005 * middle_mouse_drag.dy)))
            spin -= middle_mouse_drag.dx * 0.008 * spin_sign
            draw_state.locate_cam_zoom = cam_zoom
            draw_state.locate_spin = spin
        elif Toggles.Voxels.mouse_navigation == "trackball":
            # Mouse drag as a rotation vector in VIEW space (dy pitches about
            # screen-right, dx yaws about screen-up), through the same geometry
            # as the 3D mouse's trackball - roll is the third angle.
            tilt, spin, roll, _, _ = apply_space_mouse(
                (0.0, 0.0, 0.0, middle_mouse_drag.dy * 0.008,
                 middle_mouse_drag.dx * 0.008, 0.0),
                tilt, spin, roll, cam_zoom, (pan_x, pan_y, pan_z),
                navigation="trackball", orbit_sensitivity=1.0,
                pan_sensitivity=0.0, zoom_sensitivity=0.0)
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
            draw_state.locate_roll = roll
        else:
            spin -= middle_mouse_drag.dx * 0.008 * spin_sign
            # Tilt is UNRESTRICTED - orbit straight over the poles and keep
            # going. remainder() re-wraps into [-pi, pi] (same orientation,
            # cos/sin-continuous) so the stored angle never runs away.
            tilt = math.remainder(tilt + middle_mouse_drag.dy * 0.008, math.tau)
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
    else:
        draw_state._orbit_spin_sign = None
    if double_right_mouse_drag is not None:
        # the old viewer's shading drag: now on a DOUBLE right-drag (the 2nd
        # press of a double right-click, held and dragged): horizontal =
        # brightness, vertical = contrast (up to increase). The plain right-
        # click stays reserved for the context menu.
        cam_brightness = min(4.0, max(0.0, cam_brightness + double_right_mouse_drag.dx * 0.01))
        cam_contrast = min(5.0, max(0.01, cam_contrast - double_right_mouse_drag.dy * 0.008))
        draw_state.locate_cam_brightness = cam_brightness
        draw_state.locate_cam_contrast = cam_contrast
    if scroll_y_changed is not None:
        cam_zoom = min(135.5, max(0.0, cam_zoom * math.exp(-0.23 * scroll_y_changed.value)))
        draw_state.locate_cam_zoom = cam_zoom
    if space_mouse_changed is not None and space_mouse_changed.axes:
        # 3D mouse (events/space_mouse.py → InputHandler.feed()): the six
        # axes integrated over the frame. The mapping is voxel_camera's -
        # turntable writes tilt / spin straight from the puck's pitch / yaw,
        # trackball rotates the basis in view space and decomposes it back
        # (roll is the third angle); pan tracks the screen plane scaled by
        # the camera distance, push / pull is an e-fold dolly. Sensitivities
        # and the mode are Toggles.SpaceMouse. Axes arrive in OBJECT terms
        # (Blender's handiness folded in by space_mouse.normalize()) so this
        # is the same math as the mouse.
        tilt, spin, roll, cam_zoom, (pan_x, pan_y, pan_z) = apply_space_mouse(
            space_mouse_changed.axes, tilt, spin, roll, cam_zoom, (pan_x, pan_y, pan_z),
            navigation=Toggles.SpaceMouse.navigation,
            orbit_sensitivity=float(Toggles.SpaceMouse.orbit_sensitivity),
            pan_sensitivity=float(Toggles.SpaceMouse.pan_sensitivity),
            zoom_sensitivity=float(Toggles.SpaceMouse.zoom_sensitivity),
            pivot=Toggles.SpaceMouse.pivot)
        draw_state.locate_tilt = tilt
        draw_state.locate_spin = spin
        draw_state.locate_roll = roll
        draw_state.locate_cam_zoom = cam_zoom
        draw_state.locate_pan_x = pan_x
        draw_state.locate_pan_y = pan_y
        draw_state.locate_pan_z = pan_z

    # ── Blender-style numpad views (hover-routed key events): 7/1/3 = top/
    # front/right, ctrl = the opposite side, 5 = ortho toggle, / (either
    # slash, or numpad . like the old viewer) = recenter the pan on the
    # origin. A focused text editor owns the keyboard, so keys are ignored
    # while one is active. ────────────────────────────────────────────────
    from meltygui.core.melty import Melty
    if Melty.text_focused_ds is None:
        if kp_7_pressed is not None:
            spin, tilt = -HALF_PI, (-HALF_PI if kp_7_pressed.ctrl else HALF_PI)
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_1_pressed is not None:
            spin, tilt = (HALF_PI if kp_1_pressed.ctrl else -HALF_PI), 0.0
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_3_pressed is not None:
            spin, tilt = (math.pi if kp_3_pressed.ctrl else 0.0), 0.0
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_5_pressed is not None:
            ortho = not ortho
            draw_state.locate_ortho = ortho
        if (slash_pressed is not None or kp_divide_pressed is not None
                or kp_decimal_pressed is not None):
            pan_x = pan_y = pan_z = 0.0
            draw_state.locate_pan_x = 0.0
            draw_state.locate_pan_y = 0.0
            draw_state.locate_pan_z = 0.0
            # ...and level the horizon (a trackball session's habit).
            roll = 0.0
            draw_state.locate_roll = 0.0

    # ── plane side latch: when the view is upside-down the target plane
    # belongs on the box's OTHER face (the floor light stays fixed in world
    # space; only the catcher's marches see a mirrored box so the flipped
    # floor still catches a shadow). Same idea as _orbit_spin_sign: the side
    # only re-reads orientation while NO drag is active - mid-drag the floor
    # holds put, and the flip lands when you let go.
    if middle_mouse_drag is None:
        draw_state._plane_side = -1.0 if math.cos(tilt) < 0.0 else 1.0
    plane_side = getattr(draw_state, "_plane_side", None) or (
        -1.0 if math.cos(tilt) < 0.0 else 1.0)

    # Filtering is sampler state on the texture, view-owned, applied per frame.
    filt = gl.GL_NEAREST if nearest else gl.GL_LINEAR
    if isinstance(tex, GLTexture):
        gl.glBindTexture(tex.target, tex.texture_id)
        gl.glTexParameteri(tex.target, gl.GL_TEXTURE_MIN_FILTER, filt)
        gl.glTexParameteri(tex.target, gl.GL_TEXTURE_MAG_FILTER, filt)
        gl.glBindTexture(tex.target, 0)

    volume_scale = _volume_scale(tex.shape)

    lut_tex = luts.texture(lut)

    # ── axis coordinate positions: visible silhouette spans via the Python
    # mirror of the shader camera, computed BEFORE the GL pass - the label
    # billboards render INTO the voxel FBO with the volume's own camera ────
    axis_edges = None
    if axis_display:
        axis_edges = _axis_edges(tilt, spin, cam_zoom,
                                 width / height, width, height,
                                 scale=volume_scale,
                                 pan=(pan_x, pan_y, pan_z),
                                 ortho=ortho, roll=roll)

    # A dragged step size can cross zero (the number token has no floor). A
    # non-positive step walks the GL path BACKWARDS out of the box on its
    # first step, so every slice shows only its entry voxel - a flat plane
    # cut along the data's edges (the cuda path only uses it as a sampling
    # stride and shrugged it off, the "GL path looks broken" ticket on
    # 09-08). Floor it here, once, for both paths.
    step_size = max(float(step_size), 1e-5)

    # ── GL pass: every resource tracked + lifecycle-managed by gl_state ──
    fb = gl_state.fbo("target", width, height)
    depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        # Labels FIRST, so the volume pass composites OVER them - the floor
        # shadow (and the floor itself) darkens the labels beneath it
        # instead of the labels floating on top of the shadow.
        if axis_edges and (name_size > 0 or num_size > 0):
            # Labels as in-scene textured quads. A bake/render hiccup should
            # not take down the view (or trigger the hotswap auto-revert) -
            # log it and keep rendering the volume.
            try:
                specs = _billboard_specs(axis_edges, axis_display,
                                         volume_scale, name_size, name_padding,
                                         name_opacity, num_size, num_padding,
                                         num_opacity, num_spacing, num_angle)
                cam = {"tilt": tilt, "spin": spin, "roll": roll, "zoom": cam_zoom,
                       "pan_x": pan_x, "pan_y": pan_y, "pan_z": pan_z,
                       "ortho": ortho, "aspect": width / height}
                _render_label_billboards(
                    gl_state, specs, cam, height,
                    font=font_manager.get(Font.JETBRAINS_MONO_30) if font_manager else None)
            except Exception as e:
                if not meltygui.tensor.voxel_playground._LABEL_WARNED:
                    meltygui.tensor.voxel_playground._LABEL_WARNED = True
                    import traceback
                    print(f"label billboards disabled: {e}")
                    traceback.print_exc()
        # The volume shader outputs PREMULTIPLIED alpha (the shader
        # composites with (1-a) weights), so it layers over the labels with
        # ONE / ONE_MINUS_SRC_ALPHA - over label-free (transparent) pixels
        # this is bit-identical to the old unblended write.
        _blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA,
                               gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        # int() so a UI-dragged float never flips the uniform's inferred
        # GLSL type (the loop bound must be an int).
        if isinstance(tex, CudaVolumeView):
            # The CUDA kernel already produced the premultiplied image (on
            # the tensor's GPU, hopped to a display-GPU RGBA16F texture);
            # blit it into the FBO under the same blend state so labels,
            # outline and the rest of the view are untouched.
            import meltygui.tensor.cuda_march as _cm
            img_tex = _cuda_render(
                gl_state, tex, width, height, lut=lut, tilt=tilt, spin=spin,
                lut_texture=lut_tex,
                roll=roll, zoom=cam_zoom, pan=(pan_x, pan_y, pan_z), ortho=bool(ortho),
                volume_scale=volume_scale, step_size=float(step_size),
                max_steps=int(max_steps), density=float(density),
                threshold=float(threshold), brightness=float(cam_brightness),
                contrast=float(cam_contrast), gamma=float(Toggles.Voxels.gamma),
                centered=bool(centered),
                shade=_cm.shade_params(
                    draw_plane=bool(draw_plane), shadow_opacity=float(shadow_opacity),
                    shadow_softness=float(shadow_softness),
                    shadow_tint=tuple(float(c) for c in Toggles.Voxels.floor_shadow_color)[:3],
                    plane_side=float(plane_side), draw_shading=bool(draw_shading),
                    self_shading=bool(self_shading),
                    light_pos=tuple(float(c) for c in light_pos),
                    light_tint=tuple(float(c) for c in light_tint),
                    light_brightness=float(light_brightness),
                    ambient_light=float(ambient_light),
                    shading_strength=float(shading_strength)))
            if img_tex is not None:
                image_blit_pass(gl_state, image=img_tex)
        else:
            voxel_pass(gl_state, volume=tex, volume_lin=tex, lut=lut_tex,
                       aspect=width / height,
                       volume_scale=volume_scale, step_size=step_size,
                       max_steps=int(max_steps), density=density,
                       threshold=threshold, tilt=tilt, spin=spin, roll=roll, zoom=cam_zoom,
                       pan_x=pan_x, pan_y=pan_y, pan_z=pan_z, ortho=ortho,
                       brightness=cam_brightness, contrast=cam_contrast,
                       gamma=float(Toggles.Voxels.gamma), centered=centered,
                       draw_plane=bool(draw_plane),
                       shadow_opacity=float(shadow_opacity),
                       shadow_softness=float(shadow_softness),
                       # The floor shadow's own colour (neutral grey), not
                       # the UI's blue-black compositor Toggles.floor_color.
                       shadow_tint=tuple(float(c) for c in Toggles.Voxels.floor_shadow_color)[:3],
                       draw_shading=bool(draw_shading),
                       self_shading=bool(self_shading),
                       plane_side=float(plane_side),
                       light_pos=tuple(float(c) for c in light_pos),
                       light_tint=tuple(float(c) for c in light_tint),
                       light_brightness=float(light_brightness),
                       ambient_light=float(ambient_light),
                       shading_strength=float(shading_strength))
        if not _blend_was:
            gl.glDisable(gl.GL_BLEND)
    if depth_was_on:
        gl.glEnable(gl.GL_DEPTH_TEST)

    img_pos = imgui.get_cursor_screen_pos()
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))
    clamp_note = getattr(tex, "clamp_note", None)
    if clamp_note:
        # The volume on screen is a PREFIX of the tensor; say so on the image
        # (top-left, wrapped to the image width) rather than silently
        # showing a truncated tensor.
        _draw_image_notice(img_pos, width, clamp_note)
    if not isinstance(src, GLTexture):
        _draw_tensor_meta(img_pos, height, t)

    # ── the outline stays 2-D imgui (crisp 1px outline over the volume) ────
    if axis_edges:
        _draw_axis_lines(imgui.get_window_draw_list(), img_pos, axis_edges)

    # ── the slice sliders, one slider per unmapped dim under the volume. An
    # edit writes the full-length slices tuple to draw_state (auto-state:
    # it diverges the param, persists, and resets slice_volume's version so
    # the volume re-slices + re-uploads on the next frame). ──────────────
    _draw_slice_sliders(draw_state, slider_dims, dim_names, slices, source_shape, width)

    # ── ALL controls live in a satellite panel opening to the RIGHT of
    # the window: the renderer's full params, rendered automatically -
    # draw_state.locate_params is a live dict over this signature, each row
    # reads its framework-resolved value and an edit goes through
    # set_anywhere (draw_state by default; a higher-pri source like an
    # annotation comment claims the write when it drives the param).
    # POPOVER window_pos is relative to the CURSOR at the call,
    # so anchor at the window's right edge - the panel rides along if the
    # window is dragged. Double-click the volume to show/hide; `closed` is
    # only PASSED on init/toggle so the window's own X button works - the
    # framework owns the state between toggles and we mirror it back (a
    # forced closed= every call reopened the panel on the next pre-render,
    # which is why the X appeared dead). ─────────────────────────────────
    init = "params_panel" not in draw_state.misc
    toggled = False
    if left_mouse_double_clicked is not None:
        draw_state.misc["params_panel"] = not draw_state.misc.get("params_panel", False)
        toggled = True
        draw_state.invalidate()
        request_render()
    panel_open = bool(draw_state.misc.get("params_panel", False))
    # Anchor the panel at the window's RIGHT edge (+12px gap). Set every frame
    # so left_offset/top_offset track the right edge and the panel rides along
    # when the window is dragged; the panel's own drag accumulates into
    # window_pos on top of that, so it stays draggable.
    # Save/restore the flow cursor around the panel jump; nested in a scroll
    # view, `win` is the ENCLOSING window, so this teleports the cursor far
    # from this view's box - left unrestored, poisons the parent rect and
    # the group measure (views popped in with a huge height, then the -30
    # self-reference shrank them back 29px a frame).
    panel_kwargs = {"closed": not panel_open} if (init or toggled) else {}
    if (not middle_mouse_drag and not double_right_mouse_drag and scroll_y_changed is None
            and space_mouse_changed is None):
        _flow_cursor = imgui.get_cursor_screen_pos()
        # Anchor y: the enclosing window's top for a window voxel, this ROW's
        # top for a nested one. The panel call emits an inline item at the
        # cursor, and that item is committed into THIS view's group - an
        # anchor at win.abs_top made a nested view's rect span from the row to
        # the window top, so committed heights scaled with scroll distance
        # (the scrollbar jitter as rows crossed the viewport).
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
            # The panel is cached and must NOT invalidate per drag frame - it
            # rides its blit while a camera gesture writes the params, then
            # catches up ONCE at the gesture edge.
            if not panel_ds.closed:
                if (not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1) and not
                imgui.is_mouse_down(0) and scroll_y_changed is None
                        and space_mouse_changed is None) and changed:
                    panel_ds.invalidate_up()

        # ── status bar error surfacing only ────────────────────────────────────
        if voxel_pass.last_error:
            # imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
            imgui.text_colored(voxel_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)
        if isinstance(tex, CudaVolumeView) and _CUDA_LAST_ERROR:
            imgui.text_colored(_CUDA_LAST_ERROR.splitlines()[0], 1.0, 0.45, 0.40, 1.0)

        if changed:
            # UP, not self: this view is often nested (a live-value window,
            # a collection row) and its pixels are baked into ancestor blit
            # tiles - a self-only invalidate left the ancestor serving the
            # stale image, so panel edits "didn't take" until something else
            # repushed the ancestor.
            draw_state.invalidate_up()
            request_render()
            return changed, input_value

    return False, None


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


LABEL_VERT = """
#version 330 core
uniform float tilt, spin, roll, zoom, aspect;
uniform bool ortho;
uniform vec3 pan;
layout(location = 0) in vec3 a_anchor;   // world point ON the edge
layout(location = 1) in vec3 a_u;        // baseline dir (flipped for reading)
layout(location = 2) in vec3 a_v;        // text-up dir
layout(location = 3) in vec3 a_out;      // unflipped outward dir (placement)
layout(location = 4) in vec4 a_metrics;  // half_w, half_h, offs (NDC), alpha
layout(location = 5) in vec4 a_uvrect;   // u0, v0(bottom), u1, v1(top)
out vec2 uv;
out float v_alpha;
void main() {
    // two-triangle quad from gl_VertexID: corners in {-1,+1}²
    int id = gl_VertexID;
    vec2 q = vec2((id == 1 || id == 2 || id == 4) ? 1.0 : -1.0,
                  (id == 2 || id == 4 || id == 5) ? 1.0 : -1.0);
    uv = vec2(mix(a_uvrect.x, a_uvrect.z, q.x * 0.5 + 0.5),
              mix(a_uvrect.y, a_uvrect.w, q.y * 0.5 + 0.5));
    v_alpha = a_metrics.w;
    float ct = cos(tilt);
    vec3 fwd = -vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt));
    vec3 right0 = vec3(-sin(spin), cos(spin), 0.0);
    // roll turns right toward up about the view axis (0 = level horizon,
    // the turntable); the 3D mouse's trackball mode is what writes it.
    vec3 right = right0 * cos(roll) + cross(right0, fwd) * sin(roll);
    vec3 up = cross(right, fwd);
    vec3 eye = pan - fwd * zoom;
    // Screen-constant sizing: metrics arrive in NDC units. The world length
    // that projects to one NDC unit at the ANCHOR's depth is depth/1.7
    // (zoom/1.7 in ortho), so the label keeps its pixel size at any zoom
    // while still anchoring to and foreshortening with the scene.
    float ws = (ortho ? zoom : max(0.05, dot(a_anchor - eye, fwd))) / 1.7;
    vec3 world = a_anchor + (a_out * a_metrics.z
               + a_u * (a_metrics.x * q.x) + a_v * (a_metrics.y * q.y)) * ws;
    vec3 d = world - eye;
    // The voxel ray gen, inverted (same math as _axis_edges): perspective
    // keeps the depth in w for the divide, ortho is a plain scale.
    if (ortho) {
        float s = zoom / 1.7;
        gl_Position = vec4(dot(d, right) / (s * aspect), dot(d, up) / s, 0.0, 1.0);
    } else {
        gl_Position = vec4(1.7 * dot(d, right) / aspect, 1.7 * dot(d, up),
                           0.0, dot(d, fwd));
    }
}
"""


LABEL_FRAG = """
#version 330 core
uniform sampler2D label;
in vec2 uv;
in float v_alpha;
out vec4 FragColor;
void main() {
    vec4 t = texture(label, uv);
    FragColor = vec4(t.rgb, t.a * v_alpha);
}
"""


_LABEL_UNIFORMS = ("tilt", "spin", "roll", "zoom", "aspect", "ortho", "pan", "label")


_LABEL_FLOATS = 20  # 4×vec3 + 2×vec4 per instance


DIM_TAB_HEIGHT = 26


DIM_TAB_TEXT_PAD = 11


DIM_TAB_GAP = 3


DIM_TAB_BAND_PAD = 3


DIM_TAB_COLOR = (0.5, 0.5, 0.5)


SWAP_DIM_KEYS = frozenset({"x_dim", "y_dim", "z_dim", "line_dim"})


_AXIS_NEAR = 0.05


_EDGE_SHORTEN_PX = 14.0


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


def _label_program(gl_state):
    """The instanced label program + its uniform-location map, compiled once
    per GLState (re-created when the GLSL source changes, e.g. on hotswap)."""

    def create():
        def compile_one(kind, source):
            s = gl.glCreateShader(kind)
            gl.glShaderSource(s, source)
            gl.glCompileShader(s)
            if gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
                raise RuntimeError(gl.glGetShaderInfoLog(s).decode(errors="replace"))
            return s

        vs = compile_one(gl.GL_VERTEX_SHADER, LABEL_VERT)
        fs = compile_one(gl.GL_FRAGMENT_SHADER, LABEL_FRAG)
        prog = gl.glCreateProgram()
        gl.glAttachShader(prog, vs)
        gl.glAttachShader(prog, fs)
        gl.glLinkProgram(prog)
        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)
        if gl.glGetProgramiv(prog, gl.GL_LINK_STATUS) != gl.GL_TRUE:
            raise RuntimeError(gl.glGetProgramInfoLog(prog).decode(errors="replace"))
        loc = {n: gl.glGetUniformLocation(prog, n) for n in _LABEL_UNIFORMS}
        return prog, loc

    def delete(value):
        gl.glDeleteProgram(value[0])

    return gl_state.get("label_prog", create, delete,
                        deps=(hash(LABEL_VERT), hash(LABEL_FRAG)))


def _label_vao(gl_state):
    """(vao, vbo): one interleaved per-instance buffer (divisor 1 on every
    attribute — the quad corners come from gl_VertexID, no vertex attribs)."""

    def create():
        vao = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        stride = _LABEL_FLOATS * 4
        offset = 0
        for slot, n in ((0, 3), (1, 3), (2, 3), (3, 3), (4, 4), (5, 4)):
            gl.glEnableVertexAttribArray(slot)
            gl.glVertexAttribPointer(slot, n, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glVertexAttribDivisor(slot, 1)
            offset += n * 4
        gl.glBindVertexArray(0)
        return vao, vbo

    def delete(value):
        vao, vbo = value
        gl.glDeleteBuffers(1, [vbo])
        gl.glDeleteVertexArrays(1, [vao])

    return gl_state.get("label_vao", create, delete)


def _label_atlas(gl_state, texts, font=None):
    """The strip atlas for this view's label strings, cached until the
    string SET changes (tick sets only change at zoom thresholds, so
    re-bakes are rare). `texts` must be a sorted tuple."""

    def create():
        return bake_texts(texts, font=font)

    def delete(value):
        gl.glDeleteTextures([value[0].texture_id])

    return gl_state.get("label_atlas", create, delete, deps=(texts, id(font)))


def _axis_edges(tilt, spin, zoom, aspect, width, height,
                scale=(1.0, 1.0, 1.0), pan=(0.0, 0.0, 0.0), ortho=False, roll=0.0):
    """The volume box's silhouette edges, each clipped to its VISIBLE span —
    the Python mirror of the shader's orbit camera (extents = `scale`, the
    voxel-count-proportional volume_scale), so lines and labels land exactly
    on the rendered edges.

    Face visibility is decided in WORLD space: front-facing iff the eye is
    outside the face's plane (ortho: iff the view direction looks against
    its normal) — never from projected corner geometry. The old test used
    the projected quad's shoelace area against an absolute px² threshold and
    needed all four corners in front of the near plane; on a wide-skinny
    volume (a (1, 96, 4096) slab is a 1.0 × 0.023 × 0.0002 box) any zoom that
    makes the data readable puts the camera INSIDE the box's long span, the
    near corners fell to the behind-camera cutoff, and every face and edge
    touching them vanished — the axis hid exactly when you zoomed in to
    read it, and orbiting changed which corners died.

    An edge is on the silhouette iff exactly one adjacent face is front-
    facing (edge-on faces count as back-facing, so the camera-facing square
    contributes all four sides in an exact top view); eye inside the box —
    no face front-facing — keeps all 12, so the box stays outlined and
    labeled from the inside. Each edge then clips against the near plane in
    camera space and the image rect in screen space (screen params map back
    through the perspective-correct 1/z interpolation), so a partially-
    behind or partially-offscreen axis keeps its on-screen portion.

    Returns [(a, b, pa, pb, t0, t1, z0, z1)]: the ±1 corner sign tuples, the
    screen endpoints of the visible span, its world-param range over a→b
    (exactly 0.0 / 1.0 when that end is the true corner), and the camera
    depths at the visible ends (equal under ortho) for perspective-correct
    tick placement downstream."""
    # The shader camera's basis (voxel_camera.basis), in numpy.
    fwd, right, up = (np.array(v, np.float64) for v in _cam_basis(tilt, spin, roll))
    eye = np.asarray(pan, np.float64) - fwd * zoom
    sc = np.asarray(scale, np.float64)
    # Inverse of the shader's ray gen (rd ∝ fwd*1.7 + right*ndc.x + up*ndc.y,
    # ndc.x pre-scaled by aspect): ndc = 1.7 * cam_xy / cam_z, x /= aspect.
    # Ortho divides by the fixed frame half-size (zoom/1.7) instead of the
    # point's own depth.
    ortho_denom = max(zoom, 1e-6) / 1.7

    def to_screen(cx, cy, cz):
        denom = ortho_denom if ortho else cz / 1.7
        ndx = (cx / denom) / aspect
        ndy = cy / denom
        return ((ndx * 0.5 + 0.5) * width, (1.0 - (ndy * 0.5 + 0.5)) * height)

    def face_visible(k, s):
        # The box is centered on the ORIGIN (pan is the camera target).
        return (-s * fwd[k] > 1e-12) if ortho else (s * eye[k] > sc[k])

    vis = {(k, s): face_visible(k, s) for k in range(3) for s in (-1, 1)}
    any_vis = any(vis.values())

    def clip(a, b):
        # World → camera space (right/up/depth) at both corners.
        da = np.asarray(a, np.float64) * sc - eye
        db = np.asarray(b, np.float64) * sc - eye
        az, bz = float(da @ fwd), float(db @ fwd)
        if az < _AXIS_NEAR and bz < _AXIS_NEAR:
            return None
        t0, t1 = 0.0, 1.0
        if az < _AXIS_NEAR:
            t0 = (_AXIS_NEAR - az) / (bz - az)
        elif bz < _AXIS_NEAR:
            t1 = (_AXIS_NEAR - az) / (bz - az)
        ax, ay = float(da @ right), float(da @ up)
        bx, by = float(db @ right), float(db @ up)
        cx0, cy0, cz0 = ax + (bx - ax) * t0, ay + (by - ay) * t0, az + (bz - az) * t0
        cx1, cy1, cz1 = ax + (bx - ax) * t1, ay + (by - ay) * t1, az + (bz - az) * t1
        pa, pb = to_screen(cx0, cy0, cz0), to_screen(cx1, cy1, cz1)
        # Liang-Barsky against the image rect.
        s0, s1 = 0.0, 1.0
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        for p, q in ((-dx, pa[0]), (dx, width - pa[0]),
                     (-dy, pa[1]), (dy, height - pa[1])):
            if abs(p) < 1e-9:
                if q < 0.0:
                    return None
                continue
            r = q / p
            if p < 0.0:
                if r > s1:
                    return None
                if r > s0:
                    s0 = r
            else:
                if r < s0:
                    return None
                if r < s1:
                    s1 = r

        def world_u(s):
            # Screen param → world param over the near-clipped span: 1/z
            # interpolates linearly in screen space, so u = s-z0/(z1+s-(z0-z1));
            # ortho z is affine (u = s).
            return s if ortho else s * cz0 / (cz1 + s * (cz0 - cz1))

        u0, u1 = world_u(s0), world_u(s1)
        return (a, b,
                (pa[0] + dx * s0, pa[1] + dy * s0),
                (pa[0] + dx * s1, pa[1] + dy * s1),
                t0 + (t1 - t0) * u0, t0 + (t1 - t0) * u1,
                cz0 + (cz1 - cz0) * u0, cz0 + (cz1 - cz0) * u1)

    edges = []
    for k in range(3):
        i, j = (k + 1) % 3, (k + 2) % 3
        for si in (-1, 1):
            for sj in (-1, 1):
                if any_vis and vis[(i, si)] == vis[(j, sj)]:
                    continue  # the edge's two faces agree → not visible
                a, b = [0, 0, 0], [0, 0, 0]
                a[k], b[k] = -1, 1
                a[i] = b[i] = si
                a[j] = b[j] = sj
                rec = clip(tuple(a), tuple(b))
                if rec is not None:
                    edges.append(rec)
    return edges


def _draw_axis_lines(draw_list, img_pos, edges):
    """The visible silhouette spans as thin imgui lines, shortened near true
    CORNERS (the original fixed_shorten look); a clipped end (near plane /
    screen border) runs to its cut, since the edge continues past it. Labels
    are NOT drawn here any more — they're textured billboards in the voxel
    FBO (_billboard_specs + _render_label_billboards), so they live in the
    3-D scene."""
    line_col = pack_color(0.9, 0.9, 1.0, 0.5)
    for a, b, pa, pb, t0, t1, z0, z1 in edges:
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        length = math.hypot(dx, dy)
        if length < 0.5:
            continue   # zero-area edge: nothing to draw, skip the div
        # Short edges shorten proportionally instead of vanishing - the
        # outline only ever skips sub-2px degenerates.
        shorten = min(_EDGE_SHORTEN_PX, length * 0.25)
        sh_a = shorten if t0 == 0.0 else 0.0
        sh_b = shorten if t1 == 1.0 else 0.0
        ux, uy = dx / length, dy / length
        draw_list.add_line(img_pos[0] + pa[0] + ux * sh_a, img_pos[1] + pa[1] + uy * sh_a,
                           img_pos[0] + pb[0] - ux * sh_b, img_pos[1] + pb[1] - uy * sh_b,
                           line_col, 1.0)


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


def _billboard_specs(edges, axis_display, volume_scale,
                     name_size=24.0, name_padding=34.0, name_opacity=1.0,
                     num_size=16.0, num_padding=11.0, num_opacity=1.0,
                     num_spacing=1.6, num_angle=0.0):
    """[(text, anchor3, u_dir3, v_dir3, out_dir3, px_h, off_px, alpha)] for
    every visible silhouette span — the dim name beside the SPAN's midpoint
    (always on screen, unlike a clipped edge's full midpoint, which can sit
    behind the camera) plus integer ticks (_tick_values) at their TRUE
    positions along the edge; a clipped edge labels only its on-screen index
    range, so a zoomed-in wide volume reads like a scrolled ruler. Anchors
    are volume-box WORLD points ON the edge. All metrics are screen PIXELS,
    held at any zoom (the shader depth-converts at each anchor): `*_size` is
    the label height (0 hides that label type), `*_padding` the GAP between
    the line and the label's near edge (independent of size), `*_opacity`
    the tint alpha. u runs along the edge and v outward from the box
    ("angled perpendicular to the line"); both are flipped for readability —
    the up-axis flips when the quad shows its back (un-mirrors without
    reversing the reading direction), then a 180° spin makes text read
    left-to-right, or bottom-to-top on near-vertical edges. The offset
    always rides the UNFLIPPED outward direction, so labels never land
    inside the box. Nothing hides by projected size any more — the
    face-visibility silhouette already culls truly invisible edges, and
    _tick_values degrades to just the end values on short edges."""
    name_off = name_padding + name_size * 0.5  # anchor -> label CENTER
    num_off = num_padding + num_size * 0.5
    # tick label slant (optional, not the label plane - matplotlib-style)
    ca, sa = math.cos(math.radians(num_angle)), math.sin(math.radians(num_angle))
    pts = [p for e in edges for p in (e[2], e[3])]
    if not pts:
        return []
    scx = sum(p[0] for p in pts) / len(pts)  # silhouette's screen centroid
    scy = sum(p[1] for p in pts) / len(pts)

    specs = []
    for a, b, pa, pb, t0, t1, z0, z1 in edges:
        k = next(i for i in range(3) if a[i] != b[i])  # the axis it runs along
        if a[k] > b[k]:  # a = the texcoord-0 end (visible span flips with it)
            a, b = b, a
            pa, pb = pb, pa
            t0, t1 = 1.0 - t1, 1.0 - t0
            z0, z1 = z1, z0
        px_len = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        if px_len < 0.5:
            continue   # zero-area edge: direction math requires a length
        name, size = axis_display[k]
        a3 = tuple(a[i] * volume_scale[i] for i in range(3))
        b3 = tuple(b[i] * volume_scale[i] for i in range(3))
        length = math.sqrt(sum((b3[i] - a3[i]) ** 2 for i in range(3))) or 1.0
        w = tuple((b3[i] - a3[i]) / length for i in range(3))  # a → b, for placement
        mid_full = tuple((a3[i] + b3[i]) * 0.5 for i in range(3))
        m_len = math.sqrt(sum(c * c for c in mid_full)) or 1.0
        out = tuple(c / m_len for c in mid_full)  # outward, ⊥ the edge (mid-w = 0)
        # World endpoints + midpoint of the VISIBLE span (the label anchors).
        va = tuple(a3[i] + (b3[i] - a3[i]) * t0 for i in range(3))
        vb = tuple(a3[i] + (b3[i] - a3[i]) * t1 for i in range(3))
        mid = tuple((va[i] + vb[i]) * 0.5 for i in range(3))

        # TRUE screen directions, not the camera-basis approximation (which
        # skews under perspective for off-center edges and mirrors oblique
        # labels): the baseline from the edge's mean projected points, the
        # outward axis as its perpendicular pointing away from the
        # silhouette's screen centroid (the world `out` projects into that
        # half-space for any silhouette edge, so the signs match).
        u_s = ((pb[0] - pa[0]) / px_len, (pb[1] - pa[1]) / px_len)
        mxs, mys = (pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5
        ox, oy = mxs - scx, mys - scy
        along = ox * u_s[0] + oy * u_s[1]
        nx, ny = ox - along * u_s[0], oy - along * u_s[1]
        nl = math.hypot(nx, ny) or 1.0
        v_s = (nx / nl, ny / nl)

        u, v = w, out
        # Chirality - readable text needs cross(u_s, v_s) < 0 on a y-down
        # screen. When the quad shows its back, flip the UP axis - that
        # un-mirrors top/bottom without reversing the reading direction.
        if u_s[0] * v_s[1] - u_s[1] * v_s[0] > 0:
            v = tuple(-c for c in v)
        # 180° flipping (chirality-preserving): read left-to-right, or
        # bottom-to-top when the baseline is near-vertical on screen.
        if u_s[0] < -0.2 * abs(u_s[1]) or (
                abs(u_s[0]) <= 0.2 * abs(u_s[1]) and u_s[1] > 0):
            u = tuple(-c for c in u)
            v = tuple(-c for c in v)

        if name_size > 0:
            specs.append((name, mid, u, v, out, name_size, name_off, name_opacity))
        i0, i1 = t0 * size, t1 * size  # the visible index range
        if num_size > 0 and size > 0 and i1 - i0 > 1e-9:
            # Ticks compress into the DRAWN line span (true-corner ends draw
            # shortened; clipped ends need to be cut), and the end labels
            # sit at the visible line ends. Screen px and world params go
            # through the perspective-correct 1/z map (affine when z0 == z1,
            # i.e. ortho or an edge parallel to the screen).
            def u_of(s):
                return s if z0 == z1 else s * z0 / (z1 + s * (z0 - z1))

            def s_of(up):
                return up if z0 == z1 else up * z1 / (z0 + up * (z1 - z0))

            inset_px = min(_EDGE_SHORTEN_PX, px_len * 0.25)
            u_lo = u_of(inset_px / px_len if t0 == 0.0 else 0.0)
            u_hi = u_of(1.0 - (inset_px / px_len if t1 == 1.0 else 0.0))
            if num_angle:
                ut = tuple(ca * u[i] + sa * v[i] for i in range(3))
                vt = tuple(ca * v[i] - sa * u[i] for i in range(3))
            else:
                ut, vt = u, v
            # Step from the span's AVERAGE screen density; perspective
            # compresses the far end, so greedily skip interior ticks whose
            # SCREEN positions crowd the previous one or the end label.
            ticks = _tick_values(i0, i1, px_len / (i1 - i0), num_size,
                                 num_spacing)  # [] on an integer-free sliver
            min_gap = max(1, len(str(ticks[-1] if ticks else 0))) \
                      * 0.62 * num_size * num_spacing
            placed = []
            for n, idx in enumerate(ticks):
                up = u_lo + (u_hi - u_lo) * ((idx - i0) / (i1 - i0))
                s_px = s_of(up) * px_len
                if 0 < n < len(ticks) - 1 and placed and (
                        s_px - placed[-1] < min_gap
                        or s_of(u_hi) * px_len - s_px < min_gap):
                    continue
                placed.append(s_px)
                p = tuple(va[i] + (vb[i] - va[i]) * up for i in range(3))
                specs.append((str(idx), p, ut, vt, out, num_size, num_off, num_opacity))
    return specs


def _render_label_billboards(gl_state, specs, cam, height, font=None):
    """Draw every label spec into the CURRENT FBO with the volume's camera
    (`cam` = the camera uniform kwargs) in ONE instanced draw: assemble the
    per-instance buffer (anchor/axes/metrics/uv-rect per label), upload,
    glDrawArraysInstanced. Pixel sizes/offsets convert to NDC units against
    the viewport height (`height`); the shader depth-scales them at each
    anchor for screen-constant labels."""
    if not specs:
        return
    texts = tuple(sorted({s[0] for s in specs}))
    atlas, rects = _label_atlas(gl_state, texts, font=font)
    prog, loc = _label_program(gl_state)
    vao, vbo = _label_vao(gl_state)

    ndc_per_px = 2.0 / max(1.0, float(height))
    data = np.empty((len(specs), _LABEL_FLOATS), np.float32)
    for i, (text, anchor, u, v, out, px_h, off_px, alpha) in enumerate(specs):
        u0, v0, u1, v1, tw, th = rects[text]
        half_h = (px_h * 0.5) * ndc_per_px
        row = data[i]
        row[0:3] = anchor
        row[3:6] = u
        row[6:9] = v
        row[9:12] = out
        row[12] = half_h * (tw / max(1, th))
        row[13] = half_h
        row[14] = off_px * ndc_per_px
        row[15] = alpha
        row[16:20] = (u0, v0, u1, v1)

    blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
    prev_prog = gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)
    gl.glEnable(gl.GL_BLEND)
    gl.glBlendEquation(gl.GL_FUNC_ADD)
    gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA,
                           gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
    gl.glUseProgram(prog)
    gl.glUniform1f(loc["tilt"], cam["tilt"])
    gl.glUniform1f(loc["spin"], cam["spin"])
    gl.glUniform1f(loc["roll"], cam["roll"])
    gl.glUniform1f(loc["zoom"], cam["zoom"])
    gl.glUniform1f(loc["aspect"], cam["aspect"])
    gl.glUniform1i(loc["ortho"], 1 if cam["ortho"] else 0)
    gl.glUniform3f(loc["pan"], cam["pan_x"], cam["pan_y"], cam["pan_z"])
    gl.glUniform1i(loc["label"], 0)
    gl.glActiveTexture(gl.GL_TEXTURE0)
    gl.glBindTexture(gl.GL_TEXTURE_2D, atlas.texture_id)
    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STREAM_DRAW)
    gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 6, len(specs))
    gl.glBindVertexArray(0)
    gl.glUseProgram(prev_prog)
    if not blend_was:
        gl.glDisable(gl.GL_BLEND)
