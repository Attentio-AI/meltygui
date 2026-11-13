"""Background analysis of an editor's text, exposed as plain values.

    source = Source(text, path)
    analysis = Analysis(source)
    draw_editor(source, state=state, errors=analysis.errors, imports=analysis.imports, tints=analysis.tints)

The model follows its source by events (`Source.models`: every splice and
every landed reading calls `feed`) and wakes the views that read `errors`,
`imports` or `tints` when a result lands (`Readers`): a caller makes no call
per frame and may be a cached view.

Undefined names are the source's own (`Source.undefined`: read in Rust with
every reading of the text, see model/source_model.py). Which import
statement would bring a name is a question to the running process: the
worker answers it, once per name, and `imports` puts the answers on the
lines the source found the names on. Calls are checked by the source too
(`Source.misfits`): against the definitions in the text, and against the
signatures of what they reach in the running process, which the worker
looks up for the chains the source asks for (`Source.callees`) and pushes
as plain rows (`Source.signatures`). Syntax errors are CPython's: the worker
compiles each top-level statement of the landed reading on its own, and only
those whose text changed (`Analysis.compile`), so an edit costs the
statement it is in; until it has, `errors` shows what the source's reader is
sure of (`Source.flaws`: brackets, an open triple-quoted string). The same
pass reads, for a text that is a running module, the attributes its modules
lack (`chains`, `lacking`), again per statement. The
definition tints are the source's too (`Source.native.tints`: the tints its
symbol table states, washed over the uses of those symbols in this text);
the worker only turns its rows into marks. Given the project's roster
(model/roster_model.py) it also washes what this text uses of the tints
other files state, and the roster wakes it when one of those changes. Only an
extension's `source_diagnostics` still gets the whole text (`analyse`).
The worker also tells the source which names the running module has
(`Source.live`), again when a definition is hotswapped. Results
are swapped in as new objects and the views that read them are run again;
the editor only compares object identity, so they repaint once when they
land. `errors` (a `marks.Findings`) and `tints` carry the document version
their lines are of: the editor keeps them on their text through what was
typed since. Nothing here
runs on the render thread, and the worker shares the interpreter with it: it
works in pieces of one statement and gives way to a frame between them
(`glfw_utils.park`). Keep it so: one long call here is one long frame.
"""
import ast
import re
import threading
import weakref

from meltygui.core.rendering.gui_readers import Readers
from meltygui.core.runtime import extensions
from meltygui.core.windowing.glfw_utils import park
from meltygui.editor import marks

# Seconds the text must rest before it is analysed.
QUIET = 0.25
# Levers: a top-level statement of more lines than `SLOW` is compiled, and a
# text of more lines than `LARGE` given whole to an extension (`analyse`), only once the text rested
# `LONG` seconds: neither holds the interpreter while typing goes on.
SLOW = 2000
LARGE = 20000
LONG = 2.
# Lever: statements gone over between two looks for a frame (`park`).
PIECE = 256
# A line number inside one of CPython's messages ("on line 3", "detected at line 3").
LINE = re.compile(r'(?<= line )\d+')
# Every analysis alive: a hotswap may change the names their modules have.
LIVE = weakref.WeakSet()


