"""A small Actual Budget sync client for Finances (money#38). Stdlib only.

Actual Budget keeps a budget as a file on its server plus a log of CRDT messages (table, row, column, value, timestamp).
Its own apps download the file, then send and receive messages through POST /sync/sync. This client does the same, so
Finances can keep its data in a stock Actual Budget server (no fork, no extra packages):

- signs in with a password it made itself when it first set the server up (kept in Finances' private data),
- creates one budget file named "Finances" from the server's own default-db.sqlite (the same start a new budget gets in
  Actual's app; Actual runs its migrations on that file when someone opens it),
- keeps a mirror of every cell it has seen (snapshot of the downloaded file + every message since), so it can read the
  budget without Actual's app, and tells which cells another Actual client (the Actual web app) changed,
- sends its own changes as messages with hybrid logical clock timestamps, like every Actual client.

Encrypted budgets are not supported (Finances makes its budget unencrypted; the server is reachable only inside the app).
"""
import datetime as dt
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

ZERO = '1970-01-01T00:00:00.000Z-0000-0000000000000000'
TABLES = ('accounts', 'banks', 'payees', 'payee_mapping', 'categories', 'category_groups', 'category_mapping', 'rules',
          'transactions')
OVERLAP_MS = 10 * 60 * 1000   # ask again for the last minutes each time: another device's clock may run a bit behind
BATCH = 1500                   # messages per sync request (Actual's default limit is 20 MB per request)


class ActualError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.message, self.code = message, code


# ---------------- protobuf (sync.proto: Message, MessageEnvelope, SyncRequest, SyncResponse) ----------------

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _len_field(num, payload):
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def _str_field(num, s):
    return _len_field(num, s.encode()) if s else b''


def encode_message(dataset, row, column, value):
    return _str_field(1, dataset) + _str_field(2, row) + _str_field(3, column) + _str_field(4, value)


def encode_request(messages, file_id, group_id, since, key_id=''):
    """messages: [(timestamp, dataset, row, column, serialized value)]."""
    out = bytearray()
    for ts, ds, row, col, val in messages:
        env = _str_field(1, ts) + _len_field(3, encode_message(ds, row, col, val))
        out += _len_field(1, env)
    out += _str_field(2, file_id) + _str_field(3, group_id) + _str_field(5, key_id) + _str_field(6, since)
    return bytes(out)


def _parse(buf):
    fields, i = {}, 0
    while i < len(buf):
        key, shift = 0, 0
        while True:
            b = buf[i]; i += 1
            key |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, shift = 0, 0
            while True:
                b = buf[i]; i += 1
                v |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
        elif wire == 2:
            n, shift = 0, 0
            while True:
                b = buf[i]; i += 1
                n |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            v = bytes(buf[i:i + n]); i += n
        elif wire == 5:
            v = bytes(buf[i:i + 4]); i += 4
        elif wire == 1:
            v = bytes(buf[i:i + 8]); i += 8
        else:
            raise ActualError('Actual Budget sent a sync answer Finances cannot read.')
        fields.setdefault(num, []).append(v)
    return fields


def decode_response(buf):
    f = _parse(buf)
    msgs = []
    for env in f.get(1, []):
        e = _parse(env)
        if e.get(2, [0])[0]:
            raise ActualError('This Actual budget is encrypted; Finances only works with its own unencrypted budget.')
        m = _parse(e.get(3, [b''])[0])
        get = lambda k: m.get(k, [b''])[0].decode()
        msgs.append((e.get(1, [b''])[0].decode(), get(1), get(2), get(3), get(4)))
    merkle = f.get(2, [b'{}'])[0].decode() or '{}'
    return msgs, merkle


