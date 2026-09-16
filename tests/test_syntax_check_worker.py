"""Disk-loaded source must stay on the isolated compiler path."""
import subprocess
import sys

import pytest


def test_disk_span_syntax_checks_remain_isolated_and_worker_closes():
    # Exercise real interpreter channels and teardown in a separate process:
    # a worker that wedges on close must fail with a timeout, not hang pytest.
    result = subprocess.run(
        [sys.executable, '-c', '''
from meltygui.code.chain_converters import DiskSpanText
from meltygui.code.syntax_check_worker import _CompilerWorker

worker = _CompilerWorker()
try:
    assert worker.check(DiskSpanText('value = 1\\n'), ()) is None
    error = worker.check(DiskSpanText('value = (\\n'), ())
    assert isinstance(error, SyntaxError), error
    assert error.lineno == 1
    assert not worker.unavailable, 'silently fell back to the UI interpreter'
finally:
    worker.close()
assert worker.thread is None
assert worker.interpreter is None
'''], capture_output=True, text=True, timeout=20, close_fds=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('source_type', ['str', 'DiskSpanText'])
def test_scan_worker_does_not_import_gui_or_hang_after_background_parse(source_type):
    result = subprocess.run(
        [sys.executable, '-c', '''
import threading
import _xxsubinterpreters as interpreters
from meltygui.code.chain_converters import DiskSpanText
from meltygui.code.core_syntax import _worker, parse_to_dict, general_parse_to_str

results = []
source = DiskSpanText('value = [1, 2, 3]\\n')
thread = threading.Thread(target=lambda: results.append(
    parse_to_dict(source, frontend='worker')))
thread.start()
thread.join()
assert len(results) == 1
assert not _worker._broken
assert general_parse_to_str(results[0]) == source
interpreters.run_string(_worker._interp, """
import sys
assert 'meltygui' not in sys.modules
assert 'meltygui.core.app' not in sys.modules
""")
# Let normal process finalization destroy the interpreter, after the
# background thread that first used it has gone away.
'''.replace("source = DiskSpanText(", f"source = {source_type}(")],
        capture_output=True, text=True, timeout=20, close_fds=False)
    assert result.returncode == 0, result.stdout + result.stderr
