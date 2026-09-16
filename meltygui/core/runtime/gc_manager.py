"""Deliberate garbage-collection scheduling for the studio.

The stock collector runs generation-2 passes at arbitrary allocation points —
observed as a 3.3s GIL-held stall (613k objects collected) landing on the
render thread mid-typing, with several large cst-dict graphs resident. Three
measures, all Toggles.GC-gated, applied from the per-frame tick() (hooked in
Melty.end_frame):

  * thresholds — gen2's auto-trigger is pushed effectively out of reach
    (gen0/gen1 stay stock: young-object passes are cheap), so full
    collections only happen when WE schedule them;
  * boot freeze — at the first input-idle window after boot, one full
    collect then gc.freeze(): the stable app graph (modules, fonts, studio,
    parse caches) moves to the permanent generation and is never walked
    again. Cycles alive at freeze time are leaked by design — app-lifetime
    state doesn't care;
  * idle collects — while input stays quiet, a periodic gc.collect() drains
    the cyclic garbage editing accumulates. Post-freeze the pass only walks
    objects allocated since, so it is small — and it lands when nobody is
    typing.

Every pass reports through the "lag" notify column: its duration, how many
objects (and roughly how many bytes) went, and the top types — and the toast
is clickable: it opens that collect's REPORT (one file per collect under
Toggles.GC.report_dir: type / module / dict-key-signature / function / frame
histograms of the reclaimed cycles, sample reprs, and the scheduler's reason)
in the code editor. Toggles.memory_profile additionally histograms, for the
boot pass, the whole live graph about to be frozen, appended to
/tmp/lsd_gc_profile.log. State survives hotswap via the globals().get pattern; the
end_frame hook line in meltygui.py is restart-bound (meltygui never hotswaps).
"""
import gc
import time

from meltygui.core.diagnostics.notifications import notify
from meltygui.core.diagnostics.notifications import capture_stack
from meltygui.core.runtime.toggles import Toggles

_state = globals().get("_state") or {
    "applied": False,       # thresholds currently overridden
    "frozen": False,        # boot collect+freeze done
    "last_collect": 0.0,
    "boot_t": time.monotonic(),
    "last_tick": 0.0,       # frame gap detection (frames park in wait_events)
    "focused": True,        # glfw FOCUSED as of the last tick
    "resumed_t": 0.0,       # last focus-gain / frame-gap moment
    "unfocus_armed_t": 0.0, # focus-LOSS edge seen; not once it is confirmed
}
# Hotswap reuses the live _state dict - backfill fields added since.
for _k, _v in (("last_tick", 0.0), ("focused", True), ("resumed_t", 0.0),
               ("unfocus_armed_t", 0.0)):
    _state.setdefault(_k, _v)

PROFILE_LOG = "/tmp/lsd_gc_profile.log"


def _profile_enabled():
    from meltygui.core.runtime.toggles import Toggles
    return bool(Toggles.memory_profile)


def _type_name(o):
    t = type(o)
    mod = getattr(t, "__module__", "") or ""
    return f"{mod}.{t.__qualname__}" if mod not in ("builtins", "") else t.__qualname__


def _histogram(objs, top=30):
    from collections import Counter
    c = Counter()
    for o in objs:
        c[_type_name(o)] += 1
    return c.most_common(top), sum(c.values())


def _samples(objs, wanted, per_type=3, width=160):
    """A few truncated reprs per type so 'dict' / 'list' rows say WHICH dicts.
    Plain containers get their key/element summary instead of repr()."""
    out = {}
    for o in objs:
        tn = _type_name(o)
        if tn not in wanted:
            continue
        got = out.setdefault(tn, [])
        if len(got) >= per_type:
            continue
        try:
            if type(o) is dict:
                r = "dict keys=" + repr(list(o.keys())[:8])
            elif type(o) in (list, tuple, set):
                r = f"{tn}[{len(o)}] " + repr(o[:4] if type(o) is not set else list(o)[:4])
            elif hasattr(o, "shape") and hasattr(o, "dtype"):
                # A tensor / ndarray repr materializes (and allocs) the data.
                r = f"{tn} shape={tuple(o.shape)} dtype={o.dtype}"
            else:
                r = repr(o)
                if " object at 0x" in r and hasattr(o, "__dict__"):
                    r += " attrs=" + repr(list(vars(o).keys())[:10])
        except Exception as e:
            r = f"<repr failed: {e!r}>"
        got.append(r.replace("\n", " ")[:width])
    return out


