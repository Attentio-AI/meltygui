"""Completions, signatures and navigation inside embedded CPython."""
import builtins
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from meltygui.code import libcst_conversion as conversion
from meltygui.code import source_context


@pytest.fixture
def device_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(conversion, 'sys', SimpleNamespace(platform='ios', path=sys.path, modules=sys.modules))
    monkeypatch.setattr(conversion, '_jedi_pools', {})
    monkeypatch.setattr(conversion, '_jedi_projects', {})
    monkeypatch.setattr(conversion, '_completion_ns_cache', None)
    project = SimpleNamespace(key=(str(tmp_path),), root=str(tmp_path), source_paths=(str(tmp_path),),
                              environment='/desktop-only/.venv')
    monkeypatch.setattr(source_context, 'analysis_project', lambda **kwargs: project)
    original_import = builtins.__import__

    def no_process_import(name, *args, **kwargs):
        if name == 'multiprocessing' or name.startswith('multiprocessing.') or name == 'concurrent.futures.process':
            raise AssertionError(f'iOS attempted process import: {name}')
        return original_import(name, *args, **kwargs)

    def no_external_python(*args, **kwargs):
        raise AssertionError('Jedi attempted to start an external interpreter')

    monkeypatch.setattr(builtins, '__import__', no_process_import)
    monkeypatch.setattr(subprocess, 'Popen', no_external_python)
    yield tmp_path
    pools = list(conversion._jedi_pools.values())
    conversion.shutdown_jedi_pool()
    for pool in pools:
        pool.shutdown(wait=True, cancel_futures=True)


def test_device_executor_uses_one_worker_in_current_process(device_analysis):
    caller = threading.get_ident()
    pool = conversion._get_jedi_pool('ac')
    assert pool is conversion._get_jedi_pool('index')
    pid, worker = pool.submit(lambda: (os.getpid(), threading.get_ident())).result(timeout=5)
    assert pid == os.getpid() and worker != caller
    with pytest.raises(RuntimeError, match='embedded interpreter'):
        conversion._get_jedi_mp_ctx()


def test_completion_and_signature_help_resolve_local_project_without_external_python(device_analysis):
    root = device_analysis
    (root / 'helper.py').write_text('class Widget:\n    def greet(self, who: str) -> str:\n        return who\n')
    path = str(root / 'main.py')
    code = 'from helper import Widget\nWidget().'
    future = conversion._submit_interactive(conversion._jedi_complete_worker, code, 2, len('Widget().'), path)
    assert ('greet', 'function') in future.result(timeout=10)
    code = 'from helper import Widget\nWidget().greet('
    signatures = conversion._submit_interactive(conversion._jedi_signatures_worker, code, 2,
                                                len('Widget().greet('), path).result(timeout=10)
    assert signatures and signatures[0][0] == 'greet'
    assert any('who' in parameter for parameter in signatures[0][1])


def test_goto_and_references_use_in_memory_source_and_bundled_environment(device_analysis):
    root = device_analysis
    helper = root / 'helper.py'
    helper.write_text('def useful(value):\n    return value\n')
    code = 'from helper import useful\nanswer = useful(42)\n'
    main = root / 'main.py'
    main.write_text('old = 0\n')

    def lookup():
        script = conversion._jedi_script(main, code)
        definitions = script.goto(2, 12, follow_imports=True)
        return [(item.module_path, item.line, item.name) for item in definitions]

    assert conversion._get_jedi_pool().submit(lookup).result(timeout=10) == [(helper, 1, 'useful')]
    main.write_text('value = 42\nanswer = value\n')
    refs = conversion._get_jedi_pool().submit(conversion._jedi_worker, str(main), {'value'}).result(timeout=10)
    assert refs['value'][0][0] == str(main) and refs['value'][0][1] == 2


def test_device_namespace_uses_native_imgui_and_window_facade(device_analysis, monkeypatch):
    requested = []
    import importlib
    original = importlib.import_module

    def record(name):
        requested.append(name)
        if name == 'glfw':
            raise AssertionError('desktop GLFW is not an iOS completion dependency')
        return original(name)

    monkeypatch.setattr(importlib, 'import_module', record)
    monkeypatch.setattr(conversion, '_COMPLETION_MODULES', {'imgui': 'imgui', 'glfw': 'glfw'})
    namespace = conversion._completion_namespace()
    assert namespace['imgui'] is sys.modules['meltygui_imgui']
    assert namespace['glfw'] is sys.modules['meltygui.core.windowing.window_api']
    assert 'glfw' not in requested
    code = 'value = "hello"\nvalue.'
    completed = conversion._submit_interactive(conversion._jedi_complete_worker, code, 2, 6).result(timeout=10)
    assert ('upper', 'function') in completed


def test_cancelling_queued_completion_preserves_other_jobs_and_shutdown(device_analysis):
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(timeout=5)
        return 1

    first = conversion._submit_interactive(slow)
    assert entered.wait(timeout=5)
    queued = conversion._submit_interactive(lambda: 2)
    assert queued.cancel()
    release.set()
    assert first.result(timeout=5) == 1
    assert queued.cancelled()
    conversion.shutdown_jedi_pool()
    assert conversion._submit_interactive(lambda: 3).result(timeout=5) == 3


def test_importing_converters_does_not_import_process_executor():
    script = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'multiprocessing' or name.startswith('multiprocessing.') or name == 'concurrent.futures.process':
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import meltygui.code.libcst_conversion
'''
    subprocess.run([sys.executable, '-c', script], check=True, close_fds=False)
