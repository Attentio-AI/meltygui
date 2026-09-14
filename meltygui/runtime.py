import math
import os
import threading as _threading
import time
import types
from contextlib import contextmanager
from collections import defaultdict, deque
from copy import copy
from enum import Enum
from typing import MutableMapping, Optional

import glfw
import imgui
from src.lsd.gl_gui import hdr_color
from src.lsd.gl_gui.hdr_color import pack_color
from imgui.core import _DrawList

from src.lsd.gl_gui.notifications import draw_notifications, notify
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.shaped import best_match
from src.lsd.gl_gui.mode_defaults import register_defaults
from src.lsd.gl_gui.utils import glfw_utils
from src.lsd.gl_gui.view.attribute_churn import AttributeChurnMonitor
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.invalidation_tracker import InvalidateTracker, Note

from src.lsd.gl_gui.background import Background
from src.lsd.gl_gui.collection_action import CollectionAction
from src.lsd.gl_gui.collision import Collisions
from src.lsd.gl_gui.toggles import Toggles, Counters, Tint, Swoosh, SwooshMode
from src.lsd.gl_gui.fonts import Font, detect_auto_scale
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import set_window_registrar
from src.lsd.gl_gui.view.core_views.monitor import Monitor
from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager
from src.shader_library.shader_manager.texture_manager import TextureManager
from src.shader_library.shader_manager.filter import Filter
# Importing shaders.py is what registers every built-in @register_shader class
# (brightness_contrast, normalize_remap, ...) on the registry Filter reads. Nothing
# else in src/ imports it - our launcher force-executed it as a main module - so a
# vanilla `python latent_descent.py` came up with an EMPTY registry (09-04).
import src.shader_library.shader_manager.shaders  # noqa: F401
from src.lsd.gl_gui.events.input_handler import InputHandler, InputEvent, EventAction
from src.lsd.gl_gui.events.event_backends import ImGuiBackend, GlfwQueueBackend
from src.lsd.gl_gui.events import space_mouse
from src.lsd.gl_gui.model.core_model.core_enums import generate_id
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace, clamp_ui_scale
from src.lsd.gl_gui.perf_trace import trace as _ptrace

import OpenGL.GL as gl
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, no_save
from src.lsd.gl_gui.model.dict_conversion import DictConversion

_MOUSE_INPUTS = frozenset({'left_mouse', 'right_mouse', 'middle_mouse',
                           'cursor', 'scroll_y', 'scroll_x'})


class SearchTerm(str):
    """The active search string, plus a shared aggregation session.

    A search owner pushes one of these onto Melty.search_stack; it forwards to
    every searchable descendant as their `search_text`. Because it's a str
    subclass, views can match against it directly, while the extra fields let
    all views contribute to a single combined result set:

      - current:  the global index (0..total-1) the owner wants selected
      - scroll_to: whether the view holding `current` should scroll to it
      - offset:   running base index; each view claims [offset, offset+count)
      - total:    grand total across every view (read back by the owner)

    offset/total are reset each frame (a fresh SearchTerm is pushed) and grow
    as views register, in render order, so `current` maps to one match in one
    view deterministically.
    """
    def __new__(cls, value="", current=0, scroll_to=False):
        obj = super().__new__(cls, value)
        obj.current = current
        obj.scroll_to = scroll_to
        obj.offset = 0
        obj.total = 0
        return obj

    def claim(self, count):
        """Register `count` matches for the calling view; return its base
        offset and the local index of the global-current match (or None)."""
        base = self.offset
        self.offset += count
        self.total += count
        if count and base <= self.current < base + count:
            return base, self.current - base
        return base, None


#comment
new_int=1



def search_walk(ds, term, session, max_depth=12):
    """Count a subtree's matches into `session` AND mark the current one — the
    single source of truth for both the find UI count and the selection.

    Each searchable view stashes a `_search_matcher(term, session)` closure on
    its draw_state during render (capturing its content). Here we walk the live
    draw_state tree (`ds` plus its descendants, via DrawState.descendants) and
    invoke each matcher, so the whole subtree — including off-screen rows the
    render skips, whose matcher persists from when they last drew — contributes.
    Each matcher claims only its own direct matches; the walk supplies the
    recursion, so siblings and nested views sum without double-counting.

    As it goes it records, on each node, the local index of `session.current`
    when that global match lands in this node (_search_active_local, else None),
    and returns the node holding it. Views read that while drawing to highlight
    the right match, so the count and the selection can never disagree.
    """
    current_node = None
    for node in (ds, *ds.descendants(max_depth=max_depth)):
        matcher = getattr(node, '_search_matcher', None)
        if matcher is None:
            continue
        # Skip matchers sitting in a closed/collapsed subtree - e.g. a context
        # menu or dropdown that was opened over a node and then dismissed. Its
        # matcher persists from its last render, so without this its stale
        # matches inflate the find count (and steal the current-match mark) for
        # nodes nothing is showing. abs_closed is critical here: scrolled-off
        # rows are NOT abs_closed (their window is open + section expanded), so
        # the off-screen-rows behaviour this walk exists for is preserved.
        if node.abs_closed:
            node._search_active_local = None
            continue
        base = session.offset
        matcher(term, session)
        count = session.offset - base
        if count and base <= session.current < base + count:
            node._search_active_local = session.current - base
            current_node = node
        else:
            node._search_active_local = None
    return current_node

import hashlib
import difflib
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class FileWatch:
    observer = Observer()
    handler = FileSystemEventHandler()
    # Every directory events are expected from (by any means) - what the
    # debug logs call "scheduled"; NOT one emitter each: see watch_dir.
    _watched_dirs = set()
    # Recursive roots (watch_recursive) and the per-dir emitters (watch_dir)
    # this class actually scheduled on the observer. watchdog's inotify
    # backend opens a separate inotify INSTANCE per scheduled watch, and the
    # kernel caps instances per user (fs.inotify.max_user_instances, 128 by
    # default - shared with every other app). A per-dir emitter under a
    # recursive root is pure waste (duplicate events + an instance), so
    # watch_dir skips it and watch_recursive retires any it supersedes.
    _recursive_roots = set()
    _dir_watches = {}          # dirpath → ObservedWatch (own emitters only)
    path_to_draw_states = {}   # path → set of draw_states
    draw_state_to_path = {}
    _ds_hashes = {}            # draw_state → hash (per-view, not per-path)
    _ds_suppress_until = {}    # id(ds) → monotonic time until which to suppress dispatch
    _file_contents = {}
    _path_hash_cache = {}      # resolved path → (mtime, md5); avoids re-reading unchanged files
    _self_write_hashes = {}    # resolved path → md5 of the last IN-PROCESS write (any view)
    _self_write_text = {}      # resolved path → full text of that write (in-process reload, no disk read)
    output_debug_diff = False
    _write_suppress_window = 1.0  # seconds - for truncate+write event pairs from write_text
    # Global file-event listeners: called with every event's src_path on the
    # OBSERVER thread, before (and regardless of) the per-draw_state dispatch
    # - so a subscriber sees changes to files no view is watching. The symbol
    # index subscribes here (libcst_conversion._on_watch_event). Listeners
    # must be fast/non-blocking (debounce internally); exceptions swallowed.
    global_listeners = []
    # Resolved absolute paths of every project .py file registered by
    # watch_project_files - files watched for EXTERNAL-change tracking even
    # though no view has loaded them. Membership drives the baseline re-read
    # in _on_event (view-less files have no reader to repopulate code_cache
    # after an event pops it, so we re-read here or the SECOND external edit
    # would have no diff baseline).
    project_tracked = set()

    @classmethod
    def _covered(cls, dirpath):
        """Is `dirpath` (resolved str) under one of the recursive roots?"""
        for root in cls._recursive_roots:
            if dirpath == root or dirpath.startswith(root + os.sep):
                return True
        return False

    @classmethod
    def watch_dir(cls, dirpath):
        """Make sure events arrive for files in `dirpath` (resolved str):
        a no-op when a recursive root already covers it, else one
        non-recursive emitter. Returns False if the observer refused
        (typically EMFILE — the instance cap)."""
        dirpath = str(dirpath)
        if dirpath in cls._watched_dirs:
            return True
        if cls._covered(dirpath):
            cls._watched_dirs.add(dirpath)
            return True
        try:
            cls._dir_watches[dirpath] = cls.observer.schedule(
                cls.handler, dirpath, recursive=False)
        except OSError as e:
            print(f"FileWatch: cannot watch {dirpath}: {e}")
            return False
        cls._watched_dirs.add(dirpath)
        return True



    @classmethod
    def watch_recursive(cls, root):
        """One recursive emitter over `root` (resolved str) — a single
        inotify instance for the whole tree — retiring any per-dir emitters
        it now covers (their dirs stay in _watched_dirs: still watched)."""
        root = str(root).rstrip(os.sep) or os.sep
        if root in cls._recursive_roots:
            return True
        try:
            cls.observer.schedule(cls.handler, root, recursive=True)
        except OSError as e:
            print(f"FileWatch: cannot watch {root} recursively: {e}")
            return False
        cls._recursive_roots.add(root)
        for d, watch in list(cls._dir_watches.items()):
            if cls._covered(d):
                try:
                    cls.observer.unschedule(watch)
                except Exception:
                    pass
                cls._dir_watches.pop(d, None)
        return True

    @classmethod
    def watch_project_files(cls, root=None):
        """Register every project .py file the way a code view's
        register_draw_state does — schedule its directory on the observer and
        baseline its text in Melty.code_cache — minus the per-draw_state
        dispatch state (no view exists). This is what lets ExternalChanges /
        recompile_external_changes see edits to files the studio never opened:
        _on_event's diff baseline is the popped code_cache entry, so an
        unwatched or uncached file's external edit was invisible before.

        Idempotent and cheap on re-run (dirs dedupe via _watched_dirs; only
        uncached files are read), so recompile calls it again to pick up
        files/dirs created since startup. One-time O(project) read on first
        call — run it off the render thread."""
        from src.lsd.gl_gui.view.core_conversion.address import _PROJECT_ROOT
        root = Path(root or _PROJECT_ROOT).resolve()
        skip = {"__pycache__", "venv", ".venv", "venv-backup", "node_modules",
                "build", "dist", "resources", "tests"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in skip and not d.startswith(".")]
            pys = [f for f in filenames if f.endswith(".py")]
            if not pys:
                continue
            if not cls.watch_dir(dirpath):
                continue
            for f in pys:
                resolved = os.path.join(dirpath, f)
                cls.project_tracked.add(resolved)
                if resolved not in Melty.code_cache:
                    try:
                        Melty.read_code(resolved)
                    except Exception:
                        pass

    @classmethod
    def unwatch_dir(cls, dirpath):
        """Retire the per-dir emitter `watch_dir` scheduled for `dirpath`
        (a no-op for a dir a recursive root covers, or one never watched).
        A browser that walks directories must give each one back, or it
        eats an inotify instance per visit (the 128-per-user cap)."""
        dirpath = str(dirpath)
        cls._watched_dirs.discard(dirpath)
        watch = cls._dir_watches.pop(dirpath, None)
        if watch is not None:
            try:
                cls.observer.unschedule(watch)
            except Exception as e:
                print(f"FileWatch: cannot unwatch {dirpath}: {e}")

    @classmethod
    def start(cls):
        # Idempotent: the studio starts it from Melty.init; a @glfw_window
        # app starts it from the first view that watches a directory.
        if cls.observer.is_alive():
            return
        cls.handler.on_modified = cls._on_event
        cls.handler.on_created = cls._on_event
        cls.handler.on_moved = cls._on_moved
        cls.handler.on_deleted = cls._on_deleted
        cls.observer.start()

    @classmethod
    def _on_deleted(cls, event):
        """A deletion reaches the global listeners only (a directory
        listing wants it); the per-draw_state dispatch is for content
        changes of files views hold, which a deletion is not."""
        for listener in list(cls.global_listeners):
            try:
                listener(event.src_path)
            except Exception:
                pass

    @classmethod
    def _on_moved(cls, event):
        # Editors that save via atomic rename (write .tmp → os.replace target)
        # never fire on_modified for the target - only a MOVED event whose
        # dest_path is the real file. Route it through _on_event as a normal
        # modification of the destination so those saves aren't invisible.
        dest = getattr(event, "dest_path", None)
        if dest:
            cls._on_event(types.SimpleNamespace(src_path=dest))

    @classmethod
    def _get_hash(cls, path):
        # Cache by (path, mtime): register_draw_state hashes the file to set a
        # baseline, and can be called repeatedly during frame. Reading + MD5'ing
        # the whole file every time is what made typing in large files stall.
        # While typing the file isn't written (mtime unchanged) so we return the
        # cached digest; a save bumps mtime so a re-read exactly once.
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return None
        cached = cls._path_hash_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, 'rb') as f:
                digest = hashlib.md5(f.read()).hexdigest()
        except OSError:
            return None
        cls._path_hash_cache[path] = (mtime, digest)
        return digest

    @classmethod
    def _read_text(cls, path):
        try:
            with open(path, 'r') as f:
                return f.readlines()
        except (OSError, UnicodeDecodeError):
            return []

    @classmethod
    def _on_event(cls, event):
        if Melty.frame_count < 2:
            return
        # Invalidate any cached file text for the changed path so the next read
        # re-reads from disk. Done before the no-draw_states early-return so a
        # sibling file in a watched dir (cached by the symbol index but with no
        # view of its own) is still invalidated. Keyed the same as
        # path_to_draw_states - str(path.resolve) - so event.src_path matches.
        old_text = Melty.code_cache.pop(event.src_path, None)
        # The disk has moved: tinting over disk / sync-frame tables
        # (symbol_roster.World) key on this generation.
        try:
            from src.lsd.gl_gui.view.core_conversion.symbol_roster import (
                bump_disk_generation)
            bump_disk_generation()
        except Exception:
            pass
        # External-change tracking: the popped cache text is the last content
        # the studio READ - the diff baseline for an outside edit. Lazy import
        # (the gui stack can't be imported at melty load); exceptions swallowed
        # like global_listeners - this runs on the observer thread.
        if old_text is not None:
            try:
                from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
                ExternalChanges.on_file_event(event.src_path, old_text)
            except Exception:
                pass
        elif (event.src_path not in cls.project_tracked
                and event.src_path.endswith(".py")):
            # A project .py the walk never saw - a file created after startup
            # (or in a fresh dir another event reached). Track it with an
            # EMPTY baseline so the external window shows it as all-added and
            # recompile can absorb it.
            try:
                from src.lsd.gl_gui.view.core_conversion.address import is_editable_source
                if is_editable_source(event.src_path):
                    cls.project_tracked.add(event.src_path)
                    from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
                    ExternalChanges.on_file_event(event.src_path, "")
            except Exception:
                pass
        # Re-arm the baseline for the NEXT edit: views re-read their file on
        # dispatch, but a tracked view-less file has no reader - without this
        # its second external edit would find code_cache empty and go
        # untracked. One str object per read keeps id(...) change signals
        # (external window sig, merge memo) honest.
        if event.src_path in cls.project_tracked:
            try:
                Melty.read_code(event.src_path)
            except Exception:
                pass
        for listener in list(cls.global_listeners):
            try:
                listener(event.src_path)
            except Exception:
                pass
        draw_states = cls.path_to_draw_states.get(event.src_path)
        if not draw_states:
            return
        new_hash = cls._get_hash(event.src_path)
        if not new_hash:
            return
        now = time.monotonic()
        debug_printed = False
        for ds in list(draw_states):
            # Same-view writes (set_hash_from_content) open a short suppress
            # window. Multiple fs events may fire during one write (e.g. truncate
            # then flush). Within the window we keep _ds_hashes synced with
            # disk but never dispatch - so the view doesn't reload its own
            # write, or intermediate events it never produced.
            if now < cls._ds_suppress_until.get(id(ds), 0):
                cls._ds_hashes[id(ds)] = new_hash
                continue
            if new_hash != cls._ds_hashes.get(id(ds)):
                if cls.output_debug_diff and not debug_printed:
                    old_lines = cls._file_contents.get(event.src_path, [])
                    new_lines = cls._read_text(event.src_path)
                    diff = difflib.unified_diff(
                        old_lines, new_lines,
                        fromfile=f"{event.src_path} (old)",
                        tofile=f"{event.src_path} (new)",
                    )
                    if ''.join(diff):
                        print(''.join(diff))
                    debug_printed = True

                cls._ds_hashes[id(ds)] = new_hash
                cls.dispatch_event_for(ds)

        if cls.output_debug_diff:
            cls._file_contents[event.src_path] = cls._read_text(event.src_path)

    @classmethod
    def register_draw_state(cls, draw_state, path: Path):
        # Early-out BEFORE the resolve - this runs per frame per code view, and
        # path.resolve() is ~30 syscall/GIL round-trips - under a CPU-bound bg
        # thread that stretched frames (2026-07-31 stall sampling). The check
        # never depends on `resolved` (a re-register with a DIFFERENT path
        # also returned here), so hoisting it is behavior-identical.
        if draw_state in cls.draw_state_to_path:
            return
        resolved = str(path.resolve())

        old_path = cls.draw_state_to_path.pop(draw_state, None)
        if old_path:
            ds_set = cls.path_to_draw_states.get(old_path)
            if ds_set:
                ds_set.discard(draw_state)
                if not ds_set:
                    cls.path_to_draw_states.pop(old_path, None)
            cls._ds_hashes.pop(id(draw_state), None)
            cls._ds_suppress_until.pop(id(draw_state), None)
            if not cls.path_to_draw_states.get(old_path):
                cls._file_contents.pop(old_path, None)

        if resolved not in cls.path_to_draw_states:
            cls.path_to_draw_states[resolved] = set()
        cls.path_to_draw_states[resolved].add(draw_state)
        cls.draw_state_to_path[draw_state] = resolved
        cls._ds_hashes[id(draw_state)] = cls._get_hash(resolved)

        if cls.output_debug_diff:
            cls._file_contents[resolved] = cls._read_text(resolved)

        cls.watch_dir(os.path.dirname(resolved))

    @classmethod
    def dispatch_event_for(cls, draw_state):
        # The file moved on disk (often a SIBLING def edited in another window,
        # which moved this one's line span). The codec caches the resolved
        # Address on draw_state._addr_cache keyed by (source, mtime); busting it
        # here forces resolve_address to re-resolve the span the next time this
        # view runs, instead of handing back the stale cached Address. Without
        # this, a sibling edit leaves code_state.address pointing at the OLD span.
        if getattr(draw_state, '_addr_cache', None) is not None:
            draw_state._addr_cache = None

        # Invalidate both the parent window AND this view's own tile. The
        # _addr_cache + code_state live on THIS draw_state, so its tile must
        # re-execute resolve_address - invalidating only the parent window can
        # leave this nested code_file_io tile served from cache (stale address).
        # if draw_state._tile_id is not None:
        #     Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
        # if draw_state.parent_window is not None and draw_state.parent_window._tile_id is not None:
        #     Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10, force=True)

        draw_state._external_change = True
        request_render()


    @classmethod
    def set_hash_from_content(cls, path: Path, content: str, draw_state=None):
        """Pre-set hash from known content. Call before write.

        Also opens a brief suppress window on the target draw_state(s) so
        intermediate fs events from the upcoming write (e.g. the open("w")
        truncate before the flush) don't trigger a self-reload.

        If draw_state is given, only update that view's hash — other
        views watching the same path will see the write as an external change.
        Otherwise update all draw_states for the path (old behaviour).
        """
        resolved = str(path.resolve())
        new_hash = hashlib.md5(content.encode()).hexdigest()
        suppress_until = time.monotonic() + cls._write_suppress_window

        # Process-wide record: EVERY in-process save announces its content here
        # (codec.save / _do_save call this right before writing). is_self_write
        # lets a SIBLING view of the same file tell "one of us wrote this" from
        # "an outside program wrote this" - the per-draw_state hash above only
        # covers the writer's own view. The TEXT is recorded alongside the hash
        # so a sibling can reload its span IN-PROCESS (get_self_write_text) with
        # no disk read and no "loaded from disk" event - see code_file_io.
        cls._self_write_hashes[resolved] = new_hash
        cls._self_write_text[resolved] = content

        if draw_state is not None:
            cls._ds_hashes[id(draw_state)] = new_hash
            cls._ds_suppress_until[id(draw_state)] = suppress_until
        else:
            for ds in list(cls.path_to_draw_states.get(resolved, ())):
                cls._ds_hashes[id(ds)] = new_hash
                cls._ds_suppress_until[id(ds)] = suppress_until

        if cls.output_debug_diff:
            cls._file_contents[resolved] = content.splitlines(keepends=True)

    @classmethod
    def is_self_write(cls, path):
        """True when the file's CURRENT on-disk content is the last write made
        by an in-process editor (any code_file_io / _do_save instance) — a
        sibling view syncing through the file, not an outside program. Used to
        reload quietly instead of stamping the "loaded from disk" indication."""
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            return False
        recorded = cls._self_write_hashes.get(resolved)
        if recorded is None:
            return False
        return cls._get_hash(resolved) == recorded

    @classmethod
    def get_self_write_text(cls, path):
        """The exact full text of the last in-process write to `path`, but ONLY
        while the file on disk still holds it (same hash check as is_self_write).
        Returns None when an external write has since landed — the caller then
        falls back to a real disk load. Lets a sibling editor reload its span
        from memory on a self-write instead of re-reading the file."""
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            return None
        recorded = cls._self_write_hashes.get(resolved)
        if recorded is None or cls._get_hash(resolved) != recorded:
            return None
        return cls._self_write_text.get(resolved)

    @classmethod
    def shutdown(cls):
        if cls.observer.is_alive():
            cls.observer.stop()
            cls.observer.join()
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
            shutdown_jedi_pool, shutdown_symbol_index_daemon)
        shutdown_jedi_pool()
        # Stops the warmer daemon + clears its process guard (a future
        # restart-in-place then starts a new one) and prunes stale spans
        # from the in-memory symbol store.
        shutdown_symbol_index_daemon()

        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        PendingSave.apply_all_saves()

        # Same warm-start treatment for the span parse cache (cst dicts):
        # flush to ~/.lsd/cst_dict_cache.pkl so a fresh start skips the
        # cst.parse_module + cst_module_to_dict cost to open a view.
        # After apply_all_saves: the shutdown harvest re-keys the live hosts'
        # parses to the FINAL disk mtimes - before the flush they'd be keyed
        # to a disk state the pending writes are about to replace.
        from src.lsd.gl_gui.view.core_conversion.chain_converters import save_cst_dict_cache
        save_cst_dict_cache()


_RESOLVED_PATH_MEMO = globals().get("_RESOLVED_PATH_MEMO", {})   # str(path) → resolved key (Melty.read_code)


class _LazyRtree:
    """An rtree.index.Index built on first use, so importing melty does not pay
    for rtree (~9 ms) — a host that never draws a BVH never loads it."""
    __slots__ = ('_index',)

    def __init__(self):
        self._index = None

    def __getattr__(self, name):
        if self._index is None:
            from rtree import index as rtree_index
            self._index = rtree_index.Index()
        return getattr(self._index, name)


# Marks a cls.backgrounds entry re-submitted from a cache-sourced background's
# stamped colour (Melty.add_cached_background).
_CACHED_BACKGROUND = object()