def decode_request(buf):
    """For tests and fakes: SyncRequest -> (messages, file_id, group_id, key_id, since)."""
    f = _parse(buf)
    msgs = []
    for env in f.get(1, []):
        e = _parse(env)
        m = _parse(e.get(3, [b''])[0])
        get = lambda k: m.get(k, [b''])[0].decode()
        msgs.append((e.get(1, [b''])[0].decode(), get(1), get(2), get(3), get(4)))
    s = lambda k: f.get(k, [b''])[0].decode()
    return msgs, s(2), s(3), s(5), s(6)


def encode_response(messages, merkle='{}'):
    out = bytearray()
    for ts, ds, row, col, val in messages:
        out += _len_field(1, _str_field(1, ts) + _len_field(3, encode_message(ds, row, col, val)))
    return bytes(out + _str_field(2, merkle))


# ---------------- values and timestamps ----------------

def ser(v):
    if v is None:
        return '0:'
    if isinstance(v, bool):
        return 'N:' + ('1' if v else '0')
    if isinstance(v, (int, float)):
        return 'N:' + (str(int(v)) if float(v).is_integer() else repr(float(v)))
    return 'S:' + str(v)


def de(s):
    if not s or len(s) < 2 or s[1] != ':':
        return None
    if s[0] == 'N':
        f = float(s[2:])
        return int(f) if f.is_integer() else f
    if s[0] == 'S':
        return s[2:]
    return None


def ts_string(millis, counter, node):
    d = dt.datetime.fromtimestamp(millis / 1000, dt.timezone.utc)
    return f"{d.strftime('%Y-%m-%dT%H:%M:%S')}.{millis % 1000:03d}Z-{counter:04X}-{node}"


