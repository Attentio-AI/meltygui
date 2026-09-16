# Legacy view and widget packages retired

2026-09-16. All implementation files have moved out of `meltygui/views/` and
`meltygui/widgets/`. Their three remaining `__init__.py` files only establish
legacy import namespaces (`views`, `views.utils`, and `widgets`).

This pass relocates modules by their remaining responsibility: rendering
infrastructure, layout and process coordination live in core; dictionary/code
adapters and data live in model; annotation and animation state live in state;
demo composition lives in examples. The feature render functions already
extracted into `view/<feature>_view.py` stay there.

No text-editor implementation was changed. Every `.py` file under `editor/`
and `view/text_view.py` is byte-for-byte identical to its pre-pass contents.
Those files can keep their legacy imports until the deliberate editor refactor.

## Compatibility and live state

`core/module_compatibility.py` installs the aliases listed in
`core/legacy_modules.json`. An old import returns the canonical module itself,
so reads and writes share one namespace. Existing modules are adopted without
executing their bodies again: singleton caches, worker threads, callbacks and
class/function identities are retained. Source metadata and cached Addresses
are redirected to the new file so inspection and later hotswaps follow it.

This adoption applies to whole-file moves with unchanged line layout. Runtime
logic was not rewritten during these relocations; future function-level splits
use the separate definition-hotswap mechanism. Saved identifiers are translated
through the updated `state/module_map.json`, including pre-MeltyGUI names.

## Destinations

