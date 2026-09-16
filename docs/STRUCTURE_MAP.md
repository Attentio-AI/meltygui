# MeltyGUI structure map: wiring, renderers, and data adapters

The contribution standard is [Contributing](../CONTRIBUTING.md); the current
review of remaining coupling is [Architecture gaps](ARCHITECTURE_DEBT.md).
This document retains the initial dependency inventory and relocation history,
including pre-move paths in the detailed snapshots below. Historical path labels
are retained; their source links point to the current canonical files.

Reviewed 2026-09-16. Feature-view extraction and shared core relocation are
implemented. The deeper feature/model/state splits below remain a planning inventory.

The legacy `views/` and `widgets/` implementations have now all moved.
Only compatibility namespaces remain there. See the [complete move table](LEGACY_MODULE_MOVE.md).
The text editor is explicitly deferred and unchanged in this pass.

## Implemented layout

```text
meltygui/
    core/
        core_render.py        # render-function execution and injection
        melty.py              # shared runtime ownership and coordination
        mode.py               # renderer/converter mode definitions
        modes.py              # lazy handles that avoid import cycles
        mode_defaults.py      # shared type-to-mode policy
        render_host.py        # generic stateful-data hosting
        definition_hotswap.py  # module-independent live definition relocation
        file_core.py          # folder hosts, polling and lifecycle wiring
        ...                   # extracted feature callbacks
    view/
        file_view.py          # file/tree/selector rendering
        text_view.py          # text editor and inline value rendering
        code_view.py          # code and live-value presentation
        control_view.py       # primitive controls
        ...                   # 27 feature modules, plain functions
    model/
        file_model.py         # filesystem reconciliation and metadata adaptation
        tensor_model.py       # tensor types, slicing and data transformations
        camera_model.py       # pure orbit-camera and space-mouse math
        ...                   # feature value adapters
    state/
        file_state.py         # injected explorer/selector/tree state
        ...                   # feature state classes
```

Use `<feature>_view.py`, `<feature>_model.py`, and `<feature>_state.py`; a feature
does not need all three. Shared components can serve more than one feature.
Core owns plumbing and global coordination; views draw supplied values using
injected dependencies. Models expose stateful systems as dictionaries,
primitives or specific renderable types.

The first pass relocated 142 view functions and 29 supporting definitions.
The second pass moved 64 feature helpers out of legacy view/editor modules,
placing presentation beside the views and model/state/core helpers in their
corresponding feature files.
Old import paths retain compatibility aliases; public exports use the new view
modules. Hotswap now preserves canonical live identities and destination globals.
See [the feature/module index and validation](VIEW_RELOCATION.md).

`core_render.py`, `melty.py`, modes, `RenderHost`, generic conversion and
persistence, input, and window lifecycle now live under `core/`. See the
[core guide](../meltygui/core/README.md) and [82-module move table](CORE_RELOCATION.md).
The detailed tables below retain the earlier inventory and proposed ownership;
mixed feature modules and the editor remain separate follow-up work.
Read historical `views/<feature>` and `models/<feature>` destinations as
`view/<feature>_view.py` and `model/<feature>_model.py`.

The [tensor extraction](TENSOR_RELOCATION.md) moves tensor operations and camera
math into models, and shared presentation, dimension pickers and axis drawing
into tensor views. Shared LUTs, GPU runtime ownership, slice controls and demo
separation remain the next tensor work.

## Classification rules

| Question | Destination |
|---|---|
| Does it make arbitrary render functions work without repeated plumbing? | core |
| Does it own global-ish runtime coordination or connect views to platform/cache/lifecycle machinery? | core, with shared runtime ownership in Melty |
| Does it render a supplied value using local or injected state/events? | views/<feature> |
| Does it translate a stateful feature into a dictionary, primitive, or custom renderable type and apply edits back? | models/<feature> |
| Does it instantiate sample data, start a particular application composition, or demonstrate behavior? | examples or the consuming app |
| Does one file do several of these? | Mark mixed; classify its symbols before relocating it |

A decorator does not determine ownership. `folder_io`, `code_file_io` and
`render_host_view` can be decorated while still performing adapter or wiring
work. Likewise, a file under `views/` need not contain clean reusable views.
A Melty import is a useful inspection signal, not an automatic violation.

## Starting at core_render.py

The earlier static snapshot counted 6,509 lines, 42 directly imported internal
modules and 54 other library modules importing it. Those figures measure reach;
they are not evidence that the centralization itself is wrong.

| Existing responsibility | Proposed owner |
|---|---|
| render_func signature analysis, defaults, injection, execution and result handling | core/core_render.py |
| Melty registries, global-ish state and coordination | core/melty.py |
| DrawState and per-view runtime lifecycle | core; preserve the injection contract |
| Generic RenderHost and mutation bubbling | core; reused by multiple features |
| Conversion paths, generic codec protocol and registration | core |
| Caching, retained drawing, event dispatch, undo and window integration | core |
| Concrete controls, collection presentation, search UI and menus | views, grouped by feature |
| File/source/terminal-specific loading, reconciliation and edit application | corresponding feature models/adapters |

Keep the wrapper's ordering and shared context intact. If a concrete renderer is
embedded in wiring, examine that renderer for relocation; do not assume the
surrounding generic plumbing should be split as well. Renderer selection and
registry lookup are core responsibilities even when the selected renderer is a
feature function.

The wrapper's converter mode also runs without imgui on background threads.
That is part of its reusable contract and must survive any later changes.

## Traced feature paths

### Folder tree

Observed path:

```text
filesystem snapshots
    -> folder_io reconciles the held dictionary in place
    -> RenderHost holds {value: nested dict of names -> dict or Path}
    -> _draw_tree / draw_folder_files
    -> RenderFuncs.draw_collection
```

Evidence: `files/folder_files.py:333` defines `folder_io`; the host instances are
at 373 and 376, `folder_proxy` at 388, and `_draw_tree` at 408. The host primitive
itself is `code/render_host.py:56`.

