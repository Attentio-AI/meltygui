"""Destructive feature ablations for a fixed, already-mounted benchmark tree only.

Never imported by production. These deliberately remove correctness features;
they are attribution experiments, not proposed fast paths.
"""
import ast
import inspect
import textwrap
from types import MethodType


def noop(*args, **kwargs):
    return None


def apply(scene, cuts):
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.rendering._gui_native import fixed_body
    from meltygui import imgui
    cache = scene.host.cache
    cuts = set(cuts)
    fixed = {}
    by_unique = {binding[4]: handle for handle, binding in cache.bindings.items()}
    for node in cache.records:
        value, arguments = cache.graph.invocation(node)
        info = cache.graph.info(node)
        fixed[node] = dict(value=value, arguments=arguments, rect=info['rect'],
                           parent=info['parent'], handle=by_unique[cache.uniques[node]],
                           state=cache.states[node], scrollable=info['scrollable'])
    cache._ablation_fixed = fixed
    cache._ablation_results = {}
    cache._ablation_needs_composition = False
    source = textwrap.dedent(inspect.getsource(retained.RetainedGui._execute))

    def replace(old, new):
        nonlocal source
        assert source.count(old) == 1, old
        source = source.replace(old, new)

    if 'input' in cuts:
        cache.process_host_input = noop
        cache.imgui_input.apply = noop
        cache.imgui_input.finish = noop
    if 'input_apply' in cuts:
        cache.imgui_input.apply = noop
    if 'input_finish' in cuts:
        cache.imgui_input.finish = noop
    if 'host_input' in cuts:
        # Preserve a valid frozen host IO snapshot for the per-view apply path.
        cache.imgui_input.process(imgui.get_io())
        cache.process_host_input = noop
    if 'layout_snapshot' in cuts:
        cache.geometry.begin = noop
        cache.geometry.abort = noop
    if 'layout_commit' in cuts:
        assert cache.geometry.native is not None
        cache.geometry.native.commit = noop
    if 'layout' in cuts:
        # Freeze the mounted native surface and all geometry transactions.
        scene.host.solve_native = noop
        for name in ('begin', 'commit', 'abort', 'captured', 'bind', 'allocation', 'apply', 'paint'):
            setattr(cache.geometry, name, noop)
    if 'policy' in cuts:
        # The already-mounted scene has one root raster and no promoted children.
        cache._select_rasters = noop
        cache.calibration.enabled = False
    if 'scopes' in cuts:
        replace('''result = renderer(input_value, **arguments, return_extras=True,
                                  _gui_unique=self.uniques[node], _gui_explicit=self.caller_keys[node])''',
                '''self.identities.enter(self.uniques[node])
                body_ok = False
                try:
                    result = _fixed_body(func, input_value, arguments, self.states[node])
                    body_ok = True
                finally:
                    self.identities.leave(body_ok)''')
    if 'lifetime' in cuts:
        for name in ('_track_capture', '_evict_scroll_tiles', '_retire'):
            setattr(cache, name, noop)
        # All bodies execute synchronously during the root call in this workload.
        # Remove source-change checks and dirty scheduling scans, keep composition.
        cache.flush = cache._compose
    if 'graph' in cuts:
        assert {'declaration', 'layout', 'lifetime', 'policy', 'scopes'} <= cuts
        replace('input_value, arguments = self.graph.invocation(node)',
                "input_value, arguments = self._ablation_fixed[node]['value'], self._ablation_fixed[node]['arguments']")
        replace('old_rect = self.graph.rect(node)', "old_rect = self._ablation_fixed[node]['rect']")
        replace('self.graph.begin(node)', 'pass  # Frozen retained topology.')
        replace('self.graph.commit(node, result[:2], independent)',
                'self._ablation_results[node] = result[:2]')
        replace('resized = self.graph.measured(node, w, measured_h)', 'resized = False')
        replace('self.graph.abort(node)', 'pass  # No graph transaction in this ablation.')
        replace('if not self.graph.parent(node) or self.graph.is_portal(node):',
                "if not self._ablation_fixed[node]['parent']:")
        replace('self.backgrounds[node] = background',
                "self.backgrounds[node] = background\n        self._ablation_needs_composition = True")
        roots = tuple(cache.root_ids)
        # Freeze child placement lists from the verified initial capture. Newly
        # captured packets still replace owned buffers and are genuinely rendered.
        def compose():
            if cache._ablation_needs_composition:
                cache.gpu.render_many([(node, cache.backgrounds[node]) for node in roots])
                cache._ablation_needs_composition = False
        cache._compose = compose
        cache.flush = compose
    namespace = dict(retained.__dict__, _fixed_body=fixed_body)
    exec(compile(source, '<fixed-tree cache ablation>', 'exec'), namespace)
    cache._execute = MethodType(namespace['_execute'], cache)

    borrowed_bridge = None
    if 'execution_glue' in cuts:
        assert 'graph' in cuts
        from cached_bare_gui import CaptureBridge
        # Borrow the already-owned cache resources. The production cache remains
        # their sole closer; this bridge only uses its minimal capture procedure.
        borrowed_bridge = object.__new__(CaptureBridge)
        borrowed_bridge.imgui = imgui
        borrowed_bridge.gpu = cache.gpu
        borrowed_bridge.captures = cache.captures
        borrowed_bridge.texture_refs = cache.texture_refs
        borrowed_bridge.stack = []
        borrowed_bridge.origins = []
        borrowed_bridge.recompose = False
        borrowed_bridge.closed = False
        callers = {node: {key: row['arguments'][key] for key in cache.caller_keys[node]
                          if key in row['arguments']} for node,row in fixed.items()}
        def execute(node, *, independent):
            row = fixed[node]
            result = borrowed_bridge.capture(node, not row['parent'], cache.records[node][2],
                                             row['value'], callers[node])
            cache._ablation_results[node] = result
            cache._ablation_needs_composition = True
        cache._execute = execute

    if 'declaration' in cuts:
        by_number = {row['value'].number: node for node, row in fixed.items()}
        def declare(input_value, **kwargs):
            node = by_number[input_value.number]
            row = fixed[node]
            parent, rect = row['parent'], row['rect']
            if not parent:
                if cache.frame_roots is not None:
                    cache.frame_roots.add(node)
            if 'graph' not in cuts:
                actual = cache.graph.declare(parent, 0, row['handle'], input_value,
                                            row['arguments'], rect, False, row['state'], row['scrollable'])
                assert actual == node
            cache._execute(node, independent=False)
            if not parent:
                cache.flush()
            elif borrowed_bridge is not None:
                borrowed_bridge.stack[-1].append((node, *rect))
            else:
                x, y, w, h = rect
                imgui.set_cursor_screen_pos((x, y))
                imgui.dummy(w, h)
            return cache._ablation_results[node] if 'graph' in cuts else cache.graph.result(node, True)
        scene.host.bindings[scene.view] = declare
    if 'state_configure' in cuts:
        from cache_only_gui import bind_cache
        tree = ast.parse(textwrap.dedent(inspect.getsource(retained.RetainedGui.gui)))
        class RemoveConfigure(ast.NodeTransformer):
            count = 0
            def visit_Expr(self, node):
                call = node.value
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == 'configure' and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == 'state'):
                    self.count += 1
                    return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)
        transform = RemoveConfigure()
        tree = ast.fix_missing_locations(transform.visit(tree))
        assert transform.count == 1
        scope = dict(retained.__dict__, _bind_native=bind_cache)
        exec(compile(tree, '<skip mounted geometry state configuration>', 'exec'), scope)
        function, options = scene.view.__gui_definition__
        options = {key:value for key,value in options.items() if key != 'use_cache'}
        definitions = list(cache.renderers)
        scene.host.bindings[scene.view] = scope['gui'](cache, function, **options)
        cache.renderers[:] = definitions
    return dict(cuts=sorted(cuts), fixed_nodes=len(fixed))