class Melty:

    draw_state_registry = None
    style_manager: ImGuiStyleManager = None

    focused_ds = None
    text_focused_ds = None
    popover_focused_ds = None
    # id() of the front-most draw_state under the cursor within the open popover,
    # last frame - so begin_frame only re-runs the popover when the hovered row
    # changes (not every frame the pointer sits over one).
    _last_popover_hover = None
    # frame_count when a popover last opened - clear_focus grants it a one-frame
    # pass so the opening click can't immediately dismiss it.
    _popover_open_frame = 0
    # frame_count when text focus was last GRANTED (draw_text's request_focus /
    # rebind + click paths stamp this). clear_focus skips clearing a same-frame
    # grant: the click that opens a find bar / dropdown / context menu is routed
    # to window wrappers in the same frame the freshly-opened search box first
    # claims focus, and wrappers that run after the grant used to skip it (the
    # box is never under the opening click, so skip_this couldn't protect it).
    _text_focus_grant_frame = -99

    # Previous frame's imgui io.want_text_input - used to detect when an imgui
    # input widget newly captures the keyboard (rising edge), so a Melty text
    # editor and an imgui input_text never hold focus at once. See begin_frame.
    _prev_imgui_want_text = False
    selected = set()
    last_selected = None
    large_font = None
    font_mgr = None

    root_draw_states = defaultdict(lambda: list())
    root_draw_states_by_layer = defaultdict(lambda: list())
    paint_ordered_ds = []

    # Callables posted from worker threads, drained on the render thread at
    # end_frame (_drain_render_tasks) - for work that must not race a frame
    # in progress, e.g. mutating a live view-model tree that frame walkers
    # iterate. post_to_render wakes the loop, so an idle thread drains promptly.
    _render_tasks = []
    _render_tasks_lock = _threading.Lock()

    filter = Filter()
    detached = False

    @classmethod
    def default_framebuffer(cls):
        """The framebuffer a frame's draws go to: the fp16 scene target
        (scene_target.py) while a frame is open, else the window's 0. Every
        'bind 0' inside a frame must go through here."""
        from src.lsd.gl_gui import scene_target
        return scene_target.framebuffer()

    seen_values = []

    window_drag = False
    on_drag = False
    # A 3D-mouse flight is in progress (events/space_mouse.py, set per frame
    # right after its pump). Folded into on_drag below; the gates that poll
    # the mouse buttons directly read this beside them.
    space_mouse_drag = False
    on_scroll = False

    # Frame guard: some view rendered a VALUE-PENDING placeholder this frame (a
    # host with nothing held yet, a "Parsing..." line, an unloaded image). While
    # stamped, the render_func wrapper's auto_resize guard refuses to SHRINK a
    # draw_state's persisted content_height - the collapse is the placeholder,
    # not the content, and committing it is what threw away last session's
    # state and made views re-settle when the value landed. Growth commits
    # normally, and once the placeholder renders the guard is inert.
    pending_placeholder_frame = -1
    on_scroll_buffer = deque(maxlen=5)
    last_scroll_time = 0

    mode_stack = []
    search_stack = []
    # Active data-source codec stack. core_render pushes a value's codec
    # (function / call-site / class / decorations) on entry and pops on exit,
    # mirroring mode_stack. Every draw_state in the subtree stashes the top as
    # ds._codec, so any descendant view can ask "which data source am I
    # rendering?" (e.g. to apply the codec's source tint prominently).
    codec_stack = []
    # The draw_state holding the current search match (set by the search owner's
    # pre-body walk). The find UI resolves this to a click target on Ctrl+Enter
    # (see new_core_view.search_activate_target) and injects a mouse-down there.
    search_current_node = None
    # (tile_id, InputEvent) queued by Ctrl+Enter to "click" the selected search
    # result. Applied in begin_frame, right after events are rebuilt and before
    # target renders, so the target reliably reads it (the find UI renders too
    # late in the frame to inject directly). One-shot.
    search_click_pending = None

    # ── App-load lifecycle ──────────────────────────────────────────
    # Callbacks fired once, after the app model has finished loading (the
    # studio fires fire_on_load right after load_app_model). Registering after
    # the fire runs the callback immediately, so feature modules imported
    # later in the load (the feature imports) still get the hook.
    # Each callback: fn(vis, root).
    on_load_callbacks = []
    _on_load_fired = None  # (vis, root) once fired

    @classmethod
    def on_load(cls, func):
        """Register an app-loaded lifecycle callback (decorator-friendly).
        Re-registration from a hotswap re-exec replaces the old callback
        (keyed on module+qualname) instead of stacking a duplicate."""
        key = (getattr(func, "__module__", None), getattr(func, "__qualname__", None))
        cls.on_load_callbacks = [
            f for f in cls.on_load_callbacks
            if (getattr(f, "__module__", None), getattr(f, "__qualname__", None)) != key]
        cls.on_load_callbacks.append(func)
        if cls._on_load_fired is not None:
            cls._run_on_load(func, *cls._on_load_fired)
        return func

    @classmethod
    def fire_on_load(cls, vis, root):
        cls._on_load_fired = (vis, root)
        for func in list(cls.on_load_callbacks):
            cls._run_on_load(func, vis, root)

    @staticmethod
    def _run_on_load(func, vis, root):
        # One broken callback must not take down the load (or the others).
        try:
            func(vis, root)
        except Exception:
            import traceback
            print(f"[on_load] callback {getattr(func, '__qualname__', func)} FAILED:")
            traceback.print_exc()

    _converters = {}
    _converter_to_type = {}
    converter_flags_by_type = {}
    converter_flags = {}
    # FIM code completion registries (fim.py): provider functions, named
    # profiles, context sources. On Melty so hotswap's registry reconcile
    # keeps the function dicts pointing at the live functions (see
    # _FUNC_REGISTRY_NAMES in file_converters). The live SESSION POOL is NOT
    # here - it lives on `sys._lsd_fim_sessions` so it survives an entire-process
    # restart (the re-spawn / re-login); see fim._sessions.
    _fim_providers = {}
    _fim_profiles = {}
    _fim_context_sources = {}

    # Bumped on every REAL scroll_offset change (new_setattr in
    # invalidation_decoration); bumps the DrawState._ancestor_scroll memo so
    # mid-frame scroll deltas invalidate it without per-access parent walks.
    scroll_version = 0

    # list, full with 32 Nones
    max_depth = 32
    nested_layer_boost = 1
    top_layer_boost = 5
    max_layer = 64
    # Dedicated layer band for nested closable windows. Root windows live in
    # [0, nested_layer_base); nested windows (positive layer_offset) are lifted
    # into [nested_layer_base, nested_layer_max) by nested_window_layer(), so
    # they can never collide with — or get clamp-tied against — root window
    # layers. max_layer stays the ROOT-band size and keeps its existing
    # meaning for overlay channel counts and shadow-depth normalization;
    # nested_layer_max is the total layer budget (layers buckets, blit rank
    # clamp).
    # The root band must stay LARGER than len(registered_windows) + the front
    # boost: roots past nested_layer_base land inside the nested band, and
    # nested_window_layer's inactive-chain clamp (nested_layer_base - 1) then
    # puts a nested window AT or BELOW its parent (2026-08-19: ~66 registered
    # windows put parents at 63-65 → context menus / live windows painted
    # under their editor). 64 → 128 / 192 → 256; _LAYER_MAX (blit rank clamp)
    # and always_on_top_layer follow automatically.
    nested_layer_base = 128
    nested_layer_max = 256
    # Reserved layer for always_on_top root windows (e.g. GlobalSearch): above
    # the whole nested band, below the dragged-item layer bucket
    # (len(layers) - 1). A root window passing always_on_top=True is pinned
    # here by core_render's layer override and by pending_move_to_front.
    always_on_top_layer = nested_layer_max - 4
    drag_layer = 31
    layers = []
    active_layer = 0
    active_layer_stack = []
    # Paint order rank of the window being drawn: the LAYER term of the z
    # formula (z_pos = paint_rank * max_depth + depth), and so of every
    # input event, blit mask rank and shadow depth derived from it.
    # active_layer stays the layer BUCKET (+ in-bucket index) and keeps
    # feeding draw_state.layer, the front-root test and drag-drop
    # re-queueing. The rank is monotonic over the dispatch loop's paint order
    # (next_paint_rank: max(bucket offset, previous rank + 1)), so a window
    # painted later ALWAYS ranks higher. A bucket value alone can't promise
    # that parent siblings are over idx + d_idx and the bucket a grandchild
    # lands in (its abs_layer derives from the parent's paint_layer, not the
    # parent's in-bucket index), and the bucket clamp at nested_layer_max - 1
    # folds deep chains into one bucket - that way the parent's blocker
    # outranked its own child in the input handler and the child never got
    # an event (and its shadow read as inset). Equals active_layer whenever
    # no bucket spills.
    paint_rank = 0
    _last_paint_rank = -1
    layer_inc = 1

    bg_depth = 0
    seen_unique = set()

    last_draw_state = [(None, None)] * max_layer
    collection_index_stack = []
    hovered_ds = None

    windows = []
    collection_stack = []
    glfw_window = None
    # Frameless-window shadow margin (titlebar.window_inset): imgui's viewport
    # is the CONTENT, painted inset by frame_inset px into a framebuffer of
    # framebuffer_size - masks, tiles and filters point at the latter. Both
    # stamped per frame by SplitOverlayRenderer.process_inputs.
    frame_inset = 0
    # The content's top-left in the framebuffer: the margin plus the OS-edge
    # handoff's content shift (titlebar.content_origin); the viewport and
    # the screen→fb transform anchor here.
    frame_origin = (0, 0)
    framebuffer_size = None
    clip_stack = []
    clip_stack_holder = {}
    annotated_window_classes = {}
    registered_windows = defaultdict(lambda: ManagedWindow())
    # OS-window surfaces (surface.py / app.py). root_fill: the (w, h) a
    # top-level view fills on the active surface; stamped in the body
    # by Surface.frame and consumed by the render wrapper (width always,
    # height for the first root view of the frame). surface_requests:
    # ManagedWindow entries drawn with glfw_window=True whose OS window the
    # app loop has yet to create.
    root_fill = None
    root_fill_used = False
    surface_requests = []
    surface_windows = {}     # tile_id -> request (see surface_window_request)
    app_tick = 0             # app.py loop iteration; a request not refreshed this tick closes

    @classmethod
    def surface_window_request(cls, tile_id, name, input_value, kwargs, draw_state):
        """draw_x(glfw_window=True): record (or refresh) the request for a
        CHILD OS WINDOW of the active surface. The wrapper returns the
        deferred result; app.py creates the Surface, whose body is
        draw_surface_root, and closes it when a tick goes by without this
        call — immediate mode, like a closable window. window_pos= pins
        the parent-relative position (else the user moves it and the
        offset is adopted); window_size= sets the size. open_requested is
        a one-frame open/reopen trigger (when supplied, start closed until
        True); closed= explicitly controls visibility."""
        from types import SimpleNamespace
        req = cls.surface_windows.get(tile_id)
        if req is None:
            req = SimpleNamespace(tile_id=tile_id, name=name, surface=None, parent_surface=None,
                                  closed='open_requested' in kwargs, pinned=False, tick=-1)
            cls.surface_windows[tile_id] = req
            draw_state.closed = req.closed
        req.input_value, req.kwargs, req.draw_state = input_value, kwargs, draw_state
        req.tick = cls.app_tick
        # None/omitted retains a user close; an explicit bool controls the
        # window, just as it does for an in-surface closable view.
        if kwargs.get('closed') is not None:
            req.closed = bool(kwargs['closed'])
            draw_state.closed = req.closed
        if kwargs.get('open_requested'):
            req.closed = draw_state.closed = False
        if req.closed:
            if req in cls.surface_requests:
                cls.surface_requests.remove(req)
            if req.surface is not None:
                req.surface.closed = True
            return req
        req.pinned = 'window_pos' in kwargs
        # The parent-relative geometry lives on the request; the pare
        # draw_state rendering INLINE in the child surface, and the inline
        # path stamps window_pos itself.
        if req.pinned:
            req.window_pos = tuple(int(v) for v in kwargs['window_pos'])
        elif getattr(req, 'window_pos', None) is None:
            req.window_pos = (48, 48)
        if 'window_size' in kwargs:
            req.window_size = tuple(int(v) for v in kwargs['window_size'])
        elif getattr(req, 'window_size', None) is None:
            req.window_size = tuple(int(v) for v in (draw_state.window_size
                                                     or draw_state._initial_window_size or (600, 400)))
        if req.surface is None and not req.closed and req not in cls.surface_requests:
            from src.lsd.gl_gui.surface import Surface
            req.parent_surface = Surface.active
            cls.surface_requests.append(req)
        return req

    @classmethod
    def draw_surface_root(cls, req, surface):
        """The child surface's body: the requested view drawn as the
        surface's ROOT melty window (surface.root_view_kwargs — pinned to
        the OS window, its melty header in the chrome row when the call
        passed with_header=), on the same draw_state as the parent-side
        call, its result threaded back through pending_return_values."""
        from src.lsd.gl_gui.surface import root_view_kwargs
        kwargs = {k: v for k, v in req.kwargs.items()
                  if k not in ('glfw_window', 'window_pos', 'window_size', 'closable',
                               'layer_unique', 'draw_state', 'return_extras', 'closed', 'open_requested')}
        kwargs = root_view_kwargs(req.name, **kwargs)
        kwargs['draw_state'] = req.draw_state
        req.draw_state._wrapper(req.input_value, **kwargs)

    @classmethod
    def finish_surface_root(cls, req, surface):
        """Forward the result AFTER end_frame draws the deferred root layer.

        The child root has its own tile id. In particular a view can close
        itself while returning a value: collect that value before teardown.
        """
        result = cls.pending_return_values.pop(req.draw_state._tile_id, None)
        if result is not None:
            cls.pending_return_values[req.tile_id] = tuple(result)[:2]
            if result[0]:
                from src.lsd.gl_gui.utils.glfw_utils import request_render
                request_render()
        if req.draw_state.closed:
            req.closed = surface.closed = True
    # Self-registering RenderHost objects (id -> host). draw_main renders each one
    # in its own thread every frame; see view/core_conversion/render_host.py.
    render_hosts = {}
    render_hosts_tick = -1   # frame_tick Surface.frame last ran the hosts in (melty apps)
    scroll_stack = []
    tile_id_stack = []
    wrap_stack = []
    previous_select = None
    nested_window_refresh = None

    content_height_stack = []

    cursor = (0, 0)

    last_request_render = ""

    actions_to_apply = []

    init_window_cursor = (0, 0)

    last_invalid_attr = ""
    last_invalid = deque(maxlen=10)

    channels_split = False
    # Frame stamp of the most recent press on a resize affordance (column
    # edge handles, window frame edges and corner) - set BEFORE any edge
    # motion has been applied. draw_resize views read it in
    # mark_start_offscreen to snap a clean pre-drag capture while their
    # state is still unchanged (full tile coverage - nothing stale survives).
    resize_press_frame = -1
    # Frame stamp of the last OS-window size change seen by the GUI
    # (glbar.on_surface_resized, any backend): a compositor-driven
    # resize when the window's; edge zones hand the drag to the
    # compositor, the grab swallows the button, so no button is down
    # while the configures stream in. resize_gesture_live() counts the
    # OS_RESIZE_SETTLE_S after the last one as part of the gesture so
    # freeze_resize views stay frozen through it and settle once the
    # configures stop (in time, not frames: a drag's configures come at
    # the compositor's pace, a settle frame is cheap, a mid-drag settle is
    # a full re-render).
    os_resize_time = -1000.0
    OS_RESIZE_SETTLE_S = 0.15
    is_melty_window = False
    melty_window_stack = []
    default_font = None
    indent_size = 10
    annotation_mode = True
    # Per-THREAD annotation mode for recompile exec: re-running a class def
    # re-evaluates field annotations that CALL render funcs (`@int:
    # draw_any(...)`) - without interception they render for real on the
    # recompile's background thread (no GL context, FBO failure, imgui
    # ID-stack corruption on the render thread). Flipping the GLOBAL flag
    # would break the render thread mid-frame, so recompiles wrap their exec
    # in annotation_scope, which only the recompiling thread observes.
    _annotation_tls = _threading.local()
    depth = 0
    shadow_depth = 0
    wrapped_depth =0
    current_indent = 0
    indent_count = 0
    unindent_count = 0
    pending_move_to_front = None
    pending_delete_window = None
    imgui_popup_open = False
    imgui_active = False
    imgui_any_item_active = False
    imgui_active_pending = False
    imgui_main_window_hovered = False

    max_indent = 0
    hotkey_registry = {}
    move_draw_state_pending = {}

    # LibCST tracking -----------------------------------------
    _path_stack: list[tuple[str, int | None]] = []  # (field, idx)
    _root_by_module: dict[str, "cst.Module"] = {}   # libcst.Module; libcst is imported lazily (80 ms)
    _gen_by_module: dict[str, int] = {}

    last_attr = ""

    save_draw_state_for = 1
    spacing = (2, 1)
    padding = (2, 2)
    end_collection_spacing = 4
    collection_spacing = 2
    header_indent = 150

    vis = None
    imgui_crashed = False
    type_defaults = {}
    type_interrupts = {}
    default_view_functions = defaultdict(lambda: list())
    default_kwargs_by_type = defaultdict(lambda: dict())
    default_kwargs_by_attrib_type = defaultdict(lambda: defaultdict(lambda: dict()))

    default_funcs_by_type = defaultdict(lambda: None)
    default_funcs_by_name_type = defaultdict(lambda: defaultdict(lambda: list()))
    default_funcs_by_name = defaultdict(lambda: None)

    default_lenses_by_type = defaultdict(lambda: None)

    # Shape-refined routing (`shaped.Shaped` keys → wrapper). Consulted AFTER
    # the attribute-level registries and BEFORE the by-name / type ones: a
    # shape is a refinement of a type, so `Shaped("Tensor", (None, None))`
    # outranks the plain `"Tensor"` entry but a `tint: draw_x` annotation
    # still wins. Plain dicts (not defaultdicts) because the hotswap registry
    # reconcile in file_converters snapshots/restores them with the others.
    default_funcs_by_shape = {}
    default_lenses_by_shape = {}

    # Every @render_func wrapper, keyed by its own name (e.g. "draw_type").
    # Auto-populated by the decorator; the RenderFuncs accessor below resolves
    # against it lazily so modules can reference render_funcs by symbol without
    # importing the (often cycle-prone) module that defines them.
    render_funcs_by_name = {}

    silence_invalidate = False
    unique_stack = []
    suffix_stack = []
    size_stack = []
    window_stack = []
    window_hovered = False
    global_attrs = {}
    depth_state_stack = []
    flow_spacing = 0.0
    bg_stack = []
    bg_color_stack = []
    draw_state_stack = []
    font_style_stack = []
    font_base_stack = []
    backgrounds = []
    background_inline_indices = set()
    background_shadow_offsets = {}
    # One entry per cls.backgrounds entry: the rect the background covers,
    # the clip it was painted under and its corner radius, imgui coords -
    # the renderer copies them from the palette into the text-context
    # layer (SplitOverlayRenderer._ensure_style_context), so the text
    # shader reads the composed background instead of a framebuffer draw.
    background_rects = []
    background_gen = 0
    style_context_root = (0.0, 0.0, 0.0, 1.0)
    dynamic_style_gl = None

    input_value_stack = [None]
    window_enabled = True
    cache = None
    dirty_objects = set()
    all_dirty = False
    hovered_drawstate = set()
    hovered_drawstate_pending = set()
    frame_count = 0
    last_print_invalidate = 0

    # Emphasis flashes: key -> SimpleNamespace note, drawn in the overlay
    # pass (see the emphasize renderer next to the InvalidateTracker above).
    # A held (auto_fade=False) note that goes this many RENDERED frames
    # without its owner re-asserting it gets force-released into the fade -
    # the stuck-note guard for owners that stop rendering (window closed,
    # tab switched) without releasing. Idle frames don't count, so a held
    # note still sticks while nothing is happening.
    emphasis_hold_grace = 30
    emphasis_notes = {}
    # Effect-ledger hook (the Orchestrator binds EffectLedger.note here):
    # framework points publish observable, NOT-undoable effects through it -
    # an actual window raise (apply_move_to_front), a fired show_button
    # (headers.py) - the record/replay engine's third cue source beside the
    # edit stacks. None until the orchestrator module loads.
    effect_hook = None

    # File-text cache keyed by resolved path name. Populated by read_code,
    # invalidated by FileWatch on external change. Lets the symbol-usage index
    # avoid re-reading the same source on every index pass.
    code_cache = {}

    blocker_hovered = False

    all_uniques = set()
    profiles_results = {}
    live_attributes = {}

    event_handler = InputHandler()
    backend = ImGuiBackend(event_handler)
    events = {}
    # Raw (glfw_key, mods) for PRESS/REPEAT recorded by the GLFW callback backend
    # since the last end_frame, in order. The focused text editor drains these
    # instead of polling imgui.is_key_pressed, so keystrokes aren't lost on slow
    # frames. Cleared in end_frame after this frame's views have read them.
    frame_key_events = []

    # App-level keyboard shortcuts that fire wherever the focus is:
    # (glfw_key, modifier bits) → callback. Drained by begin_frame from the
    # press-edge queue above (never glfw.get_key level state - a tap may be
    # pressed AND released inside one UI frame). Register through
    # register_global_hotkey; re-registering a key replaces its callback, so
    # a hotswappable module re-binding at import is harmless. Survives a
    # melty.py hotswap (unchanged source expression keeps the live dict).
    global_hotkeys = {}

    # time.monotonic() of the last UI input - key (incl. held-key auto-repeat),
    # mouse button, mouse move/drag, or scroll (set in event_backends). Read by
    # the cst→dict index's cooperative loop yield to back off when the user interacts.
    _last_input_time = 0.0

    # time.monotonic() of the last pointer movement over the window (set in the
    # input backend's cursor callback). A hover deliberately does NOT
    # stamp _last_input_time (it would stall the cooperative parse yield);
    # this one is the "somebody is here" signal for gc_manager.tick, so a
    # return - the pointer crossing the window before any click - restarts
    # the idle clock instead of landing a collect in the user's face.
    _last_presence_time = 0.0
    # Pointer currently over the window (GLFW cursor_enter callback in the
    # input backend). gc_manager's unfocused tick requires it False:
    # unfocused + pointer inside = the user is in the studio.
    _pointer_inside = True

    # time.monotonic() of the last key event only (PRESS/REPEAT/RELEASE, set in
    # event_backends). Narrower than _last_input_time (which mouse activity also
    # stamps) - drives RenderHost.typing_hold's skip-hosts-while-typing debounce.
    _last_key_time = 0.0
    # GLFW keycodes currently being held (non-modifier; PRESS adds,
    # RELEASE removes). A HELD key doesn't reliably re-stamp _last_key_time
    # (Wayland repeat rate, the OS repeat delay), so typing_hold treats a
    # non-empty set as active typing regardless of the stamp, reconciling
    # polling glfw.get_key for missed releases.
    _keys_down = set()

    texture_manager = TextureManager()
    returned_values = {}
    pending_return_values = {}

    # Undo/redo: draw_state -> (value, ui) to restore. UndoManager.undo()/redo()
    # register an entry here on ctrl+z / ctrl+shift+z; core_render's render_func
    # tail intercepts the relevant draw_state's return, reports (True, value)
    # instead of the live value, restores UI (caret/selection/scroll), then pops
    # the entry so it fires for exactly one view.
    undo_requests = {}

    # Drag-and-drop reorders: collection draw_state -> op(collection) ->
    # (changed, collection). Registered by DragDrop._commit on drop; the same
    # wrapper tail that serves undo_requests pops the entry, applies the func to
    # the live collection and reports (True, reordered) so the parent writes
    # it back. See view/core_views/drag_drop.py.
    dnd_requests = {}

    # (x, y, w, h) of the dragged item's home slot while a drag is active,
    # else None. blit_offscreen checks this when blitting a cached tile that
    # contains the slot and repaints the blank socket live over the image -
    # the tile's pixels there can be incorrect (the dragged item overlapped
    # the slot when the tile was captured). Set/cleared by DragDrop.
    dnd_home_rect = None

    empty_event = InputEvent(input_id="", action="")
    events_by_type = {}
    pending_blockers = [None] * max_layer
    imgui_blockers = [None] * max_layer
    original_spacing = None
    original_window_padding = None
    original_frame_padding = None

    fixed_size_stack = []
    nested_collections = 0
    z_pos = 0
    any_window_hovered_pending = False
    any_window_hovered = False
    glfw_close_requested = False

    # BVH spatial index for draw states (rtree, built on first use: see _LazyRtree)
    _bvh = _LazyRtree()
    _bvh_next_id = 0         # process-wide: a rid is unique across every surface's index
    # rid → (index, id_to_ds) this box was inserted into. Surfaces (surface.py)
    # swap `_bvh` and `_bvh_id_to_ds` per OS window; a draw_state drawn in a
    # different index than last frame (a glfw_window=True child's root) finds
    # its old box through this and drops itself from the index it sits in.
    _bvh_home = {}
    _bvh_id_to_ds = {}
    # Bumped on every insert/delete that mutates the index. bvh_query memoizes
    # results by (x, y) and discards the memo whenever this changes. The index
    # mutates mid-frame (views call pos_changed as they render), so this keeps
    # the cached HIT SET exact - a memo only survives between two queries with
    # no intervening index change. The sort key (z_pos/closed) can shift without
    # an index mutation, so a reused result may carry a one-frame-stale ordering,
    # which is within Melty's existing frame-lag tolerance for hover/z-order.
    _bvh_gen = 0
    _bvh_query_cache = {}
    _bvh_query_cache_gen = -1

    # GL error-checking gate state (see _sync_gl_error_checking).
    _gl_check_applied = None       # Last-applied Toggles.gl_check_error value
    _gl_checker_default = None     # initial _registered value, captured once
    # id(ds) for every draw_state whose bbox is under the cursor this frame - a
    # begin_frame snapshot of bvh_query. hover_eligible / is_bounding_hovered do
    # O(1) membership against this instead of imgui.is_mouse_hovering_rect.
    bvh_hover_ids = set()

    # OS-window framebuffer size, stashed once per frame in begin_frame so
    # external code without imgui access (DrawState's nested-window position
    # cap) can read it. None until the first frame / in headless mode.
    display_size = None

    # UI scale in effect (Toggles.UIScale, resolved by apply_ui_scale between
    # frames). Scale reaches the screen two ways and ONLY these two: fonts are
    # re-baked at scale x their original size, and the imgui style metrics in
    # begin_frame are multiplied by scale. Coordinates stay 1:1 with window
    # pixels - this is deliberately NOT a whole-interface zoom.
    ui_scale = 1.0
    _auto_ui_scale = None       # cached detect_ui_scale probe (auto mode)

    items_to_delete = []
    # Foreground/overlay channel routing. The overlay draw list is channel-split
    # into max_depth channels (like the window draw list); a view adds its
    # overlay to channel = layer_channel(draw_state.layer). The top channel is
    # the unmasked global default. The renderer uses _overlay_channel_ranges to
    # stencil-mask out higher-layer windows per channel during its deferred pass.
    _overlay_channels_active = False
    _overlay_channel_ranges: list = []
    # Per-frame dense rank map: raw window z index (window_index) -> overlay
    # channel. Rebuilt in end_frame; read by overlay_window_channel().
    _overlay_channel_map: dict = {}
    # id(window ds) -> overlay channel, assigned in MINT order (top, roots
    # before nested, sibling d_idx). Unlike _overlay_channel_map (keyed by raw
    # window index), two windows can't share a channel here, so the mask
    # pass's strict "higher channel masks lower" comparison stays exact even
    # when an off-chain nested window's index crosses a neighboring root's.
    # Rebuilt each frame next to the raw map; read via overlay_channel_for().
    _overlay_channel_by_ds: dict = {}
    _overlay_probe_logged = False
    _debug_overlay_test = True  # controlled sub-top overlay to verify masking

    @classmethod
    def read_code(cls, path):
        """File text via code_cache, invalidated by FileWatch on change. Returns
        None on read error. Use for repeated reads of the same source (e.g. the
        symbol-usage index) so an unchanged file isn't re-read every pass."""
        # Path.resolve() is a realpath call (syscalls per component); the
        # roster's _file_changed calls this every frame, so the result is
        # memoized on the path string (bounded, never invalidated - a
        # symlink retarget mid-session is not a case this handles).
        path_str = str(path)
        key = _RESOLVED_PATH_MEMO.get(path_str)
        if key is None:
            key = str(Path(path).resolve())
            if len(_RESOLVED_PATH_MEMO) > 4096:
                _RESOLVED_PATH_MEMO.clear()
            _RESOLVED_PATH_MEMO[path_str] = key
        text = cls.code_cache.get(key)
        if text is None:                       # absent (not an empty file)
            try:
                text = Path(key).read_text()
            except (OSError, UnicodeDecodeError):
                # UnicodeDecodeError: binary file (an image) - this is a
                # TEXT cache; binary reads go to the git/file_system
                # proxies, which return bytes.
                return None
            cls.code_cache[key] = text
        return text

    @classmethod
    def get_default_view_function(cls, draw_state=None, real_type=None, collection_type=None, attrib_key=None,
                                  value=None):
        # `value` feeds the shape-refined tier (Shaped keys): a shape is a
        # property of the VALUE, not the type, so the by-type lookups below
        # can't see it. None is still a legitimate value - it simply has no
        # shape and skips that tier.
        if draw_state is not None:
            value = draw_state._input_value
            real_type = draw_state._kwargs.get("real_type", type(value))
            collection_type = draw_state._kwargs.get("type_collection", type(draw_state._collection))
            attrib_key = draw_state._kwargs.get("key", draw_state._kwargs.get("name", None))

        # Names often carry an imgui id suffix ("tint##caller_3"); the default
        # registries are keyed by the bare attribute name, so match on the
        # part before the ## tag.
        if isinstance(attrib_key, str) and "##" in attrib_key:
            attrib_key = attrib_key.split("##", 1)[0]

        default_view_function = None

        # Most specific: a per-(collection_type, attribute) override registered
        # by viewdefaults or a field annotation (e.g. `test_tint: draw_tuple`).
        by_name_type = cls.default_funcs_by_name_type.get(collection_type)
        if by_name_type is not None:
            candidate = by_name_type.get(attrib_key)
            if callable(candidate):
                return candidate

        default_by_name = cls.default_funcs_by_name[attrib_key]
        if default_by_name is not None:
            return default_by_name

        # Shape tier: `Shaped(of, shape, dtype)` entries registered through
        # is_default_for. Sits between the name and type tiers (see the
        # registry comment). best_match extracts the value's shape at most
        # once and only when an entry's `of` matched the type.
        if value is not None and cls.default_funcs_by_shape:
            default_by_shape = best_match(cls.default_funcs_by_shape.items(), value, real_type)
            if default_by_shape is not None:
                return default_by_shape

        default_by_type = cls.default_funcs_by_type[real_type]
        default_by_type_str = cls.default_funcs_by_name[real_type.__name__]

        # loop over super types
        super_types = real_type.__mro__[1:]
        for t in super_types:
            if default_by_type is not None:
                break
            default_by_type = cls.default_funcs_by_type[t]

        if default_by_type_str is not None:
            default_view_function = default_by_type_str

        elif default_by_type is not None:
            default_view_function = default_by_type

        return default_view_function

    @classmethod
    def get_default_lens_function(cls, driven_value):
        """The `is_lens_for` lens for a driven value. Same order as views:
        the shape-refined `Shaped` entries outrank the exact-type registry."""
        lens_func = None
        if driven_value is not None and cls.default_lenses_by_shape:
            lens_func = best_match(cls.default_lenses_by_shape.items(), driven_value)
        if lens_func is None:
            lens_func = cls.default_lenses_by_type.get(type(driven_value))
        return lens_func

    # Single source of truth for a draw_state's presence in the rtree.
    #
    # Invariant: each draw_state owns AT MOST ONE BOX in the index, under a rid
    # assigned once for its lifetime (never reused, never dropped). `_bvh_bbox`
    # always mirrors EXACTLY the box currently stored under that rid, or None
    # when the draw_state has no box in the index. bvh_sync is the only writer,
    # so the mirror can't drift: it deletes the current box (matched by the rid,
    # which is why rtree.delete - which needs an exact inserted box - always
    # hits) before adding the new one, and records the new box.
    #
    # This replaces the old register/update/unregister trio, whose fresh-rid-
    # per-register and "set _bvh_bbox without inserting" paths let a single
    # draw_state accumulate several boxes (rtree allows duplicate ids), which
    # appeared as duplicated hits in bvh_query.
    @classmethod
    def bvh_sync(cls, draw_state):
        """Make the index hold exactly the draw_state's current box, or nothing.

        Idempotent and cheap: when the desired box equals what's already stored
        it returns without touching the index, so calling this every render only
        does work when abs_left/abs_top/width/height (i.e. `bbox`) — or the
        view's visibility — actually changed.

        The box is dropped (desired = None) when the view is off-screen
        (`inside_clip` False), closed (`closed`), or its rect is degenerate. So
        a closed or scrolled-out view clears itself the next time it syncs.

        Only `closed` (a cheap bool) is checked here, not `abs_closed`: a view
        hidden by a COLLAPSED ANCESTOR isn't rendered, so pos_changed never
        fires for it and this couldn't clear it regardless. bvh_query handles
        that case by filtering and lazily evicting abs_closed hits. Keeping the
        per-render path off the abs_closed parent-chain walk matters — this runs
        for every view every frame."""
        desired = draw_state.bbox
        if (desired is not None
                and draw_state.inside_clip
                and not draw_state.closed
                and desired[0] < desired[2] and desired[1] < desired[3]):
            pass  # keep desired
        else:
            desired = None

        current = draw_state._bvh_bbox
        rid = draw_state._bvh_id
        if current is not None:
            home = cls._bvh_home.get(rid)
            if home is not None and home[0] is not cls._bvh:
                # The view lives in another OS window's index: evict it there
                # and (re)sync here as if it were new.
                try:
                    home[0].delete(rid, current)
                except Exception:
                    pass
                home[1].pop(rid, None)
                current = None
        if desired == current:
            return

        if rid is None:
            rid = cls._bvh_next_id
            cls._bvh_next_id += 1
            draw_state._bvh_id = rid

        if current is not None:
            cls._bvh.delete(rid, current)
        if desired is not None:
            cls._bvh.insert(rid, desired)
            cls._bvh_id_to_ds[rid] = draw_state
            cls._bvh_home[rid] = (cls._bvh, cls._bvh_id_to_ds)
        else:
            cls._bvh_id_to_ds.pop(rid, None)
            cls._bvh_home.pop(rid, None)
        draw_state._bvh_bbox = desired
        cls._bvh_gen += 1

    @classmethod
    def bvh_evict(cls, draw_state):
        """Drop a draw_state's box from the index immediately, keeping its rid.

        Lazy GC for views that vanish WITHOUT re-rendering (a collapsed parent
        stops descending its children, so those children never sync themselves
        out). bvh_query calls this when it encounters such a hit. The rid is
        retained, so the view re-syncs cleanly if it ever reappears.

        Deliberately does NOT bump _bvh_gen: the caller only evicts hits it has
        already filtered out (closed/abs_closed), so removing them changes no
        query's result — and leaving gen alone keeps the query memo this frame
        valid instead of forcing a re-scan on the next identical query."""
        if draw_state._bvh_bbox is not None:
            cls._bvh.delete(draw_state._bvh_id, draw_state._bvh_bbox)
            draw_state._bvh_bbox = None
        cls._bvh_id_to_ds.pop(draw_state._bvh_id, None)

    @classmethod
    def bvh_evict_window(cls, window_ds):
        """Drop the boxes of a window AND of every view indexed under it (any
        draw_state whose parent_window chain reaches it) — for a window that
        stops rendering WITHOUT closing.

        The two lazy paths can't reach such a window: bvh_sync only runs when
        a view renders, and bvh_query only evicts hits that are closed /
        abs_closed. A nested window that is merely DISCARDED — dropped from
        root_draw_states by apply_refresh_nested_windows on an editor tab
        switch or file close, or released with a deleted root — is neither,
        so its boxes stayed in the index for the whole session, front-most by
        z_pos: an invisible window that won every hit test under it
        (Melty.hovered_ds / bvh_hover_ids), so the views actually drawn there
        never passed hover_eligible and never reached the input handler. The
        2026-08-24 Code Editor "top-left is dead" was a discarded 1421x1322
        live-value window (causal_mask) whose editor tab had been switched
        away. Rids are kept: a window that comes back re-syncs on its next
        render. Bumps _bvh_gen — unlike bvh_evict's lazy path these hits were
        never filtered out of a query, so this frame's memo must drop."""
        if window_ds is None:
            return
        evicted = 0
        for ds in list(cls._bvh_id_to_ds.values()):
            node = ds
            for _ in range(32):
                if node is None:
                    break
                if node is window_ds:
                    cls.bvh_evict(ds)
                    evicted += 1
                    break
                nxt = node.parent_window
                if nxt is node:
                    break
                node = nxt
        if evicted:
            cls._bvh_gen += 1
        return evicted

    @classmethod
    def _resolve_channel_command_ranges(cls, overlay, idx_boundaries):
        """Partition the merged index buffer into per-channel index ranges.

        Returns a list of (channel_idx, idx_lo, idx_hi) for each non-empty
        channel, where [idx_lo, idx_hi) are positions in the merged index
        buffer. We work in index space — not command space — because
        ChannelsMerge fuses adjacent channels' draws into a single command when
        their clip rect + texture match. A whole-command assignment would then
        lump every channel's indices onto one channel; index ranges stay
        correct regardless of fusion, and the renderer splits commands at these
        boundaries."""
        ranges = []
        prev = 0
        for ch, boundary in enumerate(idx_boundaries):
            if boundary > prev:
                ranges.append((ch, prev, boundary))
            prev = boundary
        return ranges

    @classmethod
    def layer_channel(cls, layer) -> int:
        """Map a draw_state layer to its overlay draw-list channel, using the
        same clamp as the window draw list. The top channel (max_depth - 1) is
        the unmasked global channel; the renderer masks channel C with every
        window whose layer_channel is greater than C."""
        return max(0, min(int(layer), cls.max_depth - 1))

    @classmethod
    def _chain_root(cls, ds):
        """Walk parent_window up to the top-level window that owns this nested
        chain. Bounded, and guarded against a self-referencing node the same
        way apply_move_to_front's subtree walk is."""
        node, n = ds, 0
        while n < 64:
            nxt = node.parent_window
            if nxt is None or nxt is node:
                return node
            node = nxt
            n += 1
        return node

    @classmethod
    def nested_window_layer(cls, parent_layer, layer_offset, ds=None) -> int:
        """Layer for a nested closable window, given its parent window's layer.

        Only chains rooted at the FRONT top-level window get the dedicated
        nested band [nested_layer_base, nested_layer_max): band start + parent
        layer + offset, with a nested-of-nested chain (whose parent is already
        in the band) climbing within it. The front root is the boosted one —
        its layer is len(registered_windows) + top_layer_boost, while inactive
        roots keep their registry-index layer — so `root.layer >=
        len(registered_windows)` is the test.

        Chains rooted at an INACTIVE window stay parent-relative in the root
        band with the climb compressed to +1 per level (offset 0 stays 0), and
        are capped below the band, so they ride just above their parent but
        can never cross the focused front window.

        Negative offsets (window drawn BEHIND its parent, e.g. the voxel
        controls panel) always stay parent-relative — lifting them into the
        band would put them in front of every root window, inverting their
        meaning. The 0-clamp mirrors abs_layer's: the dispatch loop never
        visits negative buckets."""
        if layer_offset < 0:
            return max(0, parent_layer + layer_offset)
        if ds is not None:
            root_layer = cls._chain_root(ds).layer
            if root_layer is not None and root_layer < len(cls.registered_windows):
                return min(cls.nested_layer_base - 1,
                           parent_layer + min(layer_offset, 1))
        if parent_layer >= cls.nested_layer_base:
            return min(cls.nested_layer_max - 1, parent_layer + layer_offset)
        return min(cls.nested_layer_max - 1,
                   cls.nested_layer_base + parent_layer + layer_offset)

    @classmethod
    def next_paint_rank(cls, bucket_value):
        """Rank for the next window the dispatch loop paints (see
        paint_rank): never below its bucket value, so ranks equal
        active_layer while no bucket spills, and always above the window
        painted before it. Reset (_last_paint_rank = -1) at the top of the
        dispatch loop."""
        rank = max(bucket_value, cls._last_paint_rank + 1)
        cls._last_paint_rank = rank
        return rank

    @classmethod
    def emphasize(cls, key, rect, tint=hdr_color.p3(1.0, 0.82, 0.22), auto_fade=True,
                  rounding=6.0, fade_frames=18, thickness=2.0, clip=None):
        """Register a rounded-rect emphasis flash, drawn by the overlay pass
        (next to the InvalidateTracker loop) with the same frame-based fade:
        alpha = 1 - frames_past / fade_frames.

        auto_fade=True: fire-and-forget — call once and the fade plays out on
        its own (the note keeps requesting frames until it expires). Repeat
        calls while it is fading do NOT re-stamp it, so a per-frame caller can
        keep passing a fresh rect without freezing the animation.

        auto_fade=False: the note holds at full alpha — no frames of the fade
        play until the caller says so — so it sticks around (e.g. until the
        mouse moves a little). Call again with auto_fade=True to release it
        and let the fade play out from that moment. Holding notes draw from
        whatever frames render anyway (no render requests), so the release
        check naturally runs when input wakes the caller's view back up.
        A hold is a lease, not a latch: keep re-asserting it (call again with
        auto_fade=False) while the owner's body runs, and the overlay pass
        force-releases any hold that goes emphasis_hold_grace rendered frames
        untouched — so a note can never stick around after its owner stops
        rendering.

        tint is a HUE: an extended-sRGB tuple (the default is a Display-P3
        amber). The overlay pass lifts it into HDR at draw time — the
        outline at 2^Toggles.HDR.emphasis_stops × reference white, the fill
        at 2^Toggles.HDR.emphasis_fill_stops, plus a faint bloom halo — so
        callers never bake brightness into the tint and the knobs are live.

        rect is (x0, y0, x1, y1) in absolute screen coords, or a zero-arg
        callable returning one (or None to skip a frame) so the flash can
        track a scrolling target. A callable that raises (e.g. its captured
        draw_state died) drops the note.

        clip: optional (x0, y0, x1, y1) screen rect -- or a zero-arg callable
        returning one -- the flash is scissored to (e.g. the editor view the
        flashed lines scroll inside), so a rect that runs past its owner's
        edges doesn't paint over neighbours."""
        note = cls.emphasis_notes.get(key)
        if note is None or not auto_fade or not note.auto_fade:
            cls.emphasis_notes[key] = types.SimpleNamespace(
                rect=rect, tint=tint, frame=cls.frame_count,
                auto_fade=auto_fade, rounding=rounding,
                fade_frames=fade_frames, thickness=thickness,
                last_touch=cls.frame_count, clip=clip)
        else:
            # Already fading automatically: refresh geometry/looks only.
            note.rect, note.tint = rect, tint
            note.rounding, note.thickness = rounding, thickness
            note.last_touch = cls.frame_count
            note.clip = clip

    @classmethod
    def emphasize_click(cls, key, center, tint=hdr_color.p3(1.0, 0.82, 0.22), radius=None,
                        fade_frames=24, thickness=2.5):
        """Circular click emphasis: an expanding, fading ring pair centered
        on `center` (absolute screen coords). Fire-and-forget — one call per
        click, unique keys let overlapping ripples coexist. Rides the
        emphasis_notes pipeline (kind="ripple"), so fade pacing and cleanup
        are the same as the rect flashes. The Orchestrator stamps one per
        INJECTED press so replayed clicks are visible."""
        cls.emphasis_notes[key] = types.SimpleNamespace(
            rect=None, kind="ripple", center=center, tint=tint,
            radius=radius if radius is not None else cls.px(24.0),
            frame=cls.frame_count, auto_fade=True, rounding=0.0,
            fade_frames=fade_frames, thickness=thickness,
            last_touch=cls.frame_count, clip=None)
        request_render()

    @classmethod
    def emphasize_cursor(cls, key, center, tint=(0.97, 0.97, 0.97), hold=True):
        """Virtual pointer arrow at `center` (a (x, y) or a zero-arg callable
        — the Orchestrator passes a callable reading its virtual cursor).
        hold=True is the emphasize lease: re-assert every frame the driver
        runs; the overlay pass force-releases a lease that goes
        emphasis_hold_grace frames untouched, and an explicit hold=False
        call releases it into a short fade (replay finished)."""
        note = cls.emphasis_notes.get(key)
        if note is not None and getattr(note, "kind", None) == "cursor":
            note.center, note.tint = center, tint
            note.last_touch = cls.frame_count
            if not hold and not note.auto_fade:
                note.auto_fade = True
                note.frame = cls.frame_count
            return
        cls.emphasis_notes[key] = types.SimpleNamespace(
            rect=None, kind="cursor", center=center, tint=tint,
            frame=cls.frame_count, auto_fade=not hold, rounding=0.0,
            fade_frames=14, thickness=1.5, last_touch=cls.frame_count,
            clip=None)
        request_render()

    @classmethod
    def _emphasis_colors(cls, tint):
        """(line, fill) extended-sRGB tuples for an emphasis note's hue
        tint: its linear light lifted by Toggles.HDR.emphasis_stops /
        emphasis_fill_stops (read live, so editing the toggle retunes every
        flash on screen). The fill stays near SDR so the flashed content
        under it is still readable; the outline and rings are what glow."""
        hue = tuple(tint[:3])
        line = hdr_color.scale(hue, 2.0 ** float(Toggles.HDR.emphasis_stops))
        fill = hdr_color.scale(hue, 2.0 ** float(Toggles.HDR.emphasis_fill_stops))
        return line, fill

    @classmethod
    def _draw_emphasis_shape(cls, overlay, kind, note, center, alpha):
        """The non-rect emphasis kinds (overlay pass). ripple: two expanding
        HDR rings plus a faint wide bloom ring; cursor: a classic pointer
        arrow with a soft drop shadow (no polyline outline — fills only, so
        it needs no draw-list flags)."""
        x, y = center
        r, g, b = note.tint[:3]
        if kind == "ripple":
            (lr, lg, lb), _fill = cls._emphasis_colors(note.tint)
            progress = min(1.0, (cls.frame_count - note.frame)
                           / max(1, note.fade_frames))
            ring = note.radius * (0.30 + 0.70 * progress)
            # bloom: a wide, faint HDR ring under the crisp one
            overlay.add_circle(x, y, ring,
                               pack_color(lr, lg, lb, 0.22 * alpha),
                               32, note.thickness * 3.0)
            overlay.add_circle(x, y, ring,
                               pack_color(lr, lg, lb, 0.85 * alpha),
                               32, note.thickness)
            overlay.add_circle(x, y, ring * 0.55,
                               pack_color(lr, lg, lb, 0.40 * alpha),
                               32, max(1.0, note.thickness * 0.6))
            return
        if kind == "cursor":
            scale = cls.px(1.15)
            # classic pointer polygon, split into convex pieces (the tail
            # notch makes the whole concave): two head triangles + tail quad
            def _pieces(ox, oy, color):
                overlay.add_triangle_filled(x + ox, y + oy,
                                            x + ox, y + oy + 16.5 * scale,
                                            x + ox + 4.4 * scale, y + oy + 12.8 * scale,
                                            color)
                overlay.add_triangle_filled(x + ox, y + oy,
                                            x + ox + 4.4 * scale, y + oy + 12.8 * scale,
                                            x + ox + 12.1 * scale, y + oy + 11.9 * scale,
                                            color)
                overlay.add_quad_filled(x + ox + 4.4 * scale, y + oy + 12.8 * scale,
                                        x + ox + 6.6 * scale, y + oy + 11.9 * scale,
                                        x + ox + 9.4 * scale, y + oy + 17.9 * scale,
                                        x + ox + 7.2 * scale, y + oy + 18.9 * scale,
                                        color)
            shadow = 1.4 * scale
            _pieces(shadow, shadow, pack_color(0.0, 0.0, 0.0, 0.45 * alpha))
            _pieces(0.0, 0.0, pack_color(r, g, b, 0.95 * alpha))

    @classmethod
    def overlay_channel_for(cls, ds) -> int:
        """Overlay channel for a draw_state, from the paint-order per-window
        map. A non-window view resolves to its nearest enclosing window's
        channel (bounded parent_window walk, self-loop guarded). Falls back to
        the raw-index rank map for windows registered after this frame's map
        was built — the pre-existing approximation for that case."""
        node, n = ds, 0
        while node is not None and n < 64:
            ch = cls._overlay_channel_by_ds.get(id(node))
            if ch is not None:
                return ch
            nxt = node.parent_window
            if nxt is node:
                break
            node = nxt
            n += 1
        return cls.overlay_window_channel(ds.window_index)

    @classmethod
    def overlay_window_channel(cls, raw_index) -> int:
        """Overlay channel for a window's raw z index (window_index),
        compressed through this frame's dense rank map so z ordering survives
        the max_layer channel budget. Raw indices grow fast with nesting
        (layer_offset stacks +4 per level), and routing them straight into
        channels overflowed the min(index, max_layer - 1) clamp: everything
        past the clamp collapsed onto the top channel, which the stencil pass
        never masks — so a deep-nested window's outline/swoosh drew above the
        very top window, and windows sharing the clamped channel couldn't mask
        each other. Ranks keep every distinct raw index on its own channel,
        strictly below any window above it, capped at max_layer - 2 so the
        global top channel stays reserved for unmasked overlays."""
        if raw_index is None:
            return cls.max_layer - 2
        ch = cls._overlay_channel_map.get(raw_index)
        if ch is None:
            # Index not present when the frame map was built (e.g. a window
            # registered mid-frame): rank it against the known indices. It may
            # tie for a neighbor's channel, which just means no masking
            # between those two - the z-map behavior for equal indices.
            ch = sum(1 for k in cls._overlay_channel_map if k < raw_index)
        return min(ch, cls.max_layer - 2)

    @classmethod
    def bvh_query(cls, x, y):
        """Hit test — returns all DrawStates under the point.

        Memoized per (x, y) and invalidated whenever the index mutates (via
        _bvh_gen), so the many identical cursor queries issued across a single
        frame — hover-suppression fires one per bounding-hovered view — collapse
        onto a single rtree.intersection call. Callers must treat the returned
        list as read-only (it is shared across cache hits); all current callers
        only iterate or index it."""
        if cls._bvh_query_cache_gen != cls._bvh_gen:
            cls._bvh_query_cache = {}
            cls._bvh_query_cache_gen = cls._bvh_gen
        key = (x, y)
        if key in cls._bvh_query_cache:
            return cls._bvh_query_cache[key]

        # bvh_sync's one-box-per-rid invariant means intersection won't return a
        # rid twice - but dedup by rid anyways as a cheap, exact safety net (a
        # plain set, not the old O(n^2) `ds in hits` scan).
        hits = []
        stale = []
        seen_rids = set()
        for rid in cls._bvh.intersection((x, y, x, y)):
            if rid in seen_rids:
                continue
            seen_rids.add(rid)
            ds = cls._bvh_id_to_ds.get(rid)
            if ds is None:
                continue
            # A view that closed/collapsed without re-rendering still has a stale
            # box here. Filter it out, and lazily evict so it stops being hit.
            if ds.closed or ds.abs_closed:
                stale.append(ds)
                continue
            # A nested window hidden because its spawner scrolled out of sight
            # (end frame's dispatch rebuild) keeps its geometry and BVH boxes -
            # they must NOT be evicted, or its blit-cached widgets would stay
            # hover-dead when it unhides (they only re-sync when they actually
            # re-render). Just skip hits inside any hidden window up the
            # parent_window chain while the flag is set.
            node, hidden = ds, False
            for _ in range(32):
                if node is None:
                    break
                if getattr(node, '_hidden_offscreen', False):
                    hidden = True
                    break
                nxt = node.parent_window
                if nxt is node:
                    break
                node = nxt
            if hidden:
                continue
            # The index stores full, UNCLIPPED bboxes, so a view scrolled partly
            # out of its parent still matches over its hidden region. Reject the
            # hit when the point lies outside the view's visible (clipped) region.
            cl, ct, cr, cb = ds.abs_clip_rect
            if not (cl <= x <= cr and ct <= y <= cb):
                continue
            hits.append(ds)

        for ds in stale:
            cls.bvh_evict(ds)

        # Sort front-to-back by the stored z_pos. (Do NOT switch the sort key to
        # the live abs_layer. abs_layer adds a per-nested-window layer_offset that
        # z_pos doesn't, so it reorders raised-window hits and breaks the imgui
        # press-handoff priming below - it primes hits[0], which must stay the
        # topmost view by the same z_pos order the renderer used. The just-raised-
        # window staleness is fixed at the source by apply_move_to_front, which
        # refreshes the moved view's z_pos so this sort stays correct.)
        hits.sort(key=lambda ds: ds.z_pos or 0, reverse=True)

        cls._bvh_query_cache[key] = hits
        return hits

    @classmethod
    def ancestor_closure(cls, seeds):
        """The ids of every draw_state reachable UP from `seeds` — each seed
        itself, then its `_parent` and `parent_window` chains — plus the
        context-menu hop below. This is the "inside" test the focus slots
        use: a click keeps an owner focused when the owner is in the closure
        of the hit stack (clear_focus), and a popover window is alive while
        the popover slot names something in ITS closure (popover_orphaned)."""
        closure = set()
        for seed in seeds:
            stack = [seed]
            guard = 0
            while stack and guard < 256:
                guard += 1
                node = stack.pop()
                if node is None or getattr(node, "id", None) in closure:
                    continue
                closure.add(node.id)
                parent = getattr(node, "_parent", None)
                pwin = getattr(node, "parent_window", None)
                if parent is not None and parent is not node:
                    stack.append(parent)
                if pwin is not None and pwin is not node:
                    stack.append(pwin)
                # A context-menu window's _parent/parent_window chain does NOT
                # run through the view the menu was opened ON, so a click inside
                # the menu used to clear that view's focus - including an open
                # popover (and the menu with it, since context_menu_ds renders
                # from the popover's subtree). Hop from the menu window to its
                # target: draw_context_menu receives the target draw_state as
                # its input_value, and the target's context_menu_ds points back
                # to the menu window, so the pair is identified by that mutual
                # link alone - no new state, and other draw_states whose input
                # happens to be a draw_state don't match.
                target = getattr(node, "_raw_input_value", None)
                if target is not None and getattr(target, "context_menu_ds", None) is node:
                    stack.append(target)
        return closure

    @classmethod
    def popover_orphaned(cls, ds):
        """True for a nested POPOVER window (Mode.POPOVER stamps `popover`
        in its kwargs) that nothing holds open any more: the popover slot is
        empty or names a view outside the window's ancestor closure (another
        popover took the slot, a click away cleared it). A popover used to be
        hidden only on a frame its SPAWNER drew it closed=True — so when the
        spawner stopped running (its tab switched away, its tile served from
        the blit cache after the slot was cleared) the window stayed in
        root_draw_states, dispatched from the layer loop every frame: visible,
        front-most, and no click could dismiss it. end_frame discards an
        orphaned popover directly, spawner or no spawner. The slot is set in
        the body before the window is drawn (or handed to the window right
        after, in the same frame), so a popover is never orphaned on the
        frame it opens."""
        kwargs = getattr(ds, "_kwargs", None)
        if type(kwargs) is not dict or not kwargs.get("popover", False):
            return False
        owner = cls.popover_focused_ds
        if owner is None:
            return True
        return owner.id not in cls.ancestor_closure([ds])

    @classmethod
    def clear_focus(cls, not_this=None):
        # Clear all focus slots (text / popover / general) EXCEPT any owner that is
        # an ancestor of `not_this`. `not_this` is the view(s) just interacted with
        # (e.g. the bvh hit-stack under the cursor on mouse click). We protect each
        # seed's full ANCESTOR CLOSURE - walking both _parent and parent_window -
        # so clicking anywhere inside a focus owner's subtree keeps it focused even
        # when the owner is several levels up (a dropdown's search box lives in its
        # menu window, whose parent_window is the dropdown trigger that holds the
        # popover slot). Protecting only the immediate parents dropped that owner,
        # which closed the dropdown / killed its search + arrow input on any click.
        if isinstance(not_this, (list, tuple)):
            seeds = [n for n in not_this if n is not None]
        elif not_this is not None:
            seeds = [not_this]
        else:
            seeds = []

        protect = cls.ancestor_closure(seeds)

        # A just-opened popover gets a one-frame grace: the very click that opens
        # it also fires clear_focus, and the opener (e.g. a tiny colour swatch) may
        # not be the bvh hit, so it wouldn't be in `protect`. Without the grace the
        # popover would close on the same click that opened it. We do NOT protect a
        # popover owner's _parent (its containing window) - that kept popovers open when
        # clicking the parent window, defeating click-outside-to-dismiss.
        popover_grace = (cls.frame_count - getattr(cls, "_popover_open_frame", -99)) <= 1

        # Same-frame text-focus grace: a grant stamped THIS frame outlives the
        # click being processed this frame (the click physically happened before
        # the grant, e.g. it's the very click that opened the search box now
        # claiming focus). Clears triggered by later clicks run in later frames
        # and proceed normally.
        text_grace = cls.frame_count == getattr(cls, "_text_focus_grant_frame", -99)

        for ds in (cls.focused_ds, cls.text_focused_ds, cls.popover_focused_ds):

            if ds is None or ds.id in protect:
                continue
            if ds is cls.popover_focused_ds and popover_grace:
                continue
            if ds is cls.text_focused_ds and text_grace:
                continue
            if Toggles.TextEditor.text_focus_stack_trace:
                print_stack_trace(size=5)

            # Search stays open on focus loss - only Esc or the close (X) button
            # closes a find bar, and multiple bars may be open at once. clear_focus
            # only clears TEXT focus here (below); it no longer touches search_active
            # / _search_was_active, which used to proactively close the find bar
            # when focus moved away. Leaving _search_was_active alone also keeps
            # the box's existing re-grab gated on `text_focused_ds is None`, so this
            # doesn't change text box behavior.
            ds.invalidate_up()
            if ds is cls.popover_focused_ds:
                # A popover is only HIDDEN on a frame its spawner draws it
                # closed=True, and a nested window is dispatched from the
                # layer loop, where the tile stack is empty - its tile has no
                # parent key, so invalidate_up above reaches nowhere over it.
                # Re-run the view whose body spawned it (draw_tuple_fast's host:
                # the slot holds the PICKER window there, not the chip's ds).
                spawner = getattr(ds, "_parent", None)
                if spawner is not None and spawner is not ds:
                    spawner.invalidate()

            if cls.focused_ds is ds:
                cls.focused_ds = None
            if cls.text_focused_ds is ds:
                cls.text_focused_ds = None
            if cls.popover_focused_ds is ds:
                try:  # XXX: dropdown-close investigation
                    import io as _io, traceback as _tb, imgui as _ig
                    _buf = _io.StringIO(); _tb.print_stack(file=_buf)
                    _tail = "".join(_buf.getvalue().splitlines(keepends=True)[-9:-1])
                    _mx, _my = _ig.get_mouse_pos()
                    _rect = (getattr(ds, "_abs_left", lambda: None)(), getattr(ds, "_abs_top", lambda: None)(),
                             getattr(ds, "width", None), getattr(ds, "height", None))
                    _seed_names = [(getattr(s, "name", None), id(s)) for s in seeds]
                    _in_rect = None
                    try:
                        _l, _t = ds._abs_left(), ds._abs_top()
                        _in_rect = (_l <= _mx <= _l + (ds.width or 0)) and (_t <= _my <= _t + (ds.height or 0))
                    except Exception:
                        pass
                    with open("/tmp/dd_debug.log", "a") as _fh:
                        _fh.write(f"[DD-DBG] clear_focus NULLED popover f={cls.frame_count} "
                                  f"ds={id(ds)}({getattr(ds,'name',None)!r}) grace={popover_grace} "
                                  f"mouse=({_mx:.0f},{_my:.0f}) dd_rect={_rect} mouse_in_dd_rect={_in_rect}\n"
                                  f"   seeds={_seed_names}\n{_tail}\n")
                except Exception as _e:
                    try:
                        with open("/tmp/dd_debug.log", "a") as _fh:
                            _fh.write(f"[DD-DBG] clear_focus NULLED (log err {_e}) f={cls.frame_count} ds={id(ds)}\n")
                    except Exception:
                        pass
                cls.popover_focused_ds = None



    @classmethod
    def _sync_gl_error_checking(cls):
        """Honor Toggles.gl_check_error, disabling PyOpenGL's per-call
        glGetError round-trip when off (a render-thread hotspot).

        Every GL wrapper shares one _ErrorChecker and calls its _currentChecker
        after each GL call. Swapping that to nullGetError suppresses the driver
        round-trip across all built and future functions at once — live and
        independent of import order, unlike the build-time OpenGL.ERROR_CHECKING
        flag. Re-checked each frame (a bool compare + early return) so the
        toggle takes effect at runtime. Best-effort: any failure leaves GL
        checking in its current state."""
        want = bool(Toggles.gl_check_error)
        if want == cls._gl_check_applied:
            return
        try:
            from OpenGL.raw.GL import _errors
            ec = _errors._error_checker
            if ec is None:
                return
            # Capture the default checker once, before the first swap, so enabling
            # restores the exact original (safeGetError or _getErrors).
            if cls._gl_checker_default is None:
                cls._gl_checker_default = ec._registeredChecker
            ec._registeredChecker = cls._gl_checker_default if want else ec.nullGetError
            ec._currentChecker = ec._registeredChecker
            # Keep newly-built wrappers consistent with the live state.
            import OpenGL
            OpenGL.ERROR_CHECKING = want
            cls._gl_check_applied = want
        except Exception:
            pass

    @classmethod
    def px(cls, value):
        """Scale a pixel constant that was AUTHORED at ui_scale 1.0.

        Fonts and imgui style metrics follow the UI scale on their own, but a
        hand-written pixel number in view code does not — a tab pinned to
        height=30 keeps its 30px box while its label grows out of it. Wrap
        those constants in this so they track the text.

        Only for authored constants. A number MEASURED at runtime (a drag
        pickup size, a content_width, anything read back off a draw_state) is
        already in real pixels and would be scaled twice.
        """
        return value * cls.ui_scale

    @classmethod
    def resolve_ui_scale(cls) -> float:
        """Read Toggles.UIScale into cls.ui_scale and return it. Pure resolve
        — no atlas work — so boot can size the first atlas correctly before
        any frame exists (see FontManager construction in LSDStudio)."""
        if Toggles.UIScale.auto_scale:
            scale = cls._auto_ui_scale
            # Re-probe periodically: dragging the OS window to another monitor
            # should retune shortly after, without a glfw walk every frame.
            if scale is None or cls.frame_count % 120 == 0:
                scale = detect_auto_scale(cls.glfw_window)
                cls._auto_ui_scale = scale
        else:
            scale = Toggles.UIScale.scale
        cls.ui_scale = clamp_ui_scale(scale)
        return cls.ui_scale

    @classmethod
    def apply_ui_scale(cls, impl=None):
        """Resolve Toggles.UIScale and, when it moved, re-bake the font atlas
        at the new size.

        MUST be called BETWEEN frames (before imgui.new_frame()) — rebuilding
        the atlas mid-frame pulls the glyph texture out from under the draw
        data being built. This is the only place the scale changes, so views
        see one stable value for the whole frame.

        Scaling here is deliberately just fonts + style metrics: a real
        whole-interface zoom (reporting a logical display size to imgui and
        magnifying at present time) made every offscreen tile allocate and
        repaint at physical resolution and put a resample between the tile and
        the screen — slow, and soft/aliased at any scale but 1.

        The rebuild re-rasterizes every font, so it is gated on the value
        actually changing. Cached handles die with the old atlas: the two
        long-lived ones are refreshed here, and every cached tile is
        invalidated since all text on screen just changed size.
        """
        cls.resolve_ui_scale()

        if cls.font_mgr is None:
            return
        if cls.font_mgr.scale != cls.ui_scale:
            if not cls.font_mgr.rebuild(cls.ui_scale, impl):
                return
        # Lazy fonts: bake whatever last-frame's get()s queued (fonts
        # are seeded minimally at boot - see FontManager.prewarm). Same
        # between-frames slot and same aftermath as a scale rebuild: the
        # atlas repacked, so old handles dangle and every tile is stale.
        elif not cls.font_mgr.flush_pending(impl):
            return
        # Handles grabbed once at boot now point into the freed atlas.
        # peek, not get: a get here would bake DEJAVU_SANS_50 after every
        # flush and force it into the atlas even when nothing draws it (its
        # only push site is test_applet). Consumers of large_font get None
        # until something actually get()s the font.
        cls.large_font = cls.font_mgr.peek(Font.DEJAVU_SANS_50)
        if cls.vis is not None and hasattr(cls.vis, "fa_font"):
            cls.vis.fa_font = cls.font_mgr.peek(Font.FONTAWESOME_14)
        if cls.cache is not None:
            cls.cache.invalidate_all()

    @classmethod
    def resize_gesture_live(cls):
        """A gesture that may be changing view sizes is in flight: a mouse
        drag (any button, or Melty.on_drag), or an OS-window resize whose
        last configure landed within OS_RESIZE_SETTLE_S. The
        activity test of the frozen-blit path (blit_offscreen) and of the
        edge-solve invalidate deferral (columns._drag_live)."""
        if imgui.is_mouse_down(0) or imgui.is_mouse_down(1) or imgui.is_mouse_down(2) or cls.on_drag:
            return True
        return cls.os_resize_live()

    @classmethod
    def os_resize_live(cls):
        """An OS-window resize is in flight: a configure landed within
        OS_RESIZE_SETTLE_S (titlebar.on_surface_resized stamps it)."""
        return time.monotonic() - cls.os_resize_time <= cls.OS_RESIZE_SETTLE_S

    @classmethod
    def editor_keeps_escape(cls, focused):
        kwargs = getattr(focused, '_kwargs', None) or {}
        return (not kwargs.get('single_line', False)
                and not kwargs.get('is_search_box', False)
                and not getattr(cls.focused_ds, 'search_active', False))

    @classmethod
    def focused_key_pending(cls):
        """True when the focused text view must re-run THIS frame to catch
        keyboard input: a non-modifier key event is queued, or one is held.

        Press EDGES from the callback queue (frame_key_events) come FIRST,
        level state second. Under load a key is pressed AND released inside
        one slow frame — both arrive in the same wait_events batch — so
        glfw.get_key already reads RELEASE by the time begin_frame runs, the
        focused editor never re-ran, and end_frame cleared the queued
        keystroke unseen: typed characters vanished whenever the app lagged.
        The queue keeps every press (same fix begin_frame's popover block
        got). The glfw.get_key level scan stays as the HELD-key fallback:
        GLFW REPEAT is sparse or absent (Wayland), so a key held across
        frames must keep re-rendering for the editor's imgui-synthesized
        auto-repeat to sample.

        Bare modifiers count in NEITHER path — no edge, no text — a held
        Shift during a camera pan force-redrew the focused editor's whole
        parent window per frame (120 → 40fps); a modifier+key combo still
        triggers via the non-modifier key itself."""
        for k, _m in cls.frame_key_events:
            if not (glfw.KEY_LEFT_SHIFT <= k <= glfw.KEY_RIGHT_SUPER):
                return True
        for k in range(32, 349):  # GLFW_KEY_SPACE through GLFW_KEY_LAST
            if glfw.KEY_LEFT_SHIFT <= k <= glfw.KEY_RIGHT_SUPER:
                continue
            if glfw.get_key(cls.glfw_window, k) == glfw.PRESS:
                return True
        return False

    @classmethod
    def add_background(cls, style, *, rect=None, corner_radius=0.0, draw_list=None):
        """Suggest a background in the current view's geometry and paint channel.

        Colours are deferred until draw_backgrounds. The image is only a
        compositing slot, preserving clipping, rounded edges and window order.
        """
        if not Toggles.dynamic_styles:
            return
        from src.lsd.gl_gui.gl_state import GLState, gl_limits
        if not cls.draw_state_stack and rect is None:
            return  # Outside a view there is no surface to paint.
        draw_state = cls.draw_state_stack[-1] if cls.draw_state_stack else None
        initializing = cls.dynamic_style_gl is None or not cls.backgrounds
        if cls.dynamic_style_gl is None:
            cls.dynamic_style_gl = GLState()
        capacity = gl_limits()['max_2d']
        if len(cls.backgrounds) >= capacity:
            raise RuntimeError("Dynamic background palette exceeds GL texture capacity")
        previous_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D) if initializing else None
        palette = cls.dynamic_style_gl.fbo('background_palette', capacity, 1)
        if initializing:
            gl.glBindTexture(gl.GL_TEXTURE_2D, previous_texture)
        index = len(cls.backgrounds)
        cls.backgrounds.append((draw_state, style))
        if rect is not None:
            # Inline primitives inherit the owning view without replacing its
            # context (not making the next sibling inherit the button).
            cls.background_inline_indices.add(index)
            left, top, width, height = rect
            dl = draw_list if draw_list is not None else imgui.get_window_draw_list()
            clip = (*dl.get_clip_rect_min(), *dl.get_clip_rect_max())
            cls.background_rects.append((left, top, width, height, clip, corner_radius))
            uv = ((index + 0.5) / capacity, 0.5)
            dl.add_image_rounded(palette.texture_id, (left, top),
                                 (left + width, top + height), uv, uv, 0xffffffff, corner_radius)
            return
        left, top = draw_state.abs_left, draw_state.abs_top
        width, height = draw_state.width or 0, draw_state.height or 0
        # Shadow metadata uses the same enclosing background contexts as colour.
        # Submit over the existing shadow pass while its clip/layer are live.
        from src.lsd.gl_gui.style import resolve_shadow_offset, default_scalar_accumulation
        from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
        if index == 0:
            cls.background_shadow_offsets.clear()
        # Walk up to the nearest ancestor resolved THIS frame, else one whose
        # body was cache-served (its tile is blitted, so it never re-submits)
        # and carries the offset it resolved to last time (_dynamic_shadow).
        parent = draw_state._parent
        seen = {id(draw_state)}
        ancestors = []
        while (parent is not None and id(parent) not in cls.background_shadow_offsets
               and id(parent) not in seen and getattr(parent, '_dynamic_shadow', None) is None):
            seen.add(id(parent))
            ancestors.append(parent)
            parent = parent._parent
        offset, shadow_fn = cls.background_shadow_offsets.get(
            id(parent), getattr(parent, '_dynamic_shadow', None) or (0.0, default_scalar_accumulation))
        for ancestor in reversed(ancestors):
            kwargs = getattr(ancestor, '_kwargs', None) or {}
            ancestor_style = kwargs.get('style', kwargs.get('tint'))
            override = getattr(ancestor_style, 'shadow_fn', None)
            if override is not None:
                shadow_fn = override
        override = getattr(style, 'shadow_fn', None)
        if override is not None:
            shadow_fn = override
        offset = resolve_shadow_offset(style, offset, shadow_fn)
        cls.background_shadow_offsets[id(draw_state)] = (offset, shadow_fn)
        # Bypass the @live hook: a per-frame stamp must not invalidate the tile.
        object.__setattr__(draw_state, '_dynamic_shadow', (offset, shadow_fn))
        if width <= 0 or height <= 0:
            cls.background_rects.append(None)
            return
        cls.background_rects.append(cls._background_rect(draw_state, left, top, width, height))
        if offset:
            add_shadow((left, top, width, height), offset=offset,
                       corner_radius=draw_state.corner_radius)
        uv = ((index + 0.5) / capacity, 0.5)
        imgui.get_window_draw_list().add_image_rounded(
            palette.texture_id, (left, top), (left + width, top + height),
            uv, uv, 0xffffffff, draw_state.corner_radius)

    @staticmethod
    def _background_rect(draw_state, left, top, width, height):
        clip = getattr(draw_state, 'abs_clip_rect', None)
        if not clip:
            clip = (left, top, left + width, top + height)
        return (left, top, width, height, tuple(float(c) for c in clip),
                float(getattr(draw_state, 'corner_radius', 0.0) or 0.0))

    @classmethod
    def add_cached_background(cls, draw_state):
        """A blit-cache hit skips the body, so add_background never runs for
        it; its tile already holds the right pixels, but live text drawn
        OVER the tile (a hovered row) must still see its surface. Re-submit
        the rect with the colour the view resolved to last time."""
        if not Toggles.dynamic_styles or getattr(draw_state, '_dynamic_bg', None) is None:
            return
        width, height = draw_state.width or 0, draw_state.height or 0
        if width <= 0 or height <= 0:
            return
        cls.backgrounds.append((draw_state, _CACHED_BACKGROUND))
        cls.background_rects.append(cls._background_rect(
            draw_state, draw_state.abs_left, draw_state.abs_top, width, height))

    @classmethod
    def draw_backgrounds(cls):
        """Resolve the queued style hierarchy in one pass before draw lists."""
        if not Toggles.dynamic_styles or not cls.backgrounds:
            return
        import numpy as np
        from src.lsd.gl_gui.style import draw_background, default_tint_accumulation
        from src.lsd.gl_gui.gl_state import gl_limits
        root = tuple(hdr_color.srgb_to_linear(c) for c in Toggles.dynamic_style_root) + (1.0,)
        resolved = {}
        colors = []
        for index, (draw_state, style) in enumerate(cls.backgrounds):
            inline = index in cls.background_inline_indices
            if style is _CACHED_BACKGROUND:
                color, tint_fn = draw_state._dynamic_bg
                resolved[id(draw_state)] = (color, tint_fn)
                colors.append(color)
                continue
            parent = draw_state
            seen = set()
            ancestors = []
            # Nearest ancestor resolved this frame, else one whose body was
            # cache-served (blitted tile, no re-submit) with last frame's
            # colour stamped on it (_dynamic_bg): a live child under a cached
            # parent keeps drawing on the parent's surface, not the root.
            while (parent is not None and id(parent) not in resolved and id(parent) not in seen
                   and ((parent is draw_state and not inline) or getattr(parent, '_dynamic_bg', None) is None)):
                seen.add(id(parent))
                if parent is not draw_state or inline:
                    ancestors.append(parent)
                parent = parent._parent
            behind, tint_fn = resolved.get(
                id(parent), getattr(parent, '_dynamic_bg', None) or (root, default_tint_accumulation))
            # A view can change policy without painting a background of its own.
            for ancestor in reversed(ancestors):
                kwargs = getattr(ancestor, '_kwargs', None) or {}
                ancestor_style = kwargs.get('style', kwargs.get('tint'))
                override = getattr(ancestor_style, 'tint_fn', None)
                if override is not None:
                    tint_fn = override
            override = getattr(style, 'tint_fn', None)
            if override is not None:
                tint_fn = override
            color = draw_background(style, behind, tint_fn)
            if not inline:
                resolved[id(draw_state)] = (color, tint_fn)
                object.__setattr__(draw_state, '_dynamic_bg', (color, tint_fn))
            colors.append(color)
        palette = cls.dynamic_style_gl.fbo('background_palette', gl_limits()['max_2d'], 1)
        previous_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, palette.texture_id)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, len(colors), 1,
                           gl.GL_RGBA, gl.GL_FLOAT, np.asarray(colors, dtype=np.float32))
        gl.glBindTexture(gl.GL_TEXTURE_2D, previous_texture)
        cls.style_context_root = root
        cls.background_gen += 1

    @classmethod
    def begin_frame(cls):
        cls._sync_gl_error_checking()
        # Every cached tile holds pixels composed on the previous root colour
        # (and then adjusted against it): a root / toggle change repaints all.
        # Tracked ON the cache: every Surface swaps in its own, and a key on
        # this class would let the first surface to notice consume the change.
        dynamic_style_key = (Toggles.dynamic_styles, tuple(Toggles.dynamic_style_root))
        if (cls.cache is not None
                and dynamic_style_key != getattr(cls.cache, '_dynamic_style_seen', None)):
            cls.cache._dynamic_style_seen = dynamic_style_key
            cls.cache.invalidate_all()
        cls.backgrounds.clear()
        cls.background_inline_indices.clear()
        cls.background_rects.clear()
        cls.background_shadow_offsets.clear()
        cls.unique_stack = []
        cls.draw_state_stack = []
        cls.font_style_stack = []
        cls.font_base_stack = []
        cls.flow_spacing = 0.0
        cls.indent_count = 0
        cls.unindent_count = 0

        if cls.draw_state_registry is None:
            cls.draw_state_registry = cls.vis.root.draw_state_registry

        # Style metrics are authored at scale 1 and multiplied by the UI scale
        # (the other half of Toggles.UIScale - see apply_ui_scale). These are
        # re-clamped from the constants every frame, so the multiply is
        # idempotent and a live scale edit lands on the next frame. Rounding
        # scales too, so corners keep their proportion to the frame.
        s = cls.ui_scale
        style = imgui.get_style()
        style.frame_rounding = 5.0 * s
        style.item_spacing = (5 * s, 0)
        style.window_padding = (3 * s, 0)
        style.frame_padding = (4 * s, 1 * s)
        style.scrollbar_size = 14.0 * s
        style.scrollbar_rounding = 9.0 * s
        style.grab_min_size = 10.0 * s
        style.indent_spacing = 21.0 * s
        style.item_inner_spacing = (4 * s, 4 * s)

        cls.any_window_hovered = cls.any_window_hovered_pending
        cls.any_window_hovered_pending = False
        style = imgui.get_style()
        cls.seen_unique = set()
        cls.original_spacing = style.item_spacing
        cls.original_window_padding = style.window_padding
        cls.original_frame_padding = style.frame_padding

        cls.returned_values.update(cls.pending_return_values)
        cls.pending_return_values = {}

        Counters.nested_window_count = 0

        if cls.glfw_close_requested:
            cls.event_handler.feed_down(input_id="glfw_close", x=0, y=0, t=time.perf_counter())
            cls.glfw_close_requested = False

        cls.layer_inc = 0.04 / ((Melty.max_layer - 1.0) * (Melty.max_depth - 1.0)) * 65535.0

        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cls.default_framebuffer())
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)
        is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
        Melty.imgui_popup_open = is_popup_open

        # --- Melty/imgui text-focus mutual exclusion (reverse direction) ---
        # A Melty draw_text editor (Melty.text_focused_ds) and an imgui
        # input_text must never both own the keyboard. The forward case - a
        # click exits a Melty editor - is handled by imgui itself (the click
        # lands outside the active input, so imgui deactivates it next frame).
        # The reverse case isn't: clicking an imgui input_text leaves the old
        # Melty editor focused, so we drain keystrokes. Detect imgui newly
        # capturing text (io.want_text_input rising edge - only an imgui input
        # raises it; Melty editors aren't imgui items) and clear Melty focus.
        # Edge-triggered, not level: a level check would re-clear Melty focus
        # for the one frame after a forward click while imgui is still shutting
        # down its old input, so the editor could never keep focus.
        want_text = imgui.get_io().want_text_input
        if want_text and not cls._prev_imgui_want_text and cls.text_focused_ds is not None:
            cls.text_focused_ds = None
            if Toggles.TextEditor.text_focus_stack_trace:
                print_stack_trace()
        cls._prev_imgui_want_text = want_text

        # Route the keyboard to the focused text view. While a text editor holds
        # focus, any held key force-invalidates its tile (and its parent window
        # subtree, so the cached window re-descends into the editor) BEFORE the
        # views draw this frame. That lets draw_text re-execute and catch
        # imgui's is_key_pressed edge in the SAME frame the key goes down, even
        # when the mouse isn't hovering. This must run in begin_frame, not
        # end_frame: end_frame invalidation lands one frame too late, after the
        # key edge has already passed, which is why typing only worked while
        # hovering (the hover path keeps the tile dirty before each draw).
        focused = None
        if cls.text_focused_ds is not None and cls.glfw_window is not None:
            focused = cls.text_focused_ds
            # Multiline editors keep keyboard ownership on Escape. Otherwise
            # dismissing an already-closed hint turns ordinary typing into
            # global shortcuts. Single-line fields retain their pop gesture.
        # Press EDGE from the callback queue, not glfw.get_key level state: a
        # tap stays PRESSED across multiple frames, so the level check would
        # re-fire on frame 2 - right after a popup consumed the event - and
        # clear focus anyway.
        if any(k == glfw.KEY_ESCAPE for k, _ in cls.frame_key_events):
            # An open editor popup (code suggestions / usage-jump / import
            # quick-fix) owns this Esc: it should only close the popup, not
            # release text focus. The Esc handlers live inside the focused
            # editor's render, so re-run its subtree and let them consume it.
            if focused is not None and (cls.editor_keeps_escape(focused)
                                        or getattr(focused, '_ac_open', False)
                                        or getattr(focused, '_uj_open', False)
                                        or getattr(focused, '_qf_open', False)):
                cls.cache.invalidate_up(focused._tile_id, force=True)
                request_render()
            else:
                # Esc closes the active find bar - no hover required. clear_focus no
                # longer closes search (so clicking away leaves bars open), so close
                # the focused_ds owner's bar explicitly here. Other open find bars
                # stay until their own Esc tap. Once focused_ds is cleared, the
                # bar's re-grab can't reclaim focus, so the bar dismisses itself.
                if cls.focused_ds is not None and getattr(cls.focused_ds, 'search_active', False):
                    cls.focused_ds.search_active = False
                    cls.focused_ds._search_was_active = False
                    cls.focused_ds.invalidate_up()

                cls.clear_focus()

                if Toggles.TextEditor.text_focus_stack_trace:
                    print_stack_trace()
                request_render()

        # Global hotkey: bare E toggles the invalidation tracker (the rects
        # in the "invalidate" notify log). Press edge from the callback
        # queue; only when no Melty editor and no imgui input owns the
        # keyboard, and no modifier is held (Ctrl+E etc. stay free).
        if (focused is None and not want_text
                and any(k == glfw.KEY_E and not (m & (glfw.MOD_CONTROL | glfw.MOD_ALT | glfw.MOD_SUPER))
                        for k, m in cls.frame_key_events)):
            from src.lsd.gl_gui.view.core_views.new_core_view import toggle_setting
            new_value = toggle_setting("InvalidateTracker.enable")
            notify(f"InvalidateTracker {'on' if new_value else 'off'}",
                   tint=(1, 1, 0.4), tag="InvalidateTracker")
            request_render()

        # Registered global hotkeys (register_global_hotkey), same press edge
        # queue; a Melty editor or an imgui input owning the keyboard mutes
        # the registrations that didn't opt into text focus.
        if cls.fire_global_hotkeys(keyboard_owned=focused is not None
                                   or bool(want_text)):
            request_render()

        if focused is not None and not any(k == glfw.KEY_ESCAPE
                                           for k, _ in cls.frame_key_events):
            if cls.focused_key_pending():
                # Owner-anchored: invalidate_up on the FOCUSED editor re-runs
                # its own subtree (token views catch the key edge) and the
                # invalidate() climb force-marks every ancestor on the
                # parent-key path, so the cached window still re-descends
                # into the editor. Targeting the parent WINDOW here instead
                # force-swept the window's ENTIRE subtree - the structured
                # code-dict pane included, a ~16-20ms rebuild - on every
                # frame any key was held.
                note = Note(name="Melty, on glfw key press", tint=(1, 0.5, 0), rect=(0, 0, 100, 20))
                cls.cache.invalidate_up(focused._tile_id, force=True, note=note)
                request_render()

        # Open dropdown popover: re-run its owning view on the EVENTS it responds to
        # rather than every frame - (a) a navigation key is down (Esc/arrows/Enter,
        # delivered with no delay), or (b) the text view under the cursor inside
        # the popover changed (the pointer moved to a different row, so a sub-menu
        # should open/close). The cached owner wouldn't otherwise re-descend into
        # its un-cached menu. A still pointer + no keys repaints nothing. (Typing
        # into the search box is handled by the text-focus block above, which
        # invalidates up to this view through the box's parent.)
        if cls.popover_focused_ds is not None and cls.glfw_window is not None:
            pop = cls.popover_focused_ds
            _need = False
            _nav_keys = (glfw.KEY_ESCAPE, glfw.KEY_UP, glfw.KEY_DOWN,
                         glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
            # Work off the GLFW windowed key QUEUE (frame_key_events) - the same
            # source the popover reads - not glfw.get_key level state. A fast Esc /
            # Enter key is pressed-and-released between frames, so the level check
            # misses it and the popover never re-runs to handle it (arrows survive
            # only/c they're held); the queue keeps every press. glfw.get_key
            # stays as a fallback so a held key keeps re-rendering.
            if any(k in _nav_keys for k, _ in cls.frame_key_events):
                _need = True
            elif any(glfw.get_key(cls.glfw_window, k) == glfw.PRESS for k in _nav_keys):
                _need = True

            if _need:
                # Owner-anchored (same reason as the text-focus block above):
                # invalidate_up on the popover owner re-runs the menu subtree and
                # the ancestor climb re-descends the window below it. The parent-
                # WINDOW sweep this replaces force-invalidated every sibling
                # subtree per held nav key - Enter is a nav too, so with the
                # one popup open that was a full dict-pane rebuild per
                # frame ("Unnamed invalidate_up" in the bump trace).
                cls.cache.invalidate_up(pop._tile_id, force=True)
                request_render()

        mouse_pos = imgui.get_mouse_pos()
        ds_under_mouse = Melty.bvh_query(mouse_pos[0], mouse_pos[1])
        cls.bvh_hover_ids = {id(ds) for ds in ds_under_mouse}

        last_hovered = cls.hovered_ds
        cls.hovered_ds = ds_under_mouse[0] if ds_under_mouse else None
        # for ds in ds_under_mouse:
        #     ds._hover_eligible = Melty.frame_count
        #
        # if cls.hovered_ds is not None and not cls.on_drag:
        #     if (not cls.on_drag and not imgui.is_mouse_down(2)):
        #         Melty.cache.invalidate(cls.hovered_ds._parent._tile_id, do_store=False, force=True)

        hovered_id = id(cls.hovered_ds) if cls.hovered_ds is not None else None
        last_hovered_id = id(last_hovered) if last_hovered is not None else None
        #
        # if hovered_id != last_hovered_id:
        #     if last_hovered is not None:
        #         Melty.cache.invalidate(last_hovered, do_store=False, force=True)

        cls.backend.pump()
        space_mouse.pump(cls.event_handler)
        cls.space_mouse_drag = space_mouse.active()

        # Orchestrated record/replay injection does NOT run here: it runs
        # from SplitOverlayRenderer.process_inputs, BEFORE imgui.new_frame,
        # so an injected press reaches the handler AND imgui's io in the
        # SAME frame - the order a real GLFW callback + process_inputs give
        # a real press. Injecting from here (inside the imgui frame) the
        # handler saw a press one frame before imgui did: the handler-only
        # frame raised a behind window and latched its move handle while
        # the widget under the press was not active yet, and the gesture
        # then dragged the window instead of the widget (09-01).

        cls.last_draw_state = [(None, None)] * cls.max_layer

        cls.imgui_active = cls.imgui_active_pending
        cls.imgui_active_pending = False
        cls.bg_depth = 0

        cls.imgui_blockers = cls.pending_blockers
        cls.pending_blockers = [None] * cls.max_layer

        cls.events, cls.events_by_type = cls.event_handler.process_frame(on_pointer_down=cls.raise_pressed_window)

        # Pointer shape: the frame about to draw, pushed NOW against the
        # freshest pointer position rather than after the (possibly slow)
        # draw pass; the render tail pushes again against imgui's immediate
        # shapes. See mouse_cursor.apply.
        if cls.glfw_window is not None:
            from src.lsd.gl_gui import mouse_cursor
            mouse_cursor.apply(cls.glfw_window, early=True)

        # Apply a Ctrl+Enter "click the selected search result" injection queued
        # last frame - now, before any view renders, so the view reads it via
        # the per-view event merge. Add two names so it reaches whichever the
        # target view reads (button: left_mouse_down; managed window: mouse_down).
        if cls.search_click_pending is not None:
            _click_tid, _click_ev = cls.search_click_pending
            cls.search_click_pending = None
            _click_slot = cls.events.setdefault(_click_tid, {})
            _click_slot["left_mouse_down"] = _click_ev
            _click_slot["mouse_down"] = _click_ev

        cls.window_drag = ((("left_mouse_drag" in cls.events_by_type) or ("left_mouse_held" in cls.events_by_type)) or
                           (("right_mouse_drag" in cls.events_by_type) or ("right_mouse_held" in cls.events_by_type)))

        left_mouse_drag_event = cls.events_by_type.get("left_mouse_drag", None)
        right_mouse_drag_event = cls.events_by_type.get("right_mouse_drag", None)
        if right_mouse_drag_event is not None:
            is_window_resize = "window_resize" in str(right_mouse_drag_event.keys())
        else:
            is_window_resize = False

        if left_mouse_drag_event is not None:
            # dnd_item: an item drag-and-drop gesture gets the same churn
            # suppression (hover invalidation, content-height changes, scroll
            # clamps) as a window drag - both ride the blit fast path.
            is_window_drag = ("window_move" in str(left_mouse_drag_event.keys())
                              or "corner_drag" in str(left_mouse_drag_event.keys())
                              or "dnd_item" in str(left_mouse_drag_event.keys()))
        else:
            is_window_drag = False

        # A 3D-mouse flight counts as a drag: every invalidation gate keyed
        # on on_drag (hover changes, clip reveals, glow kills, content-height
        # commits, scroll clamps, freeze-resize) stays quiet until the cap
        # settles, exactly like for a held mouse button.
        cls.on_drag = (((is_window_drag or ("left_mouse_down" in cls.events_by_type))
                        and (not cls.imgui_active)) or cls.space_mouse_drag)

        cls.event_handler.begin_frame()

        if ("right_mouse_drag" in cls.events_by_type):
            right_mouse_drag_events = cls.events_by_type["right_mouse_drag"]
            for event in right_mouse_drag_events:
                Melty.cache.invalidate(event)

        if ("middle_mouse_drag" in cls.events_by_type):
            right_mouse_drag_events = cls.events_by_type["middle_mouse_drag"]
            for event in right_mouse_drag_events:
                note = Note(name=event, reason="middle_mouse_drag", tint=(0, 1, 1))
                Melty.cache.invalidate(event, note=note)

        # A 3D-mouse flight is the middle-drag's twin: the receiving view
        # (draw_voxels) is force-refreshed each frame by the SAME per-target
        # blit bypass, and nothing else; on_drag (folded from
        # space_mouse_drag) keeps the general purpose invalidate_up below and
        # every other churn gate quiet for the rest of the app.
        if ("space_mouse_changed" in cls.events_by_type):
            for event in cls.events_by_type["space_mouse_changed"]:
                note = Note(name=event, reason="space_mouse", tint=(0, 1, 1))
                Melty.cache.invalidate(event, note=note)

        # Double-drags (the 2nd press of a double-click, held + dragged) are
        # always deliberate content gestures - never a window move - so a view
        # receiving one is force-refreshed each frame, the same blit bypass as
        # right/middle camera drags get above. Without this a cached target
        # (use_cache=True, e.g. a voxel brightness/contrast slider) would apply
        # the first frame then ride its blit and freeze. Action-keyed so it
        # covers double_left/right/middle_mouse_drag regardless of param name.
        for _evts in cls.events_by_type.values():
            for _view_id, _ev in _evts.items():
                if _ev.action == EventAction.DOUBLE_DRAGGED:
                    note = Note(name=_view_id, reason="double_drag", tint=(1, 0, 1))
                    Melty.cache.invalidate(_view_id, note=note)

        event_keys = list(cls.events.keys())
        # To string
        event_keys_str = [str(k) for k in event_keys]
        concat_names = "_".join(event_keys_str)

        cls.on_scroll_buffer.append("scroll_y_changed" in cls.events_by_type and "view_scroll" in concat_names)
        cls.on_scroll = any(cls.on_scroll_buffer)


        # for view_id, evts in cls.events.items():
        #     for e in evts:
        #         print(f"  {view_id}: {e.input_id}:{e.action}")

        # cls.texture_manager.upload_pending()

        # # Check live attributes
        # for obj, attributes in cls.live_attributes.items():
        #     for attrib in attributes:
        #         try:
        #             new_value = getattr(obj, attrib)
        #         except Exception:
        #             continue

        Melty.bg_stack = [(0, 0, 0)]

        Melty.active_layer = 0
        Melty.paint_rank = 0
        cls.frame_count += 1
        cls.blocker_hovered = False

        cls.layers.clear()
        for _ in range(cls.nested_layer_max):
            cls.layers.append([])
        for view_id, evts in cls.events.items():
            first_event = list(evts.values())[0]
            if first_event.tile_id != "hovered":
                if (first_event.tile_id is not None and not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
                        and not imgui.is_mouse_down(2) and not cls.on_scroll
                        and not cls.space_mouse_drag):
                        print(first_event)
                        Melty.cache.invalidate_up(first_event.tile_id, max_depth=10, force=True)

        Melty.all_uniques = set()

        Melty.hovered_drawstate_pending = set()

        Melty.clip_stack = []
        # cls._root_by_module[module_id] = root
        # cls._gen_by_module.setdefault(module_id, 0)
        # cls._path_stack.clear()
        fb_w, fb_h = map(int, imgui.get_io().display_size)  # WINDOW CONTENT (frame_inset)
        cls.display_size = (fb_w, fb_h)
        # Masks/tiles cover the WHOLE framebuffer (shadow margin included).
        real_fb = cls.framebuffer_size or (fb_w, fb_h)
        cls.cache.mask_begin_frame((int(real_fb[0]), int(real_fb[1])))

        # Channel-split the foreground/overlay draw list the same way as the
        # window draw list (max_depth channels) so per-window overlays can be
        # stencil-masked by higher-layer windows during the deferred pass.
        # Channel = Melty.layer_channel(draw_state.layer). The top channel is
        # the unmasked default, so global overlays appear on top.
        overlay = imgui.get_overlay_draw_list()
        overlay.channels_split(Melty.max_layer)
        overlay.channels_set_current(Melty.max_layer - 1)
        cls._overlay_channels_active = True
        cls._overlay_channel_ranges = []

        from src.lsd.gl_gui.view.core_views.core_render_helpers import clear_floating_text_cache
        clear_floating_text_cache()

    @staticmethod
    def _closest_perimeter_points(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
        """Connection points anchored at the center of the rects' shared edge.

        On each axis the connection coordinate is the center of the overlap span
        between the two rects, then clamped onto each rect. Where the rects
        overlap this lands the connector on the midpoint of their shared edge;
        where they don't it slides out to the facing edges/corners. The overlap
        bounds use a smooth min/max (window Swoosh.edge_softness) so the
        anchor glides as the overlap region changes instead of snapping at the
        kinks of hard min/max.

        When the two rects overlap on both axes the shared-edge center lands
        *inside* their intersection rect — under the (on-top) child, hiding the
        parent's point and forcing the connector across the child. If
        Swoosh.avoid_intersection is on, both endpoints are then slid along their
        own rect's edge, out of the intersection rect, to flank a reentrant
        corner of the union (a corner of the intersection where a parent edge
        meets a child edge). The returned control point bows the curve out
        through that corner into the exterior, so the line hugs the outside of
        the overlap instead of crossing either view. The slide is bounded by the
        exposed edge length, so it degrades to a short hook on heavy overlap and
        falls back to the plain anchor under full containment.

        As the overlap deepens from zero, the endpoints ease from the plain
        shared-edge anchor to the slid corner positions over Swoosh.intersect_soft
        px (and the control point blends with the default bow over the same
        window) so entering the overlap doesn't snap between modes.

        Returns (x0, y0, x1, y1, ctrl, g) where ctrl is a (cx, cy) bezier control
        point (None to use the default perpendicular bow) and g in [0, 1] is the
        blend weight of the overlap mode (the caller blends the bow by g).
        """
        k = Swoosh.edge_softness

        def smin(a, b):
            # Polynomial smooth-min: blends within a window of width k.
            if k <= 0.0:
                return a if a < b else b
            h = max(k - abs(a - b), 0.0) / k
            return (a if a < b else b) - h * h * k * 0.25

        def smax(a, b):
            return -smin(-a, -b)

        def clamp(v, lo, hi):
            return lo if v < lo else hi if v > hi else v

        # Center of the (smoothed) overlap span on each axis = shared-edge center.
        cx = (smax(ax0, bx0) + smin(ax1, bx1)) * 0.5
        cy = (smax(ay0, by0) + smin(ay1, by1)) * 0.5

        # Disjoint anchor (the g=0 end of the transition): plain shared-edge
        # center clamped onto each rect.
        dax, day = clamp(cx, ax0, ax1), clamp(cy, ay0, ay1)
        dbx, dby = clamp(cx, bx0, bx1), clamp(cy, by0, by1)

        if not Swoosh.avoid_intersection:
            return dax, day, dbx, dby, None, 1.0

        # Hard intersection rect of the two rects (the region under the child).
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        wx, wy = ix1 - ix0, iy1 - iy0
        if wx <= 0.0 or wy <= 0.0:
            # No 2-D intersection: the shared-edge center is already outside.
            return dax, day, dbx, dby, None, 1.0

        tol = 1e-6
        # Which rect owns each intersection edge (an edge may be shared).
        xL_P, xL_C = abs(ix0 - ax0) < tol, abs(ix0 - bx0) < tol
        xR_P, xR_C = abs(ix1 - ax1) < tol, abs(ix1 - bx1) < tol
        yT_P, yT_C = abs(iy0 - ay0) < tol, abs(iy0 - by0) < tol
        yB_P, yB_C = abs(iy1 - ay1) < tol, abs(iy1 - by1) < tol

        # Reentrant corners of the union: an intersection corner where one rect
        # owns the x-edge and the other owns the y-edge (so a parent edge meets a
        # child edge there). The exterior opens up outside such a corner.
        corners = []
        for ex, exP, exC, xv in (("L", xL_P, xL_C, ix0), ("R", xR_P, xR_C, ix1)):
            for ey, eyP, eyC, yv in (("T", yT_P, yT_C, iy0), ("B", yB_P, yB_C, iy1)):
                p_owns_x = exP and eyC   # parent owns x-edge, child owns y-edge
                p_owns_y = exC and eyP   # child owns x-edge, parent owns y-edge
                if p_owns_x or p_owns_y:
                    corners.append((ex, ey, xv, yv, p_owns_x))
        if not corners:
            # Full containment - no exterior notch (no reentrant corner exists).
            if bx0 <= ax0 and by0 <= ay0 and bx1 >= ax1 and by1 >= ay1:
                # Parent is completely under the (on-top) child: hide the connector.
                return None
            # Child sits inside the parent: anchor to matching edges on the side
            # where it sits closest - left↔left / right↔right when closest
            # horizontally, top↔top / bottom↔bottom when vertically - blending
            # between the two as it rounds a corner. Each endpoint is the exit of a
            # ray cast from its rect's centre through the blended edge target, so
            # the points slide smoothly along the corners (the default slope bow
            # then curves the diagonal as in the non-overlap case).
            acx, acy = (ax0 + ax1) * 0.5, (ay0 + ay1) * 0.5
            bcx, bcy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
            # Always anchor to the left and bottom edges so the connection never
            # flips left↔right or top↔bottom as the child crosses centre.
            h_px, h_cx, gh = ax0, bx0, bx0 - ax0   # left edge
            v_py, v_cy, gv = ay1, by1, ay1 - by1   # bottom edge
            # Prefer horizontal edges: pin s to 0 (horizontal/left edge) or 1
            # (vertical/bottom edge), only opening the diagonal blend window when
            # the child is actually near the corner - i.e. the nearer gap is
            # within a zone (Swoosh.envelop_corner × the parent's shorter side).
            # Far from the corner the window collapses, so the line stays flat
            # until it is near to rounding the corner.
            raw = gh / (gh + gv + 1e-6)
            zone = Swoosh.envelop_corner * min(ax1 - ax0, ay1 - ay0)
            m = gh if gh < gv else gv
            prox = (1.0 - m / zone) if (zone > 1e-6 and m < zone) else 0.0
            prox = prox * prox * (3.0 - 2.0 * prox)
            w = Swoosh.envelop_tie * prox
            t = (raw - 0.5 + w) / (2.0 * w) if w > 1e-6 else (1.0 if raw >= 0.5 else 0.0)
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            s = t * t * (3.0 - 2.0 * t)

            def ray(cx0, cy0, rx0, ry0, rx1, ry1, dx, dy):
                tx = (rx1 - cx0) / dx if dx > 1e-9 else (rx0 - cx0) / dx if dx < -1e-9 else float("inf")
                ty = (ry1 - cy0) / dy if dy > 1e-9 else (ry0 - cy0) / dy if dy < -1e-9 else float("inf")
                t = tx if tx < ty else ty
                return cx0 + dx * t, cy0 + dy * t

            xa, ya = ray(acx, acy, ax0, ay0, ax1, ay1,
                         (h_px - acx) * (1 - s) + (bcx - acx) * s,
                         (bcy - acy) * (1 - s) + (v_py - acy) * s)
            xb, yb = ray(bcx, bcy, bx0, by0, bx1, by1,
                         (h_cx - bcx) * (1 - s), (v_cy - bcy) * s)
            return xa, ya, xb, yb, None, 1.0

        icx, icy = (ix0 + ix1) * 0.5, (iy0 + iy1) * 0.5
        acx, acy = (ax0 + ax1) * 0.5, (ay0 + ay1) * 0.5
        bcx, bcy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
        ddx, ddy = bcx - acx, bcy - acy

        # Pick the reentrant corner continuously: most counter-clockwise from the
        # parent->child vector. Rotates as the child is dragged; only flips when
        # the rects are (anti)concentric or the overlap changes corner/edge type.
        best = None
        for cn in corners:
            key = ddx * (cn[3] - icy) - ddy * (cn[2] - icx)
            if best is None or key > best[0]:
                best = (key, cn)
        ex, ey, xv, yv, p_owns_x = best[1]

        hook = Swoosh.intersect_hook

        def lim(avail):
            return max(0.0, min(hook, avail))

        if p_owns_x:
            # parent on the vertical x=xv edge; child on the horizontal y=yv edge
            hp = lim(iy0 - ay0 if ey == "T" else ay1 - iy1)
            nax, nay = xv, (iy0 - hp if ey == "T" else iy1 + hp)
            hc = lim(ix0 - bx0 if ex == "L" else bx1 - ix1)
            nbx, nby = (ix0 - hc if ex == "L" else ix1 + hc), yv
        else:
            # parent on the horizontal y=yv edge; child on the vertical x=xv edge
            hp = lim(ix0 - ax0 if ex == "L" else ax1 - ix1)
            nax, nay = (ix0 - hp if ex == "L" else ix1 + hp), yv
            hc = lim(iy0 - by0 if ey == "T" else by1 - iy1)
            nbx, nby = xv, (iy0 - hc if ey == "T" else iy1 + hc)

        # Control point: bow out through the corner, away from the overlap centre.
        dx, dy = xv - icx, yv - icy
        n = math.hypot(dx, dy) or 1.0
        push = max(hp, hc) * 0.9
        ctrl = (xv + dx / n * push, yv + dy / n * push)

        # Ease from the shared-edge anchor to the slid corner anchor as the
        # overlap deepens. Both coordinates are interpolated; the caller projects
        # the result back onto the rect's (rounded) perimeter so the endpoint
        # slides along the edge and around corners instead of cutting across.
        soft = Swoosh.intersect_soft
        depth = min(wx, wy)
        if soft <= 0.0:
            g = 1.0
        else:
            t = depth / soft
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            g = t * t * (3.0 - 2.0 * t)
        xa = dax + (nax - dax) * g
        ya = day + (nay - day) * g
        xb = dbx + (nbx - dbx) * g
        yb = dby + (nby - dby) * g
        return xa, ya, xb, yb, ctrl, g

    @staticmethod
    def _round_rect_point(px, py, x0, y0, x1, y1, r):
        """Project a point on a rect's square boundary onto its rounded-corner
        boundary, so the connector meets the visible (rounded) edge instead of
        sitting just off the square corner. Points on the straight portions of
        edges are left unchanged."""
        if r <= 0.0:
            return px, py
        # Corner-arc center: clamp into the inner box inset by r. On a straight
        # edge this stays level with the point (no shift); near a corner it pins
        # to the arc center, and we reproject the point onto that arc.
        cxc = x0 + r if px < x0 + r else (x1 - r if px > x1 - r else px)
        cyc = y0 + r if py < y0 + r else (y1 - r if py > y1 - r else py)
        dx, dy = px - cxc, py - cyc
        d = math.hypot(dx, dy)
        if d > r and d > 1e-6:
            return cxc + dx / d * r, cyc + dy / d * r
        return px, py

    @staticmethod
    def _project_to_rounded_rect(px, py, x0, y0, x1, y1, r):
        """Project an arbitrary point onto the nearest point of a rect's
        rounded-corner perimeter. A rounded rect is the inset box [+r] expanded
        by r, so the nearest boundary point is the nearest point of the inset box
        pushed out by r along the offset direction. Used to keep a connection
        endpoint on the visible edge while both of its coordinates interpolate."""
        ix0, iy0, ix1, iy1 = x0 + r, y0 + r, x1 - r, y1 - r
        if ix1 < ix0:
            ix0 = ix1 = (x0 + x1) * 0.5
        if iy1 < iy0:
            iy0 = iy1 = (y0 + y1) * 0.5
        qx = ix0 if px < ix0 else ix1 if px > ix1 else px
        qy = iy0 if py < iy0 else iy1 if py > iy1 else py
        dx, dy = px - qx, py - qy
        d = math.hypot(dx, dy)
        if d > 1e-6:
            # Outside the inset box: ride the rounded boundary at radius r.
            return qx + dx / d * r, qy + dy / d * r
        # Inside the inset box: drop straight out to the nearest straight edge.
        dl, dr, dt, db = px - x0, x1 - px, py - y0, y1 - py
        m = min(dl, dr, dt, db)
        if m == dl:
            return x0, py
        if m == dr:
            return x1, py
        if m == dt:
            return px, y0
        return px, y1

    @staticmethod
    def _highlight_rgb(tint=None):
        """Super-bright version of a view's tint, used for the nested-view
        highlight (swoosh + outline boxes). Pass the tint stashed on the
        draw_state at draw time (draw_state.current_tint): by this post-draw
        pass the style manager no longer holds it. Falls back to the live tint,
        then to a static tint, when nothing was stashed."""
        sm = Melty.style_manager
        if sm is None:
            return Swoosh.tint
        if tint is None:
            tint = sm.get_tint()
        return sm.make_custom(*tint, Swoosh.value,
                              saturation_scale=Swoosh.saturation)[:3]

    @staticmethod
    def _saturated_rgb(tint=None):
        """Super-bright version of a view's tint, used for the nested-view
        highlight (swoosh + outline boxes). Pass the tint stashed on the
        draw_state at draw time (draw_state.current_tint): by this post-draw
        pass the style manager no longer holds it. Falls back to the live tint,
        then to a static tint, when nothing was stashed."""
        sm = Melty.style_manager
        if sm is None:
            return Swoosh.tint
        if tint is None:
            tint = sm.get_tint()
        return sm.make_custom(*tint,
                              saturation_scale=1.0, value=0.6)[:3]
    @staticmethod
    def _lerp_rgb(a, b, t):
        """Plain RGB lerp for the parent->child connector gradient."""
        return (a[0] + (b[0] - a[0]) * t,
                a[1] + (b[1] - a[1]) * t,
                a[2] + (b[2] - a[2]) * t)

    @staticmethod
    def _point_rect_distance(mx, my, x0, y0, x1, y1):
        """Distance from point (mx, my) to the axis-aligned rect
        [x0, x1] x [y0, y1]. Zero when the point is inside; otherwise the
        Euclidean distance to the nearest edge/corner. The per-axis overshoot
        is max(low - p, 0, p - high), which is 0 while the point straddles the
        span and grows once it falls outside, so corners get the diagonal."""
        dx = max(x0 - mx, 0.0, mx - x1)
        dy = max(y0 - my, 0.0, my - y1)
        return math.hypot(dx, dy)

    @staticmethod
    def _mouse_fade(parent_rect, child_rect):
        """Opacity multiplier for the swoosh based on how close the mouse is to
        the views it joins. Each END fades on its OWN distance scale
        (Swoosh.mouse_falloff_dist_parent / _child) so the parent end can drop
        off sooner than the child end; the connector takes whichever side is
        brighter (max), then eases to Swoosh.mouse_falloff_floor far from both.
        Swoosh.mouse_falloff_exp shapes the curve (>1 keeps it bright near the
        rect then drops off). Returns 1.0 when the feature is disabled."""
        if not Swoosh.mouse_falloff:
            return 1.0
        mx, my = imgui.get_mouse_pos()
        exp = max(Swoosh.mouse_falloff_exp, 0.0)

        def side_factor(rect, scale):
            if scale <= 0.0:
                return 0.0                          # this side never lights the connector
            dist = Melty._point_rect_distance(mx, my, *rect)
            t = min(max(dist / scale, 0.0), 1.0)    # 0 on the rect -> 1 past `scale`
            return (1.0 - t) ** exp                 # 1 near -> 0 far

        factor = max(side_factor(parent_rect, Swoosh.mouse_falloff_dist_parent),
                     side_factor(child_rect, Swoosh.mouse_falloff_dist_child))
        floor = Swoosh.mouse_falloff_floor
        return floor + (1.0 - floor) * factor

    @staticmethod
    def _swoosh_drag_focus(draw_state, offset_ds, dragging_tiles, hovered):
        """Drag-focus opacity decision for one connector (Swoosh.drag_focus).

        Returns True for Swoosh.drag_alpha, False for Swoosh.rest_alpha — the
        same two values `hover` already means to _draw_swoosh, so the distance
        fade is bypassed entirely and the default is the rest alpha. A
        connector lights up when:
          * its CHILD window is the one being dragged/resized (that window's
            connector only), or
          * any window ENCLOSING the parent view is being dragged — dragging a
            parent lights every connector hanging off it, and the walk covers
            nested windows moving with an ancestor, or
          * `hovered` is truthy — the PARENT view is hovered (the spawner half
            of the hover pass, hovered_spawner_ids). Hovering the child window
            itself deliberately does NOT light it; only dragging it does.
        """
        if str(draw_state._tile_id) in dragging_tiles:
            return True
        walk = offset_ds
        depth = 0
        while walk is not None and depth < 64:   # cheap guard against a cycle
            if str(walk._tile_id) in dragging_tiles:
                return True
            walk = walk.parent_window
            depth += 1
        return bool(hovered)

    @staticmethod
    def _draw_ribbon(overlay_dl, px0, py0, px1, py1, nx0, ny0, nx1, ny1,
                     rgb, rgb2=None, p_round=0.0, n_round=0.0, mouse_fade=1.0):
        """Thick-ribbon connector: instead of the thin tapered line, bridge
        the two views' facing edges with a full band. Each end of the band
        sits on the straight (un-rounded) portion of its view's facing edge,
        centered on the shared overlap span, and is sized from its OWN edge
        length (Swoosh.ribbon_coverage of it, capped by ribbon_max_width) —
        so a small child hanging off a big parent gets a funnel, wide at the
        parent and narrow at the child. When the views are offset the two
        bands land at different positions and the band's boundary curves
        s-curve between them (cubics with tangents perpendicular to the
        edges — the classic node-link shape). Returns True when drawn; False
        when the rects overlap (no facing gap to bridge) or an edge is all
        corner, so the caller falls back to the thin line. Tunables:
        Swoosh.ribbon_*."""
        gap_r, gap_l = nx0 - px1, px0 - nx1
        gap_b, gap_t = ny0 - py1, py0 - ny1
        gx, gy = max(gap_r, gap_l), max(gap_b, gap_t)
        if gx <= 0.0 and gy <= 0.0:
            return False

        # Bridge along the axis with the most gap, weighted by ribbon_axis_bias:
        # 0.5 is the neutral `gx >= gy` choice; sliding toward 1 weights the
        # horizontal (left/right) gap so the band prefers the sides; toward 0
        # weights the vertical (top/bottom) gap. The weighting keeps the choice
        # monotonic in the bias while still respecting overlap - an axis with
        # no facing gap is a negative term, so an extreme bias can't force a
        # bridge where there's nothing to bridge; it falls back to the axis that
        # actually has a gap.
        bias = Swoosh.ribbon_axis_bias
        # ep/ec are the two facing edge coordinates on the chosen axis; lo/hi
        # bound the straight portion of each facing edge (inset by its corner
        # radius) on the cross axis.
        if gx * bias >= gy * (1.0 - bias):
            ep, ec = (px1, nx0) if gap_r >= gap_l else (px0, nx1)
            p_lo, p_hi = py0 + p_round, py1 - p_round
            c_lo, c_hi = ny0 + n_round, ny1 - n_round
            shared = (max(py0, ny0) + min(py1, ny1)) * 0.5
            pt = lambda along, across: (along, across)
        else:
            ep, ec = (py1, ny0) if gap_b >= gap_t else (py0, ny1)
            p_lo, p_hi = px0 + p_round, px1 - p_round
            c_lo, c_hi = nx0 + n_round, nx1 - n_round
            shared = (max(px0, nx0) + min(px1, nx1)) * 0.5
            pt = lambda along, across: (across, along)

        # Per-end half-widths: each end is sized from its own edge, clamped to
        # that edge's full straight span (coverage >= 1 spans the whole edge)
        # and optionally capped in px. Mismatched views make a funnel.
        def end_hw(lo, hi):
            w = (hi - lo) * Swoosh.ribbon_coverage
            if Swoosh.ribbon_max_width > 0.0:
                w = min(w, Swoosh.ribbon_max_width)
            return min(w, hi - lo) * 0.5
        p_hw = end_hw(p_lo, p_hi)
        c_hw = end_hw(c_lo, c_hi)
        if p_hw < 1.0 or c_hw < 1.0:
            return False
        # Center each band on the shared-span center, clamped into its own
        # straight edge: aligned views get a straight band; offset views get
        # bands at different positions with the curves bridge them.
        pc = min(max(shared, p_lo + p_hw), p_hi - p_hw)
        cc = min(max(shared, c_lo + c_hw), c_hi - c_hw)

        segments = max(2, int(Swoosh.segments))
        out = -1.0 if ec < ep else 1.0   # bridge direction out of the parent
        raw = []
        for sign in (-1.0, 1.0):
            a0, a3 = pc + sign * p_hw, cc + sign * c_hw
            # Tangent reach of this boundary cubic, perpendicular to the edges
            # at both ends. The distance that matters is the ALONG-axis gap -
            # a left-to-right ribbon curves with its x distance, a vertical
            # one with its y distance. A side's across-axis travel (the offset
            # it has to swerve) counts too, but with the much smaller
            # ribbon_curve_across weight, so the two sides of a funnel still
            # differ: the side travelling further bows a little deeper.
            # Signed so the tangents always point out of their view.
            reach = out * (Swoosh.ribbon_curve * abs(ec - ep)
                           + Swoosh.ribbon_curve_across * abs(a3 - a0))
            pts = []
            for i in range(segments + 1):
                t = i / segments
                u = 1.0 - t
                along = (u * u * u * ep + 3 * u * u * t * (ep + reach)
                         + 3 * u * t * t * (ec - reach) + t * t * t * ec)
                across = (u * u * u * a0 + 3 * u * u * t * a0
                          + 3 * u * t * t * a3 + t * t * t * a3)
                pts.append((along, across))
            raw.append(pts)

        # One-direction bow (bend-strip model): rather than the symmetric S,
        # shift both edges the same way so the band reads as one arc - bent
        # rubber keeps its width, it doesn't pinch. The direction is picked
        # from the swerve itself: the bulge leads "out" toward the child's
        # side (a negative ribbon_bow flips it "in" - hug the parent's level,
        # then dive late). The amount is what the proportions tell you: a band
        # much wider than it is long can't physically s-bend all that distance,
        # so the bow scales with width/length (saturating at 1) and with the
        # offset it has to absorb, while long thin ribbons keep the pure S.
        # The bow is zero at the ends, so they stay pinned to the views.
        bow = Swoosh.ribbon_bow
        offset = cc - pc
        length = abs(ec - ep)
        if bow != 0.0 and abs(offset) > 1e-6 and length > 1e-6:
            aspect = min(1.0, (p_hw + c_hw) / length)
            amp = bow * offset * aspect   # signed: bulges with the swerve
            shape = max(Swoosh.ribbon_bow_shape, 0.05)
            sa, sb = raw
            for i in range(1, segments):
                t = i / segments
                push = amp * (4.0 * t * (1.0 - t)) ** shape
                sa[i] = (sa[i][0], sa[i][1] + push)
                sb[i] = (sb[i][0], sb[i][1] + push)
        sides = [[pt(al, ac) for al, ac in side] for side in raw]

        # Fill between the two boundary polylines; the band isn't convex, so
        # fill segment quads as triangle pairs. Per-triangle antialiasing is
        # deliberately OFF for the fill: AA feathers a fringe around every
        # triangle, and on the shared interior edges the overlapping fringes
        # over-blend into visible seams - a wireframe across the translucent
        # band. Without AA adjacent triangles rasterize watertight (identical
        # shared vertices), and the band's outer edges are feathered by the
        # boundary strokes below instead.
        #
        # The band's opacity is a function of its AREA: total ink is what
        # overwhelms, and area is a property of the whole ribbon, so one fade
        # factor for the band - fill and edge strokes alike. Once the
        # band's area exceeds ribbon_fade_size² (a fade_size × fade_size
        # square) the factor scales inversely with area - constant total ink -
        # so a huge sail washes out to a faint outline while a small tab
        # keeps full ribbon_alpha / ribbon_edge_alpha.
        a, b = sides
        fade_f = 1.0
        fade = Swoosh.ribbon_fade_size
        if fade > 0.0:
            # Shoelace area of the band polygon (side a forward, side b back).
            poly = a + b[::-1]
            n = len(poly)
            area2 = 0.0
            for i in range(n):
                x0, y0 = poly[i]
                x1, y1 = poly[(i + 1) % n]
                area2 += x0 * y1 - x1 * y0
            area = abs(area2) * 0.5
            ref = fade * fade
            if area > ref:
                fade_f = ref / area
                
        # [tint=(0.72, 0.11, 0.11), show_tint=True]
        alpha = Swoosh.ribbon_alpha * fade_f * mouse_fade
        # Gradient: `sides` runs parent (i=0) -> child (i=segments), so the
        # fill (and the boundary strokes below) lerp from the parent color to
        # the child color along the bridge. rgb2 None/equal means single color.
        grad = rgb2 is not None and tuple(rgb2[:3]) != tuple(rgb[:3])
        if rgb2 is None:
            rgb2 = rgb
        fill = pack_color(*rgb, alpha)
        dl_flags = overlay_dl.flags
        overlay_dl.flags = dl_flags & ~imgui.DRAW_LIST_ANTI_ALIASED_FILL
        try:
            for i in range(segments):
                if grad:
                    fill = pack_color(
                        *Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments),
                        alpha)
                overlay_dl.add_triangle_filled(a[i][0], a[i][1], b[i][0], b[i][1],
                                               a[i + 1][0], a[i + 1][1], fill)
                overlay_dl.add_triangle_filled(b[i][0], b[i][1], b[i + 1][0], b[i + 1][1],
                                               a[i + 1][0], a[i + 1][1], fill)
        finally:
            overlay_dl.flags = dl_flags

        # Stroke the boundary curves (add_polyline is antialiased) for
        # definition and to soften the hard triangle edges. The band's ends
        # sit flush against the view edges, so no caps are needed. Each
        # stroke fades with its OWN arc length (not the band's area): past
        # ribbon_edge_fade_length px the alpha scales inversely with the
        # length - constant ink along the wire - so a long sweeping edge
        # thins out while a short hop stays crisp. The two sides fade
        # independently - the short side of a funnel keeps its definition
        # next to its long faded partner.
        if Swoosh.ribbon_edge_thickness > 0.0:
            fade_len = Swoosh.ribbon_edge_fade_length
            side_alpha = []
            for pts in (a, b):
                e_alpha = Swoosh.ribbon_edge_alpha
                if fade_len > 0.0:
                    ln = 0.0
                    for i in range(len(pts) - 1):
                        ln += math.hypot(pts[i + 1][0] - pts[i][0],
                                         pts[i + 1][1] - pts[i][1])
                    if ln > fade_len:
                        e_alpha = Swoosh.ribbon_edge_alpha * fade_len / ln
                side_alpha.append(e_alpha * mouse_fade)
            if grad:
                # Per-segment strokes carry the same gradient as the fill (a
                # polyline is one color; consecutive segments share their
                # endpoints, so the joints are tight).
                for i in range(segments):
                    seg_rgb = Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments)
                    for pts, e_alpha in ((a, side_alpha[0]), (b, side_alpha[1])):
                        edge = pack_color(*seg_rgb, e_alpha)
                        overlay_dl.add_polyline([pts[i], pts[i + 1]], edge,
                                                flags=imgui.DRAW_NONE,
                                                thickness=Swoosh.ribbon_edge_thickness)
            else:
                for pts, e_alpha in ((a, side_alpha[0]), (b, side_alpha[1])):
                    edge = pack_color(*rgb, e_alpha)
                    overlay_dl.add_polyline(pts, edge, flags=imgui.DRAW_NONE,
                                            thickness=Swoosh.ribbon_edge_thickness)
        return True

    @staticmethod
    def _draw_swoosh(overlay_dl, px, py, pw, ph, nx, ny, nw, nh, rgb,
                     rgb2=None, p_round=0.0, n_round=0.0, p_clip=None,
                     mode=None, hover=None):
        """Draw a curved connector from the parent view's outline to the nested
        view. The line is thick at both endpoints and tapers thin in the middle.
        `rgb` is the resolved highlight color (see _highlight_rgb); `rgb2`,
        when given, is the CHILD end's color — fill, edge strokes and end caps
        all blend from `rgb` at the parent end to `rgb2` at the child end (in
        both line and ribbon modes). p_round /
        n_round are the parent/nested corner radii so the ends meet the rounded
        edge. p_clip, if given, is the parent's absolute clip rect
        (left, top, right, bottom): the parent end is anchored against the
        *visible* (clipped) part of the parent rect so the cap dot never lands
        on a region that's been scrolled/clipped away. `mode` is a SwooshMode
        (or its string value) selecting the connector style per window — the
        swoosh_mode window kwarg lands here; None follows the global
        Swoosh.ribbon toggle. `hover` is the nested-window hover override:
        None means no nested window is hovered anywhere (keep the default
        opacity behavior); True means THIS connector's window is the hovered
        one (Swoosh.drag_alpha); False means some OTHER window is hovered
        (drop to Swoosh.rest_alpha). Under Swoosh.drag_focus the caller
        resolves the same True/False from the live drag gesture instead of
        hover alone (see _swoosh_drag_focus), so it is never None there and
        the proximity fade never runs. Tunables live on Swoosh.*."""
        if mode is None:
            mode = SwooshMode.RIBBON if Swoosh.ribbon else SwooshMode.LINE
        elif isinstance(mode, str):
            mode = SwooshMode(mode)
        # Clamp the parent rect to its visible region so the connector anchors on
        # what's actually on screen rather than a clipped-off edge. When the
        # parent is scrolled/clipped completely out of view there is no visible
        # edge to anchor to - draw nothing rather than tether to a phantom rect.
        if p_clip is not None:
            cl, ct, cr, cb = p_clip
            vx0, vy0 = max(px, cl), max(py, ct)
            vx1, vy1 = min(px + pw, cr), min(py + ph, cb)
            if vx1 <= vx0 or vy1 <= vy0:
                return
            px, py, pw, ph = vx0, vy0, vx1 - vx0, vy1 - vy0

        # Real (un-grown) view rects: the endpoints must land on these.
        rpx0, rpy0, rpx1, rpy1 = px, py, px + pw, py + ph
        rnx0, rny0, rnx1, rny1 = nx, ny, nx + nw, ny + nh

        # The hover override (see docstring) beats the proximity fade entirely.
        # Otherwise the mouse-proximity fade applies to RIBBON mode only: dim the whole band by
        # how close the cursor is to each connected view, parent and child fading
        # on their own distance scales (see _mouse_fade). When it fully fades out
        # there is nothing to draw, so skip the geometry entirely. LINE mode uses a
        # static opacity (Swoosh.alpha) anyway, so it computes no fade - this is
        # local to the selected mode, so a ribbon that falls through to the line on
        # overlapping views keeps the live fade.
        if hover is not None:
            # A nested window is hovered/dragged: its own connector reads at
            # Swoosh.drag_alpha and every other connector drops to
            # Swoosh.rest_alpha, in either order - hover names one connector, so
            # distance no longer gets a vote.
            mouse_fade = Swoosh.drag_alpha if hover else Swoosh.rest_alpha
            if mouse_fade <= 0.0:
                return
        elif mode is SwooshMode.RIBBON:
            mouse_fade = Melty._mouse_fade(
                (rpx0, rpy0, rpx1, rpy1), (rnx0, rny0, rnx1, rny1))
            if mouse_fade <= 0.0:
                return
        else:
            mouse_fade = 1.0

        # Ribbon mode: a full band between the view edges replaces the thin
        # line whenever the views have a space to bridge; overlapping views fall
        # through to the line, which knows how to route around the intersection.
        if mode is SwooshMode.RIBBON and Melty._draw_ribbon(
                overlay_dl, rpx0, rpy0, rpx1, rpy1,
                rnx0, rny0, rnx1, rny1, rgb, rgb2=rgb2,
                p_round=p_round, n_round=n_round, mouse_fade=mouse_fade):
            return

        # The overlap transition is computed on the grown rects so it begins
        # as the views approach, before they actually touch (Swoosh.overlap_padding).
        # Only the math uses the grown rects; the endpoints are pulled back onto
        # the real view edges below.
        pad = Swoosh.overlap_padding
        gpx0, gpy0, gpx1, gpy1 = rpx0 - pad, rpy0 - pad, rpx1 + pad, rpy1 + pad
        gnx0, gny0, gnx1, gny1 = rnx0 - pad, rny0 - pad, rnx1 + pad, rny1 + pad

        # Anchor both ends at the center of the rects' shared edge (smoothed),
        # so the connector stays centered and glides as the rects move. When the
        # rects overlap, the ends slide out of the intersection and `ctrl` bows
        # the curve through it (see _closest_perimeter_points).
        geom = Melty._closest_perimeter_points(
            gpx0, gpy0, gpx1, gpy1,
            gnx0, gny0, gnx1, gny1,
        )
        if geom is None:
            # Parent fully hidden under the child: no connector to draw.
            return
        x0, y0, x1, y1, ctrl, ctrl_g = geom

        # Project each endpoint onto the real view's (rounded) perimeter: this
        # pulls it off the grown rect to the real edge and, since both
        # coordinates were interpolated, lets it slide along the edge and around
        # the rounded corners. The control point stays out in the exterior so the
        # bow is preserved.
        x0, y0 = Melty._project_to_rounded_rect(x0, y0, rpx0, rpy0, rpx1, rpy1, p_round)
        x1, y1 = Melty._project_to_rounded_rect(x1, y1, rnx0, rny0, rnx1, rny1, n_round)

        seg_dx, seg_dy = x1 - x0, y1 - y0
        seg_len = math.hypot(seg_dx, seg_dy)
        if seg_len < 1.0:
            return

        # Ramp the bow in with the connector's slope rather than turning it on:
        # the ratio of the shorter axis span to the longer one is 0 when the line
        # is axis-aligned and 1 at 45 degrees, so level runs stay straight and
        # the curve grows smoothly as the line tilts toward diagonal.
        adx, ady = abs(seg_dx), abs(seg_dy)
        slope_ratio = min(adx, ady) / max(adx, ady) if max(adx, ady) > 1e-6 else 0.0
        curve_factor = slope_ratio ** Swoosh.curve_ramp

        # Quadratic bezier control point: bow the curve perpendicular to the
        # chord by an amount scaled by curve_factor. If the ends were slid out
        # of an overlap, _closest_perimeter_points also hands back a control
        # point that bows the curve through the exterior notch; bias toward it
        # by ctrl_g (the overlap-mode weight) so the bow eases in with the slide.
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        perp_x, perp_y = -seg_dy / seg_len, seg_dx / seg_len
        bow = seg_len * Swoosh.curve * curve_factor
        cxp, cyp = mx + perp_x * bow, my + perp_y * bow
        if ctrl is not None:
            # The exterior route's bow also scales with Swoosh.curve (measured
            # from the chord midpoint, designed full at curve≈0.22) so lowering
            # curve flattens it and curve=0 gives a straight connector.
            cg = Swoosh.curve / 0.22
            ncx = mx + (ctrl[0] - mx) * cg
            ncy = my + (ctrl[1] - my) * cg
            cxp = cxp + (ncx - cxp) * ctrl_g
            cyp = cyp + (ncy - cyp) * ctrl_g

        grad = rgb2 is not None and tuple(rgb2[:3]) != tuple(rgb[:3])
        if rgb2 is None:
            rgb2 = rgb
        alpha = Swoosh.alpha * mouse_fade
        col = pack_color(*rgb, alpha)
        segments = max(2, int(Swoosh.segments))
        end_hw = Swoosh.end_thickness
        mid_hw = Swoosh.mid_thickness
        taper = Swoosh.taper

        def bezier(t):
            u = 1.0 - t
            bx = u * u * x0 + 2 * u * t * cxp + t * t * x1
            by = u * u * y0 + 2 * u * t * cyp + t * t * y1
            # derivative for tangent direction
            tx = 2 * u * (cxp - x0) + 2 * t * (x1 - cxp)
            ty = 2 * u * (cyp - y0) + 2 * t * (y1 - cyp)
            return bx, by, tx, ty

        def half_width(t):
            # (2t-1)^taper is 1 at the ends, 0 at the center.
            edge = abs(2.0 * t - 1.0) ** taper
            return mid_hw + (end_hw - mid_hw) * edge

        # Build the two offset edges of the ribbon, then fill it segment by
        # segment (the shape isn't convex, we fill quads as triangle pairs).
        left = []
        right = []
        for i in range(segments + 1):
            t = i / segments
            bx, by, tx, ty = bezier(t)
            tlen = math.hypot(tx, ty)
            if tlen < 1e-6:
                nxn, nyn = perp_x, perp_y
            else:
                nxn, nyn = -ty / tlen, tx / tlen
            hw = half_width(t)
            left.append((bx + nxn * hw, by + nyn * hw))
            right.append((bx - nxn * hw, by - nyn * hw))

        for i in range(segments):
            if grad:
                # Remember t=0 is the parent end, t=1 the child end, so the gradient
                # blends parent color -> child color along its length.
                col = pack_color(
                    *Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments),
                    alpha)
            l0, l1 = left[i], left[i + 1]
            r0, r1 = right[i], right[i + 1]
            overlay_dl.add_triangle_filled(l0[0], l0[1], r0[0], r0[1], l1[0], l1[1], col)
            overlay_dl.add_triangle_filled(r0[0], r0[1], r1[0], r1[1], l1[0], l1[1], col)

        # add_triangle_filled has hard (aliased) edges, but add_polyline is
        # antialiased (DRAW_LIST_ANTI_ALIASED_LINES, on by default). Stroke the
        # ribbon's two long edges to feather them; the square ends are covered by
        # the AA cap circles below. Gradient mode strokes per segment (one
        # color per polyline; shared endpoints keep the joints tight).
        if Swoosh.aa_width > 0.0:
            if grad:
                for i in range(segments):
                    seg_col = pack_color(
                        *Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments),
                        alpha)
                    overlay_dl.add_polyline([left[i], left[i + 1]], seg_col,
                                            flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
                    overlay_dl.add_polyline([right[i], right[i + 1]], seg_col,
                                            flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
            else:
                overlay_dl.add_polyline(left, col, flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
                overlay_dl.add_polyline(right, col, flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)

        # Round caps over the flat (square) ends of the ribbon so the endpoints
        # read as dots rather than chopped-off edges - each in its own endcap color.
        cap_r = end_hw * Swoosh.cap_scale
        overlay_dl.add_circle_filled(x0, y0, cap_r,
                                     pack_color(*rgb, alpha))
        overlay_dl.add_circle_filled(x1, y1, cap_r,
                                     pack_color(*rgb2, alpha))

    @classmethod
    def is_wrapped(cls):
        if len(cls.wrap_stack) == 0:        
            return False
        else:
            return cls.wrap_stack[-1]

    @classmethod
    def draw(cls, draw_state, cursor_pos=None, detached=False):


        if draw_state is None:
            return

        if draw_state.closed:
            return

        # Melty.depth = 0
        Melty.bg_depth = draw_state._bg_depth

        original_bg_stack = copy(Melty.bg_stack)
        if draw_state._bg_stack is not None:
            from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
            if DragDrop.active and draw_state is DragDrop.item_ds:
                # A dragged item is the one window that flips inline ->
                # window mid-life. Its contents lay out with
                # content_margin = len(bg_stack) * 2 (absolute depth), so the
                # tint-tail truncation below would change every level's
                # content_width from how it looks inline. Restore the FULL
                # spawn snapshot: depths (margins) match inline exactly, and
                # color reads are tail-relative (get_bg_color(-1/-2)) so they
                # see the same entries either way.
                Melty.bg_stack = list(draw_state._bg_stack)
            elif len(draw_state._bg_stack) > 1:
                Melty.bg_stack = draw_state._bg_stack[-2:]
            else:
                Melty.bg_stack = [draw_state._bg_stack[-1]]


        view_func = draw_state._wrapper
        input_value = draw_state._raw_input_value
        kwargs = draw_state._kwargs
        # draw_state._kwargs is last render's FULLY-RESOLVED kwargs, so it still
        # carries a concrete value for every PER-FRAME param. Replaying it as-is
        # is wrong for two reasons:
        #   1. Auto-state params (the diverged set in auto_params) appear in the
        #      wrapper's explicit_param_keys and freeze at the stale snapshot - a
        #      voxel camera drag's spin/tilt/zoom never accumulate (snap back).
        #   2. Event params still hold last frame's InputEvent; once the gesture
        #      ends there's no new event to overwrite it, so the wrapper keeps
        #      reapplying the same drag forever (runaway spin).
        # Drop both so the wrapper re-resolves them: auto-state from the live
        # auto_params, events from this frame's Melty.events (if any). Genuine
        # static caller overrides never diverge into auto_params and are never
        # InputEvents, so they stay put.
        from src.lsd.gl_gui.events.input_handler import InputEvent as _InputEvent
        _ap = draw_state.__dict__.get('auto_params')
        for _k in list(kwargs):
            if (_ap and _k in _ap) or isinstance(kwargs[_k], _InputEvent):
                kwargs.pop(_k, None)
        # Live-view comment re-splat: last spawn kwargs froze the MARKER's
        # last splat of the site's `# [...]` override comment, and the marker
        # specifically doesn't re-render on a comment save (self-write
        # absorb) - so a set_anywhere comment edit made between marker
        # renders would never reach a replayed window (and a stale
        # auto_params entry for the same param would drive instead). The
        # marker stamps live_root (the live parse tree; updated IN PLACE by
        # comment writes) + live_key on the window ds - re-read the entry
        # here so every replay carries current comment values as explicit
        # kwargs, exactly like a fresh wrapper call.
        _lr = draw_state.__dict__.get('live_root')
        _lk = draw_state.__dict__.get('live_key')
        if isinstance(_lr, dict) and draw_state.__dict__.get('_lv_locator') is not None:
            # The stamp is only as fresh as the marker's last render; a
            # re-save since replaced the tree. Resolve the owner against the
            # editor's CURRENT tree (memoized per tree on the ds) so the
            # replay splats the values the save will read, not the orphan's.
            from src.lsd.gl_gui.view.core_views.live_view_views import (
                current_live_root)
            _lr = current_live_root(draw_state)
        if isinstance(_lr, dict) and _lk:
            _ca = _lr.get("__overrides__", {}).get(f"__{_lk}__")
            if isinstance(_ca, dict):
                for _ck, _cv in _ca.items():
                    if not (isinstance(_ck, str) and _ck.startswith("__")):
                        if _ck == "dim_names":
                            # A loop site's accumulated value carries auto
                            # loop dims (stamped on the ds by the marker);
                            # the comment names only the per-iteration dims.
                            # Redo the marker's merge + <...> padding -
                            # splatting the raw comment list here would
                            # clobber them.
                            _ad = draw_state.__dict__.get('_lv_auto_dims')
                            _nd = draw_state.__dict__.get('_lv_ndim')
                            if _ad or _nd:
                                from src.lsd.gl_gui.view.core_views.live_view_views import (
                                    _merged_dim_names, _padded_dim_names)
                                if _ad:
                                    _cv = _merged_dim_names(_ad, _cv)
                                _cv = _padded_dim_names(_cv, _nd or 0) or _cv
                        kwargs[_ck] = _cv
        # Live-value re-read: a live value window replays from kwargs the
        # MARKER last passed; while the marker is culled (scrolled off) a
        # rerun's return invalidates this window but would redraw the OLD
        # tensor - stale on screen and pinning a whole superseded
        # generation. Read the current store value by the key the marker
        # stamped back into the stored kwargs instead, so the previous tensor
        # is released right here.
        _lso = draw_state.__dict__.get('_lv_store_obj')
        _lkp = draw_state.__dict__.get('_lv_key_path')
        if _lso is not None and _lkp is not None:
            try:
                _lstore = getattr(_lso, '__live_values__', None)
                if _lstore and _lkp in _lstore:
                    _lv = _lstore[_lkp]
                    if _lv is not kwargs.get('input_value'):
                        from src.lsd.gl_gui.view.core_views.live_view_views import (
                            _stacked_list_value)
                        kwargs['input_value'] = _stacked_list_value(_lv, draw_state)
            except Exception:
                pass
        kwargs['layer_unique'] = draw_state.unique
        imgui.set_cursor_screen_pos((int(draw_state.abs_left), int(draw_state.abs_top)))

        if Toggles.debug_z_depth:
            draw_list = imgui.get_overlay_draw_list()
            draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 40, pack_color(1, 0, 0, 1),
                               f"Layer {draw_state.layer} "
                               f"Depth {draw_state.depth} "
                               f"zpos {draw_state.z_pos} "
                               f"depth_and_layer {draw_state.depth_and_layer} "
                               f"Melty.active_layer {cls.active_layer} "
                               f"Melty.z_pos {cls.z_pos} "
                               f"Melty.depth {cls.depth}")

            draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 20, pack_color(1, 1, 0, 1),
                               f"kwargs['active_layer'] {kwargs['active_layer']} "
                               )

        kwargs['input_value'] = input_value
        kwargs['detached'] = detached
        return_val = view_func(**kwargs)
        if return_val is not None:
            cls.pending_return_values[draw_state._tile_id] = return_val
            if return_val[0]:
                note=Note(name="delayed return", reason=f"{draw_state.name}", tint=(0, 1, 1))
                if draw_state.parent_window is not None:
                    Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=6, frame_delta=1, note=note)
                else:
                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=6, force=True, frame_delta=1, note=note)

                request_render()
        else:
            print(f"{draw_state.name}")

        Melty.bg_stack = original_bg_stack

    # cls.cache.remove_parent()

    @classmethod
    def init_input_backend(cls, window):
        """Swap to the GLFW-callback input backend (event-queued, frame-rate
        independent). Call once after the imgui GlfwRenderer is created so our
        callbacks chain onto (and preserve) imgui's."""
        try:
            cls.backend = GlfwQueueBackend(cls.event_handler, window)
        except Exception as e:
            print(f"GlfwQueueBackend unavailable, keeping ImGuiBackend: {e}")
        # 3D mouse: the spacenavd socket reader (events/space_mouse.py), a
        # process-lifetime thread; its per-frame pump runs beside the
        # backend's below. A view subscribes with `space_mouse_changed=None`.
        try:
            from src.lsd.gl_gui.events.space_mouse import start as start_space_mouse
            start_space_mouse()
        except Exception as e:
            print(f"Space mouse unavailable: {e}")
        # OS-level 3-finger click/drag (events/touchpad_backend.py) - DISABLED.
        # The TM3414's contact sensing proved unreliable for 3-finger detection
        # (reports 1-2 flickering contacts for 3 pressed fingers in most
        # sessions); re-enable by uncommenting when that's resolved.
        # try:
        #     from src.lsd.gl_gui.events.touchpad_backend import start_three_finger_drag
        #     start_three_finger_drag()
        # except Exception as e:
        #     print(f"Touchpad 3-finger drag unavailable: {e}")

    @classmethod
    def apply_refresh_nested_windows(cls, nested_window_refresh=None):
        if nested_window_refresh is None:
            parent_window = cls.nested_window_refresh
            cls.nested_window_refresh = None

        else:
            parent_window = nested_window_refresh

        if parent_window is None:
            return

        to_discard = set()

        parent_ds_id = parent_window.id
        ds_list = cls.root_draw_states.get(parent_ds_id, [])
        for idx, ds in enumerate(ds_list):
            to_discard.add((parent_ds_id, ds))
            if ds.closable:
                cls.apply_refresh_nested_windows(ds)

        for ds_id, discard_ds in to_discard:
            # release_window_tree on an earlier discard pops the whole
            # nested list of a closed parent, so a closed child also queued
            # here may already be gone - tolerate it.
            siblings = cls.root_draw_states.get(ds_id)
            if siblings is not None and discard_ds in siblings:
                siblings.remove(discard_ds)
            # A discarded nested window keeps its draw_state (the framework
            # contract) - and with it every wrapper slot holding the value
            # it last rendered, plus its GLState textures. In the live lab
            # that is a multi-GB tensor per window: switching editor tabs
            # discarded the old tab's value windows and pinned their
            # stacks for the session. Release the value refs now; a window
            # the parent re-registers on its next frame simply refills them.
            cls.release_window_tree(discard_ds)

        note = Note(name="refresh_nested_windows", reason="refresh_nested_windows", tint=(1, 0, 1))
        Melty.cache.invalidate_up(parent_window._tile_id,
                                  max_depth=10, force=True, note=note)


    @classmethod
    def release_window_tree(cls, ds, unregister_nested=True):
        """Lifecycle release for a window that is going away (deleted root,
        X-closed or discarded nested window, session teardown): drop every
        wrapper-owned reference to the value it last rendered (core_render.
        release_input_refs — the input slots + offscreen/blit caches) on the
        ds and its descendants, do the same for every nested window
        registered UNDER it in root_draw_states (recursively — a deleted
        editor window takes its live value windows and their satellites
        with it), and queue its GL resources for deletion. Draw_states
        persist (framework contract): a window that comes back simply
        refills its slots on its next render. With `unregister_nested` the
        nested entries are also removed from root_draw_states — a deleted
        root's nested windows would otherwise linger orphaned. Never
        raises; idempotent."""
        if ds is None:
            return
        try:
            from src.lsd.gl_gui.view.core_views.core_render import release_input_refs
            release_input_refs(ds)
            for d in ds.descendants(max_depth=8):
                release_input_refs(d)
        except Exception:
            pass
        nested = cls.root_draw_states.get(ds.id)
        if nested:
            for child in list(nested):
                if child is not ds:
                    cls.release_window_tree(child, unregister_nested)
            if unregister_nested:
                cls.root_draw_states.pop(ds.id, None)
        # Hit-test boxes go with the window: a released window no longer
        # renders, so nothing else would ever sync its (and its content's)
        # boxes out - and a DISCARDED one isn't closed, so bvh_query's lazy
        # evict never fires for it either. See bvh_evict_window.
        try:
            cls.bvh_evict_window(ds)
        except Exception:
            pass
        try:
            from src.lsd.gl_gui.gl_state import GLState
            GLState.on_window_deleted(ds)
        except Exception:
            pass
        try:
            from src.lsd.gl_gui.fim import FimState
            FimState.on_window_deleted(ds)
        except Exception:
            pass

    @classmethod
    def refresh_nested_windows(cls, draw_state):
        parent_window = draw_state.parent_window if draw_state.parent_window is not None else \
        Melty.melty_window_stack[
            -1] if len(Melty.melty_window_stack) > 0 else draw_state
        cls.nested_window_refresh = parent_window

    @classmethod
    def post_to_render(cls, fn):
        """Queue `fn` to run on the render thread between frames (drained at
        end_frame). Safe from any thread; wakes the loop so an idle app runs
        it promptly. For work that must not race a frame in progress — e.g.
        mutating a live view-model tree that frame walkers iterate."""
        with cls._render_tasks_lock:
            cls._render_tasks.append(fn)
        request_render()

    @classmethod
    def _drain_render_tasks(cls):
        if not cls._render_tasks:
            return
        with cls._render_tasks_lock:
            tasks, cls._render_tasks = cls._render_tasks, []
        for fn in tasks:
            try:
                fn()
            except Exception as e:
                print(f"[melty] render task failed: {e}")
                print_stack_trace(exception=e)

    @classmethod
    def _spawner_fully_clipped(cls, ds, _depth=0):
        """True when the view this nested window was spawned from (the same
        anchor the swoosh tethers to) is completely scrolled/clipped out of
        view — or when the window's own parent window is hidden for that
        reason, so chains of nested windows hide together.

        Deliberately a LIVE-geometry test, not a BVH lookup or a "did the
        parent render this frame" test: BVH boxes only catch up when a view
        re-renders, so they are stale during the very scroll that pushes the
        parent away — and a blit-cached parent skips its render while being
        perfectly visible, so render-recency can't distinguish "offscreen"
        from "cached". abs_left/abs_top (and so abs_clip_rect) are computed
        live off the persistent draw_state — ancestor scroll included —
        regardless of how (or whether) the parent was drawn this frame, so
        an empty visible rect means exactly "the spawner is out of sight".

        `hide_offscreen=False` skips the window's OWN spawner test (the parent
        -window chain below still applies): pinned windows whose anchor is a
        tiny inline token — live-view value windows — would hide the moment
        that token scrolled past the viewport edge. They rely on the pinned
        -branch clamp in DrawState._pinned_base_y instead: the window rides the
        token up, stops with its bottom at its window's top, stays reachable."""
        # The floating DnD window rides the cursor and must survive its
        # source view auto-scrolling out from under the drag.
        try:
            from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
            if DragDrop.is_dragged_item(ds):
                return False
        except Exception:
            pass

        parent = ds._parent
        opted_out = getattr(ds, '_kwargs', {}).get("hide_offscreen", True) is False
        if parent is not None and parent is not ds and not opted_out:
            anchor = getattr(parent, '_offset_ds', None)
            if anchor is None:
                anchor = parent
            if (anchor.width is not None and anchor.height is not None
                    and anchor.clipped_by_rect is not None):
                vl, vt, vr, vb = anchor.abs_clip_rect
                if vr <= vl or vb <= vt:
                    return True

        pw = ds.parent_window
        if (_depth < 16 and pw is not None and pw is not ds
                and pw.closable and pw._parent is not None):
            return cls._spawner_fully_clipped(pw, _depth + 1)
        return False

    @classmethod
    def end_frame(cls):
        # TEMP perf: section stamps (draw_main's _dm_marks pattern); a frame
        # over _ef_trace_ms logs the split to the perf log.
        _ef_trace_ms = 0.9
        _ef_marks = [("start", time.perf_counter())]
        _ef_mark = lambda label: _ef_marks.append((label, time.perf_counter()))
        if glfw_utils.frames_left > 0:
            request_render()

        cls.apply_move_to_front()

        # Caret / text-focus navigation steps: text focus is settled for the
        # frame now (apply_move_to_front may just have cleared it), so this
        # is where the undo swaps the focused caret with last-frame's.
        from src.lsd.gl_gui.view.core_views.core_undo import NavUndo
        NavUndo.poll_caret()

        # Drain GL resources queued for deletion (released GLStates, shader
        # programs invalidated by an edit) - must run on the render thread with
        # the context current, which is exactly here.
        from src.lsd.gl_gui.gl_state import GLState
        GLState.flush_deletes()

        # Drain callables posted from worker threads (post_to_render) - work
        # that must not race the frame, e.g. attaching symbol usages into a
        # LIVE gp tree that view walkers iterate (inserting a dict key during
        # another thread's iteration raises RuntimeError). Same thread here as
        # the GL delete queue above.
        cls._drain_render_tasks()

        # Deregister RenderHosts whose consumer windows have all closed, so
        # draw_main stops drawing/parsing them every frame. Toggle-gated
        # (Toggles.HostLifecycle): the host + its parse stay in the code-host
        # cache and re-register on reopen.
        from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
        RenderHost.sweep()

        # Deliberate GC scheduling - deferred gen2 + freeze + idle collects
        # (see gc_manager module docstring). Toggles.GC-gated inside.
        from src.lsd.gl_gui import gc_manager
        gc_manager.tick()

        cls.apply_refresh_nested_windows()

        # Reset overlay routing to the top (global, unmasked) channel so
        # end_frame draws - FPS counter, selection rects, debug text - don't
        # accidentally land on whatever per-window channel a view last set.
        if cls._overlay_channels_active:
            imgui.get_overlay_draw_list().channels_set_current(cls.max_layer - 1)

        Melty.mode_stack = []

        from src.lsd.gl_gui.modes import Modes
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_with_modes
        # draw_with_modes(Counters, name="counters", modes=(Modes.CODE_UI, Modes.CODE_PLAIN_TEXT), mode=Modes.WINDOW)

        if Toggles.debug_z_depth:
            draw_state = list(cls.selected)[-1] if len(cls.selected) > 0 else None
            if draw_state is not None:
                draw_list = imgui.get_overlay_draw_list()
                draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 40, pack_color(1, 0, 0, 1),
                                   f"Layer {draw_state.layer} "
                                   f"Depth {draw_state.depth} "
                                   f"zpos {draw_state.z_pos} "
                                   f"depth_and_layer {draw_state.depth_and_layer} "
                                   f"Melty.active_layer {Melty.active_layer} "
                                   f"Melty.z_pos {Melty.z_pos} "
                                   f"Melty.depth {Melty.depth}")

        _ef_mark("pre")   # TEMP perf
        cls.root_draw_states_by_layer = defaultdict(list)
        dynamic_offset = 0
        empty_parents = set()
        to_discard = set()

        for parent_ds_id, ds_list in cls.root_draw_states.items():
            for idx, ds in enumerate(ds_list):
                if ds.abs_closed or ds.closed:
                    to_discard.add((parent_ds_id, ds))
                    continue
                # A popover whose slot moved away closes itself - its spawner
                # may never draw it closed (see popover_orphaned).
                if cls.popover_orphaned(ds):
                    ds.closed = True
                    to_discard.add((parent_ds_id, ds))
                    continue
                # Hide - don't discard - nested windows whose spawning view is
                # fully offset-clipped out of sight. The window stays
                # registered (a discard could never come back while the layer
                # rides the blit cache, since only the parent's live call site
                # re-registers it), it just isn't dispatched: no draw, no
                # swoosh, no highlight. It reappears the moment the spawner
                # scrolls back into view.
                try:
                    hidden = cls._spawner_fully_clipped(ds)
                except Exception:
                    hidden = False
                if hidden != getattr(ds, '_hidden_offscreen', False):
                    ds._hidden_offscreen = hidden
                    # bvh_query memoizes per (x, y) keyed on _bvh_gen alone: a
                    # flag flip changes its effective result without an index
                    # mutation, so bump gen to invalidate the stale memo.
                    cls._bvh_gen += 1
                if not hidden:
                    cls.root_draw_states_by_layer[ds.abs_layer].append(ds)

        for ds_id, discard_ds in to_discard:
            # release_window_tree on an earlier discard pops the whole
            # nested list of a closed parent, so a closed child also queued
            # here could already be gone - tolerate it.
            siblings = cls.root_draw_states.get(ds_id)
            if siblings is not None and discard_ds in siblings:
                siblings.remove(discard_ds)
            # A closed nested window drops what it rendered (value slots,
            # nested windows below it, GL) - not just its registration.
            cls.release_window_tree(discard_ds)

        # Universal item drag-and-drop: pick up armed header drags, draw the
        # drop-point lines and commit/cancel on release. BEFORE the layer
        # dispatch - on frames where the (blitted) source collection doesn't
        # run, deferring inline drawing, this re-registers the floating
        # dragged window into its layer so the loop below still draws it
        # (see view/core_views/drag_drop.py). No per-frame invalidation:
        # the drag rides the closable-window blit fastpath.
        try:
            from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
            DragDrop.frame_update()
        except Exception as dnd_e:
            print(f"DragDrop.frame_update failed: {dnd_e}")

        # Rebuild the dense overlay channel map for this frame (see
        # overlay_window_channel): one entry per distinct window z index that
        # will render this frame - top-level registered windows plus every
        # dispatched nested window (whose window_index will be
        # abs_layer + its position in the by-layer list, mirroring the
        # _nested_index stamp in the dispatch loop below). Built after
        # DragDrop.frame_update so a re-registered floating drag window is
        # included.
        _ef_mark("discard")   # TEMP perf
        raw_window_indices = set()
        for w in cls.registered_windows.values():
            w_ds = getattr(w, 'draw_state', None)
            if w_ds is not None and not w_ds.closed and w_ds.layer is not None:
                raw_window_indices.add(w_ds.window_index)
        for l_idx, l_ds_list in cls.root_draw_states_by_layer.items():
            for d_idx in range(len(l_ds_list)):
                raw_window_indices.add(l_idx + d_idx)
        cls._overlay_channel_map = {
            raw: rank for rank, raw in enumerate(sorted(raw_window_indices))}

        # Per-window channels in exact paint order: the dispatch loop draws
        # bucket idx's ROOTS (cls.layers) before its nested windows
        # (root_draw_states_by_layer, in d_idx order), so sorting on
        # (bucket, root/nested, seq) reproduces the visual Z order even where
        # raw window_index values tie (e.g. an inactive-chain nested window at
        # parent+1 sharing an index with the next registry root).
        paint_ordered = []
        for seq, w in enumerate(cls.registered_windows.values()):
            w_ds = getattr(w, 'draw_state', None)
            if w_ds is not None and not w_ds.closed and w_ds.layer is not None:
                paint_ordered.append(((w_ds.layer, 0, seq), w_ds))
        for l_idx, l_ds_list in cls.root_draw_states_by_layer.items():
            for d_idx, n_ds in enumerate(l_ds_list):
                paint_ordered.append(((l_idx, 1, d_idx), n_ds))
        paint_ordered.sort(key=lambda t: t[0])
        cls._overlay_channel_by_ds = {
            id(p_ds): min(rank, cls.max_layer - 2)
            for rank, (_k, p_ds) in enumerate(paint_ordered)}
        # Exact visual z order of every dispatched window (roots + nested,
        # back to front) - the blit cache's window-occlusion mask for
        # shadow/glow stamps is built from this list's LIVE rects.
        cls.paint_ordered_ds = [p_ds for _k, p_ds in paint_ordered]

        # Which swoosh(es) the mouse is over: walk up from the BVH-hovered
        # draw_state (begin_frame's bvh_query hit, so occlusion and hidden
        # subtrees are already incorporated) to the first ancestor that is
        # either a dispatched nested-window root or the parent view
        # (offset_ds) that spawned one. Drives the swoosh hover override
        # below: hovering a window snaps its own connector to full opacity,
        # hovering a spawning view snaps the connector of every window it
        # spawned; all other connectors drop to the falloff floor. The
        # innermost matching ancestor wins, so hovering a spawner from
        # inside a nested window reads as the spawner, not the window. When
        # nothing matches, the connector keeps the default distance-based
        # fade.
        # spawner_targets is the SPAWNER half only (the parent view -> the
        # windows it spawned), without the child-hovers-itself entry. Drag-
        # focus mode reads that half: hovering the PARENT view lights its
        # connectors, but hovering a child window itself does not - only
        # dragging it does.
        _ef_mark("paint_order")   # TEMP perf
        swoosh_targets = defaultdict(set)  # id(hoverable ds) -> {id(window ds)}
        spawner_targets = defaultdict(set)
        for ds_list in cls.root_draw_states_by_layer.values():
            for ds in ds_list:
                swoosh_targets[id(ds)].add(id(ds))
                if ds._parent is not None:
                    off = ds._parent._offset_ds
                    if off is None:
                        off = ds._parent
                    swoosh_targets[id(off)].add(id(ds))
                    spawner_targets[id(off)].add(id(ds))
        hovered_swoosh_ids = None
        hovered_spawner_ids = None
        walk = cls.hovered_ds
        while walk is not None:
            hit = swoosh_targets.get(id(walk))
            if hit:
                hovered_swoosh_ids = hit
                # Same innermost node, spawner half only. Empty when that node
                # is just a hovered window (not a spawner), which is exactly
                # the case drag-focus must NOT light.
                hovered_spawner_ids = spawner_targets.get(id(walk))
                break
            if walk._parent is walk:  # root ds parents itself - end of chain
                break
            walk = walk._parent

        # Drag-focus mode (Swoosh.drag_focus): which windows are mid-gesture
        # this frame. A window move subscribes ("left_mouse_drag",
        # "window_move"); a right-drag corner resize (bottom-right, or
        # top-left with the left button held as a chord) subscribes
        # ("right_mouse_drag", "corner_drag"). on_action composes the view_id
        # as f"{tile_id}_{view_id}", so the set of windows currently being
        # dragged is read straight off this frame's event map - no extra state
        # to keep in sync with the gesture's lifetime.
        dragging_tiles = set()
        if Swoosh.drag_focus:
            for ev_type, suffix in (("left_mouse_drag", "_window_move"),
                                    ("right_mouse_drag", "_corner_drag")):
                for view_id in (cls.events_by_type.get(ev_type) or ()):
                    vid = str(view_id)
                    if vid.endswith(suffix):
                        dragging_tiles.add(vid[:-len(suffix)])

        _ef_mark("swoosh")   # TEMP perf
        cls._last_paint_rank = -1
        for idx in range(len(cls.layers)):
            layer = cls.layers[idx]
            imgui.set_cursor_screen_pos((0, 0))
            Melty.active_layer = idx
            Melty.active_layer_stack = []

            if not Melty.channels_split:
                imgui.get_window_draw_list().channels_split(Melty.max_depth)
                imgui.get_window_draw_list().channels_set_current(Melty.max_depth - 1)
                Melty.channels_split = True

            for draw_state in layer:
                if draw_state is not None:
                    # Same once-per-frame guard as the root_draw_states pass
                    # below: a draw_state already fully drawn this frame (its
                    # unique is registered in the wrapper) is never drawn
                    # again - double-queuing must not cause double-drawing.
                    if draw_state.unique not in cls.seen_unique:
                        Melty.paint_rank = cls.next_paint_rank(idx)
                        cls.draw(draw_state)

            Melty.depth = 0
            if Melty.channels_split:
                # Flatten layers into single channel
                imgui.get_window_draw_list().channels_set_current(0)
                imgui.get_window_draw_list().channels_merge()
                Melty.channels_split = False
            # Sort by y position (draw_state.abs_top)

            # sort by draw_state.z_pos

            # sorted_root_ds = sorted(cls.root_draw_states_by_layer[idx], key=lambda ds: ds.z_pos)

            for d_idx, draw_state in enumerate(cls.root_draw_states_by_layer[idx]):
                # Melty.cache.mask_mark_view(draw_state.z_pos, draw_state.left,
                #                            draw_state.top, draw_state.width, draw_state.height,
                #                            f"view_mask_{draw_state.id}", 4)

                Melty.active_layer = idx + (d_idx)
                Melty.paint_rank = cls.next_paint_rank(Melty.active_layer)
                draw_state._nested_index = (d_idx)
                Melty.z_pos = (Melty.paint_rank * Melty.max_depth) + Melty.depth

                draw_state.layer = Melty.active_layer
                draw_state.z_pos = Melty.z_pos
                draw_state.depth_and_layer = (Melty.shadow_depth, Melty.paint_rank)
                draw_state._kwargs['active_layer'] = Melty.active_layer

                child_highlight = None
                if draw_state._kwargs.get("swoosh", True):
                    if draw_state._parent is not None:
                        offset_ds = draw_state._parent._offset_ds

                        if offset_ds is None:
                            offset_ds = draw_state._parent


                        overlay_dl: _DrawList = imgui.get_overlay_draw_list()
                        # Route to the window's z-order channel so this overlay
                        # sits above the window's own content but is masked by
                        # any higher-layer window (matches the renderer's mask).
                        layer_index = draw_state.window_index

                        overlay_dl.channels_set_current(Melty.overlay_channel_for(offset_ds))

                        # Color the highlight using the *parent* window's tint:
                        # the nested view doesn't always carry a tint of its own.
                        # current_tint is stashed at draw time (the style manager's
                        # live tint is gone by this post-draw highlight code).
                        parent_tint = offset_ds.current_tint or (draw_state._kwargs.get("tint", (1, 1, 1))[:3], 1.0)
                        highlight_rgb = Melty._highlight_rgb(parent_tint)
                        outline_col = pack_color(*highlight_rgb, Tint.highlight_outline_alpha)
                        bg_col = pack_color(*highlight_rgb, Tint.highlight_bg_alpha)

                        # Parent view: faint fill + matching highlight outline,
                        # clipped to the parent's own clip rect so the highlight
                        # doesn't bleed past where the parent is scrolled/clipped.
                        parent_clip = offset_ds.abs_clip_rect if offset_ds.clipped_by_rect is not None else None
                        if parent_clip is not None:
                            overlay_dl.push_clip_rect(parent_clip[0], parent_clip[1],
                                                      parent_clip[2], parent_clip[3], True)
                        offset_rounding = getattr(offset_ds, 'corner_radius', 6)
                        # Read the anchor's LIVE position (_abs_left/_abs_top), not
                        # its per-frame-cached abs_left/abs_top. When the parent
                        # WINDOW is dragged, the window's own abs cache mutates
                        # on new window_pos, but a sub-view INSIDE it keeps the same
                        # cache key (its own left_offset/window_pos didn't change),
                        # so its cached abs_left lags one frame behind the blitted
                        # window pixels - the highlight box and swoosh would trail
                        # the window during a drag. _abs_left re-walks the moved
                        # window through the parent chain, so it tracks the drag
                        # (the same reason draw_state.pin_rect reads live abs).
                        o_l, o_t = offset_ds._abs_left(), offset_ds._abs_top()
                        overlay_dl.add_rect_filled(o_l, o_t,
                                                   o_l + offset_ds.width,
                                                   o_t + offset_ds.height,
                                                   bg_col, rounding=offset_rounding)
                        overlay_dl.add_rect(o_l, o_t,
                                            o_l + offset_ds.width,
                                            o_t + offset_ds.height,
                                            outline_col, rounding=offset_rounding,
                                            thickness=Tint.highlight_outline_thickness)
                        if parent_clip is not None:
                            overlay_dl.pop_clip_rect()

                        # The child outline + swoosh depend on the child's SIZE,
                        # which only becomes current after cls.draw(draw_state) below
                        # (auto_resize windows compute their width/height as they
                        # draw). Stash the params and draw them post-draw to avoid a
                        # frame of lag. (Positions are read live below, not cached here.)
                        child_highlight = (overlay_dl, offset_ds,
                                           outline_col, highlight_rgb, parent_clip)

                if not Melty.channels_split:
                    imgui.get_window_draw_list().channels_split(Melty.max_depth)
                    imgui.get_window_draw_list().channels_set_current(min(Melty.active_layer, Melty.max_depth - 1))
                    Melty.channels_split = True


                if draw_state.unique not in cls.seen_unique:
                    cls.draw(draw_state)

                # Now that the child has been drawn this frame, its SIZE is
                # current: draw the child outline + swoosh of current bounds.
                if child_highlight is not None and draw_state.width is not None and draw_state.height is not None:
                    overlay_dl, offset_ds, outline_col, highlight_rgb, parent_clip = child_highlight
                    # Route to the *nested* view's own overlay channel (not its
                    # window_index, which collapses to the parent's layer for a
                    # first-level nested view) so the line/outline aren't masked
                    # by the nested window. cls.draw may also have moved the channel.
                    rounding = getattr(draw_state, 'corner_radius', 6)

                    # The child end wears the CHILD window's OWN color: the
                    # highlight outline takes it and the swoosh blends parent ->
                    # child between the two ends. current_tint is intentionally
                    # NOT preferred here - it's the AMBIENT tint at draw
                    # (stashed before the wrapper pushes the window's own tint,
                    # and replayed as ambient context for latched closable
                    # windows), so it reads the enclosing window, not this one.
                    # Prefer the live child tint kwarg, then the ds.tint
                    # first-draw stamp; ambient only when the child declares no
                    # tint of its own (the old single-color look).
                    child_tint = draw_state._kwargs.get("tint")
                    if not (isinstance(child_tint, (tuple, list)) and len(child_tint) >= 3):
                        child_tint = draw_state.tint
                    if not (isinstance(child_tint, (tuple, list)) and len(child_tint) >= 3):
                        child_tint = draw_state.current_tint
                    child_rgb = (Melty._highlight_rgb(tuple(child_tint[:3]))
                                 if child_tint else highlight_rgb)
                    child_outline_col = pack_color(
                        *child_rgb, Tint.highlight_outline_alpha)

                    overlay_dl.channels_set_current(Melty.overlay_channel_for(draw_state))

                    # Live endpoint positions for both ends - see the note on the
                    # parent highlight box above. Either end can be a child of
                    # (or be) a window that's mid-drag; the cached abs_left/abs_top
                    # lag a frame behind the blitted windows, so the connector and
                    # the child outline read live to stay locked to the windows.
                    o_l, o_t = offset_ds._abs_left(), offset_ds._abs_top()
                    c_l, c_t = draw_state._abs_left(), draw_state._abs_top()

                    overlay_dl.add_rect(c_l, c_t,
                                        c_l + draw_state.width,
                                        c_t + draw_state.height,
                                        child_outline_col, rounding=rounding,
                                        thickness=Tint.highlight_outline_thickness)

                    # overlay_dl.channels_set_current(min(Melty.max_layer - 1, offset_ds.window_index))

                    # Default: the hovered state override (None = nobody
                    # hovered, keep the proximity fade). Drag-focus mode
                    # resolves the same True/False override from the live drag
                    # gesture instead, so an un-lit connector always sits at
                    # the floor rather than fading with the offset.
                    swoosh_hover = (None if hovered_swoosh_ids is None
                                    else id(draw_state) in hovered_swoosh_ids)
                    if Swoosh.drag_focus:
                        swoosh_hover = Melty._swoosh_drag_focus(
                            draw_state, offset_ds, dragging_tiles,
                            hovered_spawner_ids is not None
                            and id(draw_state) in hovered_spawner_ids)

                    Melty._draw_swoosh(
                        overlay_dl,
                        o_l, o_t,
                        offset_ds.width, offset_ds.height,
                        c_l, c_t,
                        draw_state.width, draw_state.height,
                        highlight_rgb,
                        rgb2=child_rgb,
                        p_round=getattr(offset_ds, 'corner_radius', 6),
                        n_round=rounding,
                        p_clip=parent_clip,
                        mode=draw_state._kwargs.get("swoosh_mode"),
                        hover=swoosh_hover,
                    )

                if Melty.channels_split:
                    # Flatten layers into single channel
                    imgui.get_window_draw_list().channels_set_current(0)
                    imgui.get_window_draw_list().channels_merge()
                    Melty.channels_split = False


                    # for i in range(draw_state.context_menu_offset):
                    #     if offset_ds._parent is None:
                    #         break
                    #     offset_ds = offset_ds._parent

                # Melty.depth = draw_state.depth + d_idx
                # Melty.cache.draw_tile(draw_state)
                # last_bounding_hovered = draw_state._bounding_hovered
                # new_bounding_hovered = draw_state.is_bounding_hovered()
                # hover_changed = last_bounding_hovered != new_bounding_hovered
                # draw_state._bounding_hovered = new_bounding_hovered
                # if (draw_state.width is None or draw_state.height is None or hover_changed or
                #         draw_state._bounding_hovered != draw_state._imgui_popover_open):
                #     Melty.cache.invalidate(draw_state._tile_id)
                #     # draw_state.draw_rect()
        _ef_mark("layer_loop")   # TEMP perf
        cls.layers = []

        is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
        Melty.imgui_popup_open = is_popup_open
        #
        from src.lsd.gl_gui.view.core_views.core_render import get_melty_state
        melty = get_melty_state()

        melty.hover_stack = []
        melty.hotkey_stack = []
        melty.unique_stack = []
        Melty.draw_state_stack = []

        if not melty.nearest_drop_target is None:
            melty.drag_drop_target = melty.nearest_drop_target
            melty.drag_drop_target_tag = melty.nearest_drop_target_tag

        while len(cls.items_to_delete) > 0:
            key, collection = cls.items_to_delete.pop(0)
            error = delete_from_collection(key, collection)
            if error is not None:
                print(error)
            cls.cache.invalidate_by_obj(collection)
            request_render()

        Melty.hovered_drawstate = Melty.hovered_drawstate_pending
        Melty.imgui_any_item_active = imgui.is_any_item_active()
        Melty.active_layer = 0
        Melty.paint_rank = 0
        style = imgui.get_style()

        style.item_spacing = Melty.original_spacing
        style.window_padding = Melty.original_window_padding
        style.frame_padding = Melty.original_frame_padding

        overlay: _DrawList = imgui.get_overlay_draw_list()


        window_size = imgui.get_io().display_size
        if Toggles.show_fps:
            overlay.add_text(window_size.x - 600, 5, pack_color(1, 1, 1, 1),
                             f"FPS: {imgui.get_io().framerate:.1f}")

        _ef_mark("post_layers")   # TEMP perf
        to_unselect = set()
        for selected_ds in cls.selected:
            if selected_ds.abs_closed or selected_ds.closed:
                to_unselect.add(selected_ds)

            if not selected_ds._kwargs.get("selectable", True):
                to_unselect.add(selected_ds)

        for ds in to_unselect:
            if ds in cls.selected:
                cls.selected.remove(ds)


        for selected_ds in cls.selected:

            if selected_ds.width is None or selected_ds.height is None:
                continue

            draw_fill = True
            if selected_ds.height > 30:
                draw_fill = False

            # Draw the selection rect to the selected view's own overlay
            # channel (same as the nested-view highlight and swoosh) so a
            # higher-layer window stencil-masks it, rather than the rect
            # floating on top of everything on the global top channel.
            overlay.channels_set_current(cls.overlay_channel_for(selected_ds))

            # Color from the view's storable tint, brightened the same way as
            # the highlight boxes (current_tint may be None -> falls back to
            # the live tint inside _highlight_rgb).
            select_rgb = cls._highlight_rgb(selected_ds.current_tint)
            bg_col = pack_color(*select_rgb, Tint.select_bg_alpha)
            outline_col = pack_color(*select_rgb, Tint.select_outline_alpha)
            rounding = getattr(selected_ds, 'corner_radius', 6)

            clip_rect = selected_ds.abs_clip_rect
            overlay.push_clip_rect(clip_rect[0], clip_rect[1], clip_rect[2], clip_rect[3], True)
            x0, y0 = selected_ds.abs_left, selected_ds.abs_top
            x1, y1 = x0 + selected_ds.width, y0 + selected_ds.height
            if draw_fill:
                overlay.add_rect_filled(x0, y0, x1, y1, bg_col, rounding=rounding)
            overlay.add_rect(x0, y0, x1, y1, outline_col, rounding=rounding,
                             thickness=Tint.select_outline_thickness)
            overlay.pop_clip_rect()

        # Restore the global top channel for any later overlay draws.
        if cls._overlay_channels_active:
            overlay.channels_set_current(cls.max_layer - 1)

        # Emphasis flashes (Melty.emphasize): rounded rect fades on the same
        # frame-count scale as the InvalidateTracker notes below. Manual
        # (auto_fade=False) notes hold at full alpha until the caller
        # releases them with an auto_fade=True call.
        _ef_mark("selection")   # TEMP perf
        for key in list(cls.emphasis_notes.keys()):
            note = cls.emphasis_notes[key]
            if (not note.auto_fade and cls.frame_count
                    - getattr(note, "last_touch", note.frame) > cls.emphasis_hold_grace):
                # Stuck-note guard: the owner stopped re-asserting its hold
                # (window closed, tab switched, body no longer runs) -
                # force-release into the fade.
                note.auto_fade = True
                note.frame = cls.frame_count
            if note.auto_fade:
                frames_past = cls.frame_count - note.frame
                alpha = 1.0 - frames_past / max(1, note.fade_frames)
                if alpha <= 0.0:
                    del cls.emphasis_notes[key]
                    continue
                # Keep frames flowing so the fade animates even while input
                # is idle. Holding (manual) notes are static - they just
                # ride whatever frames render anyway.
                request_render()
            else:
                alpha = 1.0
            # Non-rect kinds (click ripple, virtual cursor) skip the fade /
            # hold bookkeeping above but draw their own shapes.
            kind = getattr(note, "kind", "rect")
            if kind != "rect":
                try:
                    center = note.center() if callable(note.center) else note.center
                except Exception:
                    del cls.emphasis_notes[key]
                    continue
                if center is not None:
                    cls._draw_emphasis_shape(overlay, kind, note, center, alpha)
                continue
            try:
                rect = note.rect() if callable(note.rect) else note.rect
            except Exception:
                # The rect provider died (e.g. captured draw_state torn
                # down) - a note that can't place itself must not linger.
                del cls.emphasis_notes[key]
                continue
            if rect is not None:
                x0, y0, x1, y1 = rect
                # HDR lift (Toggles.HDR.emphasis_stops / emphasis_fill_stops):
                # the outline glows past the desktop's white, the fill stays
                # near SDR so the flashed content is still readable.
                (lr, lg, lb), (fr, fg, fb) = cls._emphasis_colors(note.tint)
                # Draw-list-level scissor (NOT imgui.push_clip_rect -- that
                # one corrupts tiles): the note's own clip, if any.
                clip = getattr(note, "clip", None)
                try:
                    clip = clip() if callable(clip) else clip
                except Exception:
                    clip = None
                if clip is not None:
                    overlay.push_clip_rect(clip[0], clip[1], clip[2], clip[3], True)
                overlay.add_rect_filled(x0, y0, x1, y1,
                                        pack_color(fr, fg, fb, 0.25 * alpha),
                                        rounding=note.rounding)
                # bloom halo: a wider, softer outline under the crisp one
                overlay.add_rect(x0, y0, x1, y1,
                                 pack_color(lr, lg, lb, 0.28 * alpha),
                                 rounding=note.rounding, thickness=note.thickness * 3.0)
                overlay.add_rect(x0, y0, x1, y1,
                                 pack_color(lr, lg, lb, 0.9 * alpha),
                                 rounding=note.rounding, thickness=note.thickness)
                if clip is not None:
                    overlay.pop_clip_rect()

        if Toggles.InvalidateTracker.enable:
            for key, note in InvalidateTracker.invalidations.items():
                ds = note.draw_state
                color = note.tint

                frames_past = Melty.frame_count - note.frame
                alpha_from_frame_past = max(0, 1.0 - (frames_past / max(1, Toggles.InvalidateTracker.keep_for_frames)))
                alpha_from_note = note.tint[3] if len(note.tint) > 3 else 1.0

                if ds is None:
                    continue

                invalidation_rect = (ds.abs_left, ds.abs_top,
                                     ds.abs_left + (ds.width or 0),
                                     ds.abs_top + (ds.height or 0))
                text_size = imgui.calc_text_size(f"{note.name} | {note.reason}")
                overlay.add_rect_filled(invalidation_rect[0] + ds.width - text_size.x, invalidation_rect[1],
                                        invalidation_rect[0] + ds.width,
                                        invalidation_rect[1] + text_size.y,
                                        pack_color(*color[:3], alpha_from_frame_past * alpha_from_note))

                overlay.add_text(invalidation_rect[0] + ds.width - text_size.x, invalidation_rect[1], pack_color(*(0,0,0),
                                                                                                           alpha_from_frame_past * alpha_from_note),
                                 f"{note.name} |{note.reason}")

                if Toggles.InvalidateTracker.draw_rect:
                    overlay.add_rect(invalidation_rect[0], invalidation_rect[1], invalidation_rect[2], invalidation_rect[3],
                                        pack_color(*color[:3], alpha_from_frame_past / 2.0 * alpha_from_note), thickness=1.0)



        if Toggles.InvalidateTracker.draw_bvh:
            for key, note in InvalidateTracker.invalidations.items():
                if note.rect is not None:
                    ds = note.draw_state
                    color = note.tint

                    frames_past = Melty.frame_count - note.frame
                    alpha_from_frame_past = max(0, 1.0 - (frames_past / Toggles.InvalidateTracker.keep_for_frames))

                    invalidation_rect = note.rect

                    overlay.add_text(invalidation_rect[0], invalidation_rect[1] - 15, pack_color(*color,
                                                                                                               alpha_from_frame_past),
                                     f"{note.name} |{note.reason}")


                    overlay.add_rect(invalidation_rect[0], invalidation_rect[1], invalidation_rect[2], invalidation_rect[3],
                                     pack_color(*color, alpha_from_frame_past), thickness=1.0)

        if Toggles.show_filled_tiles:
            # Mirror the InvalidateTracker overlay loop, but for tiles whose
            # filled_bbox now covers their full area - a transparent green
            # wash so you can see at a glance which views the scroll-driven
            # invalidation has stopped touching.
            fill_col = pack_color(0.0, 1.0, 0.2, 0.18)
            edge_col = pack_color(0.0, 1.0, 0.2, 0.55)

            fill_col_fill = pack_color(1.0, 1.0, 0.2, 0.18)
            edge_col_fill = pack_color(1.0, 1.0, 0.2, 0.55)
            for tile in cls.cache._tiles.values():
                ds = tile.draw_state
                if ds is None or ds.width is None or ds.height is None:
                    continue
                if tile is None or not cls.cache._tile_fully_filled(tile):
                    x0, y0 = ds.abs_left, ds.abs_top
                    x1, y1 = x0 + ds.width, y0 + ds.height
                    overlay.add_rect_filled(x0, y0, x1, y1, fill_col_fill)
                    overlay.add_rect(x0, y0, x1, y1, edge_col_fill, thickness=1.0)
                else:

                    x0, y0 = ds.abs_left, ds.abs_top
                    x1, y1 = x0 + ds.width, y0 + ds.height
                    overlay.add_rect_filled(x0, y0, x1, y1, fill_col)
                    overlay.add_rect(x0, y0, x1, y1, edge_col, thickness=1.0)

        _ef_mark("emphasis+debug_draw")   # TEMP perf
        Collisions.handle_collisions()

        if Toggles.developer_mode:
            draw_notifications()

        for parent_ds_id, ds_list in cls.root_draw_states.items():
            if len(ds_list) == 0:
                empty_parents.add(parent_ds_id)
        for empty_parent in empty_parents:
            cls.root_draw_states.pop(empty_parent, None)

        # Drop key events now that every view has rendered - including the
        # windows drawn above in this method's layer loop (the editors, the
        # floating search box). Clearing earlier would empty the buffer before
        # those windows read it, which is why text input saw no keys.
        _ef_mark("debug")   # TEMP perf
        cls.frame_key_events = []
        _ef_mark("tail")   # TEMP perf: end of section split
        _ef_total = (_ef_marks[-1][1] - _ef_marks[0][1]) * 1000.0
        if _ef_total >= _ef_trace_ms:
            from src.lsd.gl_gui.perf_trace import trace as _ef_trace
            _ef_trace("end_frame perf", total_ms=round(_ef_total, 2),
                      breakdown=" ".join(f"{_l1}={(_t1 - _t0) * 1000.0:.2f}"
                                         for (_l0, _t0), (_l1, _t1) in zip(_ef_marks, _ef_marks[1:])))



    @classmethod
    def finalize_overlay_channels(cls):
        """Capture per-channel index counts, then merge the foreground draw
        list's channels. MUST run before imgui.end_frame(): ImGui's render path
        requires channels to be merged, and leaving them split collapses the
        per-channel content. We use cumulative index counts (not command
        counts) as boundaries because ChannelsMerge drops trailing empty
        commands and can fuse a channel's first command into the previous
        channel's last command — index counts survive both."""
        if not cls._overlay_channels_active:
            return
        overlay = imgui.get_overlay_draw_list()
        cum_idx = 0
        idx_boundaries = []
        per_channel = []
        for ch in range(cls.max_layer):
            overlay.channels_set_current(ch)
            n = overlay.idx_buffer_size
            per_channel.append(n)
            cum_idx += n
            idx_boundaries.append(cum_idx)
        overlay.channels_merge()
        cls._overlay_channel_ranges = cls._resolve_channel_command_ranges(
            overlay, idx_boundaries
        )
        cls._overlay_channels_active = False

        if cls.frame_count % 120 == 0:
            nz = [(ch, n) for ch, n in enumerate(per_channel) if n > 0]

    @classmethod
    def to_delete(cls, key, collection):
        cls.items_to_delete.append((key, collection))

    @classmethod
    def post_frame(cls, imgui_impl, window):
        # TEMP perf (present-stall hunt): CPU split of each frame segment
        # plus GPU timestamps at the same boundaries. The CPU numbers say
        # where THIS thread blocked; the GPU numbers (read DEPTH frames late,
        # never blocking) say which segment's draw calls the GPU actually
        # spent the stall executing - a swap that blocks for 500ms with all
        # CPU segments cheap is pure backpressure, and only the GPU split
        # can name the flooding pass.
        from src.lsd.gl_gui import perf_trace as _pt
        from src.lsd.gl_gui.gpu_frame_timer import GPU_TIMER as _gt
        _pp = time.perf_counter
        _gt.begin(_pt.enabled(), cls.frame_count)
        _gt.stamp("t0")
        _ps_t0 = _pp()
        imgui_impl.begin_frame_split()
        imgui.render()

        # The REAL framebuffer: imgui's display_size is the content inset by
        # the shadow margin (frame_inset); captures, masks and the filters
        # run over the whole surface.
        fb_w, fb_h = cls.framebuffer_size or imgui.get_io().display_size
        draw_data = imgui.get_draw_data()
        cls.draw_backgrounds()
        _ps_t1 = _pp()
        imgui_impl.render_except_overlay(draw_data)
        # The renderer paints into the inset viewport; everything below
        # (filters, and reading GL_VIEWPORT for the FB-0 size) is full-frame.
        gl.glViewport(0, 0, int(fb_w), int(fb_h))
        _gt.stamp("ui")
        _ps_t2 = _pp()
        Melty.cache.finalize_captures((int(fb_w), int(fb_h)))
        # The frameless window's shadow margin sees only the content's
        # silhouette in the depth mask (an overhanging window would cast
        # its own shadows out there - see clear_mask_outside).
        _inset = int(cls.frame_inset or 0)
        _ox, _oy = (int(v) for v in (cls.frame_origin or (0, 0)))
        if _inset > 0 or _ox or _oy:
            Melty.cache.clear_mask_outside(_ox, _oy, int(fb_w) - _inset, int(fb_h) - _inset)
        _gt.stamp("captures")
        _ps_t3 = _pp()
        
        _scene_fb = Melty.default_framebuffer()
        if Toggles.filter_brightness:
            Melty.filter.brightness_contrast(
                         input_framebuffer=_scene_fb,
                         output_framebuffer=_scene_fb,
                         brightness=Toggles.brightness,
                         contrast=Toggles.contrast,
                         width=int(fb_w),
                         height=int(fb_h)
                     )


        if Toggles.filters:
         
            # if Toggles.draw_melty:
            total_layers = 1.0 / ((Melty.max_layer - 1.0) * (Melty.max_depth - 1.0)) * 100.0
            diff = (total_layers * 65535.0)
            # The shadow passes sample the R16 rank mask DIRECTLY and scale at
            # sample time (depth_scale) instead of going through a normalize()
            # pre-pass. That pass rendered the depth map into an RGBA8 filter
            # texture: one 8-bit quantum was ~6.6 of shadow_cast's 16 depth
            # slices, so quantized caster/receiver gaps flipped between k and
            # k+1 quanta whenever a window's z slot (~3.26 quanta) changed —
            # shadow intensity visibly wandered on every z reorder — and the
            # [0,1] clamp flattened all depths above layer ~78. Direct R16
            # sampling keeps ~0.3-quantum resolution, has no clamp, and drops
            # a full-screen pass.
            depth_scale = 1.0 / total_layers

            # Render the (expensive) shadow_cast pass at a reduced resolution.
            # shadow_cast's math is in UV space, so a low-res mask produces the
            # same soft shadow with far fewer fragment invocations. The composite
            # filter samples the shadow map as a sampler2D (GL_LINEAR), so it
            # upscales automatically over the full-res UI.
            downscale = max(1, int(Toggles.shadow_downscale))
            shadow_size = (
                max(1, int(fb_w) // downscale),
                max(1, int(fb_h) // downscale),
            ) if downscale > 1 else None

            shadow_raw = Melty.filter.shadow_cast(
                Melty.cache._full_mask_tex,
                max_steps=diff / 2.0,
                depth_scale=depth_scale,
                output_size=shadow_size,
                light_dir=tuple(Toggles.shadow_light_dir),
                height_scale=float(Toggles.shadow_height_scale),
                blur_scale=float(Toggles.shadow_blur_scale),
                blur_exponent=float(Toggles.shadow_blur_exponent),
                blur_samples=max(1, int(Toggles.shadow_blur_samples)),
                hit_strength=float(Toggles.shadow_hit_strength),
                hit_falloff=float(Toggles.shadow_hit_falloff),
                shadow_strength=float(Toggles.shadow_strength),
            )

            if not Toggles.draw_legacy:
                # Resolution of the shadow map when composited - controls the
                # bilateral upsample so the low-res shadow sticks to the crisp
                # rounded-rect edges instead of fringing.
                composite_shadow_size = shadow_size or (int(fb_w), int(fb_h))
                # Glow rects as light sources: hand the composite the low-res
                # glow buffer (add_glow marks, stamped in finalize_captures
                # PASS 6). glow_strength=0 skips the path in the shader, so an
                # empty/absent glow costs nothing; texture 0 is a legal
                # placeholder bind for the sampler in that case.
                _glow_tex = (Melty.cache.glow_tex
                             if Melty.cache.glow_active else None)
                _glow_on = _glow_tex is not None and Toggles.glow
                # The frameless window's frame (titlebar.frame_geometry):
                # outside the content's rounded rect the composite emits the
                # shadow as premultiplied alpha - the OS window's shadow IS
                # this pass, cast by the root's mark / the frame add_shadow.
                from src.lsd.gl_gui.titlebar import frame_geometry
                _f_origin, _f_radius, _f_size = frame_geometry(int(fb_w), int(fb_h))
                Melty.filter.shadow_composite(
                    input_framebuffer=_scene_fb,
                    output_framebuffer=_scene_fb,
                    shadow_map=shadow_raw,
                    frame_origin=_f_origin,
                    frame_radius=_f_radius,
                    frame_size=_f_size,
                    depth_map=Melty.cache._full_mask_tex,
                    depth_scale=depth_scale,
                    shadow_opacity=float(Toggles.shadow_opacity),
                    shadow_color=tuple(Toggles.shadow_color),
                    shadow_size=(float(composite_shadow_size[0]),
                                 float(composite_shadow_size[1])),
                    depth_sharpness=float(Toggles.shadow_edge_sharpness),
                    glow_map=_glow_tex if _glow_on else 0,
                    glow_strength=(float(Toggles.glow_strength)
                                   if _glow_on else 0.0),
                    glow_shadow_cut=float(Toggles.glow_shadow_cut),
                    # Specular rim on the lit edge - same light_dir as the
                    # cast pass so highlight and shadow stay opposite.
                    light_dir=tuple(Toggles.shadow_light_dir),
                    specular_bevel=float(Toggles.specular_bevel),
                    specular_roughness=float(Toggles.specular_roughness),
                    specular_strength=float(Toggles.specular_opacity),
                    specular_fade=float(Toggles.specular_fade),
                    specular_fade_rel=float(Toggles.specular_fade_rel),
                    # Window-to-window lookup for the fade origin: rank mask
                    # + rect table built in _build_window_mask. Texture 0
                    # (before the first mask build) samples as 0 → fade 1.
                    win_mask=Melty.cache._win_mask_tex or 0,
                    win_rects=Melty.cache._win_rects_tex or 0,
                    specular_depth_falloff=float(
                        Toggles.specular_depth_falloff),
                    specular_slope_tol=float(Toggles.specular_slope_tol),
                )

        # Debug: replace the frame with the raw low-res glow light buffer -
        # shows exactly what PASS 6 stamped, independent of the composite.
        if Toggles.glow_debug_view:
            _gdbg = Melty.cache.glow_tex
            if _gdbg is not None:
                Melty.filter.passthrough(_gdbg, output_framebuffer=_scene_fb)

        _gt.stamp("filters")
        _ps_t4 = _pp()
        # Overlay last, so the highlight/swoosh sits on top of the shadow pass
        # (the split renderer's intended slot: "below overlay" is everything above).
        imgui_impl.render_overlay_only(draw_data)
        # The frameless OS window's frame alpha - the last thing rendered
        # (titlebar.composite_window_frame): content opaque, corners/margin
        # cut (the shadow pass above has put the shadow there).
        from src.lsd.gl_gui.titlebar import composite_window_frame, wants_transparent_framebuffer
        if wants_transparent_framebuffer():
            gl.glViewport(0, 0, int(fb_w), int(fb_h))
            composite_window_frame(int(fb_w), int(fb_h))
        # Presentation: the linear fp16 scene encoded into the swapchain
        # (scene_target.present, Toggles.HDR.output). The texture GL back
        # holds the finished, display-encoded frame the screenshots read.
        from src.lsd.gl_gui import scene_target, wayland_color
        wayland_color.sync(window)        # surface tag follows Toggles.HDR.output
        scene_target.present(int(fb_w), int(fb_h))
        _gt.stamp("overlay")
        _ps_t5 = _pp()

        # Fulfill any pending MCP window screenshots now: the full frame is in
        # GL_BACK and the GL context is current on this (render) thread.
        from src.lsd.gl_gui.screenshot import process_captures, process_take_screenshot_flags
        process_captures(window)
        # Service deferred context-menu 'window' screenshots (front + settle, then grab).
        process_take_screenshot_flags(window)

        # Run any pending MCP eval_python commands on this (render) thread, where
        # it's safe to touch Melty/imgui state.
        from src.lsd.gl_gui.mcp_eval import process_evals
        process_evals()

        # Push this frame's resolved pointer shape to GLFW: imgui's own
        # request (titlebar edges, widgets) first, else the topmost hover
        # subscription's cursor= (gl_gui/mouse_cursor.py). Same thread as
        # every other GLFW call here.
        from src.lsd.gl_gui import mouse_cursor
        mouse_cursor.apply(window)

        _ps_t6 = _pp()
        glfw.swap_buffers(window)
        _gt.stamp("post")
        _ps_t7 = _pp()
        # Fetch GPU split (a few frames old) - log if it was expensive,
        # plus a periodic heartbeat line so a silent log reads as "GPU quiet",
        # never as "timer dead" (which gets its own one-shot line below).
        _gres = _gt.end()
        if _gres is not None:
            _gf, _gsegs = _gres
            _gtot = sum(_gsegs.values())
            if _gtot >= 20.0 or _gf % 300 == 0:
                _pt.trace("gpu frame split", frame=_gf,
                          total_ms=round(_gtot, 1),
                          **{k: round(v, 1) for k, v in _gsegs.items()})
        if _gt._dead and not getattr(cls, "_gpu_timer_dead_logged", False):
            cls._gpu_timer_dead_logged = True
            _pt.trace("gpu frame timer DEAD (GL error) — no gpu splits this session")
        # CPU split of the present, with the capture pass's metrics riding
        # along (tiles re-grabbed, their pixel area, mask rects touched).
        if (_ps_t7 - _ps_t0) * 1000.0 >= 30.0:
            _cap_n, _cap_px, _cap_m = getattr(Melty.cache,
                                              "last_capture_stats", (0, 0, 0))
            _pt.trace("present split (cpu)",
                      render=round((_ps_t1 - _ps_t0) * 1000.0, 1),
                      ui=round((_ps_t2 - _ps_t1) * 1000.0, 1),
                      captures=round((_ps_t3 - _ps_t2) * 1000.0, 1),
                      filters=round((_ps_t4 - _ps_t3) * 1000.0, 1),
                      overlay=round((_ps_t5 - _ps_t4) * 1000.0, 1),
                      shots=round((_ps_t6 - _ps_t5) * 1000.0, 1),
                      swap=round((_ps_t7 - _ps_t6) * 1000.0, 1),
                      cap_tiles=_cap_n, cap_px=_cap_px, cap_masks=_cap_m)

        from src.lsd.gl_gui.view.core_views.core_render import apply_drag_and_drop
        apply_drag_and_drop()
        pass

        InvalidateTracker.on_frame_end()
        AttributeChurnMonitor.on_frame_end()

    @classmethod
    def get_latest_mouse(cls):
        return imgui.get_io().mouse_pos

    @classmethod
    def report_imgui_active(cls):
        cls.imgui_active_pending = True
        cls.imgui_active = True

    @classmethod
    def add_blocker(cls, rect, layer=None):
        if layer is None:
            layer = cls.active_layer
        cls.pending_blockers[layer] = rect

    @classmethod
    def on(cls, event_name, tile_id) -> Optional[InputEvent]:
        id_str = tile_id
        if id_str in cls.events:
            if event_name in cls.events[id_str]:
                return cls.events[id_str][event_name]
        return None

    @classmethod
    def to_apply(cls, action: CollectionAction):
        cls.actions_to_apply.append(action)

    @classmethod
    def cleanup(cls):
        import time as _time_mod
        _t_cleanup0 = _time_mod.monotonic()
        _ptrace("melty: cleanup start (teardown of the old session)")
        # Drop any hanging MCP connections first, before the teardown below - a
        # client holding a streaming/keep-alive connection can otherwise block
        # shutdown. Lazy import keeps melty free of the mcp_server dependency.
        try:
            from src.lsd.gl_gui.mcp_server import notify_melty_shutdown
            notify_melty_shutdown()
        except Exception as e:
            print(f"[melty] mcp shutdown notify failed: {e}")
        # View teardown hooks FIRST (@render_func(on_cleanup=fn)): a view
        # severs what its draw_state holds (GPU tensors, interop textures)
        # while GLState can still queue the GPU side normally. Cheap: one
        # registry walk + per-view slot resets.
        try:
            from src.lsd.gl_gui.view.core_views.core_render import run_cleanup_callbacks
            n_hooks = run_cleanup_callbacks()
            _ptrace(f"melty: on_cleanup hooks run", n=n_hooks)
        except Exception as e:
            print(f"[melty] on_cleanup hooks failed: {e}")
        # The live-view STORES next: they ride the store-owning function
        # objects (module globals, parked def-runners) which outlive the
        # session, and without this every captured value and loop stack -
        # the (layer, head, query, key) volumes, 14 GB in one session -
        # carried straight into the next one. Same sweep as the OOM
        # responder: values, accumulators, watchers, marker/window refs.
        try:
            from src.lsd.gl_gui.view.core_conversion.live_view import release_all_live_stores
            n_keys = release_all_live_stores()
            _ptrace(f"melty: live stores released", n=n_keys)
        except Exception as e:
            print(f"[melty] live store release failed: {e}")
        # Then every window tree - registered roots and whatever nested
        # windows hang under them - drops its rendered values, so a restart
        # (the old session's roots get around, see sys._lsd_* dedupe) doesn't
        # carry the old session's tensors into the new one.
        try:
            n_trees = 0
            for mw in list(cls.registered_windows.values()):
                ds = getattr(mw, "draw_state", None)
                if ds is not None:
                    cls.release_window_tree(ds); n_trees += 1
                # The ManagedWindow's own slots pin the root's input AND its
                # draw_state; a relaunch in this process must not inherit
                # either (see ManagedWindow.unbind).
                mw.unbind()
            for parent_id in list(cls.root_draw_states.keys()):
                for ds in list(cls.root_draw_states.get(parent_id) or ()):
                    cls.release_window_tree(ds); n_trees += 1
            _ptrace(f"melty: window trees released", n=n_trees)
        except Exception as e:
            print(f"[melty] window tree release failed: {e}")
        try:
            from src.lsd.gl_gui.gl_state import GLState
            from src.lsd.gl_gui import scene_target
            scene_target.shutdown()
            GLState.shutdown_all()
        except Exception as e:
            print(f"[melty] gl_state shutdown failed: {e}")
        try:
            from src.lsd.gl_gui.fim import FimState
            FimState.shutdown_all()
        except Exception as e:
            print(f"[melty] fim shutdown failed: {e}")
        cls.filter.cleanup()
        cls.texture_manager.clear()
        Background.shutdown()
        Monitor.shutdown()
        FileWatch.shutdown()
        cls.glfw_window = None
        # Hand back the VRAM the hooks just released (refcount-freed).
        # Deliberately NO gc.collect() here: a full pass at teardown walks AND
        # deallocates the whole old session synchronously and that made shutdown
        # take seconds. The next session's boot pass does that work.
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as e:
            print(f"[melty] cuda empty_cache at cleanup failed: {e}")
        _ptrace(f"melty: cleanup done in "
                f"{(_time_mod.monotonic() - _t_cleanup0) * 1000:.0f}ms")

    @classmethod
    def get_channel(cls, depth=None):
        if depth is None:
            depth = cls.depth

        if depth < 0:
            depth = 0

        if depth >= cls.max_depth - 3:
            return cls.max_depth - 1

        return depth + 3
        # return max(min(cls.max_depth - 3, cls.depth), 0)

    @classmethod
    def delete_window(cls, draw_state):
        if draw_state is None:
            return
        window_key = draw_state._tile_id
        cls.pending_delete_window = (window_key, draw_state)

    @classmethod
    def register_global_hotkey(cls, key, mods, callback, text_focus_ok=False):
        """Bind `callback` to a keyboard shortcut that fires wherever the
        mouse is (begin_frame drains it from the press-edge queue). `key` is
        a glfw.KEY_*, `mods` the exact glfw.MOD_* mask that must be held
        (0 for a bare key). Hover-routed shortcuts belong on a render_func
        as event-named params (`ctrl_m_down`); this is for the few
        app-level ones. `text_focus_ok=True` lets it fire while a text
        editor / imgui input owns the keyboard — only for combos no editor
        binds. Re-registering a (key, mods) replaces the callback."""
        mod_mask = (glfw.MOD_CONTROL | glfw.MOD_SHIFT | glfw.MOD_ALT
                    | glfw.MOD_SUPER)
        cls.global_hotkeys[(key, mods & mod_mask)] = (callback,
                                                     bool(text_focus_ok))

    @classmethod
    def fire_global_hotkeys(cls, keyboard_owned=False):
        """begin_frame's drain of the register_global_hotkey bindings: every
        press edge in frame_key_events whose (key, exact modifier set — lock
        bits masked off) is bound runs its callback. `keyboard_owned` (a
        Melty text editor or an imgui input has the keyboard) mutes the
        bindings registered with text_focus_ok=False — the bare-E toggle's
        rule — while a text_focus_ok binding fires regardless (a Ctrl combo
        no editor binds, e.g. Ctrl+M). A raising callback is printed, never
        propagated into the frame. Edit and navigation history are framework
        defaults, available in every Surface as well as the studio; an app
        can override a chord with register_global_hotkey. Returns True when
        anything fired."""
        if not cls.frame_key_events:
            return False
        from src.lsd.gl_gui.view.core_views.core_undo import UndoManager, NavUndo
        control = glfw.MOD_CONTROL
        control_shift = control | glfw.MOD_SHIFT
        history_hotkeys = {
            (glfw.KEY_Z, control): (UndoManager.undo, True),
            (glfw.KEY_Z, control_shift): (UndoManager.redo, True),
            (glfw.KEY_Y, control): (UndoManager.redo, True),
            (glfw.KEY_LEFT, control_shift): (NavUndo.undo, True),
            (glfw.KEY_RIGHT, control_shift): (NavUndo.redo, True),
        }
        mod_mask = (glfw.MOD_CONTROL | glfw.MOD_SHIFT | glfw.MOD_ALT
                    | glfw.MOD_SUPER)
        fired = False
        for key, mods in list(cls.frame_key_events):
            chord = (key, mods & mod_mask)
            entry = cls.global_hotkeys.get(chord, history_hotkeys.get(chord))
            if entry is None:
                continue
            callback, text_focus_ok = entry
            if keyboard_owned and not text_focus_ok:
                continue
            try:
                callback()
            except Exception:
                import traceback
                traceback.print_exc()
            fired = True
        return fired

    @classmethod
    def find_window(cls, name):
        """The draw_state of a registered top-level window matching `name` — the
        full registered name ("Foo##@window") or just the display name ("Foo").
        Returns None if no such window is registered. Works for closed windows:
        a window's ManagedWindow stays registered (with its draw_state and
        position) while hidden, which is what makes launching one from elsewhere
        — e.g. search — possible without it being open first."""
        target = str(name)
        clean = target.split("##")[0]
        for w in cls.registered_windows.values():
            wn = getattr(w, 'name', None)
            if not wn or not (wn == target or wn.split("##")[0] == clean):
                continue
            # Only a BOUND entry counts. A hollow one (draw_state=None) is a
            # saved key nothing has drawn under this run - usually the
            # window's key from a previous draw_state (the id is part of the
            # key), sitting ahead of the live one in z-order. Returning that
            # makes Ctrl+Shift+F, the dock and the Windows search tab open
            # nothing (09-10).
            draw_state = getattr(w, 'draw_state', None)
            if draw_state is not None:
                return draw_state
        return None

    @classmethod
    def reclaim_window_slot(cls, name, tile_id):
        """A window registering under a NEW key (`tile_id` unseen this run)
        takes over the hollow entry saved under its `name`, if any: the key
        is `name##hash(unique + draw_state.id)`, so a window whose
        draw_state was re-minted (the registry rebuilt, a pruned entry)
        comes back under a fresh key while the profile still carries the
        old one — hollow for good, since nothing ever draws under it, and
        FIRST in every by-name scan (find_window, the Windows search tab).
        The entry is re-keyed in place (same ManagedWindow, same z-order
        slot, same dict object — it is the AppModel's) rather than left to
        pile up; a twin that was itself saved beside the live key is
        dropped. Called by the wrapper on a key's first registration of the
        run (its entry is still hollow), so the scan is once per window.
        Returns True when a slot was reclaimed."""
        windows = cls.registered_windows
        stale_key = None
        for key, managed_window in windows.items():
            if (key != tile_id and getattr(managed_window, 'name', None) == name
                    and getattr(managed_window, 'draw_state', None) is None):
                stale_key = key
                break
        if stale_key is None:
            return False
        if tile_id in windows:
            # Both keys were saved (the twin outlived a session): the live
            # key's place is the current z-order and the old one just goes.
            del windows[stale_key]
            return True
        items = list(windows.items())
        windows.clear()
        for key, managed_window in items:
            windows[tile_id if key == stale_key else key] = managed_window
        return True

    @classmethod
    def open_window(cls, name):
        """Open (un-hide) and raise a registered window by `name`, returning its
        draw_state (or None). Use to launch a closed/"lost" window from anywhere
        — e.g. a search result jumping to its window."""
        ds = cls.find_window(name)
        if ds is not None:
            ds.closed = False
            cls.move_window_to_front(ds)
        return ds

    @classmethod
    def window_tint(cls, name):
        """The display tint of a registered window by name. It lives on the
        ManagedWindow's input_value (the window's own object), NOT its draw_state
        — the draw_state keeps the generic default — so this is the colour to use
        when tinting things by window (e.g. search results). None if unknown or
        the window has no tint."""
        if name is None:
            return None
        clean = str(name).split("##")[0]
        # Bound entries first: a hollow one (see find_window) has no
        # input_value to read a tint from.
        for w in cls.registered_windows.values():
            wn = getattr(w, 'name', None)
            if wn and str(wn).split("##")[0] == clean and getattr(w, 'draw_state', None) is not None:
                return getattr(getattr(w, 'input_value', None), 'tint', getattr(w, 'tint', None))
        return None

    @classmethod
    def clamp_window_pos(cls, draw_state, x, y, margin=None):
        """Pull an intended window top-left back inside the display, keeping
        `margin` px (Toggles.WindowSettings.edge_margin) of room at every edge.

        Applied wherever a window is PLACED programmatically — summoned to the
        cursor by Ctrl+Shift+F, launched from a search hit, opened from the
        dock — so it never lands half off-screen and, in particular, never
        lands with its BOTTOM below the bottom of the display. Dragging is
        deliberately not clamped: a window you tuck off the edge yourself
        stays where you put it.

        The top-left wins when the window is larger than the display — its
        grab edge stays reachable and the overflow goes off the far side. A
        window that has never rendered has no width/height yet, so only its
        top-left is bounded; the next placement sees the real size."""
        disp = cls.display_size
        if draw_state is None or disp is None:
            return x, y
        if margin is None:
            margin = Toggles.WindowSettings.edge_margin
        width = draw_state.width or 0
        height = draw_state.height or 0
        return (max(margin, min(x, disp[0] - width - margin)),
                max(margin, min(y, disp[1] - height - margin)))

    @classmethod
    def summon_window(cls, draw_state, x, y):
        """Move a window so its top-left lands at screen (x, y) AND raise it —
        the "summon" the Dock's target button does, so a launched window comes
        to where you are instead of staying put (maybe off-screen). window_pos
        is the unanchored origin, so offset by the window's anchor delta
        (abs - window_pos), same as the Dock summon.

        The target is bounded by clamp_window_pos first, so summoning to a
        cursor near an edge (or to a dock row near the bottom) still lands the
        whole window on screen."""
        if draw_state is None:
            return
        x, y = cls.clamp_window_pos(draw_state, x, y)
        wp = draw_state.window_pos or (0, 0)
        from_zero_x = (draw_state.abs_left or 0) - wp[0]
        from_zero_y = (draw_state.abs_top or 0) - wp[1]
        draw_state.window_pos = (x - from_zero_x, y - from_zero_y)
        cls.move_window_to_front(draw_state)
        if cls.cache is not None and draw_state._tile_id is not None:
            cls.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=4)
        request_render()

    @classmethod
    def raise_pressed_window(cls, event):
        """Activate the hit window before controls run, without taking their press.

        Use the captured press point, not the cursor (which may already have
        moved). Only the frontmost hit owns activation; non-blocking handlers
        on covered windows must never raise those windows through the front one.
        """
        if cls.imgui_popup_open:
            return
        # Cached BVH z stamps can rank a covered descendant above the window
        # actually painted over it. The renderer already maintains that order;
        # scan WINDOWS only, and only on a press. Never fall back to stale hits
        # when no painted window holds the point.
        for window in reversed(cls.paint_ordered_ds):
            node = window
            hidden = False
            for _ in range(64):
                if ((node.closed and node.closable)
                        or getattr(node, "_hidden_offscreen", False)):
                    hidden = True
                    break
                parent = node.parent_window
                if parent is None or parent is node:
                    break
                if not parent.expanded:
                    hidden = True
                    break
                node = parent
            if hidden:
                continue
            left, top = window.abs_left, window.abs_top
            width, height = window.width, window.height
            if width is None or height is None or width <= 0 or height <= 0:
                continue
            clip_left, clip_top, clip_right, clip_bottom = window.abs_clip_rect
            if (max(left, clip_left) <= event.x < min(left + width, clip_right)
                    and max(top, clip_top) <= event.y < min(top + height, clip_bottom)):
                # Placed popovers retain their owner's text focus and stacking.
                # They still OCCLUDE their owner; don't search through them.
                if window.closable and window._kwargs.get("window_pos") is None:
                    cls.move_window_to_front(window)
                return

    @classmethod
    def move_window_to_front(cls, draw_state):
            if draw_state is None:
                return



            # Child windows aren't registered with the window manager (only
            # top-level windows, where parent_window is None, get registered).
            # If this draw_state isn't itself registered, walk up the
            # parent_window chain to the root window and bring it to front
            # instead, so dragging/clicking a nested view raises its owner.
            #
            # Collect every nested (unclosable, unregistered) window on that
            # walk - the clicked one and each intermediate sub-window up to but
            # excluding the registered root. apply_move_to_front raises each to
            # the front of ITS parent's nested-window list, so clicking one of
            # several sibling sub-windows under the same parent brings just that
            # one forward among them. Raising the root window only reorders whole
            # top-level windows, leaving sibling sub-windows in their own order.
            nested_chain = []
            if draw_state._tile_id not in Melty.registered_windows:
                node = draw_state
                while (node._tile_id not in Melty.registered_windows
                       and node.parent_window is not None
                       and node.parent_window is not node):
                    nested_chain.append(node)
                    node = node.parent_window
                if node._tile_id in Melty.registered_windows:
                    draw_state = node

            window_key = draw_state._tile_id
            # The walk may end without ever reaching a registered window - e.g. the
            # raise-on-press interaction passes whatever view is topmost under the
            # cursor, which can be a non-window top-level view. Don't set a move
            # for something the window manager doesn't track (apply_move_to_front
            # would only warn and no-op); just leave the z-order untouched.
            if window_key not in Melty.registered_windows:
                return
            cls.pending_move_to_front = (window_key, draw_state, nested_chain)

            # window_key = f"{cls.pending_move_to_front[0]}_window"
            # if window_key in Melty.registered_windows:
            #     # Remove and re-insert to move to end (top)
            #     window = Melty.registered_windows.pop(window_key)
            #     Melty.registered_windows[window_key] = window

        # Melty.cache.invalidate_up(tile_id)

    @classmethod
    def _render_windows_store(cls):
        """AppModel.render_windows — the first-seen window-name list — or
        None before the app model exists (boot, headless tests)."""
        root = getattr(cls.vis, "root", None)
        store = getattr(root, "render_windows", None)
        return store if isinstance(store, list) else None

    @classmethod
    def note_window_seen(cls, name):
        """A closable root registered under `name` this frame: append it to
        AppModel.render_windows if the list has never held it. Called from the
        wrapper's registration site for every managed window every frame, so
        the miss path is one list scan; the dock's signature already repaints
        on a new name. Returns True when the name was new."""
        if not name:
            return False
        store = cls._render_windows_store()
        if store is None:
            return False
        name = str(name)
        if name in store:
            return False
        store.append(name)
        return True

    @classmethod
    def recent_windows(cls, count):
        """The last `count` first-seen window names, most recent first."""
        store = cls._render_windows_store()
        if not store or count <= 0:
            return []
        return list(reversed(store[-count:]))

    @classmethod
    def adopt_registered_windows(cls, app_model):
        """Point Melty.registered_windows at the APP MODEL's dict so window
        z-order persists between runs: the dict's insertion order IS the
        z-order (apply_move_to_front pops + reinserts on every raise), and as
        an AppModel field it pickles/unpickles with the rest of the app.
        Called once from LSDStudio right after load_app_model. Saved keys
        keep their saved position; anything registered before the load (or
        first seen this session) merges in behind them. A pickled defaultdict
        comes back a plain dict, so the adopted dict is re-wrapped; loaded
        values are hollow ManagedWindows (name/hidden only) that re-fill at
        registration/draw like any never-drawn window."""
        loaded = getattr(app_model, "registered_windows", None)
        adopted = defaultdict(lambda: ManagedWindow())
        if isinstance(loaded, dict):
            for window_key, managed_window in loaded.items():
                adopted[window_key] = (managed_window if isinstance(managed_window, ManagedWindow)
                                       else ManagedWindow(name=str(window_key)))
        for window_key, managed_window in cls.registered_windows.items():
            # A pre-load entry is either hollow or left over from the previous
            # run in this process (cleanup unbinds it, but never trust it):
            # its draw_state will belong to the OLD root and shadow the one
            # just loaded under the same registry key. Keep the entry for its
            # old position but drop the bindings.
            if isinstance(managed_window, ManagedWindow):
                managed_window.unbind()
            adopted[window_key] = managed_window
        cls.registered_windows = adopted
        app_model.registered_windows = adopted
        return adopted

    @classmethod
    def apply_move_to_front(cls):
        # # No bring-to-front when the press lands on an imgui widget - the
        # # raise reshuffles z-order/caches mid-gesture and disrupts the
        # # widget. is_any_item_hovered() also reports the PREV frame's
        # # HoveredId, so it stays true on the press frame even when the
        # # widget's tile blit-skipped this frame (on_drag suppresses the
        # # self-invalidate on press frames, so the widget isn't submitted).
        # if imgui.is_any_item_hovered():
        #     return

        if cls.pending_delete_window is not None:
            window_key, draw_state = cls.pending_delete_window
            if window_key in Melty.registered_windows:
                draw_state.last_seen = None
                del Melty.registered_windows[window_key]

                print(f"Deleted window {window_key}")
                Melty.cache.invalidate_by_obj(Melty.registered_windows)
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=4, force=True)
            else:
                print(f"Warning: Tried to delete window but {window_key} not found in registered_windows")
                print(f"Registered windows: {list(Melty.registered_windows.keys())}")

            # A deleted window gives back everything under it: the value
            # refs its views hold, its nested windows (live value windows
            # and their satellites get unregistered too, or they'd linger
            # orphaned) and the GL resources (queued; drained by
            # flush_deletes in end_frame). Draw_states survive, so a
            # re-created window lazily refills on its next render.
            cls.release_window_tree(draw_state)

            cls.pending_delete_window = None
            request_render()
            return

        if cls.pending_move_to_front is None or Melty.imgui_popup_open:
            return

        if not cls.imgui_active:

            window_key = cls.pending_move_to_front[0]

            # Raising a window takes text focus away from an editor that lives
            # in a DIFFERENT window. The clicked window is the innermost nested
            # one (first of the chain) or the root itself; focus survives only
            # when that window is on the focused draw_state's parent_window path.
            focused = cls.text_focused_ds
            if focused is not None:
                nested_chain = cls.pending_move_to_front[2]
                clicked = nested_chain[0] if nested_chain else cls.pending_move_to_front[1]
                node, _n, inside = focused, 0, False
                while node is not None and _n < 64:
                    if node is clicked:
                        inside = True
                        break
                    nxt = node.parent_window
                    if nxt is node:
                        break
                    node = nxt
                    _n += 1
                if not inside:
                    focused.invalidate_up()
                    cls.text_focused_ds = None

            # Raise each clicked nested window to the front (end) of its
            # parent's nested-window list - the order there determines sibling
            # z-stacking (later in the list draws on top). Runs independently
            # of the root reorder below; clicking a back sub-window while its
            # parent is already the front top-level window must still restack it
            # among its siblings, the case the window_z_pos == layer gate misses.
            reordered = False
            for nested in cls.pending_move_to_front[2]:
                parent = nested.parent_window
                siblings = Melty.root_draw_states.get(parent.id) if parent is not None else None
                if siblings and siblings[-1] is not nested and nested in siblings:
                    siblings.remove(nested)
                    siblings.append(nested)
                    reordered = True

            window_z_pos = len(Melty.registered_windows) + Melty.top_layer_boost
            if cls.pending_move_to_front[1]._kwargs.get("always_on_top", False):
                # An always_on_top window never leaves its dedicated layer -
                # the standard front boost would drop it back into the root
                # layer, underneath the nested-window band.
                window_z_pos = cls.always_on_top_layer
            # layer == top_layer can't prove "already front": a window CLOSED
            # while front keeps its boosted layer (closed windows never
            # re-render, so nothing re-stamps it) while other windows get
            # raised above it in registered_windows order. The first
            # open_window() would then be skipped here and the window reopens
            # buried; the render pass re-stamps its registry index as layer,
            # which is why a SECOND raise worked. Only skip when the window is
            # is actually the top (last) registry entry.
            already_front = next(reversed(Melty.registered_windows), None) == window_key
            if window_z_pos != cls.pending_move_to_front[1].layer or not already_front:
                reordered = True
                old_layer = cls.pending_move_to_front[1].layer
                cls.pending_move_to_front[1].layer = window_z_pos
                draw_state = cls.pending_move_to_front[1]
                draw_state.active_layer = window_z_pos
                # Effect ledger: an ACTUAL raise (this window is not already
                # front) is an observable, not-undoable effect - the
                # Orchestrator cues off it, which is what makes a window-dock
                # row click verifiable by its effect ("Voxels came to front")
                # with no structure on the dock. Gated on the real change so
                # in-window press-raises of the front window stay silent.
                if cls.effect_hook is not None:
                    try:
                        cls.effect_hook(
                            "raise",
                            str(getattr(draw_state, "name", "?")).split("##")[0],
                            draw_state)
                    except Exception:
                        pass

                # Refresh the z_pos sort key for the raised window's WHOLE subtree
                # now, not when it next renders. bvh_query sorts hits by the stored
                # z_pos, but z_pos is only recomputed at render - a blit-cached
                # subtree keeps a behind-era z_pos, so a press over the just-raised
                # window sorts under the window now behind it and click-to-raise
                # picks the wrong one (the "resizing the front window raises the one
                # behind it" bug). Shift every descendant by the SAME layer delta
                # the render loop would (z_pos = active_layer * max_depth + depth),
                # so the whole subtree lifts above the windows it now sits in front
                # of while its INTERNAL order is preserved - that internal order is
                # what the imgui press-handoff priming (begin_frame) relies on to
                # pick the widget under the cursor, so don't disrupt it. Bounded
                # walk up parent_window avoids a self-referential cycle.
                delta_z = (window_z_pos - old_layer) * Melty.max_depth if old_layer is not None else 0
                if delta_z:
                    for _ds in list(cls._bvh_id_to_ds.values()):
                        node, _n = _ds, 0
                        while node is not None and _n < 64:
                            if node is draw_state:
                                if _ds.z_pos is not None:
                                    _ds.z_pos += delta_z
                                break
                            nxt = node.parent_window
                            if nxt is node:
                                break
                            node = nxt
                            _n += 1

                if window_key in Melty.registered_windows:
                    # Remove and re-insert to move to end (top)
                    window = Melty.registered_windows.pop(window_key)
                    Melty.registered_windows[window_key] = window
                else:
                    print(f"Warning: Tried to move window to front but {window_key} not found in registered_windows")
                    print(f"Registered windows: {list(Melty.registered_windows.keys())}")

                # if not cls.window_drag and not imgui.is_mouse_down(0):
                #     Melty.cache.invalidate_by_obj(Melty.registered_windows)
                #     note = Note(name="", reason="move_to_front", draw_state=draw_state, tint=(0.5, 1.0, 0.5))
                #     Melty.cache.invalidate_up(cls.pending_move_to_front[1]._tile_id, max_depth=4, force=True, note=note)

            # The reorder above changed the live z-order (abs_layer / sibling
            # list order) without changing the BVH index, so bump _bvh_gen to drop
            # the per-(x,y) bvh_query memo - otherwise a result cached for this
            # frame would match the pre-raise ordering and click-to-raise (which
            # reads bvh_query) could still pick the old front window for a frame.
            if reordered:
                cls._bvh_gen += 1

            # Clear once handled (inside the not-imgui_active gate so the move
            # still works through across frames where imgui owns the interaction). The
            # nested restack above already ran, and a root that's already front
            # needs no action - so don't leave the request pending anymore in
            # that case.
            cls.pending_move_to_front = None

    @classmethod
    def draw_blockers_to(cls):
        # Draw invisible buttons for each
        current_pos = imgui.get_cursor_screen_pos()
        blockers_rev = reversed(cls.imgui_blockers[:])
        for layer, rect in enumerate(blockers_rev):
            if rect is not None:
                imgui.set_cursor_screen_pos((rect[0], rect[1]))
                imgui.button(f"melty_blocker_{layer}",
                             rect[2] - rect[0],
                             rect[3] - rect[1])

        imgui.set_cursor_screen_pos(current_pos)

    @classmethod
    def set_channel(cls, layer_idx):
        if cls.channels_split:
            imgui.get_window_draw_list().channels_set_current(layer_idx)


    @classmethod
    def get_tile_id(cls):
        if len(cls.tile_id_stack) > 0:
            return cls.tile_id_stack[-1]
        else:
            return ""

    @classmethod
    def get_parent_tile_id(cls):
        if len(cls.tile_id_stack) > 1:
            return cls.tile_id_stack[-2]
        else:
            return None

    @classmethod
    def push_clip(cls, rect):
        draw_list = imgui.get_window_draw_list()
        current_clip = cls.get_clip_rect()
        if current_clip is not None:
            from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
            clip_new_rect = (
                max(current_clip[0], snap_int(rect[0])),
                max(current_clip[1], snap_int(rect[1])),
                min(current_clip[2], snap_int(rect[2])),
                min(current_clip[3], snap_int(rect[3])),
            )
            rect = clip_new_rect

        draw_list.push_clip_rect(*rect)
        cls.clip_stack.append(rect)

    @classmethod
    def pop_clip(cls):
        if len(cls.clip_stack) == 0:
            return
        draw_list = imgui.get_window_draw_list()
        draw_list.pop_clip_rect()
        cls.clip_stack.pop()

    @classmethod
    def get_clip_rect(cls):
        if len(cls.clip_stack) == 0:
            # fixed_size = cls.fixed_size_stack[-1] if len(cls.fixed_size_stack) > 0 else None
            # # if fixed_size is not None and fixed_size.width is not None and fixed_size.height is not None:
            # #     return (
            # #         fixed_size.left,
            # #         fixed_size.top,
            # #         fixed_size.left + fixed_size.width,
            # #         fixed_size.top + fixed_size.height
            # #     )
            return None

        return cls.clip_stack[-1]

    @classmethod
    def apply_clip_ds(self, draw_state):
        x = draw_state.left
        y = draw_state.top
        left, top = self.apply_clip((x, y))
        width, height = draw_state.width, draw_state.height
        right, bottom = draw_state.abs_left + width, draw_state.abs_top + height
        right, bottom = self.apply_clip((right, bottom))
        width, height = right - x, bottom - y

        return (draw_state.abs_left, draw_state.abs_top, width, draw_state.height)

    @classmethod
    def apply_clip(cls, point, fixed_size_ds=None):
        x,y = point
        fix_sized_ds = cls.fixed_size_stack[-1] if len(cls.fixed_size_stack) > 0 else fixed_size_ds
        if fix_sized_ds is not None and fix_sized_ds.width is not None and fix_sized_ds.height is not None:
            margin = (len(Melty.bg_stack) + 1) * 2.0
            clip_rect = (
                fix_sized_ds.abs_left,
                fix_sized_ds.abs_top,
                fix_sized_ds.abs_left + fix_sized_ds.width,
                fix_sized_ds.abs_top + fix_sized_ds.height
            )
        else:
            clip_rect = cls.get_clip_rect()

        if clip_rect is None:
            return x,y

        clip_left, clip_top, clip_right, clip_bottom = clip_rect
        x = max(clip_left, min(x, clip_right))
        y = max(clip_top, min(y, clip_bottom))
        return (x, y)

    @classmethod
    def apply_clip_x(cls, x):

        x,y = cls.apply_clip((x,0))
        return x

    @classmethod
    def apply_clip_width(cls, draw_state):
        width = draw_state.width
        fix_sized_ds = cls.fixed_size_stack[-1] if len(cls.fixed_size_stack) > 0 else None
        if fix_sized_ds is not None and fix_sized_ds.width is not None:

            x = draw_state.abs_left + draw_state.width
            x, y = cls.apply_clip((x, 0))
            width = x - draw_state.abs_left
        return width


    @classmethod
    def get_clip_size(cls):
        if len(cls.clip_stack) == 0:
            return None
        rect = cls.clip_stack[-1]
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        return width - 1, height - 1

    @classmethod
    def has_clip(cls):
        return len(cls.clip_stack) > 0

    @classmethod
    def get_parent_size(cls):
        if len(cls.clip_stack) < 2:
            return None, None
        rect = cls.clip_stack[-2]
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        return width, height

    @classmethod
    def get_space_left(cls):
        clip_rect = cls.get_clip_rect()
        if clip_rect is None:
            return 40
        cursor_x, _ = imgui.get_cursor_screen_pos()
        space_left = clip_rect[2] - cursor_x - 23
        return space_left

    @classmethod
    def init_complete(cls):
        return cls.frame_count > 2

    @classmethod
    def inside_clip(cls, draw_state=None, rect=None):
        clip_rect = cls.get_clip_rect()
        if clip_rect is None:
            return True
        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        if draw_state is not None:
            left = draw_state.left
            top = draw_state.top
            width = draw_state.width
            height = draw_state.height
        else:
            left, top, width, height = rect

        if top is None or left is None:
            return True

        if width is None or height is None:
            return True

        if (top + height < clip_top or top > clip_bottom):
            return False
        return True

    @classmethod
    def fully_inside_clip(cls, draw_state=None, rect=None):
        clip_rect = cls.get_clip_rect()
        if clip_rect is None:
            return True
        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        if draw_state is not None:
            left = draw_state.left
            top = draw_state.top
            width = draw_state.width
            height = draw_state.height
        else:
            left, top, width, height = rect

        if top is None or left is None:
            return True

        if width is None or height is None:
            return True

        if (top < clip_top or top + height > clip_bottom):
            return False

        return True

    @classmethod
    def undo_clip_n(cls, undo_point_id, n: int):
        """Undo (pop) only the last `n` clip rects and remember them for redo."""
        if not isinstance(n, int):
            raise TypeError("n must be an int")
        if n <= 0:
            return

        if not cls.clip_stack:
            cls.clip_stack_holder[undo_point_id] = []
            return

        n = min(n, len(cls.clip_stack))
        popped = cls.clip_stack[-n:]  # tail in original push order

        # Save only what we popped so redo can reapply just those.
        cls.clip_stack_holder[undo_point_id] = popped

        draw_list = imgui.get_window_draw_list()
        for _ in range(n):
            draw_list.pop_clip_rect()

        # Keep the remaining stack
        cls.clip_stack = cls.clip_stack[:-n]

    @classmethod
    def redo_clip_n(cls, undo_point_id):
        """Redo (push) the clip rects saved by undo_clip_n()."""
        popped = cls.clip_stack_holder.pop(undo_point_id, None)
        if not popped:
            return

        draw_list = imgui.get_window_draw_list()
        for rect in popped:
            draw_list.push_clip_rect(*rect)

        cls.clip_stack.extend(popped)

    @classmethod
    def undo_clip(cls, undo_point_id, n: int | None = None):
        if n is None:
            n = len(cls.clip_stack)
        return cls.undo_clip_n(undo_point_id, n)

    @classmethod
    def redo_clip(cls, undo_point_id):
        return cls.redo_clip_n(undo_point_id)

    @classmethod
    def current_path(cls) -> tuple[tuple[str, int | None], ...]:
        return tuple(cls._path_stack)

    @classmethod
    def push_slot(cls, field: str, idx: int | None):
        cls._path_stack.append((field, idx))

    @classmethod
    def pop_slot(cls):
        cls._path_stack.pop()

    @classmethod
    def current_root(cls, module_id: str) -> "cst.Module":
        return cls._root_by_module[module_id]

    @classmethod
    def bump_gen(cls, module_id: str):
        cls._gen_by_module[module_id] += 1

    @classmethod
    def current_gen(cls, module_id: str) -> int:
        return cls._gen_by_module[module_id]

    # LibCST tracking---------------------------------------------------------

    @classmethod
    def indent(cls, amount):
        if amount == 0:
            return
        cls.indent_count += 1
        cls.current_indent += amount
        cls.max_indent = max(cls.max_indent, cls.current_indent)
        imgui.indent(amount)

    @classmethod
    def unindent(cls, amount):
        if amount == 0:
            return
        cls.current_indent -= amount
        imgui.unindent(amount)
        cls.unindent_count += 1

    @classmethod
    def inside_window(cls):
        return len(cls.window_stack) > 0

    @classmethod
    def shift_down(cls):
        return cls.shift_key()

    @classmethod
    def is_window_enabled(cls):
        return cls.window_enabled
        # if len(cls.window_stack) == 0:
        #     return True
        # return cls.window_stack[-1][1]

    @classmethod
    def get_bg_color(cls, depth=None):
        if depth is None:
            depth = cls.depth
        if len(cls.bg_stack) == 0:
            return 0, 0, 0

        # Allow for negative index from end, but clamp to available range
        if depth < 0:
            depth = len(cls.bg_stack) + depth
        depth = max(0, min(depth, len(cls.bg_stack) - 1))
        return cls.bg_stack[depth][0:3]

    # Modifier state. The studio reads it off its owner glfw window (vis.window);
    # a melty app has no vis (several glfw windows, the owner window hidden), so
    # it reads imgui's io state which the active surface's input backend feeds.
    # Before: `cls.vis.window` raised AttributeError in an app, and every body that
    # asked mid-frame (the code editor's right column) was cut short there.
    @classmethod
    def shift_key(cls):
        if cls.vis is None:
            return bool(imgui.get_io().key_shift)
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @classmethod
    def ctrl_key(cls, ):
        if cls.vis is None:
            return bool(imgui.get_io().key_ctrl)
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_CONTROL) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_CONTROL) == glfw.PRESS)

    @classmethod
    def init(cls, **kwargs):


        for key, value in kwargs.items():
            setattr(cls, key, value)

        FileWatch.start()

        # Register EVERY project .py for external-change tracking (dirs on the
        # observer + code-file baselines), not just files views load - one
        # O(files) call, off-thread so startup doesn't pay for it.
        _threading.Thread(target=FileWatch.watch_project_files,
                          name="project-file-watch-warm", daemon=True).start()

        # Spin up the interactive jedi (autocomplete) server now, off-thread -
        # cold it takes seconds, and lazily that lands on the first popup.
        try:
            from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
                warm_interactive_jedi)
            warm_interactive_jedi()
        except Exception:
            pass

        cls.global_attrs["style_manager"] = getattr(cls, "style_manager", None)

        cls.annotation_mode = False
        # NOTE: do NOT pin RenderFuncs.<name> to its resolved function here
        # (the old `setattr(RenderFuncs, name, func._resolve())` loop). The
        # original version froze whatever wrapper was registered at init, so a
        # later recompile of e.g. `button` never reached RenderFuncs.button
        # call sites. _LazyRenderFunc re-resolves through render_funcs_by_name
        # on every call by design (one dict get - noise compared to a render);
        # leaving the handles in place is what makes recompiles take.


    @classmethod
    def in_annotation_mode(cls):
        """Annotation calls intercepted? — startup's global flag OR this
        thread's recompile-exec scope (annotation_scope)."""
        return cls.annotation_mode or getattr(cls._annotation_tls, "active", False)

    @classmethod
    @contextmanager
    def annotation_scope(cls):
        """Thread-local annotation mode for a recompile's exec: field
        annotations that call render funcs return carriers (annotation_track)
        instead of rendering on a non-GL thread. Other threads — including the
        render thread mid-frame — are unaffected."""
        prev = getattr(cls._annotation_tls, "active", False)
        cls._annotation_tls.active = True
        try:
            yield
        finally:
            cls._annotation_tls.active = prev

    @classmethod
    def init_ui(cls, **kwargs):
        pass
        # cls.backend.start()

    @classmethod
    def is_key_pressed(cls, key=glfw.KEY_ESCAPE):
        # A focused text editor owns the keyboard. Block all global hotkeys so
        # typing (including the editor's own Ctrl shortcuts) never leaks into
        # app-level handlers. Centralized here so call sites don't each have to
        # guard with `and Melty.text_focused_ds is None`.
        if cls.text_focused_ds is not None:
            return False
        if imgui.is_any_item_focused() or imgui.is_any_item_active():
            if not cls.ctrl_key():
                # If any item is focused or active, we don't want to capture key presses
                return False

        if key not in cls.vis.tracked_keys:
            cls.vis.tracked_keys.append(key)
            cls.vis.first_frame_keys.add(key)

        if glfw.get_key(cls.vis.window, key) == glfw.PRESS:
            if key in cls.vis.first_frame_keys:
                return True
        return False

