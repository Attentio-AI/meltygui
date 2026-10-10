"""Build the opt-in Rust experiment using this Python interpreter (no package install)."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import sysconfig


def main():
    root = Path(__file__).resolve().parents[1]
    cargo = shutil.which('cargo')
    if cargo is None:
        raise SystemExit('Install Rust/cargo before building the gui prototype.')
    # Override with MELTY_RUST_TOOLCHAIN if the default toolchain is old.
    toolchain = os.environ.get('MELTY_RUST_TOOLCHAIN')
    command = [cargo, *([f'+{toolchain}'] if toolchain else []), 'build', '--release',
               '--manifest-path', str(root / 'native/gui_prototype/Cargo.toml'), '--locked']
    if not (root / 'native/gui_prototype/Cargo.lock').exists():
        command.remove('--locked')
    subprocess.run(command, check=True, close_fds=False,
                   env={**os.environ, 'PYO3_PYTHON': sys.executable})
    library = ('_gui_native.dll' if sys.platform == 'win32' else
               'lib_gui_native.dylib' if sys.platform == 'darwin' else 'lib_gui_native.so')
    source = root / 'native/gui_prototype/target/release' / library
    destination = root / 'meltygui/core/rendering' / ('_gui_native' + sysconfig.get_config_var('EXT_SUFFIX'))
    # Atomic replacement keeps another process's loaded mapping intact.
    temporary = destination.with_suffix('.building')
    shutil.copy2(source, temporary)
    temporary.replace(destination)
    print(f'Built {destination}')


if __name__ == '__main__':
    main()
