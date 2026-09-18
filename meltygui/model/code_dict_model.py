"""CodeDict: a class, function, module or package as a plain mutable dict.

    toggles = CodeDict(Toggles, write_to=(Codebase, LaunchOverride, Hotswap))
    toggles["my_dict"]["0"] = True
    toggles["SomeInnerClass"]["my_int_toggle"] = 2

    package = CodeDict(meltygui, write_to=Codebase)
    package["core"]["runtime"]["toggles"]["Toggles"]["SomeInnerClass"]["my_int_toggle"] = 2

A definition lives in several places at once, and `write_to` says which of
them an assignment reaches:

    Codebase        the source file. The edit is spliced into the original text
                    (core_syntax: untouched code stays byte-identical) and queued
                    as a pending save; the framework writes the queue to disk,
                    usually at app quit.
    Hotswap         the live object: a class attribute, a module global, a
                    dict/list held by one (mutated in place, identity kept), a
                    function's parameter default or constant local. No recompile.
    LaunchOverride  the user's launch_overrides.json: applied to the live object
                    at the next launch, the code stays as it is
                    (meltygui.core.runtime.launch_override).

Reads are plain dict reads and return the EFFECTIVE value: the live object's
value where there is one, the source's otherwise.

SHAPE. Shaped like GitProxy: a handle is a dict subclass, one child object per
key for as long as the handle lives, refreshed in place (`refresh()` returns
whether anything changed). Handles are cheap and each keeps its own
`write_to`; what they share is the `_CodeCore` of the TOP-LEVEL definition
they belong to (its parse, its address, its pending generation). A write on
one handle is mirrored into every other handle of that core at the same
path: one dict store per handle, no tree walk.

    CodeDict(Toggles.TextEditor)  is the core of Toggles at path ("TextEditor",)
    CodeDict(module)["Toggles"]   is a handle on the core of Toggles

so two CodeDicts never queue overlapping spans, and a top-level definition
queues the same Address the editor's code host uses for it (last writer
wins on that span). Before a Codebase write the core compares the file's
(mtime, PendingSave generation) with what it parsed from and re-parses from
the pending overlay when another writer moved it, so a write never reverts
somebody else's key.

Values need a source form to reach Codebase or LaunchOverride (literals,
enum members, what core_syntax.render accepts); every sink validates before
any of them writes. A mutable leaf that is not a dict or list is replaced by
assignment (`d["x"] = new`), not mutated in place.

A package's submodules are its keys; reading into one imports it.
"""
import ast
import importlib
import inspect
import os
import pkgutil
import sys
import types
import weakref

from meltygui.code import core_syntax
from meltygui.code.chain_converters import _apply_function_parse
from meltygui.code.chain_converters import _raw_function
from meltygui.code.libcst_conversion import ClassParse
from meltygui.code.libcst_conversion import CodeLine
from meltygui.code.libcst_conversion import Comment
from meltygui.code.libcst_conversion import FunctionParse
from meltygui.code.libcst_conversion import _build_src_scope
from meltygui.code.libcst_conversion import _module_scope
from meltygui.code.new_codecs import FunctionCodec
from meltygui.code.new_codecs import ModuleCodec
from meltygui.code.new_codecs import TypeCodec
from meltygui.core.melty import Melty
from meltygui.core.runtime import launch_override
from meltygui.editor.pending_save import PendingSave
from meltygui.editor.pending_save import three_way_merge

# A function's parse keeps its editable values under these two keys.
FUNCTION_SECTIONS = ("parameters", "locals")
# How far a Hotswap write repaints cached views drawn from the live object.
HOTSWAP_INVALIDATE_DEPTH = 10

# A write runs its sinks in this order whatever order write_to names them:
# the one that can still refuse (the produced source must compile) goes first,
# so a refused write has touched nothing.
SINK_ORDER = ("Codebase", "LaunchOverride", "Hotswap")

_DELETED = object()
_MISSING = object()
# (module name, top-level name or None for the module itself) -> _CodeCore
_cores = {}


