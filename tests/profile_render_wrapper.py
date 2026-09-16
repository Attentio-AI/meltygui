"""Profile @render_func wrapper overhead vs a plain function, per nesting depth.

Blit/tile cache is DISABLED (TileCacheMasked.enabled=False), matching the
"blit off, wrapper still slow" scenario. Headless imgui context, no GLFW.

Passes:
  A. depth sweep   — plain nested calls vs render_func nested views,
                     per-level EXCLUSIVE wrapper overhead (outer span minus
                     body span at each level)
  B. cProfile      — hot functions at a deep nesting
  C. line_profiler — per-line cost inside the wrapper closure itself

Run:  venv/bin/python tests/profile_render_wrapper.py
"""

import argparse
import cProfile
import io as iolib
import os
import pstats
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import meltygui_imgui as imgui

imgui.create_context()
_io = imgui.get_io()
_io.display_size = (1920, 1080)
_io.delta_time = 1.0 / 60.0
_io.fonts.get_tex_data_as_rgba32()

from meltygui.melty import Melty
import meltygui.rendering.core_render as core_render
from meltygui.rendering.core_render import render_func
from meltygui.views.blit_offscreen import TileCacheMasked
from meltygui.state.new_core_model import DrawState

PC = time.perf_counter_ns
MAXD = 64


# ── Melty setup (mirrors tests/test_render_func_integration._init_melty) ──

class _StyleStub:
    accessed = set()

    def get_tint(self):
        return (0.0, 0.0, 0.0)

    def set_imgui_tint(self, *a, **k):
        pass

    def __getattr__(self, name):
        _StyleStub.accessed.add(name)

        def _noop(*a, **k):
            return None

        return _noop


class _NS:
    pass


HOST_DS = None


def _init_melty():
    global HOST_DS
    if Melty.cache is None:
        Melty.cache = TileCacheMasked()
    Melty.cache.enabled = False  # blit OFF - isolate wrapper overhead
    Melty.frame_count = 0
    Melty.annotation_mode = False
    Melty.on_drag = False
    Melty.window_drag = False
    Melty.imgui_active = False
    Melty.imgui_popup_open = False
    Melty.imgui_any_item_active = False
    Melty.imgui_main_window_hovered = True

    style = imgui.get_style()
    if Melty.original_spacing is None:
        Melty.original_spacing = style.item_spacing
        Melty.original_window_padding = style.window_padding
        Melty.original_frame_padding = style.frame_padding

    try:
        from meltygui.views.utils.imgui_style_manager_class import ImGuiStyleManager
        Melty.style_manager = ImGuiStyleManager()
    except Exception as e:
        print(f"(real ImGuiStyleManager unavailable, using stub: {e})")
        Melty.style_manager = _StyleStub()
    Melty.global_attrs['style_manager'] = Melty.style_manager

    vis = _NS()
    vis.root = _NS()
    vis.root.draw_state_registry = {}
    Melty.vis = vis
    Melty.draw_state_registry = vis.root.draw_state_registry

    # Host meltygui-window so views get a parent_window (like in the real app)
    HOST_DS = DrawState()
    HOST_DS.name = "ProfileHost"
    HOST_DS.left_offset = 0
    HOST_DS.top_offset = 0
    HOST_DS.window_pos = (0, 0)
    HOST_DS.width = 1800
    HOST_DS.height = 1000
    HOST_DS.melty_window = True
    HOST_DS.layer = 0

    _tick()


def _tick():
    M = Melty
    M.frame_count += 1
    M.depth = 0
    M.shadow_depth = 0
    M.z_pos = 0
    M.active_layer = 0
    M.channels_split = False
    M.unique_stack = []
    M.suffix_stack = []
    M.mode_stack = []
    M.draw_state_stack = []
    M.input_value_stack = [None]
    M.size_stack = []
    M.wrap_stack = []
    M.bg_stack = []
    M.bg_color_stack = []
    M.collection_stack = []
    M.collection_index_stack = []
    M.fixed_size_stack = []
    M.clip_stack = []
    M.all_uniques = set()
    M.seen_unique = set()
    M.seen_values = []
    M.indent_count = 0
    M.unindent_count = 0
    M.bg_depth = 0
    M.detached = False
    M.silence_invalidate = False
    M.nested_collections = 0
    M.last_draw_state = [(None, None)] * M.max_layer
    M.melty_window_stack = [HOST_DS]


# ── twin nested functions ─────────────────────────────────────────────────────

LEVEL_NAMES = [f"lvl{i}" for i in range(MAXD)]

