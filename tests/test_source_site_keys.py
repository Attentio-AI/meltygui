"""Deterministic CST-free statement keys, independent of editable dict shadowing."""
import pickle

import conftest  # noqa: F401
from meltygui.code.melty_scan import scan_extract
from meltygui.code.core_syntax import parse_to_dict, reparse_incremental, reparse_reusing


def origin(text):
    result = scan_extract(text)
    assert result[0] == 'ok', result
    return result[2]


def snapshot(o):
    return {key: (s.kind, s.start_line, s.start_column, s.end_line, s.end_column)
            for key, s in o.source_sites.items()}


def test_repeated_bindings_preserve_source_order_without_changing_dict_shadowing():
    text = 'x = 1\nx = 2\nclass C:\n    x = 3\n    x = 4\n'
    result = scan_extract(text)
    assert result[1]['x'] == 2
    assert result[1]['C']['x'] == 4
    assert set(result[2].source_sites) == {('x',), ('x#1',), ('C',), ('C', 'x'), ('C', 'x#1')}
    assert snapshot(result[2]) == snapshot(origin(text))
    assert snapshot(pickle.loads(pickle.dumps(result[2]))) == snapshot(result[2])


def test_existing_function_keys_and_nested_branch_counters():
    o = origin('def f():\n    x = 1\n    x = 2\n    foo()\n    foo()\n    if a:\n        if b:\n            x = 3\n    elif c:\n        x = 4\n    elif d:\n        x = 5\n    else:\n        x = 6\n')
    p = ('f', 'locals')
    assert p + ('x',) in o.source_sites
    assert p + ('x#1',) in o.source_sites
    assert p + ('foo()#1',) in o.source_sites
    assert p + ('if##0', 'if##1', 'x') in o.source_sites
    assert p + ('elif##0', 'x') in o.source_sites
    assert p + ('elif##1', 'x') in o.source_sites
    assert p + ('else##0', 'x') in o.source_sites


def test_unrepresented_statements_blocks_and_while_else():
    text = '''import os
from sys import version
async def f():
    while ready:
        with lock:
            x += 1
            assert(x)
        break
    else:
        raise(Error())
    match value:
        case 1:
            return(1)
        case _:
            yield(value)
    await work()
'''
    o = origin(text)
    for line in range(1, len(text.splitlines()) + 1):
        assert o.sites_at_line(line), (line, text.splitlines()[line - 1])
    assert o.sites_at_line(7)[0].path[-1] == '##assert'
    assert o.sites_at_line(10)[0].path[-1] == '##raise'
    assert o.sites_at_line(13)[0].path[-1] == '##return'


def test_headers_blank_lines_decorators_unicode_and_semicolons():
    text = '@decorate\ndef f(\n    arg,\n):\n    # comment\n\n    é = (\n        1\n        # comment\n\n    )\n    x = 1; y = 2\n    return(x)\n'
    o = origin(text)
    assert o.sites_at_line(1)[0].kind == 'decorator'
    assert all(o.sites_at_line(n)[0].path == ('f',) for n in (2, 3, 4))
    assert all(not o.sites_at_line(n) for n in (5, 6, 9, 10))
    assert o.sites_at_line(11)[0].path == ('f', 'locals', 'é')
    assert [s.path[-1] for s in o.sites_at_line(12)] == ['x', 'y']
    assert o.sites_at_line(12, 12)[0].path[-1] == 'y'
    assert not o.sites_at_line(12, 0)
    assert not o.sites_at_line(0)
    assert not o.sites_at_line(999)


def test_shifts_permutations_and_value_edits_preserve_distinct_keys():
    a = origin('x = 1\ny = 2\n')
    b = origin('\n# head\ny = 999\n\nx = 3\n')
    assert set(a.source_sites) == set(b.source_sites)


def test_incremental_full_worker_and_pickle_parity():
    text = ''.join(f'def f{i}():\n    x = {i}\n    return(x)\n\n' for i in range(20))
    gp = parse_to_dict(text, frontend='scan')
    for new in (text.replace('x = 10', 'x = 100'),
                text.replace('x = 10', 'x = 10\n    y = 2'),
                text.replace('def f10', 'def changed')):
        incremental = reparse_incremental(gp, new)
        expected = snapshot(origin(new))
        assert snapshot(incremental['__origin__']) == expected
        assert snapshot(reparse_reusing(incremental, new)['__origin__']) == expected
        assert snapshot(parse_to_dict(new, frontend='ast')['__origin__']) == expected
        assert snapshot(parse_to_dict(new, frontend='worker')['__origin__']) == expected
        gp = incremental


