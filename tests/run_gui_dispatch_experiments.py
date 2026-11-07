"""Run ordered, isolated experiment controls on an already reserved desktop."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native-path', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=200)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).with_name('gui_dispatch_experiment.py').resolve()
    order = ['baseline', 'empty-layout', 'native-bridge', 'combined',
             'combined', 'native-bridge', 'empty-layout', 'baseline']
    report = []
    for number, experiment in enumerate(order):
        output = args.output / f'{number}-{experiment}.json'
        command = [sys.executable, str(runner), '--experiment', experiment]
        if experiment in ('native-bridge', 'combined'):
            command += ['--native-path', str(args.native_path.resolve())]
        command += ['--target', 'benchmark_gui_overhead.py', '--', '--graphics',
                    '--auto-cache', 'on', '--trees', '5:4', '--work', '0',
                    '--visibility', 'visible', '--warm', '24', '--frames', str(args.frames),
                    '--json-out', str(output)]
        with output.with_suffix('.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, close_fds=False, check=True)
        data = json.loads(output.read_text())
        rows = data['cases'][0]['results']
        summary = {row['variant']: row['cpu'] for row in rows}
        report.append(dict(experiment=experiment, file=str(output), cpu=summary))
        (args.output / 'summary.json').write_text(json.dumps(report, indent=2))
        print(number, experiment, summary['gui_redraw'], flush=True)


if __name__ == '__main__':
    main()
