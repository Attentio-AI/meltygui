"""Supply shared palette values and connect edits to cached view invalidation."""
import sys

from meltygui.core.melty import Melty
from meltygui.model.lut_model import LutPalette


def get_luts():
    if Melty.luts is None:
        # Migration only: reuse the old host's edited lists and allocations,
        # then remove that host from the frame pump. New sessions need no host.
        runtime = vars(Melty).get('lut_runtime')
        legacy = sys.modules.get('meltygui.tensor.voxel_playground')
        host = runtime.host if runtime is not None else (
            vars(legacy).get('lut_host') if legacy is not None else None)
        source = host.input_value if host is not None else None
        palette = LutPalette(source)
        states = list(runtime.resources.states.values()) if runtime is not None else []
        if host is not None and host._wrapper_draw_state is not None:
            state = host._wrapper_draw_state.misc.get('gl_state')
            if state is not None and state not in states:
                states.append(state)
        for state in states:
            for name in palette:
                texture = palette.texture(name)
                texture.adopt(state, f'lut_{name}')
                for key in list(state._resources):
                    if isinstance(key, tuple) and key[:2] == ('cuda_lut', name):
                        texture.adopt(state, key, ('cuda_lut', key[2]))
        if runtime is not None:
            cache = runtime.resources.textures
            cache.clear()
            cache.update(palette._textures)
            palette._textures = cache
            runtime.resources.states.clear()
            Melty.lut_runtime = None
        if host is not None:
            Melty.render_hosts.pop(id(host), None)
            host._registered = False
            host.hidden = True
            if host._draw_state is not None:
                host._draw_state.closed = True
        Melty.luts = palette
    return Melty.luts


def inject_luts(draw_state, kwargs):
    if kwargs.get('luts') is None:
        kwargs['luts'] = get_luts()
    palette = kwargs['luts']
    if isinstance(palette, LutPalette):
        palette.watch(draw_state.invalidate_up, max_depth=8, force=True)
