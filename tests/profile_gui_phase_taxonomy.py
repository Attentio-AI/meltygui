"""Exclusive, process-local attribution of the unchanged retained GUI path."""
import argparse
import ast
from contextlib import contextmanager
from functools import wraps
import inspect
import json
from pathlib import Path
import textwrap
from time import perf_counter_ns


class Trace:
    def __init__(self, coarse=False, selected=None):
        self.coarse = coarse
        self.selected = selected
        self.enabled = False
        self.stack = []
        self.values = {}
        self.patches = []
        self.copy_bytes = 0
        self.overhead = {}

    def selects(self, label):
        return self.selected is None or label == 'frame.unattributed' or any(
            label == item or (item.endswith('*') and label.startswith(item[:-1])) for item in self.selected)

    def begin(self, label):
        entry = [perf_counter_ns(), 0, 0]
        self.stack.append(entry)
        entry[2] = perf_counter_ns()
        return entry

    def end(self, label, entry):
        elapsed = perf_counter_ns() - entry[2]
        self.stack.pop()
        value = self.values.setdefault(label, [0, 0, 0])
        value[0] += 1
        value[1] += elapsed
        value[2] += elapsed - entry[1]
        overhead = perf_counter_ns() - entry[0] - elapsed
        self.overhead[label] = self.overhead.get(label, 0) + overhead
        if self.stack:
            self.stack[-1][1] += elapsed + overhead

    @contextmanager
    def scope(self, label):
        if not self.enabled:
            yield
            return
        entry = self.begin(label)
        try:
            yield
        finally:
            self.end(label, entry)

    def wrap(self, label, function):
        if not self.selects(label):
            return function
        @wraps(function)
        def measured(*args, **kwargs):
            if not self.enabled:
                return function(*args, **kwargs)
            entry = self.begin(label)
            try:
                return function(*args, **kwargs)
            finally:
                self.end(label, entry)
        return measured

    def patch(self, owner, name, label):
        if not self.selects(label):
            return
        old = getattr(owner, name)
        descriptor = inspect.getattr_static(owner, name)
        self.patches.append((owner, name, descriptor))
        replacement = self.wrap(label, old)
        if isinstance(descriptor, staticmethod):
            replacement = staticmethod(replacement)
        setattr(owner, name, replacement)

    def install(self, bench):
        from meltygui.core.rendering import retained_gui_prototype as retained, gui_window_prototype as window
        from meltygui.core.rendering.gui_layout_prototype import GuiGeometry
        from meltygui.core.rendering.gui_imgui_input import ImGuiInput
        from meltygui.core.rendering.gui_native_collision import NativeCollision
        import meltygui_imgui as imgui
        import ctypes
        trace = self

        class GraphProxy:
            # Cache bound probes once. The underlying Rust graph and values stay unchanged.
            def __init__(self, graph):
                self.graph = graph

            def __getattr__(self, name):
                result = getattr(self.graph, name)
                if callable(result):
                    result = trace.wrap('graph.' + name, result)
                setattr(self, name, result)
                return result

        original_init = retained.RetainedGui.__init__
        self.patches.append((retained.RetainedGui, '__init__', original_init))
        def initialize(cache, *args, **kwargs):
            original_init(cache, *args, **kwargs)
            if not trace.coarse and trace.selects('graph.declare'):
                cache.graph = GraphProxy(cache.graph)
        retained.RetainedGui.__init__ = initialize

        original_gui = bench.gui
        self.patches.append((bench, 'gui', original_gui))
        def public_gui(func, **options):
            return trace.wrap('dispatch.public', original_gui(trace.wrap('body.useful', func), **options))
        bench.gui = public_gui

        original_factory = retained.RetainedGui.gui
        self.patches.append((retained.RetainedGui, 'gui', original_factory))
        def factory(cache, func=None, **kwargs):
            result = original_factory(cache, func, **kwargs)
            return trace.wrap('declaration.python', result) if func is not None else result
        retained.RetainedGui.gui = factory

        for module in (retained, window):
            original = module._bind_native
            self.patches.append((module, '_bind_native', original))
            def bind(native, func=None, _original=original, **kwargs):
                result = _original(native, func, **kwargs)
                return trace.wrap('dispatch.native_boundary', result) if func is not None else result
            module._bind_native = bind

        original_execute = retained.RetainedGui._execute
        source = ast.parse(textwrap.dedent(inspect.getsource(original_execute)))
        blocks = sorted((node for node in ast.walk(source) if isinstance(node, ast.If)
                         and ast.unparse(node.test) == 'self.graphics'), key=lambda node: node.lineno)
        assert len(blocks) == 4
        for block, label in zip(blocks, ('imgui.setup', 'imgui.finalize', 'capture.packet', 'bookkeeping.capture')):
            if not self.selects(label):
                continue
            block.body = [ast.With(items=[ast.withitem(context_expr=ast.Call(
                func=ast.Attribute(value=ast.Name(id='_trace', ctx=ast.Load()), attr='scope', ctx=ast.Load()),
                args=[ast.Constant(label)], keywords=[]))], body=block.body)]
        namespace = {**original_execute.__globals__, '_trace': self}
        exec(compile(ast.fix_missing_locations(source), '<exclusive phase instrumentation>', 'exec'), namespace)
        self.patches.append((retained.RetainedGui, '_execute', original_execute))
        retained.RetainedGui._execute = namespace['_execute']
        for name in (('_execute', 'flush', 'invalidate') if self.coarse else
                     ('_execute', 'flush', '_retire', 'associate', 'invalidate', '_track_capture')):
            self.patch(retained.RetainedGui, name, 'bookkeeping.' + name)
        for name in ('_compose', '_select_rasters', '_evict_scroll_tiles', 'present'):
            self.patch(retained.RetainedGui, name, 'replay.' + name)
        for name in (() if self.coarse else ('_context', '_begin_window')):
            self.patch(retained.RetainedGui, name, 'imgui.' + name)
        for name in (('begin', 'commit') if self.coarse else ('begin', 'commit', 'abort', 'captured', 'bind', 'apply')):
            self.patch(GuiGeometry, name, 'layout.' + name)
        self.patch(window._GuiWindow, 'solve_native', 'layout.host_native')
        self.patch(NativeCollision, 'commit', 'layout.native_commit')
        self.patch(window._GuiWindow, 'call', 'dispatch.binding')
        self.patch(ImGuiInput, 'apply', 'imgui.input_apply')
        self.patch(ImGuiInput, 'finish', 'bookkeeping.input_finish')
        self.patch(bench.Scene, 'frame', 'bookkeeping.frame')
        self.patch(bench.Graphics, 'submit', 'host.submit')
        for name in (() if self.coarse else ('get_current_context', 'set_current_context', 'get_io', 'new_frame',
                     'set_next_window_position', 'set_next_window_size', 'begin', 'end',
                     'render', 'push_id', 'pop_id', 'begin_group', 'end_group', 'get_item_rect_size')):
            self.patch(imgui, name, 'imgui.api.' + name)
        original_copy = ctypes.string_at
        self.patches.append((ctypes, 'string_at', original_copy))
        def copying(pointer, size=-1):
            with trace.scope('capture.cpu_bytes_copy'):
                result = original_copy(pointer, size)
                trace.copy_bytes += len(result)
                return result
        if not self.coarse and self.selects('capture.cpu_bytes_copy'):
            ctypes.string_at = copying

    def close(self):
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)


