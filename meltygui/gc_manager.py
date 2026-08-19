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

Every pass reports through the "lag" notify column (lag_span), so the cost
stays visible. Toggles.memory_profile turns every managed collect into a
profiled one (_collect): a by-type histogram of the cyclic garbage reclaimed
— and, for the boot pass, of the whole live graph about to be frozen — is
appended to /tmp/lsd_gc_profile.log. State survives hotswap via the globals().get pattern; the
end_frame hook line in melty.py is restart-bound (melty never hotswaps).
"""
import gc
import time

from src.lsd.gl_gui.notifications import lag_span, notify

_state = globals().get("_state") or {
    "applied": False,       # thresholds currently overridden
    "frozen": False,        # boot collect+freeze done
    "last_collect": 0.0,
    "boot_t": time.monotonic(),
}

PROFILE_LOG = "/tmp/lsd_gc_profile.log"


def _profile_enabled():
    from src.lsd.gl_gui.toggles import Toggles
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


def _thread_report():
    """One line per thread: name + the innermost src.* frame's module, tagged
    STALE when that module dict is no longer the one registered in
    sys.modules — i.e. a thread left over from a previous in-process session,
    pinning that session's whole graph (see lifecycle.module_is_live)."""
    import sys
    import threading
    from src.lsd.gl_gui.lifecycle import module_is_live
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


def _collect(label, live_graph=False):
    """gc.collect() wrapped in the memory_profile instrumentation.
    Off: identical to a bare gc.collect(). On: DEBUG_SAVEALL parks every
    reclaimed cyclic object in gc.garbage so we can histogram it by type
    (plus a few reprs per top type), then releases them; with live_graph=True
    (the boot pass) also histograms EVERYTHING gc tracks — that is the graph
    about to be frozen, and its size is the boot collect's price."""
    import threading
    if not _profile_enabled():
        return gc.collect()
    stamp = time.strftime("%H:%M:%S")
    thread = threading.current_thread().name
    lines = [f"===== {stamp}  gc: {label}  [{thread}]  gen counts={gc.get_count()}"]
    if live_graph:
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
    lines.append(f"--- CYCLIC garbage reclaimed: collect()={n}  gc.garbage={total}  ({ms:.0f}ms)")
    for tn, cnt in hist:
        lines.append(f"  {cnt:>10}  {tn}")
    for tn, reprs in samples.items():
        for r in reprs:
            lines.append(f"      e.g. {tn}: {r}")
    del samples
    # Release: SAVEALL kept the cycles alive via gc.garbage; dropping the
    # reference leaves them unreachable again, and the follow-up (un-instrumented)
    # collect actually frees them.
    del garbage
    gc.garbage.clear()
    gc.collect()
    _write(lines)
    notify(f"gc profile: {label} → {total} cyclic objs, top {hist[0][1] if hist else 0} "
           f"{hist[0][0] if hist else '-'}  (see {PROFILE_LOG})",
           tint=(0.9, 0.7, 0.3), tag="lag")
    return n


def _boot_collect_and_freeze(label):
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
    _collect(label, live_graph=True)
    gc.freeze()
    _state["frozen"] = True
    _state["last_collect"] = time.monotonic()
    notify(f"gc: froze {gc.get_freeze_count()} objects out of gen2 scans"
           f" (unfroze {prev_frozen} from prior sessions first)",
           tint=(0.4, 0.9, 0.4), tag="lag")


def tick():
    """Once per frame from Melty.end_frame (render thread). Cheap when there
    is nothing to do: two attribute reads and a couple of comparisons."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
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
        return
    last_input = getattr(Melty, "_last_input_time", 0.0)
    if now - last_input < Toggles.GC.idle_seconds:
        return
    if not _state["frozen"]:
        with lag_span("gc: boot collect+freeze", 0.0):
            _boot_collect_and_freeze("boot")
    elif now - _state["last_collect"] >= Toggles.GC.idle_collect_s:
        with lag_span("gc: idle collect", 0.0):
            _collect("idle")
        _state["last_collect"] = now


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
    from src.lsd.gl_gui.toggles import Toggles
    import threading
    min_s = float(Toggles.GC.post_run_min_s or 0.0)
    now = time.monotonic()
    since = now - _state.get("last_post_run", 0.0)
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
        with lag_span(f"gc: post-{label} collect+freeze", 0.0):
            _boot_collect_and_freeze(f"post-{label}")
            _release_cuda_cache()
        return
    with lag_span(f"gc: post-{label} collect", 0.0):
        _collect(f"post-{label}")
        _release_cuda_cache()
    _state["last_collect"] = time.monotonic()


def _release_cuda_cache():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