class Codebase:
    """Sink: the source file, through the pending-save queue."""

    @staticmethod
    def validate(core, path, value):
        if core.address is None:
            raise TypeError(f"{core.label} has no editable source")
        if value is not _DELETED:
            _source_of(value)

    @staticmethod
    def write(core, path, value):
        core.sync()
        node = core.parse_node(path[:-1])
        # A replaced literal was already checked as an expression (validate);
        # inserted, deleted and container edits move statements around, so the
        # produced text is compiled before it may be queued.
        structural = value is _DELETED or path[-1] not in node or _is_container(value)
        if value is _DELETED:
            node.pop(path[-1], None)
        else:
            node[path[-1]] = value
        try:
            core.queue_save(check=structural)
        except Exception:
            core.reload()       # drop the edit that has no valid source form
            raise
        core.journal[tuple(path)] = value


class Hotswap:
    """Sink: the live object, in place."""

    @staticmethod
    def validate(core, path, value):
        if core.live_at(path[:-1]) is None and not _through_function(core, path):
            raise TypeError(f"{core.label} has no live object at {'.'.join(map(str, path[:-1]))}")

    @staticmethod
    def write(core, path, value):
        owner = _write_live(core.live, path, value)
        if Melty.cache is None:
            return
        for source in {id(core.live): core.live, id(owner): owner}.values():
            if isinstance(source, types.FunctionType):
                Melty.cache.invalidate_up_by_func(source, max_depth=HOTSWAP_INVALIDATE_DEPTH)
            else:
                Melty.cache.invalidate_up_by_obj(source, max_depth=HOTSWAP_INVALIDATE_DEPTH)


class LaunchOverride:
    """Sink: the user's launch overrides file."""

    @staticmethod
    def validate(core, path, value):
        if any(key in FUNCTION_SECTIONS for key in path) and _through_function(core, path):
            raise TypeError("a launch override cannot reach inside a function")
        if value is not _DELETED:
            _source_of(value)

    @staticmethod
    def write(core, path, value):
        full_path = core.module_path + tuple(path)
        if value is _DELETED:
            launch_override.clear_override(core.module_name, full_path)
            return
        default = core.source_value(path)
        launch_override.set_override(
            core.module_name, full_path, _source_of(value),
            None if default is _DELETED else _source_of(default, strict=False))


def _source_of(value, strict=True):
    """The Python expression for `value`, the text a sink persists."""
    try:
        text = core_syntax.render(value)
        ast.parse(text, mode="eval")    # render falls back to repr(): `<object at 0x..>` is not source
        return text
    except Exception as error:
        if not strict:
            return None
        raise TypeError(f"{type(value).__name__} value has no source form: {error}") from error


def _through_function(core, path):
    node = core.parse_node(())
    for key in path:
        if isinstance(node, FunctionParse):
            return True
        node = node.get(key) if isinstance(node, dict) else None
    return False


def _write_live(live, path, value):
    """Apply one write to the live object tree. Returns the object that owns
    the written key (the class, module or function), for cache invalidation."""
    owner = live
    for index, key in enumerate(path[:-1]):
        function = _raw_function(live) if not isinstance(live, (type, types.ModuleType, dict, list)) else None
        if function is not None:
            # path = (..., "parameters" | "locals", name): the function path
            # patches __defaults__ / co_consts, there is nothing to setattr.
            section, name = path[index], path[-1]
            if section in FUNCTION_SECTIONS and index == len(path) - 2 and value is not _DELETED:
                _apply_function_parse(function, {section: {name: value}})
            return function
        live = _live_member(live, key)
        if isinstance(live, (type, types.ModuleType)) or _raw_function(live) is not None:
            owner = live
    if value is _DELETED:
        launch_override.delete_at_path(live, path[-1:])
    else:
        launch_override.set_at_path(live, path[-1:], value)
    return owner


def _live_member(live, key, default=None):
    """The live object behind `key`, or `default` when there is none."""
    if isinstance(live, types.ModuleType):
        return live.__dict__.get(key, default)
    if isinstance(live, type):
        return inspect.getattr_static(live, key, default)
    if isinstance(live, dict):
        return live.get(key, default)
    return default


def _is_container(value):
    return isinstance(value, (dict, list)) and not isinstance(value, (CodeLine, Comment))