rf_outer = [0] * MAXD   # span of the rf call at level d, measured by parent
rf_body = [0] * MAXD    # span inside the func body at level d
rf_calls = [0] * MAXD

pl_outer = [0] * MAXD
pl_body = [0] * MAXD
pl_calls = [0] * MAXD


def _zero(*arrays):
    for a in arrays:
        for i in range(len(a)):
            a[i] = 0


@render_func()
def rf_nest(input_value=None, draw_state=None, depth_left=0, level=0, **kwargs):
    t0 = PC()
    rf_calls[level] += 1
    if depth_left > 0:
        c0 = PC()
        rf_nest(input_value, depth_left=depth_left - 1, level=level + 1,
                key=0, name=LEVEL_NAMES[level + 1])
        rf_outer[level + 1] += PC() - c0
    rf_body[level] += PC() - t0
    return False, input_value


def pl_nest(input_value=None, draw_state=None, depth_left=0, level=0, **kwargs):
    t0 = PC()
    pl_calls[level] += 1
    if depth_left > 0:
        c0 = PC()
        pl_nest(input_value, depth_left=depth_left - 1, level=level + 1,
                key=0, name=LEVEL_NAMES[level + 1])
        pl_outer[level + 1] += PC() - c0
    pl_body[level] += PC() - t0
    return False, input_value


# ── frame driver ───────────────────────────────────────────────────────────

def run_frame(fn, depth):
    """One headless frame around a root call. Returns root span in ns."""
    imgui.new_frame()
    imgui.begin("Host")
    t0 = PC()
    fn(7, depth_left=depth, level=0, key=0, name=LEVEL_NAMES[0])
    t1 = PC()
    if Melty.channels_split:
        imgui.get_window_draw_list().channels_merge()
    imgui.end()
    imgui.end_frame()
    _tick()
    return t1 - t0


def run_frames(fn, depth, n):
    spans = []
    for _ in range(n):
        spans.append(run_frame(fn, depth))
    return spans


# ── Pass A: depth sweep ────────────────────────────────────────────────────

