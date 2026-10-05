#!/usr/bin/env python3
"""Build iPhone FreeType/libspatialindex frameworks and their Python wrappers.

Run with a host Python 3.13 environment containing setuptools, setuptools-scm,
wheel and CMake. Native libraries target ARM64 iphoneos only. The frameworks
remain unsigned here; the application packaging phase must embed/sign them.
"""
from __future__ import annotations

from meltygui.platforms.ios import build_directory
BUILD = build_directory()

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

from meltygui.platforms.ios.prepare_bundle import validate_device_binary


SOURCES = {
    "spatialindex": (
        "2.1.0", "spatialindex-src-2.1.0.tar.gz", "spatialindex-src-2.1.0",
        "https://github.com/libspatialindex/libspatialindex/releases/download/2.1.0/spatialindex-src-2.1.0.tar.gz",
        "b36e2f8ac4c91a6d292f11d5925d584e13674015afd2132ed2870f1b5ec7b9ad"),
    "freetype": (
        "2.14.3", "freetype-2.14.3.tar.xz", "freetype-2.14.3",
        "https://download.savannah.gnu.org/releases/freetype/freetype-2.14.3.tar.xz",
        "36bc4f1cc413335368ee656c42afca65c5a3987e8768cc28cf11ba775e785a5f"),
    "rtree": (
        "1.4.1", "rtree-1.4.1.tar.gz", "rtree-1.4.1",
        "https://files.pythonhosted.org/packages/95/09/7302695875a019514de9a5dd17b8320e7a19d6e7bc8f85dcfb79a4ce2da3/rtree-1.4.1.tar.gz",
        "c6b1b3550881e57ebe530cc6cffefc87cd9bf49c30b37b894065a9f810875e46"),
    "freetype-py": (
        "2.5.1", "freetype-py-2.5.1.zip", "freetype-py-2.5.1",
        "https://files.pythonhosted.org/packages/d0/9c/61ba17f846b922c2d6d101cc886b0e8fb597c109cedfcb39b8c5d2304b54/freetype-py-2.5.1.zip",
        "cfe2686a174d0dd3d71a9d8ee9bf6a2c23f5872385cf8ce9f24af83d076e2fbd"),
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, *, env, log=None):
    command = list(map(str, command))
    executable = shutil.which(command[0], path=env.get("PATH"))
    if executable is None:
        raise ValueError(f"Build tool is unavailable: {command[0]}")
    command[0] = str(Path(executable).absolute())
    if log is None:
        return subprocess.check_output(command, env=env, close_fds=False, text=True).strip()
    with Path(log).open("w") as stream:
        stream.write("Arguments: " + json.dumps(command) + "\n")
        stream.flush()
        result = subprocess.run(command, env=env, close_fds=False, stdout=stream,
                                stderr=subprocess.STDOUT)
    if result.returncode:
        tail = "\n".join(Path(log).read_text().splitlines()[-35:])
        raise RuntimeError(f"Build failed; see {log}\n{tail}")


def source_tree(name, output):
    version, filename, directory, url, expected = SOURCES[name]
    downloads, sources = output / "downloads", output / "sources"
    downloads.mkdir(exist_ok=True)
    sources.mkdir(exist_ok=True)
    archive = downloads / filename
    if not archive.exists():
        temporary = archive.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if digest(temporary) != expected:
            raise ValueError(f"Downloaded {filename} SHA256 does not match its pinned release")
        temporary.replace(archive)
    if digest(archive) != expected:
        raise ValueError(f"Source SHA256 mismatch: {archive}")
    target = sources / directory
    if not target.exists():
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    path = PurePosixPath(member.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError(f"Unsafe source archive path: {member.filename}")
                source.extractall(sources)
        else:
            with tarfile.open(archive) as source:
                source.extractall(sources, filter="data")
    if not target.is_dir():
        raise ValueError(f"Source archive did not contain {directory}")
    return target, dict(name=name, version=version, url=url, sha256=expected, archive=str(archive))


def build_static(name, source, output, common, options, env, cmake, jobs):
    directory = output / "native" / name
    run([cmake, "-S", source, "-B", directory, *common, *options], env=env,
        log=output / f"{name}-configure.log")
    run([cmake, "--build", directory, "--parallel", str(jobs)], env=env,
        log=output / f"{name}-build.log")
    return directory


def framework(name, archives, output, compiler, sdk, deployment, env, symbols, libraries=()):
    directory = output / "frameworks" / f"{name}.framework"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    run([compiler, "-target", f"arm64-apple-ios{deployment}", "-isysroot", sdk,
         "-dynamiclib", "-Wl,-all_load", *archives, *libraries,
         "-Wl,-install_name,@rpath/" + f"{name}.framework/{name}",
         "-Wl,-headerpad_max_install_names", "-o", binary], env=env,
        log=output / f"{name}-link.log")
    validate_device_binary(binary)
    exports = run(["/usr/bin/nm", "-gU", binary], env=env)
    for symbol in symbols:
        if not any(line.split()[-1] == "_" + symbol for line in exports.splitlines()):
            raise RuntimeError(f"Required native API {symbol} is absent from {binary}")
    linked = run(["/usr/bin/otool", "-L", binary], env=env)
    dependencies = [line.strip().split(" (", 1)[0] for line in linked.splitlines()[2:]]
    if any(not name.startswith(("/usr/lib/", "/System/Library/")) for name in dependencies):
        raise RuntimeError(f"Unexpected non-system dependency in {binary}: {dependencies}")
    (directory / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": name, "CFBundleIdentifier": f"local.melty.dependencies.{name}".replace("_", "-"),
        "CFBundlePackageType": "FMWK", "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
        "MinimumOSVersion": deployment, "CFBundleSupportedPlatforms": ["iPhoneOS"],
    }))
    return dict(name=name, path=str(directory), binary=str(binary), sha256=digest(binary),
                required_symbols=list(symbols), dependencies=dependencies)