Classification: scanning/reconciliation and filesystem metadata adaptation are
files-model responsibilities; collection rendering is a reusable view;
RenderHost's general coordination is core. The existing file also contains
poller registries, sample host instances and window composition. It is mixed,
not ready for a whole-file move into clean views.

Specific boundary to resolve later: `folder_io` stores `_seen_paths` on
DrawState and explicitly invalidates cache entries. `watch_folder` starts the
poller and stores draw states in a global registry. Preserve behavior while
identifying which parts are feature data state versus shared subscription
plumbing; do not add another one-off global service.

### Terminal sessions

Observed path:

```text
live tmux sessions
    -> claude_terminals_io reconciles {session name: Terminal}
    -> RenderHost exposes that dictionary
    -> collection/window composition
    -> draw_terminal_screen(input_value: Terminal, view_state, events, ...)
```

Evidence: `widgets/claude_terminals.py:191`, host construction at 260;
`widgets/terminal_playground.py:295` defines Terminal and 713 defines its screen
renderer. Terminal is a custom stateful type, not a dict subclass; the session
collection is the dict-shaped interface. Do not force every leaf into a dict.

Classification: PTY/screen/session behavior belongs to the terminal model;
screen painting belongs to terminal views; generic host machinery belongs to
core. The session launcher/composition is separate from reusable screen drawing.

Boundary leak: `draw_claude_terminals` starts a global poller, while the screen
renderer connects the terminal's reader invalidation target and updates global
focus. Some runtime interaction is necessary, but the generic connection should
be supplied by the framework rather than copied into each feature renderer.
The exact injected interface remains to be designed; no new mechanism is assumed.

### File-backed text and live code

Observed mechanisms:

- `code/new_converters.py:2033`: `code_file_io` selects/resolves a codec and
  supplies its loaded value to a view; its default view is `RenderFuncs.draw_text`.
- `code/new_codecs.py:1036`: TextFileCodec maps a file path to a whole-file
  Address and watcher information; inherited behavior supplies load/save.
- `editor/text_editor.py:9869`: `draw_text` takes `input_value: str`.
- `code/render_host.py`: generic hosts can chain held values through upstream
  hosts, feeding edits back through the stateful I/O wrapper.

Classification: loading/saving a source is adapter work; rendering a string is
text-view work. The codec protocol and generic host execution are shared wiring.
The current code_file_io also contains UI and Python-specific actions, so moving
its entire module to core would hide a mixed responsibility rather than settle it.

### Immutable syntax presented as mutable data

`code/libcst_conversion.py:244` defines `GeneralParse(dict)`; specialized parse
types reuse the collection/type-dispatch path. `views/cst_proxy.py:330` defines
CSTDictProxy and 589 defines CSTProxy, adapting immutable CST structures and
bubbling edits. These are related adapter mechanisms, not asserted here to be
one identical execution path.

Classification: source-specific parsing and proxies belong to code models;
collection and code presentation belong to feature views. The reusable host,
conversion and mutation propagation machinery belongs to core. CST proxies are
a particularly clear example of a model currently filed under views.

## Current files and their likely destinations

These are ownership assignments, not a mechanical move list. “Mixed” means a
symbol-level review is required before assigning a complete file.