def _register_annotated_window(cls, kwargs):
    # @window(view_func=RenderFuncs.draw_blank) hands us a _LazyRenderFunc - a
    # name placeholder, since the target isn't importable at decoration time
    # (cycles). By the time the window registers, the render func is registered,
    # so turn the placeholder into the real function now: downstream (draw_main's
    # window loop) then sees a plain function reference, resolved once, not a
    # proxy re-resolved every call. Name check avoids importing render_funcs.
    vf = kwargs.get("view_func")
    if "name" in kwargs:
        Melty.annotated_window_classes[kwargs["name"]] = (cls, kwargs)
    else:
        if type(vf).__name__ == "_LazyRenderFunc":
            real = Melty.render_funcs_by_name.get(vf.__name__)
            if real is not None:
                kwargs["view_func"] = real
        Melty.annotated_window_classes[cls.__name__] = (cls, kwargs)


set_window_registrar(_register_annotated_window)

# The RenderFuncs accessor + CodeGenerator live in render_funcs.py (its own file
# so the generator only ever rewrites that small module). Melty just owns the
# render_funcs_by_name registry the @render_func decorator populates.

register_defaults()


class Action:

    def __init__(self, trigger_condition, clear_condition, re_arm_condition=None):
        self.trigger_condition = trigger_condition
        self.clear_condition = clear_condition
        self.re_arm_condition = re_arm_condition


