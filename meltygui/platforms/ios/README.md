# MeltyGUI on iOS

UIKit owns one native surface and display link; Metal owns GPU submission.
MeltyGUI supplies the application lifecycle, ordinary window/view rendering,
input, settings, persistence and tile cache. The same application entry module
runs on desktop and iOS. Applications do not implement a native adapter.

## Application inputs

The build tools are installed with MeltyGUI:

```sh
python -m meltygui.platforms.ios stage --help
python -m meltygui.platforms.ios generate --help
```

They read existing `[tool.melty.app]` metadata from the application's
`pyproject.toml`. A minimal application can use:

```toml
[tool.melty.app]
entry = "main.py"
bundle_id = "org.example.counter"
dependencies = ["meltygui"]
```

`entry` can be a relative Python file or dotted module. `sources` defaults to
`["*.py"]`; package applications should list their package directories.
`resources` lists additional relative paths or glob patterns (fonts, templates,
images). `dependencies` defaults to `[project].dependencies`, or `["meltygui"]`
for a script app without project metadata. Development checkout paths are build
inputs, never runtime import paths. The app's normal `meltygui.boot(app_id=...)`
continues to choose its saved-session identity.

The app's root `.desktop` launcher also supplies its iOS icon automatically, on
project generation and before each device Run. No icon setting or resource glob
is needed. Selection follows the folder icon resolver (matching launcher name
first); `X-HDR-Icon` takes precedence over `Icon`. Absolute, project-relative and
theme icon paths are supported. When a checkout moves to the Mac, stale absolute
paths can be rebased against the project name or a launcher `Exec` executable
that also exists in the new checkout.

HDR artwork must be a 16-bit Rec.2020/PQ PNG with a cICP tag. It is resized in
premultiplied linear light and written as straight-alpha 16-bit PQ RGBA PNGs,
preserving full transparency with cICP and content-light metadata. iPhone/iPad icon
files are registered through `CFBundleIcons` and copied without PNG compression.
This deliberately bypasses `actool`: Xcode's app-icon asset compilation converts
PQ artwork to 8-bit sRGB. The built bundle preserves HDR; the system launcher
controls how it displays those files. The Home Screen's native Default/Dark icon
appearance supplies the background behind the transparent artwork; the build
does not paint a background. This was checked with the iOS 27.2 Home Screen.
This path has not been validated for App Store submission or for HDR brightness
on the Home Screen.

Artwork is fitted without cropping onto a transparent square. By default,
`Toggles.Mobile.icon_auto_margin` tops up the visible foreground's existing inset
to 10% of the icon's width on each edge. The estimate ignores nearly invisible
shadow tails and isolated specks; these pixels remain in the artwork. Sufficiently
padded artwork keeps its original fit. HDR and SDR use the same geometry, with
transparent padding. Disable the toggle to retain the original fit; changing it
regenerates the icons on the next Run.

A declared icon that cannot be resolved or decoded produces an actionable error;
a declared HDR icon never silently falls back to SDR. Apps without launcher
icons continue to build. Icon edits/additions/removal trigger a native rebuild
and reinstall; unchanged icons preserve source-only device updates. Generated
artwork and its dependency receipt live in the app's iOS build directory.

`examples/portable_counter` is a second app with no Pro dependency and no
platform Python code. The editor likewise uses its existing `editor.py`.

## Device dependencies

Use full Xcode with the iPhoneOS SDK and MetalToolchain. Before a configured
app's device Run, MeltyGUI prepares missing or mismatched build inputs. It
downloads the matching BeeWare iOS Python support package, or builds it from
source when a release is unavailable. Missing native wheels are downloaded
when compatible builds exist, otherwise compiled on the Mac and cached.
Runtime headers and Mach-O platform metadata are checked, rejecting
macOS/simulator extensions.
Build artifacts default to `build/ios` in the calling directory; set
`MELTY_IOS_BUILD_DIR` to share an explicit artifact location. Nothing writes into
the installed toolkit.

The Python running staging, project generation and packaging does not have to
match the device Python. The project interpreter defaults to `.venv/bin/python`;
`stage` and `generate` accept `--project-python` for a different project venv.
That interpreter selects dependency markers, the runtime version and bytecode
format. It is also used directly for build-time compilation, without needing
MeltyGUI installed in it. Its major/minor version selects the iOS runtime;
the desktop executable itself is never bundled. This includes the BeeWare
backports for Python 3.10, 3.11 and 3.12. The application's and dependencies'
declared Python compatibility still applies.

Prepared inputs live under `$XDG_CACHE_HOME/meltygui/ios` (default
`~/.cache/meltygui/ios`), separated by Python version, SDK and build recipe
metadata. Builds use owned Python and Rust environments, leaving the project
venv intact. Later runs reuse valid inputs without downloading or compiling
them. Concurrent preparations share a cache lock. Failed builds include the
compiler's diagnostic and a persistent log path, and can be retried.

To prepare an existing generated project ahead of Run:

```sh
python -m meltygui.platforms.ios prepare --application /path/to/app
```

