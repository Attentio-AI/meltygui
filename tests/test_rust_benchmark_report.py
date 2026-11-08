"""The launcher reports failures without silently measuring a stale build."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('failed_job,expected_jobs', [(0, 1), (1, 3), (None, 3)])
def test_report_records_failures_and_skips_stale_build(tmp_path, monkeypatch, failed_job, expected_jobs):
    script = Path(__file__).resolve().parents[1] / 'tools/run_rust_benchmarks.py'
    spec = importlib.util.spec_from_file_location('benchmark_report', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    (tmp_path / 'native/gui_prototype').mkdir(parents=True)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner.platform, 'platform', lambda: 'Test platform')
    monkeypatch.setattr(runner.sys, 'argv', [str(script), '--output-dir', str(tmp_path / 'reports')])
    calls = []

    def run(command, *, stdout, stderr, env, close_fds):
        index = len(calls)
        calls.append(command)
        stdout.write('<diagnostic>\n')
        if index and index != failed_job:
            Path(command[-1]).write_text(json.dumps({'results': [{'median_ms': 1.25, 'samples_ms': [1.25]}]}))
        return SimpleNamespace(returncode=1 if index == failed_job else 0)

    monkeypatch.setattr(runner.subprocess, 'run', run)
    assert runner.main() == (0 if failed_job is None else 1)
    assert len(calls) == expected_jobs
    report_path = next((tmp_path / 'reports').glob('*/report.json'))
    report = json.loads(report_path.read_text())
    assert report['success'] == (failed_job is None)
    html = report_path.with_suffix('.html').read_text()
    assert '&lt;diagnostic&gt;' in html
    assert '<diagnostic>' not in html
    if failed_job is not None:
        assert 'FAILED' in html
        assert report['jobs'][failed_job]['exit_code'] == 1
