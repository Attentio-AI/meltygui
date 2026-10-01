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
