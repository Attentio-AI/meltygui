"""Native launch failures retain their reason and command-line usage output."""
from types import SimpleNamespace

import pytest

from meltygui.platforms.ios.Python import melty_ios_bootstrap as bootstrap


def test_command_exit_shows_usage_without_a_crash_traceback(monkeypatch):
    written = []
    log = bootstrap._Log(SimpleNamespace(write_log=written.append))
    monkeypatch.setattr(bootstrap.sys, 'stderr', log)
    log.write('viewer IMAGE\nChoose an image to open.\n')
    try:
        raise SystemExit(2)
    except SystemExit as error:
        message = bootstrap.describe_failure(error)
    assert message.startswith('Program exited with code 2.')
    assert 'viewer IMAGE\nChoose an image to open.' in message
    assert 'Traceback' not in message
    assert written == ['viewer IMAGE\nChoose an image to open.\n']


@pytest.mark.parametrize(('code', 'expected'), [
    (None, 'Program finished.'),
    (0, 'Program finished.'),
    (3, 'Program exited with code 3.'),
    ('not a file: picture.png', 'Program exited: not a file: picture.png'),
])
def test_exit_reason_is_preserved(code, expected):
    assert bootstrap.describe_failure(SystemExit(code)).startswith(expected)


def test_real_exception_keeps_full_traceback_with_reason_first():
    try:
        raise ValueError('missing image data')
    except ValueError as error:
        message = bootstrap.describe_failure(error)
    assert message.startswith('ValueError: missing image data\n\nTraceback')
    assert 'test_real_exception_keeps_full_traceback_with_reason_first' in message
    assert message.endswith('ValueError: missing image data\n')


def test_console_tail_is_bounded_without_truncating_native_log(monkeypatch):
    written = []
    log = bootstrap._Log(SimpleNamespace(write_log=written.append))
    monkeypatch.setattr(bootstrap.sys, 'stderr', log)
    first, last = 'old output\n' * 2000, '\nviewer IMAGE\n'
    log.write(first)
    assert log.write(last) == len(last)
    assert len(log.tail) == 16000
    assert log.tail.endswith(last)
    assert ''.join(written) == first + last
    assert bootstrap.describe_failure(SystemExit(2)).endswith('viewer IMAGE')
