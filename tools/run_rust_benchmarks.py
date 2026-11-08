"""Build and benchmark Rust MeltyGUI, saving a standalone HTML report per run."""
import argparse
from datetime import datetime
import fcntl
import html
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path.home() / 'Documents/MeltyGUI Benchmarks')
    parser.add_argument('--open', action='store_true', help='Open the finished report in the default browser')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Keep simultaneous dock clicks from building or measuring against each other.
    lock = (ROOT / 'native/gui_prototype/.benchmark.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Rust benchmarks are already running.', flush=True)
        return 1
    folder = args.output_dir / datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')
    folder.mkdir()
    print(f'Reports and logs: {folder}', flush=True)
    env = dict(os.environ)
    env['PATH'] = str(Path.home() / '.cargo/bin') + os.pathsep + env.get('PATH', '')
    env['PYTHONUNBUFFERED'] = '1'
    jobs = [
        ('Build release extension', ROOT / 'tools/build_gui_prototype.py', []),
        ('Python versus Rust CPU', ROOT / 'tests/benchmark_gui_prototype.py',
         ['--json-out', str(folder / 'cpu.json')]),
        ('Rust retained OpenGL', ROOT / 'tests/benchmark_gui_retained.py',
         ['--json-out', str(folder / 'retained.json')]),
    ]
    results = []
    for index, (name, script, options) in enumerate(jobs):
        command = [sys.executable, str(script), *options]
        log = folder / f'{index + 1}.log'
        print(f'\n{name}...', flush=True)
        started = time.monotonic()
        try:
            with log.open('w') as stream:
                process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                         env=env, close_fds=False)
            code = process.returncode
        except OSError as error:
            log.write_text(str(error))
            code = 1
        results.append(dict(name=name, command=command, exit_code=code,
                            seconds=time.monotonic() - started, log=log.name))
        print(f'{"Passed" if code == 0 else "FAILED"}: {name} ({results[-1]["seconds"]:.1f}s)', flush=True)
        if index == 0 and code:
            print('Build failed; benchmarks skipped to avoid measuring an old extension.', flush=True)
            break
    success = len(results) == len(jobs) and all(item['exit_code'] == 0 for item in results)
    report = dict(created=datetime.now().astimezone().isoformat(), platform=platform.platform(),
                  python=sys.version, source=str(ROOT), success=success, jobs=results)
    sections = []
    for filename in ('cpu.json', 'retained.json'):
        path = folder / filename
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        report[filename.removesuffix('.json')] = data
        rows = data['results']
        columns = [key for key in rows[0] if key != 'samples_ms'] if rows else []
        table = '<tr>' + ''.join(f'<th>{html.escape(key)}</th>' for key in columns) + '</tr>'
        for row in rows:
            table += '<tr>' + ''.join(f'<td>{html.escape(format(row[key], ".4f") if isinstance(row[key], float) else str(row[key]))}</td>' for key in columns) + '</tr>'
        sections.append(f'<h2>{html.escape(filename)}</h2><p>{html.escape(data.get("renderer", data.get("benchmark", "")))}</p><table>{table}</table>')
    report_path = folder / 'report.html'
    status = 'Completed' if success else 'FAILED - see logs below'
    logs = ''.join(f'<h2>{html.escape(item["name"])}: exit {item["exit_code"]}</h2>'
                   f'<p>{item["seconds"]:.1f} seconds</p><pre>{html.escape((folder / item["log"]).read_text(errors="replace"))}</pre>'
                   for item in results)
    report_path.write_text('<!doctype html><meta charset="utf-8"><title>MeltyGUI Rust benchmarks</title>'
                          '<style>body{font:16px system-ui;margin:40px;background:#fafafa;color:#222}table{border-collapse:collapse}td,th{padding:10px;border:1px solid #ccc;text-align:left}pre{white-space:pre-wrap;background:#eee;padding:16px}</style>'
                          f'<h1>MeltyGUI Rust benchmarks</h1><p>{status}</p><p>{html.escape(report["created"])}<br>{html.escape(report["platform"])}</p>'
                          '<p>CPU comparison: identical bodies, cache off, no GPU timing. Retained OpenGL: completed offscreen work, excluding window presentation and OS events. These are experimental host measurements, not a full application speedup.</p>'
                          + ''.join(sections) + logs)
    (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'\n{status}\n{report_path}', flush=True)
    if args.open:
        opener = shutil.which('xdg-open')
        if opener:
            subprocess.Popen([opener, str(report_path)], close_fds=False)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
