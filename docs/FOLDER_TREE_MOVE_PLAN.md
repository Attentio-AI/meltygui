# Folder-tree feature: symbol-level placement plan

2026-09-16. Planning record with first implementation completed.

Actual destinations use the subsequently agreed filenames:
`model/file_model.py`, `view/file_view.py`, and `core/file_core.py`.
The adapter functions and reusable presentation have been extracted. Registered
window/lifecycle wrappers remain at `files/folder_files.py` for compatibility;
no examples migration or new state class was forced into this slice. Polling
behavior is preserved in core. The tables below retain the original proposed
split to distinguish remaining contract work from the completed file move.

Validation: 33 targeted tests passed; old-source migration/hotswap checks passed.
Isolated UI comparison found the same nested-Path renderer issue in the original
and extracted paths. That pre-existing issue remains unresolved.

Source reviewed: `meltygui/files/folder_files.py` (the complete module), plus its
host notification contract, metadata store references, public exports and callers.

## Intended arrangement

```text
core/
    core_render.py
    melty.py
    render_host.py
    ...                         # shared subscriptions and lifetime machinery
models/files/
    folder_tree.py              # filesystem ↔ mutable dictionary adapter
    file_meta.py                # existing metadata model and store
views/files/
    folder_tree.py              # rendering the held tree
    file_meta.py                # rendering metadata supplied as a value
examples/
    folder_tree.py              # concrete roots, hosts and demo windows
files/
    folder_files.py             # temporary compatibility facade at old path
```

The compatibility facade is transitional, not a fourth ownership category.
Public names keep their behavior during migration; the reusable renderer is
separated from the existing public function's window/demo composition.
Do not rename an existing API and silently change its input contract in the
same move.

## Data and edit path

```text
filesystem
    -> snapshot + reconciliation in folder adapter
    -> RenderHost holds {"value": tree}
    -> reusable tree renderer receives tree
    -> draw_collection renders dicts and Path leaves
    -> mutable edits remain in the SAME held tree
    -> adapter plans creates/moves/deletes and applies metadata edits
```

The generic host's `value_key` need not always be "value"; that is this feature's
current host configuration. The reusable tree renderer should not know about
this envelope or private host fields. The composition/host boundary unwraps it.
The adapter must distinguish inbound disk changes from user edits; maintaining
`seen` and snapshot state is essential to that distinction.

## Exact function placement

All 17 top-level functions in the current folder module are covered below.
Destination names describe the proposal, not changes already made.

| Current symbol | Target | Treatment |
|---|---|---|
| `_scan` | models/files/folder_tree.py | Keep filesystem-to-dict traversal; no drawing dependencies |
| `_create` | models/files/folder_tree.py | Keep create/move handling and pending-delete pairing |
| `_delete` | models/files/folder_tree.py | Keep deletion policy and Toggles.FileSafety gate |
| `_reconcile` | models/files/folder_tree.py | Keep in-place reconciliation and global create-before-delete order |
| `_collect` | models/files/folder_tree.py | Keep recursive edit planning, snapshot and seen-path bookkeeping |
| `_file_meta` | models/files/file_meta.py or compatibility facade | Reuse existing file_meta_store; avoid a second store. Retain old helper while callers migrate |
| `_apply_meta` | models/files/folder_tree.py | Keep adapter-specific metadata-to-tree mapping and bubbling behavior |
| `_collect_meta` | models/files/folder_tree.py | Keep tree-to-metadata mapping and key-order persistence |
| `_init_file_meta` | models/files/file_meta.py + core lifecycle binding | Metadata upgrade/materialization belongs to model; lifecycle registration belongs to wiring. Preserve registration and invocation timing |
| `file_meta_debug` | views/files/file_meta.py + composition wrapper | Extract renderer taking metadata as input; current window retrieves global store and supplies it |
| `folder_io` | models/files/folder_tree.py + core bridge | Adapter before/after-view behavior stays feature-owned; host subscription/cache details stay in core. Remove embedded root-label drawing into presentation |
| `folder_proxy` | models/files/folder_tree.py factory + core-owned shared registry | Preserve callable API and per-root host reuse; feature factory constructs adapter, generic host remains core |
| `watch_folder` | core wiring bridge; old function temporarily forwards | Subscription/startup is not renderer responsibility. Resolve lifecycle using existing host machinery first |
| `_draw_tree` | views/files/folder_tree.py + composition wrapper | Extract draw_collection presentation; remove polling/startup and host-envelope access from renderer |
| `draw_folder_files` | examples/folder_tree.py composition + public compatibility wrapper | Keep existing public behavior while delegating to reusable view; do not classify current module-level window as a pure renderer |
| `draw_test_folders` | examples/folder_tree.py | Explicit sample root/host/window composition |
| `_poll_loop` | model scan operation + core scheduling/publication | Filesystem scanning is feature-specific; lifecycle, waking and reaching consumers is shared wiring |

The table is checked against the Python AST when updating the plan.

## State and module-level objects

