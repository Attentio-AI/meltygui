#!/usr/bin/env python3
"""Build pinned Melty ImGui bindings for CPython 3.13 on ARM64 iOS devices.

Run with a host Python containing Cython==3.2.4. This compiles the project's
actual binding and ImGui sources; it does not link a second ImGui into the
native renderer. Each extension retains the implementation used by its
upstream setup.py. The output wheel and packages directory are unsigned build
inputs for prepare_bundle.py, which performs the iOS framework conversion.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

from meltygui.platforms.ios.prepare_bundle import validate_device_binary

from meltygui.platforms.ios import build_directory

BUILD = build_directory()
ROOT = Path(__file__).resolve().parent
VERSION = "2.0.1"
CYTHON_VERSION = "3.2.4"
SDIST_NAME = f"meltygui_imgui-{VERSION}.tar.gz"
SDIST_URL = (
    "https://files.pythonhosted.org/packages/0d/93/"
    "f0653038d70e13b0669a92df98ff3b54aa1f0554ffc0833946003afb523d/"
    + SDIST_NAME
)
SDIST_SHA256 = "794476ab9a209be6d90acdcf4dd77437c1ce15691c207e7f2212eff300cdf403"
COMMON_SOURCES = (
    "imgui-cpp/imgui.cpp", "imgui-cpp/imgui_draw.cpp",
    "imgui-cpp/imgui_demo.cpp", "imgui-cpp/imgui_widgets.cpp",
    "imgui-cpp/imgui_tables.cpp", "config-cpp/py_imconfig.cpp",
)


def run(command, *, env, cwd=None, log=None):
    if log is None:
        return subprocess.check_output(command, env=env, cwd=cwd, text=True).strip()
    with Path(log).open("w") as stream:
        stream.write("Arguments: " + json.dumps(list(map(str, command))) + "\n")
        stream.flush()
        result = subprocess.run(command, env=env, cwd=cwd, stdout=stream,
                                stderr=subprocess.STDOUT)
    if result.returncode:
        tail = "\n".join(Path(log).read_text().splitlines()[-40:])
        raise RuntimeError(f"Build command failed; see {log}\n{tail}")


def verified_source(output, sdist=None):
    archive = Path(sdist).resolve() if sdist else output / SDIST_NAME
    if not archive.exists():
        if sdist:
            raise ValueError(f"Source archive does not exist: {archive}")
        temporary = archive.with_suffix(".part")
        urllib.request.urlretrieve(SDIST_URL, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != SDIST_SHA256:
            raise ValueError("Downloaded meltygui-imgui source SHA256 does not match")
        temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SDIST_SHA256:
        raise ValueError("Use the unmodified meltygui-imgui 2.0.1 PyPI source archive")
    source_parent = output / "source"
    source_parent.mkdir(exist_ok=True)
    with tarfile.open(archive) as source:
        # Use Python's safe extraction filter; never copy a cached desktop build.
        source.extractall(source_parent, filter="data")
    return source_parent / f"meltygui_imgui-{VERSION}"


def write_wheel(packages, output, tag):
    dist = f"meltygui_imgui-{VERSION}.dist-info"
    record = packages / dist / "RECORD"
    rows = []
    for path in sorted(packages.rglob("*")):
        if path.is_file() and path != record:
            content = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            rows.append((path.relative_to(packages).as_posix(),
                         "sha256=" + digest.decode(), str(len(content))))
    rows.append((f"{dist}/RECORD", "", ""))
    text = io.StringIO(newline="")
    csv.writer(text, lineterminator="\n").writerows(rows)
    record.write_text(text.getvalue())
    wheel = output / f"meltygui_imgui-{VERSION}-{tag}.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(packages.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(packages).as_posix(),
                                       date_time=(2026, 10, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if path.suffix == ".so" else 0o100644) << 16
                archive.writestr(info, path.read_bytes())
    return wheel


def build(*, python_framework, output=BUILD / "binding", sdist=None,
          developer_dir=None, deployment_target="17.0", jobs=4):
    import Cython
    if Cython.__version__ != CYTHON_VERSION:
        raise ValueError(f"Run this helper with Cython=={CYTHON_VERSION}, found {Cython.__version__}")
    if not re.fullmatch(r"\d+\.\d+", deployment_target):
        raise ValueError("Deployment target must have major.minor form")
    if tuple(map(int, deployment_target.split("."))) < (17, 0):
        raise ValueError("The native host requires iOS 17.0 or newer")
    if jobs < 1:
        raise ValueError("--jobs must be positive")
    framework = Path(python_framework).resolve()
    header = (framework / "Headers/patchlevel.h").read_text()
    if not all(re.search(rf"^#define PY_{name}_VERSION\s+{value}\s*$", header, re.M)
               for name, value in (("MAJOR", 3), ("MINOR", 13))):
        raise ValueError("The device binding requires CPython 3.13 headers")
    validate_device_binary(framework / "Python")
    env = os.environ.copy()
    if developer_dir:
        env["DEVELOPER_DIR"] = str(Path(developer_dir).resolve())
    sdk = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], env=env)
    compiler = run(["xcrun", "--sdk", "iphoneos", "--find", "clang++"], env=env)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    logs, objects = output / "logs", output / "objects"
    logs.mkdir(exist_ok=True)
    objects.mkdir(exist_ok=True)
    source = verified_source(output, sdist)

    # The published sdist contains Cython 0.29 output, which cannot target 3.13.
    # Match the binding's pinned 3.13 build directives without importing its
    # desktop-only setup dependencies (GLFW and PyOpenGL).
    for module in ("core", "internal"):
        run([sys.executable, "-m", "cython", "--cplus", "-I", str(source), "-X", "language_level=2",
             "-X", "legacy_implicit_noexcept=True", f"meltygui_imgui/{module}.pyx"],
            env=env, cwd=source, log=logs / f"cython-{module}.log")

    abi = source / "melty_ios_abi.cpp"
    abi.write_text("""#include <stddef.h>