class Analysis:
    def __init__(self, source, roster=None):
        self.source, self.path = source, source.path
        # The project's roster (model/roster_model.py): `tints` then also washes what this text uses of
        # the tints other files state. None: only the tints this text states itself.
        self.roster = roster if source.path else None
        self.washed, self.needs = [], None     # the source's own tint rows; what the roster's answer hung on
        source.lint = True
        self.readers = Readers()   # the views that read the results: run again when one lands
        self.found, self._tints = [], None     # the attributes the running module's modules lack, by `compile`; `tints`
        self.told = []             # what extensions report of the whole text (`analyse`)
        self.syntax = []           # syntax errors by `compile`; they and `found` are of the source text at version `checked`
        self.checked = -1
        self.compiled = {}         # top-level statement key -> (its errors, what `chains` read off it), lines from its first
        self.passed = None         # (text version, running module, statements left waiting) of the last whole pass of `compile`
        self.module = None         # the running module of the text's file, if it is one
        self.tinted = -1           # the source reading `tints` was made from
        self.merged, self.key = [], None       # `errors` as last put together, and from what
        self.fixes, self.fixed = {}, None      # `imports` likewise
        self.suggested = {}        # name -> the import statements that would bring it ([]: none), by the worker
        self.wanted = set()        # names the worker is still to look up
        self.analysed = -1         # the source version `told` belongs to
        self.fed = -1              # the version the worker was last woken for
        self.names = None          # the live module's names as last told to the source
        self.signed = {}           # chain -> the signature told to the source (None: not to be judged)
        self.resign = False        # the running process changed: every signature is looked up again
        self.seen = -1             # the source reading `feed` last looked at
        self.swapped = False       # a definition was hotswapped: the names may have changed
        self._wake = threading.Event()
        self._thread = None
        self.closed = False
        LIVE.add(self)
        source.models.add(self)
        self.feed()

    def statements(self, name):
        """The import statements that would bring `name`: [] when none or
        not looked up yet (the worker does that, then a frame is asked for)."""
        found = self.suggested.get(name)
        if found is None:
            self.wanted.add(name)
            self.wake()
        return found or []

    @property
    def errors(self):
        """[(line, message)] as a `marks.Findings`: the worker's findings
        (syntax errors first), the names the source finds undefined and the
        calls it finds not to fit; a new list whenever either changed. Its
        lines are those of the landed reading (`version`): what the worker
        found in an older text is brought forward to it (`carried`), and the
        editor brings the whole on to the text as it is. A name the running
        module has is an error only when an import would bring it."""
        self.readers.note()
        key = (id(self.found), id(self.told), id(self.syntax), self.checked, self.source.stamp, len(self.suggested))
        if key != self.key:
            at = self.source.version
            # Until the worker compiled this text, what the source's reader is sure of.
            flawed = [] if self.checked == at else [(line + 1, message) for line, message in self.source.flaws]
            names = [(line + 1, f"name '{name}' is not defined") for line, _, name, live, noted in self.source.undefined
                     if not noted and (not live or self.statements(name))]
            found = self.carried(sorted(self.syntax + self.found), self.checked) + self.carried(self.told, self.analysed)
            self.key = key
            self.merged = marks.Findings(sorted(set(found + flawed + names + self.source.misfits)), at if at >= 0 else None)
        return self.merged

    def carried(self, found, version):
        """`found` ([(file line, message)], sorted, of the text at
        `version`) on the lines of the landed reading: each finding stays
        with its line through the splices between the two. [] when they are
        no longer kept: the worker's next pass tells them again."""
        at = self.source.version
        if not found or version >= at:
            return found
        entries = self.source.since(version)
        if entries is None:
            return []
        changes = [entry[1] for entry in entries if entry[0] <= at]
        return [(line + 1, message) for line, message in marks.carried([(line - 1, message) for line, message in found], changes)]

    @property
    def tints(self):
        """The definition tints as a `Marks` set (None: none yet): what `draw_editor` takes as `tints`."""
        self.readers.note()
        return self._tints

    @property
    def imports(self):
        """{line: [import statements]} for the names the source finds
        undefined that an import would bring: what `draw_editor` takes as `imports`."""
        self.readers.note()
        key = (self.source.stamp, len(self.suggested))
        if key != self.fixed:
            found = {}
            for line, _, name, _, _ in self.source.undefined:
                for statement in self.statements(name):
                    if statement not in found.setdefault(line + 1, []):
                        found[line + 1].append(statement)
            self.fixed, self.fixes = key, {line: found[line] for line in found if found[line]}
        return self.fixes

    def feed(self):
        """Wake the worker when the text moved on since it last looked. The
        source calls this for every splice and every landed reading
        (`Source.tell`); nobody else has to."""
        version = self.source.native.version
        if version != self.fed:
            self.fed = version
            self.wake()
        # A reading landed: its statements are to compile, its calls may reach what has no signature yet,
        # and `errors` reads its names and calls: the views that read them run again.
        if self.source.stamp != self.seen:
            self.seen = self.source.stamp
            self.wake()
            self.readers.wake()

    def wake(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name='editor-analysis', daemon=True)
            self._thread.start()
        self._wake.set()

    def close(self):
        self.closed = True
        self.source.models.discard(self)
        self._wake.set()

    def _work(self):
        native = self.source.native
        while not self.closed:
            self._wake.wait()
            self._wake.clear()
            # Typing restarts the wait: analyse once the text rests.
            while self._wake.wait(QUIET):
                self._wake.clear()
            if self.closed:
                return
            self.live()
            self.sign()
            self.look_up()
            waiting = self.compile()
            self.tint()
            large = native.lines > LARGE
            if not large:
                self.analyse()
            # What is slow waits for a longer rest: a long statement to compile, a large text to go over whole.
            if (waiting or large) and not self._wake.wait(LONG) and not self.closed:
                self.compile(patient=True)
                self.analyse()

    def compile(self, patient=False):
        """What CPython and the running process say of the source's landed
        reading, one top-level statement at a time (`Source.native.tops`):
        its syntax error, and for a text that is a running module the
        attributes its modules lack. A statement is read only when its text
        is another than last time, so an edit costs the statement it is in.
        One of more than `SLOW` lines waits for `patient`. Returns whether
        some still wait."""
        native, stamp, module = self.source.native, self.source.stamp, self.module
        # A reading of the same text (a search term changed, a breakpoint was set) has the same
        # statements: nothing to go over again, unless long ones still wait for this patient pass.
        if self.passed is not None and self.passed[:2] == (self.source.version, module) and not (patient and self.passed[2]):
            return self.passed[2]
        kept, syntax, pieces, waiting = {}, [], [], False
        for at, (first, last, key) in enumerate(native.tops()):
            was = self.compiled.get(key)
            if was is None or (module is not None and was[1] is None and not was[0]):
                if last - first > SLOW and not patient:
                    waiting = True
                    continue
                text = native.span(first, last)
                found = flaws(text, self.path)
                was = self.compiled[key] = (found, chains(ast.parse(text)) if module is not None and not found else None)
                park()
            elif at % PIECE == PIECE - 1:
                park()
            kept[key] = was
            # (A statement keeps its findings by its own lines: they are told on the lines it stands on now.)
            syntax += [(first + line, LINE.sub(lambda number: str(int(number[0]) + first), message)) for line, message in was[0]]
            if was[1] is not None:
                pieces.append((first, was[1]))
        if self.source.stamp != stamp:
            # Another reading landed meanwhile: what was read is kept, the statements are gone over again.
            self.wake()
            return False
        found = lacking(pieces, module) if module is not None else []
        version, moved = self.source.version, self.checked != self.source.version
        if syntax != self.syntax or found != self.found:
            self.syntax, self.found, moved = syntax, found, True
        # (The findings first, then the version they are of: a frame reading between the two only carries them once more.)
        self.compiled, self.checked = kept, version
        self.passed = (version, module, waiting)
        if moved:
            self.readers.wake()
        return waiting

    def tint(self):
        """The definition tints of the source's landed reading, as marks (`tints`): those its own table
        states and, with a roster, those other files state for the names this text uses of them. Made
        again when a reading lands, and when the roster woke for something its last answer hung on."""
        stamp, version, roster = self.source.stamp, self.source.version, self.roster
        fresh = stamp != self.tinted
        if fresh or (self.needs is not None and roster.stale(self.needs)):
            rows = self.source.native.tints() if fresh else self.washed
            others, needs = roster.tints(self.source) if roster is not None else ([], None)
            if self.source.stamp != stamp:
                return self.wake()     # another reading landed meanwhile
            self.tinted, self.washed, self.needs = stamp, rows, needs
            if needs is not None:
                roster.watch(self, needs)
            if rows or others or self._tints:
                self._tints = washes(rows, others, version) if roster is not None else marks.tints(rows, version)
                self.readers.wake()

    def analyse(self):
        """What extensions report of the whole text (`source_diagnostics`): nothing is read without one.
        Only of a text the source's reading has landed for, so that every finding in `errors` is of a
        version the others can be brought to (typed on since: the next landing wakes the worker)."""
        native = self.source.native
        if native.version == self.analysed or native.version != self.source.version:
            return
        told = []
        if self.path and extensions.get('source_diagnostics') is not None:
            # The text and the version it is at (an edit between the two reads: again).
            version, text = native.version, self.source.text
            while version != native.version:
                version, text = native.version, self.source.text
            try:
                told = sorted(set(extensions.call('source_diagnostics', text, self.path) or ()))
            except Exception as error:
                told = [(1, f'lint failed: {error}')]
            self.analysed = version
        else:
            self.analysed = native.version
        if told != self.told:
            self.told = told
            self.readers.wake()

    def look_up(self):
        """Ask the running process which import statements would bring the names wanted."""
        from meltygui.code import code_checks
        while self.wanted:
            name = self.wanted.pop()
            try:
                self.suggested[name] = list(code_checks._suggest_import_for_path(name, self.path) or ())
            except Exception:
                self.suggested[name] = []      # a lookup must never break the analysis
            self.readers.wake()

    def live(self):
        """Tell the source the names its running module has, when they changed."""
        from meltygui.code import code_checks
        if self.swapped:
            self.suggested = {}        # what is importable may have changed too
        self.resign |= self.swapped
        self.swapped = False
        module = self.module = code_checks._module_for(self.path)
        names = frozenset(vars(module)) if module is not None else frozenset()
        if names != self.names:
            self.names, self.resign = names, True
            self.source.live(names)

    def sign(self):
        """Tell the source the signatures of what its calls reach in the
        running process: of the chains its last reading asks for, and of
        all of them again when the process changed (`resign`)."""
        from meltygui.code import code_checks
        wanted = set(self.source.native.callees()) | (set(self.signed) if self.resign else set())
        self.resign = False
        if not wanted:
            return
        module = code_checks._module_for(self.path)
        found = dict(self.signed)
        for chain in wanted:
            try:
                found[chain] = signature(chain, module)
            except Exception:
                found[chain] = None    # a lookup must never break the analysis
        if found != self.signed:
            self.signed = found
            self.source.signatures(found)


