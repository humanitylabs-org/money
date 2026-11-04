"""Package: run `wam package-inline .`, then apply this app's runtime specifics (port, pinned base image,
process limit). Run from the repo root:  python3 tools/package.py"""
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
subprocess.run(['wam', 'package-inline', str(root)], check=True, stdout=subprocess.DEVNULL)
p = root / 'runtipi' / 'docker-compose.yml'
t = p.read_text()
swaps = [
    ('    image: python:3.12-alpine\n',
     '    image: python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4\n    pids_limit: 64\n'),
    ("      PORT: '8799'\n", "      PORT: '8809'\n      BIND: 0.0.0.0\n      PYTHONDONTWRITEBYTECODE: '1'\n"),
    ('      internal_port: 8799\n', '      internal_port: 8809\n'),
    ('      DATA_DIR: /data\n', '      DATA_DIR: /data/private\n'
                            "      # Money's own alerts (a bank that needs you, large charges) go through the Notifications app.\n"
                            '      WIZARD_NOTIFY_URL: ${WIZARD_NOTIFY_URL:-http://notifications:8080/mcp}\n'
                            '      MONEY_ALERT_LARGE: ${MONEY_ALERT_LARGE:-500}\n'),
]
for old, new in swaps:
    if t.count(old) != 1:
        raise SystemExit(f'package template changed; expected one {old.strip()!r}, found {t.count(old)}')
    t = t.replace(old, new)
# Linux caps one command-line argument at 128 KiB: ship the inlined files zlib-compressed.
import base64, json, zlib
head = "files=json.loads(base64.b64decode('"
i = t.index(head) + len(head); j = t.index("'))", i)
packed = base64.b64encode(zlib.compress(base64.b64decode(t[i:j]), 9)).decode()
t = t[:i - len(head)] + "import zlib\\nfiles=json.loads(zlib.decompress(base64.b64decode('" + packed + "')))" + t[j + 3:]
assert max(len(l) for l in t.splitlines()) < 120000, 'inline payload still too large'
assert '/data/private' in t and '@sha256:' in t and '8799' not in t
p.write_text(t)
print('packaged', p.relative_to(root))