#include "imgui.h"
static_assert(sizeof(ImDrawIdx) == 4, "Metal expects 32-bit indices");
static_assert(sizeof(ImDrawVert) == 20, "Metal expects 20-byte ImGui vertices");
static_assert(offsetof(ImDrawVert, pos) == 0, "ImGui position offset changed");
static_assert(offsetof(ImDrawVert, uv) == 8, "ImGui UV offset changed");
static_assert(offsetof(ImDrawVert, col) == 16, "ImGui color offset changed");
""")
    flags = ["-target", f"arm64-apple-ios{deployment_target}", "-isysroot", sdk,
             "-std=c++17", "-O2", "-fPIC", "-fvisibility=hidden",
             "-DPYIMGUI_CUSTOM_EXCEPTION", "-include", str(source / "config-cpp/py_imconfig.h"),
             "-I", str(framework / "Headers"),
             f"-ffile-prefix-map={source}=meltygui_imgui-{VERSION}"]
    for include in ("meltygui_imgui", "config-cpp", "imgui-cpp", "ansifeed-cpp"):
        flags += ["-I", str(source / include)]

    def compile_one(relative):
        name = relative.replace("/", "-").removesuffix(".cpp")
        target = objects / f"{name}.o"
        run([compiler, *flags, "-c", str(source / relative), "-o", str(target)],
            env=env, cwd=source, log=logs / f"{name}.log")
        return relative, target

    sources = (*COMMON_SOURCES, "meltygui_imgui/core.cpp", "meltygui_imgui/internal.cpp",
               "melty_ios_abi.cpp")
    with ThreadPoolExecutor(max_workers=jobs) as workers:
        compiled = dict(workers.map(compile_one, sources))

    packages = output / "packages"
    package = packages / "meltygui_imgui"
    if package.exists():
        shutil.rmtree(package)
    # Preserve upstream Python modules/resources; exclude all native/build input.
    shutil.copytree(source / "meltygui_imgui", package,
                    ignore=shutil.ignore_patterns("*.cpp", "*.h", "*.pyx", "*.pxd", "*.pxi",
                                                  "*.so", "*.pyc", "__pycache__"))
    for module in ("core", "internal"):
        binary = package / f"{module}.cpython-313-iphoneos.so"
        run([compiler, "-target", f"arm64-apple-ios{deployment_target}", "-isysroot", sdk,
             "-dynamiclib", *map(str, (compiled[item] for item in COMMON_SOURCES)),
             str(compiled[f"meltygui_imgui/{module}.cpp"]), str(compiled["melty_ios_abi.cpp"]),
             "-F", str(framework.parent), "-framework", "Python",
             "-Wl,-rpath,@loader_path/..",
             f"-Wl,-install_name,@rpath/meltygui_imgui.{module}.framework/meltygui_imgui.{module}",
             "-o", str(binary)], env=env, log=logs / f"link-{module}.log")
        validate_device_binary(binary)
        exports = run(["xcrun", "nm", "-gU", str(binary)], env=env)
        if not re.search(rf"\b_PyInit_{module}$", exports, re.M):
            raise RuntimeError(f"Missing Python module entry point in {binary}")

    tag = "cp313-cp313-ios_" + deployment_target.replace(".", "_") + "_arm64_iphoneos"
    dist = packages / f"meltygui_imgui-{VERSION}.dist-info"
    dist.mkdir(exist_ok=True)
    (dist / "METADATA").write_bytes((source / "PKG-INFO").read_bytes())
    (dist / "WHEEL").write_text("Wheel-Version: 1.0\nGenerator: melty-ios-build-imgui\n"
                                f"Root-Is-Purelib: false\nTag: {tag}\n")
    (dist / "top_level.txt").write_text("meltygui_imgui\n")
    (dist / "licenses").mkdir(exist_ok=True)
    shutil.copy2(source / "LICENSE", dist / "licenses/LICENSE")
    shutil.copytree(source / "LICENSES", dist / "licenses/LICENSES", dirs_exist_ok=True)
    wheel = write_wheel(packages, output, tag)
    (output / "build-manifest.json").write_text(json.dumps({
        "source_url": SDIST_URL, "source_sha256": SDIST_SHA256,
        "cython": CYTHON_VERSION, "python": "3.13", "target": f"arm64-apple-ios{deployment_target}",
        "sdk": sdk, "compiler": run([compiler, "--version"], env=env),
        "wheel": str(wheel), "packages": str(packages),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "verification": "compile/link, iOS Mach-O metadata, Python exports and ImGui layout; no device execution",
    }, indent=2) + "\n")
    return wheel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if sys.version_info < (3, 12):
        parser.error("Use a host Python 3.12 or newer with Cython==3.2.4")
    parser.add_argument("--python-framework", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=BUILD / "binding")
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--developer-dir", type=Path)
    parser.add_argument("--deployment-target", default="17.0")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    try:
        print(build(**vars(parser.parse_args())))
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cannot build device ImGui binding: {error}\n")


if __name__ == "__main__":
    main()
