"""Focused semantics of the isolated cohesive native declaration entry."""
import gc
import weakref
import pytest
from meltygui.core.rendering import _gui_native
from meltygui.core.rendering.retained_gui_prototype import RetainedGui

pytestmark=pytest.mark.skipif(not getattr(RetainedGui.gui,'__rust_declaration_prototype__',False),
                             reason='isolated declaration prototype only')


def plan(view):
    return next(cell.cell_contents for cell in view.__closure__
                if isinstance(cell.cell_contents,(_gui_native.RetainedDeclaration, getattr(_gui_native,'StoredDeclaration',_gui_native.RetainedDeclaration))))


def test_public_kwargs_state_precedence_and_first_alias():
    cache=RetainedGui(graphics=False)
    @cache.gui(left=2,top=4,width=100,height=20)
    def view(input_value,draw_state=None):return False,input_value
    model={};initial={'left':3};kwargs={'key':'stable','initial':initial,'return_extras':True}
    try:
        _,value,state=view(model,**kwargs)
        assert value is model and state.left==3 and state.top==4
        assert kwargs=={'key':'stable','initial':initial,'return_extras':True}
        assert kwargs['initial'] is initial and initial=={'left':3}
        assert cache.invalidate(view)==1 and cache.invalidate(state)==1
        state.left=50
        assert view(model,**kwargs,left=7)[2] is state and state.left==7
        assert view(model,**kwargs)[2] is state and state.left==50
    finally:cache.close()


def test_recursive_declaration_from_layout_hook_has_no_held_graph_borrow():
    cache=RetainedGui(graphics=False);calls=[]
    @cache.gui(width=100,height=20)
    def view(input_value):calls.append(input_value);return False,input_value
    allocation=cache.geometry.allocation;entered=[False]
    def allocate(parent):
        if not entered[0]:
            entered[0]=True
            assert view('inner',key='inner')==(False,'inner')
        return allocation(parent)
    cache.geometry.allocation=allocate
    try:
        assert view('outer',key='outer')==(False,'outer')
        assert calls==['inner','outer'] and len(cache.records)==2
        assert len(set(cache.uniques.values()))==2
    finally:cache.close()


def test_plan_defaults_are_snapshotted_before_reentrant_refresh():
    cache=RetainedGui(graphics=False)
    @cache.gui(left=3,top=4,width=100,height=20)
    def view(input_value):return False,input_value
    declaration=plan(view);allocation=cache.geometry.allocation;entered=[False]
    def allocate(parent):
        if not entered[0]:entered[0]=True;declaration.positions(30,40)
        return allocation(parent)
    cache.geometry.allocation=allocate
    try:
        state=view(None,key='stable',return_extras=True)[2]
        assert (state.left,state.top)==(3,4)
        assert view(None,key='stable',return_extras=True)[2] is state
        assert (state.left,state.top)==(30,40)
    finally:cache.close()


def test_recursive_body_code_change_refreshes_same_declaration_plan():
    cache=RetainedGui(graphics=False)
    namespace={'__name__':'declaration_hotswap_probe','calls':[]}
    exec('''def replacement(input_value, draw_state=None, left=9):
    calls.append(('new',input_value))
    return False,input_value
def body(input_value, draw_state=None, left=3):
    calls.append(('old',input_value))
    body.__code__=replacement.__code__
    body.__defaults__=replacement.__defaults__
    view(1,key='child')
    return False,input_value
''',namespace)
    view=cache.gui(namespace['body']);namespace['view']=view
    try:
        _,value,state=view(0,key='root',return_extras=True)
        assert value==0 and state.left==9
        assert ('old',0) in namespace['calls'] and ('new',1) in namespace['calls']
        assert view(0,key='root',return_extras=True)[2] is state
    finally:cache.close()


def test_initial_validation_propagates_python_exceptions():
    class BadInitial(dict):
        def __contains__(self,key):raise LookupError('initial containment failed')
    cache=RetainedGui(graphics=False)
    @cache.gui
    def view(input_value):return False,input_value
    try:
        with pytest.raises(LookupError,match='initial containment failed'):
            view(None,initial=BadInitial(x=1))
        with pytest.raises(TypeError,match='relative left and top'):
            view(None,window_pos=(1,2),initial=BadInitial(x=1))
    finally:cache.close()


def test_native_flag_validation_and_decimal_bounds_are_preserved():
    from decimal import Decimal
    cache=RetainedGui(graphics=False)
    @cache.gui
    def view(input_value):return False,input_value
    try:
        for arguments in ({'scroll':1},{'melty_window':1}):
            with pytest.raises(TypeError):view(None,**arguments)
        view(None,min_height=Decimal('1.00000000000000001'),height=0)
        node=cache.graph.nodes()[0]
        assert cache.graph.invocation(node)[1]['min_height']==2
        with pytest.raises(ValueError,match='height bounds'):
            view(None,min_height=Decimal('1.00000000000000001'),max_height=Decimal('1'))
    finally:cache.close()


def test_declaration_cycle_releases_cache_and_function_after_close():
    cache=RetainedGui(graphics=False)
    @cache.gui
    def view(input_value):return False,input_value
    view(None)
    cache_ref=weakref.ref(cache);view_ref=weakref.ref(view)
    cache.close();del view,cache;gc.collect()
    assert cache_ref() is None and view_ref() is None


@pytest.mark.skipif(not hasattr(_gui_native.RetainedGraph,'result_read_counts'),reason='test-only native counter build')
def test_native_lazy_acknowledgement_matches_python_observer_contract():
    cache=RetainedGui(graphics=False)
    @cache.gui
    def view(input_value):return False,input_value
    try:
        assert view('first')==(False,'first')
        cache.graph.reset_result_reads()
        assert view('first')==(False,'first')
        assert cache.graph.result_read_counts()==(0,1)
        cache.graph.reset_result_reads()
        assert view('second')==(False,'second')
        assert cache.graph.result_read_counts()==(1,1)
    finally:cache.close()
