"""Process-local host-commit and bookkeeping candidates; no production patches."""
import argparse
import ast
import inspect
from pathlib import Path
import runpy
import sys
import textwrap


def install(candidate, python_baseline=None):
    method_sources={}
    if python_baseline is not None:
        from meltygui.core.rendering import retained_gui_prototype as retained
        source=Path(python_baseline).read_text();lines=source.splitlines()
        cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='RetainedGui')
        for node in cls.body:
            if isinstance(node,ast.FunctionDef) and node.name in ('gui','_execute'):
                method_sources[node.name]=textwrap.dedent('\n'.join(lines[node.lineno-1:node.end_lineno]))
                namespace=dict(retained.__dict__)
                exec(compile(method_sources[node.name],'<pre-bookkeeping control>','exec'),namespace)
                setattr(retained.RetainedGui,node.name,namespace[node.name])
    if candidate in ('host','combined'):
        from meltygui.core.rendering.gui_native_collision import NativeCollision
        original_init=NativeCollision.__init__;original_observe=NativeCollision.observe;original_commit=NativeCollision.commit
        def initialize(self,*args,**kwargs):
            original_init(self,*args,**kwargs);self._commit_key=None;self._commit_roots=();self._commit_adapter=None
        def observe(self,*args,**kwargs):
            # Every observation still reaches the platform acknowledgement path.
            self._commit_key=None
            return original_observe(self,*args,**kwargs)
        def roots(self):
            g=self.geometry;c=g.cache
            return tuple((node,g.frames.get(node),c.invocations.get(node)) for node in c.root_ids)
        def same_roots(a,b):
            return len(a)==len(b) and all(x[0]==y[0] and x[1] is y[1] and x[2] is y[2] for x,y in zip(a,b))
        def commit(self):
            if not self.pairs:return
            g=self.geometry
            key=(g.axes[0],g.axes[1],g.axes[0].revision(),g.axes[1].revision())
            current=roots(self)
            if self._commit_adapter is self.os_frame and self._commit_key==key and same_roots(self._commit_roots,current):return
            original_commit(self)
            self._commit_key=key
            self._commit_roots=roots(self)
            self._commit_adapter=self.os_frame
        commit.__gui_commit_candidate__=True
        NativeCollision.__init__=initialize;NativeCollision.observe=observe;NativeCollision.commit=commit
    if candidate in ('bookkeeping','combined','fused-bookkeeping'):
        from meltygui.core.rendering import retained_gui_prototype as retained
        changes={
          '_execute':[("input_value, arguments = self.graph.invocation(node)\n    info = self.graph.info(node)\n    w, requested_h = info['constraints']\n    old_rect = info['rect']",
                       ("input_value, arguments, old_rect, requested_h = self.graph.execution(node)\n    w = old_rect[2]"
                        if candidate=='fused-bookkeeping' else
                        "input_value, arguments = self.graph.invocation(node)\n    old_rect = self.graph.rect(node)\n    w = old_rect[2]\n    requested_h = -1. if arguments['height'] is None else arguments['height']")),
                      ("self.states[node] = result[2]\n            self.associate(result[2], node)",
                       "state_changed = self.states[node] is not result[2]\n            self.states[node] = result[2]\n            if state_changed or node not in self.backgrounds:\n                self.associate(result[2], node)")],
          'gui':[("        self.associate(input_value, node)",
                  "        if previous is None or previous[0] is not input_value or isinstance(input_value, (str, Path)):\n            self.associate(input_value, node)")],
        }
        for name,replacements in changes.items():
            original=getattr(retained.RetainedGui,name)
            source=method_sources[name] if name in method_sources else textwrap.dedent(inspect.getsource(original))
            for old,new in replacements:
                assert source.count(old)==1,(name,old)
                source=source.replace(old,new)
            namespace=dict(original.__globals__)
            exec(compile(source,'<isolated bookkeeping candidate>','exec'),namespace)
            setattr(retained.RetainedGui,name,namespace[name])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',choices=['control','host','bookkeeping','combined','fused-bookkeeping'],required=True)
    parser.add_argument('--native-path',type=Path,required=True)
    parser.add_argument('--python-baseline',type=Path,help='Archived pre-bookkeeping retained_gui_prototype.py for reproducible controls')
    parser.add_argument('--target',default='profile_gui_phase_taxonomy.py')
    args,remaining=parser.parse_known_args()
    from profile_gui_overhead import load_native
    load_native(args.native_path);install(args.candidate,args.python_baseline)
    sys.argv=[args.target]+(remaining[1:] if remaining[:1]==['--'] else remaining)
    if args.target=='pytest':
        import pytest
        raise SystemExit(pytest.main(sys.argv[1:]))
    runpy.run_path(str(Path(__file__).with_name(args.target)),run_name='__main__')


if __name__=='__main__':main()