def drag_released(unique):
    mouse_released = imgui.is_mouse_released(0)
    if mouse_released:
        pass
    drag_released = (imgui.is_mouse_released(0) and
                     unique == Melty.triggered_actions.get('on_drag', None))
    return drag_released

class ActionType(Enum):
    CLICK = 'on_click'
    DOWN = 'on_mouse_down'
    DRAG = 'on_drag'
    DRAG_UP = 'on_drag_up'
    HOVERED = 'on_hover'
    SCROLL = 'on_scroll'

class MouseAction:
    def __init__(self, action_type: ActionType, button=0, value=None):
        self.action_type = action_type
        self.button = button
        self.value = value

from enum import Enum


def add_to_collection(collection, item, preferred_key=None):
    """
    Add an item to a collection (list or dict).
    If a dict and preferred_key is given, use it if unique; else generate_id() until unique.
    Returns:
      - None on success, or an error message (str) on failure.
    """
    try:
        if hasattr(collection, "append_to"):
            collection.append_to(item)
            return collection
        elif isinstance(collection, list):
            collection.append(item)
            return None
        elif isinstance(collection, (dict, MutableMapping)):
            if hasattr(item, 'id'):
                preferred_key = item.id
            key = preferred_key
            if key is not None and key in collection:
                key = None
            if key is None:
                key = generate_id()
            collection[key] = item

        elif hasattr(collection, '__dict__') and not isinstance(collection, (types.MappingProxyType)):
            collection = collection.__dict__
            if hasattr(item, 'id'):
                preferred_key = str(item.id)

            key = preferred_key
            if key is not None and key in collection:
                key = None
            if key is None:
                key = generate_id()
            collection[key] = item

        if hasattr(item, 'tint'):
            if item.tint is None or item.tint == (0, 0, 0):
                lighten = 0.2
                item.tint = Melty.bg_stack[-1]
                item.tint = (min(1.0, item.tint[0] + lighten),
                             min(1.0, item.tint[1] + lighten),
                             min(1.0, item.tint[2] + lighten))

        Melty.cache.invalidate_by_obj(collection)
        request_render()
    except Exception as e:
        print_stack_trace(exception=e)

    return collection


