"""Build the Rust `Source` extension (native/source). It uses Python's stable
ABI: one build serves CPython 3.11 and later on this platform.

    .venv/bin/python tools/build_source.py

The library lands beside its Python face as meltygui/model/_source_native.*.
Needs cargo (on PATH or in ~/.cargo/bin); `MELTY_RUST_TOOLCHAIN` picks an
installed rustup toolchain.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    crate = root / 'native/source'
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo/bin'))
    if cargo is None:
        raise SystemExit('Install Rust/cargo before building the source extension.')
    toolchain = os.environ.get('MELTY_RUST_TOOLCHAIN')
    command = [cargo, *([f'+{toolchain}'] if toolchain else []), 'build', '--release',
               '--manifest-path', str(crate / 'Cargo.toml'), *sys.argv[1:]]
    subprocess.run(command, check=True, close_fds=False, env={**os.environ, 'PYO3_PYTHON': sys.executable})
    library = ('_source_native.dll' if sys.platform == 'win32' else
               'lib_source_native.dylib' if sys.platform == 'darwin' else 'lib_source_native.so')
    folder = root / 'meltygui/model'
    destination = folder / ('_source_native.pyd' if sys.platform == 'win32' else '_source_native.abi3.so')
    # Atomic replacement keeps another process's loaded mapping intact.
    temporary = destination.with_suffix('.building')
    shutil.copy2(crate / 'target/release' / library, temporary)
    temporary.replace(destination)
    # A build for one interpreter version would be imported before this one.
    for stale in folder.glob('_source_native*'):
        if stale != destination:
            stale.unlink()
    print(f'Built {destination}')


if __name__ == '__main__':
    main()
