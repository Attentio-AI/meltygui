"""Process-local retained dispatch experiments. Never imported by production.

Invoke: python tests/gui_dispatch_experiment.py --experiment empty-layout
        --target profile_gui_overhead.py -- <normal harness arguments>
"""
import argparse
import inspect
from pathlib import Path
import runpy
import sys
import textwrap


def install(experiment):
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.rendering.gui_layout_prototype import GuiGeometry

    if experiment in ('empty-layout', 'combined', 'fused'):
        begin, commit, abort = GuiGeometry.begin, GuiGeometry.commit, GuiGeometry.abort
        sentinel = object()

        def empty(manager):
            return not (manager.layouts or manager.frames or manager.bindings or manager.windows
                        or manager.native is not None or manager.gesture is not None)

        def lean_begin(manager, node):
            # Empty Python maps do not prove virgin native graph state: retired
            # layouts leave versions/serials, and native callers can own edges.
            if (empty(manager) and manager.axes[0].stats() == (0, 0, 0, 0)
                    and manager.axes[1].stats() == (0, 0, 0, 0)):
                manager.transactions[node] = sentinel
                manager.staged[node] = set()
            else:
                begin(manager, node)

        def lean_commit(manager, node):
            if manager.transactions.get(node) is sentinel and empty(manager):
                manager.staged.pop(node, None)
                manager.transactions.pop(node)
            else:
                commit(manager, node)

        def lean_abort(manager, node):
            if manager.transactions.get(node) is sentinel:
                from meltygui.core.rendering._gui_native import EdgeGraph
                manager.staged.pop(node, None)
                manager.transactions.pop(node)
                manager.axes = (EdgeGraph(), EdgeGraph())
                manager.layouts.clear()
                manager.owner_layouts.clear()
                manager.frames.clear()
                manager.bindings.clear()
                manager.windows.clear()
            else:
                abort(manager, node)

        GuiGeometry.begin, GuiGeometry.commit, GuiGeometry.abort = lean_begin, lean_commit, lean_abort

    if experiment in ('native-bridge', 'combined', 'fused'):
        source = textwrap.dedent(inspect.getsource(retained.RetainedGui._execute))
        replacements = [
            ("input_value, arguments = self.graph.invocation(node)\n    info = self.graph.info(node)\n    w, requested_h = info['constraints']\n    old_rect = info['rect']",
             "input_value, arguments, old_rect, requested_h = self.graph.execution(node)\n    w = old_rect[2]"),
            ("self.gpu.add_packet(node,\n                    ctypes.string_at(draw_list.vtx_buffer_data, draw_list.vtx_buffer_size * 20),\n                    ctypes.string_at(draw_list.idx_buffer_data, draw_list.idx_buffer_size * 4), commands)",
             "self.gpu.add_packet_pointer(node,\n                    draw_list.vtx_buffer_data, draw_list.vtx_buffer_size * 20,\n                    draw_list.idx_buffer_data, draw_list.idx_buffer_size * 4, commands)"),
        ]
        for old, new in replacements:
            assert source.count(old) == 1, old
            source = source.replace(old, new)
        if experiment == 'fused':
            old = '''native.begin_frame(width=w, cache=self, view_events=events, identity_frame=False)
            try:
                result = renderer(input_value, **arguments, return_extras=True,
                                  _gui_unique=self.uniques[node], _gui_explicit=self.caller_keys[node])
            finally:
                native.end_frame()'''
            new = '''result = renderer.__gui_retained__(input_value, arguments, self.uniques[node],
                                               self.caller_keys[node], self, events, w)'''
            assert source.count(old) == 1
            source = source.replace(old, new)
        namespace = dict(retained.__dict__)
        exec(compile(source, '<isolated native bridge experiment>', 'exec'), namespace)
        retained.RetainedGui._execute = namespace['_execute']
    if experiment == 'fused':
        from meltygui.core.rendering import gui_prototype
        source = textwrap.dedent(inspect.getsource(gui_prototype._bind_native))
        needle = '    wrapper.__gui_native__ = renderer'
        assert source.count(needle) == 1
        source = source.replace(needle, '''    def retained_call(input_value, arguments, unique, explicit, cache, events, width):
        nonlocal code
        if func.__code__ is not code:
            renderer.reconfigure(compile_plan())
            code = func.__code__
        return renderer.retained_execute(input_value, arguments, unique, explicit, cache, events, width)

    wrapper.__gui_retained__ = retained_call
''' + needle)
        namespace = dict(gui_prototype.__dict__)
        exec(compile(source, '<isolated fused dispatch experiment>', 'exec'), namespace)
        retained._bind_native = namespace['_bind_native']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', choices=['baseline', 'empty-layout', 'native-bridge', 'combined', 'fused'], required=True)
    parser.add_argument('--native-path', type=Path)
    parser.add_argument('--target', default='profile_gui_overhead.py')
    args, remaining = parser.parse_known_args()
    if args.native_path:
        from profile_gui_overhead import load_native
        load_native(args.native_path)
    install(args.experiment)
    sys.argv = [args.target] + (remaining[1:] if remaining[:1] == ['--'] else remaining)
    if args.target == 'pytest':
        import pytest
        raise SystemExit(pytest.main(sys.argv[1:]))
    runpy.run_path(str(Path(__file__).parent / args.target), run_name='__main__')


if __name__ == '__main__':
    main()
