"""Keep the complete retained runtime; bypass only benchmark-body dispatch.

Only valid for the explicit Scene.body(input_value, left, top, width, height,
key) signature, fixed geometry, draw-list-only bodies and unchanged returns.
NativeState remains internal to the retained graph, but is never injected into
the body. Declaration configuration and runtime begin/end are intentionally kept.
"""
import inspect
import textwrap


def execution_methods():
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.rendering._gui_native import fixed_body
    original = retained.RetainedGui._execute
    source = textwrap.dedent(inspect.getsource(original))
    old = '''result = renderer(input_value, **arguments, return_extras=True,
                                  _gui_unique=self.uniques[node], _gui_explicit=self.caller_keys[node])'''
    assert source.count(old) == 1, 'Production dispatch changed; review the isolation boundary'
    source = source.replace(old, 'result = _fixed_body(func, input_value, arguments, self.states[node])')
    namespace = dict(retained.__dict__, _fixed_body=fixed_body)
    exec(compile(source, '<isolated fixed-body cache experiment>', 'exec'), namespace)
    return original, namespace['_execute']
