#!/usr/bin/env python3
"""Xcode build phase: package the project's CPython device runtime and signed modules."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import py_compile
import re
import shutil
import struct
import subprocess
import sys
import uuid


def compile_bytecode(root):
    """Precompile immutable bundle files; editable app sources stay source-only."""
    compiled = skipped = 0
    for source in sorted(Path(root).rglob("*.py")):
        # Explicit output ignores a build machine's PYTHONPYCACHEPREFIX and -O.
        cache = source.parent / "__pycache__" / f"{source.stem}.{sys.implementation.cache_tag}.pyc"
        try:
            py_compile.compile(str(source), cfile=str(cache), doraise=True, optimize=0,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        except py_compile.PyCompileError:
            # Libraries can ship deliberately invalid test fixtures or templates.
            # Keep their source and let an actual import report any syntax error.
            skipped += 1
        else:
            compiled += 1
    if skipped:
        print(f"Bytecode: kept {skipped} uncompileable source files in {root}")
    return compiled


def validate_device_binary(path):
    """Reject ARM64 macOS and simulator binaries even though their CPU matches."""
    data = Path(path).read_bytes()
    if len(data) < 32:
        raise ValueError(f"Not a Mach-O binary: {path}")
    magic, cpu, _, _, count, commands_size, _, _ = struct.unpack_from("<8I", data)
    if magic != 0xFEEDFACF or cpu != 0x0100000C:
        raise ValueError(f"Requires a thin ARM64 iOS device binary: {path}")
    offset = 32
    end = offset + commands_size
    if end > len(data):
        raise ValueError(f"Truncated Mach-O commands: {path}")
    device = False
    for _ in range(count):
        if offset + 8 > end:
            raise ValueError(f"Truncated Mach-O command: {path}")
        command, size = struct.unpack_from("<2I", data, offset)
        if size < 8 or offset + size > end:
            raise ValueError(f"Invalid Mach-O command size: {path}")
        if command == 0x32:  # LC_BUILD_VERSION
            if size < 24:
                raise ValueError(f"Invalid platform command: {path}")
            platform = struct.unpack_from("<I", data, offset + 8)[0]
            if platform != 2:  # PLATFORM_IOS; simulator is 7, macOS is 1.
                raise ValueError(f"Expected iOS device platform, found {platform}: {path}")
            device = True
        elif command == 0x25:  # LC_VERSION_MIN_IPHONEOS, older device builds.
            device = True
        elif command in (0x24, 0x2F, 0x30):
            raise ValueError(f"Not an iOS device binary: {path}")
        offset += size
    if not device:
        raise ValueError(f"Missing iOS device platform metadata: {path}")


def copy_tree(source, destination, *, checkout_root=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError(f"Packaging source/destination overlap: {source} and {destination}")
    if destination.exists():
        shutil.rmtree(destination)
    ignore_common = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".git", ".hg", ".svn")

    def ignore(directory, names):
        excluded = ignore_common(directory, names)
        if checkout_root and Path(directory) == source:
            excluded.update({".venv", "venv", "build", "dist"}.intersection(names))
        return excluded

    shutil.copytree(source, destination, ignore=ignore)


def package_extensions(root, bundle, bundle_id, sign, python_version=None):
    names = set()
    frameworks = bundle / "Frameworks"
    frameworks.mkdir(exist_ok=True)
    for binary in sorted(root.rglob("*.so")):
        abi = re.search(r'\.cpython-(\d+)[a-z]*-', binary.name)
        if python_version and abi and abi[1] != python_version.replace('.', ''):
            raise ValueError(f'Native module {binary} does not match project CPython {python_version}; rebuild its iOS wheel')
        validate_device_binary(binary)
        relative = binary.relative_to(root)
        # An extension can use an ABI suffix; package directories remain intact.
        name = ".".join((*relative.parts[:-1], relative.name.split(".")[0]))
        if name in names or (frameworks / f"{name}.framework").exists():
            raise ValueError(f"Duplicate native module framework: {name}")
        names.add(name)
        framework = frameworks / f"{name}.framework"
        framework.mkdir()
        executable = framework / name
        shutil.move(str(binary), executable)
        marker = binary.with_suffix(".fwork")
        marker.write_text(executable.relative_to(bundle).as_posix() + "\n")
        (framework / f"{name}.origin").write_text(marker.relative_to(bundle).as_posix() + "\n")
        (framework / ".melty-extension").touch()
        (framework / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": name, "CFBundleIdentifier": f"{bundle_id}.{name}".replace("_", "-"),
            "CFBundlePackageType": "FMWK", "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
            "MinimumOSVersion": "17.0", "CFBundleSupportedPlatforms": ["iPhoneOS"],
        }))
        sign(framework)
    return names


def prepare(config, bundle, *, identity=None, signing_allowed=True):
    from meltygui.platforms.ios.runtime import build_python

    bundle = Path(bundle).resolve()
    if bundle.suffix != ".app" or not bundle.is_dir():
        raise ValueError("Packaging requires an existing Xcode .app build directory")
    if signing_allowed and not identity:
        raise ValueError("Device extension signing requires EXPANDED_CODE_SIGN_IDENTITY")
    compiler, runtime = build_python(config)
    version = runtime['version']
    # Delete only frameworks previously generated by this phase.
    for marker in (bundle / "Frameworks").glob("*.framework/.melty-extension"):
        shutil.rmtree(marker.parent)
    (bundle / "python").mkdir(exist_ok=True)
    copy_tree(Path(config["python_lib"]), bundle / "python/lib")
    copy_tree(Path(config["app_dir"]), bundle / "app", checkout_root=True)
    copy_tree(Path(config["bootstrap_dir"]), bundle / "host")
    packages = bundle / "app_packages"
    if config.get("packages_dir"):
        copy_tree(Path(config["packages_dir"]), packages)
    else:
        if packages.exists():
            shutil.rmtree(packages)
        packages.mkdir()
    if config.get("shaders_dir"):
        for name in ("Melty.metallib", "metal-programs.json"):
            source = Path(config["shaders_dir"]) / name
            if not source.is_file():
                raise ValueError(f"Compile the Metal shaders before packaging: missing {source}")
            shutil.copy2(source, bundle / name)

    def sign(framework):
        if signing_allowed:
            try:
                subprocess.run(["/usr/bin/codesign", "--force", "--sign", identity,
                                "--timestamp=none", str(framework)], check=True,
                               close_fds=False, stderr=subprocess.PIPE, text=True)
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or str(error)).strip()
                if 'errSecInternalComponent' in detail:
                    raise ValueError(
                        f'macOS could not access the signing key for {framework.name}. '
                        'Unlock the login keychain on the build Mac (Keychain Access, or '
                        '`security unlock-keychain ~/Library/Keychains/login.keychain-db` '
                        'in your own terminal), then retry Run. If it is already unlocked, '
                        f'check that codesign is allowed to use the signing key. Details: {detail}'
                    ) from None
                raise ValueError(f'Code signing failed for {framework.name}: {detail}') from None

    # Linked dependencies must be embedded frameworks. Do not silently ship a
    # desktop NumPy/Pillow wheel or leave an unsigned vendored dylib in resources.
    for root in (bundle / "python/lib", bundle / "app", packages):
        dylibs = list(root.rglob("*.dylib"))
        if dylibs:
            raise ValueError(f"Convert dependent dylibs to device frameworks before packaging: {dylibs[0]}")
    roots = (bundle / f"python/lib/python{version}/lib-dynload", bundle / "app", packages)
    modules = set()
    for root in roots:
        modules.update(package_extensions(root, bundle, config["bundle_id"], sign, version))
    remaining = list((bundle / "python").rglob("*.so"))
    if remaining:
        raise ValueError(f"Put additional native packages in --packages-dir: {remaining[0]}")
    # Signed resources are immutable. Hash-based caches survive installation's
    # timestamp changes; unchecked mode avoids rereading their source on import.
    # Never put these caches in app/: that tree is copied to writable storage.
    # The build tools may use a different Python. Invoke the project venv only
    # for compilation; this standalone worker needs no Melty installation there.
    subprocess.run([compiler, '-I', str(Path(__file__).resolve()), '--compile-bytecode',
                    str(bundle / f"python/lib/python{version}"), str(bundle / "host"), str(packages),
                    '--expected-magic', runtime['magic']], check=True, close_fds=False)
    # Preserve host metadata explicitly: installation may round file timestamps.
    # This keeps unchanged host files distinguishable from edits made on-device.
    files = {p.relative_to(bundle / "app").as_posix(): [p.stat().st_mtime_ns, p.stat().st_size]
             for p in sorted((bundle / "app").rglob("*")) if p.is_file()}
    (bundle / "HostSettings.plist").write_bytes(plistlib.dumps({
        "entry_module": config["entry_module"], "source_generation": uuid.uuid4().hex,
        "source_updates": 1, "source_files": files,
    }))
    return modules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--compile-bytecode", type=Path, nargs='+')
    parser.add_argument("--expected-magic")
    args = parser.parse_args()
    try:
        if args.compile_bytecode:
            if importlib.util.MAGIC_NUMBER.hex() != args.expected_magic:
                raise ValueError('Project Python bytecode format changed during packaging')
            compiled = sum(compile_bytecode(root) for root in args.compile_bytecode)
            print(f"Precompiled {compiled} bundled Python modules using CPython {sys.version.split()[0]}")
            return
        if not args.config:
            parser.error('--config is required')
        config = json.loads(args.config.read_text())
        target = os.environ.get("CODESIGNING_FOLDER_PATH") or os.environ.get("TARGET_BUILD_DIR", "") + "/" + os.environ.get("WRAPPER_NAME", "")
        modules = prepare(config, target, identity=os.environ.get("EXPANDED_CODE_SIGN_IDENTITY"),
                          signing_allowed=os.environ.get("CODE_SIGNING_ALLOWED", "YES") != "NO")
        print(f"Packaged project CPython and {len(modules)} ARM64 iOS extension frameworks")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cannot package iOS Python runtime: {error}\n")


if __name__ == "__main__":
    main()