def hotswapped(live=None):
    """A definition was swapped in the running process: the modules may bind other names now."""
    for analysis in list(LIVE):
        analysis.swapped = True
        analysis.wake()


extensions.register('definition_hotswapped', hotswapped)


def washes(rows, others, version):
    """The marks of a source's tint rows (`marks.tints`) with the roster's among them: (line, column,
    characters, red, green, blue, scale). A wash keeps its scale (1: the symbol itself) as the mark's `data`."""
    from meltygui.editor.marks import Mark, Marks
    found = sorted([(*row, 1.) for row in rows] + others)
    return Marks([(line, Mark('wash', column, column + chars, (red, green, blue), scale) if chars else Mark('tint', 0, None, (red, green, blue)))
                  for line, column, chars, red, green, blue, scale in found], version)


def signature(chain, module):
    """What a call of the dotted `chain` reaches in the running process, as
    the plain rows `Source.signatures` takes: (name, 0 positional only / 1
    positional or keyword / 2 keyword only / 3 `*` / 4 `**`, it has no
    default, annotated type or ''). None when it is not to be judged. The
    chain is walked from `module`'s namespace through modules only
    (`builtins.name`: the builtin), as `code_checks._check_call_live` does."""
    import builtins
    import inspect
    import types
    from meltygui.code import code_checks
    miss = code_checks._MISS
    base, *attrs = chain.split('.')
    if base == 'builtins' and len(attrs) == 1:
        found, attrs = getattr(builtins, attrs[0], miss), []
    else:
        found = vars(module).get(base, miss) if module is not None else miss
    for attr in attrs:
        if not isinstance(found, types.ModuleType):
            return None
        found = inspect.getattr_static(found, attr, miss)
    if found is miss or not callable(found):
        return None
    spec = code_checks._object_spec(found, chain.rpartition('.')[2])
    if spec is None:
        return None
    rows = [(name, int(at >= spec.n_posonly), name in spec.required, spec.types.get(name, '')) for at, name in enumerate(spec.named)]
    rows += [(name, 2, name in spec.kw_required, spec.types.get(name, '')) for name in spec.kwonly]
    return rows + [('', 3, False, '')] * spec.has_var_pos + [('', 4, False, '')] * spec.has_var_kw