| Current files/area | Proposed assignment |
|---|---|
| rendering/core_render.py, melty.py | core; keep their central roles |
| rendering/render_funcs.py, rendering/decorators, rendering/shaped.py | core registration/execution support |
| code/render_host.py, code/bubbling.py | core generic stateful-data hosting |
| code/path_finder.py, cache_tree.py, converter_register.py, chain.py | core conversion machinery; review source-specific dependencies |
| code/new_codecs.py, new_converters.py, chain_converters.py, basic_converters.py | Mixed: generic protocol/dispatch in core; feature conversion and presentation with their features |
| state/new_core_model.py | Mixed: DrawState in core; editor/menu/dropdown/zoom state beside corresponding views |
| state/dict_conversion*, load_save_v2.py, module_names.py, module_map.json, graph_compare.py | core persistence and identity machinery |
| state/core_undo.py and enums/markers | core where shared; feature-owned types follow their feature |
| models/core_decoration.py, dynamic_obj.py | core reusable model mechanisms |
| models/file_meta.py | files model, with shared scheduling glue identified separately |
| models/function_console.py, orchestration.py | console/orchestration feature models |
| views/blit_offscreen.py and debug renderer, split_overlay_renderer.py | core cache/compositing/overlay machinery; inspect debug UI separately |
| views/cst_proxy.py | code models/adapters |
| views/new_core_view.py | Mixed: controls/collections/inspection/menu views and framework-facing helpers |
| views/columns.py | Mixed: public row/column renderers in views/layout; frame/edge coordination in core |
| views/drag_drop.py, core_meta.py, anywhere.py, view_func_selection.py | core dispatch/interaction/metadata machinery; any concrete UI remains in views |
| views/headers.py, basic_view_utils.py | Review symbols: reusable presentation in views; wrapper-specific coordination in core |
| views/menu_bar.py, texture_view.py, line_graph_playground.py | Feature views; playground suffix alone does not make a renderer an example |
| views/tile_manager.py, fast_dock.py | Layout/workspace feature; separate runtime integration from presentation |
| views/*debug*, monitor.py, stack_trace_view.py, core_settings.py | Diagnostic/settings views; global observation/recording stays in core |
| views/utils/* | Split drawing/animation primitives, backend state and example support by actual role |
| editor/text_editor.py and other editor modules | text/code views plus models/adapters; pending saves and external-change handling are not pure drawing |
| code/core_syntax.py, libcst_conversion.py, fileref.py, project_code.py, source_context.py, symbol_roster.py | code models and source services, with framework hotswap hooks separated where necessary |
| code/file_converters.py, hotswap_guard.py, live_instrument.py, live_view.py, melty_scan.py | Mixed source-feature work and core hotswap/live-execution integration |
| code/code_checks.py, syntax_check.py, syntax_check_worker.py | code feature services/models |
| files/* | files views and adapters; folder_files is explicitly mixed |
| widgets/terminal_playground.py, claude_terminals.py | terminal views/models plus session composition; explicitly mixed |
| other widgets/* | Classify by feature (files, diagnostics, orchestration, controls); separate sample launchers |
| chat/*, completion/*, accounts/* | chat/completion/account feature models/services and views; retain external I/O behind the feature interface |
| tensor/* | tensor feature views and GPU/data adapters; shared context machinery stays core |
| graphics/* | shared shader/texture backend in core; demo content in examples |
| events/*, windows/backends/* | core input and platform wiring; event examples in examples |
| surface.py, lifecycle.py, window_api.py, window_visibility.py, os_frame.py, app_session.py | core window/session wiring behind public API |
| titlebar*.py, notifications.py | Mixed core overlay integration and presentation; not automatically clean feature views |
| background.py, gc_manager.py, warm_start.py, extensions.py | core lifecycle/scheduling/integration |
| toggles.py, modes.py, mode_defaults.py, style.py, global_style.py | core shared configuration/style mechanisms; feature mode definitions need individual review |
| fonts.py, text_texture.py, scene_target.py, gl_state.py, hdr_color.py, pbr.py, shader_func.py | shared rendering support in core |
| text_index.py, image_load.py | feature data/services; inspect shared consumers before final placement |
| mouse_cursor.py, window_constants.py, wayland_*.py, hypr_left_drag.py | core platform integration |
| debug/*, perf_trace.py, resize_trace.py, gpu_frame_timer.py, session_status.py | core diagnostics; separate rendered panels |
| mcp_*.py | core external runtime integration; presentation remains feature-owned |
| collision.py, geometry_feed.py, collection_action.py, func_metadata.py | core shared mechanisms |
| utils/glfw_utils.py | core, with mixed scheduling/platform/diagnostic contents noted |
| other utils/*, paths.py, settings.py, screenshot.py | assess consumers; shared framework support in core, feature behavior with feature |
| examples/* | examples, retaining reusable functions in views when also used outside demos |

Rows outside the traced features are provisional classifications from inventory,
symbols and imports, not complete semantic audits. No claim is made that every
current renderer already meets the local-state style guide.


The first symbol-level follow-up is [the folder-tree move plan](FOLDER_TREE_MOVE_PLAN.md).

## What should happen next

1. Agree on the three ownership roots and the rule for mixed files.
2. Use the folder-tree feature as the first concrete placement exercise: identify
   reusable renderers, its data adapter, generic host wiring, and demo instances.
3. Work through core_render's dependencies with the same rule. Keep reusable
   plumbing together; extract only concrete feature code that belongs elsewhere.
4. Produce an exact file/symbol move list before changing imports. Make one
   coherent migration at a time, preserving public exports and old saved names.
5. Verify hotswap/live state, background conversion, injected events/state,
   mutation reporting and caches with targeted tests and isolated UI checks.

Shared settings remain in Toggles. View-specific state is injected; adding
framework-wide DrawState fields needs Lukas's agreement. Framework gaps should
be reported rather than hidden by manual per-frame invalidation or restart workarounds.

## Scope and evidence limits

This review reads `/home/lukas/Desktop/meltygui`, with latent-descent's original
implementation and style guide as references. Other application repositories
have not been audited. Existing edits in meltygui were preserved.

The appendix below preserves the initial static inventory (258 Python files,
176,253 physical lines). Files are being edited concurrently, so lengths and
line references are snapshot navigation aids. Imports include local and
TYPE_CHECKING statements; dynamic imports and registry references are not fully
represented. Mutual imports are not by themselves design defects or runtime
import failures. No runtime/UI tests were run for this documentation-only work.

## Complete current package inventory

Counts below group files by their first directory; package initializers count
in their actual directory. This is a file inventory, not a claim that each
package has a coherent responsibility.

| Area | Python files | Physical lines |
|---|---:|---:|
| (package root) | 50 | 30,400 |
| accounts | 2 | 1,993 |
| chat | 13 | 4,861 |
| code | 25 | 33,867 |
| completion | 13 | 4,290 |
| debug | 7 | 1,338 |
| editor | 16 | 23,689 |
| events | 6 | 3,003 |
| examples | 12 | 910 |
| files | 4 | 1,830 |
| graphics | 12 | 4,182 |
| models | 6 | 963 |
| rendering | 10 | 8,119 |
| state | 12 | 6,885 |
| tensor | 6 | 4,569 |
| utils | 8 | 3,266 |
| views | 30 | 30,442 |
| widgets | 21 | 10,571 |
| windows | 5 | 1,089 |

### (package root)

- [__init__.py](../meltygui/__init__.py) — 99 lines
- [app.py](../meltygui/core/app.py) — 722 lines
- [app_session.py](../meltygui/core/app_session.py) — 140 lines
- [background.py](../meltygui/core/background.py) — 564 lines
- [collection_action.py](../meltygui/core/collection_action.py) — 45 lines
- [collision.py](../meltygui/core/collision.py) — 165 lines
- [extensions.py](../meltygui/core/extensions.py) — 48 lines
- [fonts.py](../meltygui/core/fonts.py) — 623 lines
- [func_metadata.py](../meltygui/core/func_metadata.py) — 398 lines
- [gc_manager.py](../meltygui/core/gc_manager.py) — 1,162 lines
- [geometry_feed.py](../meltygui/core/geometry_feed.py) — 855 lines
- [gl_state.py](../meltygui/core/gl_state.py) — 618 lines
- [global_style.py](../meltygui/core/global_style.py) — 338 lines
- [gpu_frame_timer.py](../meltygui/core/gpu_frame_timer.py) — 103 lines
- [hdr_color.py](../meltygui/hdr_color.py) — 757 lines
- [hypr_left_drag.py](../meltygui/core/hypr_left_drag.py) — 323 lines
- [image_load.py](../meltygui/image_load.py) — 308 lines
- [lifecycle.py](../meltygui/core/lifecycle.py) — 21 lines
- [mcp_eval.py](../meltygui/core/mcp_eval.py) — 167 lines
- [mcp_hotswap.py](../meltygui/core/mcp_hotswap.py) — 107 lines
- [mcp_query.py](../meltygui/core/mcp_query.py) — 552 lines
- [mcp_server.py](../meltygui/core/mcp_server.py) — 657 lines
- [melty.py](../meltygui/core/melty.py) — 6,718 lines
- [mode_defaults.py](../meltygui/core/mode_defaults.py) — 41 lines
- [modes.py](../meltygui/core/modes.py) — 136 lines
- [mouse_cursor.py](../meltygui/core/mouse_cursor.py) — 355 lines
- [notifications.py](../meltygui/core/notifications.py) — 706 lines
- [os_frame.py](../meltygui/core/os_frame.py) — 1,455 lines
- [paths.py](../meltygui/core/paths.py) — 20 lines
- [pbr.py](../meltygui/pbr.py) — 1,576 lines
- [perf_trace.py](../meltygui/core/perf_trace.py) — 280 lines
- [resize_trace.py](../meltygui/core/resize_trace.py) — 62 lines
- [scene_target.py](../meltygui/core/scene_target.py) — 180 lines
- [screenshot.py](../meltygui/core/screenshot.py) — 439 lines
- [session_status.py](../meltygui/core/session_status.py) — 98 lines
- [settings.py](../meltygui/core/settings.py) — 14 lines
- [shader_func.py](../meltygui/core/shader_func.py) — 477 lines
- [style.py](../meltygui/core/style.py) — 198 lines
- [surface.py](../meltygui/core/surface.py) — 637 lines
- [text_index.py](../meltygui/text_index.py) — 816 lines
- [text_texture.py](../meltygui/core/text_texture.py) — 322 lines
- [titlebar.py](../meltygui/core/titlebar.py) — 1,512 lines
- [titlebar_buttons.py](../meltygui/core/titlebar_buttons.py) — 281 lines
- [toggles.py](../meltygui/core/toggles.py) — 3,066 lines
- [warm_start.py](../meltygui/core/warm_start.py) — 149 lines
- [wayland_color.py](../meltygui/core/wayland_color.py) — 635 lines
- [wayland_move.py](../meltygui/core/wayland_move.py) — 932 lines
- [window_api.py](../meltygui/core/window_api.py) — 62 lines
- [window_constants.py](../meltygui/core/window_constants.py) — 339 lines
- [window_visibility.py](../meltygui/core/window_visibility.py) — 122 lines

### accounts

- [accounts/__init__.py](../meltygui/accounts/__init__.py) — 0 lines
- [accounts/internet_accounts.py](../meltygui/accounts/internet_accounts.py) — 1,993 lines

### chat

- [chat/__init__.py](../meltygui/chat/__init__.py) — 91 lines
- [chat/activity.py](../meltygui/chat/activity.py) — 75 lines
- [chat/backends.py](../meltygui/chat/backends.py) — 36 lines
- [chat/chat_interface.py](../meltygui/chat/chat_interface.py) — 2,504 lines
- [chat/chat_proxy.py](../meltygui/chat/chat_proxy.py) — 352 lines
- [chat/codex_proxy.py](../meltygui/chat/codex_proxy.py) — 592 lines
- [chat/codex_settings.py](../meltygui/chat/codex_settings.py) — 100 lines
- [chat/codex_transport.py](../meltygui/chat/codex_transport.py) — 60 lines
- [chat/command_parser.py](../meltygui/chat/command_parser.py) — 204 lines
- [chat/images.py](../meltygui/chat/images.py) — 227 lines
- [chat/messages.py](../meltygui/chat/messages.py) — 417 lines
- [chat/metadata.py](../meltygui/chat/metadata.py) — 139 lines
- [chat/writer_locks.py](../meltygui/chat/writer_locks.py) — 64 lines

### code

- [code/__init__.py](../meltygui/code/__init__.py) — 0 lines
- [code/basic_converters.py](../meltygui/code/basic_converters.py) — 533 lines
- [code/bubbling.py](../meltygui/core/bubbling.py) — 599 lines
- [code/cache_tree.py](../meltygui/core/cache_tree.py) — 188 lines
- [code/chain.py](../meltygui/core/chain.py) — 113 lines
- [code/chain_converters.py](../meltygui/code/chain_converters.py) — 2,149 lines
- [code/code_checks.py](../meltygui/code/code_checks.py) — 2,198 lines
- [code/converter_register.py](../meltygui/core/converter_register.py) — 145 lines
- [code/core_syntax.py](../meltygui/code/core_syntax.py) — 1,430 lines
- [code/file_converters.py](../meltygui/code/file_converters.py) — 1,876 lines
- [code/fileref.py](../meltygui/code/fileref.py) — 702 lines
- [code/hotswap_guard.py](../meltygui/code/hotswap_guard.py) — 144 lines
- [code/libcst_conversion.py](../meltygui/code/libcst_conversion.py) — 9,719 lines
- [code/live_instrument.py](../meltygui/code/live_instrument.py) — 392 lines
- [code/live_view.py](../meltygui/code/live_view.py) — 2,490 lines
- [code/melty_scan.py](../meltygui/code/melty_scan.py) — 2,661 lines
- [code/new_codecs.py](../meltygui/code/new_codecs.py) — 1,255 lines
- [code/new_converters.py](../meltygui/code/new_converters.py) — 3,507 lines
- [code/path_finder.py](../meltygui/core/path_finder.py) — 606 lines
- [code/project_code.py](../meltygui/code/project_code.py) — 278 lines
- [code/render_host.py](../meltygui/core/render_host.py) — 1,083 lines
- [code/source_context.py](../meltygui/code/source_context.py) — 63 lines
- [code/symbol_roster.py](../meltygui/code/symbol_roster.py) — 1,588 lines
- [code/syntax_check.py](../meltygui/code/syntax_check.py) — 34 lines
- [code/syntax_check_worker.py](../meltygui/code/syntax_check_worker.py) — 114 lines

### completion

- [completion/__init__.py](../meltygui/completion/__init__.py) — 0 lines
- [completion/fim.py](../meltygui/completion/fim.py) — 1,232 lines
- [completion/fim_context.py](../meltygui/completion/fim_context.py) — 481 lines
- [completion/providers/__init__.py](../meltygui/completion/providers/__init__.py) — 0 lines
- [completion/providers/anthropic_oauth.py](../meltygui/completion/providers/anthropic_oauth.py) — 446 lines
- [completion/providers/anthropic_requests.py](../meltygui/completion/providers/anthropic_requests.py) — 69 lines
- [completion/providers/claude.py](../meltygui/completion/providers/claude.py) — 203 lines
- [completion/providers/claude_usage.py](../meltygui/completion/providers/claude_usage.py) — 499 lines
- [completion/providers/codex_accounts.py](../meltygui/completion/providers/codex_accounts.py) — 180 lines
- [completion/providers/copilot.py](../meltygui/completion/providers/copilot.py) — 617 lines
- [completion/providers/oauth_popup.py](../meltygui/completion/providers/oauth_popup.py) — 220 lines
- [completion/providers/ollama.py](../meltygui/completion/providers/ollama.py) — 320 lines
- [completion/providers/profiles.py](../meltygui/completion/providers/profiles.py) — 23 lines

### debug

- [debug/__init__.py](../meltygui/debug/__init__.py) — 0 lines
- [debug/app_view_utils.py](../meltygui/debug/app_view_utils.py) — 9 lines
- [debug/attribute_churn.py](../meltygui/core/attribute_churn.py) — 38 lines
- [debug/framebuffer_recorder.py](../meltygui/core/framebuffer_recorder.py) — 337 lines
- [debug/invalidation_tracker.py](../meltygui/core/invalidation_tracker.py) — 43 lines
- [debug/jump_to.py](../meltygui/debug/jump_to.py) — 95 lines
- [debug/mode.py](../meltygui/core/mode.py) — 816 lines

### editor

- [editor/__init__.py](../meltygui/editor/__init__.py) — 0 lines
- [editor/bash_syntax.py](../meltygui/editor/bash_syntax.py) — 30 lines
- [editor/code_line_fast.py](../meltygui/editor/code_line_fast.py) — 273 lines
- [editor/diff.py](../meltygui/editor/diff.py) — 139 lines
- [editor/external_changes.py](../meltygui/editor/external_changes.py) — 259 lines
- [editor/file_header.py](../meltygui/editor/file_header.py) — 106 lines
- [editor/live_usage.py](../meltygui/editor/live_usage.py) — 169 lines
- [editor/live_view_views.py](../meltygui/editor/live_view_views.py) — 3,195 lines
- [editor/pending_save.py](../meltygui/editor/pending_save.py) — 1,442 lines
- [editor/roster_tints.py](../meltygui/editor/roster_tints.py) — 547 lines
- [editor/source_preview.py](../meltygui/editor/source_preview.py) — 67 lines
- [editor/source_tools.py](../meltygui/editor/source_tools.py) — 10 lines
- [editor/source_ui.py](../meltygui/editor/source_ui.py) — 70 lines
- [editor/spell_check.py](../meltygui/editor/spell_check.py) — 77 lines
- [editor/text_editor.py](../meltygui/editor/text_editor.py) — 16,696 lines
- [editor/usage_picker.py](../meltygui/editor/usage_picker.py) — 609 lines

### events

- [events/__init__.py](../meltygui/events/__init__.py) — 0 lines
- [events/example.py](../meltygui/events/example.py) — 118 lines
- [events/input_handler.py](../meltygui/core/input_handler.py) — 1,103 lines
- [events/pynput_backend.py](../meltygui/core/pynput_backend.py) — 1,054 lines
- [events/space_mouse.py](../meltygui/core/space_mouse.py) — 335 lines
- [events/touchpad_backend.py](../meltygui/core/touchpad_backend.py) — 393 lines

### examples

- [examples/__init__.py](../meltygui/examples/__init__.py) — 0 lines
- [examples/columns_demo.py](../meltygui/examples/columns_demo.py) — 33 lines
- [examples/context_menu_demo.py](../meltygui/examples/context_menu_demo.py) — 24 lines
- [examples/gui_playground.py](../meltygui/examples/gui_playground.py) — 127 lines
- [examples/live_view_playground.py](../meltygui/examples/live_view_playground.py) — 256 lines
- [examples/lora.py](../meltygui/examples/lora.py) — 43 lines
- [examples/lora_data.py](../meltygui/examples/lora_data.py) — 59 lines
- [examples/lora_policies.py](../meltygui/examples/lora_policies.py) — 67 lines
- [examples/scalar_policies.py](../meltygui/examples/scalar_policies.py) — 60 lines
- [examples/style_layouts.py](../meltygui/examples/style_layouts.py) — 149 lines
- [examples/tint_functions.py](../meltygui/examples/tint_functions.py) — 56 lines
- [examples/two_windows.py](../meltygui/examples/two_windows.py) — 36 lines

### files

- [files/__init__.py](../meltygui/files/__init__.py) — 0 lines
- [files/fast_file_explorer.py](../meltygui/files/fast_file_explorer.py) — 1,287 lines
- [files/file_selector.py](../meltygui/files/file_selector.py) — 82 lines
- [files/folder_files.py](../meltygui/files/folder_files.py) — 461 lines

### graphics

- [graphics/__init__.py](../meltygui/graphics/__init__.py) — 6 lines
- [graphics/base.py](../meltygui/graphics/base.py) — 85 lines
- [graphics/examples.py](../meltygui/graphics/examples.py) — 507 lines
- [graphics/executor.py](../meltygui/graphics/executor.py) — 520 lines
- [graphics/filter.py](../meltygui/graphics/filter.py) — 667 lines
- [graphics/generate_stubs.py](../meltygui/graphics/generate_stubs.py) — 22 lines
- [graphics/registry.py](../meltygui/graphics/registry.py) — 203 lines
- [graphics/shader_compiler.py](../meltygui/graphics/shader_compiler.py) — 155 lines
- [graphics/shaders.py](../meltygui/graphics/shaders.py) — 1,302 lines
- [graphics/stub_generator.py](../meltygui/graphics/stub_generator.py) — 250 lines
- [graphics/texture_manager.py](../meltygui/graphics/texture_manager.py) — 170 lines
- [graphics/texture_min_max.py](../meltygui/graphics/texture_min_max.py) — 295 lines

### models

- [models/__init__.py](../meltygui/models/__init__.py) — 0 lines
- [models/core_decoration.py](../meltygui/core/data_decoration.py) — 67 lines
- [models/dynamic_obj.py](../meltygui/core/dynamic_obj.py) — 90 lines
- [models/file_meta.py](../meltygui/models/file_meta.py) — 574 lines
- [models/function_console.py](../meltygui/models/function_console.py) — 184 lines
- [models/orchestration.py](../meltygui/models/orchestration.py) — 48 lines

### rendering

- [rendering/__init__.py](../meltygui/rendering/__init__.py) — 0 lines
- [rendering/core_render.py](../meltygui/core/core_render.py) — 6,509 lines
- [rendering/core_render_helpers.py](../meltygui/core/core_render_helpers.py) — 328 lines
- [rendering/decorators/__init__.py](../meltygui/rendering/decorators/__init__.py) — 0 lines
- [rendering/decorators/core_decoration.py](../meltygui/core/core_decoration.py) — 431 lines
- [rendering/decorators/invalidation_decoration.py](../meltygui/core/invalidation_decoration.py) — 153 lines
- [rendering/decorators/profile_decoration.py](../meltygui/core/profile_decoration.py) — 88 lines
- [rendering/decorators/window_decoration.py](../meltygui/core/window_decoration.py) — 25 lines
- [rendering/render_funcs.py](../meltygui/core/render_funcs.py) — 273 lines
- [rendering/shaped.py](../meltygui/core/shaped.py) — 312 lines

### state

- [state/__init__.py](../meltygui/state/__init__.py) — 0 lines
- [state/core_enums.py](../meltygui/state/core_enums.py) — 59 lines
- [state/core_markers.py](../meltygui/state/core_markers.py) — 115 lines
- [state/core_undo.py](../meltygui/state/core_undo.py) — 1,075 lines
- [state/dict_conversion.py](../meltygui/core/dict_conversion.py) — 1,815 lines
- [state/dict_conversion_util.py](../meltygui/core/dict_conversion_util.py) — 177 lines
- [state/graph_compare.py](../meltygui/core/graph_compare.py) — 183 lines
- [state/load_save_v2.py](../meltygui/core/load_save_v2.py) — 1,032 lines
- [state/missing_saved_class.py](../meltygui/core/missing_saved_class.py) — 36 lines
- [state/model_enums.py](../meltygui/state/model_enums.py) — 11 lines
- [state/module_names.py](../meltygui/core/module_names.py) — 19 lines
- [state/new_core_model.py](../meltygui/state/new_core_model.py) — 2,363 lines

### tensor

- [tensor/__init__.py](../meltygui/tensor/__init__.py) — 0 lines
- [tensor/cuda_interop.py](../meltygui/tensor/cuda_interop.py) — 265 lines
- [tensor/cuda_march.py](../meltygui/tensor/cuda_march.py) — 961 lines
- [tensor/line_kernels.py](../meltygui/tensor/line_kernels.py) — 115 lines
- [tensor/voxel_camera.py](../meltygui/model/camera_model.py) — 159 lines
- [tensor/voxel_playground.py](../meltygui/tensor/voxel_playground.py) — 3,069 lines

### utils

- [utils/__init__.py](../meltygui/utils/__init__.py) — 0 lines
- [utils/glfw_utils.py](../meltygui/core/glfw_utils.py) — 1,343 lines
- [utils/jump_to_code.py](../meltygui/utils/jump_to_code.py) — 344 lines
- [utils/pkl_inspect.py](../meltygui/utils/pkl_inspect.py) — 90 lines
- [utils/render_utils.py](../meltygui/utils/render_utils.py) — 1,419 lines
- [utils/singleton.py](../meltygui/core/singleton.py) — 16 lines
- [utils/thread_safe_bool.py](../meltygui/core/thread_safe_bool.py) — 24 lines
- [utils/thread_signal.py](../meltygui/core/thread_signal.py) — 30 lines

### views

- [views/__init__.py](../meltygui/views/__init__.py) — 0 lines
- [views/anywhere.py](../meltygui/core/parameter_core.py) — 1,661 lines
- [views/basic_view_utils.py](../meltygui/core/cursor_core.py) — 161 lines
- [views/blit_offscreen.py](../meltygui/core/tile_cache.py) — 5,968 lines
- [views/blit_offscreen_debug_renderers.py](../meltygui/core/cache_diagnostics.py) — 0 lines
- [views/columns.py](../meltygui/core/column_core.py) — 2,452 lines
- [views/core_meta.py](../meltygui/state/annotation_state.py) — 22 lines
- [views/core_settings.py](../meltygui/core/metadata_core.py) — 51 lines
- [views/cst_proxy.py](../meltygui/model/code_proxy_model.py) — 876 lines
- [views/drag_drop.py](../meltygui/core/drag_drop_core.py) — 1,525 lines
- [views/fa_icons.py](../meltygui/model/icon_model.py) — 1,024 lines
- [views/fast_dock.py](../meltygui/core/dock_core.py) — 688 lines
- [views/file_watch_debug.py](../meltygui/core/file_watch_core.py) — 147 lines
- [views/headers.py](../meltygui/core/header_runtime.py) — 915 lines
- [views/inspect_utils.py](../meltygui/core/inspection_core.py) — 169 lines
- [views/line_graph_playground.py](../meltygui/core/graph_core.py) — 789 lines
- [views/menu_bar.py](../meltygui/view/menu_view.py) — 263 lines
- [views/monitor.py](../meltygui/core/monitor_core.py) — 105 lines
- [views/new_core_view.py](../meltygui/core/render_dispatch.py) — 9,205 lines
- [views/search_glow.py](../meltygui/view/search_view.py) — 152 lines
- [views/split_overlay_renderer.py](../meltygui/core/overlay_renderer.py) — 984 lines
- [views/stack_trace_view.py](../meltygui/core/trace_core.py) — 1,002 lines
- [views/texture_view.py](../meltygui/view/texture_view.py) — 437 lines
- [views/tile_manager.py](../meltygui/core/tile_manager_core.py) — 543 lines
- [views/utils/__init__.py](../meltygui/views/utils/__init__.py) — 0 lines
- [views/utils/animator.py](../meltygui/state/animation_state.py) — 97 lines
- [views/utils/grid_dots_shader.py](../meltygui/core/grid_core.py) — 117 lines
- [views/utils/imgui_style_manager_class.py](../meltygui/core/style_core.py) — 522 lines
- [views/utils/view_utils.py](../meltygui/model/format_model.py) — 390 lines
- [views/view_func_selection.py](../meltygui/core/view_selection.py) — 177 lines

### widgets

- [widgets/__init__.py](../meltygui/widgets/__init__.py) — 0 lines
- [widgets/actions_playground.py](../meltygui/core/action_core.py) — 213 lines
- [widgets/change_value.py](../meltygui/core/value_core.py) — 1,752 lines
- [widgets/claude_terminals.py](../meltygui/core/claude_terminal_core.py) — 352 lines
- [widgets/columns_playground.py](../meltygui/examples/columns_window_demo.py) — 61 lines
- [widgets/context_menu_playground.py](../meltygui/examples/context_menu_window_demo.py) — 49 lines
- [widgets/crash_reports.py](../meltygui/model/trace_report_model.py) — 667 lines
- [widgets/file_graph.py](../meltygui/model/import_graph_model.py) — 468 lines
- [widgets/file_tree.py](../meltygui/core/file_tree_core.py) — 537 lines
- [widgets/import_graph_view.py](../meltygui/core/import_graph_core.py) — 344 lines
- [widgets/mcp_query_playground.py](../meltygui/core/query_core.py) — 253 lines
- [widgets/mode_test_playground.py](../meltygui/examples/mode_demo.py) — 49 lines
- [widgets/modifies_playground.py](../meltygui/examples/modifies_demo.py) — 121 lines
- [widgets/orchestrator.py](../meltygui/core/orchestration_core.py) — 3,283 lines
- [widgets/region_screenshot.py](../meltygui/core/screenshot_core.py) — 247 lines
- [widgets/selectors.py](../meltygui/core/selector_core.py) — 340 lines
- [widgets/space_mouse_playground.py](../meltygui/core/input_core.py) — 444 lines
- [widgets/stack_trace_playground.py](../meltygui/examples/trace_demo.py) — 88 lines
- [widgets/terminal_playground.py](../meltygui/core/terminal_core.py) — 1,101 lines
- [widgets/tile_manager_playground.py](../meltygui/examples/tile_manager_demo.py) — 42 lines
- [widgets/tint_debug.py](../meltygui/examples/tint_demo.py) — 160 lines

### windows

- [windows/__init__.py](../meltygui/windows/__init__.py) — 0 lines
- [windows/backends/__init__.py](../meltygui/windows/backends/__init__.py) — 0 lines
- [windows/backends/imgui_renderer.py](../meltygui/core/backends/imgui_renderer.py) — 138 lines
- [windows/backends/native_wayland.py](../meltygui/core/backends/native_wayland.py) — 850 lines
- [windows/backends/wayland_protocol.py](../meltygui/core/backends/wayland_protocol.py) — 101 lines

## core_render.py import sites

Includes imports inside functions. Repeated imports are retained so the exact
use sites can be inspected.

| Line | Internal module | Imported symbols |
|---:|---|---|
| 15 | `meltygui.window_api` | `(module)` |
| 17 | `meltygui.hdr_color` | `pack_color` |
| 20 | `meltygui.mouse_cursor` | `(module)` |
| 21 | `meltygui.resize_trace` | `(module)` |
| 22 | `meltygui.background` | `Background` |
| 23 | `meltygui.background` | `Pending` |
| 24 | `meltygui.events.input_handler` | `ALL_ACTIONS` |
| 25 | `meltygui.rendering.render_funcs` | `RenderFuncs` |
| 26 | `meltygui.toggles` | `Counters` |
| 27 | `meltygui.toggles` | `Toggles` |
| 28 | `meltygui.toggles` | `Tint` |
| 29 | `meltygui.toggles` | `SwooshMode` |
| 30 | `meltygui.mode_defaults` | `ModeDefaults` |
| 31 | `meltygui.code.cache_tree` | `UNSET_VALUE` |
| 32 | `meltygui.code.fileref` | `to_address` |
| 33 | `meltygui.code.fileref` | `Address` |
| 34 | `meltygui.code.path_finder` | `PendingState` |
| 35 | `meltygui.state.new_core_model` | `DrawState` |
| 36 | `meltygui.state.new_core_model` | `Hotkey` |
| 37 | `meltygui.state.new_core_model` | `DragMode` |
| 38 | `meltygui.state.new_core_model` | `Anchor` |
| 39 | `meltygui.state.new_core_model` | `Pin` |
| 40 | `meltygui.state.new_core_model` | `TileMode` |
| 41 | `meltygui.state.new_core_model` | `AttrDict` |
| 42 | `meltygui.state.new_core_model` | `TOP_ANCHORS` |
| 43 | `meltygui.state.new_core_model` | `LEFT_ANCHORS` |
| 44 | `meltygui.state.new_core_model` | `ExpandMode` |
| 45 | `meltygui.state.core_enums` | `PendingAction` |
| 46 | `meltygui.rendering.shaped` | `Shaped` |
| 47 | `meltygui.utils.render_utils` | `push_style_var` |
| 48 | `meltygui.utils.render_utils` | `pop_style_var` |
| 49 | `meltygui.utils.glfw_utils` | `request_render` |
| 50 | `meltygui.utils.glfw_utils` | `print_stack_trace` |
| 51 | `meltygui.utils.glfw_utils` | `trace_group` |
| 52 | `meltygui.utils.glfw_utils` | `get_live_frames` |
| 53 | `meltygui.melty` | `Melty` |
| 54 | `meltygui.melty` | `apply_collection_action` |
| 55 | `meltygui.melty` | `MeltyState` |
| 56 | `meltygui.melty` | `SearchTerm` |
| 57 | `meltygui.melty` | `search_walk` |
| 58 | `meltygui.views.basic_view_utils` | `same_line` |
| 59 | `meltygui.views.blit_offscreen` | `snap_int` |
| 60 | `meltygui.views.blit_offscreen` | `add_shadow` |
| 61 | `meltygui.views.blit_offscreen` | `clear_shadows` |
| 62 | `meltygui.views.core_meta` | `AnnotationOverride` |
| 63 | `meltygui.state.core_undo` | `UndoManager` |
| 64 | `meltygui.state.core_undo` | `handle_undo` |
| 65 | `meltygui.rendering.decorators.core_decoration` | `Core` |
| 66 | `meltygui.rendering.decorators.window_decoration` | `window` |
| 69 | `meltygui.views.drag_drop` | `(module)` |
| 70 | `meltygui.debug.invalidation_tracker` | `Note` |
| 516 | `meltygui.code.new_codecs` | `type_to_codec` |
| 782 | `meltygui.views.view_func_selection` | `configured_view_func` |
| 786 | `meltygui.notifications` | `notify` |
| 868 | `meltygui.views.headers` | `draw_header_end` |
| 869 | `meltygui.views.headers` | `draw_header` |
| 1092 | `meltygui.window_visibility` | `resolved_window_kwargs` |
| 1124 | `meltygui.window_visibility` | `requested_window_closed` |
| 1177 | `meltygui.surface` | `Surface` |
| 1427 | `meltygui.code.chain_converters` | `caller_site` |
| 1428 | `meltygui.code.chain_converters` | `call_stack_frames` |
| 1446 | `meltygui.code.fileref` | `is_editable_source` |
| 1504 | `meltygui.code.live_view` | `publish_stack_locals` |
| 1642 | `meltygui.code.chain_converters` | `call_stack_frames` |
| 1650 | `meltygui.code.fileref` | `is_editable_source` |
| 1660 | `meltygui.code.live_view` | `publish_stack_locals` |
| 1907 | `meltygui.code.new_codecs` | `Codec` |
| 1989 | `meltygui.style` | `resolve_font_style` |
| 1990 | `meltygui.fonts` | `Font` |
| 2253 | `meltygui.views.columns` | `(module)` |
| 2633 | `meltygui.os_frame` | `(module)` |
| 2795 | `meltygui.state.core_undo` | `NavUndo` |
| 2846 | `meltygui.views.columns` | `(module)` |
| 2847 | `meltygui.os_frame` | `(module)` |
| 3504 | `meltygui.views.new_core_view` | `pending_window` |
| 3506 | `meltygui.debug.mode` | `Mode` |
| 3513 | `meltygui.views.new_core_view` | `draw_search` |
| 3514 | `meltygui.views.new_core_view` | `search_pill_layout` |
| 4111 | `meltygui.views.new_core_view` | `compute_bg_color` |
| 4142 | `meltygui.views.new_core_view` | `draw_bg` |
| 4275 | `meltygui.views.new_core_view` | `draw_context_menu` |
| 4276 | `meltygui.views.new_core_view` | `draw_context_menu_items` |
| 4277 | `meltygui.views.new_core_view` | `INSPECT` |
| 4302 | `meltygui.events.input_handler` | `CLICK_MAX_DISTANCE` |
| 5195 | `meltygui.code.hotswap_guard` | `(module)` |
| 5430 | `meltygui.perf_trace` | `trace` |
| 5763 | `meltygui.views.new_core_view` | `run_scoped_eval` |
| 5813 | `meltygui.code.live_view` | `call_with_body_capture` |
| 6041 | `meltygui.rendering.decorators.window_decoration` | `(module)` |
| 6078 | `meltygui.views.new_core_view` | `draw_any` |

## Modules importing core_render.py

- `meltygui.accounts.internet_accounts`
- `meltygui.chat.chat_interface`
- `meltygui.code.chain_converters`
- `meltygui.code.file_converters`
- `meltygui.code.libcst_conversion`
- `meltygui.code.new_converters`
- `meltygui.code.render_host`
- `meltygui.debug.jump_to`
- `meltygui.editor.external_changes`
- `meltygui.editor.live_view_views`
- `meltygui.editor.pending_save`
- `meltygui.editor.source_preview`
- `meltygui.editor.text_editor`
- `meltygui.editor.usage_picker`
- `meltygui.examples.columns_demo`
- `meltygui.examples.context_menu_demo`
- `meltygui.examples.gui_playground`
- `meltygui.examples.live_view_playground`
- `meltygui.examples.lora_policies`
- `meltygui.examples.scalar_policies`
- `meltygui.examples.style_layouts`
- `meltygui.examples.tint_functions`
- `meltygui.files.fast_file_explorer`
- `meltygui.files.file_selector`
- `meltygui.files.folder_files`
- `meltygui.melty`
- `meltygui.tensor.voxel_playground`
- `meltygui.views.anywhere`
- `meltygui.views.blit_offscreen`
- `meltygui.views.columns`
- `meltygui.views.fast_dock`
- `meltygui.views.file_watch_debug`
- `meltygui.views.line_graph_playground`
- `meltygui.views.menu_bar`
- `meltygui.views.new_core_view`
- `meltygui.views.stack_trace_view`
- `meltygui.views.texture_view`
- `meltygui.views.view_func_selection`
- `meltygui.widgets.actions_playground`
- `meltygui.widgets.claude_terminals`
- `meltygui.widgets.columns_playground`
- `meltygui.widgets.context_menu_playground`
- `meltygui.widgets.crash_reports`
- `meltygui.widgets.file_tree`
- `meltygui.widgets.import_graph_view`
- `meltygui.widgets.mcp_query_playground`
- `meltygui.widgets.mode_test_playground`
- `meltygui.widgets.modifies_playground`
- `meltygui.widgets.orchestrator`
- `meltygui.widgets.space_mouse_playground`
- `meltygui.widgets.stack_trace_playground`
- `meltygui.widgets.terminal_playground`
- `meltygui.widgets.tile_manager_playground`
- `meltygui.widgets.tint_debug`
