"""Publishing reuses successful exact-source artifacts without running tests again."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepared_release', ROOT / 'tools/prepared_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SHA = 'a' * 40
REPO = 'Attentio-AI/meltygui'


def resolve(run_changes=None, artifacts=None, required=None):
    run = dict(id=123, workflow_id=10, head_sha=SHA, status='completed', conclusion='success',
               event='workflow_dispatch', head_repository={'full_name': REPO})
    run.update(run_changes or {})
    def read(path):
        if path.endswith('/runs/456'):
            return {'workflow_id': 10}
        if '/workflows/10/runs?' in path:
            assert 'head_sha=' + SHA in path
            return {'workflow_runs': [run]}
        if path.endswith('/runs/123/artifacts?per_page=100'):
            return {'artifacts': artifacts if artifacts is not None else [{'name': 'release', 'expired': False}]}
        raise AssertionError(path)
    return module.prepared_run(read, REPO, 456, SHA, required or {'release'})


def test_reuses_successful_exact_source():
    assert resolve() == 123


@pytest.mark.parametrize('changes', [
    {'head_sha': 'b' * 40}, {'conclusion': 'failure'}, {'status': 'in_progress'},
    {'workflow_id': 99}, {'event': 'pull_request'}, {'id': 456},
    {'head_repository': {'full_name': 'someone/fork'}},
])
def test_rejects_unverified_source(changes):
    with pytest.raises(ValueError, match='Run preparation first'):
        resolve(changes)


@pytest.mark.parametrize('artifacts', [[], [{'name': 'release', 'expired': True}], [{'name': 'other'}]])
def test_missing_or_expired_artifacts_require_preparation(artifacts):
    with pytest.raises(ValueError, match='Run preparation first'):
        resolve(artifacts=artifacts)


def test_windows_requires_both_artifacts():
    with pytest.raises(ValueError):
        resolve(required={'release', 'release-windows'})
    assert resolve(artifacts=[{'name': 'release'}, {'name': 'release-windows'}],
                   required={'release', 'release-windows'}) == 123


def test_rejects_unpinned_source_before_api_call():
    with pytest.raises(ValueError, match='exact source commit'):
        module.prepared_run(lambda path: pytest.fail(path), REPO, 456, 'main', {'release'})


def test_pagination_finds_older_preparation():
    def read(path):
        if path.endswith('/runs/456'):
            return {'workflow_id': 10}
        if '/workflows/10/runs?' in path:
            count = 100 if path.endswith('page=1') else 1
            return {'workflow_runs': [dict(id=123 if count == 1 else 456, workflow_id=10,
                head_sha=SHA, status='completed', conclusion='success', event='push',
                head_repository={'full_name': REPO})] * count}
        return {'artifacts': [{'name': 'release'}]}
    assert module.prepared_run(read, REPO, 456, SHA, {'release'}) == 123


def test_publish_path_uses_original_artifacts_and_skips_build_steps():
    import re
    source = (ROOT / '.github/workflows/release.yml').read_text()
    job = 'verify' if ROOT.name == 'meltygui' else 'build'
    build = source.split(f'  {job}:\n', 1)[1].split('\n  windows:', 1)[0].split('\n  publish:', 1)[0]
    steps = re.split(r'(?=^      - )', build, flags=re.M)[1:]
    exempt = ('      - uses: actions/checkout@v4', '      - uses: astral-sh/setup-uv@v6',
              '      - name: Reuse prepared release', '      - name: Check confirmed release version')
    for step in steps:
        if step.startswith(exempt) and 'path: .release-sources' not in step:
            continue
        assert "if: steps.prepared.outputs.run_id == ''" in step, step
    publish = source.split('\n  publish:', 1)[1]
    assert f'run-id: ${{{{ needs.{job}.outputs.prepared_run }}}}' in publish
    assert 'github-token: ${{ github.token }}' in publish
    assert 'actions: read' in publish
    if '\n  windows:' in source:
        windows = source.split('\n  windows:', 1)[1].split('\n  publish:', 1)[0]
        for step in re.split(r'(?=^      - )', windows, flags=re.M)[1:]:
            if 'name: Reuse prepared Windows artifacts' not in step:
                assert "if: needs.build.outputs.prepared_run == ''" in step
        assert 'tools/prepared_release.py release release-windows' in build
