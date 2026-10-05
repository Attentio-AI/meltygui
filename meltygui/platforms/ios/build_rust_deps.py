#!/usr/bin/env python3
"""Build pinned Rust Python extensions for CPython 3.13 on ARM64 iPhoneOS.

Use a host Python 3.13 environment containing the exact BUILD_TOOLS below,
Rust 1.99.0 with the aarch64-apple-ios standard library, and the staged device
Python.framework. No simulator, host extension, or native JIT is involved.

    build/ios/dependencies/rust/tools/bin/python \
        -m meltygui.platforms.ios.build_rust_deps --jobs 1

Sources are SHA256 checked; Cargo uses the upstream lockfiles. Builds run
sequentially at reduced priority, with at most two Cargo jobs. The wheelhouse,
unpacked packages, logs and manifest live in build/dependencies/rust. The
manifest validates device Mach-O load commands, Python API symbols, and wheel
RECORD hashes. Import/functionality validation still requires an iPhone.

To prepare an isolated build environment, install BUILD_TOOLS with uv pip
into a Python 3.13 venv. Use --only to build a subset and --skip-build to
revalidate existing wheels. --offline disallows source/Cargo downloads.
"""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from meltygui.platforms.ios.download_numeric_wheels import digest, symbols, unpack, validate_native


from meltygui.platforms.ios import build_directory

BUILD = build_directory()
ROOT = Path(__file__).resolve().parent
TARGET = "aarch64-apple-ios"
DEPLOYMENT = "17.0"
EXT_SUFFIX = ".cpython-313-iphoneos.so"
TAG = "cp313-cp313-ios_17_0_arm64_iphoneos"
BUILD_TOOLS = {
    "maturin": "1.15.0", "setuptools": "84.0.0", "setuptools-rust": "1.13.0",
    "setuptools-scm": "10.3.4", "wheel": "0.48.0", "packaging": "26.3",
    "semantic-version": "2.10.0", "vcs-versioning": "2.5.0",
}
PINS = (
    dict(name="libcst", version="1.9.0", directory="libcst-1.9.0",
         url="https://files.pythonhosted.org/packages/02/c0/098e5c91ff1537f00c85a6438b6cb1863d17144680cc91f47c87f104a200/libcst-1.9.0.tar.gz",
         sha256="087b58a9afe076bb08e2d726478e1f16cb928d67ffa9092817e033c335de522a",
         manifest="native/libcst/Cargo.toml", lock="native/Cargo.lock"),
    dict(name="pydantic_core", version="2.46.5", directory="pydantic_core-2.46.5",
         url="https://files.pythonhosted.org/packages/af/f9/8a06bea35ef8daf588f707784c973a7046e0034c8d8cfb08828eeffb8b75/pydantic_core-2.46.5.tar.gz",
         sha256="10416c15b8839ecc4ef4d0885da76da6fd0f67333a0eb8aff6d93c4b8f2910fc",
         manifest="Cargo.toml", lock="Cargo.lock"),
    dict(name="jiter", version="0.17.0", directory="jiter-0.17.0",
         url="https://files.pythonhosted.org/packages/9c/1f/8176d92e001f86505424b41664032ae26a882bc9ca41a32c803f373f9195/jiter-0.17.0.tar.gz",
         sha256="03e432f226a453851079fb84cd17c6da9991eab723e28d716f14ae3d906e0c12",
         manifest="crates/jiter-python/Cargo.toml", lock="Cargo.lock"),
    dict(name="rpds-py", version="2026.6.3", directory="rpds_py-2026.6.3",
         url="https://files.pythonhosted.org/packages/aa/2a/9618a122aeb2a169a28b03889a2995fe297588964333d4a7d67bdf46e147/rpds_py-2026.6.3.tar.gz",
         sha256="1cebd1337c242e4ec2293e541f712b2da849b29f48f0c293684b71c0632625d4",
         manifest="Cargo.toml", lock="Cargo.lock"),
)


def run(command, env, log=None):
    command = [str(arg) for arg in command]
    if log is None:
        return subprocess.check_output(command, env=env, text=True, close_fds=False).strip()
    with log.open("w") as stream:
        stream.write(shlex.join(command) + "\n")
        stream.flush()
        result = subprocess.run(command, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, close_fds=False)
    if result.returncode:
        raise RuntimeError(f"Build failed ({result.returncode}); see {log}\n{log.read_text()[-5000:]}")


