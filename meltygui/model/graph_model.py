"""Graph data preparation for reusable line views."""

from meltygui.model.tensor_model import _resolve_dim
from meltygui.model.tensor_model import to_display_dtype


def _resolve_line_axes(shape, dim_names, x_dim, line_dim):
    """(x, line) tensor dims for a shape: by index or NAME, None / garbage /
    duplicates derive — x = second-to-last (the only dim for 1-D), lines =
    last. Returns line=None when there is no series axis (1-D input, or
    every other dim is claimed)."""
    n = len(shape)
    x = _resolve_dim(dim_names, x_dim, n)
    line = _resolve_dim(dim_names, line_dim, n)
    if x is None:
        x = n - 2 if n >= 2 else 0
        if line is not None and x == line:
            x = n - 1
    if line is not None and line == x:
        line = None
    if line is None and n >= 2:
        line = n - 1 if x != n - 1 else n - 2
    return x, line


def slice_lines(t, dim_names=(), x_dim=None, line_dim=None, slices=(),
                mean_dims=(), normalize=False, materialize=True):
    """tensor → (lines, samples) 2-D display matrix, PURE (every choice is an
    argument, nothing stored). Unmapped dims pin to their `slices` index
    (missing → 0) or average when in mean_dims; `normalize` min-max scales
    EACH LINE to [0, 1] (compare shapes, not magnitudes). Stays on t's
    device. Returns (lines2d, (x_dim, line_dim|None), shape)."""
    import torch
    from meltygui.model.tensor_model import _display_view_dtype
    t = (to_display_dtype if materialize else _display_view_dtype)(t.detach())
    if t.numel() == 0:
        raise ValueError(f"empty tensor (shape {tuple(t.shape)}) — nothing to plot")
    if t.dim() == 0:
        t = t.unsqueeze(0)
    n = t.dim()
    shape = tuple(int(s) for s in t.shape)
    xd, ld = _resolve_line_axes(shape, dim_names, x_dim, line_dim)
    picked = {xd} | ({ld} if ld is not None else set())
    mean_set = {int(d) for d in (mean_dims or ()) if 0 <= int(d) < n}
    for d in mean_set:
        m = t.mean(dim=d, keepdim=True)
        t = m.expand(t.shape) if d in picked else m

    def _pin(d):
        try:
            v = int(slices[d]) if d < len(slices) else 0
        except (TypeError, ValueError):
            v = 0
        return max(0, min(v, shape[d] - 1))

    index = tuple(slice(None) if d in picked
                  else (0 if d in mean_set else _pin(d))
                  for d in range(n))
    sub = t[index]
    if ld is None:
        lines = sub.reshape(1, -1)
    else:
        lines = sub.transpose(0, 1) if ld > xd else sub   # → (line, sample)
    if materialize:
        lines = lines.contiguous()
    if normalize and materialize:
        lo = lines.amin(dim=1, keepdim=True)
        hi = lines.amax(dim=1, keepdim=True)
        lines = (lines - lo) / (hi - lo + 1e-12)
    return lines, (xd, ld), shape


def pack_series(lines, max_w):
    """(lines, samples) → (lines, rows, W) volume for the series texture:
    W = min(samples, max_w), rows = ceil(samples / W), zero-padded tail.
    The shader never reads past n_samples, so the pad is inert."""
    import torch
    n_lines, n = (int(s) for s in lines.shape)
    w = max(1, min(n, int(max_w)))
    rows = (n + w - 1) // w
    if rows * w != n:
        lines = torch.nn.functional.pad(lines, (0, rows * w - n))
    return lines.reshape(n_lines, rows, w).contiguous(), w


def _finite_range(lines):
    """(min, max) over the FINITE values, (0, 1) when there are none, and a
    non-degenerate span for constant data (so a flat line sits mid-plot)."""
    import torch
    finite = lines[torch.isfinite(lines)]
    if finite.numel() == 0:
        return 0.0, 1.0
    lo, hi = torch.aminmax(finite)
    lo, hi = float(lo), float(hi)
    if hi - lo < 1e-12:
        pad = abs(lo) * 0.5 or 0.5
        return lo - pad, hi + pad
    return lo, hi
