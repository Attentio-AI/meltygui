#!/usr/bin/env python3
"""Download pinned NumPy, Pillow and cffi wheels for CPython 3.13 on iPhone.

Run with a host Python 3.12+ after staging the device Python.framework:

    python -m meltygui.platforms.ios.download_numeric_wheels

Defaults to build/dependencies/numeric/{wheelhouse,unpacked,manifest.json}.
Use --output and --python-framework to select other locations. --offline
revalidates previously downloaded wheels without network access. The wheels
come from the official BeeWare index (NumPy) and upstream PyPI (Pillow/cffi).
NumPy targets iOS 17 and uses Apple's Accelerate framework; the other wheels
target iOS 13. No simulator, Intel build, or native compilation is performed.

The manifest records package requirements, runtime library links, and binary
hashes. cffi also needs the pure-Python pycparser package; the full dependency
assembler resolves that requirement. Framework conversion/signing belongs to
the app packaging stage. Static validation here does not run device imports.
"""
from __future__ import annotations

import argparse
import base64
import csv
import email
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import subprocess
import tempfile
import urllib.request
import zipfile

from meltygui.platforms.ios import build_directory

BUILD = build_directory()
ROOT = Path(__file__).resolve().parent
PINS = (
    {
        "name": "numpy", "version": "2.5.2.post1",
        "url": "https://pypi.anaconda.org/beeware/simple/numpy/2.5.2.post1/numpy-2.5.2.post1-cp313-cp313-ios_17_0_arm64_iphoneos.whl",
        "sha256": "9fd06cac5ce4ba16decd52c189b5046cb727a36a9a1e50dbd3cc38c512cac1a3",
        "metadata_url": "https://api.anaconda.org/release/beeware/numpy/2.5.2.post1",
    },
    {
        "name": "pillow", "version": "12.3.0",
        "url": "https://files.pythonhosted.org/packages/9d/ac/31fb64e1e7efb5a4b50cd3d92049ba89ac6e4d8d3bb6a74e15048ca3353e/pillow-12.3.0-cp313-cp313-ios_13_0_arm64_iphoneos.whl",
        "sha256": "21900ce7ba264168cd50defae43cd75d25c833ad4ad6e73ffc5596d12e25ac89",
        "metadata_url": "https://pypi.org/pypi/pillow/12.3.0/json",
    },
    {
        "name": "cffi", "version": "2.1.1",
        "url": "https://files.pythonhosted.org/packages/9d/f4/035513d4117049066b4779dc3b7c0c0fdad175fa13731c9f4003f1cd1478/cffi-2.1.1-cp313-cp313-ios_13_0_arm64_iphoneos.whl",
        "sha256": "b5bdfd1c873d4e093aabc0ca84c4ca6dbc4f752afb5c86f146d9742580c9da2e",
        "metadata_url": "https://pypi.org/pypi/cffi/2.1.1/json",
    },
)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(pin, wheelhouse, offline):
    path = wheelhouse / pin["url"].rsplit("/", 1)[-1]
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"Offline wheel is missing: {path}")
        temporary = path.with_suffix(".download")
        try:
            with urllib.request.urlopen(pin["url"], timeout=60) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != pin["sha256"]:
                raise ValueError(f"Downloaded wheel SHA256 mismatch: {path.name}")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(path) != pin["sha256"]:
        raise ValueError(f"Cached wheel SHA256 mismatch: {path}")
    return path