def python_wheel(name, source, output, deployment, env):
    wheels = output / "wheels"
    wheels.mkdir(exist_ok=True)
    args = ["setup.py", "bdist_wheel", "--dist-dir", str(wheels)]
    if name == "rtree":
        args += ["--plat-name", "ios_" + deployment.replace(".", "_") + "_arm64_iphoneos"]
    # chdir inside a short-lived child; spawning the build itself keeps Python's
    # posix_spawn path and never forks the interactive editor process.
    runner = "import os, runpy, sys; os.chdir(sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path('setup.py', run_name='__main__')"
    run([sys.executable, "-c", runner, source, *args], env=env, log=output / f"{name}-wheel.log")
    candidates = list(wheels.glob(name.replace("-", "_") + "-" + SOURCES[name][0] + "-*.whl"))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one built {name} wheel, found {candidates}")
    wheel = candidates[0]
    with zipfile.ZipFile(wheel) as archive:
        native = [name for name in archive.namelist() if name.endswith((".so", ".dylib", ".pyd", ".dll"))]
        if native:
            raise RuntimeError(f"Wrapper wheel unexpectedly bundled native binaries: {native}")
    return dict(name=name, version=SOURCES[name][0], path=str(wheel), sha256=digest(wheel))


def validate_loaders(python_stdlib, rtree_source, output, env):
    """Check iOS path discovery using the bundled CPython sources, not dlopen.

    iPhoneOS code cannot execute in a macOS host process. Keep this discovery
    check distinct from the Mach-O/export checks and subsequent device tests.
    """
    util = python_stdlib / "ctypes/util.py"
    dyld = python_stdlib / "ctypes/macholib/dyld.py"
    for path in (util, dyld):
        if not path.is_file():
            raise ValueError(f"CPython 3.13 device stdlib source is required: {path}")
    runner = '''import ctypes, importlib.util, json, os, platform, runpy, sys
from pathlib import Path
stdlib, finder, frameworks = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('ctypes.macholib.dyld', stdlib / 'ctypes/macholib/dyld.py')
dyld = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dyld)
sys.modules['ctypes.macholib.dyld'] = dyld
os.environ['DYLD_FRAMEWORK_PATH'] = str(frameworks)
os.environ['SPATIALINDEX_C_LIBRARY'] = str(frameworks / 'spatialindex_c.framework/spatialindex_c')
sys.platform = 'ios'
platform.system = lambda: 'iOS'
find = runpy.run_path(str(stdlib / 'ctypes/util.py'))['find_library']
found = find('freetype')
expected = str(frameworks / 'freetype.framework/freetype')
assert found == expected, (found, expected)
loads = []
def record_load(path):
    loads.append(path)
    return path
ctypes.cdll.LoadLibrary = record_load
rtree = runpy.run_path(str(finder))['load']()
assert loads == [os.environ['SPATIALINDEX_C_LIBRARY']], loads
print(json.dumps({'freetype': found, 'spatialindex_c': rtree, 'device_execution': False}))
'''
    found = json.loads(run([sys.executable, "-c", runner, python_stdlib,
                           rtree_source / "rtree/finder.py", output / "frameworks"], env=env))
    found["ctypes_sources"] = {str(path): digest(path) for path in (util, dyld)}
    return found


