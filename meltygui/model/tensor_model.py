"""Tensor types and data transformations, independent of rendering and GL."""

import math

import numpy as np

# Display-axis position in a sliced (z, y, x) volume.
_AXIS_POS = {"z": 0, "y": 1, "x": 2}



class TensorDim(int):
    """A tensor dim index that is still an int everywhere it matters
    (indexing, comparisons, arithmetic, `int()`, pickling) but carries its own
    TYPE, so meltygui routes it to its own renderer instead of the plain int one
    — a dim picker rather than a number field.

    Values only stay TensorDim if whatever writes them keeps the type: a
    renderer registered `@render_func(is_default_for=TensorDim)` should return
    TensorDim(...), otherwise the first edit stores a plain int and the row
    falls back to the int renderer."""

    __slots__ = ()

    def __repr__(self):
        return f"TensorDim({int(self)})"


class TensorDims(tuple):
    """A SET of tensor dim indices (`mean_dims`) — tuple everywhere it
    matters, but typed so it routes to the same dim picker as TensorDim
    (multi-select tabs). A tuple needs SOME type to route by; this is the
    minimal one, and the renderer is shared."""

    __slots__ = ()

    def __repr__(self):
        return f"TensorDims({tuple(int(v) for v in self)})"


from meltygui.model.lut_model import Lut


def _clean_dim_name(x, i):
    """A dim name is a short single-line LABEL, whatever lands in the list —
    DnD/paste can drop arbitrary objects whose str() is a multi-KB code repr,
    and one of those blows up every radio row and billboard bake."""
    first = (str(x).splitlines() or [""])[0].strip()
    return first[:48] if first else f"dim{i}"


def _collection_dim_labels(col):
    """Dim-name labels from the collection's `dim_names` entry; [] when no
    names are in reach."""
    raw_names = col.get("dim_names", ()) if col is not None else ()
    return [_clean_dim_name(x, i) for i, x in enumerate(raw_names or ())]


def _resolve_dim(dim_names, v, n):
    """A dim given by INDEX or by NAME (resolved through dim_names); None
    stays None, out-of-range collapses to None."""
    if v is None:
        return None
    if isinstance(v, str):
        names = list(dim_names or ())
        if v not in names:
            return None
        v = names.index(v)
    try:
        v = int(v)
    except (TypeError, ValueError):
        # params are user-editable from the panel and from source, so a dim
        # can arrive as anything at all. Unusable = unset.
        return None
    return v if 0 <= v < n else None


def _resolve_axes(shape, dim_names, x_dim, y_dim, z_dim):
    """(z, y, x) display dims for a shape: dims by index or NAME, None
    derives the default (last three dims → z/y/x, like the old viewer).

    ALWAYS returns three DISTINCT in-range dims (for n >= 3) — the params are
    editable from the panel and from source, so two axes can name the same dim
    or a garbage one. A dim already claimed by an earlier axis is treated as
    unset and re-derived, which keeps the slicing downstream well-formed
    (duplicate picks collapse the sliced volume to 2 dims and the permute
    blows up). z wins over y wins over x, so the LAST axis you retarget onto a
    taken dim is the one that moves."""
    n = len(shape)
    resolved = []
    taken = set()
    for cur in (z_dim, y_dim, x_dim):
        d = _resolve_dim(dim_names, cur, n)
        if d is None or d in taken:
            resolved.append(None)       # unset, or a duplicate: re-derive
        else:
            taken.add(d)
            resolved.append(d)

    def fill(default):
        # The default dim, else the nearest free one scanning down then up.
        # (The old walk stopped at 0 and could hand back a taken 0.)
        if default not in taken:
            return default
        for d in range(default - 1, -1, -1):
            if d not in taken:
                return d
        for d in range(default + 1, n):
            if d not in taken:
                return d
        return default                  # n < 3: nothing free left

    for i, default in enumerate((max(0, n - 3), max(0, n - 2), max(0, n - 1))):
        if resolved[i] is None:
            resolved[i] = fill(default)
            taken.add(resolved[i])
    return tuple(resolved)


