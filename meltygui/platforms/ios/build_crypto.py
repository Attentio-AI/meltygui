#!/usr/bin/env python3
"""Build cryptography 50.0.2 with static OpenSSL for the supplied iPhone CPython.

Requires Xcode and a Rust toolchain with the
aarch64-apple-ios target. The default Rust homes are the isolated installations
under build/dependencies/{cargo,rustup}. No global Python or Rust environment is
modified. Source archives are pinned by SHA256 and Cargo uses the upstream lock.

The resulting wheel and unpacked package live in build/dependencies/crypto.
OpenSSL is linked statically into the extension; only Python.framework and iOS
system libraries need embedding/linking. Framework conversion and code signing
remain the application's responsibility. This script validates device Mach-O
load commands, Python symbols, and wheel RECORD, but cannot execute iOS code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from meltygui.platforms.ios.download_numeric_wheels import symbols, unpack, validate_native


from meltygui.platforms.ios import build_directory
from meltygui.platforms.ios.runtime import framework_version

BUILD = build_directory()
ROOT = Path(__file__).resolve().parent
SOURCES = {
    "cryptography": {
        "version": "50.0.2",
        "url": "https://files.pythonhosted.org/packages/9d/af/182eb91b0df3fe75c4d9f26fe70684569566745f6ba7e5c9c73a862c5252/cryptography-50.0.2.tar.gz",
        "sha256": "7b46165bb56eb4704e2eaaf86f3c940d19154535d9b0ca7d6d590b04060e00d5",
    },
    "openssl": {
        "version": "4.0.3",
        "url": "https://github.com/openssl/openssl/releases/download/openssl-4.0.3/openssl-4.0.3.tar.gz",
        "sha256": "325b5c806167c13b40b1ffeadfe0248197c00eccc4cf123ec1e28d2d2fd216d9",
    },
}
BUILD_REQUIREMENTS = ("maturin==1.15.0", "cffi==2.1.1", "pycparser==3.0", "setuptools==84.0.0")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, *, env, log=None, directory=None):
    command = list(map(str, command))
    executable = shutil.which(command[0], path=env.get("PATH"))
    if executable is None:
        raise ValueError(f"Build tool is unavailable: {command[0]}")
    command[0] = str(Path(executable).absolute())
    if directory is not None:
        # Keep subprocess creation on posix_spawn. Only the new child changes
        # directory before replacing itself with the actual build executable.
        runner = "import os,sys; os.chdir(sys.argv[1]); os.execve(sys.argv[2],sys.argv[2:],os.environ)"
        command = [sys.executable, "-c", runner, str(directory), *command]
    if log is None:
        return subprocess.check_output(command, env=env, close_fds=False, text=True).strip()
    with Path(log).open("w") as stream:
        stream.write("Arguments: " + json.dumps(command) + "\n")
        stream.flush()
        result = subprocess.run(command, env=env, close_fds=False,
                                stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        tail = "\n".join(Path(log).read_text().splitlines()[-40:])
        raise RuntimeError(f"Build failed; see {log}\n{tail}")


def source_tree(name, output, offline):
    pin = SOURCES[name]
    archive = output / "downloads" / pin["url"].rsplit("/", 1)[-1]
    if not archive.exists():
        if offline:
            raise FileNotFoundError(f"Offline source is missing: {archive}")
        temporary = archive.with_suffix(".download")
        try:
            with urllib.request.urlopen(pin["url"], timeout=60) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != pin["sha256"]:
                raise ValueError(f"Downloaded SHA256 mismatch: {archive.name}")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(archive) != pin["sha256"]:
        raise ValueError(f"Cached SHA256 mismatch: {archive}")
    source = output / "sources" / f"{name}-{pin['version']}"
    if not source.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(output / "sources", filter="data")
    if not source.is_dir():
        raise ValueError(f"Source archive did not contain {source.name}")
    return source, dict(name=name, **pin, archive=str(archive))


def host_tools(output, env, offline):
    python = output / "tools/bin/python"
    if not python.exists():
        run([sys.executable, "-m", "venv", output / "tools"], env=env,
            log=output / "host-tools-create.log")
    check = """import importlib.metadata,json