The first preparation needs network access and may take time to build native
dependencies. A package whose source cannot cross-compile still needs a
compatible recipe or upstream fix; missing prebuilt wheels alone no longer
stop device Run. The commands below remain available for manual recipe builds;
the older numeric/Rust catalogues target 3.13, while automatic preparation
selects or compiles wheels for the project interpreter.

Pillow source builds first compile and cache a static ARM64 iPhone
libjpeg-turbo library, and use zlib from the iPhoneOS SDK. JPEG and PNG support
remain enabled. Target paths are supplied explicitly so a Mac/Homebrew codec
cannot be accidentally linked into the device wheel.
CFFI source builds use the matching ARM64 iPhone libffi development archive
from BeeWare, with explicit target include/library paths and static linking.

Run the native recipes using their documented build environment with MeltyGUI installed plus
build, setuptools, setuptools-scm, wheel, hatchling, packaging, Cython 3.2.4 and
CMake. Rust recipes require an isolated Cargo/Rustup installation with the
`aarch64-apple-ios` target. `--help` lists each recipe's runtime/toolchain paths.

```sh
python -m meltygui.platforms.ios build-imgui --python-framework /device/Python.framework
python -m meltygui.platforms.ios numeric-wheels --python-framework /device/Python.framework
python -m meltygui.platforms.ios build-platform --help
python -m meltygui.platforms.ios build-rust --help
python -m meltygui.platforms.ios build-crypto --help
```

Recipes cover ImGui, NumPy, Pillow, CFFI, LibCST, Pydantic Core, jiter, rpds-py
and cryptography. FreeType and libspatialindex are separate embedded frameworks.
Cryptography statically includes OpenSSL; NumPy uses Accelerate. Watchdog uses
its polling observer and PyYAML-ft its Python implementation.

`dependencies.json` is a catalogue of hash-pinned portable package recipes.
Staging selects only the application's dependency closure, including extras and
iOS/project-Python markers. Wheels for other Python versions are excluded.
Unused recipes are not downloaded or shipped. Supply
additional native wheels with `--wheel-dir`, a different locked catalogue with
`--lock`, or source packages to build as ordinary wheels with `--package-source`.
Conflicting versions and missing device dependencies fail explicitly.

## Stage and build an app

Run in the application directory. For a development checkout, explicitly supply
the package sources used by that app; a toolkit-only app needs no Pro source:

```sh
python -m meltygui.platforms.ios stage --application . \
  --package-source /checkout/meltygui
```

For the editor, add `--package-source /checkout/meltygui-pro`. For installed
releases, supply their ordinary wheels with `--wheel-dir`. Staging produces
`build/ios/app-bundle/{app,packages,manifest.json}` and includes no editable
install, `.pth` file, desktop virtualenv or implicit sibling checkout. The manifest
records explicit `--package-source` paths; device Run rebuilds those wheels before
Xcode builds, so toolkit edits reach the phone too. Restage older bundles once
with `--package-source` to enable this. Wheel-only dependencies stay pinned.

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
python -m meltygui.platforms.ios generate --application . \
  --python-framework /device/Python.framework --python-lib /device/lib \
  --embed-framework build/ios/dependencies/platform/frameworks/freetype.framework \
  --embed-framework build/ios/dependencies/platform/frameworks/spatialindex_c.framework \
  --team YOURTEAMID

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
xcodebuild -project build/ios/MeltyIOS.xcodeproj -scheme Melty \
  -configuration Debug -destination 'generic/platform=iOS' build
