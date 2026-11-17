# Native editor and the Rust `Source`: design and state of the port

State as of 2026-11-11. `draw_editor` is the code and text editor rebuilt as
native `@gui` views ([the Rust `@gui` host](GUI_PROTOTYPE.md)); `Source` is the
Rust model it reads its text and code facts from. Together they are meant to
replace `draw_text` (`meltygui/view/text_view.py`) and, for code files, the code
hosts behind it.

**Status in one paragraph.** The architecture is in place and measured: a frame
costs the same at 100,000 lines as at 1,000. Editing, colouring, folds, inline
widgets, undo on the framework's stack, lint, tints, completion, search with
its find bar (Ctrl+F), go to definition and usages (Ctrl+B), breakpoints, spell
check, several panes on one file and the cross-file roster work and are tested. It is not the editor of the Melty code editor yet:
`meltygui_pro/editor/code_editor.py` still calls `draw_text`, a list of
`draw_text` features is not ported ([below](#not-ported-from-draw_text)), and
only Linux with OpenGL has been built and run ([platforms](#platforms-and-packaging)).

## Run it

```sh
.venv/bin/python tools/build_source.py          # the `Source` crate: once, and after every change to it
.venv/bin/python tools/build_gui_prototype.py   # the `@gui` host crate, likewise
.venv/bin/python examples/gui_editor.py [file.py]
.venv/bin/python examples/gui_editor_roster.py  # tints, lookups and Ctrl+B across files
.venv/bin/python examples/gui_source.py                # `Source(Toggles)`: it edits the repository's own files
.venv/bin/python examples/gui_source_side_by_side.py   # `Source(text, path)` beside `Source(thing)`, on a scratch copy
```

`gui_editor.py` has a toolbar for what needs a caller (run with live values, a
debugger line, a search field of the caller's own) and a Split checkbox that shows the same source in a
second pane. A running app keeps the extension it loaded: restart it after a
build.

## The parts

```text
draw_editor (view/editor_view.py)      keyboard, pointer, caret, popovers
  pane                                 the scrolling viewport: declares the blocks near it
    block                              paints about 32 lines; runs again only when its stamp changes
      @inline widgets                  views on tokens and live values
Editor (editor/editor.py)              one session: caret, rows, marks, what the view declares
  Document (editor/document.py)        the session's face on the text; holds none of it
Source (model/source_model.py)         the text, its version, its splices; facts by line
  native/source (Rust)                 chunks, lexer, bindings, lint, tables, the reading thread
  Tree, Branch (model/tree_model.py)   `source.tree`: the code as keys and values, for `draw_any`
Scope (model/source_scope_model.py)    `Source(Toggles)`: the code of a class, a function, a module
  File (model/source_file_model.py)    a Source whose text is a file's: one per path
    native/source/src/file.rs          the io thread: load, write behind, follow the disk
  Fixed (model/source_version_model.py)  the code at launch or at a git commit, read only
Analysis (model/editor_model.py)       what only the running process knows: compile, imports, signatures
Roster (model/roster_model.py)         the project's symbols across files, in Rust
Spelling (model/spell_model.py)        spell check as a decoration
```

| Module | Role | Lever |
| --- | --- | --- |
| `view/editor_view.py` | `draw_editor`, `pane`, `block`, painters, built-in inline widgets | `paint`, `SHAPES`, `@inline` |
| `view/icon_picker_view.py` | The icon glyph chip and its picker | |
| `state/editor_state.py` | `EditorState` (caret, collapsed folds, viewport snapshot), `LOADING` | |
| `editor/editor.py` | `Editor` session, `Pane` handle | `sync`, `land`, `window`, `put` |
| `editor/document.py` | Lines, columns, offsets, caret, `splice` | `BLOCK` (the crate's `LINES`) |
| `editor/keys.py` | `(key, modifiers)` to commands; fold chords | `@bind` |
| `editor/edit.py` | Edit commands over a `Document` | one function per command |
| `editor/history.py` | Undo steps on `UndoManager.stack` | |
| `editor/marks.py` | `Marks`, `Mark`, `Hits`, producers | a producer per decoration |
| `editor/rows.py` | Face over the Rust `Rows`; `Numbers` (buffer line to file line) | |
| `editor/syntax.py` | Colour runs cached by line text; `warm` loads the helpers off-thread | |
| `editor/inline.py` | `@inline(kind)` registry: `lead`, `trail`, `solid`, `caret` | |
| `editor/suggest.py` | Suggestions, snippet stops, signature hint, import fix; `Menu` | |
| `editor/lookup.py` | Ctrl+B: `seek` (the places a name leads to, on a pool thread), `Lookup` (the press, the usage picker); Ctrl+Shift+B: `external` | `MOST`, `WIDE`, `external` |
| `editor/find.py` | Search: `Find` (the find bar's field, the current hit, next and previous), `Search` (what a caller reads) | `Find.typed`, `LONGEST` |
| `model/source_model.py` | `Source` | a fact is a method here plus one in `lib.rs` |
| `model/tree_model.py` | `Source.tree`: `Tree`, `Branch`, `Code` ([below](#sourcetree-the-code-as-keys-and-values)) | `spell`, `QUIET`, `INLINE` |
| `model/source_scope_model.py` | `Scope`: what `Source(thing)` returns, the interface ([below](#sourcething-the-code-is-the-file)) | `where`, `at=` |
| `model/source_file_model.py` | `File`, `opened(path)`, `FILES`: the one text of a file, the io under the interface | `framing`; the disk levers are in `file.rs` |
| `model/source_version_model.py` | `Fixed`, `version(path, at)`, `capture`: launch and git versions | `LARGEST`, `SLACK`, `version` |
| `model/editor_model.py` | `Analysis` worker | `QUIET`, `SLOW`, `LARGE`, `LONG` |
| `model/roster_model.py` | `Roster`, `project(path)` | |
| `model/spell_model.py` | `Spelling` | `QUIET`, `SLICE` |
| `native/source` | The Rust crate: see [its README](../native/source/README.md) | |

Every module's docstring says how to change it.

## Using `draw_editor`

```python
source = Source(text, path)                       # a code file; a str is for small fields
roster = project(path)                            # the project's symbols: one per project, kept by Melty
analysis = Analysis(source, roster=roster)        # optional models, each a plain value source
spelling = Spelling(source)

changed, source = draw_editor(source, state=state, path=path,
                              errors=analysis.errors, imports=analysis.imports, tints=analysis.tints,
                              breakpoints=breakpoints, marks={'spelling': spelling.marks},
                              roster=roster, on_open=open_place)   # Ctrl+B across files
```

- **A code file is passed as a `Source`.** It is edited in place and returned, so
  no edit copies the text and the caller's models read the same facts. A `str`
  is joined again for every returned edit (at 100k lines: 8.3 ms to mount and
  2.4 ms a keystroke, against 3.9 and 1.2). A `str` is the caller's: pass the
  returned text back to keep an edit. A caller that keeps passing the same text
  sees it again a few frames after each edit, as with any `@gui` view
  ([GUI prototype](GUI_PROTOTYPE.md#a-view-shows-what-its-caller-passes)).
- **Everything shown besides the text is a plain value**, read again only when
  the object passed changes: `errors`, `imports`, `tints`, `live`, `breakpoints`,
  `paused`, `marks`, `folds`, `search`, `ghost`. A retained view does not see an
  in-place change of its argument: pass another object.
- **The models need no call per frame.** `Analysis` and `Spelling` hear of
  every splice and every landed reading from the source (`Source.models`), and
  a result that lands runs the view that read it again (`Readers`,
  [GUI prototype](GUI_PROTOTYPE.md#results-from-other-threads)). The caller
  may be a cached view: an `@os_window` root without `use_cache=False` works.
  One call is left: `spelling.feed()` after `enable_spell_check` was switched,
  since no event tells the model of the setting.
- **`errors` are `[(line, message)]`.** A plain list is of the text as it is.
  A model whose findings were read from an older text gives a
  `marks.Findings`, a list with the `version` its lines are of
  (`Analysis.errors` does): the editor brings them forward over what was typed
  since, so an error never stands on the line number it had before a line
  break. An error shows as an underline and a chip in place of its line
  number; a click on the chip opens the message and its quick fixes in a
  little window beside the chip, above the error's line (below it on the first
  rows); another click, Escape, a press elsewhere or the error's end closes it. Without a
  number column (`line_numbers=False`, a single line field) the message
  stands after the line instead.
- **Breakpoints are keyed by source site, never by line**:
  `{site key: {'enabled': bool}}`, the shape file metadata keeps. A gutter click
  toggles the statement on that line by its key.
- **Lines in parameters are file lines.** `first_line` or a `line_numbers` list
  maps the buffer onto its file (a part of a file, a diff).
- **`state.pane`** is the caller's handle on the running editor: `jump`,
  `select`, `focus`, and geometry (`row`, `line`, `y`, `scroll`).
- **Search needs no caller code.** Ctrl+F opens the editor's own find bar. A
  caller with a field of its own passes its term as `search=`, steps with
  `state.pane.find(1)` / `find(-1)` and reads `state.pane.search`: `(term,
  index, count)`, the current hit counted from 1 (0: none).
- **Ctrl+B needs nothing for one file.** `roster=` makes its places the
  project's. The editor opens no file: a place outside the buffer is handed to
  `on_open(path, line, column)` (a file line, a column from 0), where the
  caller jumps in the pane that shows the file (`state.pane.jump(line,
  column=column)`, `focus()`) or opens it. Without `on_open` such places are
  listed in the picker and picking one only flashes the name. Pass the same
  callable every frame: the host compares callables by identity, so a bound
  method taken anew each frame (`on_open=model.open`) runs the editor's body
  every frame (measured: 50 of 50 idle frames; the same holds for `on_submit`).
- **`LOADING`** as the value paints the rows the last session showed, from the
  state's snapshot, until the text arrives.
- A decoration of your own is a `Marks` set under a name in `marks=`. It may be
  built on any thread; the editor keeps it on its text through later edits.

## `Source`

The text of a file is the only state. Everything else is a value read from one
version of it.

- **Text.** Shared immutable chunks of 32 lines, the editor's blocks. An edit
  replaces the chunks it touches and costs those, whatever the file size.
- **One way to change it.** `source.splice(low, high, new, by=session)`, for an
  editor and for anyone else. The source owns the version (`head`) and the log
  of the last 512 splices (`since(version)`).
- **Many sessions.** Editors register in `source.sessions`. Each follows the
  splices it did not make (`Editor.sync`: rows, marks, and its caret stays on
  its character) and takes its own folds and search hits out of one reading
  (`Editor.land`). Undo steps belong to the text: a step replays on the source
  even after the session that made it closed.
- **Readings.** `ask` hands a snapshot to the source's thread, which reads the
  facts without the interpreter lock and wakes each session's view through a C
  handle of the frame loop. The newest request wins; a burst of edits is read
  once. Landing costs nothing per fact: they stay in Rust and cross where they
  are asked for.
- **Facts**, asked by line range or by key: fold ranges, definitions, names in
  sight, bindings, references, calls, statements, parameters, undefined names,
  call misfits, syntax flaws, the file's symbol table with its tints, lookup
  (use to definition, definition to uses), source sites, live-view spans.
- **What the running process adds** is pushed in as plain rows: `live(names)`
  (the module's names) and `signatures(specs)` (what calls reach outside the
  text).

`Source(text, path)` is a text of its own: the caller reads the file and
writes it. `Source(thing)` is the code of a class, a function or a module, with
the file io done for the caller ([next section](#sourcething-the-code-is-the-file)).

Decisions behind it (Lukas, 2026-11-11): Rust is a dependency everywhere, iOS
included. The boundary is fact tables, never a syntax tree, so the parser can be
replaced; it is home-built and tolerant. Real syntax errors stay CPython's
`compile`, off the render thread. Views take plain values. For stacked
decorators the nearest one's tint wins.

## `Source(thing)`: the code is the file

A prototype (2026-11-17, by Claude; **Lukas has not reviewed it**), beside
`Source(text, path)`, which is unchanged. What Lukas asked for is in
[GUI requirements, section 11](GUI_REQUIREMENTS.md#11-the-editor-and-source).

```python
toggles = Source(Toggles)              # a class, a function, a method, a module, or a Path
draw_any(toggles.tree)                 # a class's body, a function's parameters and locals, a module's names
toggles.tree['show_fps'] = False       # the file on disk says so: nothing to save
toggles.text                           # its source; set it and the file changes
draw_editor(toggles.file, ...)         # the whole file's `Source` (a `File`): sessions, facts, the tree

Source(Toggles, at='launch')           # the code as it was when the app started
Source(Toggles, at='HEAD~1')           # at a git commit; both read only
```

`examples/gui_source.py` is the usage code as Lukas wrote it;
`examples/gui_source_side_by_side.py` shows both forms on one scratch file.

Two things are kept apart, as he asked: the interface and what makes it fast.

**The interface** (`Scope`, source_scope_model.py). `Source(thing)` is the
code: no load, no save, no dirty flag, no pending text.

- **One text per file.** A scope is a light handle: the file's text (`File`,
  one per real path, in `source_file_model.FILES`) and the key path of the
  thing in that file's tree (`where`). `Source(Toggles)` and `Source` of the
  module that defines `Toggles` are the same text, the same reading and the same
  `Branch` objects, so an edit through one shows in the other.
- **What can be passed.** A module is its file. A class or function is the path
  of its `__qualname__` (`Knobs.scale`; a nested function under its parent's
  `locals`). A decorated function is the function it wraps (`__wrapped__`), a
  bound method its function, a property its getter. Where a name is defined
  twice, the last definition. Something without a Python source file (built in,
  typed at a prompt, a `partial`) raises `TypeError`.
- **`scope.tree`** is the `Branch` at that path. A scope whose code is gone (the
  class was renamed in the file) is empty and comes back when the code does.
- **A str is still a text.** `Source('x = 1\n', path)` is what it was; only a
  first argument that is no str takes the new path (`Source.__new__`).
  `Source(thing)` returns a `Scope`, which is not an instance of `Source`: pass
  `scope.file` where a `Source` is wanted (`draw_editor`).
- **Versions** are `at=`: `'disk'` (the default), `'launch'`, or a git revision.
  A version that is not the file is a `Fixed`: a write to it is ignored.

**What makes it fast** (`File`, source_file_model.py, and
`native/source/src/file.rs`). One Rust thread, `melty-file`, does the io of
every file and never holds the interpreter lock. It keeps its own copy of each
text's chunk list, which an edit follows by the chunks it replaced.

- **Load.** The constructor reads nothing. The io thread reads the file, cuts
  it into chunks and reads its facts with the tree. A view never waits: it draws
  what is there (an empty tree on the first frame) and the io thread wakes the
  frame loop through the C wake handle when the text and its reading arrived.
  Code outside a frame that asks for `.tree` or `.text` waits for the io thread.
- **Write.** Every splice follows to the file: the whole text to
  `<name>.<pid>.tmp` beside it, renamed over it, no fsync, the file's mode
  kept. There is no timer. Edits made while one write runs are one write of the
  newest text. The compiled copies in `__pycache__` are removed after each
  write, since Python tells a stale one by second and size and a dragged digit
  keeps both. At exit the last write is waited for.
- **Follow.** One inotify instance watches the folders of the open files
  (close after write, and rename onto). A changed file is read on the io
  thread and compared with its copy from both ends; what differs crosses as one
  splice, which every session and tree follows like any edit. A write of our
  own is known by the stamp taken as it was written (mtime, size, inode), never
  by its text.
- **The file wins.** Before a write the file's stamp must be the one last read
  or written. When it is not, the file is read instead and the text takes it,
  over an edit that was not written yet.
- **Line endings and a UTF-8 byte order mark stay** as the file has them. A
  file that is no UTF-8 is shown and never written; a read-only file is said
  on stderr and left alone (`file.error`).

**Versions** (source_version_model.py). `at='launch'` reads the working state
kept when the first `Source(thing)` of a repository was made: a git tree written
from a copy of the index with its objects in a temp folder (`capture`), nothing
in the repository. A file unchanged since (ctime) is read from disk. `at=<a
revision>` is `git cat-file blob <revision>:<path>`. Git runs on a Python
thread through `posix_spawn`.

### Cost of `Source(thing)`

Measured 2026-11-17, one process, headless, lines of 85 characters, medians
(`File` is the text behind `Source(thing)`):

| On the render thread | 1,000 lines | 10,000 lines | 100,000 lines |
| --- | --- | --- | --- |
| `str` slice of one line | 57 ns | 64 ns | 64 ns |
| one line of a `Source` / of a `File` | 114 / 112 ns | 124 / 124 ns | 130 / 115 ns |
| `str` slice of 32 lines | 106 ns | 116 ns | 114 ns |
| 32 lines of a `Source` / of a `File` | 345 / 364 ns | 380 / 365 ns | 390 / 389 ns |
| `source.text` (a copy each time) | 5.9 us | 57 us | 1.24 ms |
| `file.text` (one str kept per version) | 126 ns | 127 ns | 126 ns |
| `file.arrive()` when nothing came (one flag) | 47 ns | 48 ns | 49 ns |
| `tree[key]` of a `Source` / of a `File` | 553 / 612 ns | 543 / 615 ns | 548 / 618 ns |
| one character replaced in a `str` | 3.2 us | 35 us | 0.65 ms |
| `splice` of one character, no tree: `Source` / `File` | 5.7 / 6.4 us | 5.7 / 6.3 us | 5.6 / 6.4 us |
| the same with a tree (each asks for a reading) | 9.5 / 12.4 us | 10.9 / 11.6 us | 26.2 / 25.1 us |
| `tree[key] = value`: `Source` / `File` | 13.3 / 17.1 us | 12.8 / 15.5 us | 29.7 / 30.6 us |

| Off the render thread | 1,000 lines | 10,000 lines | 100,000 lines |
| --- | --- | --- | --- |
| an edit until it is in the file | 0.10 ms | 0.24 ms | 3.0 ms |
| the same while the disk wrote back 1 GB of earlier runs | 0.5 ms | 6 ms | 51 ms |
| written during a 3 s drag (an edit a frame, 360 edits) | 31 MB | 306 MB | 3,060 MB |

An edit through a `File` costs the render thread 0.6 to 0.8 us more than one
through a `Source` (the chunk handed to the io thread), a tree write 1 to 4 us
more. **Every edit is one write of the whole file**: a write is faster than a
frame, so edits rarely pile up, and a drag in a file of 8.5 MB writes 1 GB a
second to the page cache. The lever for that, if it is wanted, is a pause
between two writes of one file in `work` (file.rs), in proportion to what the
last write took. Launch of `examples/gui_source.py` against the same file
without its three sources, and frame times while a value is dragged, are in the
[requirements row](GUI_REQUIREMENTS.md#11-the-editor-and-source).

### Not there yet

- **The verbatim usage code shows only its first tree.** A view called without a
  size fills its parent, so `draw_any(toggles_source.tree)` takes the window and
  the two calls after it have no room. Three plain dicts drawn the same way
  behave the same: it is the layout rule, not `Source`. With a `height=` on
  each call all three show.
- **'running'**, the code being executed, is not built (`at='running'` raises).
  Nothing records the text a module was compiled from. The one place every
  hotswap passes is `_announce_hotswap` (`code/file_converters.py`), which
  would have to be given the file and the source; a function or class swap
  knows only its span, which has to be spliced into the text kept for the file.
  No hotswap runs in a plain `@os_window` app today.
- **Another worktree** is `Source(Path(...))` of the file there; there is no
  spelling that maps a class to its file in another worktree.
- **No conflict view.** An edit lost to a file that changed under it is not
  offered back. A change from outside that arrives while every frame makes an
  edit (a drag) waits for a frame without one when the file is large.
- **A file's text is kept for the process**: nothing closes a `File`.
- The launch capture starts with the first `Source(thing)`, not with the app.
  A file ignored by git, or over `LARGEST` (1 MB) when it differed from the
  index, has no launch version once it changed; a file outside a repository has
  none.
- A definition under an `if` or `try` is not found by its qualified name; the
  decorators above a definition are not part of `scope.text`.
- Linux only for following the disk (`watch` in file.rs); elsewhere changes by
  other programs are not seen.
- The legacy stack does not know about it: see Known gaps.

## `Source.tree`: the code as keys and values

A prototype (2026-11-12, branch `rust-source-tree`): the Rust counterpart of what
the code dict (`core_syntax.parse_to_dict`, `CodeDict`) reads and writes.
`source.tree` is the code of a `Source` as a mapping, read and written like a
dict, and `draw_any` draws it unmodified:

```python
source = Source(text, path)
draw_editor(source, state=state, ...)                # the text
draw_any(source.tree, name='tree', ...)              # the same code as rows of keys and values

source.tree['Knobs']['gain']                         # 0.5
source.tree['Knobs']['gain'] = 0.75                  # one splice: every editor on the source shows it
source.tree['Knobs']['scale']['parameters']['factor'] = 3.0
```

`examples/gui_editor_tree.py` shows both side by side: drag a number or press a
chip on the right and the text changes; type on the left and the rows follow
once typing rests.

- **What is in it.** The names a module, a class or a function assigns, with
  the value: a number, a text, True / False, None; a `Branch` of the items of a
  list, tuple, dict, set or call; `Code` (a `str`: the source text) for any
  other expression. A class is a `Branch` of its body, a function one of
  `parameters` (the default, or `EMPTY`) and `locals`, a block (`if##0`,
  `for x in y`) one of its body, an `if` holds its test as `##if`, a call
  statement its arguments (`print()`). The keys are the source site keys, so a
  path of statements names a breakpoint's statement too.
- **It is a fact of the source.** Rust reads the tree on the source's thread
  (`native/source/src/tree.rs`) and keeps it; a `Branch` is a path of keys that
  asks for the entries it is looked at for (`tree_keys`, `tree_item`), so a
  view pays for its rows, never for the file. From the first `source.tree` on
  every reading of that source grows the tree too; a source nobody asked the
  tree of pays nothing.
- **It follows the text.** An edit, by an editor or anyone, is read once it
  rests (the editor's own request, or the tree's after `QUIET`), and the views
  that read the tree are woken by the reading thread through their handles. A
  reading that changed no key runs the rows whose values changed and nothing else.
- **`source.tree` is the same object while the keys are the same.** When a key
  was added, removed or renamed anywhere it is another object, which is how a
  retained view sees that its rows changed; open branches are kept by their
  paths. Branches held from before stay good and answer from the text as it is.
- **A write replaces one value's text** (`Tree.put`): the entry's span, brought
  forward over the splices made since its reading. A key that is a statement (a
  class, a function, its `parameters` and `locals`, a block) raises `TypeError`:
  a write never replaces a header. A list or dict that was written is answered
  by its `Branch`, so a write to one of its items reaches the text too. The tree answers the value
  written until the reading of that text lands, so a drag does not snap back. A
  str is quoted like the literal it replaces, a float is written without float
  noise, a `Code` as it is (and is answered as `Code` from then on, so a field
  that is typed into stays a text field).

### Cost of the tree

`tests/benchmark_source_tree.py` on a reserved desktop, 2026-11-12: hidden
window, no swap, `draw_editor(source)` beside `draw_any(source.tree)`, the
generated sources of `benchmark_editor.py`. Median ms of three runs:

| Row | 1,030 lines | 10,036 lines | 100,020 lines |
| --- | --- | --- | --- |
| idle | 0.22 | 0.22 | 0.22 |
| a character typed in the editor, no tree on the source | 1.19 | 1.19 | 1.24 |
| the same with the tree beside it | 1.22 | 1.25 | 1.30 |
| the frame a reading is taken in, no tree | 0.59 | 0.60 | 0.84 |
| the same with the tree: its rows run again | 0.89 | 0.91 | 1.40 |
| a number dragged in the tree (a splice a frame, the editor repaints its line) | 1.38 | 1.74 | 1.21 |
| the wheel over the tree's rows | 0.61 | 0.61 | 0.62 |
| a branch opened or shut | 0.71 | 0.71 | 0.71 |
| off the render thread: a first reading, without and with the tree | 0.34, 0.84 | 2.7, 11.1 | 29.4, 120 |
| a reading after an edit, without and with (21,056 top-level statements at the largest) | 0.06, 0.13 | 0.34, 0.63 | 4.4, 7.4 |

The frame of a landed reading is slower at 100,000 lines with and without the
tree (the editor's rows take the folds, and the frame runs right after the
thread's work); the tree's share is one run of the collection, 0.3 to 0.55 ms.
A reading after an edit takes the tree parts of every top-level statement
that did not change (`Tree::grow`); before that, growing the whole tree cost
61 ms a reading at 100,000 lines. On the repository's own large files (6,000
to 9,800 lines) a first reading goes from 2.7 to 4.8 ms to 6.5 to 10.3 ms with
the tree, a reading after an edit from 0.4 to 0.6 ms to 0.6 to 0.8 ms, or to
3.7 to 5.3 ms where the edit is inside a top-level statement of thousands of
lines (`text_view.py`, `melty.py`, `core_render.py`): see the gaps below.

No row of the existing benchmarks moved. Two sets of three interleaved runs of
each tree (before / after, median ms at 1k, 10k, 100k lines, the second set):
`type` 1.18 / 1.18, 1.17 / 1.16, 1.31 / 1.20; `caret` 1.59 / 1.61, 1.67 / 1.59,
1.57 / 1.77 (its two modes, as above); `enter` 1.48 / 1.49, 1.39 / 1.40, 1.42 /
1.42; `busy` 1.54 / 1.54, 2.05 / 2.06, 1.73 / 1.77; `find` 1.62 / 1.64, 2.16 /
2.17, 1.76 / 1.77; `word` 1.30 / 1.30, 1.44 / 1.42, 1.50 / 1.50; `scan` 0.2 /
0.2, 0.5 / 0.5, 5.0 / 5.0. `benchmark_gui_any.py` at 1,000, 100,000 and
1,000,000 entries: every row within 0.03 ms (mount 1.51 / 1.57, 2.42 / 2.52,
15.5 / 15.9). `benchmark_editor_session.py --lines 10000` (a real window,
everything on, one run each): 7 frames over 8.33 ms before, 6 after, the
frames named above (`launch`, `type#45`, `type#221`, `header#37`), typing
median 2.38 / 2.43 ms, p99 5.04 / 4.90 ms.

### Not there yet

- Adding, removing and reordering keys (`tree['NEW'] = 1` raises).
- Undo of a write: it is an edit from outside for the editor's history.
- **An edit inside a large top-level statement grows that statement's tree
  again**, before the reading is handed over (3.7 to 5.3 ms inside a class or
  function of thousands of lines). Parts per nested statement would bound it
  ([the crate's README](../native/source/README.md#known-gaps)).
- A tuple is a `Branch`, so a view registered for a shape of tuples (a colour
  swatch for three floats) does not see it.
- Targets that are no single name (`self.x = 1`, `a, b = 1, 2`), decorators,
  imports and the keyword statements are not surfaced; strings over several
  lines, bytes, f-strings and implicit concatenations are `Code`.
- `CodeDict`'s other sinks (the live object, launch overrides, pending saves)
  and its runtime side (positional arguments bound to signatures, enums
  resolved) are not on it: the tree is of the text alone.
- A `Branch` holds its source weakly: keep the `Source`.

## Background models

- **`Analysis(source, roster=None)`**: one worker thread. Compiles each
  top-level statement once (kept by the statement's key), reads the attributes a
  running module lacks, resolves import suggestions and signatures, turns tint
  rows into marks. What it found in an older text than the landed reading is
  carried over the splices between the two (`Analysis.carried`), so `errors`
  is of one version. It shares the interpreter with the frame, so it works in
  pieces of one statement and calls `glfw_utils.park()` between them.
- **`Roster`** (`project(path)`): one per project, kept in `Melty.rosters` and
  injected. Rust holds a symbol table per file, a module index and, per name,
  the files that define and use it, built on the roster's own thread. Python
  says which files belong and pushes unsaved text when `PendingSave` announces
  an edit; an open `Source` is its file's table (`roster.join(source)`). Ask
  `tints`, `lookup`, `resolve` and `uses` off the render thread; each `tints`
  answer names what it hung on, and only watchers whose needs went stale wake.
- **`Spelling(source)`**: teal wavy underlines while
  `Toggles.TextEditor.enable_spell_check` is on. Words are read the way code
  spells them (`editor/spell_check.py`): every character that is no letter
  delimits, camel case names are their parts, and Python's and the loaded
  modules' own terms are known; parts under four letters are left alone. Detection uses a word set; the
  suggestion index is built only on the first suggest.

## What works

- **Editing.** Caret by character, word, row, page, line and document; selection
  by Shift, double and triple click, drag by word or line, creeping past the
  edge. Typing, pair wrapping of a selection, paste that reindents, Backspace
  and Delete by unit and by indent stop, Enter with the indent its context asks
  for (open bracket, block opener, comment continuation, string split), Tab
  indent and bracket alignment, comment toggle, cut of a whole line, copy, paste.
- **Undo.** Every command is a `Step` on `UndoManager.stack`; typing folds by
  the framework's rule. There is no editor history.
- **Display.** Python colouring from the Rust lexer (with the names of keyword
  arguments in calls in their own colour, `kwarg`, which the legacy lexer does
  not have), line numbers, scope guides,
  cursor line, file tint, search hits, a flash where a jump or undo lands,
  sideways scroll, scroll bar, single line fields, ghost text.
- **Folds.** Scope folds collapsed by name (they survive edits and restarts), a
  caller's ranges as a second layer, default folds (comment runs, strings,
  imports), the fold chords (Ctrl+Minus, Ctrl+Equal, with Shift for all).
- **Inline widgets.** A switch on booleans (a double click flips it: a single
  click puts the caret in the word), a drag on numbers, swatches with a
  picker on colour tuples and hex colours, the icon chip with its picker (both
  pickers are [temporary popups](GUI_PROTOTYPE.md#temporary-popups): a click
  elsewhere or Escape closes them), and widgets by type for live values. A widget may add cells to its line (`lead`,
  `trail`); positions go through the line's column map.
- **Code facts shown.** Undefined names, call misfits, syntax errors (a chip
  in the gutter, the message in a popover on a click), quick-fix
  imports (Alt+Enter), definition tints and their washes over uses (across files
  with a roster), live values, breakpoints, the paused line, spelling.
- **Search (Ctrl+F).** The find bar opens in the editor's lower right corner
  with the selection as its term (selected, so typing replaces it) and takes
  the keyboard. A term is matched case blind, hits never overlap. Every hit is
  washed, the current one framed; the bar shows `3 of 41`, or `no match`. A new
  term's current hit is the first at or after the caret; Enter and Down step
  on, Shift+Enter and Up back, around the ends. Landing on a hit opens the
  folds over it, centres it when out of sight, scrolls sideways when it must
  and flashes it. The caret and the selection stay where they are, as in the
  legacy editor. Escape (or the cross) closes the bar, removes the highlights
  and gives the keyboard back to the text; the term is kept for the next
  Ctrl+F. A press in the text takes the keyboard back and leaves the bar
  open. The hits stay in Rust (`Hits`): a step is two bisections
  (`Hits.seek`, `Hits.at`), the count is a length, and the current hit is one
  mark that follows edits. A small text is searched in the frame the term
  changes; a text over `INLINE` characters on the source's thread once the
  term rests, one reading for a term typed letter by letter.
- **Go to definition and usages (Ctrl+B).** On a use the caret goes to the
  definition; on a definition's name to its use, or with several a picker
  lists them (the line's text, `file:line`, `guess` where the roster guessed):
  arrows, Page keys, hover and the wheel move in it, Enter or a click jumps,
  Escape or any other key closes it. A jump opens the folds over its place,
  centres it when out of sight and flashes the name; a name that leads nowhere
  flashes red. The places are the text's own symbols (`Source.lookup`) or, with
  `roster=`, the project's (`Roster.lookup`); a local or a parameter leads
  from a use to its binding (`Source.bound`). The lookup runs on a pool thread
  of the landed reading (a press on text typed since waits for its reading);
  the press frame only queues it. Ctrl+Shift+B opens the caret's file line in
  the external editor (IntelliJ, `lookup.external`), on a thread of its own.
- **Completion.** A row shows the tint its symbol states and, for a function
  or a method, its parameters (`Suggest.extra`: read for the rows on screen,
  from the source's facts or the loaded object; the tints of a receiver's
  members from the roster on a pool thread). Names in sight from the source, members of loaded modules,
  looked-up members and signatures, snippets with Tab stops, import shortcut
  rows, a caller's `completion_source`; Ctrl+Space, the signature hint (Ctrl+P),
  and the Escape rules of the legacy editor.
- **Several panes on one file**, each with its own caret, folds and scroll.

## Not ported from `draw_text`

From the review of `draw_text` and the passes since. Small refinements are not
listed.

| Missing | What exists already |
| --- | --- |
| The find bar's legacy extras: Ctrl+Enter "clicks" the current hit, Enter held repeats, one search over several views (`SearchTerm`), the glow around hits | each editor searches its own text; the bar's field has no undo of its own and no pointer selection |
| Usage counts (heat) in the gutter and the click on them | descoped (Lukas, 2026-11-11): not to be built, nor its roster facts. The `heat` mark shape paints for a caller that has counts |
| The usage picker as a tree (file, enclosing scopes, line) with tints and coloured code, its resize and "N more" expansion | the picker is a flat list (`editor/lookup.py`), cut at `MOST` rows with a count |
| Ctrl+B from the binding of a local to its uses | `Source.bound` leads from a use to the binding |
| The tint settings `usage_tint`, `def_tint_propagation`, `roster_def_tints` | definition tints and washes, always on |
| Error chips of lines out of sight, pinned to the top and bottom row with an arrow that scrolls there | an error on screen has its chip in the gutter; the click opens the message |
| Compare / diff rendering | `folds=` and a `line_numbers` list carry the layout |
| Live-view overlays | `Source.spans`, `label`, `closes` give their geometry |
| Run buttons | |
| Console and bash modes; colouring for other languages | non-Python text paints plain |
| Context menu, middle button, right-drag pan | a right drag over the editor resizes its window, as over any view (Lukas, 2026-11-12) |
| Touch | the host delivers touch contacts (`event.touches()`); the editor does not ask for them |
| Swap into `meltygui_pro` and `melty_code_editor` | untouched; they call `draw_text` |

Code hosts are still what `draw_text` and `CodeDict` use. `Source` replaces them
only inside the native editor so far; `Source.tree` is the first part of the
code dict on it ([above](#sourcetree-the-code-as-keys-and-values)).

## Performance

Target: 120 fps, 8.33 ms a frame, for every interaction at 10,000 lines and far
beyond.

### Rules

- Nothing on the render thread is proportional to the file. Ask facts by line;
  `Document.text` and `source.text` are copies of the whole text.
- A view declares only the blocks near its viewport; a keystroke repaints one
  block, a scroll none.
- A Python worker shares the interpreter with the frame. A frame lets the lock
  go at every GL call and waits up to a switch interval (5 ms) to get it back
  from a thread that is computing. So a worker does pieces well under a
  millisecond and calls `glfw_utils.park()` between them, and makes no long
  single C call (`compile` or `ast.parse` of a whole text, a conversion of a
  whole table).
- Work that needs the whole text belongs in Rust, on the source's or the
  roster's thread.
- A result repaints only when what it shows on screen differs (`Editor.same`).

### Frame cost against file size

`tests/benchmark_editor.py --folded --lines 1000,10000,100000` on a reserved
desktop: hidden window, no workers, no swap. Median ms a frame, 2026-11-11:

| Row | 1,030 lines | 10,036 lines | 100,020 lines | largest / smallest |
| --- | --- | --- | --- | --- |
| mount (first frame) | 4.5 | 3.6 | 3.9 | |
| idle | 0.20 | 0.20 | 0.20 | 0.98 |
| type | 1.26 | 1.16 | 1.20 | 0.95 |
| caret | 1.68 | 1.76 | 1.68 | 1.00 |
| enter | 1.47 | 1.42 | 1.42 | 0.97 |
| scroll | 0.37 | 0.38 | 0.37 | 1.01 |
| fresh (rows never painted) | 0.91 | 0.91 | 0.89 | 0.99 |
| busy (a reading every frame) | 1.52 | 2.09 | 1.73 | 1.13 |
| find (23,688 hits at the largest) | 1.69 | 2.18 | 1.73 | 1.02 |
| word (names in sight every frame) | 1.26 | 1.37 | 1.43 | 1.13 |

The gate: every row at the largest size within about 1.3 of the smallest. Always
pass `--folded` (the view's default) and read `busy`, `find` and `word`.

Search and Ctrl+Shift+B did not move these rows: three runs of each tree,
interleaved, the same evening (before / after, median of the runs at 1k, 10k,
100k lines): `type` 1.21 / 1.23, 1.16 / 1.16, 1.24 / 1.30; `caret` 1.57 /
1.65, 1.54 / 1.74, 1.78 / 1.69; `busy` 1.53 / 1.51, 2.04 / 2.05, 1.84 / 1.89;
`find` 1.60 / 1.60, 2.12 / 2.17, 1.76 / 1.85; `word` 1.23 / 1.24, 1.37 / 1.39,
1.41 / 1.43. `caret` has two modes about 0.2 ms apart in either tree (four
more runs at 10k: 1.51, 1.71, 1.52, 1.76 before; 1.74, 1.87, 1.54, 1.50
after): compare several runs. Start-up of `examples/gui_editor.py`
(`MELTY_BENCH=1`, second launch on, seven launches each, interleaved): first
frame presented after 360 to 401 ms before (median 372) and 351 to 434 ms
after (median 377); `editor/find.py` imports in 0.11 ms.

### Stutters in a real session

The frame benchmark has no workers and no swap, so it cannot show a worker
holding the interpreter lock or a first-use cost. That is what
`tests/benchmark_editor_session.py --lines 1000,10000,100000` is for: one
process per size with a real window in `app.run` and everything on (lint,
`Analysis`, roster, spell check, breakpoints, two panes), driven by a script of
typing, Enter, jumps, search, scrolling, folding, the find bar (Ctrl+F, a term
typed, nine steps, Escape) and Ctrl+B (a jump, then the picker). A frame is
timed from when it was due until its buffer was handed over: a real window,
swap interval 0, frames paced at 120 a second, every worker on. Two runs,
2026-11-11 (evening), 4,379 frames per size; both values where they differ:

| | 1,030 lines | 10,036 lines | 100,020 lines |
| --- | --- | --- | --- |
| Frames over 8.33 ms | 6, 6 | 7, 11 | 28, 16 |
| of those in the launch phase | 5, 4 | 6, 9 | 19, 9 |
| Worst frame (the first) | 36.2, 39.4 ms | 33.6, 36.9 ms | 33.8, 37.9 ms |
| Worst frame after launch | 8.4, 9.5 ms | 9.5, 10.0 ms | 19.4, 20.6 ms |
| Median while typing | 2.19, 2.62 ms | 2.29, 2.26 ms | 2.28, 2.23 ms |
| p99 while typing | 5.62, 5.57 ms | 5.12, 4.83 ms | 6.75, 6.85 ms |
| Find bar phase: median, p99, worst | 2.6, 6.6, 7.5 ms | 2.8, 5.4, 6.9 ms | 2.3, 4.1, 5.7 ms |
| Ctrl+B phase: median, p99, worst | 2.2, 4.1, 5.4 ms | 2.2, 3.9, 5.4 ms | 2.3, 3.8, 5.8 ms |

(Find bar and Ctrl+B rows: the worse of the two runs.) The tree as it was
before search was added, same evening, same script without the last two
phases (3,787 frames): 9 and 7, 20 and 9, 24 and 20 frames over; worst after
launch 9.6 and 9.8, 16.5 and 10.9, 22.0 and 21.1 ms; typing median 2.23 to
2.37, p99 5.20 to 6.56 ms. The frames over budget after launch are the same
ones in both trees (`type#45`, `type#221`, `header#37`, `fold#1`, `unfold#1`
and single frames at 100k lines); none is in the find bar, Ctrl+B or search
phases. One was, and is fixed: a search term makes a reading of the same text,
which woke the `Analysis` worker for a pass over all 21k statements (20 ms,
twice: once a quarter second after the hits landed, once two seconds later),
and a frame beside that pass took 8.4 to 11.8 ms (three of three runs at 100k
lines, none with `--off analysis`). `Analysis.compile` now skips a reading
whose text it has gone over.

Before the stutter pass of the same day the worst frames were 1.1 s, 1.0 s and
3.3 s. Counts differ by up to 5 between two runs and a phase's worst frame by 10
to 15%: compare phase by phase, never one frame.

What is still over budget, with its cause:

- The first frame, 36 to 41 ms (mount).
- One frame of 19 to 30 ms in the first second: the framework's kwarg-names
  warm-up (one `ast.parse`), in every app.
- One frame of 9 to 21 ms around the sixteenth frame: not attributed yet.
- The first letters of a word, 9 to 11 ms: `code_checks.project_importables`
  rescans modules when one was imported since.
- The first signature lookup in a call, 9 to 21 ms: the whole text is joined and
  handed to jedi.
- At 100k lines: fold all 21 ms and unfold all 13 ms (18k fold names made and
  saved), and single frames of 8.4 to 11 ms during edits, jumps and scrolls
  while the worker passes over 21k statements.
- A 12.7 ms frame while scrolling at 10k lines in this run: not attributed.
- The first launch after the cache is wiped also bakes the fonts.

To attribute a stutter: turn parts off with `--off`, or time the suspects with
thread names against the `slow` list in the `--json` output. Measure the second
launch (the benchmark does), keep swap interval 0, and never subtract the swap
time: the wait for the lock is after it.

### Occasional actions: Ctrl+B, Ctrl+Shift+B, search

`tests/benchmark_editor_actions.py` on a reserved desktop, 2026-11-11: hidden
window, no swap, no `Analysis` or spelling worker, the lookups on the pool
thread as in an app, the collector run between rounds. 30 rounds per size (60
presses), median ms and the worst in brackets:

| Row | 1,186 lines | 10,192 lines | 100,176 lines |
| --- | --- | --- | --- |
| press (Ctrl+B: queues the lookup) | 0.73 (1.70) | 0.78 (1.49) | 0.57 (1.19) |
| lookup, off the render thread: the source alone | 0.52 (0.74) | 2.26 (4.15) | 22.4 (33.4) |
| lookup with a joined roster (`--roster`) | 0.19 (0.45) | 0.16 (0.36) | 0.53 (27.5) |
| jump (the frame it lands in: unfold, scroll, flash) | 2.27 (18.9) | 2.70 (3.68) | 3.44 (5.83) |
| open (the picker, 150 rows) | 1.14 (2.48) | 1.31 (2.74) | 1.39 (2.45) |
| arrow (a key in the picker) | 1.13 (1.77) | 1.31 (1.97) | 1.20 (1.67) |
| outside (Ctrl+Shift+B, launcher stubbed) | 0.39 (0.60) | 0.39 (0.67) | 0.39 (0.53) |
| term (a search term changes) | 1.93 (2.74) | 1.31 (2.11) | 1.22 (1.79) |
| hits (its hits land: 189, 1,848, 18,424; the first is revealed) | 0.24 (0.44) | 1.71 (2.37) | 2.19 (3.00) |
| next | 1.74 (2.64) | 1.53 (2.36) | 1.54 (2.34) |
| previous | 1.67 (2.33) | 1.55 (2.35) | 1.44 (2.60) |

At 1,186 lines the text is under `INLINE`, so `term` holds the search itself
and `hits` nothing. `Source.lookup` builds the symbol table per call, which is
why the source alone costs the file's size off the thread; a joined roster has
the table. Two single frames were over budget in these runs and in the
`--roster` run beside it (the 18.9 ms `jump`, a 15.5 ms `term`, both at the
smallest size); 240 further rounds at that size had none over 6 ms. They are
not attributed. Without the collection between rounds a whole collection (9 to
30 ms in this host, which has no `gc_manager`) falls into about one timed
frame in a hundred rounds: that was the 12.5 ms "first jump" of the earlier
table; the first jump of a fresh process costs 1.8 to 2.1 ms (6 of 6).

### Off the render thread

A reading of 91k lines: 40 ms fresh, 5 ms after an edit, 6 to 7.5 ms with lint
(20 ms when an edit changes a binding or a header and every chunk is read
again). A held `Source` costs about 6.5 times its text in memory after a
reading.

## Platforms and packaging

- **Verified:** Linux, native Wayland windows, OpenGL. No CUDA path is involved.
- **`Source` extension:** built against the stable ABI (`abi3-py311`). One build
  loaded under CPython 3.11 to 3.14 here. `hatch_build.py` compiles it for every
  wheel and editable install. A wheel built today is
  `meltygui-0.1.9-cp311-abi3-linux_x86_64.whl` and holds
  `meltygui/model/_source_native.abi3.so`.
- **The `@gui` host is not in the wheel.** `_gui_native` is built by
  `tools/build_gui_prototype.py` only, for one interpreter version (not the
  stable ABI), and the build hook does not name it. A wheel install therefore
  has `Source` and the roster but cannot run `draw_editor`. The editor runs from
  a checkout where both crates were built.
- **Not done:** Windows and macOS builds have not been attempted. The Linux tag
  is `linux_x86_64`, not a manylinux tag an index accepts. The release flow
  (`tools/release.py`, CI) still assumes a pure Python wheel; whether it takes
  per-platform wheels is an open decision.
- **iOS:** the crate type-checks for `aarch64-apple-ios`, and nothing more. It
  has no entry in `meltygui/platforms/ios/build_rust_deps.py`; the retained
  `@gui` host draws with OpenGL only, so the editor cannot render there; and the
  UIKit host offers no way to request a frame without the interpreter
  (`window_api.poke` returns none for it).

## Framework changes made for the port

- **Input** (`core/rendering/gui_imgui_input.py`): `Pointer.button`; the event
  kinds `hold`, `hover` and `touch`; `@gui(cursor=shape)`. No new input handler.
  (Since 2026-11-13 these are methods of the view's `event`,
  `core/input/event.py`: the buttons by name, `left_mouse_held_outside()`,
  `is_hovered()`, `touches()`.)
- **Wake without the interpreter** (`native/gui_prototype/src/wake.rs`,
  `glfw_utils.waker` / `drain_wakes` / `close_wakes`, `window_api.poke`,
  `RetainedCache.waker`): a native thread queues a token and rouses the event
  wait; the frame loop drains the queue in `app.run`, `NativeApp.frame` and
  `RetainedCache.flush`.
- **`glfw_utils.park()`** for Python workers.
- **Retained host:** strings over 4,096 characters alias by identity
  (`PATH_ALIAS`) instead of being compared.
- **Start-up:** the fonts of the first frame are remembered and prebaked
  (`core/styling/fonts.py`, `warm_start.py`); the kwarg-names warm-up in
  `core_render.py` was cut from 65 to 115 ms down to 20 to 28 ms.
- **`Melty.rosters`**: the project rosters.
- **`Readers`** (`core/rendering/gui_readers.py`, 2026-11-12): a model notes
  the view that reads its values and wakes it when a worker swaps in new
  ones. Before it a result only asked for a frame, which a cached caller
  answers without running.
- **Build:** `tools/build_source.py`, `hatch_build.py`, the hook in
  `pyproject.toml`.

## Tests

Run modules one at a time; a whole-suite run segfaults in
`tests/test_render_background.py`, which predates this work.

| Module | Tests | Needs |
| --- | --- | --- |
| `test_source.py` | 105 | |
| `test_editor_document.py` | 38 | |
| `test_editor_keys.py` | 16 | |
| `test_roster.py` | 13 | |
| `test_editor_suggest.py` | 11 | |
| `test_editor_lookup.py` | 8 | |
| `test_editor_find.py` | 6 | |
| `test_editor_hints.py` | 8 | |
| `test_breakpoint_model.py` | 8 | |
| `test_spell_model.py` | 6 | |
| `test_icon_picker.py` | 7 | |
| `test_source_tree.py` | 19 | |
| `test_source_file.py` | 17 | |
| `test_source_scope.py` | 7 | |
| `test_source_version.py` | 4 | git |
| `test_source_tree_view.py` | 8 | a desktop (GL) |
| `test_source_file_view.py` | 4 | a desktop (GL) |
| `test_editor_view.py` | 40 | a desktop (GL) |
| `test_gui_readers.py` | 2 | a desktop (GL) |
| `test_editor_hints_view.py` | 8 | a desktop (GL) |
| `test_editor_lookup_view.py` | 6 | a desktop (GL) |
| `test_editor_find_view.py` | 8 | a desktop (GL) |
| `test_editor_pointer.py` | 6 | a desktop (GL) |
| `test_spell_view.py` | 1 | a desktop (GL) |

`test_retained_gui_prototype.py` (15) and `test_native_app.py` (38) cover the
host changes. All of the above passed on 2026-11-11, the view tests on native
Wayland. Isolate every run: fresh `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME` and
`MELTY_FILE_META`. In tests put undo steps frames apart: the framework groups
steps made within two frames into one undo.

## Known gaps

- **Open bugs in `Source`**: a panic of the reading thread is still told to
  nobody, and the lint has false alarms on Python 3.12 type parameters, 3.12
  f-strings and 3.14 t-strings. Fixed on 2026-11-17, each with a test: the store
  panic on deleting from a block boundary to the text's end, the long non-ASCII
  token that killed the reading thread, `Already borrowed` between inline
  readings and another thread, an older reading replacing a newer one. Causes
  and reproductions are in
  [the crate's README](../native/source/README.md#known-bugs).
- **Open bugs in `Source.tree` writes** (found 2026-11-17, not fixed): a string
  continued with a backslash has the span of its first line, so a write to it
  corrupts the next line; `spell` writes a NumPy scalar as its repr
  (`np.int64(3)`); `Tree.spot` reads the whole text again on the calling thread
  when an edit from outside touched the value's ends (21.5 ms at 100,000
  lines), which a write to an item of a list just written in a text over
  `INLINE` characters also runs into.
- **`Source(thing)` and the legacy stack disagree.** Unsaved text of the legacy
  editor lives in `PendingSave`, and `model/roster_model.py` and
  `editor/lookup.py` read `PendingSave.current_file_text`. They do not see a
  `File`'s text until it is on disk, and a `File` does not see a pending save
  until it is written. Pending spans flushed at exit are written over what a
  `File` wrote. `@save(to_code=True)` leaves a field alone, and says so, when
  the file's value changed during the run, which a write through a `File` is.
  In the studio (`Melty.init`) every write through a `File` is also an event
  for `FileWatch`: the file is read again there and its listeners run. A plain
  `@os_window` app starts no watcher and no hotswap.
- The roster does not propagate tints to locals, has no write-through for
  setting a tint, no worlds for merge panes, does not notice files created or
  deleted after `gather`, and does not list uses through an alias.
- **Usage counts in the gutter are descoped** (Lukas, 2026-11-11). They would
  need a roster answer for a whole buffer with `Needs`, an index key for "a
  file began or stopped using a name" and the uses of locals; none of it is
  planned.
- Ctrl+B answers from the tables the roster has when asked: while a project is
  still being read for the first time, uses in files not read yet are missing.
  A picker row's text for a file that is not open is read through
  `PendingSave.current_file_text`, as the roster's own pushes are.
- The roster's thread wakes watchers through a Python call (measured harmless,
  guarded at exit); the `Source` thread uses the C handle.
- The first signature lookup and a member lookup without an answer join the
  whole text for jedi.
- `Editor.sync` asks for a reading after every edit of a source that lints;
  `Source.ask` clones the chunk list (15 us at 100k lines).
- An intermittent `NativeState is unsendable` message appears in GL test runs.
- Code debts are listed in [Architecture gaps](ARCHITECTURE_DEBT.md#native-editor-and-source-debts-from-the-port).
- The crate's own gaps are in [its README](../native/source/README.md#known-gaps).
