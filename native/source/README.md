# `melty-source`: a text and the facts derived from it

The Rust side of `Source` (`meltygui/model/source_model.py`) and of the project
roster (`meltygui/model/roster_model.py`). The text of a file is the only state;
everything else (folds, colours, bindings, lint, symbol tables, search hits) is a
value read from one version of that text, on a thread that never holds the
interpreter lock. The native editor is its first user:
[docs/NATIVE_EDITOR.md](../../docs/NATIVE_EDITOR.md) describes the whole port and
its state.

## Build

```sh
.venv/bin/python tools/build_source.py            # add --offline when the crates are cached
```

Needs cargo (on `PATH` or in `~/.cargo/bin`); `MELTY_RUST_TOOLCHAIN` picks a rustup
toolchain. The library is copied to `meltygui/model/_source_native.abi3.so`
(`.pyd` on Windows) and other variants there are removed. It is built against
Python's stable ABI (`abi3-py311` in `Cargo.toml`): one build loads in every
CPython from 3.11 on. `uv pip install -e .` and every wheel build run the same
script through `hatch_build.py`. A running app keeps the library it loaded:
restart it after a build.

`cargo test --no-default-features` compiles the crate without libpython; it holds
no Rust unit tests. The tests are Python (`tests/test_source.py`,
`tests/test_roster.py`) and most are differential: the legacy Python
implementation is the oracle.

## Layout

