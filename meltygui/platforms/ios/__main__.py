"""Run with python -m meltygui.platforms.ios COMMAND --help."""
import importlib
import sys


def main():
    commands = {
        'stage': 'stage_dependencies', 'generate': 'generate',
        'shaders': 'compile_shaders', 'build-imgui': 'build_imgui',
        'build-platform': 'build_platform_deps', 'build-rust': 'build_rust_deps',
        'build-crypto': 'build_crypto', 'numeric-wheels': 'download_numeric_wheels',
        'prepare': 'provision',
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        raise SystemExit('Choose an iOS command: ' + ', '.join(commands))
    command = sys.argv.pop(1)
    importlib.import_module('meltygui.platforms.ios.' + commands[command]).main()


if __name__ == '__main__':
    main()
