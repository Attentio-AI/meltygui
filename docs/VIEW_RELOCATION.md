# Feature view organization

2026-09-16. Reusable renderers live in `meltygui/view/<feature>_view.py` as
plain functions. This pass moved 142 view definitions into 27 feature modules,
plus 29 supporting definitions into model, state and core modules. The earlier
file-tree and metadata views remain in `file_view.py`.

Public `meltygui.draw_*` exports resolve to feature modules. Temporary forwarding
imports have now been removed, and consumers use canonical modules. Saved names
translate on load; definition hotswap remains independent of import compatibility.
No view classes were introduced. The validation below records earlier stages.

## Layout

| Module | Render functions |
|---|---|
| [account_view.py](../meltygui/view/account_view.py) | `draw_internet_accounts` |
| [action_view.py](../meltygui/view/action_view.py) | `draw_actions`, `draw_action_runner` |
| [chat_view.py](../meltygui/view/chat_view.py) | `draw_chat_sidebar`, `draw_chat_terminal`, `draw_chat_queue`, `draw_messages`, `draw_chat_requests`, `draw_conversation_title`, `draw_chat_navigation`, `draw_effort_slider`, `draw_chat_interface` |
| [code_view.py](../meltygui/view/code_view.py) | `draw_live_view_overlay`, `draw_live_view_marker`, `draw_snapshot_overlay`, `draw_function_live`, `draw_source_preview`, `draw_pending_preview`, `draw_usage_picker`, `run_button`, `draw_with_view_funcs`, `draw_code_tabs_from_cache`, `draw_text_from_code_cache`, `draw_module`, `draw_type_name`, `draw_symbol_usage`, `draw_property`, `draw_type`, `draw_usage`, `draw_comment`, `draw_parameter`, `draw_function`, `draw_jump_to`, `draw_code_line_fast` |
| [collection_view.py](../meltygui/view/collection_view.py) | `draw_collection_as_tabs`, `draw_collection`, `draw_tuple`, `draw_tuple_fast`, `draw_mapping_proxy` |
| [color_view.py](../meltygui/view/color_view.py) | `draw_style_policy_fast`, `draw_style_residuals_fast`, `draw_view_offsets_fast`, `draw_color_picker`, `draw_tint_context` |
| [control_view.py](../meltygui/view/control_view.py) | `empty`, `button`, `draw_none`, `draw_bool`, `text`, `draw_str`, `draw_float_ctx`, `draw_float`, `draw_button`, `draw_int`, `draw_enum`, `draw_single`, `draw_blank` |
| [decoration_view.py](../meltygui/view/decoration_view.py) | `draw_drag_drop_target`, `draw_vertical_scrollbar`, `draw_bg` |
| [diagnostic_view.py](../meltygui/view/diagnostic_view.py) | `draw_frame`, `render_profiler_time`, `draw_style_manager`, `draw_debug_label`, `draw_debug`, `draw_undo_manager`, `draw_draw_state_info`, `draw_pending`, `pending_window`, `draw_draw_state` |
| [dropdown_view.py](../meltygui/view/dropdown_view.py) | `draw_drop_down_item`, `draw_dropdown`, `draw_dd_menu`, `dd_menu_row` |
| [file_view.py](../meltygui/view/file_view.py) | `draw_external_changes`, `draw_pending_saves`, `draw_file_listing`, `draw_shortcuts`, `draw_fast_file_explorer`, `draw_file_selector`, `file_watch_debug`, `render_file_tree`, `draw_changed_file_header` |
| [graph_view.py](../meltygui/view/graph_view.py) | `draw_line_graph`, `render_import_graph` |
| [header_view.py](../meltygui/view/header_view.py) | `render_search`, `draw_header_arrow`, `draw_header`, `draw_footer`, `draw_header_end`, `flat_button` |
| [input_view.py](../meltygui/view/input_view.py) | `render_puck`, `draw_space_mouse` |
| [inspection_view.py](../meltygui/view/inspection_view.py) | `draw_with_modes`, `draw_view_func_selector`, `draw_param_matrix`, `draw_lens`, `context_menu_settings`, `draw_info_param`, `draw_info_tab`, `draw_config_tab`, `draw_live_tab`, `draw_func_tab`, `draw_eval_tab`, `draw_input_tab`, `draw_class_tab`, `draw_mode_tab`, `draw_context_menu_items`, `draw_context_menu` |
| [layout_view.py](../meltygui/view/layout_view.py) | `draw_columns`, `draw_rows`, `draw_fast_dock` |
| [menu_view.py](../meltygui/view/menu_view.py) | `draw_menu_bar` |
| [orchestration_view.py](../meltygui/view/orchestration_view.py) | `draw_orchestrator` |
| [query_view.py](../meltygui/view/query_view.py) | `draw_mcp_query` |
| [search_view.py](../meltygui/view/search_view.py) | `draw_search`, `draw_search_highlight`, `draw_search_highlight_multi` |
| [tab_view.py](../meltygui/view/tab_view.py) | `draw_tab_bar`, `draw_enum_tabs` |
| [tensor_view.py](../meltygui/view/tensor_view.py) | `draw_tensor_dim`, `draw_tensor_slices`, `draw_tensor_error` |
| [voxel_view.py](../meltygui/view/voxel_view.py) | `draw_voxels`, volume passes and axis labels |
| [lut_view.py](../meltygui/view/lut_view.py) | `draw_lut`, palette swatches |
| [terminal_view.py](../meltygui/view/terminal_view.py) | `draw_terminal_screen`, `draw_terminal`, `draw_session_terminal` |
| [text_view.py](../meltygui/view/text_view.py) | `draw_icon_selector_plain`, `draw_bool_token`, `draw_number_token`, `draw_bool_token_plain`, `draw_number_token_plain`, `draw_color3_token`, `draw_color3_token_plain`, `draw_colorhex_token_plain`, `draw_fnrun_params_panel`, `draw_run_fn_token_plain`, `draw_text` |
| [texture_view.py](../meltygui/view/texture_view.py) | `draw_texture`, `draw_pending_texture` |
| [trace_view.py](../meltygui/view/trace_view.py) | `draw_stack_trace`, `draw_crash_reports` |
| [window_view.py](../meltygui/view/window_view.py) | `draw_managed_window` |

