#!/usr/bin/env python3
"""Generate a device-only Xcode project without third-party project tools."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sys

from meltygui.platforms.ios.prepare_bundle import validate_device_binary
from meltygui.platforms.ios.runtime import project_python as select_project_python, python_info, validate_runtime

from meltygui.platforms.ios import build_directory

BUILD = build_directory()
ROOT = Path(__file__).resolve().parent


def generate(*, python_framework, python_lib, app_dir, packages_dir=None,
             output=BUILD, team="", bundle_id=None,
             entry_module=None, application=None, project_python=None,
             renderer_sources=(), embed_frameworks=(), toolkit_dir=ROOT.parents[1]):
    from meltygui.platforms.ios.application import read_application
    app_config = read_application(application or app_dir)
    bundle_id = bundle_id or app_config['bundle_id']
    entry_module = entry_module or app_config['entry_module']
    python_framework = Path(python_framework).resolve()
    python_lib = Path(python_lib).resolve()
    app_dir = Path(app_dir).resolve()
    packages_dir = Path(packages_dir).resolve() if packages_dir else None
    output = Path(output).resolve()
    toolkit_dir = Path(toolkit_dir).resolve()
    if python_framework.name != "Python.framework":
        raise ValueError("Pass the ARM64 iOS device slice's Python.framework")
    project_python = select_project_python(app_config['root'], project_python)
    runtime = python_info(project_python)
    validate_runtime(runtime, python_framework, python_lib)
    validate_device_binary(python_framework / "Python")
    if not app_dir.is_dir() or (packages_dir is not None and not packages_dir.is_dir()):
        raise ValueError("Application and package inputs must be existing directories")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id):
        raise ValueError("Use a reverse-DNS bundle identifier for device signing")
    if not all(part.isidentifier() for part in entry_module.split(".")):
        raise ValueError("The entry module must be a Python dotted module name")

    sources = sorted((ROOT / "Host").glob("*.mm")) + [Path(path).resolve() for path in renderer_sources]
    frameworks = [python_framework] + [Path(path).resolve() for path in embed_frameworks]
    for path in sources + frameworks:
        if not path.exists():
            raise ValueError(f"Missing native source or framework: {path}")
    if len({path.name for path in frameworks}) != len(frameworks):
        raise ValueError("Embedded frameworks must have unique names")

    objects = {}

    def add(key, isa, **fields):
        oid = hashlib.sha256(key.encode()).hexdigest()[:24].upper()
        objects[oid] = {"isa": isa, **fields}
        return oid

    def file(path, kind, tree="<absolute>"):
        return add(f"file:{tree}:{path}", "PBXFileReference", lastKnownFileType=kind,
                   name=Path(path).name, path=str(path), sourceTree=tree)

    def build_file(ref, settings=None, key=""):
        fields = {"fileRef": ref}
        if settings:
            fields["settings"] = settings
        return add(f"build:{key}:{ref}", "PBXBuildFile", **fields)

    source_refs = [file(path, "sourcecode.cpp.objcpp") for path in sources]
    header_refs = [file(path, "sourcecode.c.h") for path in sorted((ROOT / "Host").glob("*.h*"))]
    linked_refs = [file(path, "wrapper.framework") for path in frameworks]
    system_refs = [file(f"System/Library/Frameworks/{name}.framework", "wrapper.framework", "SDKROOT")
                   for name in ("UIKit", "Foundation", "CoreGraphics", "Metal", "QuartzCore")]
    product = add("product", "PBXFileReference", explicitFileType="wrapper.application",
                  path="Melty.app", sourceTree="BUILT_PRODUCTS_DIR")
    products = add("products", "PBXGroup", children=[product], name="Products", sourceTree="<group>")
    group = add("group", "PBXGroup", children=source_refs + header_refs + linked_refs + system_refs + [products],
                sourceTree="<group>")
    compile_phase = add("sources", "PBXSourcesBuildPhase", buildActionMask=2147483647,
                        files=[build_file(ref) for ref in source_refs], runOnlyForDeploymentPostprocessing=0)
    link_phase = add("frameworks", "PBXFrameworksBuildPhase", buildActionMask=2147483647,
                     files=[build_file(ref) for ref in linked_refs + system_refs], runOnlyForDeploymentPostprocessing=0)
    embed_phase = add("embed", "PBXCopyFilesBuildPhase", buildActionMask=2147483647,
                      files=[build_file(ref, {"ATTRIBUTES": ["CodeSignOnCopy", "RemoveHeadersOnCopy"]}, "embed")
                             for ref in linked_refs], dstPath="", dstSubfolderSpec=10, name="Embed Frameworks",
                      runOnlyForDeploymentPostprocessing=0)
    config_path = output / "host-build.json"
    # Preserve a virtualenv's interpreter path: resolving its symlink would
    # lose the installed toolkit when Xcode runs the module commands.
    python = str(Path(sys.executable).absolute())
    # Shell quote every generated path; project input paths may contain spaces.
    import shlex
    shaders_dir = output / "shaders"
    script = (f"set -eu\n{shlex.quote(python)} -m meltygui.platforms.ios.compile_shaders "
              f"--toolkit {shlex.quote(str(toolkit_dir))} --output {shlex.quote(str(shaders_dir))}\n"
              f"{shlex.quote(python)} -m meltygui.platforms.ios.prepare_bundle --config {shlex.quote(str(config_path))}\n")
    # Xcode must invalidate the enclosing app's CodeSign task when this phase
    # replaces resources/native modules during an otherwise incremental build.
    stage_phase = add("stage", "PBXShellScriptBuildPhase", buildActionMask=2147483647, files=[],
                      inputPaths=[], outputPaths=[
                          "$(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/" + name
                          for name in ("HostSettings.plist", "app", "app_packages", "host", "python")
                      ], alwaysOutOfDate=1,
                      name="Package embedded Python", shellPath="/bin/sh", shellScript=script,
                      runOnlyForDeploymentPostprocessing=0)
    configurations = []
    project_configs = []
    for name in ("Debug", "Release"):
        settings = {
            "ARCHS": "arm64", "VALID_ARCHS": "arm64", "SUPPORTED_PLATFORMS": "iphoneos",
            "SDKROOT": "iphoneos", "IPHONEOS_DEPLOYMENT_TARGET": "17.0",
            "TARGETED_DEVICE_FAMILY": "1,2", "SUPPORTS_MACCATALYST": "NO",
            "SUPPORTS_MAC_DESIGNED_FOR_IPHONE_IPAD": "NO", "ONLY_ACTIVE_ARCH": "YES",
            "CLANG_ENABLE_OBJC_ARC": "YES", "CLANG_CXX_LANGUAGE_STANDARD": "c++17",
            "ALWAYS_SEARCH_USER_PATHS": "NO",
            "CLANG_WARN_QUOTED_INCLUDE_IN_FRAMEWORK_HEADER": "NO",
            "ENABLE_USER_SCRIPT_SANDBOXING": "NO", "CODE_SIGN_STYLE": "Automatic",
            "PRODUCT_NAME": "Melty", "PRODUCT_BUNDLE_IDENTIFIER": bundle_id,
            "INFOPLIST_FILE": str(ROOT / "Host/Info.plist"), "GENERATE_INFOPLIST_FILE": "NO",
            "FRAMEWORK_SEARCH_PATHS": ["$(inherited)"] + sorted({str(path.parent) for path in frameworks}),
            "HEADER_SEARCH_PATHS": ["$(inherited)", str(ROOT / "Host"), str(python_framework / "Headers")],
            "LD_RUNPATH_SEARCH_PATHS": ["$(inherited)", "@executable_path/Frameworks"],
            "GCC_OPTIMIZATION_LEVEL": "0" if name == "Debug" else "s",
            "GCC_PREPROCESSOR_DEFINITIONS": ["$(inherited)", "DEBUG=1"] if name == "Debug" else ["$(inherited)"],
            "DEBUG_INFORMATION_FORMAT": "dwarf" if name == "Debug" else "dwarf-with-dsym",
            "OTHER_LDFLAGS": ["$(inherited)", "-ObjC"],
        }
        if team:
            settings["DEVELOPMENT_TEAM"] = team
        configurations.append(add(f"target:{name}", "XCBuildConfiguration", name=name, buildSettings=settings))
        project_configs.append(add(f"project:{name}", "XCBuildConfiguration", name=name, buildSettings={}))
    config_list = add("target configs", "XCConfigurationList", buildConfigurations=configurations,
                      defaultConfigurationIsVisible=0, defaultConfigurationName="Debug")
    project_config_list = add("project configs", "XCConfigurationList", buildConfigurations=project_configs,
                              defaultConfigurationIsVisible=0, defaultConfigurationName="Debug")
    target = add("target", "PBXNativeTarget", name="Melty", productName="Melty", productReference=product,
                 productType="com.apple.product-type.application", buildConfigurationList=config_list,
                 buildPhases=[compile_phase, link_phase, embed_phase, stage_phase], buildRules=[], dependencies=[])
    project = add("project", "PBXProject", attributes={"LastUpgradeCheck": "1600"},
                  buildConfigurationList=project_config_list, compatibilityVersion="Xcode 14.0",
                  developmentRegion="en", knownRegions=["en", "Base"], mainGroup=group,
                  productRefGroup=products, projectDirPath="", projectRoot="", targets=[target])
    project_dir = output / "MeltyIOS.xcodeproj"
    project_dir.mkdir(parents=True, exist_ok=True)
    # Xcode accepts XML property lists as well as the usual OpenStep spelling.
    (project_dir / "project.pbxproj").write_bytes(plistlib.dumps({
        "archiveVersion": "1", "classes": {}, "objectVersion": "56", "objects": objects, "rootObject": project,
    }, sort_keys=True))
    config_path.write_text(json.dumps({
        "python_lib": str(python_lib), "app_dir": str(app_dir),
        "python_framework": str(python_framework), "python_version": runtime['version'],
        "python_magic": runtime['magic'], "project_python": project_python,
        "project_dir": str(app_config['root'].resolve()),
        "generator": {"team": team, "toolkit_dir": str(toolkit_dir),
                      "renderer_sources": [str(Path(path).resolve()) for path in renderer_sources],
                      "embed_frameworks": [str(Path(path).resolve()) for path in embed_frameworks]},
        "packages_dir": str(packages_dir) if packages_dir else None,
        "bootstrap_dir": str(ROOT / "Python"), "bundle_id": bundle_id, "entry_module": entry_module,
        "shaders_dir": str(shaders_dir),
    }, indent=2) + "\n")
    from meltygui.platforms.ios.app_icon import sync_project_icon
    sync_project_icon(app_config['root'], output)
    return project_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-framework", required=True, type=Path)
    parser.add_argument("--python-lib", required=True, type=Path)
    parser.add_argument("--application", type=Path, default=Path.cwd())
    parser.add_argument("--project-python", type=Path, help="Project venv interpreter (default: APPLICATION/.venv/bin/python)")
    parser.add_argument("--app-dir", type=Path, default=BUILD / "app-bundle/app")
    parser.add_argument("--packages-dir", type=Path, default=BUILD / "app-bundle/packages")
    parser.add_argument("--output", type=Path, default=BUILD)
    parser.add_argument("--team", default="")
    parser.add_argument("--bundle-id")
    parser.add_argument("--entry-module")
    parser.add_argument("--toolkit-dir", type=Path, default=ROOT.parents[1])
    parser.add_argument("--renderer-source", dest="renderer_sources", action="append", default=[], type=Path)
    parser.add_argument("--embed-framework", dest="embed_frameworks", action="append", default=[], type=Path)
    args = parser.parse_args()
    try:
        print(generate(**vars(args)))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot generate iOS project: {error}\n")


if __name__ == "__main__":
    main()