def attributes(text, path=None):
    """[(line, message)] of what the source does not read, for a whole text
    at once: the attributes the modules of its running module lack and what
    extensions report (`source_diagnostics`). The worker reads the first
    per statement (`Analysis.compile`); this is the same reading of a text
    without a `Source`."""
    from meltygui.code import code_checks
    found = list(extensions.call('source_diagnostics', text, path) or ()) if path else []
    try:
        found += lacking([(0, chains(code_checks._parse_for_analysis(text)))], code_checks._module_for(path))
    except RecursionError:
        pass
    return sorted(set(found))


def chains(tree):
    """What a parsed piece of module-level code (a top-level statement, a
    whole text) says about attributes, as plain data that is kept instead of
    the tree: (the names it binds at module level by import, those it binds
    otherwise, the dotted paths it makes exist, its chains). A chain is
    (name, ((attribute, line), ..), sure): an attribute chain read off
    `name` where only an import may bind it. `sure`: an import inside the
    piece binds it; else whether the module's other statements leave it an
    import is for `lacking`. Lines count from the piece's first (1)."""
    from meltygui.code import code_checks
    collector = code_checks._Collector()
    collector.run(tree)
    module, found = collector.module, []
    for base, attrs, scope, guarded in collector.attr_chains:
        owner = code_checks._binding_scope(scope, base.id)
        local = owner is not None and owner is not module
        if not guarded and (not local or (base.id in owner.import_binds and base.id not in owner.other_binds)):
            found.append((base.id, tuple((attr, node.lineno) for attr, node in attrs), local))
    return frozenset(module.import_binds), frozenset(module.other_binds), frozenset(collector.declared_attrs), found