def delete_from_collection(key, collection):
    if isinstance(collection, list):
        try:
            idx = int(key)
            if 0 <= idx < len(collection):
                collection.pop(idx)
                return None
            else:
                return f"Index {idx} out of range for list of length {len(collection)}."
        except Exception as e:
            return f"Error removing index {key} from list: {e}"
    elif isinstance(collection, (dict, MutableMapping)):
        if key in collection:
            collection.pop(key)
            return None
        else:
            return f"Key {key!r} not found in dict."
    elif hasattr(collection, '__dict__'):
        collection = collection.__dict__
        if key in collection:
            collection.pop(key)
            return None
        else:
            return f"Key {key!r} not found in object's __dict__."


def _supports_reorder(mp) -> bool:
    return hasattr(mp, "reorder") and callable(getattr(mp, "reorder"))


def _compute_reordered_keys(mp, moving_key: str, anchor_key: str | None, tag: str) -> list[str]:
    keys = list(mp.keys())
    if moving_key in keys:
        keys.remove(moving_key)
    if anchor_key is not None and anchor_key in keys:
        idx = keys.index(anchor_key) + (1 if tag == "bottom" else 0)
    else:
        idx = 0 if tag == "top" else len(keys)
    keys.insert(idx, moving_key)
    return keys


def _reorder_keys_in_mapping(mp, keys: list[str]) -> bool:
    if _supports_reorder(mp):
        mp.reorder(keys)
        return True
    if isinstance(mp, dict):  # was: type(mp) is dict
        old = dict(mp)
        mp.clear()
        for k in keys:
            if k in old:
                mp[k] = old[k]
        for k, v in old.items():
            if k not in mp:
                mp[k] = v
        return True
    return False