def _plain(value):
    """A handle (or a tree holding handles) as plain dicts and lists, the
    form every sink stores."""
    if isinstance(value, CodeDict) or type(value) is dict:
        return {key: _plain(item) for key, item in dict.items(value)}
    if isinstance(value, CodeList) or type(value) is list:
        return [_plain(item) for item in value]
    return value


class _CodeCore:
    """What every handle of one top-level definition (or one module's own
    globals) shares: the live object, its Address, its parse and the
    staleness stamp the parse was made from."""

    def __init__(self, live, module, top_name):
        self.live = live
        self.module = module
        self.module_name = module.__name__
        self.module_path = (top_name,) if top_name else ()
        self.label = f"{self.module_name}:{top_name}" if top_name else self.module_name
        self.codec = (ModuleCodec if top_name is None
                      else TypeCodec if isinstance(live, type) else FunctionCodec)
        self.address = None
        self.parse = None
        self.stamp = None
        # path -> value of every Codebase write still pending: what is replayed
        # when the file changed on disk under them and a text merge conflicts.
        self.journal = {}
        # path -> [weakref to each handle at that path]
        self.handles = {}
        self.load()

    def _stamp_now(self):
        path = self.address.path
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = None
        return mtime, PendingSave.pending_gen_for(path)

    def load(self):
        """(Re)resolve the address and parse the span from the pending overlay."""
        address = self.codec.resolve_address(_raw_function(self.live) or self.live)
        if address is None:
            if self.address is not None:
                # It resolved before: the file is mid-edit (does not parse). Keep
                # the last good parse; the unchanged stamp retries on the next sync.
                raise LookupError(f"the source of {self.label} cannot be resolved right now")
            self.parse = core_syntax.GeneralParse()
            return
        self.address = address
        text = str(self.codec.load(self.address))
        PendingSave.mark_load(address=self.address, codec=self.codec, data=text)
        self.parse = self._parse(text)
        self.stamp = self._stamp_now()
        return text

    def _parse(self, text):
        with _module_scope(_build_src_scope()):
            # In-process scanner: this caller waits for the result, so the
            # subinterpreter front end big files default to only adds its hop.
            return core_syntax.parse_to_dict(text, file_path=self.address.path, frontend="scan")

    def sync(self):
        """Re-parse when the file or its pending queue moved under the parse.
        Returns whether it did; handles are refreshed in place."""
        if self.address is None or self.stamp == self._stamp_now():
            return False
        self.reload()
        return True

    def reload(self):
        held = self._pending_entry()
        disk_moved = self.stamp is not None and self._stamp_now()[0] != self.stamp[0]
        if held is not None and disk_moved:
            self._rebase(*held)
        else:
            if held is None:
                self.journal.clear()        # flushed or discarded: nothing pending to replay
            self.load()
        for handle in self.live_handles():
            handle._fill()

    def _pending_entry(self):
        """(address, text, load-time text) of this span's queued edit, if any."""
        entry = PendingSave.pending_saves.get(self.address) if self.address is not None else None
        if entry is None or not isinstance(entry[1].get("data"), str):
            return None
        return self.address, entry[1]["data"], PendingSave.originals.get(self.address)

    def _rebase(self, old_address, mine, base):
        """The file changed on disk under a queued edit (code added above the
        definition moves its lines; an edit inside it changes them). The entry
        is keyed by the OLD line range, so it cannot stay: merge it onto the
        new disk text and queue the result at the new range. When the two
        sides touched the same lines, the disk wins those lines and this
        core's own writes are replayed on top (last writer wins per key)."""
        PendingSave.discard_entry_for(old_address)
        try:
            theirs = self.load()
        except Exception:
            # Unreadable right now (mid-edit): put the edit back, retry next sync.
            PendingSave.pending_saves[old_address] = (self.codec, {"data": mine})
            if base is not None:
                PendingSave.originals[old_address] = base
            raise
        merged = three_way_merge(base, mine, theirs) if isinstance(base, str) else None
        if merged is not None:
            if merged == theirs:
                self.journal.clear()        # the disk already holds everything
                return
            self.parse = self._parse(merged)
            PendingSave.queue_save(address=self.address, codec=self.codec, data=merged)
        else:
            for path, value in list(self.journal.items()):
                try:
                    node = self.parse_node(path[:-1])
                except (KeyError, TypeError, IndexError):
                    del self.journal[path]      # its holder is gone from the new source
                    continue
                if value is _DELETED:
                    node.pop(path[-1], None)
                else:
                    node[path[-1]] = value
            self.queue_save(check=True)
        self.stamp = self._stamp_now()

    def queue_save(self, check=False):
        text = core_syntax.general_parse_to_str(self.parse, check=check)
        PendingSave.queue_save(address=self.address, codec=self.codec, data=text)
        self.stamp = self._stamp_now()      # our own bump is not somebody else's edit

    def parse_node(self, path):
        node = self.parse.get(self.module_path[0]) if self.module_path else self.parse
        for key in path:
            node = node[key]
        return node

    def source_value(self, path):
        try:
            return self.parse_node(path)
        except (KeyError, TypeError, IndexError):
            return _DELETED

    def live_at(self, path):
        live = self.live
        for key in path:
            if live is None or _raw_function(live) is not None:
                return None     # nothing to step into below a function
            live = _live_member(live, key)
        return live

    def register(self, handle):
        self.handles.setdefault(handle._path, []).append(weakref.ref(handle))

    def handles_at(self, path):
        refs = self.handles.get(path)
        if not refs:
            return []
        alive = [handle for handle in (ref() for ref in refs) if handle is not None]
        if len(alive) != len(refs):
            self.handles[path] = [weakref.ref(handle) for handle in alive]
        return alive

    def live_handles(self):
        return [handle for path in list(self.handles) for handle in self.handles_at(path)]

    def write(self, handle, key, value):
        """One assignment (or deletion) from `handle`: validate against all of
        its sinks, write to them, mirror into every handle at the path."""
        path = handle._path + (key,)
        value = _plain(value)
        sinks = sorted(handle._write_to, key=lambda sink: SINK_ORDER.index(sink.__name__))
        for sink in sinks:
            sink.validate(self, path, value)
        for sink in sinks:
            sink.write(self, path, value)
        for other in self.handles_at(handle._path):
            other._mirror(key, value)


