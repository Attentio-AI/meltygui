"""`Source(thing, at=...)` (model/source_version_model.py): the code at launch and at a git commit,
read only, in a repository made for the test."""
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

from meltygui.model import source_version_model
from meltygui.model.source_file_model import flush
from meltygui.model.source_model import Source
from meltygui.model.source_version_model import Fixed, capture, root_of

GIT = shutil.which('git')
FIRST = 'GAIN = 1\n\n\nclass Knobs:\n    level = 1\n'
SECOND = FIRST.replace('level = 1', 'level = 2')
THIRD = SECOND.replace('GAIN = 1', 'GAIN = 3')
FOURTH = THIRD.replace('level = 2', 'level = 4')


def run(folder, *arguments):
    subprocess.run([GIT, '-C', str(folder), '-c', 'user.name=test', '-c', 'user.email=test@example.com', *arguments],
                   check=True, capture_output=True, close_fds=False)


@pytest.fixture
def knobs(tmp_path, request):
    """A module in a repository of its own: committed as FIRST and SECOND, changed to THIRD and not committed."""
    path = tmp_path / 'pkg' / 'knobs.py'
    path.parent.mkdir()
    run(tmp_path, 'init', '-q')
    for text in (FIRST, SECOND):
        path.write_text(text)
        run(tmp_path, 'add', '.')
        run(tmp_path, 'commit', '-q', '-m', 'knobs')
    path.write_text(THIRD)
    (tmp_path / 'new.py').write_text('NEW = 1\n')               # untracked
    (tmp_path / 'large.py').write_text('LARGE = 1\n' * 10)      # untracked, over the cap of this test
    name = f'version_knobs_{request.node.name}'
    spec = importlib.util.spec_from_file_location(name, path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    del sys.modules[name]


def test_the_launch_version_is_the_working_state_when_the_first_source_was_made(knobs, tmp_path, monkeypatch):
    monkeypatch.setattr(source_version_model, 'LARGEST', 50)
    status = subprocess.run([GIT, '-C', str(tmp_path), 'status', '--porcelain'], capture_output=True, text=True, close_fds=False).stdout
    disk = Source(knobs.Knobs)                          # the first source of the repository: its state is kept from here
    kept = capture(knobs.__file__)
    kept.done.wait()
    assert kept.error is None and root_of(knobs.__file__) == str(tmp_path) and 'large.py' in kept.left and 'new.py' not in kept.left
    # Nothing was written into the repository: its index and its objects are as they were.
    assert subprocess.run([GIT, '-C', str(tmp_path), 'status', '--porcelain'], capture_output=True, text=True, close_fds=False).stdout == status
    assert subprocess.run([GIT, '-C', str(tmp_path), 'cat-file', '-e', kept.tree], capture_output=True, close_fds=False).returncode != 0
    # Unchanged since: read from disk. Then the file changes, and the launch version is still what it was.
    launch = Source(knobs.Knobs, at='launch')
    assert isinstance(launch.file, Fixed) and launch.file.fixed and launch.tree['level'] == 2 and launch.file.text == THIRD
    disk.tree['level'] = 4
    flush()
    assert open(knobs.__file__).read() == FOURTH and launch.tree['level'] == 2
    assert Source(knobs, at='launch').file is launch.file
    # A file first asked for after it changed is read from the captured tree.
    (tmp_path / 'new.py').write_text('NEW = 2\n')
    assert Source(tmp_path / 'new.py', at='launch').text == 'NEW = 1\n'
    source_version_model.VERSIONS.pop((knobs.__file__, 'launch'))
    assert Source(knobs, at='launch').text == THIRD
    # One left out for its size is said, not answered with another state.
    (tmp_path / 'large.py').write_text('LARGE = 2\n')
    large = Source(tmp_path / 'large.py', at='launch')
    assert large.text == '' and 'not kept' in large.file.error


def test_a_commit_is_read_like_the_file(knobs):
    assert Source(knobs.Knobs, at='HEAD').tree['level'] == 2 and Source(knobs, at='HEAD').text == SECOND
    assert Source(knobs.Knobs, at='HEAD~1').tree.plain() == {'level': 1} and Source(knobs, at='disk').text == THIRD
    assert Source(knobs, at='HEAD').file is Source(knobs.Knobs, at='HEAD').file is not Source(knobs, at='HEAD~1').file
    missing = Source(knobs, at='no-such-revision')
    assert missing.text == '' and len(missing.tree) == 0 and 'failed' in missing.file.error


def test_a_version_is_read_only(knobs):
    head = Source(knobs.Knobs, at='HEAD')
    head.tree['level'] = 9
    head.text = 'class Knobs:\n    level = 9\n'
    head.file.text = 'x = 1\n'
    assert head.file.splice(0, 0, 'x') is None and head.file.put('y') is None
    assert head.tree['level'] == 2 and head.file.text == SECOND and head.file.head == 1 and not head.file.grown.pending
    flush()
    assert open(knobs.__file__).read() == THIRD


def test_what_has_no_version(tmp_path, knobs):
    with pytest.raises(ValueError, match='not kept yet'):
        Source(knobs, at='running')
    loose = tmp_path.parent / f'loose_{os.getpid()}.py'
    loose.write_text('A = 1\n')
    try:
        assert root_of(str(loose)) is None
        for at in ('launch', 'HEAD'):
            with pytest.raises(LookupError, match='in no git repository'):
                Source(loose, at=at)
    finally:
        loose.unlink()