| Previous module (under `meltygui/`) | Canonical module |
|---|---|
| `views/anywhere.py` | [core/parameter_core.py](../meltygui/core/rendering/parameter_core.py) |
| `views/basic_view_utils.py` | [core/cursor_core.py](../meltygui/core/layout/cursor_core.py) |
| `views/blit_offscreen.py` | [core/tile_cache.py](../meltygui/core/cache/tile_cache.py) |
| `views/blit_offscreen_debug_renderers.py` | [core/cache_diagnostics.py](../meltygui/core/cache/cache_diagnostics.py) |
| `views/columns.py` | [core/column_core.py](../meltygui/core/layout/column_core.py) |
| `views/core_meta.py` | [state/annotation_state.py](../meltygui/state/annotation_state.py) |
| `views/core_settings.py` | [core/metadata_core.py](../meltygui/core/files/metadata_core.py) |
| `views/cst_proxy.py` | [model/code_proxy_model.py](../meltygui/model/code_proxy_model.py) |
| `views/drag_drop.py` | [core/drag_drop_core.py](../meltygui/core/input/drag_drop_core.py) |
| `views/fa_icons.py` | [model/icon_model.py](../meltygui/model/icon_model.py) |
| `views/fast_dock.py` | [core/dock_core.py](../meltygui/core/windowing/dock_core.py) |
| `views/file_watch_debug.py` | [core/file_watch_core.py](../meltygui/core/files/file_watch_core.py) |
| `views/headers.py` | [core/header_runtime.py](../meltygui/core/layout/header_runtime.py) |
| `views/inspect_utils.py` | [core/inspection_core.py](../meltygui/core/diagnostics/inspection_core.py) |
| `views/line_graph_playground.py` | [core/graph_core.py](../meltygui/core/graphics/graph_core.py) |
| `views/menu_bar.py` | [view/menu_view.py](../meltygui/view/menu_view.py) |
| `views/monitor.py` | [core/monitor_core.py](../meltygui/core/diagnostics/monitor_core.py) |
| `views/new_core_view.py` | [core/render_dispatch.py](../meltygui/core/rendering/render_dispatch.py) |
| `views/search_glow.py` | [view/search_view.py](../meltygui/view/search_view.py) |
| `views/split_overlay_renderer.py` | [core/overlay_renderer.py](../meltygui/core/graphics/overlay_renderer.py) |
| `views/stack_trace_view.py` | [core/trace_core.py](../meltygui/core/diagnostics/trace_core.py) |
| `views/texture_view.py` | [view/texture_view.py](../meltygui/view/texture_view.py) |
| `views/tile_manager.py` | [core/tile_manager_core.py](../meltygui/core/layout/tile_manager_core.py) |
| `views/utils/animator.py` | [state/animation_state.py](../meltygui/state/animation_state.py) |
| `views/utils/grid_dots_shader.py` | [core/grid_core.py](../meltygui/core/layout/grid_core.py) |
| `views/utils/imgui_style_manager_class.py` | [core/style_core.py](../meltygui/core/styling/style_core.py) |
| `views/utils/view_utils.py` | [model/format_model.py](../meltygui/model/format_model.py) |
| `views/view_func_selection.py` | [core/view_selection.py](../meltygui/core/input/view_selection.py) |
| `widgets/actions_playground.py` | [core/action_core.py](../meltygui/core/automation/action_core.py) |
| `widgets/change_value.py` | [core/value_core.py](../meltygui/core/automation/value_core.py) |
| `widgets/claude_terminals.py` | [core/claude_terminal_core.py](../meltygui/core/services/claude_terminal_core.py) |
| `widgets/columns_playground.py` | [examples/columns_window_demo.py](../meltygui/examples/columns_window_demo.py) |
| `widgets/context_menu_playground.py` | [examples/context_menu_window_demo.py](../meltygui/examples/context_menu_window_demo.py) |
| `widgets/crash_reports.py` | [model/trace_report_model.py](../meltygui/model/trace_report_model.py) |
| `widgets/file_graph.py` | [model/import_graph_model.py](../meltygui/model/import_graph_model.py) |
| `widgets/file_tree.py` | [core/file_tree_core.py](../meltygui/core/files/file_tree_core.py) |
| `widgets/import_graph_view.py` | [core/import_graph_core.py](../meltygui/core/files/import_graph_core.py) |
| `widgets/mcp_query_playground.py` | [core/query_core.py](../meltygui/core/automation/query_core.py) |
| `widgets/mode_test_playground.py` | [examples/mode_demo.py](../meltygui/examples/mode_demo.py) |
| `widgets/modifies_playground.py` | [examples/modifies_demo.py](../meltygui/examples/modifies_demo.py) |
| `widgets/orchestrator.py` | [core/orchestration_core.py](../meltygui/core/automation/orchestration_core.py) |
| `widgets/region_screenshot.py` | [core/screenshot_core.py](../meltygui/core/diagnostics/screenshot_core.py) |
| `widgets/selectors.py` | [core/selector_core.py](../meltygui/core/automation/selector_core.py) |
| `widgets/space_mouse_playground.py` | [core/input_core.py](../meltygui/core/input/input_core.py) |
| `widgets/stack_trace_playground.py` | [examples/trace_demo.py](../meltygui/examples/trace_demo.py) |
| `widgets/terminal_playground.py` | [core/terminal_core.py](../meltygui/core/services/terminal_core.py) |
| `widgets/tile_manager_playground.py` | [examples/tile_manager_demo.py](../meltygui/examples/tile_manager_demo.py) |
| `widgets/tint_debug.py` | [examples/tint_demo.py](../meltygui/examples/tint_demo.py) |

## Verification

- All 48 old/new import pairs passed in isolated Python processes.
- An actual-source migration exercise retained all 48 aliases, cached objects,
  live definitions and the already-running monitor worker.
- Unit tests cover both cold import orders, live module moves, cached source
  Addresses, subsequent edits at the new path, and old saved class identifiers.
- The broad non-CUDA suite initially passed 513 tests and 383 subtests. Its one
  stale physical-path fixture was updated and passed on rerun; the additional
  saved-class compatibility test passed as well. CUDA/voxel GPU tests were not
  included in this structural pass.
- Melt code editor started in an isolated session; tab switching, in-file search
  and its highlights, and the Open File dialog were checked visually.
- All Python modules compile. Frozen editor files were compared against the
  pre-pass snapshot.