def _core_for(live):
    """(core, path inside it) for a class, function or module."""
    if isinstance(live, types.ModuleType):
        module, top_name, path = live, None, ()
    else:
        target = _raw_function(live) if not isinstance(live, type) else live
        if target is None:
            raise TypeError(f"CodeDict cannot mirror {live!r}")
        parts = target.__qualname__.split(".")
        if "<locals>" in parts:
            raise TypeError(f"{target.__qualname__} is defined inside a function")
        module = sys.modules[target.__module__]
        top_name, path = parts[0], tuple(parts[1:])
        live = module.__dict__.get(top_name, target)
    key = (module.__name__, top_name)
    core = _cores.get(key)
    # A hotswap keeps identity; a name rebound to a NEW object gets a fresh core.
    if core is None or core.live is not live:
        core = _cores[key] = _CodeCore(live, module, top_name)
    return core, path


class CodeDict(dict):
    """See the module docstring. `source` is a class, function, module or
    package; `write_to` one sink or a tuple of them."""

    def __init__(self, source, write_to=(), *, list_package=True):
        super().__init__()
        self._write_to = tuple(write_to) if isinstance(write_to, (tuple, list)) else (write_to,)
        self._package = None
        self._pending_source = None
        self._list_package = list_package
        self._fill_changed = False
        if list_package and isinstance(source, types.ModuleType) and hasattr(source, "__path__"):
            self._core, self._path = None, ()
            self._package = source
            self._fill_package()
            return
        self._core, self._path = _core_for(source)
        self._core.register(self)
        self._fill()

    @classmethod
    def _at(cls, core, path, write_to, data=None):
        """A child handle at `path` of `core`, filled from the parse or (a
        plain value with no parse yet) from `data`."""
        handle = cls.__new__(cls)
        dict.__init__(handle)
        handle._write_to, handle._package, handle._pending_source = write_to, None, None
        handle._list_package, handle._fill_changed = True, False
        handle._core, handle._path = core, path
        core.register(handle)
        handle._fill(data)
        return handle

    @classmethod
    def _lazy(cls, source_loader, write_to, list_package=True):
        """A handle that becomes `CodeDict(source_loader())` on first read."""
        handle = cls.__new__(cls)
        dict.__init__(handle)
        handle._write_to, handle._package = write_to, None
        handle._list_package, handle._fill_changed = list_package, False
        handle._core, handle._path = None, ()
        handle._pending_source = source_loader
        return handle

    def _ensure(self):
        loader = self._pending_source
        if loader is not None:
            self._pending_source = None
            CodeDict.__init__(self, loader(), self._write_to, list_package=self._list_package)

    # -- filling ---------------------------------------------------------

    def _fill_package(self):
        package = self._package
        for info in pkgutil.iter_modules(package.__path__):
            name = f"{package.__name__}.{info.name}"
            dict.__setitem__(self, info.name, CodeDict._lazy(
                lambda name=name: sys.modules.get(name) or importlib.import_module(name),
                self._write_to))
        # The package's own __init__ body, as a module (not this listing again).
        dict.__setitem__(self, "__init__", CodeDict._lazy(lambda: package, self._write_to,
                                                          list_package=False))

    def _fill(self, data=None):
        """Bring this handle's content in line with the parse and the live
        object, in place, keeping the child handle of every surviving key.
        Returns whether anything changed."""
        core = self._core
        live = core.live_at(self._path)
        if data is None:
            data = core.source_value(self._path)
            if not isinstance(data, dict):
                # No source to mirror (an installed app, library code): the live class.
                data = _live_dict(live) if isinstance(live, type) else {}
        if isinstance(live, dict) and not isinstance(data, core_syntax.GeneralParse):
            data = live         # a plain dict value: the live content is the effective one
        before = dict.copy(self)
        fresh = {}
        self._fill_changed = False
        for key, value in data.items():
            if key == core_syntax.ORIGIN_KEY:
                continue
            fresh[key] = self._child(key, value, live, before.get(key))
        dict.clear(self)
        dict.update(self, fresh)
        return self._fill_changed or before != fresh

    def _child(self, key, value, live, existing):
        core, path = self._core, self._path + (key,)
        member = _live_member(live, key, _MISSING)
        if core.module_path == () and isinstance(value, (ClassParse, FunctionParse)) \
                and _defined_in(member, core.module):
            # A top-level definition is its own core (its own span Address).
            if isinstance(existing, CodeDict) and existing._core is not core:
                self._fill_changed |= existing.refresh()
                return existing
            return CodeDict(member, self._write_to)
        if isinstance(value, dict) and _is_container(value):
            if isinstance(existing, CodeDict) and existing._core is core:
                self._fill_changed |= existing._fill(
                    None if isinstance(value, core_syntax.GeneralParse) else value)
                return existing
            return CodeDict._at(core, path, self._write_to,
                                None if isinstance(value, core_syntax.GeneralParse) else value)
        if _is_container(value):
            items = member if isinstance(member, list) else value
            if isinstance(existing, CodeList):
                self._fill_changed |= list(existing) != list(items)
                list.__setitem__(existing, slice(None), items)
                return existing
            return CodeList(items, self, key)
        if isinstance(live, (type, types.ModuleType)) and _is_data(member):
            return getattr(live, key)
        return value

    def _mirror(self, key, value):
        """Another handle (or this one) wrote `key`: show it here."""
        if value is _DELETED:
            dict.pop(self, key, None)
            return
        dict.__setitem__(self, key, self._child_of_value(key, value, dict.get(self, key)))

    def _child_of_value(self, key, value, existing):
        if isinstance(value, dict):
            if isinstance(existing, CodeDict) and existing._core is self._core:
                existing._fill(value)
                return existing
            return CodeDict._at(self._core, self._path + (key,), self._write_to, value)
        if isinstance(value, list):
            if isinstance(existing, CodeList):
                list.__setitem__(existing, slice(None), value)
                return existing
            return CodeList(value, self, key)
        return value

    def refresh(self):
        """Re-read the source (when it moved) and the live values. Returns
        whether this handle changed."""
        self._ensure()
        if self._core is None:
            return False
        if self._core.sync():
            return True
        return self._fill()

    # -- writes ----------------------------------------------------------

    def __setitem__(self, key, value):
        self._ensure()
        if self._core is None:
            raise TypeError("a package's keys are its submodules; assign inside one")
        if isinstance(value, (CodeDict, CodeList)) and dict.get(self, key) is value:
            # The render contract: a view mutates a child in place and its
            # parent re-assigns the same object. The child already wrote.
            return
        self._core.write(self, key, value)

    def __delitem__(self, key):
        self._ensure()
        if key not in self:
            raise KeyError(key)
        self._core.write(self, key, _DELETED)

    def update(self, other=(), /, **kwargs):
        for key, value in dict(other, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def popitem(self):
        key = next(reversed(self))
        return key, self.pop(key)

    def clear(self):
        for key in list(self):
            del self[key]

    def __ior__(self, other):
        self.update(other)
        return self

    # -- reads (plain dict reads once a lazy handle has loaded) ------------

    def __getitem__(self, key):
        self._ensure()
        return dict.__getitem__(self, key)

    def __iter__(self):
        self._ensure()
        return dict.__iter__(self)

    def __len__(self):
        self._ensure()
        return dict.__len__(self)

    def __contains__(self, key):
        self._ensure()
        return dict.__contains__(self, key)

    def __eq__(self, other):
        self._ensure()
        return dict.__eq__(self, other)

    def __repr__(self):
        if self._pending_source is not None:
            return "CodeDict(<not loaded>)"
        return dict.__repr__(self)

    def get(self, key, default=None):
        self._ensure()
        return dict.get(self, key, default)

    def keys(self):
        self._ensure()
        return dict.keys(self)

    def values(self):
        self._ensure()
        return dict.values(self)

    def items(self):
        self._ensure()
        return dict.items(self)


class CodeList(list):
    """A list value inside a CodeDict. Any mutation is one write of the whole
    list at its key, through the owning handle's sinks."""

    def __init__(self, items, owner, key):
        super().__init__(_plain(item) for item in items)
        self._owner = weakref.ref(owner)
        self._key = key

    def _write(self, mutate):
        updated = list(self)
        result = mutate(updated)
        owner = self._owner()
        if owner is None:
            raise ReferenceError("the CodeDict holding this list is gone")
        owner[self._key] = updated
        return result

    def __setitem__(self, index, value):
        self._write(lambda items: items.__setitem__(index, value))

    def __delitem__(self, index):
        self._write(lambda items: items.__delitem__(index))

    def append(self, value):
        self._write(lambda items: items.append(value))

    def extend(self, values):
        self._write(lambda items: items.extend(values))

    def insert(self, index, value):
        self._write(lambda items: items.insert(index, value))

    def pop(self, index=-1):
        return self._write(lambda items: items.pop(index))

    def remove(self, value):
        self._write(lambda items: items.remove(value))

    def clear(self):
        self._write(lambda items: items.clear())

    def sort(self, *, key=None, reverse=False):
        self._write(lambda items: items.sort(key=key, reverse=reverse))

    def reverse(self):
        self._write(lambda items: items.reverse())

    def __iadd__(self, values):
        self.extend(values)
        return self


def _defined_in(member, module):
    target = member if isinstance(member, type) else _raw_function(member)
    return target is not None and getattr(target, "__module__", None) == module.__name__ \
        and "." not in target.__qualname__


def _live_dict(cls):
    """A class's data attributes and nested classes as plain nested dicts."""
    return {key: _live_dict(value) if isinstance(value, type) else value
            for key, value in vars(cls).items()
            if not key.startswith("__") and (isinstance(value, type) or _is_data(value))}


def _is_data(member):
    """Whether a live attribute is a plain value (what the dict shows), not a
    function, descriptor, class or module."""
    return member is not _MISSING and not isinstance(
        member, (types.FunctionType, staticmethod, classmethod, property, type, types.ModuleType))
