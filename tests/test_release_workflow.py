"""Execute the release workflow's version check, not a reimplementation of it."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest


@pytest.mark.parametrize('expected,accepted', [('0.1.1', True), ('', True), ('0.1.2', False)])
def test_confirmed_release_version_shell(tmp_path, expected, accepted):
    workflow = Path(__file__).resolve().parents[1] / '.github/workflows/release.yml'
    if not workflow.is_file():
        pytest.skip('Workflow is only present in the source checkout')
    uv = shutil.which('uv')
    bash = shutil.which('bash')
    if not uv or not bash:
        pytest.skip('The workflow shell check requires uv and bash')
    source = workflow.read_text().split('      - name: Check confirmed release version\n', 1)[1]
    source = source.split('        run: |\n', 1)[1]
    script = textwrap.dedent(re.split(r'^      - ', source, maxsplit=1, flags=re.M)[0])
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "release-test"\nversion = "0.1.1"\n')
    result = subprocess.run(
        [bash, '-e', '-c', 'cd -- "$1"\n' + script, 'release-version-check', str(tmp_path)],
        env={**os.environ, 'EXPECTED_VERSION': expected, 'UV_PYTHON': '3.12',
             'PATH': str(Path(uv).parent) + os.pathsep + os.environ.get('PATH', '')},
        capture_output=True, text=True, close_fds=False, timeout=60,
    )
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
    if not accepted:
        assert 'Confirmed 0.1.2 but source states 0.1.1' in result.stdout


@pytest.mark.parametrize('staged', [False, True])
def test_install_matrix_selects_each_interpreters_staged_wheels(tmp_path, staged):
    """Run the actual shell step; a recording uv stands in for network installs."""
    workflow = Path(__file__).resolve().parents[1] / '.github/workflows/release.yml'
    if not workflow.is_file():
        pytest.skip('Workflow is only present in the source checkout')
    source = workflow.read_text().split('      - name: Install the wheel with published or staged support wheels\n', 1)[1]
    source = source.split('        run: |\n', 1)[1]
    script = textwrap.dedent(re.split(r'^      - ', source, maxsplit=1, flags=re.M)[0])
    release = tmp_path / 'dist/release'
    release.mkdir(parents=True)
    (release / 'meltygui-0.1.1-py3-none-any.whl').touch()
    if staged:
        for name in ('imgui', 'pycuda'):
            directory = tmp_path / 'dist/support' / name
            directory.mkdir(parents=True)
            for abi in ('311', '312', '313'):
                (directory / f'meltygui_{name}-1.0-cp{abi}-cp{abi}-manylinux_2_17_x86_64.whl').touch()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    uv = bin_dir / 'uv'
    uv.write_text('#!/bin/bash\nset -e\nif [ "$1" = venv ]; then\n mkdir -p "$2/bin"\n printf "#!/bin/sh\\nexit 0\\n" > "$2/bin/python"\n chmod +x "$2/bin/python"\nelse\n printf "%s\\n" "$*" >> "$INSTALL_LOG"\nfi\n')
    uv.chmod(0o755)
    result = subprocess.run([shutil.which('bash'), '-e', '-c', 'cd -- "$1"\n' + script, 'release-install-matrix', str(tmp_path)],
                            env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
                                 'RUNNER_TEMP': str(tmp_path), 'INSTALL_LOG': str(tmp_path / 'installs')},
                            text=True, capture_output=True, close_fds=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    installs = (tmp_path / 'installs').read_text().splitlines()
    assert len(installs) == 3
    for version, line in zip(('311', '312', '313'), installs):
        assert '*' not in line
        assert 'meltygui-0.1.1-py3-none-any.whl' in line
        for native in ('imgui', 'pycuda'):
            assert (f'meltygui_{native}-1.0-cp{version}-cp{version}-' in line) == staged


@pytest.mark.parametrize('fault', ['', 'failed', 'fork', 'sha', 'id'])
def test_staged_source_resolution_requires_successful_owned_commit(tmp_path, monkeypatch, fault):
    import io
    import json
    import urllib.request
    workflow = Path(__file__).resolve().parents[1] / '.github/workflows/release.yml'
    if not workflow.is_file():
        pytest.skip('Workflow is only present in the source checkout')
    section = workflow.read_text().split('      - name: Resolve verified support commits\n', 1)[1]
    script = textwrap.dedent(section.split("          python - <<'PYTHON'\n", 1)[1].split('          PYTHON\n', 1)[0])
    run = {'status': 'completed', 'conclusion': 'success', 'head_sha': 'a' * 40,
           'head_repository': {'full_name': 'Attentio-AI/meltygui-imgui'}}
    if fault == 'failed':
        run['conclusion'] = 'failure'
    if fault == 'fork':
        run['head_repository']['full_name'] = 'Example/fork'
    if fault == 'sha':
        run['head_sha'] = 'main'
    monkeypatch.setenv('IMGUI_RUN_ID', '123' if fault != 'id' else '../other')
    monkeypatch.setenv('PYCUDA_RUN_ID', '')
    monkeypatch.setenv('GITHUB_OUTPUT', str(tmp_path / 'output'))
    def response(request, timeout):
        assert request.full_url == 'https://api.github.com/repos/Attentio-AI/meltygui-imgui/actions/runs/123'
        assert not request.has_header('Authorization')
        return io.BytesIO(json.dumps(run).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', response)
    if fault:
        with pytest.raises(SystemExit):
            exec(compile(script, '<workflow-support-source>', 'exec'), {})
        assert not (tmp_path / 'output').exists()
    else:
        exec(compile(script, '<workflow-support-source>', 'exec'), {})
        assert (tmp_path / 'output').read_text() == 'imgui_sha=' + 'a' * 40 + '\n'
