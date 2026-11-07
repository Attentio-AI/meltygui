"""Isolated Rust-owned bookkeeping integration; no production source patching."""
import inspect
import textwrap
from collections.abc import Mapping

class _Inspection(Mapping):
    def __init__(self, nodes, field):self.nodes,self.field=nodes,field
    def __iter__(self):return iter(self.nodes.ids())
    def __len__(self):return len(self.nodes)
    def __getitem__(self,node):
        row=self.nodes.execution(node)
        return row[:3] if self.field=='records' else row[3 if self.field=='states' else 4]


def _compile(original, replacements, extra, name=None):
    source=textwrap.dedent(inspect.getsource(original))
    for old,new in replacements:
        assert old in source, old
        source=source.replace(old,new)
    namespace={**inspect.getmodule(original).__dict__,**extra}
    exec(compile(source,'<isolated Rust records integration>','exec'),namespace)
    return namespace[name or original.__name__]


def classes():
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.rendering import gui_layout_prototype as layout
    from meltygui.core.rendering import gui_native_collision as collision
    from meltygui.core.rendering import gui_imgui_input as inputs
    from meltygui.core.rendering._gui_native import RetainedRecords,StoredDeclaration
    from gui_declaration_prototype import install as declaration_install
    original_gui=retained.RetainedGui.gui
    declaration_install()
    declared=retained.RetainedGui.gui
    retained.RetainedGui.gui=original_gui
    StoredCollision=_compile(collision.NativeCollision,[
        ('value,args=geometry.cache.invocations[node]\n                geometry.cache.invocations[node]=(value,{**args,\'width\':rect[2],\'height\':rect[3]})',
         'geometry.cache._nodes.resize_snapshot(node,rect[2],rect[3])')],{})
    # Dispatch once when attaching the bridge, never inside per-frame commit.
    original_collision=collision.NativeCollision
    class SelectCollision(original_collision):
        def __new__(cls,geometry):
            if hasattr(geometry.cache,'_nodes'):return StoredCollision(geometry)
            return super().__new__(cls)
    collision.NativeCollision=SelectCollision
    StoredGeometry=_compile(layout.GuiGeometry,[
        ('args=self.cache.invocations[node][1]','args=self.cache._nodes.invocation(node)[1]'),
        ('node not in cache.records','node not in cache._nodes'),
        ("cache.invocations[node][1].get('freeze_resize',False)","cache._nodes.argument(node,'freeze_resize',False)"),
        ("self.cache.invocations[portal][1].get('movable',True)","self.cache._nodes.argument(portal,'movable',True)"),
        ("self.cache.invocations[portal][1].get('resizable',True)","self.cache._nodes.argument(portal,'resizable',True)")],{})
    StoredInput=_compile(inputs.ImGuiInput,[('node in self.cache.records','node in self.cache._nodes')],{})
    replacements=[
        ('self.uniques = {}','self._nodes = _RetainedRecords()\n        self.uniques = _Inspection(self._nodes,"uniques")'),
        ('self.caller_keys = {}',''),('self.invocations = {}',''),
        ('self.records = {}','self.records = _Inspection(self._nodes,"records")'),
        ('self.states = {}','self.states = _Inspection(self._nodes,"states")'),
        ('self.imgui_input = ImGuiInput(self)','self.imgui_input = _StoredInput(self)'),
        ('self.geometry = GuiGeometry(self)','self.geometry = _StoredGeometry(self)'),
        ('native, renderer, func = self.records[node]','native, renderer, func, state, unique, explicit = self._nodes.execution(node)'),
        ('_gui_unique=self.uniques[node], _gui_explicit=self.caller_keys[node]','_gui_unique=unique, _gui_explicit=explicit'),
        ('state_changed = self.states[node] is not result[2]\n                self.states[node] = result[2]','state_changed = state is not result[2]\n                if state_changed: self._nodes.set_state(node,result[2])'),
        ('len(self.records)','len(self._nodes)'),
        ('self.records[node][2].__name__','self._nodes.execution(node)[2].__name__'),
        ('self.records.pop(node, None)','self._nodes.remove(node)'),
        ('(self.invocations, self.events, self.regions, self.states, self.uniques, self.caller_keys,','(self.events, self.regions,'),
        ('node in self.records','node in self._nodes'),('self.hover[0] in self.records','self.hover[0] in self._nodes'),
        ('self.states[node].left','self._nodes.state(node).left'),('self.states[node].top','self._nodes.state(node).top'),
    ]
    # The declaration method is replaced below; leave its cold text out of these transforms.
    source=textwrap.dedent(inspect.getsource(retained.RetainedGui))
    start=source.index('    def gui('); end=source.index('\n    def ',start+5)
    # gui has decorators above it; replacing its body with a stub keeps class parsing simple.
    source=source[:start]+'    def gui(self,*args,**kwargs):pass\n'+source[end:]
    for old,new in replacements:
        assert old in source,old
        source=source.replace(old,new)
    namespace={**retained.__dict__,'_RetainedRecords':RetainedRecords,'_Inspection':_Inspection,'_StoredGeometry':StoredGeometry,'_StoredInput':StoredInput}
    exec(compile(source,'<isolated Rust records cache>','exec'),namespace)
    StoredGui=namespace['RetainedGui']
    source=declared.__declaration_source__
    source=source.replace('for node, (_, _, renderer_func) in self.records.items():','for node in self._nodes.ids():\n            _, _, renderer_func, state, unique, explicit = self._nodes.execution(node)')
    source=source.replace('self.invocations[node][1]','self._nodes.invocation(node)[1]').replace('self.caller_keys[node]','explicit').replace('self.states[node].configure','state.configure')
    namespace={**original_gui.__globals__,'_RetainedDeclaration':StoredDeclaration}
    exec(compile(source,'<isolated Rust records declaration>','exec'),namespace)
    StoredGui.gui=namespace['gui']
    StoredGui.gui.__rust_declaration_prototype__=True
    StoredGui.gui.__rust_records_prototype__=True
    return retained.RetainedGui,declared,StoredGui


def install():
    from meltygui.core.rendering import retained_gui_prototype as retained,gui_window_prototype as window
    _,_,stored=classes()
    retained.RetainedGui=window.RetainedGui=stored

if __name__=='__main__':
    import argparse,sys,runpy
    from pathlib import Path
    parser=argparse.ArgumentParser();parser.add_argument('--native-path',required=True,type=Path);parser.add_argument('--target',default='pytest')
    args,rest=parser.parse_known_args()
    from profile_gui_overhead import load_native
    load_native(args.native_path);install()
    sys.argv=[args.target]+(rest[1:] if rest[:1]==['--'] else rest)
    if args.target=='pytest':
        import pytest
        raise SystemExit(pytest.main(sys.argv[1:]))
    runpy.run_path(str(Path(__file__).with_name(args.target)),run_name='__main__')
