"""Tensor core functions and supporting definitions."""
from meltygui.core.core_render import release_input_refs


def _voxels_cleanup(draw_state):
    """Melty.cleanup hook (@render_func(on_cleanup=…)) for draw_voxels: sever
    every reference this draw_state holds to the source tensor and the
    uploaded volume, so the session teardown's torch.cuda.empty_cache() can
    actually return the VRAM. Three places hold it:
      * gl_state — the 3-D texture / CUDA-registered PBO ("volume" /
        "volume_cuda") and the label atlas: release() queues them for the
        GL-thread delete the teardown flushes;
      * the wrapper's input slots (_input_value & co.) — the tensor the view
        rendered; release_input_refs resets them;
      * anything else tensor-shaped that landed on the draw_state (misc or a
        plain attribute) — scrubbed generically rather than by name, so a new
        field can't silently pin a volume.
    The draw_state itself survives (registry entries persist across a restart-
    in-place); a re-render refills everything."""
    from meltygui.model.tensor_model import _is_tensorish

    misc = getattr(draw_state, "misc", None)
    if isinstance(misc, dict):
        gl_state = misc.get("gl_state")
        if gl_state is not None:
            try:
                gl_state.release()
            except Exception as e:
                print(f"[draw_voxels] gl_state release failed: {e!r}")
        for k in [k for k, v in misc.items() if _is_tensorish(v)]:
            misc.pop(k, None)
    release_input_refs(draw_state)
    for k, v in list(vars(draw_state).items()):
        if k != "misc" and _is_tensorish(v):
            setattr(draw_state, k, None)