def source_tree(pin, output, offline):
    downloads, sources = output / "downloads", output / "sources"
    downloads.mkdir(exist_ok=True)
    sources.mkdir(exist_ok=True)
    archive = downloads / pin["url"].rsplit("/", 1)[-1]
    if not archive.exists():
        if offline:
            raise FileNotFoundError(f"Missing cached source: {archive}")
        temporary = archive.with_suffix(".download")
        try:
            with urllib.request.urlopen(pin["url"], timeout=60) as src, temporary.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if digest(temporary) != pin["sha256"]:
                raise ValueError(f"Source SHA256 mismatch: {archive}")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(archive) != pin["sha256"]:
        raise ValueError(f"Cached source SHA256 mismatch: {archive}")
    target = sources / pin["directory"]
    marker = target / ".melty-source-sha256"
    if not marker.exists() or marker.read_text().strip() != pin["sha256"]:
        with tempfile.TemporaryDirectory(dir=sources) as temporary:
            with tarfile.open(archive) as src:
                src.extractall(temporary, filter="data")
            extracted = Path(temporary) / pin["directory"]
            if not extracted.is_dir():
                raise ValueError(f"Missing source directory in {archive}")
            if target.exists():
                shutil.rmtree(target)
            shutil.move(extracted, target)
        marker.write_text(pin["sha256"] + "\n")
    return target


def build_wheel(pin, source, output, env, jobs):
    wheelhouse = output / "wheelhouse"
    if pin["name"] == "libcst":
        # Upstream setuptools-rust handles Python package data and metadata.
        # Supply --locked without changing the pinned upstream setup.py.
        runner = (
            "import os, runpy, sys; os.chdir(sys.argv[1]); sys.argv=sys.argv[2:]; "
            "import setuptools_rust; original=setuptools_rust.RustExtension; "
            "setuptools_rust.RustExtension=lambda *a, **kw: "
            "original(*a, **dict(kw, args=['--locked', *kw.get('args', ())])); "
            "runpy.run_path('setup.py', run_name='__main__')"
        )
        command = [sys.executable, "-c", runner, source, "setup.py", "bdist_wheel",
                   "--plat-name", "ios_17_0_arm64_iphoneos", "--dist-dir", wheelhouse]
    else:
        maturin = Path(sys.executable).parent / "maturin"
        command = [maturin, "build", "--release", "--locked", "--target", TARGET,
                   "--jobs", str(jobs), "--manifest-path", source / pin["manifest"],
                   "--out", wheelhouse, "--interpreter", "python3.13"]
    print(f"Building {pin['name']} {pin['version']} ({jobs} Cargo job(s))", flush=True)
    run(command, env, output / f"{pin['name']}-build.log")


def validate_wheel(pin, output, python_exports):
    wheel = output / "wheelhouse" / f"{pin['name'].replace('-', '_')}-{pin['version']}-{TAG}.whl"
    if not wheel.is_file():
        raise FileNotFoundError(f"Expected CPython 3.13 iPhoneOS wheel: {wheel}")
    destination = output / "unpacked" / pin["name"].replace("-", "_")
    info = unpack(wheel, destination, pin)
    with zipfile.ZipFile(wheel) as archive:
        wheel_info, = (name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))
        if f"Tag: {TAG}" not in archive.read(wheel_info).decode():
            raise ValueError(f"Incorrect wheel tag: {wheel}")
    native = []
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.suffix in (".so", ".dylib", ".pyd", ".dll"):
            if not path.name.endswith(EXT_SUFFIX):
                raise ValueError(f"Incorrect device extension suffix: {path}")
            native.append(dict(path=str(path.relative_to(destination)), **validate_native(path, python_exports)))
    if len(native) != 1:
        raise ValueError(f"Expected one Rust Python extension in {wheel}, found {len(native)}")
    return dict(name=pin["name"], version=pin["version"], wheel=str(wheel),
                sha256=digest(wheel), unpacked=str(destination), native=native, **info)