def to_display_dtype(t):
    """Coerce ANY torch tensor into something the raymarcher can sample:
    a dense, real, float16/float32 tensor. float16/32 pass through untouched
    (they upload as R16F/R32F with no copy); every other dtype maps to
    float32 by meaning, not by bit pattern — complex → magnitude, bool →
    0/1, ints/uints → their values, float64/bfloat16 → narrowed (bfloat16
    must NOT go to float16: its exponent range overflows). Quantized tensors
    dequantize, sparse layouts densify. Raises ValueError with a readable
    reason for anything that can't become a real float volume."""
    import torch
    if t.is_quantized:
        t = t.dequantize()
    if t.layout != torch.strided:
        try:
            t = t.to_dense()
        except Exception as e:
            raise ValueError(f"cannot densify {t.layout} tensor: {e}") from e
    if t.dtype in (torch.float16, torch.float32):
        return t
    if t.is_complex():
        return t.abs().float()
    if t.dtype == torch.bool or not t.is_floating_point():
        return t.to(torch.float32)          # bool, int8..int64, uint8..
    try:
        return t.float()                    # float64, bfloat16, float8_*...
    except Exception as e:
        raise ValueError(f"unsupported tensor dtype {t.dtype}: {e}") from e


def _display_view_dtype(t):
    """Keep supported CUDA dtypes/layouts intact; reject implicit conversions.
    CPU reference paths can still materialize sparse/complex/quantized inputs.
    bf16/ints/bool/f64 decode in the CUDA kernel without an f32 copy."""
    import torch
    if t.is_cuda and (t.is_quantized or t.layout != torch.strided or t.is_complex()):
        raise ValueError("Direct CUDA rendering requires a dense real-valued tensor; "
                         "convert explicitly in user code to visualize this value.")
    if t.is_quantized:
        t = t.dequantize()
    if t.layout != torch.strided:
        try:
            t = t.to_dense()
        except Exception as e:
            raise ValueError(f"cannot densify {t.layout} tensor: {e}") from e
    if t.is_complex():
        t = t.abs().float()
    return t


def _slice_core(t, dim_names, x_dim, y_dim, z_dim, slices, mean_dims, sort_dim,
                nf_on, nf_chop, nf_along, materialize):
    """Shared slice logic: tensor → (z, y, x) volume as a VIEW (no
    contiguous() — `materialize` decides the dtype pre-pass), plus the
    mapping, source shape and the resolved neural-flow axes (positions in
    the (z, y, x) volume, None = off)."""
    import torch
    t = (to_display_dtype if materialize else _display_view_dtype)(t.detach())
    if t.numel() == 0:
        raise ValueError(f"empty tensor (shape {tuple(t.shape)}) — nothing to display")
    while t.dim() < 3:
        t = t.unsqueeze(0)
    n = t.dim()
    shape = tuple(int(s) for s in t.shape)
    zd, yd, xd = _resolve_axes(shape, dim_names, x_dim, y_dim, z_dim)
    if 0 <= int(sort_dim) < n:
        t = torch.sort(t, dim=int(sort_dim), descending=True).values
    picked = (zd, yd, xd)
    mean_set = {int(d) for d in (mean_dims or ()) if 0 <= int(d) < n}
    for d in mean_set:
        # f32 accumulate + result regardless of the input dtype (int inputs
        # need it; bf16 inputs would otherwise round the mean - the view
        # path must match the materialized one bit for bit).
        m = t.mean(dim=d, keepdim=True, dtype=torch.float32)
        # A DISPLAYED dim keeps its extent with the mean BROADCAST along it
        # (the same value repeats across the plot - visual convenience);
        # an unmapped dim stays collapsed and pins at 0 below.
        t = m.expand(t.shape) if d in picked else m
    def _pin(d):
        # A pinned index from `slices` can be anything the panel/source lets;
        # clamp into range instead of letting torch raise (or silently wrap on
        # a negative).
        try:
            v = int(slices[d]) if d < len(slices) else 0
        except (TypeError, ValueError):
            v = 0
        return max(0, min(v, shape[d] - 1))

    index = tuple(
        slice(None) if d in picked
        else (0 if d in mean_set else _pin(d))
        for d in range(n))
    sub = t[index]  # picked 3 dims keep original order
    remaining = sorted(picked)
    vol = sub.permute(remaining.index(zd), remaining.index(yd),
                      remaining.index(xd))
    chop = along = None
    if nf_on:
        # Flow is pinned to TENSOR DIMS (remapping x/y/z never changes WHICH
        # data gets chopped); unset dims default to chop=x, along=z. A chop
        # or along dim that isn't mapped makes it a no-op.
        chop_d = _resolve_dim(dim_names, nf_chop, n)
        along_d = _resolve_dim(dim_names, nf_along, n)
        dim_to_axis = {xd: "x", yd: "y", zd: "z"}
        chop = dim_to_axis.get(xd if chop_d is None else chop_d)
        along = dim_to_axis.get(zd if along_d is None else along_d)
        if not (chop and along and chop != along):
            chop = along = None
    return vol, (zd, yd, xd), shape, chop, along


