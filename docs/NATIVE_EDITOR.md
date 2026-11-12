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
analysis.feed(); spelling.feed()                  # after every run; cheap
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

Decisions behind it (Lukas, 2026-11-11): Rust is a dependency everywhere, iOS
included. The boundary is fact tables, never a syntax tree, so the parser can be
replaced; it is home-built and tolerant. Real syntax errors stay CPython's
`compile`, off the render thread. Views take plain values. For stacked
decorators the nearest one's tint wins.

## Background models

- **`Analysis(source, roster=None)`**: one worker thread. Compiles each
  top-level statement once (kept by the statement's key), reads the attributes a
  running module lacks, resolves import suggestions and signatures, turns tint
  rows into marks. It shares the interpreter with the frame, so it works in
  pieces of one statement and calls `glfw_utils.park()` between them.
- **`Roster`** (`project(path)`): one per project, kept in `Melty.rosters` and
  injected. Rust holds a symbol table per file, a module index and, per name,
  the files that define and use it, built on the roster's own thread. Python
  says which files belong and pushes unsaved text when `PendingSave` announces
  an edit; an open `Source` is its file's table (`roster.join(source)`). Ask
  `tints`, `lookup`, `resolve` and `uses` off the render thread; each `tints`
  answer names what it hung on, and only watchers whose needs went stale wake.
- **`Spelling(source)`**: wavy underlines while
  `Toggles.TextEditor.enable_spell_check` is on. Detection uses a word set; the
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
- **Display.** Python colouring from the Rust lexer, line numbers, scope guides,
  cursor line, file tint, search hits, a flash where a jump or undo lands,
  sideways scroll, scroll bar, right-button pan, single line fields, ghost text.
- **Folds.** Scope folds collapsed by name (they survive edits and restarts), a
  caller's ranges as a second layer, default folds (comment runs, strings,
  imports), the fold chords (Ctrl+Minus, Ctrl+Equal, with Shift for all).
- **Inline widgets.** A switch on booleans, a drag on numbers, swatches with a
  picker on colour tuples and hex colours, the icon chip with its picker, and
  widgets by type for live values. A widget may add cells to its line (`lead`,
  `trail`); positions go through the line's column map.
- **Code facts shown.** Undefined names, call misfits, syntax errors, quick-fix
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
- **Completion.** Names in sight from the source, members of loaded modules,
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
| Error buttons in the gutter | the message after the line opens the detail |
| Compare / diff rendering | `folds=` and a `line_numbers` list carry the layout |
| Live-view overlays | `Source.spans`, `label`, `closes` give their geometry |
| Run buttons | |
| Console and bash modes; colouring for other languages | non-Python text paints plain |
| Context menu, middle button | the right button pans |
| Touch | the host delivers `touch` events; the editor does not listen |
| Swap into `meltygui_pro` and `melty_code_editor` | untouched; they call `draw_text` |

Code hosts are still what `draw_text` and `CodeDict` use. `Source` replaces them
only inside the native editor so far.

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
- **Build:** `tools/build_source.py`, `hatch_build.py`, the hook in
  `pyproject.toml`.

## Tests

Run modules one at a time; a whole-suite run segfaults in
`tests/test_render_background.py`, which predates this work.

| Module | Tests | Needs |
| --- | --- | --- |
| `test_source.py` | 100 | |
| `test_editor_document.py` | 36 | |
| `test_editor_keys.py` | 16 | |
| `test_roster.py` | 13 | |
| `test_editor_suggest.py` | 11 | |
| `test_editor_lookup.py` | 8 | |
| `test_editor_find.py` | 6 | |
| `test_editor_hints.py` | 8 | |
| `test_breakpoint_model.py` | 8 | |
| `test_spell_model.py` | 6 | |
| `test_icon_picker.py` | 6 | |
| `test_editor_view.py` | 38 | a desktop (GL) |
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

- **Open bugs in `Source`** (review of 2026-11-11, not fixed): a store panic on
  deleting from a block boundary to the text's end, a silent death of the
  reading thread on a very long non-ASCII token, `Already borrowed` between
  inline readings and the spelling worker, an older reading replacing a newer
  one, and lint false alarms on Python 3.12 type parameters, 3.12 f-strings and
  3.14 t-strings. Each with its cause and a reproduction in
  [the crate's README](../native/source/README.md#known-bugs).
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
- The right button reaches the editor through a hit region and an item declared
  in `draw_editor`; the proper place is the host's input.
- An intermittent `NativeState is unsendable` message appears in GL test runs.
- Code debts are listed in [Architecture gaps](ARCHITECTURE_DEBT.md#native-editor-and-source-debts-from-the-port).
- The crate's own gaps are in [its README](../native/source/README.md#known-gaps).
