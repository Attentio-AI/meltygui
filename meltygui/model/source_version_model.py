"""Versions of the code that are not the file on disk: read only.

    Source(Toggles, at='launch')     # the code as it was when the app started
    Source(Toggles, at='HEAD~1')     # at a git commit: anything `git` reads as a revision

`version(path, at)` gives the whole file's text at that version as a `Fixed`, a `Source` nothing
can change: a write to it, or to its tree, is ignored. It is read on a thread; code outside a
frame that asks for it waits, a view draws it when it arrives (as a file's: source_file_model.py).

The launch version is not made by reading every file at launch. `capture` stores the working
state of the repository a file is in as a git tree, once per repository, on a thread: a copy of
the index in a temp folder (`GIT_INDEX_FILE`), with its objects in that folder too
(`GIT_OBJECT_DIRECTORY`; the repository's own are `GIT_ALTERNATE_OBJECT_DIRECTORIES`), the
changed and the untracked files up to `LARGEST` bytes added, then `git write-tree`. Nothing is
written into the repository. A file is then read like a commit's: `git cat-file blob
<tree>:<path>`. One that did not change since the capture (its ctime) is read from disk.

The capture starts with the first `Source(thing)` of a file in the repository, which is at
launch for one made at the top of a module. Lever: call `capture(path)` earlier, from the app's
boot, to have it for every app.

Levers: `LARGEST`; `git` (how a process is started); `version` (what `at` can be).
Not there: 'running' (the code being executed: docs/NATIVE_EDITOR.md); a file outside a git
repository has no launch version; another worktree.
Tests: tests/test_source_version.py.
"""
import atexit
import os
import shutil
import tempfile
import threading
import time

from meltygui.model.source_file_model import File

# Lever: a changed or untracked file larger than this is left out of the launch capture.
LARGEST = 1 << 20
# Lever: how long before the capture a file must have changed last to count as unchanged since
# (nanoseconds). A file's ctime is stamped by a coarser clock than the one read here.
SLACK = 50_000_000
ROOTS = {}        # folder -> the root of the repository it is in, or None
CAPTURES = {}     # repository root -> Capture
VERSIONS = {}     # (real path, at) -> Fixed: one text for everyone who names that version
_starting = threading.Lock()


class Fixed(File):
    """The text of a file at a version that is not the file. A `Source` like any, read only."""
    fixed = True

    def __init__(self, path, read):
        File.__init__(self, path, disk=False)
        threading.Thread(target=self.fill, args=(read,), name='melty-source-version', daemon=True).start()

    def fill(self, read):
        try:
            found, error = read(), None
        except Exception as problem:
            found, error = b'', f'{type(problem).__name__}: {problem}'
        self.native.fill(found, error)

    def splice(self, low, high, new, version=None, by=None):
        """Ignored: this version of the code is not the file."""


def root_of(path):
    """The root of the git repository `path` is in, or None. Folders are looked at once."""
    folder = os.path.dirname(path)
    if folder not in ROOTS:
        at = folder
        while not os.path.exists(os.path.join(at, '.git')):
            above = os.path.dirname(at)
            if above == at:
                at = None
                break
            at = above
        ROOTS[folder] = at
    return ROOTS[folder]


def git(root, *arguments, env=None):
    """What `git -C root arguments` prints. Raises LookupError when git fails or is not installed.
    A full path started by `posix_spawn`: the app is never forked."""
    program = shutil.which('git')
    if program is None:
        raise LookupError('git is not installed')
    argv = [program, '-C', root, *arguments]
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', **(env or {})}
    if not hasattr(os, 'posix_spawn'):
        import subprocess
        done = subprocess.run(argv, env=env, capture_output=True, close_fds=False)
        if done.returncode:
            raise LookupError(done.stderr.decode(errors='replace').strip() or f'git {arguments[0]} failed')
        return done.stdout
    read, write = os.pipe()
    try:
        pid = os.posix_spawn(program, argv, env, file_actions=[
            (os.POSIX_SPAWN_DUP2, write, 1), (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0)])
    finally:
        os.close(write)
    with open(read, 'rb') as out:
        found = out.read()
    if os.waitpid(pid, 0)[1]:
        raise LookupError(f'git {" ".join(arguments[:3])} failed')
    return found