def pass_a(depths, warm, frames):
    print("=" * 78)
    print(f"PASS A — depth sweep, {warm} warmup + {frames} measured frames per config")
    print("  (cache/blit disabled, steady state; times are per FRAME)")
    print("=" * 78)

    rows = []
    deepest = max(depths)
    deep_excl = None

    for depth in depths:
        # plain baseline
        _zero(pl_outer, pl_body, pl_calls)
        run_frames(pl_nest, depth, warm)
        _zero(pl_outer, pl_body, pl_calls)
        pl_spans = run_frames(pl_nest, depth, frames)

        # render_func
        _zero(rf_outer, rf_body, rf_calls)
        run_frames(rf_nest, depth, warm)
        _zero(rf_outer, rf_body, rf_calls)
        rf_spans = run_frames(rf_nest, depth, frames)

        n_calls = depth + 1
        bad = [lvl for lvl in range(n_calls) if rf_calls[lvl] != frames]
        if bad:
            print(f"  WARNING depth={depth}: body call counts off at levels {bad} "
                  f"(counts={[rf_calls[l] for l in bad]}, expected {frames}) — cache skip?")

        pl_med = sorted(pl_spans)[len(pl_spans) // 2]
        rf_med = sorted(rf_spans)[len(rf_spans) // 2]
        per_call = (rf_med - pl_med) / n_calls
        rows.append((depth, n_calls, pl_med, rf_med, per_call))

        if depth == deepest:
            # exclusive wrapper overhead at each level:
            #   outer(level) - body(level); outer(0) comes from the root span
            rf_o0 = sum(rf_spans)
            pl_o0 = sum(pl_spans)
            deep_excl = []
            for lvl in range(n_calls):
                ro = rf_o0 if lvl == 0 else rf_outer[lvl]
                po = pl_o0 if lvl == 0 else pl_outer[lvl]
                rf_e = (ro - rf_body[lvl]) / frames
                pl_e = (po - pl_body[lvl]) / frames
                deep_excl.append((lvl, rf_e, pl_e))

    print(f"\n{'depth':>5} {'calls':>5} {'plain/frame':>12} {'rf/frame':>12} "
          f"{'overhead/call':>14} {'ratio':>8}")
    for depth, n_calls, pl_med, rf_med, per_call in rows:
        ratio = rf_med / pl_med if pl_med else float('inf')
        print(f"{depth:>5} {n_calls:>5} {pl_med / 1e3:>10.1f}us {rf_med / 1e3:>10.1f}us "
              f"{per_call / 1e3:>12.1f}us {ratio:>7.0f}x")

    if deep_excl:
        print(f"\nPer-level EXCLUSIVE wrapper overhead at depth={deepest} "
              f"(median-frame basis, us per call):")
        print(f"{'level':>5} {'rf wrapper us':>14} {'plain us':>10}")
        for lvl, rf_e, pl_e in deep_excl:
            bar = '#' * max(0, int(rf_e / 1e3 / 5))
            print(f"{lvl:>5} {rf_e / 1e3:>12.1f}  {pl_e / 1e3:>8.2f}  {bar}")
        # linear fit: overhead(level) = a + b*level
        n = len(deep_excl)
        xs = [r[0] for r in deep_excl]
        ys = [r[1] / 1e3 for r in deep_excl]
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs) or 1
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        a = my - b * mx
        print(f"\n  fit: wrapper_overhead(level) ~= {a:.1f}us + {b:.2f}us * level"
              f"  -> {'grows with depth (O(depth) work per call!)' if b > max(0.5, 0.05 * a) else 'roughly flat per call'}")

    print(f"\n  draw_state registry size: {len(Melty.draw_state_registry)}")
    if _StyleStub.accessed:
        print(f"  style stub fallback attrs hit: {sorted(_StyleStub.accessed)}")
    return rows


# ── Pass B: cProfile ───────────────────────────────────────────────────────

def pass_b(depth, frames):
    print("\n" + "=" * 78)
    print(f"PASS B — cProfile, depth={depth}, {frames} frames")
    print("=" * 78)
    run_frames(rf_nest, depth, 5)  # warm
    pr = cProfile.Profile()
    pr.enable()
    run_frames(rf_nest, depth, frames)
    pr.disable()

    for sort in ('tottime', 'cumtime'):
        buf = iolib.StringIO()
        ps = pstats.Stats(pr, stream=buf)
        ps.strip_dirs().sort_stats(sort).print_stats(28)
        text = buf.getvalue()
        # strip pstats preamble lines
        lines = text.splitlines()
        start = next(i for i, l in enumerate(lines) if 'ncalls' in l)
        print(f"\n--- top by {sort} ---")
        print('\n'.join(lines[start - 1:start + 30]))


# ── Pass C: line_profiler ──────────────────────────────────────────────────

def _lp_add(lp, obj, label):
    try:
        if isinstance(obj, property):
            obj = obj.fget
        obj = getattr(obj, '__func__', obj)
        lp.add_function(obj)
        return True
    except Exception as e:
        print(f"  (line_profiler: could not add {label}: {e})")
        return False


def pass_c(depth, frames, pct_threshold):
    from line_profiler import LineProfiler
    print("\n" + "=" * 78)
    print(f"PASS C — line_profiler, depth={depth}, {frames} frames "
          f"(lines >= {pct_threshold}% of their function)")
    print("    NOTE: tracing inflates absolute times; trust the %% column.")
    print("=" * 78)

    lp = LineProfiler()
    _lp_add(lp, rf_nest, "rf_nest wrapper")
    _lp_add(lp, DrawState.__dict__.get('_ancestor_scroll'), "_ancestor_scroll")
    _lp_add(lp, DrawState.__dict__.get('abs_left'), "abs_left")
    _lp_add(lp, DrawState.__dict__.get('abs_top'), "abs_top")
    _lp_add(lp, DrawState.__dict__.get('pos_changed'), "pos_changed")
    _lp_add(lp, core_render.get_draw_state, "get_draw_state")
    _lp_add(lp, core_render.ui_id, "ui_id")
    bvh_update = getattr(Melty, 'bvh_update', None)
    if bvh_update is not None:
        _lp_add(lp, bvh_update, "Melty.bvh_update")
    bvh_sync = getattr(DrawState, 'bvh_sync', None) or getattr(Melty, 'bvh_sync', None)
    if bvh_sync is not None:
        _lp_add(lp, bvh_sync if not isinstance(bvh_sync, property) else bvh_sync, "bvh_sync")

    run_frames(rf_nest, depth, 3)  # warm
    lp.enable_by_count()
    run_frames(rf_nest, depth, frames)
    lp.disable_by_count()

    buf = iolib.StringIO()
    lp.print_stats(stream=buf)
    full = buf.getvalue()
    out_path = '/tmp/render_wrapper_lineprofile.txt'
    with open(out_path, 'w') as f:
        f.write(full)

    # condense, keep section headers + lines above the % threshold
    import re
    row_re = re.compile(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s(.*)$")
    keep_prefixes = ('Total time:', 'File:', 'Function:', 'Timer unit:')
    current_header = []
    printed_header = False
    for line in full.splitlines():
        if line.startswith(('Total time:', 'File:', 'Function:')):
            if line.startswith('Total time:'):
                current_header = [line]
                printed_header = False
            else:
                current_header.append(line)
            continue
        m = row_re.match(line)
        if m and float(m.group(5)) >= pct_threshold:
            if not printed_header:
                print('\n' + '\n'.join(current_header))
                printed_header = True
            src = m.group(6).strip()
            print(f"  line {m.group(1):>5}  hits={m.group(2):>7}  "
                  f"{float(m.group(3)) / 1e6:>8.1f}ms  {m.group(5):>5}%%  | {src[:90]}")
    print(f"\n  full line profile written to {out_path}")


# ── Pass D: ablations - patch out various mechanisms, re-measure ──────────

def _measure_per_call(depth, warm, frames):
    _zero(pl_outer, pl_body, pl_calls)
    run_frames(pl_nest, depth, warm)
    _zero(pl_outer, pl_body, pl_calls)
    pl_spans = run_frames(pl_nest, depth, frames)
    _zero(rf_outer, rf_body, rf_calls)
    run_frames(rf_nest, depth, warm)
    _zero(rf_outer, rf_body, rf_calls)
    rf_spans = run_frames(rf_nest, depth, frames)
    pl_med = sorted(pl_spans)[len(pl_spans) // 2]
    rf_med = sorted(rf_spans)[len(rf_spans) // 2]
    return (rf_med - pl_med) / (depth + 1), rf_med


def pass_d(depth, warm, frames):
    print("\n" + "=" * 78)
    print(f"PASS D — ablations at depth={depth} (per-call wrapper overhead, untraced)")
    print("=" * 78)

    orig_setattr = DrawState.__setattr__
    orig_walk = DrawState._ancestor_scroll

    variants = []

    base, base_frame = _measure_per_call(depth, warm, frames)
    variants.append(("baseline", base, base_frame))

    DrawState.__setattr__ = object.__setattr__
    v, fr = _measure_per_call(depth, warm, frames)
    variants.append(("no @live setattr instrumentation", v, fr))
    DrawState.__setattr__ = orig_setattr

    DrawState._ancestor_scroll = lambda self: (0, 0)
    v, fr = _measure_per_call(depth, warm, frames)
    variants.append(("no _ancestor_scroll walks", v, fr))
    DrawState._ancestor_scroll = orig_walk

    DrawState.__setattr__ = object.__setattr__
    DrawState._ancestor_scroll = lambda self: (0, 0)
    v, fr = _measure_per_call(depth, warm, frames)
    variants.append(("both removed", v, fr))
    DrawState.__setattr__ = orig_setattr
    DrawState._ancestor_scroll = orig_walk

    print(f"\n{'variant':<36} {'per call':>10} {'saved':>9} {'frame':>10}")
    for name, v, fr in variants:
        print(f"{name:<36} {v / 1e3:>8.1f}us {(base - v) / 1e3:>7.1f}us {fr / 1e3:>8.1f}us")


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--depths', default='1,2,4,8,12,16,24')
    ap.add_argument('--frames', type=int, default=60)
    ap.add_argument('--warm', type=int, default=10)
    ap.add_argument('--profile-depth', type=int, default=16)
    ap.add_argument('--cprofile-frames', type=int, default=40)
    ap.add_argument('--line-frames', type=int, default=12)
    ap.add_argument('--line-pct', type=float, default=1.0)
    ap.add_argument('--validate', action='store_true',
                    help='3 frames at depth 3 with full tracebacks, then exit')
    ap.add_argument('--ablate-only', action='store_true')
    ap.add_argument('--line-only', action='store_true')
    args = ap.parse_args()

    _init_melty()

    if args.validate:
        _zero(rf_calls, rf_outer, rf_body)
        for i in range(3):
            span = run_frame(rf_nest, 3)
            print(f"frame {i}: root span {span / 1e3:.0f}us, "
                  f"calls per level {[rf_calls[l] for l in range(4)]}")
        print(f"registry: {len(Melty.draw_state_registry)} draw_states")
        return

    depths = [int(d) for d in args.depths.split(',')]
    if args.ablate_only:
        pass_d(args.profile_depth, args.warm, args.frames)
        return
    if args.line_only:
        pass_c(args.profile_depth, args.line_frames, args.line_pct)
        return
    pass_a(depths, args.warm, args.frames)
    pass_b(args.profile_depth, args.cprofile_frames)
    pass_c(args.profile_depth, args.line_frames, args.line_pct)
    pass_d(args.profile_depth, args.warm, args.frames)


if __name__ == '__main__':
    main()