def ts_parse(ts):
    p = ts.split('-')
    if len(p) != 5:
        return None
    try:
        d = dt.datetime.strptime('-'.join(p[:3]), '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp() * 1000), int(p[3], 16), p[4]
    except ValueError:
        return None


def uid(*parts):
    """A stable id for one Finances thing (so a retried copy never makes a second row)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'wizard-finances:' + ':'.join(str(x) for x in parts)))


# ---------------- the client ----------------

class Client:
    def __init__(self, base, data_dir, name='Finances', timeout=30):
        self.base = base.rstrip('/')
        self.dir = Path(data_dir)
        self.name = name
        self.timeout = timeout
        self.lock = threading.RLock()
        self._state = None

    # state: password, token, file id, group id, node, clock, since (kept 0600 in Finances' private data)
    @property
    def state_file(self):
        return self.dir / 'actual.json'

    def state(self):
        if self._state is None:
            try:
                self._state = json.loads(self.state_file.read_text())
            except (OSError, ValueError):
                self._state = {}
        return self._state

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name('.actual.json.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(self._state))
        os.replace(tmp, self.state_file)

    def mirror(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / 'actual.sqlite3'
        c = sqlite3.connect(p, timeout=15)
        c.row_factory = sqlite3.Row
        c.executescript('''
          CREATE TABLE IF NOT EXISTS cells (ds TEXT, row TEXT, col TEXT, val TEXT, ts TEXT, PRIMARY KEY (ds, row, col));
          CREATE INDEX IF NOT EXISTS cells_ds ON cells(ds, row);
          CREATE TABLE IF NOT EXISTS link (key TEXT PRIMARY KEY, aid TEXT, sig TEXT);
        ''')
        os.chmod(p, 0o600)
        return c

    # ---- http ----
    def _http(self, method, path, body=None, headers=None, kind='application/json', raw=False, auth=True):
        h = {'Accept': '*/*', **(headers or {})}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h['Content-Type'] = kind
        if auth and self.state().get('token'):
            h['X-ACTUAL-TOKEN'] = self.state()['token']
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors='replace')[:300]
            try:
                reason = json.loads(text).get('reason') or text
            except ValueError:
                reason = text
            raise ActualError(reason or f'Actual Budget answered {e.code}.', e.code) from None
        except OSError:
            raise ActualError("Actual Budget isn't answering yet.") from None
        if raw:
            return out
        try:
            d = json.loads(out or b'{}')
        except ValueError:
            raise ActualError('Actual Budget sent something unreadable.') from None
        if isinstance(d, dict) and d.get('status') == 'error':
            raise ActualError(str(d.get('reason') or 'Actual Budget refused that.'))
        return d

    def _authed(self, *a, **k):
        try:
            return self._http(*a, **k)
        except ActualError as e:
            if e.code != 401 and 'unauthorized' not in e.message and 'token-expired' not in e.message:
                raise
        self.login()
        return self._http(*a, **k)

    # ---- sign in, budget file ----
    def login(self):
        st = self.state()
        boot = self._http('GET', '/account/needs-bootstrap', auth=False).get('data') or {}
        if not boot.get('bootstrapped'):
            st['password'] = st.get('password') or secrets.token_urlsafe(24)
            self.save()
            d = self._http('POST', '/account/bootstrap', {'password': st['password']}, auth=False).get('data') or {}
        else:
            if not st.get('password'):
                raise ActualError("Actual Budget here already has a password that Finances didn't set, so Finances can't sign in.")
            d = self._http('POST', '/account/login', {'loginMethod': 'password', 'password': st['password']}, auth=False).get('data') or {}
        if not d.get('token'):
            raise ActualError("Actual Budget didn't let Finances sign in.")
        st['token'] = d['token']
        self.save()

    def ensure(self):
        """Signed in and the budget file exists and is current. Returns True when the file was just made."""
        with self.lock:
            st = self.state()
            if not st.get('node'):
                st['node'] = secrets.token_hex(8)
                self.save()
            if not st.get('token'):
                self.login()
            files = self._authed('GET', '/sync/list-user-files').get('data') or []
            f = next((x for x in files if x.get('fileId') == st.get('file_id') and not x.get('deleted')), None)
            if not f:
                self.create()
                return True
            if f.get('encryptKeyId'):
                raise ActualError('This Actual budget is encrypted; Finances only works with its own unencrypted budget.')
            if f.get('groupId') != st.get('group_id'):
                self.reload()   # someone reset sync in Actual: start again from the file
            return False

    def create(self):
        st = self.state()
        default = self._http('GET', '/data/default-db.sqlite', raw=True, auth=False)
        file_id = str(uuid.uuid4())
        meta = {'id': 'Finances-' + file_id[:7], 'budgetName': self.name, 'cloudFileId': file_id, 'groupId': None,
                'resetClock': True, 'lastUploaded': dt.date.today().isoformat()}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('db.sqlite', default)
            z.writestr('metadata.json', json.dumps(meta))
        d = self._authed('POST', '/sync/upload-user-file', buf.getvalue(), kind='application/encrypted-file',
                         headers={'X-ACTUAL-NAME': quote(self.name), 'X-ACTUAL-FILE-ID': file_id, 'X-ACTUAL-FORMAT': '2'})
        st.update(file_id=file_id, group_id=d.get('groupId'), created=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        self.save()
        self.reload()

    def reload(self):
        """Mirror := the budget file's tables, then every message since the file's newest one."""
        st = self.state()
        hdr = {'X-ACTUAL-FILE-ID': st['file_id']}
        info = self._authed('GET', '/sync/get-user-file-info', headers=hdr).get('data') or {}
        blob = self._authed('GET', '/sync/download-user-file', headers=hdr, raw=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            dbbytes = z.read('db.sqlite')
        tmp = self.dir / '.actual-download.sqlite'
        tmp.write_bytes(dbbytes)
        try:
            src = sqlite3.connect(tmp)
            have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            m = self.mirror()
            try:
                with m:
                    m.execute('DELETE FROM cells')
                    for t in TABLES:
                        if t not in have:
                            continue
                        cur = src.execute(f'SELECT * FROM "{t}"')
                        cols = [d[0] for d in cur.description]
                        for r in cur:
                            rid = r[cols.index('id')]
                            m.executemany('INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?)',
                                          [(t, rid, k, ser(v), '') for k, v in zip(cols, r) if k != 'id'])
                    since = ZERO
                    if 'messages_crdt' in have:
                        since = src.execute('SELECT MAX(timestamp) FROM messages_crdt').fetchone()[0] or ZERO
            finally:
                m.close()
            src.close()
        finally:
            tmp.unlink(missing_ok=True)
        st.update(group_id=info.get('groupId'), since=since)
        self.save()

    # ---- clock ----
    def next_ts(self):
        st = self.state()
        cm, cc = st.get('clock') or [0, 0]
        now = int(time.time() * 1000)
        if now > cm:
            cm, cc = now, 0
        else:
            cc += 1
            if cc > 0xFFFF:
                cm, cc = cm + 1, 0
        st['clock'] = [cm, cc]
        return ts_string(cm, cc, st['node'])

    def _recv(self, ts):
        p = ts_parse(ts)
        st = self.state()
        if p and p[:2] > tuple(st.get('clock') or [0, 0]):
            st['clock'] = [p[0], p[1]]

    # ---- sync ----
    def stamp(self, changes):
        """[(dataset, row, column, python value)] -> messages with fresh timestamps."""
        return [(self.next_ts(), ds, row, col, ser(v)) for ds, row, col, v in changes]

    def sync(self, messages=()):
        """Send messages (may be none), take in everyone's newer ones. Returns the cells other clients changed:
        {(dataset, row): {columns}}."""
        with self.lock:
            st = self.state()
            foreign = {}
            messages = list(messages)
            sent_any = False
            for i in range(0, max(len(messages), 1), BATCH):
                part = messages[i:i + BATCH]
                since = st.get('since') or ZERO
                p = ts_parse(since)
                ask = ts_string(max(0, p[0] - OVERLAP_MS), 0, '0' * 16) if p and p[0] > OVERLAP_MS else ZERO
                body = encode_request(part, st['file_id'], st.get('group_id') or '', ask)
                try:
                    raw = self._authed('POST', '/sync/sync', body, kind='application/actual-sync', raw=True)
                except ActualError as e:
                    if e.code == 400 and 'file-has-reset' in e.message:
                        self.reload()
                        raise ActualError('Actual Budget was reset; Finances reloaded it and copies again next time.') from None
                    raise
                got, _ = decode_response(raw)
                sent_any = sent_any or bool(part)
                m = self.mirror()
                try:
                    with m:
                        for ts, ds, row, col, val in list(got) + list(part):
                            cur = m.execute('SELECT ts FROM cells WHERE ds=? AND row=? AND col=?', (ds, row, col)).fetchone()
                            if cur is None or ts > (cur[0] or ''):
                                m.execute('INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?)', (ds, row, col, val, ts))
                                if not ts.endswith('-' + st['node']):
                                    foreign.setdefault((ds, row), set()).add(col)
                finally:
                    m.close()
                newest = max([since] + [g[0] for g in got])
                for g in got:
                    self._recv(g[0])
                st['since'] = newest
                st['last_sync'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                self.save()
            return foreign

    # ---- reading the mirror ----
    @staticmethod
    def rows(m, ds):
        out = {}
        for r in m.execute('SELECT row, col, val FROM cells WHERE ds=?', (ds,)):
            out.setdefault(r['row'], {})[r['col']] = de(r['val'])
        return out

    @staticmethod
    def live(rows):
        return {k: v for k, v in rows.items() if not v.get('tombstone')}

    # ---- bank sync through Actual's SimpleFIN ----
    def post(self, path, body=None, file_header=True):
        hdr = {}
        return self._authed('POST', path, body if body is not None else {}, headers=hdr)

    def delete_secret(self, name):
        try:
            self._authed('DELETE', '/secret/' + name)
        except ActualError as e:
            if e.code != 404:
                raise