def set_content(bench, content, commands):
    original = bench.Scene.__init__
    source = textwrap.dedent(inspect.getsource(original))
    line = 'imgui.get_window_draw_list().add_rect_filled(x, y, x + 8, y + min(2., height), color)'
    assert source.count(line) == 1
    if content == 'none':
        new = 'pass'
    elif content == 'widget':
        new = '''if not input_value.children:
                imgui.push_id(str(input_value.number))
                imgui.set_cursor_screen_pos((x, y))
                imgui.button('##cell', width=8., height=min(12., height))
                imgui.pop_id()'''
    else:
        new = '\n            '.join([line] * commands)
    namespace = bench.__dict__
    exec(compile(source.replace(line, new), '<content-controlled scene>', 'exec'), namespace)
    bench.Scene.__init__ = namespace['__init__']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['plain', 'phases', 'coarse', 'focused', 'native'], default='plain')
    parser.add_argument('--focus', choices=['declaration','native-dispatch','public-dispatch','lifecycle',
        'capture','layout','layout-detail','bookkeeping','replay','body','new-frame','imgui-apis','graph'])
    parser.add_argument('--native-path', type=Path)
    parser.add_argument('--native-probes', action='store_true', help='Collect native counters alongside a sparse Python probe')
    parser.add_argument('--lifecycle-probes', type=Path, help='Same-library direct ImGui timer bridge from lifecycle benchmark')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--hostless', action='store_true')
    parser.add_argument('--culled', action='store_true')
    parser.add_argument('--depth', type=int, default=5)
    parser.add_argument('--fanout', type=int, default=4)
    parser.add_argument('--content', choices=['rect', 'none', 'widget'], default='rect')
    parser.add_argument('--commands', type=int, default=1)
    parser.add_argument('--work', type=int, default=0)
    parser.add_argument('--frames', type=int, default=100)
    parser.add_argument('--warm', type=int, default=24)
    parser.add_argument('--hot', action='store_true')
    args = parser.parse_args()
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    import benchmark_gui_overhead as bench
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.rendering import _gui_native
    Toggles.Rust.auto_cache = True
    set_content(bench, args.content, args.commands)
    graphics = bench.Graphics()
    reference = bench.Scene(bench.tree(args.depth, args.fanout, args.culled), 'raw', args.work, True)
    mount(reference, graphics, False)
    pixels = graphics.pixels()
    reference.close()
    focus_labels = {
        'declaration': ['declaration.python','bookkeeping._execute','bookkeeping.flush'],
        'native-dispatch': ['dispatch.native_boundary','body.useful'],
        'public-dispatch': ['dispatch.public','dispatch.binding','declaration.python'],
        'lifecycle': ['imgui.setup','imgui.finalize','bookkeeping.input_finish'],
        'capture': ['capture.packet'],
        'layout': ['layout.begin','layout.commit','layout.host_native'],
        'layout-detail': ['layout.begin','layout.commit','layout.host_native','layout.apply','layout.native_commit'],
        'bookkeeping': ['bookkeeping._execute','dispatch.native_boundary','imgui.setup','imgui.finalize',
                        'capture.packet','layout.begin','layout.commit'],
        'replay': ['replay.*'],
        'body': ['body.useful','dispatch.public'],
        'new-frame': ['imgui.api.new_frame'],
        'imgui-apis': ['imgui.api.*'],
        'graph': ['graph.*'],
    }
    if args.mode == 'focused':assert args.focus
    trace = (Trace(coarse=args.mode=='coarse', selected=focus_labels[args.focus] if args.mode=='focused' else None)
             if args.mode in ('phases','coarse','focused') else None)
    if trace:
        trace.install(bench)
    from types import SimpleNamespace
    adapter = SimpleNamespace(
        prototype_snapshot=lambda: (((0.,128.),(0.,512.)),((0.,128.),(0.,512.)),'walls',1,((),())),
        prototype_commit=lambda pairs: None, applied_origin=lambda axis:0.)
    scene = bench.Scene(bench.tree(args.depth,args.fanout,args.culled), 'gui_hot' if args.hot else 'gui_redraw',
                        args.work, True, args.culled)
    cache = scene.host.cache
    cache.calibration.enabled = False
    if not args.hostless:
        scene.host.solve_native(adapter)
    cold_start = perf_counter_ns()
    mount(scene, graphics, args.culled)
    cold_us = (perf_counter_ns()-cold_start)/1000
    assert graphics.pixels() == pixels, 'mount pixels'
    nodes = [] if args.hot else list(bench.selected_nodes(scene.root, True))
    expected = len(nodes), sum(bench.digest(node.number,args.work) for node in nodes)
    lifecycle_values={};lifecycle_active=[False];lifecycle_originals={}
    if args.lifecycle_probes:
        import ctypes
        import meltygui_imgui as imgui
        library=ctypes.PyDLL(str(args.lifecycle_probes))
        library.probe_header_version.restype=ctypes.c_char_p
        assert library.probe_header_version().decode()==imgui.get_version()
        for name in ('new_frame','render','end'):
            original=getattr(imgui,name);lifecycle_originals[name]=original
            direct=getattr(library,'probe_'+name);direct.argtypes=[];direct.restype=ctypes.c_double
            def measured(_name=name,_original=original,_direct=direct):
                if not lifecycle_active[0]:return _original()
                # Exact additional Python work performed by the Cython NewFrame binding.
                if _name=='new_frame':imgui.get_current_context()._keepalive_cache.clear()
                elapsed=_direct()
                key=('retained.' if cache.stack else 'host.')+_name
                value=lifecycle_values.setdefault(key,[0,0.]);value[0]+=1;value[1]+=elapsed
            setattr(imgui,name,measured)
    samples, gpu_samples, root_samples = [], [], []
    try:
        for frame in range(args.warm+args.frames):
            graphics.begin()
            if frame == args.warm:
                if trace:
                    trace.values.clear();trace.overhead.clear();trace.copy_bytes=0
                if args.mode == 'native' or args.native_probes:
                    _gui_native.profile_reset()
            start = perf_counter_ns()
            lifecycle_active[0]=frame>=args.warm
            if trace:
                trace.enabled = True
                with trace.scope('frame.unattributed'):
                    if not args.hostless:scene.host.solve_native(adapter)
                    scene.frame();graphics.submit()
                trace.enabled = False
            else:
                if not args.hostless:scene.host.solve_native(adapter)
                scene.frame();graphics.submit()
            elapsed = (perf_counter_ns()-start)/1000
            lifecycle_active[0]=False
            gpu = graphics.finish()
            assert (scene.count,scene.checksum) == expected
            if frame >= args.warm:
                samples.append(elapsed);gpu_samples.append(gpu)
        assert graphics.pixels() == pixels, 'final pixels'
        report = dict(args={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
            cpu=bench.summary(samples),gpu=bench.summary(gpu_samples),cpu_samples_us=samples,
            gpu_samples_us=gpu_samples,cold_us=cold_us,bodies=expected[0],checksum=expected[1],pixels_match_raw=True,
            resources=dict(contexts=len(cache.contexts),ids=len(set(cache.uniques.values())),gpu=cache.gpu.stats(),
                           representations=cache.gpu.representation_stats(),scratch=cache.gpu.scratch_stats(),
                           native_axes=[axis.stats() for axis in cache.geometry.axes]))
        if trace:
            report['phases']={k:dict(calls=v[0]/args.frames,inclusive_us=v[1]/args.frames/1000,
                exclusive_us=v[2]/args.frames/1000) for k,v in trace.values.items()}
            report['python_copy_bytes_per_frame']=trace.copy_bytes/args.frames
            report['reconciled_exclusive_us']=sum(v[2] for v in trace.values.values())/args.frames/1000
            report['root_inclusive_us']=trace.values['frame.unattributed'][1]/args.frames/1000
            report['measured_probe_overhead_us']=sum(v for k,v in trace.overhead.items()
                                                     if k!='frame.unattributed')/args.frames/1000
        if args.mode=='native' or args.native_probes:report['native_phases']=_gui_native.profile_snapshot()
        if args.lifecycle_probes:report['imgui_core']={k:dict(calls=v[0]/args.frames,mean_us=v[1]/args.frames/1000)
                                                      for k,v in lifecycle_values.items()}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2))
        print(args.output,report['cpu'],flush=True)
    finally:
        scene.close()
        if trace:trace.close()
        for name,original in lifecycle_originals.items():setattr(imgui,name,original)
        graphics.close()


if __name__ == '__main__':main()