def lacking(pieces, module):
    """[(line, message)] of the attributes `module`'s modules lack, sorted:
    `pieces` are (first line, what `chains` read) of the module's top-level
    statements, `module` the running module of the text (None: nothing is
    known). The chains are walked through live modules only
    (`code_checks._walk_chain`)."""
    from types import SimpleNamespace
    from meltygui.code import code_checks
    imported, others, declared = set(), set(), set()
    for at, (_, (imports, binds, paths, _)) in enumerate(pieces):
        imported |= imports
        others |= binds
        declared |= paths
        if at % PIECE == PIECE - 1:
            park()
    live = code_checks._LiveCtx(module, declared)
    # The scopes a chain's name is looked up in: the module as every statement binds it, and an import of its own.
    whole, own = code_checks._Scope('module', None), code_checks._Scope('module', None)
    whole.binds, whole.import_binds, whole.other_binds = imported | others, imported, others
    found = set()
    for at, (first, (_, _, _, rows)) in enumerate(pieces):
        for name, attrs, sure in rows:
            if sure:
                own.binds = own.import_binds = {name}
            try:
                report = code_checks._walk_chain(live, own if sure else whole, SimpleNamespace(id=name),
                                                 [(attr, SimpleNamespace(lineno=first + line)) for attr, line in attrs])[1]
            except Exception:
                report = None              # a lookup must never break the analysis
            if report is not None:
                found.add(report)
        if at % PIECE == PIECE - 1:
            park()
    return sorted(found)


def flaws(text, path=None):
    """[(line, message)] of the syntax error CPython finds in `text`, if any; lines from 1."""
    try:
        compile(text, str(path or '<editor>'), 'exec', dont_inherit=True)
    except SyntaxError as error:
        return [(error.lineno or 1, error.msg)]
    except (ValueError, RecursionError, MemoryError) as error:
        return [(1, str(error))]
    return []
