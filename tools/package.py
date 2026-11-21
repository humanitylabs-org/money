"""Check the run-from-source recipe (wizard.md `runtime.package: source`). Run from the repo root:
python3 tools/package.py

Finances used to inline every file into one `python -c` argument (`wam package-inline`). That argument grew to
110 KB, close to Linux's 128 KiB per-argument limit (past it the container crash-loops with "argument list too
long"). Now the App Manager copies the pinned source to the box and mounts it read-only at /app
(${APP_SOURCE_DIR}), so the start command stays the same size however big the app gets. Nothing is generated:
this script only verifies the compose still has that shape and this app's runtime specifics."""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent


def problems(root=root):
    out = []
    md = (root / 'wizard.md').read_text()
    runtime = re.search(r'^runtime:\n((?:  .*\n)+)', md, re.M)
    if not runtime or not re.search(r'^  package: source$', runtime.group(1), re.M):
        out.append('wizard.md runtime: needs `package: source`')
    t = (root / 'runtipi' / 'docker-compose.yml').read_text()
    need = [
        '    image: python:3.12-alpine@sha256:',
        '    pids_limit: 64\n',
        '    user: 1000:1000\n',
        '    read_only: true\n',
        '    working_dir: /app\n',
        '    - ${APP_SOURCE_DIR}:/app:ro\n',
        '    - ${APP_DATA_DIR}/data:/data\n',
        '    command: [python, /app/app/server.py]\n',
        "      PORT: '8809'\n",
        '      DATA_DIR: /data/private\n',
        '      internal_port: 8809\n',
    ]
    out += [f'docker-compose.yml is missing {n.strip()!r}' for n in need if n not in t]
    if 'b64decode' in t or 'runpy' in t or ' -c\n' in t:
        out.append('docker-compose.yml still inlines the app; run it from ${APP_SOURCE_DIR} instead')
    if 'build:' in t:
        out.append('source apps build nothing; remove build:')
    longest = max(len(line) for line in t.splitlines())
    if longest > 4096:
        out.append(f'docker-compose.yml has a {longest}-character line; the start command must stay small')
    for name in ('app/server.py', 'web/index.html', 'web/app.js'):
        if not (root / name).is_file():
            out.append(f'{name} is missing from the source the box mounts')
    return out


if __name__ == '__main__':
    bad = problems()
    for b in bad:
        print('package:', b, file=sys.stderr)
    if bad:
        sys.exit(1)
    print('package recipe ok: runtipi/docker-compose.yml runs app/server.py from ${APP_SOURCE_DIR}')
