#!/usr/bin/env python3
"""Build the real Metal encoder as a local test extension (no UIKit/simulator)."""
from pathlib import Path
from meltygui.platforms.ios import build_directory
import subprocess
import sys
import sysconfig

ROOT=Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"
OUTPUT=build_directory() / "metal-test"


def build():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,"-m","meltygui.platforms.ios.compile_shaders","--toolkit",str(ROOT.parents[1]),
                    "--sdk","macosx","--output",str(OUTPUT)],check=True,close_fds=False)
    binary=OUTPUT/("_melty_metal"+sysconfig.get_config_var("EXT_SUFFIX"))
    subprocess.run(["xcrun","--sdk","macosx","clang++","-std=c++17","-fobjc-arc","-shared",
        "-undefined","dynamic_lookup","-DMELTY_METAL_TESTING=1","-Wall","-Wextra","-Werror","-Wno-unused-parameter",
        "-I"+sysconfig.get_path("include"),"-framework","Foundation","-framework","Metal","-framework","QuartzCore",
        str(ROOT/"Host/MeltyMetalRenderer.mm"),"-o",str(binary)],check=True,close_fds=False)
    return OUTPUT


if __name__=="__main__":
    print(build())
