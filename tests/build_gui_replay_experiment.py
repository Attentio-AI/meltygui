"""Build an isolated constant-index-upload experiment; never install it."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'native/gui_prototype'
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('Cargo.toml', 'Cargo.lock'):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / 'src', destination / 'src')
    path = destination / 'src/texture.rs'
    text = path.read_text()
    old = '            glBufferData(ELEMENT, 24, indices.as_ptr().cast(), 0x88E0);'
    assert text.count(old) == 1
    text = text.replace(old, '''            // Child composition already uploaded these same six indices.
            if !self.children.contains_key(&id) {
                glBufferData(ELEMENT, 24, indices.as_ptr().cast(), 0x88E0);
            }''')
    path.write_text(text)
    cargo = shutil.which('cargo')
    if cargo is None:
        raise SystemExit('cargo is required')
    subprocess.run([cargo, 'build', '--release', '--locked', '--manifest-path',
                    str(destination / 'Cargo.toml')], check=True, close_fds=False,
                   env={**os.environ, 'PYO3_PYTHON': sys.executable})
    print(destination / 'target/release/lib_gui_native.so')


if __name__ == '__main__':
    main()
