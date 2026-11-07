"""Process-local integration for the isolated Rust declaration callable."""
import argparse
import inspect
from pathlib import Path
import runpy
import sys
import textwrap


def install():
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.rendering._gui_native import RetainedDeclaration
    original=retained.RetainedGui.gui
    source=textwrap.dedent(inspect.getsource(original))
    start=source.index('    @wraps(func)\n    def wrapper(')
    end=source.index('    wrapper.__gui__ = True',start)
    source=source[:start]+'''    def cold_bind(identity_handle):
        native = _new_native(graphics=self.graphics, identities=self.identities)
        renderer = _bind_native(native, func, inject=inject, events=events,
                                state_id=state_id, no_save=no_save, **decoration)
        unique = self.identities.unique(identity_handle)
        return native, renderer, func, native.state(unique), unique

    declaration = _RetainedDeclaration(self, func, cold_bind, _alias, Path, renderer_id,
        dict(left=left, top=top, width=width, height=height, min_height=min_height,
             max_height=max_height, scroll=scroll, decoration=decoration))

    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        return declaration(input_value, kwargs)

    declaration.attach(wrapper)
'''+source[end:]
    # Default-position plan refresh retains the existing source-change lifecycle.
    needle="        top = position_default('top', decorated_top)\n"
    assert source.count(needle)==1
    source=source.replace(needle,needle+'        declaration.positions(left, top)\n')
    namespace={**original.__globals__,'_RetainedDeclaration':RetainedDeclaration}
    exec(compile(source,'<isolated Rust declaration integration>','exec'),namespace)
    namespace['gui'].__rust_declaration_prototype__=True
    namespace['gui'].__declaration_source__=source
    retained.RetainedGui.gui=namespace['gui']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path',required=True,type=Path)
    parser.add_argument('--control',action='store_true')
    parser.add_argument('--target',default='profile_gui_phase_taxonomy.py')
    args,remaining=parser.parse_known_args()
    from profile_gui_overhead import load_native
    load_native(args.native_path)
    if not args.control:install()
    sys.argv=[args.target]+(remaining[1:] if remaining[:1]==['--'] else remaining)
    if args.target=='pytest':
        import pytest
        raise SystemExit(pytest.main(sys.argv[1:]))
    runpy.run_path(str(Path(__file__).with_name(args.target)),run_name='__main__')


if __name__=='__main__':main()
