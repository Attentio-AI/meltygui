"""Ownership and snapshot contracts for the isolated native record table."""
import gc
import weakref
import pytest
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
pytestmark=pytest.mark.skipif(not getattr(RetainedGui.gui,'__rust_records_prototype__',False),reason='isolated Rust records only')


def mounted():
    cache=RetainedGui(graphics=False)
    calls=[]
    @cache.gui
    def view(input_value, left=0, top=0):calls.append(input_value);return False,input_value
    view(None,key='stable')
    return cache,view,cache.graph.nodes()[0],calls


def test_execution_argument_mutation_does_not_change_caller_snapshot():
    cache,view,node,calls=mounted()
    try:
        original=cache._nodes.invocation(node)[1]
        execution=cache.graph.invocation(node)[1]
        execution['height']=999;execution['invented']=object()
        assert cache._nodes.invocation(node)[1]==original
        view(None,key='stable')
        assert len(calls)==1
        cache._nodes.resize_snapshot(node,123,45)
        assert cache._nodes.argument(node,'width',None)==123
        assert cache._nodes.argument(node,'height',None)==45
    finally:cache.close()


def test_caller_key_order_refresh_and_tuple_reuse():
    cache,view,node,calls=mounted()
    try:
        view(None,key='stable',left=2,top=3)
        before=cache._nodes.execution(node)
        assert before[5]==('left','top')
        view(None,key='stable',left=2,top=3)
        assert cache._nodes.execution(node) is before
        view(None,key='stable',top=3,left=2)
        assert cache._nodes.execution(node)[5]==('top','left')
        view(None,key='stable',left=2)
        assert cache._nodes.execution(node)[5]==('left',)
    finally:cache.close()


@pytest.mark.parametrize('operation',['replace','remove','clear'])
def test_reentrant_destructor_runs_after_table_borrow_is_released(operation):
    cache,view,node,calls=mounted();seen=[]
    class Probe:
        def __del__(self):
            seen.append((operation,len(cache._nodes),cache._nodes.ids()))
            cache._nodes.clear()
            gc.collect()
    try:
        cache._nodes.set_state(node,Probe())
        if operation=='replace':cache._nodes.set_state(node,object())
        elif operation=='remove':cache._nodes.remove(node)
        else:cache._nodes.clear()
        assert len(seen)==1
        assert len(cache._nodes)==0
    finally:cache.close()


def test_snapshot_value_keeps_identity_through_reentrant_gc():
    cache=RetainedGui(graphics=False);seen=[]
    class Value:pass
    value=Value();reference=weakref.ref(value)
    @cache.gui
    def view(input_value):
        gc.collect()
        seen.append(input_value is reference())
        return False,input_value
    try:
        view(value)
        node=cache.graph.nodes()[0]
        assert cache._nodes.invocation(node)[0] is value
        cache.invalidate(value);view(value)
        assert seen==[True,True]
        cache.close();del value,view;gc.collect()
        assert reference() is None
    finally:cache.close()


def test_equal_but_distinct_caller_values_keep_latest_references():
    cache=RetainedGui(graphics=False)
    @cache.gui
    def view(input_value,label=''):return False,input_value
    first=''.join(['a long ','caller value']);second=''.join(['a long ','caller value'])
    assert first==second and first is not second
    try:
        view(first,label=first)
        node=cache.graph.nodes()[0]
        view(second,label=second)
        assert cache._nodes.invocation(node)[0] is second
        assert cache._nodes.argument(node,'label',None) is second
    finally:cache.close()


def test_rc_only_snapshot_root_survives_retirement_and_gc():
    from meltygui.core.rendering._gui_native import RetainedRecords
    if not hasattr(RetainedRecords,'hold_snapshot'):pytest.skip('test-only snapshot-root probe')
    import sys
    cache=RetainedGui(graphics=False)
    class Value:pass
    @cache.gui
    def view(input_value):return False,input_value
    value=Value();reference=weakref.ref(value)
    view(value);node=cache.graph.nodes()[0]
    cache.graph.clear()
    for native,*_ in cache.bindings.values():native.clear()
    del value
    def retire():
        cache._nodes.clear()
        gc.collect()
        assert reference() is not None
        # Only the snapshot and this temporary getrefcount argument own it.
        count=sys.getrefcount(reference())
        assert count==2
    try:
        held=cache._nodes.hold_snapshot(node,retire)
        assert held is reference()
        del held;gc.collect()
        assert reference() is None
    finally:cache.close()