def _insert_relative_in_mapping(mp, new_key: str, value, anchor_key: str | None, tag: str) -> bool:
    """
    Insert/ensure key and position it relative to anchor without destructive deletes.
    Returns True if positioned; False if mapping can't be safely reordered.
    """
    if new_key not in mp:
        mp[new_key] = value  # inserts at end (FolderProxy will create dir; others set value)
    keys = _compute_reordered_keys(mp, new_key, anchor_key, tag)
    return _reorder_keys_in_mapping(mp, keys)


def apply_collection_action(action: CollectionAction):
    """
    Returns:
        None on success, or an error message (str) on failure.

    Notes:
      - Uses action.source_unique / action.target_unique with resolved indices
        to infer list-unique bases and shift neighbor draw-states accordingly.
      - Records the moved/copied object's draw state in Melty.move_draw_state_pending
        as { id(obj): action.source_draw_state } for the render loop to remap.
    """

    # ---------------- helpers (no mutation) ----------------
    def _norm_tag(tag):
        if tag is None:
            return "top"
        t = str(tag).lower()
        return t if t in ("top", "bottom") else None

    def _get_existing_id(obj):
        if isinstance(obj, (dict, MutableMapping)) and "id" in obj:
            return str(obj["id"])
        maybe = getattr(obj, "id", None)
        return str(maybe) if maybe is not None else None

    def _resolve_list_index(lst, key_or_index):
        # numeric index?
        try:
            idx = int(key_or_index)
            return idx if 0 <= idx < len(lst) else None
        except Exception:
            pass
        # id string?
        needle = str(key_or_index)
        for i, el in enumerate(lst):
            if isinstance(el, (dict, MutableMapping)) and "id" in el and str(el["id"]) == needle:
                return i
            maybe = getattr(el, "id", None)
            if maybe is not None and str(maybe) == needle:
                return i
        return None

    def _supports_reorder(mp) -> bool:
        return hasattr(mp, "reorder") and callable(getattr(mp, "reorder"))

    def _looks_like_dir_value(val) -> bool:
        # Keep this narrow: FolderProxy directory value
        try:
            import FolderProxy  # or import at top
        except Exception:
            FolderProxy = ()
        return isinstance(val, FolderProxy)

    def _insert_pos_for_list(anchor_index, tag):
        return anchor_index if tag == "top" else anchor_index + 1

    def _unique_key_for_dict(d, preferred: str | None):
        if preferred and preferred not in d:
            return preferred
        gen = globals().get("generate_id")
        if not callable(gen):
            return None
        k = gen()
        while k in d:
            k = gen()
        return k

    # Draw-state of the moved/copied item itself (neighbors handled separately)
    def _record_draw_state(obj):
        try:
            if obj is None or action.source_draw_state is None:
                return
            if not hasattr(Melty, "move_draw_state_pending") or Melty.move_draw_state_pending is None:
                Melty.move_draw_state_pending = {}
            Melty.move_draw_state_pending[id(obj)] = action.source_draw_state
        except Exception:
            pass  # never break the transform

    # --- List neighbor shifting via inferred base (unique(i) = base + i) ---
    def _infer_base(known_unique, known_index):
        try:
            if isinstance(known_unique, int) and isinstance(known_index, int):
                return known_unique - known_index
        except Exception:
            pass
        return None

    def _shift_range_by_base(base: int | None, start_idx: int, end_idx: int, delta: int):
        """
        Shift draw_state_registry keys for indices [start_idx..end_idx] by `delta`,
        using unique(i) = base + i. No-ops if base is None.
        """
        if base is None or delta == 0 or start_idx > end_idx:
            return
        registry = Melty.vis.root.draw_state_registry
        # Stage moves to avoid collisions
        moves = []
        for i in range(start_idx, end_idx + 1):
            old_u = base + i
            ds = registry.get(old_u)
            if ds is not None:
                new_u = old_u + delta  # invariant: Δunique == Δindex
                moves.append((old_u, new_u, ds))
        # Remove then write
        for old_u, _, _ in moves:
            registry.pop(old_u, None)
        for _, new_u, ds in moves:
            if hasattr(ds, "unique"):
                ds.unique = new_u
            registry[new_u] = ds

    # ---------------- normalize inputs ----------------
    src_owner = action.source_collection
    dst_owner = action.target_collection
    if src_owner is None or dst_owner is None:
        return "Both source_collection and target_collection must be set on the action."

    # Capture owner types BEFORE any __dict__ coercion (for __field_defaults__)
    src_owner_type = type(src_owner)
    dst_owner_type = type(dst_owner)
    item = None
    tag = _norm_tag(action.target_tag)
    if tag is None:
        return "target_tag must be 'top' or 'bottom'."

    op = action.operation.value if isinstance(action.operation, Enum) else str(action.operation).lower()
    if op not in ("move", "copy"):
        return "operation must be OperationType.MOVE or OperationType.COPY."
    is_move = (op == "move")

    # Work on raw containers (lists or dict views of objects)
    src = src_owner
    dst = dst_owner
    if not isinstance(src, (list, dict, MutableMapping)) and hasattr(src, "__dict__"):
        src = src.__dict__
    if not isinstance(dst, (list, dict, MutableMapping)) and hasattr(dst, "__dict__"):
        dst = dst.__dict__

    same_collection = (src is dst)

    # If destination is a dict but CLASS exposes a shared __field_defaults__,
    # reorder *dst* to match class_defaults order (order-only; no insertion/rebinding).
    if (not isinstance(dst, list)
            and hasattr(dst_owner_type, "__field_defaults__")
            and type(dst) is dict):
        class_defaults = dst_owner_type.__field_defaults__
        ordered_keys = [k for k in class_defaults.keys() if k in dst]
        extra_keys = [k for k in list(dst.keys()) if k not in class_defaults]
        if ordered_keys or extra_keys:
            old = dict(dst)
            dst.clear()
            for k in ordered_keys:
                dst[k] = old[k]
            for k in extra_keys:
                dst[k] = old[k]

    # ---------------- six explicit cases ----------------

    # 1) LIST -> LIST (includes list-to-self)
    if isinstance(src, list) and isinstance(dst, list):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")

        if len(dst) == 0:
            t_idx = 0
        else:
            t_idx = _resolve_list_index(dst, action.target_key)
            if t_idx is None:
                t_idx = len(dst) - 1  # last element as anchor

        # infer bases from (unique, index)
        base_same = _infer_base(action.source_unique, s_idx) if same_collection else None
        base_src = _infer_base(action.source_unique, s_idx) if not same_collection else None
        base_dst = _infer_base(action.target_unique, t_idx) if not same_collection else None

        # capture lengths BEFORE mutation
        src_len_before = len(src)
        dst_len_before = len(dst)

        # compute insert index (adjust if same list and move across pop)
        insert_at = _insert_pos_for_list(t_idx, tag)
        if is_move and same_collection:
            base = t_idx if tag == "top" else t_idx + 1
            if s_idx < base:
                base -= 1
            insert_at = max(0, min(base, len(dst)))

        item = src[s_idx]
        if is_move and same_collection:
            popped = src.pop(s_idx)
            try:
                dst.insert(insert_at, popped)
            except Exception as e:
                src.insert(s_idx, popped)
                return f"Internal error during same-list move insert: {e}"

            # neighbors in SAME list
            if insert_at < s_idx:
                _shift_range_by_base(base_same, start_idx=insert_at, end_idx=s_idx - 1, delta=+1)
            elif insert_at > s_idx:
                _shift_range_by_base(base_same, start_idx=s_idx + 1, end_idx=insert_at, delta=-1)

            _record_draw_state(popped)

        else:
            # cross-list copy/move OR same-list copy
            try:
                dst.insert(insert_at, item)
            except Exception as e:
                return f"Internal error inserting into target list: {e}"

            # target neighbors shift right from insert_at
            _shift_range_by_base(base_dst, start_idx=insert_at, end_idx=dst_len_before - 1, delta=+1)

            if is_move:
                try:
                    src.pop(s_idx)
                except Exception as e:
                    # rollback best-effort
                    try:
                        dst.pop(insert_at)
                    except Exception:
                        pass
                    return f"Internal error removing from source after insert: {e}"

                # source neighbors collapse left after s_idx
                _shift_range_by_base(base_src, start_idx=s_idx + 1, end_idx=src_len_before - 1, delta=-1)

            _record_draw_state(item)

    # 2) DICT -> DICT (reorder or transfer)
    elif isinstance(src, (dict, MutableMapping)) and isinstance(dst, (dict, MutableMapping)):
        s_key = action.source_key
        t_key = action.target_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."

        if t_key is None and len(dst) > 0:
            t_key = next(iter(dst.keys()))

        value = src[s_key]

        if same_collection:
            # pure reorder; never use clear/pop on mappings that might have side-effects
            if not (s_key == t_key or t_key is None):
                keys = _compute_reordered_keys(dst, s_key, t_key, tag)
                ok = _reorder_keys_in_mapping(dst, keys)
                if not ok:
                    return "Cannot safely reorder this mapping without destructive deletes."
        else:
            final_key = s_key
            if s_key in dst:
                is_move = False  # collision -> copy

            # If the source owner exposes a true move, use it (duck-typed; generic)
            if is_move and hasattr(src_owner, "move_item") and callable(getattr(src_owner, "move_item")):
                try:
                    # perform the physical move; returns the final key name at dst
                    final_key = src_owner.move_item(dst_owner, s_key, new_name=s_key)
                    # position it relative to t_key without destructive deletes
                    _insert_relative_in_mapping(dst, final_key, dst[final_key], t_key, tag)
                    _record_draw_state(dst[final_key])
                    return
                except NotImplementedError:
                    pass
                except Exception as e:
                    # Fall back to safe copy semantics if hook fails
                    is_move = False

            # No move hook: do a safe insert+reorder only
            ok = _insert_relative_in_mapping(dst, final_key, value, t_key, tag)
            if not ok:
                if type(dst) is dict:
                    tmp = dict(dst)
                    tmp[final_key] = value
                    keys = _compute_reordered_keys(tmp, final_key, t_key, tag)
                    dst.clear()
                    for k in keys:
                        dst[k] = tmp[k]
                else:
                    return "Target mapping cannot be reordered safely."

            # IMPORTANT: never pop a value item unless we actually moved it
            if is_move:
                if _looks_like_dir_value(value) and (_supports_reorder(src) or _supports_reorder(dst)):
                    # Treat as copy for safety (we didn't really move on disk)
                    is_move = False
                else:
                    src.pop(s_key, None)

            _record_draw_state(value)

    # 3) DICT -> LIST (insert into list)
    elif isinstance(src, (dict, MutableMapping)) and isinstance(dst, list):
        s_key = action.source_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."
        item = src[s_key]

        dst_len_before = len(dst)
        if dst_len_before == 0:
            t_idx = 0
        else:
            t_idx = _resolve_list_index(dst, action.target_key)
            if t_idx is None:
                t_idx = len(dst) - 1
        insert_at = max(0, min(_insert_pos_for_list(t_idx, tag), len(dst)))

        base_dst = _infer_base(action.target_unique, t_idx)

        try:
            dst.insert(insert_at, item)
        except Exception as e:
            return f"Internal error inserting into list: {e}"

        # target neighbors shift right
        _shift_range_by_base(base_dst, start_idx=insert_at, end_idx=dst_len_before - 1, delta=+1)

        if is_move:
            src.pop(s_key, None)

        _record_draw_state(item)

    # 4) LIST -> DICT (remove from list)
    elif isinstance(src, list) and isinstance(dst, (dict, MutableMapping)):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")
        item = src[s_idx]

        src_len_before = len(src)
        base_src = _infer_base(action.source_unique, s_idx)

        t_anchor = action.target_key
        if t_anchor is not None and len(dst) > 0 and t_anchor not in dst:
            t_anchor = list(dst.keys())[-1]

        preferred_id = _get_existing_id(item)
        new_key = _unique_key_for_dict(dst, preferred_id)
        if new_key is None:
            return "generate_id() is not available to create a unique key for list->dict."

        ok = _insert_relative_in_mapping(dst, new_key, item, t_anchor, tag)
        if not ok:
            # Fallback for plain dicts only
            if type(dst) is dict:
                tmp = dict(dst)
                if new_key not in tmp:
                    tmp[new_key] = item
                keys = _compute_reordered_keys(tmp, new_key, t_anchor, tag)
                dst.clear()
                for k in keys:
                    dst[k] = tmp[k]
            else:
                return "Target mapping cannot be reordered safely."

        if is_move:
            try:
                src.pop(s_idx)
            except Exception as e:
                # rollback best-effort
                try:
                    if new_key in dst:
                        del dst[new_key]
                except Exception:
                    pass
                return f"Internal error removing from source list after dict insert: {e}"

            # collapse gap in source list
            _shift_range_by_base(base_src, start_idx=s_idx + 1, end_idx=src_len_before - 1, delta=-1)

        _record_draw_state(item)

    else:
        return "Unsupported collection types. Expected list or dict for both source and target."

    # Melty.cache.invalidate_all()
    # ---------------- reflect order into __field_defaults__ (order-only, in place) ----------------
    if not isinstance(dst, list) and hasattr(dst_owner_type, "__field_defaults__"):
        class_defaults = dst_owner_type.__field_defaults__
        dst_keys = list(dst.keys())
        defaults_keys = list(class_defaults.keys())

        common_in_dst_order = [k for k in dst_keys if k in class_defaults]
        defaults_only_tail = [k for k in defaults_keys if k not in dst]

        new_order = common_in_dst_order + defaults_only_tail
        if new_order != defaults_keys:
            old_vals = {k: class_defaults[k] for k in class_defaults.keys()}
            class_defaults.clear()
            for k in new_order:
                class_defaults[k] = old_vals.get(k)



    return None