def test_incremental_global_occurrences_match_fresh_scan():
    text = 'x = 1\nx = 2\n' + ''.join(f'y{i} = {i}\n' for i in range(30))
    gp = parse_to_dict(text, frontend='scan')
    new = text.replace('x = 2', 'x = 20')
    assert snapshot(reparse_incremental(gp, new)['__origin__']) == snapshot(origin(new))


def test_lazy_hotswap_origin_upgrade():
    o = origin('x = 1\n')
    del o._site_text
    del o._site_index
    assert o.sites_at_line(1)[0].path == ('x',)


def test_independent_process_and_hash_seed_determinism():
    import json
    import os
    import subprocess
    import sys

    text = 'x = 1\nx = 2\ndef f():\n    if x:\n        return(x)\n    return(0)\n'
    script = ('import json,sys; from meltygui.code.melty_scan import scan_extract; '
              'print(json.dumps(list(scan_extract(sys.stdin.read())[2].source_sites)))')
    expected = json.dumps(list(origin(text).source_sites))
    for seed in ('1', '234'):
        result = subprocess.run([sys.executable, '-c', script], input=text, text=True,
                                capture_output=True, check=True, close_fds=False,
                                env={**os.environ, 'PYTHONHASHSEED': seed})
        assert result.stdout.strip() == expected


def test_try_handlers_else_finally_async_with_and_decorator_keys():
    text = '''@wrap(1)
@wrap(2)
async def f():
    async with lock:
        try:
            await work()
        except* ValueError as error:
            raise(error)
        else:
            pass
        finally:
            cleanup()
'''
    o = origin(text)
    assert ('f', 'decorators', 'wrap') in o.source_sites
    assert ('f', 'decorators', 'wrap#1') in o.source_sites
    assert all(o.sites_at_line(n) for n in range(1, 13))
    assert o.sites_at_line(7)[0].path[-1] == 'except* ValueError as error'
    assert o.sites_at_line(9)[0].path[-1] == 'try else'
    assert o.sites_at_line(11)[0].path[-1] == 'finally'


def test_root_add_delete_and_rename_match_fresh_scan():
    text = ''.join(f'x{i} = {i}\n' for i in range(30))
    for new in (text.replace('x10 = 10', 'changed = 10'),
                text.replace('x10 = 10\n', ''),
                text.replace('x10 = 10', 'x10 = 10\nx10 = 20')):
        gp = parse_to_dict(text, frontend='scan')
        assert snapshot(reparse_incremental(gp, new)['__origin__']) == snapshot(origin(new))


def test_fragment_lines_are_local_not_file_offset():
    o = parse_to_dict('x = 1\n', line_offset=100, frontend='scan')['__origin__']
    assert o.sites_at_line(1)[0].path == ('x',)
    assert not o.sites_at_line(101)


def test_multiline_string_comment_and_blank_content_is_still_statement_text():
    o = origin('x = """hello\n# string content\n\nworld"""\n')
    assert all(o.sites_at_line(line)[0].path == ('x',) for line in (1, 2, 3, 4))


def test_hotswap_existing_slotted_scanner_nodes_preserves_layout():
    from types import SimpleNamespace
    from meltygui.code import melty_scan
    from meltygui.code.file_converters import _hotswap_class

    class ExistingNode:
        __slots__ = ('lineno', 'col_offset', 'end_lineno', 'end_col_offset')

    node = ExistingNode()
    _hotswap_class(ExistingNode, melty_scan._Node)
    token = SimpleNamespace(start=(1, 0), end=(1, 3))
    assert node._pos(token, token) is node
    assert (node.lineno, node.end_col_offset) == (1, 3)

    class ExistingModule(ExistingNode):
        __slots__ = ('body',)

    class ExistingOpaque(ExistingNode):
        __slots__ = ('body',)

    module, opaque = ExistingModule(), ExistingOpaque()
    melty_scan.Module.__init__(module, [])
    melty_scan.Opaque.__init__(opaque, [])
    opaque._pos(token, token)
    assert module.body == opaque.body == []
    assert melty_scan.Module.__slots__ == melty_scan.Opaque.__slots__ == ('body',)
    in_flight_parser = melty_scan._Parser.__new__(melty_scan._Parser)
    assert in_flight_parser._site(node) == {}
    assert in_flight_parser.site_metadata[node] == {}
    metadata = {}
    tree, _, _ = melty_scan.scan('while x:\n    pass\nelse:\n    y = 1\n', site_metadata=metadata)
    index = melty_scan.SourceSiteIndex('while x:\n    pass\nelse:\n    y = 1\n', tree, metadata)
    assert index.at_line(4)[0].path == ('##while else', 'y')
    assert not hasattr(index, '_metadata')  # transient node identities are never retained