def build(args):
    if sys.version_info[:2] != (3, 13):
        raise ValueError("Use host Python 3.13 for the CPython 3.13 target wrapper wheels")
    if not 1 <= args.jobs <= 2:
        raise ValueError("--jobs must be 1 or 2")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DEVELOPER_DIR=str(args.developer_dir.resolve()))
    env.pop("FREETYPEPY_BUNDLE_FT", None)
    env["SOURCE_DATE_EPOCH"] = "315532800"
    cmake = args.cmake or shutil.which("cmake")
    if not cmake:
        raise ValueError("Install CMake in the host environment or pass --cmake")
    sdk = run(["/usr/bin/xcrun", "--sdk", "iphoneos", "--show-sdk-path"], env=env)
    clang = run(["/usr/bin/xcrun", "--sdk", "iphoneos", "--find", "clang"], env=env)
    clangxx = run(["/usr/bin/xcrun", "--sdk", "iphoneos", "--find", "clang++"], env=env)
    common = ["-G", "Unix Makefiles", "-DCMAKE_SYSTEM_NAME=iOS", "-DCMAKE_OSX_ARCHITECTURES=arm64",
              f"-DCMAKE_OSX_SYSROOT={sdk}", f"-DCMAKE_OSX_DEPLOYMENT_TARGET={args.deployment_target}",
              f"-DCMAKE_C_COMPILER={clang}", f"-DCMAKE_CXX_COMPILER={clangxx}",
              "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY", "-DCMAKE_BUILD_TYPE=Release",
              "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTING=OFF",
              "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY", "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY"]
    sources, provenance = {}, []
    for name in SOURCES:
        sources[name], record = source_tree(name, output)
        provenance.append(record)
    spatial = build_static("spatialindex", sources["spatialindex"], output, common, [], env, cmake, args.jobs)
    # Upstream's spatialindex archive already contains SIDX_CAPI_CPP. Loading
    # both archives with -all_load would duplicate every C API definition.
    spatial_archives = [next(spatial.rglob("libspatialindex.a"))]
    freetype = build_static("freetype", sources["freetype"], output, common,
                           ["-DFT_REQUIRE_ZLIB=ON", "-DFT_DISABLE_BZIP2=ON", "-DFT_DISABLE_PNG=ON",
                            "-DFT_DISABLE_HARFBUZZ=ON", "-DFT_DISABLE_BROTLI=ON"], env, cmake, args.jobs)
    frameworks = [
        framework("spatialindex_c", spatial_archives, output, clangxx, sdk, args.deployment_target, env,
                  ("Index_Create", "Index_InsertData", "Index_Intersects_id", "Index_NearestNeighbors_id", "SIDX_Version")),
        framework("freetype", [next(freetype.rglob("libfreetype.a"))], output, clang, sdk,
                  args.deployment_target, env, ("FT_Init_FreeType", "FT_New_Face", "FT_Load_Char", "FT_Render_Glyph"),
                  libraries=("-lz",)),
    ]
    shutil.copy2(sources["spatialindex"] / "COPYING", Path(frameworks[0]["path"]) / "LICENSE.txt")
    shutil.copy2(sources["freetype"] / "docs/FTL.TXT", Path(frameworks[1]["path"]) / "LICENSE.txt")
    wheels = [python_wheel(name, sources[name], output, args.deployment_target, env)
              for name in ("rtree", "freetype-py")]
    discovery = validate_loaders(args.python_stdlib.resolve(), sources["rtree"], output, env)
    manifest = dict(format_version=1, target=f"arm64-apple-ios{args.deployment_target}", python="3.13",
                    sdk=sdk, sources=provenance, frameworks=frameworks, wheels=wheels,
                    build_tools={name: importlib.metadata.version(name)
                                 for name in ("setuptools", "setuptools-scm", "wheel")},
                    cmake=run([cmake, "--version"], env=env).splitlines()[0],
                    developer_dir=env["DEVELOPER_DIR"], loader_validation=discovery,
                    loader_environment={
                        "SPATIALINDEX_C_LIBRARY": "${APP_BUNDLE}/Frameworks/spatialindex_c.framework/spatialindex_c",
                        "DYLD_FRAMEWORK_PATH": "${APP_BUNDLE}/Frameworks"},
                    limitations=["No device execution; verified compile/link, Mach-O platform and native API exports",
                                 "FreeType uses system zlib; optional PNG/BZip2/Brotli/HarfBuzz modules are disabled"])
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Built {len(frameworks)} ARM64 iOS frameworks and {len(wheels)} Python wrapper wheels: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BUILD / "dependencies/platform")
    parser.add_argument("--developer-dir", type=Path, default=Path("/Applications/Xcode-beta.app/Contents/Developer"))
    parser.add_argument("--cmake")
    parser.add_argument("--python-stdlib", type=Path,
                        default=BUILD / "runtime/device/lib/python3.13")
    parser.add_argument("--deployment-target", default="17.0")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, StopIteration) as error:
        parser.exit(1, f"Cannot build iOS platform dependencies: {error}\n")


if __name__ == "__main__":
    main()
