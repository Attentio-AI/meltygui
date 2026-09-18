"""Crash Reports: every process's reports in one list, newest first, filtered by app ID."""
import time

import pytest

from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing import glfw_utils
from meltygui.core.windowing.surface import Surface
from meltygui.model.trace_model import ALL_APPS, UNKNOWN_APP, CrashReportStore


@pytest.fixture
def reports_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(Toggles.CrashReports, 'directory', str(tmp_path))
    monkeypatch.setattr(Toggles.CrashReports, 'auto_save', True)
    # The folder watch belongs to the running app; the store is what is under test.
    monkeypatch.setattr('meltygui.model.trace_report_model.watch_reports', lambda directory: None)
    return tmp_path


def save(monkeypatch, app, error):
    monkeypatch.setattr(Surface, 'app_id', app)
    path = glfw_utils.save_crash_report('Traceback', exception=error)
    time.sleep(0.002)                                   # names sort by millisecond
    return str(path)


def test_report_header_names_the_app(reports_folder, monkeypatch):
    path = save(monkeypatch, 'melty-code-editor', ValueError('boom'))
    assert 'app: melty-code-editor\n' in open(path).read().split('\n\n')[0]


def test_one_list_newest_first_across_apps(reports_folder, monkeypatch):
    first = save(monkeypatch, 'melty-code-editor', ValueError('first'))
    second = save(monkeypatch, 'melty-admin', KeyError('second'))
    third = save(monkeypatch, 'melty-code-editor', ZeroDivisionError('third'))
    store = CrashReportStore()
    store.refresh_if_stale()
    assert list(store) == [third, second, first]
    assert [entry['app'] for entry in store.values()] == ['melty-code-editor', 'melty-admin', 'melty-code-editor']
    assert store.apps() == ['melty-admin', 'melty-code-editor']
    assert [path for path, _entry in store.shown('melty-code-editor')] == [third, first]
    assert [path for path, _entry in store.shown(ALL_APPS)] == [third, second, first]


def test_report_without_app_header_is_unknown(reports_folder):
    (reports_folder / '2026-09-18_05-22-18-603-000_AttributeError.txt').write_text(
        'time: 2026-09-18 05:22:18\nthread: MainThread\npid: 1611912\nerror: AttributeError: x\n\nTraceback\n')
    store = CrashReportStore()
    store.refresh_if_stale()
    entry = next(iter(store.values()))
    assert (entry['app'], entry['pid']) == (UNKNOWN_APP, '1611912')
    assert store.apps() == [UNKNOWN_APP]


def test_clear_keeps_the_other_apps_reports(reports_folder, monkeypatch):
    kept = save(monkeypatch, 'melty-admin', KeyError('kept'))
    save(monkeypatch, 'melty-code-editor', ValueError('cleared'))
    store = CrashReportStore()
    store.refresh_if_stale()
    store.remove_all('melty-code-editor')
    assert list(store) == [kept]
    assert [path.name for path in reports_folder.glob('*.txt')] == [kept.rsplit('/', 1)[-1]]