```

Xcode compiles the toolkit's shader ports, copies the generic bootstrap and app
resources, and packages signed device extensions. Build metadata chooses the
entry module and bundle identity. The host installs the native runtime before
importing that module; its normal decorators and `run()` then use the shared
MeltyGUI lifecycle.

The packaging phase precompiles the standard library, bootstrap and dependencies
using the project venv's compiler, retaining their sources for inspection.
These signed, read-only resources use unchecked hash-based caches, so installation
timestamp changes do not invalidate them and imports do not reread source files.
The writable application's sources are deliberately not precompiled; on-device
edits always load from source. Invalid Python test fixtures/templates remain
source-only. All Python selection, compatibility checks and bytecode compilation
happen on the build machine; there is no startup version reconciliation or
bytecode repair. The native host derives its library paths from the framework's
headers. A change to the project venv's Python version automatically prepares
matching iOS artifacts and regenerates the project, preserving signing and
native source/framework settings. Existing generated projects locate the
venv above their staged app directory, so a different build-tool Python alone
does not require regeneration.

`ios-host.log` includes `[startup]` timings for native Metal setup, CPython
initialization, bootstrap import, editable source preparation, Melty runtime
imports and application launch. The Python + application ready total includes
interpreter initialization through app setup, but excludes native Metal setup
and the first frame. The existing app `startup.log` records font/session setup
and first-frame presentation. Compare launches of the same app/session on the
same device, separating the first launch after installation from later launches.

## Run from the editor

On macOS the Tasks environment picker includes paired iPhones/iPads by their
device names. Device commands honor `DEVELOPER_DIR` and the selected full Xcode.
If only Command Line Tools are selected, they find Xcode (including Xcode beta)
in `/Applications` without changing the system developer-directory selection. Select one and Run the task for the app's declared `entry` module.
MeltyGUI stages the app's current sources (including the entry's pending editor
text), builds with Xcode, installs the signed app and streams its device console.
Stop terminates the installed app; it does not stop unrelated device processes.

Stage dependencies and generate the signed Xcode project once using the steps
above. The runner reads `build/ios/host-build.json` and `MeltyIOS.xcodeproj`.
For a different output directory, set `MELTY_IOS_BUILD_DIR` or declare
`build_directory` under `[tool.melty.app.ios]`. Re-stage dependencies when they
change. Xcode signing, device pairing and Developer Mode must already be ready;
Run reports setup/build failures in the task console.

Device targets launch the configured app entry. They do not provide a remote
shell, arbitrary task working directories or remote debugger transport. Local
venv targets continue to support Run and Debug. Device discovery/deployment uses
[Xcode's command-line tools](https://developer.apple.com/documentation/xcode/xcode-command-line-tool-reference).

## Runtime ownership

`core/windowing/surface_frame.py` owns the common root layout, settings controls,
render-host pump and frame drawing. Desktop surfaces and the UIKit owner supply
input, geometry, graphics targets and presentation. Fonts, styles and session
initialization are shared in `core/runtime/app.py`. Platform resources are
released by their owning host.

Documents/Projects is the editable workspace; Library/Application Support holds
settings/sessions and Library/Caches holds disposable data. The host imports the
app's own sources and resources from a writable copy in
`Library/Application Support/meltygui/app-source/<build generation>`. Source
navigation and edits such as the header tint's `@glfw_window` use those files.
The writable source path stays stable across builds (including adoption of an
existing generation directory). Updates replace changed managed files, retain
local-only files such as saves, and keep device edits to unchanged host files.
Conflicting device edits are backed up under `app-source/backups/<generation>`;
only unchanged, formerly managed files are removed when deleted on the Mac.
Dependencies and the signed bundle remain read-only. Clipboard access,
keyboard visibility, safe-zone updates and frame requests are native services.
Input and frame dimensions use UIKit points; `scale` converts to Metal pixels.
Keyboard viewport animation uses the presentation-layer bounds at native scale.
`Toggles.Mobile.Safezone` controls the top inset (64 points by default);
`Toggles.Mobile.bottom_safezone` controls the bottom home-indicator strip (28 points).
Both persist with the normal settings and can be set to zero for edge-to-edge content. Idle rendering pauses; active
rendering requests the available cadence up to 120 Hz.

Project execution/debugging belongs to MeltyGUI Pro's `ProjectExecution` model.
On iOS it runs Python modules in the embedded interpreter and rejects shell or
external-process requests. The editor's Tasks tile only owns its definitions,
selection and presentation. User code shares process state; cancellation is
cooperative, and native blocking calls must return.

## Incremental development runs

The first Run installs the host with the source-update protocol. Later runs
compare native/runtime inputs using file names, mtimes and sizes. With the same
installed app and unchanged native inputs, Run skips dependency builds, Xcode
and installation: changed app sources/resources are uploaded to a private
staging directory in the app data container, then committed with a descriptor
and applied on relaunch. An unchanged run launches without uploading. App-code
updates transfer the app source payload, not the runtime or dependency wheels.

Uploads use separate generations; a failed transfer leaves the last committed
payload intact. No transfer clears the app data container. Changes to runtime,
local dependencies, host code, build configuration or native app extensions
fall back to build/install. Per-device receipts live in the local build folder;
removing them safely forces a full build. The installed app identity is checked
before reusing a receipt. Source-update and save-preservation behavior is covered
by host/worker tests; physical-device transport still needs verification.

## Verification and remaining device work

Run toolkit tests with `python -m pytest tests/ios tests/test_portable_native_app.py`.
The portable-app test exercises the real Python frame, settings, cache and save
lifecycle while recording GPU calls; it does not validate Metal pixels.
On macOS, `tests/ios/build_metal_test.py` builds the real offscreen Metal encoder;
set `MELTY_METAL_TEST=1` to opt into pixel tests. On a Mac with a display server,
`tests/ios/test_presentation.py` also checks 90 real display-link drawables using
the production submission helper and Metal validation. Offscreen pixel tests
do not exercise the compositor or drawable presentation fences. Editor integration/device checks
remain in the editor repository's `tests/ios` directory.

Physical-device verification is still required for input latency, 120 Hz pacing,
keyboard transitions, task cancellation, suspension and relaunch. Queued editor
input still needs durable handling before suspension checkpoint guarantees hold.
The host currently supports basic UIKeyInput text/backspace, not full UITextInput
composition/selection, hardware-key handling or accessibility. Files coordination,
external-write conflicts and container-relative persisted references remain work.
Tensor rendering and arbitrary live GLSL authoring have no Metal implementation;
unsupported operations report an error rather than copying tensors or emulating
another graphics backend. Live GLSL text-policy edits still require rebuilding
the Metal library.
