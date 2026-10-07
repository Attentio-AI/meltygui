"""Build-time selection of a project's Python and its matching iOS runtime."""
import json
from pathlib import Path
import re
import subprocess


def project_python(root, executable=None):
    """Preserve the venv executable path rather than resolving its symlink."""
    if executable is not None:
        path = Path(executable).absolute()
    else:
        path = Path(root).absolute() / '.venv/bin/python'
    if not path.is_file():
        raise ValueError(f'Project Python is missing: {path}; select its venv with --project-python')
    return str(path)


def python_info(executable):
    program = ('import importlib.util,json,sys; print(json.dumps(dict('
               'implementation=sys.implementation.name, version="%d.%d" % sys.version_info[:2], '
               'full_version="%d.%d.%d" % sys.version_info[:3], '
               'cache_tag=sys.implementation.cache_tag, magic=importlib.util.MAGIC_NUMBER.hex())))')
    result = subprocess.run([str(Path(executable).absolute()), '-I', '-c', program],
                            check=True, close_fds=False, capture_output=True, text=True)
    info = json.loads(result.stdout)
    if info['implementation'] != 'cpython':
        raise ValueError('The project venv must use CPython to match the embedded iOS runtime')
    return info


def framework_version(framework):
    header = (Path(framework) / 'Headers/patchlevel.h').read_text()
    parts = []
    for macro in ('PY_MAJOR_VERSION', 'PY_MINOR_VERSION'):
        match = re.search(rf'^\s*#\s*define\s+{macro}\s+(\d+)\s*$', header, re.MULTILINE)
        if not match:
            raise ValueError(f'Missing {macro} in {framework}/Headers/patchlevel.h')
        parts.append(match[1])
    return '.'.join(parts)


def validate_runtime(info, framework, library):
    version = framework_version(framework)
    if version != info['version']:
        raise ValueError(f"Project venv uses CPython {info['version']}, but the supplied iOS "
                         f"Python.framework is {version}; supply an iOS runtime matching the project venv")
    if not (Path(library) / f'python{version}/encodings/__init__.py').is_file():
        raise ValueError(f'--python-lib must contain the matching device python{version} standard library')
    return version


def configured_python(config):
    """Resolve older generated projects too, using their enclosing project venv."""
    if config.get('project_python'):
        return project_python(config.get('project_dir', config['app_dir']), config['project_python'])
    if config.get('project_dir'):
        return project_python(config['project_dir'])
    app = Path(config['app_dir']).absolute()
    for root in (app, *app.parents):
        if (root / '.venv/bin/python').is_file():
            return project_python(root)
    return project_python(app)


def build_python(config):
    """Validate the selected compiler/runtime before packaging or source-only deploys."""
    compiler = configured_python(config)
    info = python_info(compiler)
    version = info['version']
    if config.get('python_framework'):
        validate_runtime(info, config['python_framework'], config['python_lib'])
    elif not (Path(config['python_lib']) / f'python{version}/encodings/__init__.py').is_file():
        raise ValueError(f'Project venv uses CPython {version}; supply its matching iOS standard library and framework')
    if config.get('python_version', version) != version or config.get('python_magic', info['magic']) != info['magic']:
        raise ValueError('The project venv Python changed; regenerate the iOS project with its matching runtime')
    return compiler, info