# First import only: until here Core.melty is the MockMelty recorder, whose
# pre-init interactions replay onto the real class. On a hotswap reinit
# Core.melty is already the (previous) Melty class - no recorder, nothing
# to replay - and the line would AttributeError, blocking the reload.
if hasattr(Core.melty, "replay"):
    applied, skipped = Core.melty.replay(Melty)
Core.melty = Melty

class DepthState:
    def __init__(self):
        self.flow_spacing = 0.0


class MeltyState:
    def __init__(self):
        self.hover_stack = []
        self.hotkey_stack = []

        self.size_stack = []
        self.triggered_actions = {}

        self.top_event_depth = {}
        self.top_event = {}

        self.dragged_item = None
        self.dragged_tile = None
        self.max_distance = 200

        self.selected_views = {}

        self.drag_in_progress = False

        self.initial_drag_offset = (0, 0)
        self.mouse_down_pos = (0, 0)
        self.total_drag_distance = 0.0
        self.total_drag_frames = 0
        self.last_mouse_pos = None
        self.drag_delta = (0,0)

        self.nearest_drop_target = None
        self.nearest_drop_target_tag = None
        self.nearest_drop_distance = self.max_distance
        self.flow_spacing = 0.0

        self.drag_drop_target = None
        self.drag_drop_target_tag = None
        self.drag_target_key = None
        self.drag_target_collection = None
        self.drag_drop_action = CollectionAction()
        self.initial_scroll_offset = (0,0)

        self.target_distance = self.max_distance

        self.items_to_delete = []


    def check_event_value(self, unique, mouse_btn, event_type):
        if unique in self.triggered_actions:
            action = self.triggered_actions[unique]
            if action.action_type == event_type:
                return action.value
        return None


    def check_event(self, unique, mouse_btn, event_type):
        if unique in self.triggered_actions:
            action = self.triggered_actions[unique]
            if action.action_type == event_type and action.button == mouse_btn:
                return True
        return False

    def mark_event(self, unique, mouse_btn, event_type: ActionType, value=None):
        self.triggered_actions[unique] = MouseAction(event_type, mouse_btn, value)
        depth = Melty.depth
        if event_type not in self.top_event_depth or depth < self.top_event_depth[event_type]:
            self.top_event_depth[event_type] = depth
            self.top_event[event_type] = unique

    def clear_events(self, unique):
        if unique in self.triggered_actions:
            self.triggered_actions.pop(unique)




