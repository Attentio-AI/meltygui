"""Voxel presentation: volume passes, camera interaction and in-scene labels."""
from meltygui.core.graphics.tensor_core import _voxels_cleanup
from meltygui.hdr_color import pack_color
from meltygui.core.graphics.gl_state import GLState
from meltygui.model.lut_model import Lut, LutPalette
from meltygui.model.tensor_model import TensorDim
from meltygui.model.tensor_model import TensorDims
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
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
from meltygui.core.graphics.text_texture import bake_texts
from meltygui.model.camera_model import basis as _cam_basis
from meltygui.core.graphics.shader_func import shader_func
from meltygui.core.graphics.tensor_core import source_identity
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.model.texture_model import _cached_volume_texture, _upload_cuda_image
from meltygui.state.voxel_state import VoxelState
from meltygui.view.texture_view import image_blit_pass
from meltygui.view.tensor_view import _describe_tensor, _view_size, _draw_image_notice
from meltygui.view.tensor_view import _draw_tensor_meta, _draw_voxel_error, _draw_slice_sliders, _tick_values


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
def draw_voxels(input_value: object = None, gl_state: GLState = None, selectable=False,
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
                luts: LutPalette = None, voxel_state: VoxelState = None,
                keyboard_available=True, pointer_buttons_down=False, **kwargs):
    """Render voxels using CUDA for CUDA tensors and OpenGL for other inputs."""
    parameters = locals()
    extra = parameters.pop("kwargs")
    return _draw_voxels(**(extra | parameters | {"backend": "auto"}))


@render_func(show_bg=True, selectable=True,
             auto_resize=False, min_width=269, with_header=draw_header,
             bg_offset=0, min_height=293, disable_scroll=True, use_cache=True,
             on_cleanup=_voxels_cleanup)
def draw_voxels_opengl(input_value: object = None, gl_state: GLState = None, selectable=False,
                draw_state=None,
                tilt=0.283, spin=0.724, roll=0.0, cam_zoom=3.4,
                pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
                cam_brightness=1.332, cam_contrast=1.0,
                density=0.7, threshold=0.297, centered=False,
                nearest=True, lut=Lut("jet"), step_size=0.0005, max_steps=4096,
                draw_plane=True, shadow_opacity=1.0, shadow_softness=0.15,
                draw_shading=True, self_shading=True,
                light_pos=(50.0, -50.0, 200.0), light_tint=(1.0, 1.0, 1.0),
                light_brightness=1.622, ambient_light=0.3, shading_strength=0.7,
                dim_names=("layer", "batch", "token", "feature"),
                x_dim=TensorDim(0), y_dim=TensorDim(2), z_dim=TensorDim(2),
                slices=(),
                mean_dims=TensorDims(()), sort_dim=TensorDim(-1),
                normalize=False, nf_on=False, nf_chop=TensorDim(-1),
                nf_along=TensorDim(-1), nf_chunk=128,
                name_size=17.0, name_padding=30.1, name_opacity=1.1,
                num_size=17.1, num_padding=5.5, num_opacity=0.8,
                num_spacing=1.0, num_angle=0.0, z_offset=1,
                middle_mouse_drag=None, double_right_mouse_drag=None,
                scroll_y_changed=None, space_mouse_changed=None,
                left_mouse_double_clicked=None,
                kp_7_pressed=None, kp_1_pressed=None, kp_3_pressed=None,
                kp_5_pressed=None, slash_pressed=None, kp_divide_pressed=None,
                kp_decimal_pressed=None, font_manager=None,
                luts: LutPalette = None, voxel_state: VoxelState = None,
                keyboard_available=True, pointer_buttons_down=False, **kwargs):
    """Raymarch tensors, NumPy arrays or GLTexture values with OpenGL."""
    parameters = locals()
    extra = parameters.pop("kwargs")
    return _draw_voxels(**(extra | parameters | {"backend": "opengl"}))


@render_func(show_bg=True, selectable=True,
             auto_resize=False, min_width=269, with_header=draw_header,
             bg_offset=0, min_height=293, disable_scroll=True, use_cache=True,
             on_cleanup=_voxels_cleanup)
