"""Find already-verified release artifacts for the exact commit being published."""
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request


def prepared_run(read, repository, current_id, sha, required):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Publishing requires an exact source commit")
    base = f"/repos/{repository}/actions"
    current = read(f"{base}/runs/{current_id}")
    workflow = current['workflow_id']
    page = 1
    while True:
        query = urllib.parse.urlencode(dict(head_sha=sha, status='success', per_page=100, page=page))
        runs = read(f"{base}/workflows/{workflow}/runs?{query}")['workflow_runs']
        for run in runs:
            if (run['id'] == current_id or run.get('head_sha') != sha
                    or run.get('workflow_id') != workflow
                    or run.get('status') != 'completed' or run.get('conclusion') != 'success'
                    or run.get('event') not in ('push', 'workflow_dispatch')
                    or run.get('head_repository', {}).get('full_name', '').lower() != repository.lower()):
                continue
            artifacts = read(f"{base}/runs/{run['id']}/artifacts?per_page=100")['artifacts']
            available = {artifact['name'] for artifact in artifacts if not artifact.get('expired')}
            if required <= available:
                return run['id']
        if len(runs) < 100:
            break
        page += 1
    raise ValueError("No verified release artifacts remain for this commit. Run preparation first; publishing does not rebuild or retest.")


def main():
    def read(path):
        request = urllib.request.Request(os.environ['GITHUB_API_URL'] + path, headers={
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json', 'User-Agent': 'prepared-release'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    run_id = prepared_run(read, os.environ['GITHUB_REPOSITORY'], int(os.environ['GITHUB_RUN_ID']),
                          os.environ['SOURCE_SHA'], set(sys.argv[1:]))
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'run_id={run_id}\n')
    print(f'Publishing verified artifacts from run {run_id}; no rebuild or second test pass.')


if __name__ == '__main__':
    main()