| Current object | Proposed owner |
|---|---|
| `ROOT`, `TEST_FOLDER` | Example/application composition supplies roots explicitly. ROOT currently derives from this source file's parent, so physically moving that expression would CHANGE the displayed directory |
| `files_proxy`, `test_folder_proxy` | Composition-owned instances with compatibility access for old imports and saved references |
| `_disk_trees` | Per-root adapter state, held by adapter/host objects rather than an unrelated new global dictionary |
| `draw_state._seen_paths` | Per-root adapter state; not a view-specific ad-hoc DrawState field |
| `_proxies` | Shared root-to-host reuse owned through Melty/core; preserve multiple windows sharing a root |
| `_window_dss` | Replace direct feature-owned view registry with existing core/host subscription contract if it supports the required behavior |
| `_poller_running` | Core runtime lifecycle ownership; no startup flag in the reusable renderer |
| `_META_SKIP_SUFFIXES` | Keep as a local in metadata initialization if single-use; promote to Toggles only if shared |

Small choices of new attribute/class names are deliberately deferred. No new
DrawState field is proposed. Adding generic machinery is not justified until
existing RenderHost/Melty behavior is checked against the contract below.

## Existing notification seam and unresolved contract

`RenderHost.notify_on_change(draw_state)` already registers external consumers,
records their last-seen frame and revives deregistered hosts. This is a candidate
for replacing `_window_dss`, not proof that the replacement is mechanical.

The current folder poller invalidates three targets: the host's private
`_wrapper_draw_state`, private `_draw_state`, and one `_window_dss[root]`.
Metadata application additionally calls `Melty.cache.invalidate_up` so cached
children repaint. A correct shared bridge must cover BOTH an incoming filesystem
snapshot that requires adapter execution and notification after the held value
changes. Consumer registration alone does not demonstrate that first step.

Resolve these cases before implementing the bridge:

- Two windows reading one root both update, including when their bodies are cached.
- Two distinct roots retain independent snapshots and seen-path history.
- Host materialization wakes all consumers without feature code accessing private draw states.
- Metadata-only changes refresh affected rows without creating a false outbound edit.
- Closing/reopening views and hotswapping preserve or restore correct subscriptions;
  no duplicate workers or replacement of live host identity.
- Background scanning publishes results through supported runtime coordination;
  GL and rendering remain on the render thread.

If Melty cannot express this contract, report the precise gap rather than copying
manual invalidation into the new view. No replacement API is invented by this plan.

## Presentation extraction details

The current `_draw_tree` does three jobs: watch the root, unwrap the host's
"value", and call draw_collection. Only the last belongs to the reusable view.
The view takes the held dictionary plus injected state and explicit display
parameters. FILE_TREE mode configuration and child display settings remain
presentation concerns; filesystem knowledge does not enter the drawing body.

`_draw_tree` currently discards draw_collection's return, and the two window
wrappers return `(False, None)`. Their current host/bubbling behavior must be
traced before changing that. A newly extracted reusable renderer must propagate
an accurate `(changed, value)` without reporting inbound refresh as a user edit.
This is a contract check, not a claim that the current windows necessarily lose
edits.

`file_meta_debug` currently fetches the shared metadata store and draws it. Its
reusable part should accept that store as its input; global lookup and window
composition stay outside. `folder_io` also calls `imgui.text(str(root))`; place
that display in its settings/composition view, not in filesystem adaptation.

## Compatibility and load-time behavior

Observed consumers in the library:

- `meltygui/__init__.py` has both a TYPE_CHECKING import and lazy string export
  for public `draw_folder_files`.
- `widgets/file_tree.py` imports `folder_proxy`, `watch_folder`, and `_file_meta`.
  Its RenderHost demo is commented out, but the imports still execute.
- `state/module_map.json` maps two historical latent-descent module paths and
  `meltygui.files.folder` to the current module. Splitting a module into several
  destinations requires preserving old exports or symbol-specific mappings;
  one blanket module rename is insufficient.
- `test_public_boundary.py` imports the public draw_folder_files name.

Importing folder_files currently creates hosts and registers `_init_file_meta`
with `Melty.on_load`. Moving every such statement to an unimported example would
change application behavior. Separate reusable implementation from activation,
then keep the old activation path until applications are explicitly migrated.
Consumers outside this repository have not yet been searched.

Do not recreate hosts on hotswap, change roots by moving `__file__` expressions,
or silently stop metadata initialization. Preserve generic RenderFuncs lookup
and function/class identities required by live sessions and serialization.

## Implementation order once planning is accepted

1. Establish characterization checks for reconciliation/edit ordering and host
   refresh behavior using temporary directories; inspect current application
   consumers and saved-name usage before choosing compatibility duration.
2. Extract filesystem/metadata helpers with behavior unchanged. Keep forwarding
   bindings at the old path; ensure live registered callbacks still resolve.
3. Extract value-based tree and metadata renderers. Keep current windows as
   composition wrappers, preserving roots, instances and public imports.
4. Resolve subscription/lifecycle plumbing through RenderHost/Melty. This may
   require a focused framework improvement, not feature-local workaround code.
5. Move explicit demo composition after startup compatibility is accounted for.
6. Verify two roots/two consumers, external changes, metadata, create/move/delete
   ordering, window close/reopen and hotswap. Run relevant import/package checks
   and isolated UI verification for the actual implementation.

Tests have not been added or run for this documentation-only pass. The current
suite search found public-boundary and file-codec coverage, but no dedicated
folder reconciliation test in this repository. A future test should exercise
behavior (especially preserving file contents across moves), not mirror helper
implementation.