def draw_voxels_cuda(input_value: object = None, gl_state: GLState = None, selectable=False,
                draw_state=None,
                tilt=0.283, spin=0.724, roll=0.0, cam_zoom=3.4,
                pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
                cam_brightness=1.332, cam_contrast=1.0,
                density=0.7, threshold=0.297, centered=False,
                nearest=True, lut=Lut("jet"), step_size=0.0005, max_steps=4096,
                draw_plane=True, shadow_opacity=1.0, shadow_softness=0.15,
                draw_shading=True, self_shading=True,
                light_pos=(50.0, -50.0, 200.0), light_tint=(1.0, 1.0, 1.0),
                light_brightness=1.622, ambient_light=0.3, shading_strength=0.7,
                dim_names=("layer", "batch", "token", "feature"),
                x_dim=TensorDim(0), y_dim=TensorDim(2), z_dim=TensorDim(2),
                slices=(),
                mean_dims=TensorDims(()), sort_dim=TensorDim(-1),
                normalize=False, nf_on=False, nf_chop=TensorDim(-1),
                nf_along=TensorDim(-1), nf_chunk=128,
                name_size=17.0, name_padding=30.1, name_opacity=1.1,
                num_size=17.1, num_padding=5.5, num_opacity=0.8,
                num_spacing=1.0, num_angle=0.0, z_offset=1,
                middle_mouse_drag=None, double_right_mouse_drag=None,
                scroll_y_changed=None, space_mouse_changed=None,
                left_mouse_double_clicked=None,
                kp_7_pressed=None, kp_1_pressed=None, kp_3_pressed=None,
                kp_5_pressed=None, slash_pressed=None, kp_divide_pressed=None,
                kp_decimal_pressed=None, font_manager=None,
                luts: LutPalette = None, voxel_state: VoxelState = None,
                keyboard_available=True, pointer_buttons_down=False, **kwargs):
    """Raymarch a CUDA tensor in place on its own GPU."""
    parameters = locals()
    extra = parameters.pop("kwargs")
    return _draw_voxels(**(extra | parameters | {"backend": "cuda"}))