| File | Holds | Lever |
| --- | --- | --- |
| `store.rs` | The text as immutable shared chunks of 32 lines (the editor's blocks), each with a stable key and an edit stamp; queries by line and offset. An edit replaces the chunks it touches; a snapshot is a clone of the chunk list. | `LINES` |
| `scan.rs` | What a chunk's lines say alone: indent, statement kind, tokens, triple-quote state (7 string states). f-string fields are lexed as code. Cached on the chunk. | the line lexer |
| `colour.rs` | `runs(line, state)`: the colour runs of one line. Port of `text_editor._tokenize_from`. | `KINDS`, `raw` |
| `bind.rs` | What a line binds, uses and calls, with the bracket stack (`Flaw`) and `match` captures. Cached per chunk as `Bound`. | `Binder::statement` |
| `call.rs` | `misfit`: do a call's arguments fit a signature. Port of `code_checks._match_spec`. | |
| `facts.rs` | One pass over the lines: folds, definitions, bindings by scope, undefined names, miscalls (findings kept per chunk), syntax flaws, top-level statements, the symbol table, tints, lookup, source sites, live-view spans, the rows a roster keeps. | `Facts::read` |
| `tree.rs` | The text as a tree of keys and values, what `Source.tree` reads and writes: a part per top-level statement (a flat table of its entries), shared between readings while the statement's text is where it was. | `Grower::simple`, `compound`, `fill`; `AHEAD`, `MISSES` |
| `rows.rs` | A session's fold layers, collapsed state and line to row mapping; `Hits` for search. Edits are followed in place. | `Rows::set`, `follow`, `land` |
| `roster.rs` | The project roster: one table per file, a module index, the files that define a name and the files that use it; its own thread. | `Ask`, `Index::install` |
| `lib.rs` | The Python classes (`Source`, `Rows`, `Hits`, `Roster`, `Needs`), the reading thread, the wake handles, the exit hook. | |

Each file's header says how to change it. Read it first.

## Rules

- **Facts are tables, never a syntax tree.** The boundary to Python is rows
  (line, column, kind, ...) asked by line range or by key, so another parser can
  fill them. Whole tables as Python lists are the exception: at 91k lines `sites`
  and `table` as Python values cost about 50 MB. The tree of `tree.rs` is rows
  too (key, kind, parent, span), asked by entry and key: `tree_keys`,
  `tree_item`, `tree_find`, `tree_span`, `tree_text`, for the rows a view shows.
- **The lexer is tolerant.** It reads line by line and never fails on broken
  code. Every lint rule errs on silence. CPython's `compile` remains the judge of
  real syntax errors (`Analysis.compile`, per top-level statement, in Python).
- **What depends on one chunk is cached on the chunk** (tokens, bindings, lint
  findings with the key they hold under). A reading after an edit rereads the
  edited chunk and merges. Do not add a per-reading pass over all tokens. What
  depends on one top-level statement is kept by the statement: a tree part is
  taken from the reading before while its text is in the same chunks and its
  keys are numbered as they were (`Tree::grow`).
- **The reading thread never runs Python.** It wakes the frame loop through C
  handles (`Waker`; the queue is `native/gui_prototype/src/wake.rs`). A job
  holds no `Py<PyAny>`.
- **A method that lets the interpreter go must not hold `&self` meanwhile**, or
  the writer's next `splice` raises "Already borrowed": take `slf: PyRef`, clone
  what is needed, `drop(slf)`, then `allow_threads`.
- **Dropping the last `Arc<Facts>` costs the file's size.** Whoever holds a
  reading last hands it to a thread of its own to free (`retire` in `lib.rs`),
  so the render thread never pays for it.

## Adding a fact

1. Collect it in `Facts::read` (or answer it from the chunk caches by line).
2. Give the native `Source` a method that returns it (`lib.rs`), by line range.
3. Give `Source` in `meltygui/model/source_model.py` a method or property.
4. Add a differential test against the Python code it replaces.

## Agreement with the Python implementations

Measured on the repository's Python files (770 to 772 files) when each fact was
ported; `tests/test_source.py` keeps a sample of each as a test.

| Fact | Oracle | Result |
| --- | --- | --- |
| Colour runs, string states | `text_editor._tokenize_from` | equal on all 266,539 lines |
| Bindings | `ast` | 0 of 93,417 scope names differ |
| References, calls | `ast` | 2 of 363,312 and 1 of 92,003 differ |
| Statements, parameter defaults | `ast` | 0 of 121,238 and 1 of 23,194 differ |
| Undefined names | `code_checks.check_source` | equal on every file |
| Call checks | `_check_call_static`, `_check_call_live` | equal on every file; on mutated calls 0 false alarms |
| Syntax flaws | `compile` | 0 false alarms on 1,544 broken texts |
| Symbol table | `symbol_roster.extract_table` | 16,107 of 16,217 entries identical, none missing |
| Definition tints | `text_editor._scan_def_tint_lines` | equal on all 8,384 entries |
| Source sites | `melty_scan.SourceSiteIndex` | identical on all 168,757 sites |

Folds differ from the legacy scanner by design where a statement continues over
several lines (inside open brackets or after a backslash): such a line neither
ends a scope nor opens one (32 of 770 files).

## Known bugs

Found in the review of 2026-11-11, each reproduced against the built library,
none fixed yet. Fix each with a regression test.

1. **Deleting from a block boundary to the text's end panics the store.**
   `Store::splice` (`store.rs`, the three `truncate(a + 1)` at its end) leaves
   `char0` / `line0` / `entry` one entry longer than `chunks` when the edit
   removes the last chunks and `a > 0`. The next `place`, `blocks`, `line` or
   `splice` indexes past the chunk list, and the source raises `PanicException`
   on every call from then on.

   ```python
   text = ''.join(f'line {i}\n' for i in range(200))
   s = Source(text); s.place(len(text))
   s.splice(s.start(64), len(text), ''); s.place(s.size)     # panics
   ```

2. **A token over 65,535 bytes holding non-ASCII kills the reading thread, and
   nobody is told.** `Tok::len` is a `u16` and `lex` / `fstring` clamp to it
   (`scan.rs`, the two `.min(u16::MAX as usize)`), so `start + len` can land
   inside a character and the next slice panics (`bind.rs` `Line::word`,
   `facts.rs` `word`). One long string literal or one long run of non-ASCII
   text on a line is enough. The thread dies (`Shift` only resets the flags),
   `land()` answers None for good and Python sees no error: folds, lint and
   hits freeze for that file. Two fixes: end the token on a character boundary
   (or widen `len`), and catch a panic in `work` so `land` can report it.

   ```python
   s = Source('x = 1\n' + 'é' * 40000 + 'a = f(1)\n'); s.ask(lint=True); s.settle(); s.land()   # None, panic on stderr
   ```

3. **`ask(now=True)` and `read` hold `&self` while the interpreter is let go**
   (`lib.rs`: `py.allow_threads(|| read(job).0)` in both), against the rule
   above. The store queries (`texts`, `blocks`, `place`, `start`, `slice`,
   `near`, `states`, `inside`) take `&mut self`, so one called from another
   thread meanwhile raises `RuntimeError: Already borrowed`. The spelling
   worker does (`Spelling.check`: `native.texts`), has no handler in `_work`,
   and stops for that file. The editor reads inline when a session is made
   and in `Editor.read`, for texts up to `INLINE` = 60,000 characters (typing
   asks the thread), so the window is those two moments. Fix: `slf: PyRef`, clone, `drop(slf)`; better, make the store queries not
   need `&mut` (the lazy prefix sums) so readers on other threads are legal.

4. **An older reading can replace a newer one.** `work` stores its result
   unconditionally (`desk.found = Some(found)`); an inline reading of a newer
   version made while the thread was still on an older one is overwritten, and
   `land()` returns the older. Needs an inline reading (a new session,
   `Editor.read`) while the thread is on an earlier request: a narrow window.
   Fix: keep the one with the higher version, in `work` and
   in `ask(now)`.

5. **Lint false alarms on valid Python** (the lint must err on silence):
   - Python 3.12 type parameters: with `def g[T](a: T)` the binder does not
     reach the parameters (`Binder::statement` expects `(` after the name), so
     `T` and every parameter are undefined; `class Box[U]` and
     `type Alias = int` likewise.
   - Python 3.12 f-strings that reuse their own quote: `f"{name["a"]}"`
     reports `a` (`scan.rs` `close` ends the string at the first quote).
   - Python 3.14 t-strings: `t"..."` reports `t` (the prefix letters in `lex`).

Smaller, from the same review:

- Nothing turns `\r\n` into `\n` on the way into a `Source`: line texts keep
  the `\r` (the roster's `disk` does normalize).
- `Facts::symbols` is made again for every `lookup`, `tints`, `table` and
  `roster` call: a pass over the whole text per call.
- `Source::land` drops a reading older than the landed one on the calling
  thread instead of through `retire`; `retire` spawns a thread per landing.
- A roster answer holds the index's read lock for its whole run; `stale`,
  `entries` or `named` called from a frame may wait behind a writer queued
  after it. Not measured.

## Known gaps

- **A tree part is a whole top-level statement.** An edit inside a class or a
  function grows the tree of all of it again, on the reading thread, before the
  reading is handed over: 0.2 ms more in a file of small statements, 4.3 ms more
  for an edit inside a 4,000 line function (`text_view.py`). Parts per nested
  statement would bound it; the entry names (`u64`: part, row) would need a level.
- The first reading with a tree costs about twice the reading alone (4.3 to 9 ms
  at 9,000 lines): every statement is read by the site reader, a statement whose
  key is numbered (a name the module gave before, an `if`) twice.
- The tree does not surface targets that are no single name (`self.x = 1`,
  `a, b = 1, 2`), decorators, imports, and `return` and the other keyword
  statements; a string over several lines, bytes, f-strings and implicit
  concatenations are code (their source text), not values.

- An edit that changes a binding or a `def` / `class` header makes the lint read
  every chunk again (off the render thread: 20 ms at 91k lines against 6 to 7.5).
- Bindings of a bracketed target list left open across a chunk boundary depend
  on where the boundary falls.
- A multi-line `match (` subject reads `match` as a call.
- The roster's thread wakes its watchers through a Python call; the `Source`
  thread does not.
- No answer gives a buffer's usage counts (the gutter heat of the legacy
  editor). Descoped on 2026-11-11: not to be added.
- Built and run on Linux only (wheel tag `linux_x86_64`). The crate type-checks
  for `aarch64-apple-ios`; no Windows, macOS or iOS build has been made.