def slice_volume(t, dim_names=(), x_dim=None, y_dim=None, z_dim=None,
                 slices=(), mean_dims=(), sort_dim=-1, normalize=False,
                 nf_on=False, nf_chop=None, nf_along=None, nf_chunk=128,
                 nf_pad=False):
    """tensor → (depth, height, width) display volume, PURE: every choice
    arrives as an argument (the draw_voxels params), nothing is stored.
    Unmapped dims pin to their `slices` index (missing entries → 0) or
    average when listed in mean_dims (keepdim, then pinned at 0); a
    DISPLAYED dim in mean_dims keeps its extent with the mean broadcast
    along it (the value repeats across the plot); sort
    orders fibers along a dim; normalize min-max stretches the DISPLAYED
    volume (signed data scales by max-magnitude so zero stays anchored).
    Stays on t's device. Returns (vol3, (z_dim, y_dim, x_dim), shape)."""
    import torch
    vol, mapping, shape, chop, along = _slice_core(
        t, dim_names, x_dim, y_dim, z_dim, slices, mean_dims, sort_dim,
        nf_on, nf_chop, nf_along, materialize=True)
    vol = vol.contiguous()
    if chop is not None:
        vol = neural_flow_volume(vol, chop, along, int(nf_chunk), pad=nf_pad)
    if normalize:
        lo, hi = vol.min(), vol.max()
        if lo < 0:
            vol = vol / (torch.maximum(hi.abs(), lo.abs()) + 1e-12)
        else:
            vol = (vol - lo) / (hi - lo + 1e-12)
    return vol, mapping, shape


class CudaVolumeView:
    """The cuda_march stand-in for the volume GLTexture: NO GL object — the
    kernel samples `view` (a strided (z, y, x) torch view of the source, on
    whatever GPU it lives) in place. Carries the same metadata draw_voxels
    reads off a volume texture (`shape` = DISPLAYED extents after neural
    flow, source_shape, mapping, clamp_note) plus the kernel's sampling
    facts: `nf` = (chop_axis, along_axis, chunk) with axes 0=z 1=y 2=x
    (chop -1 = off) and `norm` = (lo, hi, mode)."""

    __slots__ = ("view", "shape", "nf", "norm", "source_shape", "source_ndim",
                 "mapping", "clamp_note", "_vol_key", "dim_names")

    def __init__(self, view, shape, nf, norm, mapping, source_shape):
        self.view, self.shape, self.nf, self.norm = view, tuple(shape), nf, norm
        self.mapping, self.source_shape = mapping, tuple(source_shape)
        self.source_ndim = len(source_shape)
        self.clamp_note = None
        self._vol_key = None
        self.dim_names = ()

    def __repr__(self):
        return f"CudaVolumeView({self.shape} of {tuple(self.view.shape)} on {self.view.device})"


def slice_volume_view(t, dim_names=(), x_dim=None, y_dim=None, z_dim=None,
                      slices=(), mean_dims=(), sort_dim=-1, normalize=False,
                      nf_on=False, nf_chop=None, nf_along=None, nf_chunk=128,
                      nf_pad=False):
    """slice_volume for the cuda_march path: the same choices, but the result
    is a CudaVolumeView over a strided VIEW of the source — no contiguous(),
    no dtype copy, neural flow as in-kernel index math, normalize as a
    (lo, hi) pair the kernel applies per sample. Only sort/mean (genuine
    transforms) and densify/complex materialize anything; those run once
    per vol_key like everything else behind draw_voxels' cache gate."""
    vol, mapping, shape, chop, along = _slice_core(
        t, dim_names, x_dim, y_dim, z_dim, slices, mean_dims, sort_dim,
        nf_on, nf_chop, nf_along, materialize=False)
    display_shape, nf = tuple(int(s) for s in vol.shape), (-1, -1, 0)
    if chop is not None:
        display_shape, nf = nf_display_shape(
            vol.shape, _AXIS_POS[chop], _AXIS_POS[along], int(nf_chunk), pad=nf_pad)
    norm = (0.0, 1.0, 0)
    if normalize:
        lo, hi = float(vol.min()), float(vol.max())
        norm = (lo, max(abs(hi), abs(lo)), 2) if lo < 0 else (lo, hi, 1)
    return CudaVolumeView(vol, display_shape, nf, norm, mapping, shape)


