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
stays visible. State survives hotswap via the globals().get pattern; the
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


def tick():
    """Once per frame from Melty.end_frame (render thread). Cheap when there
    is nothing to do: two attribute reads and a couple of comparisons."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    cfg = Toggles.GC
    if not cfg.manage:
        if _state["applied"]:
            gc.set_threshold(700, 10, 10)   # stock CPython defaults
            _state["applied"] = False
        return
    if not _state["applied"]:
        gc.set_threshold(700, 10, int(cfg.gen2_threshold))
        _state["applied"] = True
    now = time.monotonic()
    if now - _state["boot_t"] < cfg.boot_delay_s:
        return
    last_input = getattr(Melty, "_last_input_time", 0.0)
    if now - last_input < cfg.idle_seconds:
        return
    if not _state["frozen"]:
        with lag_span("gc: boot collect+freeze", 0.0):
            gc.collect()
            gc.freeze()
        _state["frozen"] = True
        _state["last_collect"] = now
        notify(f"gc: froze {gc.get_freeze_count()} objects out of gen2 scans",
               tint=(0.4, 0.9, 0.4), tag="lag")
    elif now - _state["last_collect"] >= cfg.idle_collect_s:
        with lag_span("gc: idle collect", 0.0):
            gc.collect()
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
    guaranteed profitable; the lag column keeps the cost visible."""
    with lag_span(f"gc: post-{label} collect", 0.0):
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    _state["last_collect"] = time.monotonic()