class Capture:
    """The working state of one repository as a git tree, with its objects outside the repository."""
    __slots__ = ('root', 'folder', 'done', 'time', 'tree', 'env', 'left', 'error')

    def __init__(self, root):
        self.root = root
        self.folder = tempfile.mkdtemp(prefix='melty-launch-')     # the copied index and the objects written for the tree
        self.done = threading.Event()
        self.time = time.time_ns() - SLACK      # a file whose ctime is before this is as it was
        self.tree = None               # the tree's name, once written
        self.env = None                # what points git at the copied index and the objects beside it
        self.left = set()              # files left out for their size
        self.error = None
        atexit.register(self.close)
        threading.Thread(target=self.work, name='melty-source-launch', daemon=True).start()

    def close(self):
        """The process ends: the folder goes, once the thread is out of it."""
        self.done.wait(1.)
        shutil.rmtree(self.folder, ignore_errors=True)

    def work(self):
        try:
            root, folder = self.root, self.folder
            base, shared = git(root, 'rev-parse', '--path-format=absolute', '--git-dir', '--git-common-dir').decode().split('\n')[:2]
            index = os.path.join(folder, 'index')
            if os.path.exists(os.path.join(base, 'index')):
                shutil.copyfile(os.path.join(base, 'index'), index)
            env = {'GIT_INDEX_FILE': index, 'GIT_OBJECT_DIRECTORY': os.path.join(folder, 'objects'),
                   'GIT_ALTERNATE_OBJECT_DIRECTORIES': os.path.join(shared, 'objects'), 'GIT_LITERAL_PATHSPECS': '1'}
            os.mkdir(env['GIT_OBJECT_DIRECTORY'])
            # What differs from the index: changed, deleted and untracked files, each `XY path`.
            entries = git(root, 'status', '--porcelain', '-z', '--untracked-files=all', '--no-renames', env=env).split(b'\0')
            paths = []
            for entry in entries:
                path = entry[3:]
                if not path:
                    continue
                try:
                    large = os.stat(os.path.join(os.fsencode(root), path)).st_size > LARGEST
                except OSError:
                    large = False      # deleted: the tree has it no longer
                (self.left.add if large else paths.append)(os.fsdecode(path) if large else path)
            if paths:
                listed = os.path.join(folder, 'paths')
                with open(listed, 'wb') as file:
                    file.write(b'\0'.join(paths))
                git(root, 'add', '--all', f'--pathspec-from-file={listed}', '--pathspec-file-nul', env=env)
            self.tree, self.env = git(root, 'write-tree', env=env).decode().strip(), env
        except Exception as problem:
            self.error = f'the launch state of {self.root} was not captured ({type(problem).__name__}: {problem})'
        self.done.set()

    def read(self, path):
        """The bytes of `path` as they were at the capture."""
        if os.stat(path).st_ctime_ns < self.time:
            with open(path, 'rb') as file:
                found = file.read()
            if os.stat(path).st_ctime_ns < self.time:
                return found               # as it was, and still is
        self.done.wait()
        name = os.path.relpath(path, self.root)
        if self.error or name in self.left:
            raise LookupError(self.error or f'it was over {LARGEST} bytes at launch: not kept')
        return git(self.root, 'cat-file', 'blob', f'{self.tree}:{name.replace(os.sep, "/")}', env=self.env)


def capture(path):
    """Keep the launch state of the repository `path` is in (once per repository; nothing when
    it is in none). Returns its `Capture`, at work on its thread."""
    root = root_of(path)
    if root is None:
        return None
    found = CAPTURES.get(root)
    if found is None:
        with _starting:
            found = CAPTURES.get(root)
            if found is None:
                found = CAPTURES[root] = Capture(root)
    return found


def version(path, at):
    """The text of the file `path` at version `at`, as a `Fixed`: the same one for every caller,
    kept for the process. A revision is read once: 'HEAD' stays the commit it was when first
    asked for (lever: drop its row of `VERSIONS`). To add a version, add its row here."""
    path = os.path.realpath(path)
    found = VERSIONS.get((path, at))
    if found is not None:
        return found
    if at == 'running':
        raise ValueError("Source(thing, at='running'): the code being executed is not kept yet")
    root = root_of(path)
    if root is None:
        raise LookupError(f'{path} is in no git repository: it has no version {at!r}')
    if at == 'launch':
        kept = capture(path)
        read = lambda: kept.read(path)
    else:
        name = os.path.relpath(path, root).replace(os.sep, '/')
        read = lambda: git(root, 'cat-file', 'blob', f'{at}:{name}')
    return VERSIONS.setdefault((path, at), Fixed(path, read))