def _write(lines):
    try:
        with open(PROFILE_LOG, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _report_dir():
    """Where per-collect reports go — `Toggles.GC.report_dir` (expanded),
    falling back to the in-repo `.melty/gc_reports` (the screenshot
    convention). Created on demand; None if neither is writable."""
    import os
    from pathlib import Path
    from meltygui.core.runtime.toggles import Toggles
    candidates = []
    configured = Toggles.GC.report_dir
    if configured:
        candidates.append(Path(configured).expanduser())
    from meltygui.core.runtime.paths import cache_root
    candidates.append(cache_root() / "gc_reports")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except Exception:
            continue
    return None


def _write_report(label, lines):
    """One file per collect (`gc_<HHMMSS>_<label>.txt`), oldest pruned past
    `Toggles.GC.report_keep`. Returns the path, or None."""
    import re
    from meltygui.core.runtime.toggles import Toggles
    d = _report_dir()
    if d is None:
        return None
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_") or "collect"
    path = d / f"gc_{time.strftime('%Y%m%d_%H%M%S')}_{slug}.txt"
    try:
        path.write_text("\n".join(lines) + "\n")
        keep = int(Toggles.GC.report_keep or 0)
        if keep > 0:
            old = sorted(d.glob("gc_*.txt"))[:-keep]
            for f in old:
                f.unlink(missing_ok=True)
    except Exception:
        return None
    return path


def _detail_rows(objs, top=15):
    """WHAT the reclaimed objects were, one level below the type histogram:
    dicts grouped by their key signature (which dicts), functions by
    qualname, frames / code / cells by the code they belong to, methods by
    their function. The type histogram says "6,000 dicts"; these rows say
    "4,100 of them are draw_state kwargs dicts"."""
    import types
    from collections import Counter
    dicts, funcs, frames, cells, methods, code_objs = (Counter() for _ in range(6))
    for o in objs:
        t = type(o)
        try:
            if t is dict:
                keys = list(o.keys())
                sig = ", ".join(str(k)[:24] for k in keys[:5])
                if len(keys) > 5:
                    sig += f", … (+{len(keys) - 5})"
                dicts[f"{{{sig}}}"] += 1
            elif t is types.FunctionType:
                funcs[f"{o.__module__}.{o.__qualname__}"] += 1
            elif t is types.FrameType:
                c = o.f_code
                frames[f"{c.co_name}  {c.co_filename.rsplit('/', 1)[-1]}:{o.f_lineno}"] += 1
            elif t is types.CellType:
                cells[_type_name(o.cell_contents) if o.cell_contents is not None else "None"] += 1
            elif t is types.MethodType:
                f = o.__func__
                methods[f"{getattr(f, '__module__', '?')}.{getattr(f, '__qualname__', '?')}"] += 1
            elif t is types.CodeType:
                code_objs[f"{o.co_name}  {o.co_filename.rsplit('/', 1)[-1]}:{o.co_firstlineno}"] += 1
        except Exception:
            continue
    lines = []
    for title, counter in (("dicts by key signature", dicts),
                           ("functions by qualname", funcs),
                           ("bound methods by function", methods),
                           ("frames by code site", frames),
                           ("code objects", code_objs),
                           ("cells by content type", cells)):
        if not counter:
            continue
        lines.append(f"--- {title} (top {top} of {len(counter)} distinct):")
        for name, n in counter.most_common(top):
            lines.append(f"  {n:>10}  {name}")
    return lines


def _module_histogram(objs, top=15):
    """The reclaimed objects by the MODULE their type was defined in — the
    quickest "whose garbage is this" read (src.lsd… vs libcst vs torch)."""
    from collections import Counter
    c = Counter()
    for o in objs:
        c[getattr(type(o), "__module__", "") or "builtins"] += 1
    return [f"--- by type's module (top {top}):"] + [
        f"  {n:>10}  {m}" for m, n in c.most_common(top)]


def _approx_bytes(objs):
    """sys.getsizeof over the reclaimed objects — shallow (no referents
    that survive elsewhere), so a lower bound on what the collect freed."""
    import sys
    total = 0
    for o in objs:
        try:
            total += sys.getsizeof(o)
        except Exception:
            pass
    return total


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def _thread_report():
    """One line per thread: name + the innermost src.* frame's module, tagged
    STALE when that module dict is no longer the one registered in
    sys.modules — i.e. a thread left over from a previous in-process session,
    pinning that session's whole graph (see lifecycle.module_is_live)."""
    import sys
    import threading
    from meltygui.core.runtime.lifecycle import module_is_live
    frames = sys._current_frames()
    lines = ["--- THREADS (STALE = running in a purged prior-session module):"]
    stale = 0
    for th in threading.enumerate():
        f = frames.get(th.ident)
        where, tag = "?", ""
        while f is not None:
            g = f.f_globals
            name = g.get("__name__", "")
            if name.startswith("src."):
                where = f"{name}:{f.f_code.co_name}:{f.f_lineno}"
                if not module_is_live(g):
                    tag = "  STALE"
                    stale += 1
                break
            f = f.f_back
        lines.append(f"  {th.name:<40} {where}{tag}")
    lines.append(f"  ({stale} stale of {len(lines) - 1})")
    return lines


def _describe_root(o, world):
    """Short label for a live object that points into the stale world: what it
    is, and (for dicts / module dicts / frames) WHICH slot does the pointing."""
    import sys
    import types
    t = type(o)
    if t is dict:
        name = o.get("__name__") if "__file__" in o or "__spec__" in o else None
        keys = [k for k, v in list(o.items())[:4000] if id(v) in world][:4]
        if isinstance(name, str):
            return f"module dict {name} keys={keys}"
        return f"dict keys={keys} (of {len(o)})"
    if t is types.FrameType:
        return f"frame {o.f_globals.get('__name__')}:{o.f_code.co_name}"
    if t is types.FunctionType:
        return f"function {o.__module__}.{o.__qualname__}"
    if t is types.MethodType:
        return f"method {type(o.__self__).__name__}.{o.__func__.__qualname__}"
    if t is types.CellType:
        return "cell"
    if t in (list, tuple, set):
        return f"{t.__name__}[{len(o)}]"
    return _type_name(o)


def _stale_world_report(live):
    """Which purged src.* modules are still alive, how big the graph hanging
    off them is, and — the actual answer — which LIVE objects point into it.
    Seeds: module objects named src.* that sys.modules no longer maps to.
    World: everything reachable from the seeds WITHOUT crossing into live
    modules / live module dicts / the sys._* shared stores. Roots: objects
    outside the world with a direct referent inside it."""
    import sys
    import types
    from collections import Counter
    # Seeds are module DICTS: the module object itself usually dies with the
    # purge, but its module dict lives on in every function's __globals__.
    live_mod_dicts = {id(vars(m)) for m in list(sys.modules.values())
                      if hasattr(m, "__dict__")}

    def _stale_mod_name(d):
        if type(d) is not dict or id(d) in live_mod_dicts or "__spec__" not in d:
            return None
        n = d.get("__name__")
        return n if isinstance(n, str) and n.startswith("src.") else None

    def _stale_type(t):
        """A class is stale when the module it names no longer binds it under
        its qualname — the live class (even if reachable from old data via
        shared stores) must NOT be crossed, or every live instance of it
        reads as a root."""
        modname = getattr(t, "__module__", None)
        if not (isinstance(modname, str) and modname.startswith("src.")):
            return False
        obj = sys.modules.get(modname)
        if obj is None:
            return True
        for part in t.__qualname__.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return True
        return obj is not t

    _NO_CROSS = (types.BuiltinFunctionType, types.MethodDescriptorType,
                 types.WrapperDescriptorType, types.GetSetDescriptorType,
                 types.MemberDescriptorType, types.ClassMethodDescriptorType)

    def _crossable(r):
        """Follow into session-owned data only — never into process-shared
        singletons (builtin/foreign classes, foreign functions, descriptors,
        atoms), which would make every live object look like a 'root'."""
        if not gc.is_tracked(r):
            return False
        t = type(r)
        if t is types.ModuleType:
            n = getattr(r, "__name__", None)
            return isinstance(n, str) and n.startswith("src.") and sys.modules.get(n) is not r
        if t is dict:
            if "__spec__" in r and isinstance(r.get("__name__"), str):
                return _stale_mod_name(r) is not None
            return True
        if isinstance(r, type):
            return _stale_type(r)
        if t is types.FunctionType:
            return _stale_mod_name(r.__globals__) is not None
        if t in _NO_CROSS:
            return False
        if t.__module__ == "ast" and t.__name__ in ("Load", "Store", "Del"):
            return False   # process-shared singletons: every live ast node points at them
        return True

    seeds = [o for o in live if _stale_mod_name(o)]
    if not seeds:
        return ["--- STALE MODULES: none alive"]
    lines = [f"--- STALE MODULES (purged src.* module dicts still alive): {len(seeds)}"]
    for name, n in Counter(d["__name__"] for d in seeds).most_common(12):
        lines.append(f"  {n:>4}  {name}")
    shared = set()
    for k, v in list(vars(sys).items()):
        if k.startswith("_"):
            shared.add(id(v))
            try:
                shared.update(id(r) for r in gc.get_referents(v))
            except Exception:
                pass
    skip = live_mod_dicts | shared | {id(live), id(seeds)}
    world, stack = set(), list(seeds)
    t0 = time.perf_counter()
    while stack:
        o = stack.pop()
        if id(o) in world:
            continue
        world.add(id(o))
        for r in gc.get_referents(o):
            rid = id(r)
            if rid in world or rid in skip or not _crossable(r):
                continue
            stack.append(r)
    wc = Counter()
    for o in live:
        if id(o) in world:
            wc[_type_name(o)] += 1
    lines.append(f"--- STALE WORLD: {len(world)} objects reachable from them "
                 f"({1000*(time.perf_counter()-t0):.0f}ms); top types:")
    for tn, n in wc.most_common(15):
        lines.append(f"  {n:>9}  {tn}")
    t0 = time.perf_counter()
    roots = Counter()
    examples = {}
    me = globals()
    for o in live:
        oid = id(o)
        if oid in world or o is live or o is seeds:
            continue
        if type(o) is types.FrameType and o.f_globals is me:
            continue
        if type(o) in _NO_CROSS:
            continue           # slot/getset descriptors of the old classes: not holders
        try:
            refs = gc.get_referents(o)
        except Exception:
            continue
        if any(id(r) in world for r in refs):
            try:
                d = _describe_root(o, world)
            except Exception as e:
                d = f"{_type_name(o)} <describe failed {e!r}>"
            roots[d] += 1
            examples.setdefault(d, o)
    lines.append(f"--- ROOTS (live objects pointing INTO the stale world): "
                 f"{sum(roots.values())} ({1000*(time.perf_counter()-t0):.0f}ms)")
    # Whose dict is it? One get_referrers per top dict root (each is a full
    # heap scan, so capped): find instance/class whose __dict__ is that dict.
    owners = {}
    budget = 8
    for d, n in roots.most_common(40):
        ex = examples.get(d)
        if budget <= 0 or type(ex) is not dict or d.startswith("module dict"):
            continue
        budget -= 1
        try:
            for r in gc.get_referrers(ex):
                if r is live or r is seeds:
                    continue
                if getattr(r, "__dict__", None) is ex:
                    owners[d] = f"{_type_name(r)}" if not isinstance(r, type) \
                        else f"class {r.__module__}.{r.__qualname__}"
                    break
                if type(r) is dict:
                    ks = [k for k, v in list(r.items())[:2000] if v is ex][:2]
                    owners[d] = f"nested under dict keys={ks}"
        except Exception:
            pass
    for d, n in roots.most_common(40):
        own = owners.get(d)
        lines.append(f"  {n:>6}  {d[:150]}" + (f"   <- {own}" if own else ""))
    # Upward anchor chains for the top roots: who holds the holder, up to a
    # named anchor (module global / sys attribute / class attribute / thread
    # frame). Each hop is a full-heap get_referrers, so this is time-budgeted.
    lines.append("--- ANCHOR CHAINS (top roots, upward; budgeted):")
    deadline = time.perf_counter() + 10.0
    sys_attrs = {id(v): k for k, v in list(vars(sys).items())}
    mod_dict_names = {id(vars(m)): n for n, m in list(sys.modules.items())
                      if hasattr(m, "__dict__")}
    own_structs = {id(live), id(seeds), id(roots), id(examples), id(owners)}
    chased = 0
    for d, n in roots.most_common(12):
        if chased >= 4 or time.perf_counter() > deadline:
            break
        ex = examples.get(d)
        if ex is None or d.startswith("module dict"):
            continue
        chased += 1
        chain = [f"{d[:90]} (x{n})"]
        cur = ex
        visited = {id(cur)}
        for _hop in range(6):
            if time.perf_counter() > deadline:
                chain.append("… (time budget)")
                break
            if id(cur) in sys_attrs:
                chain.append(f"sys.{sys_attrs[id(cur)]}")
                break
            if id(cur) in mod_dict_names:
                chain.append(f"module {mod_dict_names[id(cur)]} globals")
                break
            try:
                refs = [r for r in gc.get_referrers(cur)
                        if id(r) not in own_structs and id(r) not in visited
                        and id(r) not in world
                        and not (type(r) is types.FrameType and r.f_globals is me)]
            except Exception:
                break
            if not refs:
                chain.append("(no live referrer — held only from inside the stale world)")
                break
            # Prefer the most "anchored" referrer: frames, module dicts, sys
            # values, classes first, plain containers after.
            def _rank(r):
                if type(r) is types.FrameType: return 0
                if id(r) in mod_dict_names or id(r) in sys_attrs: return 0
                if isinstance(r, type): return 1
                if type(r) is dict: return 2
                return 3
            refs.sort(key=_rank)
            nxt = refs[0]
            visited.add(id(nxt))
            if type(nxt) is dict:
                ks = [k for k, v in list(nxt.items())[:4000] if v is cur][:2]
                if id(nxt) in mod_dict_names:
                    chain.append(f"module {mod_dict_names[id(nxt)]} globals {ks}")
                    break
                chain.append(f"dict[{ks}] (of {len(nxt)})")
            elif type(nxt) is types.FrameType:
                chain.append(f"frame {nxt.f_globals.get('__name__')}:{nxt.f_code.co_name}:{nxt.f_lineno}")
                break
            elif isinstance(nxt, type):
                slot = [k for k, v in vars(nxt).items() if v is cur][:2]
                chain.append(f"class {nxt.__module__}.{nxt.__qualname__} attrs={slot}")
                break
            elif type(nxt) in (list, tuple, set, frozenset):
                chain.append(f"{type(nxt).__name__}[{len(nxt)}]")
            elif type(nxt) is types.CellType:
                chain.append("cell")
            elif type(nxt) is types.FunctionType:
                chain.append(f"function {nxt.__module__}.{nxt.__qualname__}")
            elif type(nxt) is types.MethodType:
                chain.append(f"bound method {type(nxt.__self__).__name__}.{nxt.__func__.__qualname__}")
            else:
                slot = []
                try:
                    slot = [k for k, v in vars(nxt).items() if v is cur][:2]
                except Exception:
                    pass
                chain.append(f"{_type_name(nxt)} attrs={slot}")
            cur = nxt
        lines.append("  " + "  ->  ".join(chain))
    return lines


def _collect(label, live_graph=False, reason=""):
    """gc.collect() plus a REPORT of what it reclaimed — written to one
    file per collect (`_write_report`, under Toggles.GC.report_dir) and
    summarized in a "lag"-column toast whose click opens that file in the
    code editor (the screenshot toast's convention). DEBUG_SAVEALL parks
    every reclaimed cyclic object in gc.garbage so it can be histogrammed by
    type (with a few reprs per top type), grouped one level finer
    (`_detail_rows`: which dicts, which functions, which frames) and by
    module, then released. `reason` is the scheduler's one-line "why now".

    Toggles.GC.reports off → identical to a bare gc.collect() (no toast).
    Toggles.memory_profile additionally walks the LIVE graph on the boot
    pass (live_graph=True — the graph about to be frozen, and the boot
    collect's price) and appends everything to PROFILE_LOG."""
    import threading
    from meltygui.core.runtime.toggles import Toggles
    profile = _profile_enabled()
    if not profile and (not Toggles.GC.reports or live_graph):
        # The boot pass (live_graph) walks EVERYTHING tracked; SAVEALL
        # would need a SECOND full walk to report what it reclaimed (observed:
        # 5.4 s doubled to 10 s). Bare collect, timed, toast only.
        t0 = time.perf_counter()
        n = gc.collect()
        ms = 1000 * (time.perf_counter() - t0)
        tint = (1.0, 0.25, 0.2) if ms >= 300 else (1.0, 0.65, 0.2)
        notify(f"gc: {label}  {ms:.0f}ms  [{threading.current_thread().name}]  "
               f"{n:,} unreachable — {reason or ''}",
               tint=tint, tag="lag", stack=capture_stack())
        return n
    stamp = time.strftime("%H:%M:%S")
    thread = threading.current_thread().name
    lines = [f"===== {stamp}  gc: {label}  [{thread}]",
             f"reason: {reason or '-'}",
             f"gen counts={gc.get_count()}  thresholds={gc.get_threshold()}  "
             f"frozen={gc.get_freeze_count()}"]
    if live_graph and profile:
        t0 = time.perf_counter()
        live = gc.get_objects()
        hist, total = _histogram(live, top=40)
        samples = _samples(live, {tn for tn, _ in hist[:6]})
        lines.append(f"--- LIVE graph before freeze: {total} tracked objects "
                     f"(walk {1000*(time.perf_counter()-t0):.0f}ms)")
        for tn, n in hist:
            lines.append(f"  {n:>10}  {tn}")
        for tn, reprs in samples.items():
            for r in reprs:
                lines.append(f"      e.g. {tn}: {r}")
        del samples
        lines.extend(_thread_report())
        try:
            lines.extend(_stale_world_report(live))
        except Exception as e:
            lines.append(f"--- STALE WORLD report failed: {e!r}")
        del live
    gc.set_debug(gc.DEBUG_SAVEALL)
    t0 = time.perf_counter()
    try:
        n = gc.collect()
    finally:
        gc.set_debug(0)
    ms = 1000 * (time.perf_counter() - t0)
    garbage = gc.garbage
    hist, total = _histogram(garbage, top=40)
    samples = _samples(garbage, {tn for tn, _ in hist[:8]})
    nbytes = _approx_bytes(garbage)
    lines.append(f"--- CYCLIC garbage reclaimed: collect()={n}  gc.garbage={total}  "
                 f"~{_fmt_bytes(nbytes)} shallow  ({ms:.0f}ms)")
    lines.append(f"--- by type (top 40):")
    for tn, cnt in hist:
        lines.append(f"  {cnt:>10}  {tn}")
    for tn, reprs in samples.items():
        for r in reprs:
            lines.append(f"      e.g. {tn}: {r}")
    del samples
    lines.extend(_module_histogram(garbage))
    lines.extend(_detail_rows(garbage))
    # Release: SAVEALL kept the cycles alive via gc.garbage; dropping the
    # reference leaves them unreachable again, and the follow-up (un-instrumented)
    # collect actually frees them.
    del garbage
    gc.garbage.clear()
    gc.collect()
    if profile:
        _write(lines)
    report = _write_report(label, lines) if Toggles.GC.reports else None
    # Toast: duration first (it is a lag entry), then what went - the top
    # three types by short name. Click → the report file in the editor.
    tops = " · ".join(f"{tn.rsplit('.', 1)[-1]} {cnt:,}" for tn, cnt in hist[:3]) or "nothing"
    tint = (1.0, 0.25, 0.2) if ms >= 300 else (1.0, 0.65, 0.2)
    notify(f"gc: {label}  {ms:.0f}ms  [{thread}]  {total:,} objs ~{_fmt_bytes(nbytes)}: {tops}",
           tint=tint, tag="lag", stack=capture_stack(),
           jump=(str(report), 1) if report else None)
    return n


def _boot_collect_and_freeze(label, trigger=""):
    """The once-per-session full pass: unfreeze → collect → freeze.

    A studio "restart" is IN-PROCESS (model_server purges src.* from
    sys.modules and re-imports), so this module — and its _state — is fresh
    each session while the interpreter's permanent generation is not:
    everything the PREVIOUS session froze is still parked there, where no
    collect ever looks. Old modules/caches/draw_states are all cyclic, so
    without unfreezing first every restart leaked the whole prior app graph
    for good (observed: 44M frozen vs 1.6M live). unfreeze() moves the
    permanent generation back into gen2 so this one collect reclaims the
    dead prior sessions before we freeze anew.

    Called from tick() at the first idle window, OR from collect_after_run
    once boot_delay_s has passed: a pre-freeze collect walks the entire graph
    (~900ms on 3M objects) whether or not we freeze after it, so a run that
    lands before the idle window pays the full walk exactly once and freezes
    right there instead of paying it again per run until idle."""
    prev_frozen = gc.get_freeze_count()
    gc.unfreeze()
    _collect(f"{label} collect+freeze", live_graph=True,
             reason=f"boot pass ({trigger or label}): unfreeze → full collect over the "
                    f"WHOLE tracked graph → freeze; cost = live graph size, not garbage")
    gc.freeze()
    _state["frozen"] = True
    _state["last_collect"] = time.monotonic()
    notify(f"gc: froze {gc.get_freeze_count()} objects out of gen2 scans"
           f" (unfroze {prev_frozen} from prior sessions first)",
           tint=(0.4, 0.9, 0.4), tag="lag", stack=capture_stack())


def tick():
    """Once per frame from Melty.end_frame (render thread). Cheap when there
    is nothing to do: two attribute reads and a couple of comparisons."""
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import Toggles
    if not Toggles.GC.manage:
        if _state["applied"]:
            gc.set_threshold(700, 10, 10)   # stock CPython defaults
            _state["applied"] = False
        return
    if not _state["applied"]:
        gc.set_threshold(700, 10, int(Toggles.GC.gen2_threshold))
        _state["applied"] = True
    now = time.monotonic()
    if now - _state["boot_t"] < Toggles.GC.boot_delay_s:
        _state["last_tick"] = now
        return

    # Frames only run on events (the main loop parks in glfw.poll_events), so
    # while the user is away this tick never fires; the last frame BACK saw
    # "idle for ages" and collected right in the user's face. The solution:
    # the focus-LOST edge (nobody is looking - the best possible moment to
    # pay for a collect), a focus-GAIN / long frame gap, which restarts the
    # idle clock so the collect needs idle_seconds of sleep measured from the
    # return, and POINTER PRESENCE (motion inside the window, stamped in the
    # input backend's cursor callback) - a return always starts with the
    # pointer crossing the window, before any click or focus change.
    #
    # The focus edge is POLLED, so it is only trustworthy while frames flow.
    # With frames parked, the first frame back can be the one that reads
    # "unfocused" (pointer over the window, focus not regained yet) against the
    # stale was_focused=True - a loss like that IS the return. So a loss only
    # ARMS the collect; it fires on a later frame (a wake is scheduled) once
    # the window has stayed unfocused, with no presence, for
    # Toggles.GC.unfocus_confirm_s. Any presence or focus meanwhile disarms.
    focused = _window_focused(Melty)
    was_focused = _state["focused"]
    _state["focused"] = focused
    frame_gap = now - _state["last_tick"]
    _state["last_tick"] = now
    if (focused and not was_focused) or frame_gap >= Toggles.GC.idle_seconds:
        _state["resumed_t"] = now
    lost_focus = was_focused and not focused
    last_input = max(getattr(Melty, "_last_input_time", 0.0),
                     getattr(Melty, "_last_presence_time", 0.0),
                     _state["resumed_t"])

    pointer_inside = bool(getattr(Melty, "_pointer_inside", True))
    if focused:
        _state["unfocus_armed_t"] = 0.0
    else:
        if lost_focus:
            _state["unfocus_armed_t"] = now
            _wake_in(Toggles.GC.unfocus_confirm_s + 0.05)
        armed_t = _state["unfocus_armed_t"]
        if armed_t and now - armed_t >= Toggles.GC.unfocus_confirm_s:
            _state["unfocus_armed_t"] = 0.0
            # "Nobody is looking" = no presence was input since the arm
            # (the pointer motion that WOKE the arming frame is stamped just
            # before it, hence the margin) AND the pointer is off the window
            # - unfocused with the pointer inside it is a return in progress
            # or someone reading; the pass would land in their face.
            quiet = last_input < armed_t - Toggles.GC.unfocus_confirm_s
            if quiet and not pointer_inside:
                if not _state["frozen"]:
                    _boot_collect_and_freeze(
                        "boot", trigger=f"window unfocused for {now - armed_t:.1f}s")
                elif now - _state["last_collect"] >= Toggles.GC.unfocus_collect_s:
                    _collect("unfocus collect",
                             reason=f"window unfocused for {now - armed_t:.1f}s, "
                                    f"last presence {now - last_input:.0f}s ago, "
                                    f"last collect {now - _state['last_collect']:.0f}s ago")
                    _state["last_collect"] = now
        return
    if now - last_input < Toggles.GC.idle_seconds:
        return
    if not _state["frozen"]:
        # The boot pass walks the WHOLE heap (seconds) and a 15 s pause in
        # typing is not a real pause; the unfocused branch above is the
        # best moment, and idle must be a long one.
        if now - last_input < Toggles.GC.boot_idle_seconds:
            return
        _boot_collect_and_freeze(
            "boot", trigger=f"no input / presence for {now - last_input:.0f}s")
    elif now - _state["last_collect"] >= Toggles.GC.idle_collect_s:
        _collect("idle collect",
                 reason=f"no input / presence for {now - last_input:.0f}s "
                        f"(idle_seconds={Toggles.GC.idle_seconds:g}), "
                        f"last collect {now - _state['last_collect']:.0f}s ago")
        _state["last_collect"] = now


def _wake_in(delay_s):
    """Produce a frame `delay_s` from now (the loop parks in wait_events, so
    the confirm tick above needs a wake to happen at all)."""
    import threading

    def _fire():
        try:
            from meltygui.core.windowing.glfw_utils import request_render
            request_render()
        except Exception:
            pass

    t = threading.Timer(max(0.0, float(delay_s)), _fire)
    t.daemon = True
    t.start()


def _window_focused(Melty) -> bool:
    """glfw FOCUSED of the studio window; True when there is no window yet
    (tests / headless) so the idle path behaves as before."""
    window = getattr(Melty, "glfw_window", None)
    if window is None:
        return True
    try:
        import meltygui.core.windowing.window_api as glfw
        return bool(glfw.get_window_attrib(window, glfw.FOCUSED))
    except Exception:
        return True


def collect_after_run(label="run"):
    """One full collect + CUDA cache release, called from a WORKER thread
    right after a heavy run retires its previous generation (the live lab's
    instrumented runs — live_instrument.run_instrumented). gen2's
    auto-trigger is pushed out of reach and the idle collector above waits
    for a quiet input window, but a run's cyclic garbage pins GPU tensors
    (deepcopied component trees, the prior ForwardPassResult graph), and
    VRAM can't wait minutes for idleness while the user is actively
    iterating — observed as ~a full activation generation leaked per Run.
    Post-freeze the pass only walks objects allocated since boot-freeze
    (same price the idle collect pays), scheduled at the one moment it is
    guaranteed profitable; the lag column keeps the cost visible.

    RATE-LIMITED (Toggles.GC.post_run_min_s): Auto Execute fires a run per
    param-drag tick, and a full collect per tick was a continuous ~120ms
    stall. Runs inside the spacing window coalesce onto a trailing one-shot
    timer that calls back here once the burst rests — so the LAST run's
    garbage still retires promptly (that's the VRAM that matters), while a
    burst pays at most one collect per window."""
    from meltygui.core.runtime.toggles import Toggles
    import threading
    min_s = float(Toggles.GC.post_run_min_s or 0.0)
    now = time.monotonic()
    since = now - _state.get("last_post_run", 0.0)
    # The CUDA cache release is NOT the expensive part (that's the heap
    # walk), but it is what nvidia-smi actually sees: everything the run
    # retired by refcount alone (no longer retain the live lab's per-run
    # activations once their parents re-render) sit in torch's allocator
    # cache until empty_cache. Release on its own short cadence so VRAM
    # tracks the live set while typing, independent of the collect spacing.
    rel_s = float(Toggles.GC.post_run_cache_release_s or 0.0)
    rel_since = now - _state.get("last_cache_release", 0.0)
    if rel_since >= rel_s:
        _state["last_cache_release"] = now
        _release_cuda_cache()
    else:
        # Inside the spacing window: DEFER, never skip - a typing burst's
        # last run must still hand its freed blocks back once it rests.
        release_cuda_cache_soon(rel_s - rel_since, label=label)
    if min_s > 0.0 and since < min_s:
        # Too soon - arm/replace the trailing timer timer. The timer re-enters
        # this function; by then either the window has passed (collect) or
        # newer runs re-armed a fresh timer (coalesce again).
        prev = _state.get("post_run_timer")
        if prev is not None:
            prev.cancel()
        t = threading.Timer(min_s - since, collect_after_run, args=(label,))
        t.daemon = True
        _state["post_run_timer"] = t
        t.start()
        return
    _state["last_post_run"] = now
    if not _state["frozen"] and now - _state["boot_t"] >= Toggles.GC.boot_delay_s:
        # Not frozen yet - this collect walks everything anyway; make it THE
        # boot pass so future runs (and idle) get the cheap post-freeze walk.
        _boot_collect_and_freeze(f"post-{label}", trigger=f"run '{label}' before the idle boot pass")
        _release_cuda_cache()
        return
    _collect(f"post-{label} collect", reason=f"run '{label}' retired its previous generation")
    _release_cuda_cache()
    _state["last_collect"] = time.monotonic()


def release_cuda_cache_soon(delay_s=0.5, label="release"):
    """torch.cuda.empty_cache() shortly, OFF the render thread, coalesced: a
    burst of releases (closing several live views, a prune sweep, runs inside
    the post_run_cache_release_s window) pays one call. Freed tensors only
    leave the allocator's cache — and nvidia-smi / the studio's VRAM readout
    — on empty_cache, and a close has no run behind it to trigger
    collect_after_run's release."""
    import threading
    prev = _state.get("cache_release_timer")
    if prev is not None:
        prev.cancel()

    def _fire():
        _state["cache_release_timer"] = None
        _state["last_cache_release"] = time.monotonic()
        _release_cuda_cache()

    t = threading.Timer(max(0.0, float(delay_s)), _fire)
    t.daemon = True
    _state["cache_release_timer"] = t
    t.start()


def _release_cuda_cache():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ── CUDA out-of-memory response ─────────────────────────────────────────────
# After an OOM the live set IS the VRAM: a partial run's accumulator stacks,
# every live-view window's pinned generation, the runners' previous results,
# plus whatever the failed run's traceback cycles hold - and nothing retires
# any of it (the idle/post-run collects are gated or deferred, and
# empty_cache can't nothing anything is still referenced). Left alone, a
# later run OOMs too and the only way out was a full reset. The responder
# dumps out of state deliberately, runs a REAL gc.collect (the exception →
# traceback → frame cycles are exactly what pins the failed generation), and
# empties the CUDA cache on every device, then retries what came back.
#
# Modules that hold big live state register a releaser here (called with no
# args, any thread, must not raise) - draw_function's runner threads do.
OOM_RELEASE_HOOKS = globals().get("OOM_RELEASE_HOOKS") or []


def is_cuda_oom(exc):
    """True for a CUDA allocation failure however it surfaced: torch's
    OutOfMemoryError, the runtime-API 'CUDA error: out of memory'
    RuntimeError (e.g. from a custom kernel's context), pycuda's
    MemoryError, or a GL/CUDA interop refusal carrying the same words."""
    if exc is None:
        return False
    try:
        import torch
        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
    except Exception:
        pass
    name = type(exc).__name__
    msg = str(exc).lower()
    if name == "MemoryError" and type(exc).__module__.startswith("pycuda"):
        return True
    return "out of memory" in msg and ("cuda" in msg or "cublas" in msg
                                       or name == "OutOfMemoryError")


def _device_mem():
    try:
        import torch
        if not torch.cuda.is_available():
            return {}
        return {i: (torch.cuda.memory_allocated(i), torch.cuda.memory_reserved(i))
                for i in range(torch.cuda.device_count())}
    except Exception:
        return {}


_REPORT_SKIP = ("_describe_holder", "report_vram_holders", "_oom_cleanup", "_dict_slot")


def _dict_slot(d, obj):
    for k, v in list(d.items()):
        if v is obj:
            return k
    return None


def _describe_holder(obj, depth, seen, lines, prefix, internal):
    """One line per referrer of `obj` (up to `depth` hops), naming what kind
    of container holds it and, where cheap, WHICH slot: dict key, attribute
    name on an instance, frame code name, module/function name. No
    closures in here — a genexpr capturing `obj` would show up as a cell."""
    if depth <= 0 or id(obj) in seen:
        return
    seen.add(id(obj))
    import types
    try:
        refs = gc.get_referrers(obj)
    except Exception:
        return
    internal.add(id(refs))
    shown = 0
    for r in refs:
        if id(r) in internal or r is lines or r is seen:
            continue
        if isinstance(r, types.FrameType) and r.f_code.co_name in _REPORT_SKIP:
            continue
        tn = type(r).__name__
        if isinstance(r, dict):
            key = _dict_slot(r, obj)
            owner = None
            for rr in gc.get_referrers(r):
                if getattr(rr, "__dict__", None) is r:
                    owner = rr
                    break
                if isinstance(rr, dict):
                    nm = _dict_slot(rr, r)
                    if nm is not None:
                        owner = f"dict[{nm!r}]"
                        break
            if owner is not None and not isinstance(owner, str):
                oname = (getattr(owner, "__qualname__", None) or getattr(owner, "__name__", None)
                         or getattr(owner, "name", None) or type(owner).__name__)
                label = f"attr {key!r} of {type(owner).__name__} {oname}"
            else:
                label = f"dict[{key!r}]" + (f" in {owner}" if owner else "")
        elif isinstance(r, (list, tuple, set, frozenset)):
            label = f"{tn}[{len(r)}]"
        elif isinstance(r, types.FrameType):
            label = (f"frame {r.f_code.co_name} "
                     f"({r.f_code.co_filename.rsplit('/', 1)[-1]}:{r.f_lineno})")
        elif isinstance(r, types.CellType):
            label = "closure cell"
        else:
            slot = None
            for a in getattr(r, "__slots__", ()):
                if getattr(r, a, None) is obj:
                    slot = a
                    break
            if slot is None:
                try:        # 3.12 inline class dicts: the instance IS the referrer
                    slot = _dict_slot(vars(r), obj)
                except TypeError:
                    pass
            label = tn + (f".{slot}" if slot else "")
            qn = getattr(r, "__qualname__", None)
            if isinstance(qn, str):
                label += f" {qn}"
        lines.append(f"{prefix}<- {label}")
        shown += 1
        if shown >= 4:
            lines.append(f"{prefix}   (+{len(refs) - shown} more referrers)")
            break
        if not isinstance(r, types.FrameType):
            _describe_holder(r, depth - 1, seen, lines, prefix + "   ", internal)


def report_vram_holders(top=12, device=None, depth=3):
    """Who holds the VRAM: every CUDA tensor reachable from gc, aggregated
    by STORAGE (views share one), the top-N storages by bytes with the
    referrer chain of one tensor over each. Unfreezes the permanent
    generation for the walk (gc.get_objects skips it) and re-freezes.
    Parameters show up too — the model's weights are part of the answer.
    Returns the report lines (also printed). Seconds, OOM-time only."""
    t0 = time.perf_counter()
    # Unfrozen for the WHOLE report: neither get_objects nor get_referrers
    # looks through the permanent generation. Deliberately NOT re-frozen
    # here: gc.freeze() freezes EVERYTHING tracked - including whatever
    # cyclic garbage is pending - and a frozen cycle can never be collected
    # (an earlier version did this and pinned 28 retired generations). The
    # OOM handler re-freezes after its collect (the boot regime); a manual
    # call leaves the heap unfrozen, which only makes the next collect walk
    # more.
    gc.unfreeze()
    return _report_vram_holders(top, device, depth, t0)


def _report_vram_holders(top, device, depth, t0):
    objs = gc.get_objects()
    by_storage = {}
    n_tensors = 0
    for o in objs:
        if type(o).__name__ not in ("Tensor", "Parameter"):
            continue
        try:
            if not o.is_cuda or (device is not None and (o.device.index or 0) != device):
                continue
            st = o.untyped_storage()
            key = (st.data_ptr(), o.device.index or 0)
            nbytes = st.nbytes()
        except Exception:
            continue
        n_tensors += 1
        ent = by_storage.get(key)
        if ent is None:
            by_storage[key] = [nbytes, o.device.index or 0, [o]]
        elif len(ent[2]) < 4:
            ent[2].append(o)    # the full tensor AND its views: a VIEW held
                                # by a draw call holds the storage just as well
    del objs, o
    total = 0
    per_dev = {}
    for nb, d, _t in by_storage.values():
        total += nb
        per_dev[d] = per_dev.get(d, 0) + nb
    del _t
    ranked = sorted(by_storage.values(), key=lambda e: -e[0])
    # Representatives in ONE flat list the describer knows to skip; the
    # bookkeeping containers above are released before any referrer walk.
    reps = []
    for e in ranked[:top]:
        reps.append((e[0], e[1], tuple(e[2])))
    small = 0
    for e in ranked[top:]:
        small += e[0]
    n_storages = len(by_storage)
    del by_storage, ranked, e, ent
    lines = [f"=== VRAM holders: {n_tensors} CUDA tensors over {n_storages} storages, "
             f"{total/2**30:.1f} GB reachable (walk {time.perf_counter()-t0:.1f}s)",
             "  per device: " + ", ".join(f"cuda:{d} {v/2**30:.1f} GB"
                                          for d, v in sorted(per_dev.items()))]
    seen = set()
    internal = {id(reps), id(per_dev)}
    for e in reps:
        internal.add(id(e))
        internal.add(id(e[2]))
    del e
    for i in range(len(reps)):
        nb, d, ts = reps[i]
        lines.append(f"- {nb/2**30:6.2f} GB cuda:{d} storage, {len(ts)} tensor(s) over it:")
        for t in ts:
            lines.append(f"   {type(t).__name__}{tuple(t.shape)} "
                         f"{str(t.dtype).replace('torch.', '')}"
                         f"{' (view)' if t.numel() * t.element_size() < nb else ''}")
            _describe_holder(t, depth, seen, lines, "     ", internal)
        del t, ts
    lines.append(f"  (+{small/2**30:.1f} GB in {max(0, n_storages - top)} smaller storages)")
    text = "\n".join(lines)
    print(text)
    return lines


def _oom_cleanup(where):
    import threading
    _state["oom_timer"] = None
    before = _device_mem()
    if Toggles.GC.oom_holder_report:
        try:
            report_vram_holders()
        except Exception as e:
            print(f"[gc] oom: holder report failed: {e!r}")
    dropped = 0
    try:
        from meltygui.code.live_view import release_all_live_stores
        dropped = release_all_live_stores(discover=True)
    except Exception as e:
        print(f"[gc] oom: release_all_live_stores failed: {e!r}")
    for hook in list(OOM_RELEASE_HOOKS):
        try:
            hook()
        except Exception as e:
            print(f"[gc] oom: release hook {getattr(hook, '__name__', hook)} failed: {e!r}")
    # A real collect, regardless of the profiler/idle gating: the failed
    # run's pending cycles are the generation that must die. Unfreeze
    # first (prior runs / an earlier report may have frozen garbage),
    # re-freeze what remains for the boot regime: later collects only walk
    # what's new.
    t0 = time.perf_counter()
    try:
        gc.unfreeze()
        n = gc.collect()
        gc.freeze()
    except Exception:
        n = -1
    _release_cuda_cache()
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                with torch.cuda.device(i):
                    torch.cuda.empty_cache()
    except Exception:
        pass
    after = _device_mem()
    parts = []
    for i in sorted(set(before) | set(after)):
        ba, br = before.get(i, (0, 0))
        aa, ar = after.get(i, (0, 0))
        parts.append(f"cuda:{i} reserved {br/2**30:.1f}→{ar/2**30:.1f} GB "
                     f"(allocated {ba/2**30:.1f}→{aa/2**30:.1f})")
    msg = (f"CUDA OOM in {where}: released {dropped} live keys, "
           f"gc {n} objs in {1000*(time.perf_counter()-t0):.0f}ms; "
           + "; ".join(parts))
    print(f"[gc] {msg}")
    try:
        notify(msg, tint=(1.0, 0.55, 0.3), tag="oom")
    except Exception:
        pass
    try:
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    except Exception:
        pass
    _state["last_post_run"] = _state["last_collect"] = time.monotonic()


def respond_to_cuda_oom(exc=None, where="run", delay_s=0.25):
    """Call from an except block that caught `exc` (or with exc=None to
    force). No-op unless is_cuda_oom(exc). The cleanup itself is DEFERRED
    onto a short timer thread — it must run after the raising frames have
    unwound (while the handler runs, the traceback still pins the failed
    run's tensors, and a collect there frees nothing) — and coalesced, so a
    burst of failures pays one sweep. Returns whether a cleanup was armed."""
    import threading
    if exc is not None and not is_cuda_oom(exc):
        return False
    prev = _state.get("oom_timer")
    if prev is not None:
        prev.cancel()
    t = threading.Timer(max(0.0, float(delay_s)), _oom_cleanup, args=(where,))
    t.daemon = True
    _state["oom_timer"] = t
    t.start()
    return True