@no_save("input_value", "draw_state", "window_args")
@defaults(tint=(0.2391563206911087, 0.47928887605667114, 0.7674418687820435))
class ManagedWindow(DictConversion):
    """One registered window. A DictConversion so registered_windows can live
    on the AppModel and pickle with it — but only `name`/`hidden` persist: the
    dict's KEY ORDER is the payload (it IS the window z-order), while
    input_value / draw_state / window_args re-bind at registration and would
    drag arbitrary runtime graphs into custom.pkl."""

    def __init__(self, input_value=None, draw_state=None, window_args=None, name=None):
        super().__init__()
        self.input_value = input_value
        self.draw_state = draw_state
        self.window_args = window_args
        self.name = name
        self.hidden = False

    def unbind(self):
        """Drop the runtime bindings. Everything but name/hidden belongs to
        ONE studio run: the wrapper re-fills all three at registration, and a
        draw_state carried past a run points into the OLD root. The dispatch
        loop's closed-window skip refreshes the delete countdown on whatever
        draw_state sits here instead of running the wrapper, so a stale one
        kept the old object alive while the loaded one under the same
        registry key sat at countdown 0 and was pruned from the next save —
        closed windows came back the boot after as fresh draw_states (default
        tint / size / position, grey rows in the dock)."""
        self.input_value = None
        self.draw_state = None
        self.window_args = None