def build(args):
    if sys.version_info[:2] != (3, 13):
        raise ValueError("Run this recipe using the host Python 3.13 build environment")
    if not 1 <= args.jobs <= 2:
        raise ValueError("--jobs must be 1 or 2")
    tools = {name: version(name) for name in BUILD_TOOLS}
    if tools != BUILD_TOOLS:
        raise ValueError(f"Build environment differs from pinned tool versions: {tools}; expected {BUILD_TOOLS}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("wheelhouse", "unpacked"):
        (output / name).mkdir(exist_ok=True)
    framework = args.python_framework.resolve()
    python_exports = symbols(framework / "Python", "-gUj")
    config = output / "pyo3-device.cfg"
    config.write_text("implementation=CPython\nversion=3.13\nshared=true\nabi3=false\n"
                      "pointer_width=64\nbuild_flags=\nsuppress_build_script_link_lines=true\n"
                      f"ext_suffix={EXT_SUFFIX}\n")
    env = dict(os.environ)
    for name in ("PYTHONPATH", "_PYTHON_SYSCONFIGDATA_NAME", "_PYTHON_SYSCONFIGDATA_PATH",
                 "CARGO_ENCODED_RUSTFLAGS", "RUSTFLAGS", "MACOSX_DEPLOYMENT_TARGET"):
        env.pop(name, None)
    cargo_home, rustup_home = args.cargo_home.resolve(), args.rustup_home.resolve()
    env.update(DEVELOPER_DIR=str(args.developer_dir.resolve()), CARGO_HOME=str(cargo_home),
               RUSTUP_HOME=str(rustup_home), RUSTUP_TOOLCHAIN=args.rust_toolchain,
               PATH=f"{cargo_home / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin",
               CARGO_TARGET_DIR=str(output / "target"), CARGO_BUILD_TARGET=TARGET,
               CARGO_BUILD_JOBS=str(args.jobs), PYO3_CONFIG_FILE=str(config), PYO3_CROSS="1",
               PYO3_CROSS_PYTHON_VERSION="3.13", IPHONEOS_DEPLOYMENT_TARGET=DEPLOYMENT,
               SETUPTOOLS_EXT_SUFFIX=EXT_SUFFIX, _PYTHON_HOST_PLATFORM="ios-17.0-arm64-iphoneos",
               LIBCST_NO_LOCAL_SCHEME="1", SOURCE_DATE_EPOCH="0")
    if args.offline:
        env["CARGO_NET_OFFLINE"] = "true"
    sdk = run(["/usr/bin/xcrun", "--sdk", "iphoneos", "--show-sdk-path"], env)
    env["SDKROOT"] = sdk
    # Explicit linkage resolves Python references against the device framework;
    # suppressing PyO3's library-name inference avoids macOS libpython leakage.
    env["RUSTFLAGS"] = shlex.join([
        "-C", f"link-arg=-F{framework.parent}", "-C", "link-arg=-framework",
        "-C", "link-arg=Python", "-C", "link-arg=-Wl,-headerpad_max_install_names",
    ])
    rustc = run([cargo_home / "bin/rustc", "--version"], env)
    if not rustc.startswith("rustc 1.99.0 "):
        raise ValueError(f"Expected pinned Rust 1.99.0, found {rustc}")
    os.nice(10)
    manifest_path = output / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    packages = {package["name"]: package for package in previous.get("packages", [])}
    for pin in PINS:
        if args.only and pin["name"] not in args.only:
            continue
        source = source_tree(pin, output, args.offline)
        lock_digest = digest(source / pin["lock"])
        if not args.skip_build:
            build_wheel(pin, source, output, env, args.jobs)
        if digest(source / pin["lock"]) != lock_digest:
            raise ValueError(f"Build changed upstream Cargo.lock: {source}")
        packages[pin["name"]] = dict(validate_wheel(pin, output, python_exports),
                                     source_url=pin["url"], source_sha256=pin["sha256"],
                                     cargo_lock_sha256=lock_digest)
        manifest = dict(format_version=1, target=TARGET, deployment_target=DEPLOYMENT,
                        python="3.13", python_framework_sha256=digest(framework / "Python"),
                        rustc=rustc, build_tools=tools, sdk=sdk,
                        packages=list(packages.values()),
                        limitations=["Device imports and functionality require on-device validation",
                                     "Framework conversion and code signing belong to app assembly"])
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Validated {pin['name']}: {packages[pin['name']]['wheel']}", flush=True)
    print(f"Manifest: {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=BUILD / "dependencies/rust")
    parser.add_argument("--python-framework", type=Path, default=BUILD / "runtime/device/Python.framework")
    parser.add_argument("--developer-dir", type=Path, default=Path("/Applications/Xcode-beta.app/Contents/Developer"))
    parser.add_argument("--cargo-home", type=Path, default=BUILD / "dependencies/cargo")
    parser.add_argument("--rustup-home", type=Path, default=BUILD / "dependencies/rustup")
    parser.add_argument("--rust-toolchain", default="stable")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--only", nargs="+", choices=[pin["name"] for pin in PINS])
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cannot build iOS Rust dependencies: {error}\n")


if __name__ == "__main__":
    main()