def unpack(wheel, destination, pin):
    with zipfile.ZipFile(wheel) as archive:
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        names = {entry.filename for entry in files}
        if len(names) != len(files):
            raise ValueError(f"Duplicate wheel paths: {wheel.name}")
        for entry in files:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                raise ValueError(f"Unsafe wheel path: {entry.filename}")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"Unexpected wheel symlink: {entry.filename}")
        record_name, = (name for name in names if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode())))
        if {row[0] for row in rows} != names or len(rows) != len(names):
            raise ValueError(f"RECORD does not describe every wheel file: {wheel.name}")
        for name, expected, size in rows:
            if name == record_name:
                continue
            if not expected:
                raise ValueError(f"Missing RECORD hash: {name}")
            algorithm, encoded = expected.split("=", 1)
            data = archive.read(name)
            actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).rstrip(b"=").decode()
            if actual != encoded or len(data) != int(size):
                raise ValueError(f"RECORD validation failed: {name}")
        metadata_name, = (name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        if metadata["Name"].lower() != pin["name"] or metadata["Version"] != pin["version"]:
            raise ValueError(f"Package metadata disagrees with pin: {wheel.name}")
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            archive.extractall(temporary)
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(temporary, destination)
    return {
        "requires_dist": metadata.get_all("Requires-Dist") or [],
        "static_libraries": sorted(name for name in names if name.endswith(".a")),
        "record_files_verified": len(rows) - 1,
    }


def symbols(path, flags):
    return set(subprocess.check_output(["/usr/bin/nm", flags, str(path)], text=True).splitlines())


def version_string(version):
    return f"{version >> 16}.{(version >> 8) & 255}.{version & 255}"


def validate_native(path, python_exports):
    data = path.read_bytes()
    if len(data) < 32:
        raise ValueError(f"Truncated Mach-O: {path}")
    magic, cpu, _, filetype, count, size, _, _ = struct.unpack_from("<8I", data)
    if magic != 0xFEEDFACF or cpu != 0x0100000C or filetype not in (6, 8):
        raise ValueError(f"Expected a thin ARM64 extension library: {path}")
    if 32 + size > len(data):
        raise ValueError(f"Truncated Mach-O load commands: {path}")
    cursor, platform, minimum, sdk, identity = 32, None, None, None, None
    links, rpaths = [], []
    for _ in range(count):
        if cursor + 8 > 32 + size:
            raise ValueError(f"Truncated load command: {path}")
        command, length = struct.unpack_from("<II", data, cursor)
        if length < 8 or cursor + length > 32 + size:
            raise ValueError(f"Invalid load command size: {path}")
        if command == 0x32:  # LC_BUILD_VERSION
            platform, minimum, sdk = struct.unpack_from("<III", data, cursor + 8)
        if command in (0xC, 0xD, 0x80000018, 0x8000001F, 0x80000023, 0x20, 0x8000001C):
            offset = struct.unpack_from("<I", data, cursor + 8)[0]
            if offset < 12 or offset >= length:
                raise ValueError(f"Invalid load command string: {path}")
            value = data[cursor + offset:cursor + length].split(b"\0", 1)[0].decode()
            if command == 0xD:  # LC_ID_DYLIB is identity, not a dependency.
                identity = value
            elif command == 0x8000001C:
                rpaths.append(value)
            else:
                links.append(value)
        cursor += length
    if platform != 2 or minimum is None or minimum > 17 << 16:
        raise ValueError(f"Expected an iOS device library compatible with iOS 17: {path}")
    for link in links:
        if link != "@rpath/Python.framework/Python" and not link.startswith(("/usr/lib/", "/System/Library/Frameworks/")):
            raise ValueError(f"Unbundled native dependency in {path.name}: {link}")
    exports = symbols(path, "-gUj")
    initializer = "_PyInit_" + path.name.split(".", 1)[0]
    if initializer not in exports:
        raise ValueError(f"Python extension initializer is missing: {initializer}")
    python_references = {name for name in symbols(path, "-uj") if name.startswith(("_Py", "__Py"))}
    missing = python_references - python_exports
    if missing:
        raise ValueError(f"Python.framework lacks symbols needed by {path.name}: {sorted(missing)}")
    return {
        "sha256": digest(path), "arch": "arm64", "platform": "iOS",
        "min_os": version_string(minimum), "sdk": version_string(sdk),
        "id": identity, "links": links, "rpaths": rpaths,
        "python_symbols_verified": len(python_references),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=BUILD / "dependencies/numeric")
    parser.add_argument("--python-framework", type=Path, default=BUILD / "runtime/device/Python.framework")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    wheelhouse, unpacked = output / "wheelhouse", output / "unpacked"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    unpacked.mkdir(parents=True, exist_ok=True)
    python = args.python_framework.resolve() / "Python"
    python_exports = symbols(python, "-gUj")
    packages = []
    for pin in PINS:
        wheel = download(pin, wheelhouse, args.offline)
        destination = unpacked / pin["name"]
        item = dict(pin, wheel=str(wheel), **unpack(wheel, destination, pin))
        item["native"] = []
        for path in sorted(destination.rglob("*.so")):
            item["native"].append(dict(path=path.relative_to(destination).as_posix(),
                                       **validate_native(path, python_exports)))
        if not item["native"] or list(destination.rglob("*.dylib")):
            raise ValueError(f"Unexpected native library layout: {wheel.name}")
        item["non_system_frameworks"] = ["Python.framework"]
        item["validation"] = "Pinned SHA256, wheel RECORD, ARM64 iOS load commands and Python symbols verified; not imported on device."
        packages.append(item)
        print(f"{pin['name']} {pin['version']}: {len(item['native'])} ARM64 iOS extensions verified")
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps({"schema": 1, "python_framework": str(python), "packages": packages}, indent=2) + "\n")
    print(manifest)


if __name__ == "__main__":
    main()