versions = {}
for package in ('maturin', 'cffi', 'pycparser', 'setuptools'):
    try:
        versions[package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
print(json.dumps(versions))
"""
    versions = json.loads(run([python, "-c", check], env=env))
    if any(versions.get(pin.split("==")[0]) != pin.split("==")[1] for pin in BUILD_REQUIREMENTS):
        args = [python, "-m", "pip", "install", "--only-binary=:all:"]
        if offline:
            args.append("--no-index")
        run([*args, *BUILD_REQUIREMENTS], env=env, log=output / "host-tools-install.log")
    return python


def build(args):
    if not 1 <= args.jobs <= 2:
        raise ValueError("--jobs must be 1 or 2")
    output = args.output.resolve()
    for name in ("downloads", "sources", "wheelhouse", "unpacked"):
        (output / name).mkdir(parents=True, exist_ok=True)
    device = args.runtime.resolve()
    python_framework = device / "Python.framework"
    python_version = framework_version(python_framework)
    python_lib = args.python_lib or device / 'lib'
    if not (python_framework / "Headers/Python.h").is_file():
        raise FileNotFoundError(f"Missing device Python headers: {python_framework}")
    env = dict(os.environ, DEVELOPER_DIR=str(args.developer_dir.resolve()),
               CARGO_HOME=str(args.cargo_home.resolve()), RUSTUP_HOME=str(args.rustup_home.resolve()))
    env["PATH"] = str(args.cargo_home.resolve() / "bin") + os.pathsep + env["PATH"]
    sdk = run(["/usr/bin/xcrun", "--sdk", "iphoneos", "--show-sdk-path"], env=env)
    clang = run(["/usr/bin/xcrun", "--sdk", "iphoneos", "--find", "clang"], env=env)
    env["SDKROOT"] = sdk
    env["IPHONEOS_DEPLOYMENT_TARGET"] = args.deployment_target
    host_python = host_tools(output, env, args.offline)
    sources, provenance = {}, []
    for name in SOURCES:
        sources[name], record = source_tree(name, output, args.offline)
        provenance.append(record)

    print("Building static OpenSSL for arm64 iPhoneOS", flush=True)
    openssl = output / "openssl"
    openssl_env = dict(env, CC=clang,
                       CFLAGS=f"-O2 -miphoneos-version-min={args.deployment_target} -fPIC -fvisibility=hidden")
    configure = ["/usr/bin/perl", sources["openssl"] / "Configure", "ios64-xcrun",
                 "no-shared", "no-tests", "no-apps", "no-module", "no-dso", "no-autoload-config",
                 "--prefix=" + str(openssl), "--openssldir=/private/var/empty"]
    openssl_inputs = dict(source=SOURCES["openssl"]["sha256"], sdk=sdk,
                          compiler=run([clang, "--version"], env=env),
                          cflags=openssl_env["CFLAGS"], configure=list(map(str, configure)))
    stamp = output / "openssl-build.json"
    cached = json.loads(stamp.read_text()) if stamp.exists() else {}
    libraries = [openssl / "lib" / name for name in ("libcrypto.a", "libssl.a")]
    if (cached.get("inputs") != openssl_inputs or not all(path.is_file() for path in libraries)
            or cached.get("archives") != {path.name: digest(path) for path in libraries}):
        run(configure, env=openssl_env, directory=sources["openssl"], log=output / "openssl-configure.log")
        run(["/usr/bin/make", f"-j{args.jobs}", "build_libs"], env=openssl_env,
            directory=sources["openssl"], log=output / "openssl-build.log")
        run(["/usr/bin/make", "install_dev"], env=openssl_env,
            directory=sources["openssl"], log=output / "openssl-install.log")
        stamp.write_text(json.dumps(dict(inputs=openssl_inputs,
                                        archives={path.name: digest(path) for path in libraries}), indent=2) + "\n")

    # Upstream adds the macOS clang runtime for every Apple target when OpenSSL
    # is static. iOS must use its own runtime, which clang selects automatically.
    build_rs = sources["cryptography"] / "src/rust/cryptography-cffi/build.rs"
    original = 'if target.contains("apple") && openssl_static {'
    replacement = 'if target.ends_with("apple-darwin") && openssl_static {'
    text = build_rs.read_text()
    if original not in text and replacement not in text:
        raise ValueError("Pinned cryptography clang-runtime build rule has changed")
    patched = text.replace(original, replacement)
    if patched != text:
        build_rs.write_text(patched)
    shutil.copy2(sources["openssl"] / "LICENSE.txt",
                 sources["cryptography"] / "src/cryptography/OPENSSL-LICENSE.txt")

    # cryptography-cffi derives target include paths from this Unix-shaped
    # prefix. Both point at the actual iOS runtime, never host Python headers.
    cross_prefix = output / "python"
    for relative, target in ((f"lib/python{python_version}", python_lib / f"python{python_version}"),
                             (f"include/python{python_version}", python_framework / "Headers")):
        link = cross_prefix / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and link.resolve() != target.resolve():
            link.unlink()
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
    config = output / "pyo3-config.txt"
    configuration = (f"implementation=CPython\nversion={python_version}\nshared=true\nabi3=true\n"
                     "pointer_width=64\nsuppress_build_script_link_lines=true\next_suffix=.abi3.so\n")
    if not config.exists() or config.read_text() != configuration:
        config.write_text(configuration)
    env.update(PYO3_CONFIG_FILE=str(config), PYO3_CROSS="1", PYO3_CROSS_PYTHON_VERSION=python_version,
               PYO3_CROSS_LIB_DIR=str(cross_prefix / f"lib/python{python_version}"), PYO3_PYTHON=str(host_python),
               OPENSSL_DIR=str(openssl), OPENSSL_STATIC="1", OPENSSL_NO_VENDOR="1",
               CARGO_TARGET_DIR=str(output / "target"), CARGO_BUILD_JOBS=str(args.jobs),
               CC_aarch64_apple_ios=clang, CARGO_TARGET_AARCH64_APPLE_IOS_LINKER=clang)
    env["CFLAGS_aarch64_apple_ios"] = shlex.join([
        "-target", f"arm64-apple-ios{args.deployment_target}", "-isysroot", sdk])
    env["CARGO_TARGET_AARCH64_APPLE_IOS_RUSTFLAGS"] = shlex.join([
        "-C", "link-arg=-F" + str(device), "-C", "link-arg=-framework", "-C", "link-arg=Python",
        "-C", "link-arg=-Wl,-headerpad_max_install_names"])
    print("Building cryptography against device Python.framework", flush=True)
    command = [output / "tools/bin/maturin", "build", "--release", "--locked",
               "--target", "aarch64-apple-ios", "--interpreter", str(args.project_python or f"python{python_version}"),
               "--out", output / "wheelhouse", "--jobs", str(args.jobs)]
    if args.offline:
        command.append("--offline")
    run(command, env=env, directory=sources["cryptography"], log=output / "cryptography-build.log")
    wheels = list((output / "wheelhouse").glob("cryptography-50.0.2-*-ios_*_arm64_iphoneos.whl"))
    if len(wheels) != 1:
        raise ValueError(f"Expected exactly one iPhone cryptography wheel, found {wheels}")
    wheel = wheels[0]
    package = output / "unpacked/cryptography"
    record = unpack(wheel, package, {"name": "cryptography", "version": "50.0.2"})
    extensions = list(package.rglob("*.so"))
    if len(extensions) != 1 or list(package.rglob("*.dylib")):
        raise ValueError(f"Expected one extension with static OpenSSL: {extensions}")
    exports = symbols(python_framework / "Python", "-gUj")
    native = [dict(path=path.relative_to(package).as_posix(), **validate_native(path, exports))
              for path in extensions]
    # Rust cdylibs export the Python module initializers. OpenSSL's symbols
    # must stay private so they cannot interpose on CPython's separate OpenSSL.
    if any(name.startswith(("_OPENSSL_", "_SSL_", "_EVP_", "_CRYPTO_"))
           for name in symbols(extensions[0], "-gUj")):
        raise ValueError("Static OpenSSL unexpectedly exports public symbols")
    manifest = dict(schema=1, target=f"aarch64-apple-ios{args.deployment_target}", python=python_version,
                    sdk=sdk, sources=provenance, build_requirements=list(BUILD_REQUIREMENTS),
                    rust=run(["rustc", "--version"], env=env),
                    source_patches=[dict(path="src/rust/cryptography-cffi/build.rs",
                                        reason="Only link clang_rt.osx for macOS, not iOS",
                                        before=original, after=replacement)],
                    packages=[dict(name="cryptography", version="50.0.2", wheel=str(wheel),
                                   sha256=digest(wheel), unpacked=str(package), native=native,
                                   static_openssl="4.0.3", **record)],
                    validation="Wheel RECORD, thin ARM64 iOS Mach-O, load commands, Python symbols, and hidden OpenSSL symbols verified; not imported on device.")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(output / "manifest.json", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=BUILD / "dependencies/crypto")
    parser.add_argument("--runtime", type=Path, default=BUILD / "runtime/device")
    parser.add_argument("--python-lib", type=Path)
    parser.add_argument("--project-python", type=Path)
    parser.add_argument("--developer-dir", type=Path, default=Path("/Applications/Xcode-beta.app/Contents/Developer"))
    parser.add_argument("--cargo-home", type=Path, default=BUILD / "dependencies/cargo")
    parser.add_argument("--rustup-home", type=Path, default=BUILD / "dependencies/rustup")
    parser.add_argument("--deployment-target", default="17.0")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cannot build iOS cryptography: {error}\n")


if __name__ == "__main__":
    main()
