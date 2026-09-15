# MeltyGUI

Standalone framework extracted from latent-descent. All runtime imports use
`meltygui`; do not add `src`, `lsd`, `melty`, or checkout paths to sys.path.
The original latent-descent tree is a reference, not a runtime dependency.

## Style

- Render functions return `(changed, value)`; mutate mutable values in place.
- View-local state is an injected `DictConversion`; initialize its fields in
  `__init__`. Do not add fields to DrawState without Lukas's confirmation.
- Use the draw list, `flat_button`, and `draw_dropdown`, not raw imgui widgets.
- Call nested and native windows every frame with `open_requested`; do not gate
  their lifecycle on click events. Both backends must behave identically.
- Consume background results before drawing; dispatch requested work afterwards.
- Never hash file contents to detect staleness. Use mtime, generation, or identity.
- Keep constants near the top of the function; shared settings live in Toggles.
  Access Toggles with fully spelled attribute chains. Icons are literal f-strings.
- Changes must preserve hotswap and live runtime state. If a framework/hotswap
  failure occurs, report it; do not hide it with a restart workaround.
- Subprocesses use full-path executables, `close_fds=False`, no `cwd`, `preexec_fn`,
  or `start_new_session`; the live subinterpreter makes fork unsafe.
- ShaderRegistry is not thread safe. GL work happens on the render thread.

## Checks

`uv pip install --python .venv/bin/python --find-links dist/release -e . --group dev`, then `.venv/bin/pytest`.
Build and test a noneditable wheel outside the checkout before release.
For UI verification, follow /home/lukas/AGENTS.md and reserve an agent desktop.