def _volume_scale(shape):
    """Box extents per axis for a (depth, height, width) = (z, y, x) volume,
    proportional to voxel counts (longest axis = 1), so every voxel renders
    as a CUBE and a (4, 32, 48) tensor reads as a flat slab. Returned as the
    shader's (x, y, z) order. No visibility floor: an earlier max(0.02, …)
    per axis inflated the short side of anything past 50:1 (a (2048, 16)
    time tensor drew its 16-voxel side 2.5× too wide). Thin slabs don't need
    it — opacity accumulates in volume-NORMALIZED segment lengths, so a
    1-voxel dim still reads at full density. The epsilon only guards the
    `/ volume_scale` divisions (labels/silhouette use the same scale)."""
    t_depth, t_height, t_width = (max(1, int(s)) for s in shape)
    longest = float(max(t_depth, t_height, t_width))
    return (max(1e-5, t_width / longest),
            max(1e-5, t_height / longest),
            max(1e-5, t_depth / longest))


def neural_flow_volume(vol, chop_axis, along_axis, chunk, pad=False):
    """The old viewer's neural flow on the DISPLAY volume: chop one axis into
    `chunk`-wide blocks and concatenate them group-major along another —
    identical layout to the original get_neural_flow's j*orig+i ordering,
    which is exactly cat(split). No-op when the axes coincide, or when the
    chop doesn't divide evenly — unless `pad`, which zero-fills the chop
    axis up to the next multiple first (the auto-wrap path: any chunk must
    work, a ragged last block is fine)."""
    chop, along = _AXIS_POS[chop_axis], _AXIS_POS[along_axis]
    size = int(vol.shape[chop])
    if chop == along or chunk <= 0 or size <= chunk:
        return vol
    import torch
    if size % chunk != 0:
        if not pad:
            return vol
        extra = chunk - size % chunk
        # F.pad's (before, after) pairs run from the last dim backwards.
        spec = [0, 0] * (vol.dim() - 1 - chop) + [0, extra]
        vol = torch.nn.functional.pad(vol, spec)
    return torch.cat(vol.split(chunk, dim=chop), dim=along).contiguous()


def auto_neural_flow(shape, dim_names, x_dim, y_dim, z_dim, max_extent):
    """The auto-wrap decision for a DISPLAYED axis longer than `max_extent`
    (the GL limit, or the user's readability cap): returns
    (chop_dim, along_dim, chunk) tensor-dim indices for neural flow, or None
    when every displayed extent fits. Chops the LONGEST over-limit axis into
    ~sqrt-sized chunks — the smallest divisor >= sqrt(size) when one exists
    below the limit, else ceil(sqrt) with padding — and lays the blocks along
    the SHORTEST other displayed axis (a (1, 32000) row becomes a ~180x180
    slab). One pass only; anything still over-limit afterwards clamps."""
    shape = tuple(int(s) for s in shape)
    shape = (1,) * (3 - len(shape)) + shape if len(shape) < 3 else shape
    if max_extent <= 0:
        return None
    zd, yd, xd = _resolve_axes(shape, dim_names, x_dim, y_dim, z_dim)
    shown = (zd, yd, xd)
    over = [d for d in shown if shape[d] > max_extent]
    if not over:
        return None
    chop = max(over, key=lambda d: shape[d])
    along = min((d for d in shown if d != chop), key=lambda d: shape[d])
    size = shape[chop]
    root = int(math.ceil(math.sqrt(size)))
    chunk = next((c for c in range(root, min(size, max_extent) + 1) if size % c == 0),
                 root)
    return chop, along, chunk


def _is_tensorish(v):
    """A torch tensor / ndarray, or a container whose top level holds one."""
    if isinstance(v, np.ndarray):
        return True
    if type(v).__module__.startswith("torch") and hasattr(v, "data_ptr"):
        return True
    if isinstance(v, (list, tuple)):
        return any(_is_tensorish(x) for x in v)
    if isinstance(v, dict):
        return any(_is_tensorish(x) for x in v.values())
    return False


def nf_display_shape(src_shape, chop_axis, along_axis, chunk, pad=True):
    """Displayed (z, y, x) extents after neural flow of a (z, y, x) source
    view — the cat(split) layout: chop axis → chunk, along axis →
    along * ceil(size/chunk) (the ragged last block zero-padded in the
    sampler). Axes are 0=z 1=y 2=x; chop -1 = off. Mirrors
    neural_flow_volume: a chunk that doesn't divide the axis is a no-op
    unless `pad`."""
    shape = list(int(s) for s in src_shape)
    if chop_axis < 0 or chop_axis == along_axis or chunk <= 0 or shape[chop_axis] <= chunk:
        return tuple(shape), (-1, -1, 0)
    if shape[chop_axis] % chunk != 0 and not pad:
        return tuple(shape), (-1, -1, 0)
    blocks = -(-shape[chop_axis] // chunk)
    shape[along_axis] *= blocks
    shape[chop_axis] = chunk
    return tuple(shape), (chop_axis, along_axis, chunk)
