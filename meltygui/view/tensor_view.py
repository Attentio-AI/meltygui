"""Tensor view functions and supporting definitions."""
from meltygui.core.tensor_core import _voxels_cleanup
from meltygui.gl_state import GLState
from meltygui.model.tensor_model import Lut
from meltygui.model.tensor_model import TensorDim
from meltygui.model.tensor_model import TensorDims
from meltygui.modes import Modes
from meltygui.rendering.core_render import render_func
from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.rendering.shaped import Shaped
from meltygui.toggles import SwooshMode
from meltygui.toggles import Toggles
from meltygui.view.header_view import draw_header
import OpenGL.GL as gl
import math
import meltygui_imgui as imgui
import numpy as np


@render_func(is_default_for="Lut", show_bg=False, is_tree=False,
             header_same_line=True, with_header=draw_header)
def draw_lut(input_value=None, draw_state=None, unique=0, **kwargs):
    """THE lut picker — a dropdown of the LUT names the lut host knows about
    (live host dict when it's up, baked LUTS otherwise), shared by every
    lut-typed param. Returns Lut(...) so the value keeps routing here."""
    from meltygui.model.tensor_model import Lut
    from meltygui.tensor.voxel_playground import LUTS
    from meltygui.view.dropdown_view import draw_dropdown

    host_val = getattr(globals().get("lut_host"), "input_value", None)
    luts = host_val if isinstance(host_val, dict) and host_val else LUTS
    names = [str(k) for k in luts]
    current = str(input_value) if input_value else "jet"
    changed, picked = draw_dropdown(
        current, collection={n: n for n in names},
        name=f"lut##{unique}", show_header=False, width=140)
    if changed and picked:
        return True, Lut(picked)
    return False, input_value


@render_func(is_default_for=("TensorDim", "TensorDims"), show_bg=False, is_tree=False,
             header_same_line=True, with_header=draw_header)
def draw_tensor_dim(input_value=None, draw_state=None, unique=0, **kwargs):
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
    from meltygui.tensor.voxel_playground import _collection_dim_labels
    from meltygui.tensor.voxel_playground import _draw_dim_tabs
    from meltygui.tensor.voxel_playground import _row_collection
    from meltygui.tensor.voxel_playground import _swap_sibling_dim

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
            [d for d in cur if 0 <= d < n], multi=True)
        if changed:
            return True, TensorDims(sorted(int(s) for s in selected))
        return False, input_value
    # Single-select: a leading "off" tab maps to -1 (unset - sort disabled,
    # nf/axis dims derived), so unsetting doesn't rely on double-click.
    cur = int(input_value) if input_value is not None else -1
    if not (0 <= cur < n):
        cur = -1
    changed, selected = _draw_dim_tabs(
        draw_state, [-1] + list(range(n)), ["off"] + labels, [cur], multi=False)
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
                kp_decimal_pressed=None, **kwargs):
    """The voxel renderer — owner of every render and mapping decision.
    Input is a tensor/ndarray (sliced + uploaded HERE, re-keyed by gl_state
    deps on source identity/_version/mapping) or an already-uploaded
    GLTexture (rendered as-is). Tensor METADATA — full shape, dim count —
    rides the uploaded buffer; EVERYTHING else is a parameter on this
    signature (auto draw_state params: gestures and the controls panel
    write draw_state.<name>, only diverged values persist/serialize)."""
    from meltygui.gl_state import GLTexture
    from meltygui.gl_state import gl_limits
    from meltygui.gl_state import texture3d_fit
    from meltygui.tensor.voxel_camera import apply_space_mouse
    from meltygui.tensor.voxel_playground import CudaVolumeView
    from meltygui.tensor.voxel_playground import HALF_PI
    from meltygui.tensor.voxel_playground import LUTS
    from meltygui.tensor.voxel_playground import _AXIS_POS
    from meltygui.tensor.voxel_playground import _CUDA_LAST_ERROR
    from meltygui.tensor.voxel_playground import _LUT_TEXTURES
    from meltygui.tensor.voxel_playground import _axis_edges
    from meltygui.tensor.voxel_playground import _billboard_specs
    from meltygui.tensor.voxel_playground import _cached_volume_texture
    from meltygui.tensor.voxel_playground import _clean_dim_name
    from meltygui.tensor.voxel_playground import _cuda_march_ready
    from meltygui.tensor.voxel_playground import _cuda_render
    from meltygui.tensor.voxel_playground import _describe_tensor
    from meltygui.tensor.voxel_playground import _draw_axis_lines
    from meltygui.tensor.voxel_playground import _draw_image_notice
    from meltygui.tensor.voxel_playground import _draw_slice_sliders
    from meltygui.tensor.voxel_playground import _draw_tensor_meta
    from meltygui.tensor.voxel_playground import _draw_voxel_error
    from meltygui.tensor.voxel_playground import _render_label_billboards
    from meltygui.tensor.voxel_playground import _resolve_dim
    from meltygui.tensor.voxel_playground import _view_size
    from meltygui.tensor.voxel_playground import _volume_scale
    from meltygui.tensor.voxel_playground import auto_neural_flow
    from meltygui.tensor.voxel_playground import image_blit_pass
    from meltygui.tensor.voxel_playground import slice_volume
    from meltygui.tensor.voxel_playground import slice_volume_view
    from meltygui.tensor.voxel_playground import source_identity
    from meltygui.tensor.voxel_playground import voxel_pass
    from meltygui.utils.glfw_utils import request_render
    from meltygui.core.render_dispatch import draw_any
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
    from meltygui.melty import Melty
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

    # ── LUT: prefer the shared 1-D texture the LUT host materialized; fall
    # back to a direct upload of the named lut until the host has time ────
    lut_tex = _LUT_TEXTURES.get(lut)
    if lut_tex is None:
        lut_list = LUTS.get(lut, LUTS["jet"])
        lut_tex = gl_state.texture1d("lut_fallback", lut_list,
                                     version=(lut, len(lut_list)))

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
                _render_label_billboards(gl_state, specs, cam, height)
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