def _draw_voxels(input_value: object = None, gl_state: GLState = None, selectable=False,
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
                luts: LutPalette = None, voxel_state: VoxelState = None,
                keyboard_available=True, pointer_buttons_down=False, backend="auto", **kwargs):
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
    from meltygui.model.tensor_model import _AXIS_POS
    from meltygui.model.tensor_model import _clean_dim_name
    from meltygui.model.tensor_model import _resolve_dim
    from meltygui.model.tensor_model import _volume_scale
    from meltygui.model.tensor_model import auto_neural_flow
    from meltygui.model.tensor_model import slice_volume
    from meltygui.model.tensor_model import slice_volume_view
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.rendering.render_dispatch import draw_any

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
    if backend == "cuda" and not bool(getattr(src, "is_cuda", False)):
        _draw_voxel_error(draw_state, "CUDA voxel rendering requires a CUDA tensor.")
        gl_state.drop("volume"); gl_state.drop("volume_cuda"); gl_state.drop("cuda_view")
        return False, input_value
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
        # Explicit OpenGL selection uploads the sliced tensor to a 3-D
        # texture, including CUDA tensors. Automatic selection keeps CUDA
        # sources in place; the CUDA renderer never silently falls back.
        use_cuda = backend != "opengl" and bool(getattr(t, "is_cuda", False))
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
                from meltygui.model.cuda_texture_model import tensor_to_texture
                tex = tensor_to_texture(gl_state, "volume_cuda", vol,
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
    if keyboard_available:
        if kp_7_pressed is not None:
            spin, tilt = -(math.pi / 2), (-(math.pi / 2) if kp_7_pressed.ctrl else (math.pi / 2))
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_1_pressed is not None:
            spin, tilt = ((math.pi / 2) if kp_1_pressed.ctrl else -(math.pi / 2)), 0.0
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
                if not voxel_state.label_warned:
                    voxel_state.label_warned = True
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
            from meltygui.view import voxel_cuda_view
            img_tex = _cuda_render(
                gl_state, tex, width, height, voxel_state, lut=lut, tilt=tilt, spin=spin,
                lut_texture=lut_tex,
                roll=roll, zoom=cam_zoom, pan=(pan_x, pan_y, pan_z), ortho=bool(ortho),
                volume_scale=volume_scale, step_size=float(step_size),
                max_steps=int(max_steps), density=float(density),
                threshold=float(threshold), brightness=float(cam_brightness),
                contrast=float(cam_contrast), gamma=float(Toggles.Voxels.gamma),
                centered=bool(centered),
                shade=voxel_cuda_view.shade_params(
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
    init = voxel_state.params_panel is None
    if init:
        # Adopt the same view's saved panel state during the migration.
        voxel_state.params_panel = draw_state.misc.pop("params_panel", False)
    toggled = False
    if left_mouse_double_clicked is not None:
        voxel_state.params_panel = not voxel_state.params_panel
        toggled = True
        draw_state.invalidate()
        request_render()
    panel_open = bool(voxel_state.params_panel)
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
            voxel_state.params_panel = not panel_ds.closed
            # The panel is cached and must NOT invalidate per drag frame - it
            # rides its blit while a camera gesture writes the params, then
            # catches up ONCE at the gesture edge.
            if not panel_ds.closed:
                if (not pointer_buttons_down and scroll_y_changed is None
                        and space_mouse_changed is None) and changed:
                    panel_ds.invalidate_up()

        # ── status bar error surfacing only ────────────────────────────────────
        if voxel_pass.last_error:
            # imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
            imgui.text_colored(voxel_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)
        if isinstance(tex, CudaVolumeView) and voxel_state.cuda_error:
            imgui.text_colored(voxel_state.cuda_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)

        if changed:
            # UP, not self: this view is often nested (a live-value window,
            # a collection row) and its pixels are baked into ancestor blit
            # tiles - a self-only invalidate left the ancestor serving the
            # stale image, so panel edits "didn't take" until something else
            # repushed the ancestor.
            draw_state.invalidate_up()
            request_render()
            return changed, input_value

    return False, input_value


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


_AXIS_NEAR = 0.05


_EDGE_SHORTEN_PX = 14.0


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
        return bake_texts(texts, gl_state=gl_state, font=font)

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


VOXEL_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;

// Slab intersection with the box [-bounds, +bounds]: (t_enter, t_exit).
vec2 rayBox(vec3 ro, vec3 rd, vec3 bounds) {
    vec3 inv = 1.0 / rd;
    vec3 t0 = (-bounds - ro) * inv;
    vec3 t1 = ( bounds - ro) * inv;
    vec3 lo = min(t0, t1);
    vec3 hi = max(t0, t1);
    return vec2(max(max(lo.x, lo.y), lo.z), min(min(hi.x, hi.y), hi.z));
}

// ── shared transfer function: raw volume sample at texcoord p →
// (v: remapped LUT coordinate, m: opacity drive). The old viewer's value
// pipeline verbatim — contrast about mid-grey, then brightness, on the
// GREYSCALE value; `centered` maps signed data so raw 0 sits at the LUT
// middle (pair with a diverging LUT) and opacity keys on MAGNITUDE, so
// negatives render as strongly as positives. Takes the sampler to read
// through: `volume` (the user's nearest/linear toggle) for the color march,
// `volume_lin` (always linear — same texture, its own sampler object) for
// every shading read, where smooth beats blocky regardless of the toggle.
vec2 remapValue(sampler3D vol, vec3 p) {
    float v = texture(vol, p).r;
    if (centered) { v = v * 0.5 + 0.5; }   // signed [-1,1] -> [0,1]
    v = (v - 0.5) * contrast + 0.5;
    float m;
    if (centered) {
        v = 0.5 + (v - 0.5) * brightness;
        v = clamp(v, 0.0, 1.0);
        m = abs(v - 0.5) * 2.0;
    } else {
        v *= brightness;
        v = clamp(v, 0.0, 1.0);
        m = v;
    }
    return vec2(v, m);
}

// Extended-sRGB decode (hdr_color.py's convention): the sRGB curve
// mirrored for negatives, no ceiling — a LUT entry above 1 is brighter than
// the desktop's white, a negative one is outside the sRGB gamut (P3). A
// plain pow() turned negatives into NaN.
vec3 decodeSrgb(vec3 c) { return sign(c) * pow(abs(c), vec3(2.2)); }

// The old viewer's opacity ramp: values at/above the gate (1 - threshold)
// are FULLY opaque — a hard isosurface — and below it opacity falls off as
// (m/gate)^4, scaled by density and the volume_scale-NORMALIZED segment
// length, so optical depth is a function of the FRACTION of the volume
// traversed, not the world path length — a 4096-voxel axis viewed end-on
// accumulates the same opacity as an 8-voxel one.
float alphaFor(float m, float seg_n) {
    float gate = 1.0 - clamp(threshold, 0.0, 0.999);
    if (m >= gate) return 1.0;
    return clamp(pow(m / gate, 4.0) * density * seg_n * 50.0, 0.0, 1.0);
}

// Transmittance from a world point toward the light: a COARSE fixed-count
// march of the SAME transfer function (shadows are low-frequency — fat
// steps read clean where the primary ray needs thousands), multiplying out
// per-step opacity. The result is graded the way the render is: haze dims
// the light, the opaque core blocks it. `steps` sets the quality tier
// (the plane's cast shadow affords more than per-sample self-shading) and
// `max_dist` optionally caps the march to NEAR-FIELD occluders — for
// self-shading, what's right next to a sample is most of its shadow.
// Always reads through volume_lin: shading wants smooth fields.
// The Info variant also reports WHERE occlusion happened: (T, t_occ) with
// t_occ the distance along the light ray at which transmittance first
// dropped below 0.5 — the occluder height that drives the ground-space
// penumbra radius. Rays that never occlude report the mid-chord of their
// box span instead, so pixels just OUTSIDE the umbra blur with the same
// radius as their shadowed neighbors (the penumbra widens on BOTH sides
// of the hard edge); rays that miss the box entirely report 0.
vec2 lightVisibilityInfo(vec3 p, vec3 lp, int steps, float max_dist) {
    vec3 ld = normalize(lp - p);
    vec2 span = rayBox(p, ld, volume_scale);
    float t0 = max(span.x, 0.0);
    float t1 = min(min(span.y, length(lp - p)), max_dist);
    if (t0 >= t1) return vec2(1.0, 0.0);
    float ss = (t1 - t0) / float(steps);
    float seg_n = ss * length(ld / volume_scale);
    float T = 1.0;
    float t_occ = 0.0;
    float t = t0 + ss * 0.5;
    for (int i = 0; i < steps; i++) {
        vec3 q = (p + ld * t) / volume_scale * 0.5 + 0.5;
        T *= 1.0 - alphaFor(remapValue(volume_lin, q).y, seg_n);
        if (t_occ == 0.0 && T < 0.5) { t_occ = t; }
        if (T < 0.02) break;   // fully shadowed — stop early
        t += ss;
    }
    if (t_occ == 0.0) { t_occ = 0.5 * (t0 + t1); }
    return vec2(T, t_occ);
}

float lightVisibility(vec3 p, vec3 lp, int steps, float max_dist) {
    return lightVisibilityInfo(p, lp, steps, max_dist).x;
}

// Gradient normal at texcoord p (central differences, one voxel apart),
// mapped to WORLD space (anisotropic boxes bend gradients), plus a shading
// weight (w) that fades to 0 where the gradient is too weak to trust —
// uniform haze keeps its flat unshaded look instead of picking up noise.
vec4 volumeNormal(vec3 p) {
    // 1.75-voxel stencil: with the linear sampler this is a genuine lowpass
    // on the normal field, so hard binary edges (a 0/1 mask's staircase)
    // shade as smooth ramps instead of per-voxel facets.
    vec3 e = 1.75 / vec3(textureSize(volume_lin, 0));
    vec3 g = vec3(
        texture(volume_lin, p + vec3(e.x, 0, 0)).r - texture(volume_lin, p - vec3(e.x, 0, 0)).r,
        texture(volume_lin, p + vec3(0, e.y, 0)).r - texture(volume_lin, p - vec3(0, e.y, 0)).r,
        texture(volume_lin, p + vec3(0, 0, e.z)).r - texture(volume_lin, p - vec3(0, 0, e.z)).r);
    if (centered) { g *= sign(texture(volume_lin, p).r); }   // shade |v|'s surface
    vec3 gw = g / volume_scale;              // texcoord gradient → world
    float len = length(gw);
    // The normal points from dense toward empty — that's MINUS the gradient.
    return vec4(len > 1e-6 ? -gw / len : vec3(0.0, 0.0, 1.0),
                clamp(length(g) * 6.0, 0.0, 1.0));
}

void main() {
    // Z-up orbit camera built straight from injected uniforms — tilt, spin, roll,
    // zoom, pan and ortho arrive as plain Python kwargs, no matrices anywhere.
    // The basis is analytic in spin/tilt (not cross(fwd, world-up)) so the
    // numpad top/bottom presets (tilt = ±π/2) stay well-defined; it matches
    // the old construction everywhere else.
    float ct = cos(tilt);
    vec3 fwd = -vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt));
    vec3 right0 = vec3(-sin(spin), cos(spin), 0.0);
    // roll turns right toward up about the view axis (0 = level horizon,
    // the turntable); the 3D mouse's trackball mode is what writes it.
    vec3 right = right0 * cos(roll) + cross(right0, fwd) * sin(roll);
    vec3 up = cross(right, fwd);
    vec3 eye = vec3(pan_x, pan_y, pan_z) - fwd * zoom;
    vec2 ndc = (uv * 2.0 - 1.0) * vec2(aspect, 1.0);
    // Perspective rays fan out from the eye; ortho rays march parallel from
    // a plane through it, sized to match the perspective frame at the target.
    vec3 ro = ortho ? eye + (right * ndc.x + up * ndc.y) * (zoom / 1.7) : eye;
    vec3 rd = ortho ? fwd : normalize(fwd * 1.7 + right * ndc.x + up * ndc.y);
    // Accumulate optical depth per unit of VIEW DEPTH, not per unit of ray
    // arc length. In perspective, edge rays cross the volume at a steeper
    // angle and a step's world length (seg) is ~1/cos(theta) longer than a
    // center ray's, so a thin slab reads denser toward the screen edges (a
    // screenspace radial artifact — hidden on cubes only because they saturate
    // the alpha break). cos(angle to fwd) cancels the extra path. Ortho rays
    // have rd == fwd, so view_cos == 1 and this is a no-op there.
    float view_cos = dot(rd, fwd);

    // ── shadow catcher: the plane the box rests on (z = -volume_scale.z)
    // is itself INVISIBLE — its only contribution is the shadow the volume
    // casts onto it, composited as a darkening with alpha = blocked light.
    // One-sided (backface-culled analytically): a hit only counts for rays
    // striking the TOP face — from underneath there's no shadow at all.
    float plane_t = -1.0;
    float plane_a = 0.0;
    // The caught shadow darkens toward shadow_tint — a neutral grey-black
    // (Toggles.Voxels.floor_shadow_color), decoupled from the blue-black
    // the rest of the studio's compositor shadows carry.
    vec3 plane_c = shadow_tint;
    // plane_side mirrors the catcher to the box's OTHER face when the view
    // is upside-down (+1 = floor at -z, -1 = at +z; latched between drags
    // on the Python side). The conditions are the normal ones written in
    // z' = z * plane_side; the catcher's light is mirrored to match below.
    if (draw_plane && draw_shading && rd.z * plane_side < -1e-6
            && ro.z * plane_side > -volume_scale.z) {
        plane_t = (-volume_scale.z * plane_side - ro.z) / rd.z;
        vec3 pw = ro + rd * plane_t;
        // Blocked light via the same transmittance march as the rest of the
        // shading, lifted by the ambient floor (ambient_light raises this
        // shadow like every other). The exponential radial fade bounds the
        // catcher so the darkening dies off instead of cutting.
        float ext = max(volume_scale.x, volume_scale.y);
        float r = max(length(pw.xy) - ext * 1.1, 0.0);
        // Center march gathers (visibility, occluder distance); the blur
        // then happens in FLOOR coordinates — 4 extra visibility taps on a
        // ring around the hit point, radius = shadow_softness × occluder
        // distance (higher occluders throw softer shadows), averaged with
        // the center. Skipped when the center ray misses the box (t_occ 0
        // — open floor, nothing to soften).
        // The catcher's marches use a PLANE-LOCAL light: light_pos with its
        // z mirrored to the plane's side. The real light stays fixed in
        // world space (the volume's shading uses it untouched) — this
        // mirror only makes the flipped floor catch the same silhouette
        // the bottom floor would, instead of a ceiling catching nothing.
        vec3 pl_light = vec3(light_pos.xy, light_pos.z * plane_side);
        vec2 vi = lightVisibilityInfo(pw, pl_light, 24, 1e8);
        float vis = vi.x;
        float blur_r = shadow_softness * vi.y;
        if (blur_r > 1e-4) {
            float acc_v = vis;
            for (int k = 0; k < 4; k++) {
                float ang = float(k) * 1.5707963 + 0.7853982;
                vec3 op = pw + vec3(cos(ang), sin(ang), 0.0) * blur_r;
                acc_v += lightVisibility(op, pl_light, 10, 1e8);
            }
            vis = acc_v / 5.0;
        }
        float shadow = (1.0 - ambient_light) * (1.0 - vis);
        plane_a = clamp(shadow * shadow_opacity, 0.0, 1.0) * exp(-1.5 * r / ext);
    }

    // volume_scale: box extents per axis, voxel-count-proportional — so each
    // VOXEL is a cube and the tensor keeps its true shape.
    vec2 hit = rayBox(ro, rd, volume_scale);
    bool box_hit = !(hit.x > hit.y || hit.y < 0.0);
    if (!box_hit && plane_t <= 0.0) { FragColor = vec4(0.0); return; }

    vec4 acc = vec4(0.0);
    // Plane in FRONT of the volume (looking down at foreground floor past
    // the box — possible since the fade extends beyond it): composite it
    // first. The box's bottom face lies IN the plane, so a ray never
    // crosses the plane mid-march — it's strictly before or after the box.
    if (plane_t > 0.0 && box_hit && plane_t <= max(hit.x, 0.0)) {
        acc = vec4(plane_c * plane_a, plane_a);
        plane_t = -1.0;
    }

    float t = max(hit.x, 0.0);
    // max_steps is a watchdog: the break on hit.y is what normally ends the
    // march. The whole box is covered only while max_steps * step_size
    // exceeds the worst-case chord (2*sqrt(3) ≈ 3.46 units) — a granular
    // step_size needs a higher cap or the far side of the volume clips away.
    for (int i = 0; i < max_steps; i++) {
        if (t >= hit.y || acc.a > 0.98) break;
        // Weight each sample by the segment it actually covers (the tail is
        // partial) and sample at the segment MIDPOINT: a slab thinner than
        // one step then accumulates opacity proportional to its true path
        // length instead of jumping by whole steps as the sample count
        // changes with view angle — the concentric-ring artifact on thin
        // tensors.
        float seg = min(step_size, hit.y - t);
        vec3 p = (ro + rd * (t + seg * 0.5)) / volume_scale * 0.5 + 0.5;
        // Value pipeline + opacity ramp live in remapValue/alphaFor (shared
        // with the shadow march). `lut` is a 1-D texture the LUT host baked
        // from a flat [r,g,b,...] float list.
        vec2 vm = remapValue(volume, p);
        float seg_n = seg * length(rd / volume_scale);
        float a = alphaFor(vm.y, seg_n * view_cos);
        if (a > 0.0) {
            // Composite in LINEAR light: the LUT tables are display-referred
            // sRGB, so decode each sample before accumulating (encode once at
            // the end). Blending in sRGB space skews mixes toward the more
            // saturated component — the old harsh/garish translucency.
            vec3 c = decodeSrgb(texture(lut, vm.x).rgb);
            if (draw_shading) {
                // Gradient-normal Lambert, weighted by gradient strength so
                // flat haze keeps its unshaded look; self_shading adds a
                // FAST near-field transmittance march toward the light —
                // 6 fat linear-filtered steps capped close to the sample,
                // since nearby occluders are most of a sample's shadow.
                // Both the normal taps and the march are skipped where they
                // can't show: sub-1% alpha samples, and (for the march)
                // gradient weight ≈ 0 — the mix would erase it anyway.
                float shade = 1.0;
                if (a > 0.01) {
                    vec4 nw = volumeNormal(p);
                    if (nw.w > 0.01) {
                        vec3 wp = ro + rd * (t + seg * 0.5);
                        // Half-Lambert wrap: (n·l/2 + 1/2)² instead of the
                        // hard max(n·l, 0). Faces pointing away from the
                        // light dim gently rather than clamping to the
                        // ambient floor — on binary data the hard clamp
                        // turned every off-facing step facet into the same
                        // flat dark block.
                        float ndl = dot(nw.xyz, normalize(light_pos - wp)) * 0.5 + 0.5;
                        float vis = self_shading
                                  ? lightVisibility(wp, light_pos, 6, 0.7) : 1.0;
                        shade = mix(1.0, ambient_light
                                    + (1.0 - ambient_light) * ndl * ndl * vis,
                                    nw.w * shading_strength);
                    }
                }
                c *= decodeSrgb(light_tint) * light_brightness * shade;
            }
            acc.rgb += (1.0 - acc.a) * a * c;
            acc.a   += (1.0 - acc.a) * a;
        }
        t += step_size;
    }
    // Plane BEHIND the volume (the usual case): composite it under
    // whatever the march accumulated.
    if (plane_t > 0.0) {
        acc.rgb += (1.0 - acc.a) * plane_c * plane_a;
        acc.a   += (1.0 - acc.a) * plane_a;
    }
    // The target is the linear fp16 scene (hdr_color.py): no sRGB encode
    // here, the presentation pass does that once. `gamma` is an artistic
    // curve on the linear image, 1.0 = untouched (the colorimetric result),
    // above 1 darkens the mids against the studio's dark UI. Mirrored for
    // negatives (P3 rides as negative scRGB). No dither: the fp16 target
    // doesn't band.
    FragColor = vec4(sign(acc.rgb) * pow(abs(acc.rgb), vec3(gamma)), acc.a);
}
"""


@shader_func(fragment=VOXEL_FRAG)
def voxel_pass(gl_state: GLState = None, tilt=0.5, spin=0.8, roll=0.0, zoom=3.4,
               pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
               aspect=1.0, brightness=1.0, contrast=1.0, density=1.0, gamma=1.6,
               threshold=0.1, step_size=0.0015, max_steps=4096, centered=False,
               volume=None, volume_lin=None, lut=None,
               draw_plane=True, shadow_opacity=1.0, shadow_softness=0.15,
               shadow_tint=(0.0, 0.02, 0.05), plane_side=1.0,
               draw_shading=True, self_shading=True,
               light_pos=(90.0, -90.0, 200.0), light_tint=(1.0, 1.0, 1.0),
               light_brightness=1.622, ambient_light=0.3, shading_strength=0.7,
               volume_scale=(1.0, 1.0, 1.0), program=None, **kwargs):
    # Program bound, uniforms set. volume_lin is the SAME texture as volume
    # on its own unit; a GL sampler object forces LINEAR filtering on that
    # unit so the shading reads a smooth field, while the core march keeps
    # the user's nearest/linear choice (filtering is texture-object state -
    # a bound sampler object is the rare GL mechanism that overrides it
    # per unit).
    def create():
        s = int(gl.glGenSamplers(1))
        gl.glSamplerParameteri(s, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glSamplerParameteri(s, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        for w in (gl.GL_TEXTURE_WRAP_S, gl.GL_TEXTURE_WRAP_T,
                  gl.GL_TEXTURE_WRAP_R):
            gl.glSamplerParameteri(s, w, gl.GL_CLAMP_TO_EDGE)  # = texture3d's
        return s
    sampler = gl_state.get("volume_lin_sampler", create,
                           lambda v: gl.glDeleteSamplers(1, [int(v)]))
    unit = -1
    loc = gl.glGetUniformLocation(program, "volume_lin")
    if loc >= 0:
        buf = np.zeros(1, np.int32)
        gl.glGetUniformiv(program, loc, buf)
        unit = int(buf[0])
        gl.glBindSampler(unit, sampler)
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
    if unit >= 0:
        gl.glBindSampler(unit, 0)   # sampler bindings outlive the draw call


def _cuda_march_ready():
    try:
        from meltygui.core.graphics.cuda_kernel_core import available
        return available()
    except Exception:
        return False


def _cuda_render(gl_state, cv, width, height, voxel_state: VoxelState, lut="jet", shade=None,
                 lut_texture=None, **cam):
    """Run the CUDA raymarcher over `cv` (CudaVolumeView) at width×height
    and return a display-GPU RGBA16F GLTexture holding the premultiplied
    LINEAR image (HDR headroom and P3 negatives intact, hdr_color.py) — or
    None (error recorded in voxel_state.cuda_error, drawn as status).
    The shared LUT and per-view output image live on the TENSOR's device;
    a pinned host buffer carries the image over, and `cuda_image`
    is the GL texture it lands in (all re-made only when size/device/LUT
    change)."""
    import torch
    from meltygui.view import voxel_cuda_view
    dev = cv.view.device
    W, H = int(width), int(height)
    try:
        out = gl_state.get("cuda_out",
                           lambda: torch.empty(H, W, 4, dtype=torch.float16, device=dev),
                           deps=(W, H, str(dev)))
        if lut_texture is None:
            from meltygui.model.lut_model import LutTexture, make_luts, lut_values
            lut_texture = gl_state.get(('cuda_lut_proxy', str(lut)),
                lambda: LutTexture(lut_values(make_luts(), lut)))
        lut_t = lut_texture.cuda(dev)
        # shading params ride one small device array, re-uploaded only when
        # a value changes (deps = the values themselves)
        shade_list = list(shade) if shade is not None else voxel_cuda_view.shade_params()
        shade_t = gl_state.get("cuda_shade",
                               lambda: torch.tensor(shade_list, dtype=torch.float32, device=dev),
                               deps=(tuple(shade_list), str(dev)))
        # Shading mip: baked once per volume version (one full volume read),
        # then every shading tap reads the few-MB dense copy instead of the
        # strided source - keeping shading cost independent of tensor size.
        _transfer = (float(cam["threshold"]), float(cam["density"]),
                     float(cam["brightness"]), float(cam["contrast"]),
                     bool(cam["centered"]))
        # The (j, k) mip is the colour march's TRAVERSAL grid now (two-level
        # DDA skips empty cells through it) as well as self-shading's light
        # field; it bakes OPACITY under the current transfer, so the key
        # includes it. Always baked on the cuda path.
        mip = gl_state.get(
            "cuda_mip",
            lambda: voxel_cuda_view.build_mip(
                cv.view, display_shape=cv.shape, nf=cv.nf, norm=cv.norm,
                threshold=_transfer[0], density=_transfer[1],
                brightness=_transfer[2], contrast=_transfer[3],
                centered=_transfer[4]),
            deps=(cv._vol_key, cv.shape, str(dev), _transfer))
        # Floor map is a BAKED full-res map - the plane's shadow depends on
        # the volume/light/transfer, never the camera, so it re-bakes only
        # when those change (slice slider, tensor version, light or
        # brightness edit), and orbiting reads it for free. Softness happens
        # live (the blur is map taps at render time).
        floor_map, floor_R = None, (0.0, 0.0)
        if shade_list[0] > 0.5 and shade_list[7] > 0.5:    # draw_floor
            _light = (tuple(shade_list[9:12]), float(shade_list[6]))  # pos, side
            vsc = cam["volume_scale"]
            floor_R = voxel_cuda_view.floor_map_extent(vsc)
            floor_map = gl_state.get(
                "cuda_floor",
                lambda: voxel_cuda_view.build_floor_map(
                    cv.view, display_shape=cv.shape, volume_scale=vsc,
                    nf=cv.nf, norm=cv.norm,
                    threshold=_transfer[0], density=_transfer[1],
                    brightness=_transfer[2], contrast=_transfer[3],
                    centered=_transfer[4],
                    light_pos=_light[0], plane_side=_light[1]),
                deps=(cv._vol_key, cv.shape, str(dev), _transfer, _light,
                      tuple(round(float(v), 5) for v in vsc)))
        voxel_cuda_view.march(cv.view, out, lut_t, display_shape=cv.shape, nf=cv.nf,
                         norm=cv.norm, aspect=W / H, shade=shade_t, mip=mip,
                         floor_map=floor_map, floor_extent=floor_R, **cam)
        img = _upload_cuda_image(gl_state, out)
        voxel_state.cuda_error = None
        return img
    except Exception as e:
        msg = f"cuda_march failed: {e}"
        if msg != voxel_state.cuda_error:
            print(f"[voxels] {msg}")
            print_stack_trace()
        voxel_state.cuda_error = msg
        return None