## Hotswap follows the definition

`core/definition_hotswap.py` reconciles exact object identities across loaded
module exports and injected-state metadata. It is independent of feature names
and of the render function's module. `code/file_converters.py` uses it for
whole-module edits and class methods.

Python function globals cannot be reassigned. On relocation, an existing
callable forwards to its destination implementation, which reads the new
module's globals. Melty owns that routing table. Inspection unwraps to the real
source, and subsequent span or whole-module edits update the live definition.
Existing class instances, bound methods, wrapper identities and compatible
closure state survive. Runtime-error rollback restores the previous callable.
Normal edits within a module still patch directly.

When applying a multi-file source move to an already running process, update
any previously loaded destination module before compatibility modules import
its newly added exports. Newly created modules load normally on first import.
No per-view migration/adoption code is needed.

## Verification

- 202 editor, model, hotswap and package tests, plus 376 subtests.
- 43 render, layout and text-layout tests.
- All 47 destination modules imported in independent Python processes.
- A migration exercise loaded the actual pre-move sources, applied the new
  modules, and verified all 171 destination exports retained their live identities.
- An isolated GUI rendered the file picker, boolean/string/number controls and
  syntax-highlighted text editor.
- `tests/test_definition_relocation.py` covers destination edits, imported
  consumers, injected-state metadata, bound methods, closure state and rollback.
  `tools/reproduce_view_relocation.py` is a standalone passing identity check.

## Remaining organization work

This is a view-definition extraction, not the completed repository reorganization.
Converters, dispatch, RenderHost, Melty, core_render, demo composition and raw
platform/GPU rendering remain in their existing modules. Some extracted views
still call feature helpers in legacy modules; those helpers can move into model,
state or core during the corresponding feature cleanup. Their existing behavior
was preserved here rather than redesigned during the move.

## Full code-editor smoke check

The standalone Melt code editor exposed a tensor-view indentation error left
by the final cleanup after the initial checks. Fixed the indentation, then
rechecked compilation of all view/model/state/core modules and all 47 independent
imports. Eleven hotswap/package tests passed.

Using an isolated session and temporary files, verified startup, Python syntax
highlighting, tab switching, text entry, in-file search with matching highlights,
and the Open File dialog. Both test edits were written on close; reopening
without file arguments restored the tabs and edited content. Session restoration
logged nonfatal missing `__main__.mark_folder` / `unmark_folder` callback warnings;
the editor still rendered normally. Those warnings were not changed in this pass.

## Second pass: feature helpers

Moved another 64 helper definitions: 23 presentation/geometry functions, 27
model helpers, 13 core helpers and one menu-state helper. Main render functions
keep their names and behavior. The following ownership now applies:

| Feature | View | Model / state | Core |
|---|---|---|---|
| Color | Picker geometry, three picker bodies, textures and anchors in `color_view.py` | Color arithmetic and brightness clamping in `color_model.py` | Style-policy lookup and source editing in `color_core.py` |
| Dropdown | Popup geometry, row labels/tags and painting in `dropdown_view.py` | Tree traversal, filtering and selection paths in `dropdown_model.py` | Keyboard/focus handling, scrolling and cache invalidation in `dropdown_core.py` |
| Search | Highlight geometry/glow and find-pill layout in `search_view.py` | Fuzzy matching and word matching in `search_model.py` | Target activation in `search_core.py` |
| Collection | Existing collection views | Key filtering and annotation element types in `collection_model.py` | — |
| Header / menu | Existing feature views | Menu-state initialization in `menu_state.py` | Jump-to-source in `header_core.py`; menu close handling in `dropdown_core.py` |
| Trace | File-header painting joins `trace_view.py` | Existing trace model/state | — |

Shared picker/dropdown dimensions and colors now live in `Toggles.ColorPicker`
and `Toggles.Dropdown`. Old constants and helper imports remain available for
compatibility; active feature code uses the canonical homes. Legacy
`views/search_glow.py`, `views/menu_bar.py` and `views/texture_view.py` are concise
compatibility modules. The header's existing text-color memo remains at its
legacy runtime location pending the broader global-state consolidation.

A final test rerun caught an intermittent definition-hotswap metadata bug:
replaced immutable metadata could be collected mid-walk, allowing an object id
to be reused. Canonicalization now retains the original object alongside its
replacement for the duration of the walk. A regression test checks distinct
defaults and annotations across 128 imported consumers.

Validation: 51 targeted tests passed; relocation regressions also passed in
five fresh processes. All 70 core/model/state/view modules imported independently.
The live migration exercise preserved all 64 helper identities. The isolated GUI
rendered all three color-picker modes and accepted color edits and nested-dropdown
keyboard selection. The full Melt code editor started and displayed highlighted
in-file search matches. All feature modules compiled after the final code edits.

## Legacy packages emptied

All remaining implementation files in `views/` and `widgets/` have moved to
canonical core, model, state, view or examples modules. Only import namespaces
remain in the old directories. [Full mapping and validation](LEGACY_MODULE_MOVE.md).
The text-editor implementation is frozen for a separate, deliberate refactor.
