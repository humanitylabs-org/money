"""Finances kept in Actual Budget (money#38, on for everyone), against a fake Actual server that speaks the same
sync protocol (protobuf messages, budget file upload/download, SimpleFIN endpoints). The real server image was tried
by hand with Actual's own @actual-app/api reading the budget back; see the PR."""
import datetime as dt
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_alerts import load

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import actual as A  # noqa: E402

OTHER = 'abcdefabcdefabcd'   # another Actual client (the Actual web app)


def default_db():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'db.sqlite'
        c = sqlite3.connect(p)
        c.executescript('''
          CREATE TABLE category_groups (id TEXT PRIMARY KEY, name TEXT, is_income INTEGER, sort_order REAL, tombstone INTEGER);
          CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, is_income INTEGER, cat_group TEXT, sort_order REAL, tombstone INTEGER);
          CREATE TABLE category_mapping (id TEXT PRIMARY KEY, transferId TEXT);
          CREATE TABLE messages_crdt (id INTEGER PRIMARY KEY, timestamp TEXT, dataset TEXT, row TEXT, column TEXT, value BLOB);
          INSERT INTO category_groups VALUES ('g-usual', 'Usual Expenses', 0, 1, 0), ('g-inc', 'Income', 1, 2, 0);
          INSERT INTO categories VALUES ('c-food', 'Food', 0, 'g-usual', 1, 0), ('c-inc', 'Income', 1, 'g-inc', 1, 0),
                                        ('c-start', 'Starting Balances', 1, 'g-inc', 2, 0);
          INSERT INTO category_mapping VALUES ('c-food', 'c-food'), ('c-inc', 'c-inc'), ('c-start', 'c-start');
        ''')
        c.commit(); c.close()
        return p.read_bytes()


class FakeActual:
    def __init__(self):
        self.password, self.token, self.files, self.messages, self.secrets, self.calls = None, 'tok-1', {}, [], {}, []
        self.sf_accounts = [{'id': 'ACT-1', 'name': 'Sapphire 4321', 'currency': 'USD', 'balance': '-200.00', 'balance-date': int(time.time()),
                             'org': {'name': 'Chase', 'domain': 'chase.com'}}]
        self.sf_tx = {'ACT-1': [{'transactionId': 'T1', 'booked': True, 'date': '2026-11-01', 'transactedDate': '2026-11-01',
                                 'payeeName': 'Whole Foods', 'notes': 'WHOLE FOODS #123', 'transactionAmount': {'amount': '-86.63'}},
                                {'transactionId': 'T9', 'booked': False, 'date': '2026-11-02', 'payeeName': 'Uber', 'notes': 'UBER TRIP',
                                 'transactionAmount': {'amount': '-23.10'}}]}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def out(self, code, body, kind='application/json'):
                b = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code); self.send_header('Content-Type', kind); self.send_header('Content-Length', str(len(b)))
                self.end_headers(); self.wfile.write(b)

            def authed(self):
                if self.headers.get('X-ACTUAL-TOKEN') != fake.token:
                    self.out(401, {'status': 'error', 'reason': 'unauthorized'}); return False
                return True

            def body(self):
                return self.rfile.read(int(self.headers.get('Content-Length') or 0))

            def do_GET(self):
                fake.calls.append(('GET', self.path))
                if self.path == '/account/needs-bootstrap':
                    return self.out(200, {'status': 'ok', 'data': {'bootstrapped': fake.password is not None}})
                if self.path == '/data/default-db.sqlite':
                    return self.out(200, default_db(), 'application/octet-stream')
                if not self.authed():
                    return
                fid = self.headers.get('X-ACTUAL-FILE-ID')
                if self.path == '/sync/list-user-files':
                    return self.out(200, {'status': 'ok', 'data': [{'fileId': k, 'groupId': v['group'], 'deleted': 0, 'encryptKeyId': None}
                                                                   for k, v in fake.files.items()]})
                if self.path == '/sync/get-user-file-info':
                    return self.out(200, {'status': 'ok', 'data': {'fileId': fid, 'groupId': fake.files[fid]['group']}})
                if self.path == '/sync/download-user-file':
                    return self.out(200, fake.files[fid]['blob'], 'application/octet-stream')
                self.out(404, {'status': 'error', 'reason': 'not-found'})

            def do_DELETE(self):
                fake.calls.append(('DELETE', self.path))
                if self.authed():
                    fake.secrets.pop(self.path.rsplit('/', 1)[1], None)
                    self.out(200, {'status': 'ok'})

            def do_POST(self):
                fake.calls.append(('POST', self.path))
                raw = self.body()
                if self.path == '/account/bootstrap':
                    fake.password = json.loads(raw)['password']
                    return self.out(200, {'status': 'ok', 'data': {'token': fake.token}})
                if self.path == '/account/login':
                    ok = json.loads(raw).get('password') == fake.password
                    return self.out(200 if ok else 400, {'status': 'ok', 'data': {'token': fake.token}} if ok else {'status': 'error', 'reason': 'invalid-password'})
                if not self.authed():
                    return
                if self.path == '/sync/upload-user-file':
                    fid = self.headers['X-ACTUAL-FILE-ID']
                    fake.files[fid] = {'blob': raw, 'group': 'group-1', 'name': self.headers['X-ACTUAL-NAME']}
                    return self.out(200, {'status': 'ok', 'groupId': 'group-1'})
                if self.path == '/sync/sync':
                    msgs, fid, gid, _, since = A.decode_request(raw)
                    if fid not in fake.files or gid != fake.files[fid]['group']:
                        return self.out(400, b'file-has-reset', 'text/plain')
                    new = [m for m in sorted(fake.messages) if m[0] > since]
                    have = {m[0] for m in fake.messages}
                    fake.messages += [m for m in msgs if m[0] not in have]
                    return self.out(200, A.encode_response(new), 'application/actual-sync')
                if self.path == '/secret':
                    d = json.loads(raw); fake.secrets[d['name']] = d['value']
                    return self.out(200, {'status': 'ok'})
                if self.path == '/simplefin/accounts':
                    if not fake.secrets.get('simplefin_accessKey', '').startswith('https://'):
                        return self.out(200, {'status': 'ok', 'data': {'error_code': 'INVALID_ACCESS_TOKEN', 'reason': 'Invalid SimpleFIN access token.'}})
                    return self.out(200, {'status': 'ok', 'data': {'accounts': fake.sf_accounts}})
                if self.path == '/simplefin/transactions':
                    d = json.loads(raw)
                    return self.out(200, {'status': 'ok', 'data': {a: {'transactions': {'all': fake.sf_tx.get(a, [])}} for a in d['accountId']}})
                self.out(404, {'status': 'error', 'reason': 'not-found'})

        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.srv.server_address[1]}'

    def state(self):
        """The budget as an Actual app would see it: the uploaded file's rows, then every message in order."""
        out = {}
        blob = next(iter(self.files.values()))['blob']
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'db.sqlite'
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                p.write_bytes(z.read('db.sqlite'))
            c = sqlite3.connect(p)
            for t in ('categories', 'category_groups'):
                cur = c.execute(f'SELECT * FROM {t}')
                cols = [x[0] for x in cur.description]
                for r in cur:
                    out.setdefault(t, {})[r[0]] = dict(zip(cols[1:], r[1:]))
            c.close()
        for ts, ds, row, col, val in sorted(self.messages):
            out.setdefault(ds, {}).setdefault(row, {})[col] = A.de(val)
        return {ds: {k: v for k, v in rows.items() if not v.get('tombstone')} for ds, rows in out.items()}

    def edit(self, ds, row, col, value):
        ts = A.ts_string(int(time.time() * 1000) + 5000 + len(self.messages), 0, OTHER)
        self.messages.append((ts, ds, row, col, A.ser(value)))

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class ActualBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeActual()
        self.tmp = tempfile.TemporaryDirectory()
        import os
        os.environ['MONEY_ACTUAL_URL'] = self.fake.url
        self.m = load(self.tmp.name)
        self.m._SRC.update(at=time.time() + 86400, list=[{'id': 'wizard-simplefin', 'name': 'SimpleFIN', 'installed': True,
                                                          'connected': True, 'banks': 1, 'last_sync': None}])
        m, today = self.m, dt.date.today()
        d = lambda n: (today - dt.timedelta(days=n)).isoformat()
        c = m.db()
        with c:
            m._set(c, 'source', 'wizard-simplefin')
            c.execute("INSERT INTO accounts VALUES ('wizard-simplefin','ACT-1','Chase','Sapphire 4321','4321','credit','USD',-200,?)", (d(0),))
            for tid, day, amt, desc in (('T1', 1, -86.63, 'WHOLE FOODS #123'), ('T2', 2, -5.25, 'STARBUCKS 555'),
                                        ('T3', 3, -120.0, 'COSTCO WHSE'), ('T4', 4, -9.99, 'DUPLICATE')):
                c.execute("INSERT INTO tx VALUES ('wizard-simplefin',?,?,?,?,?,NULL,NULL,'posted',?)", (tid, 'ACT-1', d(day), amt, desc, m._now()))
            c.execute("INSERT INTO rules(match,category,merchant,created,min_abs) VALUES ('starbucks','Coffee','Starbucks',?,2)", (m._now(),))
        c.close()
        m.update({'source': 'wizard-simplefin', 'id': 'T1', 'category': 'Groceries', 'note': 'weekly shop', 'reviewed': True})
        m.update({'source': 'wizard-simplefin', 'id': 'T3', 'splits': [{'category': 'Groceries', 'amount': 80}, {'category': 'Household', 'amount': 40}]})
        m.update({'source': 'wizard-simplefin', 'id': 'T4', 'hidden': True})

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def tx_by_import(self, st):
        return {t.get('financial_id'): t for t in st.get('transactions', {}).values() if t.get('financial_id')}

    def test_on_for_everyone_with_no_switch(self):
        self.assertNotIn('actual-budget', [x['id'] for x in self.m.switches_doc()['switches']])
        self.assertIn('actual', self.m.overview())
        self.assertIsNotNone(self.m.actual_copy())
        self.assertTrue(self.fake.calls)

    def test_stored_switch_off_is_ignored_and_cleared(self):
        f = Path(self.tmp.name, 'switches.json')
        f.write_text(json.dumps({'actual-budget': False, 'other': True}))
        self.assertNotIn('actual-budget', [x['id'] for x in self.m.switches_doc()['switches']])
        self.assertTrue(self.m.actual_copy()['counts'])
        self.m._sw_forget_retired()
        self.assertEqual(json.loads(f.read_text()), {'other': True})

    def test_everything_finances_shows_is_copied_once(self):
        r = self.m.actual_copy()
        self.assertTrue(r['made'] and r['sent'] > 0, r)
        st = self.fake.state()
        names = {c['name'] for c in st['categories'].values()}
        self.assertTrue({'Groceries', 'Household', 'Coffee', 'Starting Balances', 'Income'} <= names)
        self.assertNotIn('Food', names)   # a new budget's example category goes
        accts = list(st['accounts'].values())
        self.assertEqual([(a['name'], a['account_id'], a['account_sync_source']) for a in accts], [('Sapphire 4321', 'ACT-1', 'simpleFin')])
        tx = self.tx_by_import(st)
        self.assertEqual(sorted(tx), ['T1', 'T2', 'T3'])   # the hidden one stays in Finances only
        cat = {k: v['name'] for k, v in st['categories'].items()}
        pay = {k: v['name'] for k, v in st['payees'].items()}
        t1 = tx['T1']
        self.assertEqual((t1['amount'], cat[t1['category']], t1['notes'], t1['imported_description'], t1['cleared']), (-8663, 'Groceries', 'weekly shop', 'WHOLE FOODS #123', 1))
        self.assertEqual(pay[tx['T2']['description']], 'Starbucks')   # the rule's merchant name
        kids = [t for t in st['transactions'].values() if t.get('parent_id') == next(k for k, v in st['transactions'].items() if v is tx['T3'])]
        self.assertEqual(sorted((t['amount'], cat[t['category']]) for t in kids), [(-8000, 'Groceries'), (-4000, 'Household')])
        self.assertEqual(tx['T3']['isParent'], 1)
        # Actual's balance (all rows, starting balance included) is the bank's
        self.assertEqual(sum(t['amount'] for t in st['transactions'].values() if not t.get('isChild')), -20000)
        rule = next(iter(st['rules'].values()))
        self.assertEqual(json.loads(rule['conditions'])[0], {'op': 'contains', 'field': 'imported_description', 'value': 'starbucks', 'type': 'string'})
        self.assertEqual(json.loads(rule['conditions'])[1]['options'], {'outflow': True})
        self.assertEqual(cat[json.loads(rule['actions'])[0]['value']], 'Coffee')
        n = len(self.fake.messages)
        self.assertEqual(self.m.actual_copy()['sent'], 0)   # nothing changed: nothing sent
        self.assertEqual(len(self.fake.messages), n)
        ov = self.m.overview()['actual']
        self.assertEqual(ov['counts'], {'accounts': 1, 'transactions': 3, 'categories': len(names) - 1, 'rules': 1})
        self.assertTrue(ov['ready'])

    def test_finances_changes_follow(self):
        self.m.actual_copy()
        self.m.update({'source': 'wizard-simplefin', 'id': 'T2', 'category': 'Restaurants', 'note': 'with Ana'})
        self.m.update({'source': 'wizard-simplefin', 'id': 'T4', 'hidden': False})
        self.m.update({'source': 'wizard-simplefin', 'id': 'T1', 'hidden': True})
        self.m.actual_copy()
        st = self.fake.state()
        tx = self.tx_by_import(st)
        cat = {k: v['name'] for k, v in st['categories'].items()}
        self.assertEqual(sorted(tx), ['T2', 'T3', 'T4'])
        self.assertEqual((cat[tx['T2']['category']], tx['T2']['notes']), ('Restaurants', 'with Ana'))
        self.assertEqual(sum(t['amount'] for t in st['transactions'].values() if not t.get('isChild')), -20000)

    def test_changes_made_in_actual_come_back(self):
        self.m.actual_copy()
        st = self.fake.state()
        tx = {v.get('financial_id'): k for k, v in st['transactions'].items()}
        cats = {v['name']: k for k, v in st['categories'].items()}
        self.fake.edit('transactions', tx['T1'], 'category', cats['Shopping'])
        self.fake.edit('transactions', tx['T1'], 'notes', 'changed in Actual')
        self.fake.edit('transactions', tx['T2'], 'tombstone', 1)
        self.fake.edit('categories', 'new-cat', 'name', 'Pets')
        self.fake.edit('categories', 'new-cat', 'cat_group', next(iter(st['category_groups'])))
        self.fake.edit('categories', 'new-cat', 'is_income', 0)
        pid = st['transactions'][tx['T3']]['description']
        self.fake.edit('payees', pid, 'name', 'Costco Wholesale')
        r = self.m.actual_copy()
        self.assertGreaterEqual(r['took'], 3, r)
        rows = {t['id']: t for t in self.m.transactions({'all': True, 'limit': 1, 'hidden': 'true'})['rows']}
        self.assertEqual((rows['T1']['category'], rows['T1']['note'], rows['T1']['reviewed']), ('Shopping', 'changed in Actual', True))
        self.assertTrue(rows['T2']['hidden'])
        self.assertEqual(rows['T3']['merchant'], 'Costco Wholesale')
        self.assertEqual(len(rows['T3']['splits']), 2)
        self.assertIn('Pets', self.m.overview()['categories'])
        # and nothing is pushed back over what Actual now says
        self.m.actual_copy()
        st = self.fake.state()
        t1 = st['transactions'][tx['T1']]
        self.assertEqual((st['categories'][t1['category']]['name'], t1['notes']), ('Shopping', 'changed in Actual'))
        self.assertNotIn(tx['T2'], st['transactions'])

    def test_simplefin_through_actual_keeps_the_same_transactions(self):
        self.m.actual_copy()
        with self.assertRaises(self.m.Problem) as e:
            self.m.actual_bank_connect({'token': 'not a token'})
        self.assertEqual(e.exception.code, 400)
        self.m.start_sync = lambda force=False: None
        ov = self.m.actual_bank_connect({'token': 'https://demo:demo@bridge.example/simplefin'})
        self.assertEqual(self.fake.secrets.get('simplefin_accessKey'), 'https://demo:demo@bridge.example/simplefin')
        self.assertTrue(ov['actual']['bank']['configured'])
        self.assertEqual(ov['source_name'], 'SimpleFIN in Actual Budget')
        r = self.m.sync()
        self.assertEqual(r['transactions'], 2)
        rows = {t['id']: t for t in self.m.transactions({'all': True, 'limit': 1, 'hidden': 'true'})['rows']}
        self.assertEqual((rows['T1']['category'], rows['T1']['note'], rows['T1']['reviewed']), ('Groceries', 'weekly shop', True))   # same id: his edits stay
        self.assertEqual((rows['T9']['amount'], rows['T9']['status'], rows['T9']['merchant']), (-23.1, 'pending', 'Uber'))
        self.assertIn(('POST', '/simplefin/transactions'), self.fake.calls)
        n = len(self.fake.calls)
        self.assertFalse(self.m.sync()['fetched'])   # asked recently: SimpleFIN is not asked again
        self.assertEqual(len(self.fake.calls), n)
        self.m.actual_copy()
        self.assertIn('T9', self.tx_by_import(self.fake.state()))
        self.m.actual_bank_forget()
        self.assertNotIn('simplefin_accessKey', self.fake.secrets)
        self.assertEqual(self.m.overview()['source_name'], 'SimpleFIN')

    def test_a_bad_token_is_not_kept(self):
        self.m.actual_copy()
        tok = __import__('base64').b64encode(b'https://bridge.example/simplefin/claim/abc').decode()
        with self.assertRaises(self.m.Problem):
            self.m.actual_bank_connect({'token': tok})
        self.assertNotIn('simplefin_token', self.fake.secrets)
        self.assertFalse(self.m.overview()['actual']['bank']['configured'])

    def test_unreachable_actual_is_reported_not_raised(self):
        self.m._ACTUAL.clear()
        self.m.ACTUAL_URL = 'http://127.0.0.1:9'
        r = self.m.actual_copy()
        self.assertIn("isn't answering", r['error'])
        self.assertIn("isn't answering", self.m.overview()['actual']['error'])
        self.assertEqual(self.m.overview()['transactions'], 3)


class ProtocolTests(unittest.TestCase):
    def test_sync_messages_round_trip(self):
        msgs = [(A.ts_string(1795000000123, 7, '0123456789abcdef'), 'transactions', 'r1', 'notes', A.ser('café ☕')),
                ('1970-01-01T00:00:00.000Z-0000-0000000000000000', 'accounts', 'a', 'offbudget', A.ser(0))]
        got, fid, gid, key, since = A.decode_request(A.encode_request(msgs, 'f', 'g', 'since'))
        self.assertEqual((got, fid, gid, key, since), (msgs, 'f', 'g', '', 'since'))
        self.assertEqual(A.decode_response(A.encode_response(msgs, '{"hash":1}')), (msgs, '{"hash":1}'))
        self.assertEqual(msgs[0][0], '2026-11-18T11:06:40.123Z-0007-0123456789abcdef')
        self.assertEqual(A.ts_parse(msgs[0][0]), (1795000000123, 7, '0123456789abcdef'))

    def test_values(self):
        for v, s in ((None, '0:'), (3, 'N:3'), (2.5, 'N:2.5'), (True, 'N:1'), ('x', 'S:x'), (-8663.0, 'N:-8663')):
            self.assertEqual(A.ser(v), s)
        self.assertEqual([A.de(x) for x in ('0:', 'N:3', 'N:2.5', 'S:x')], [None, 3, 2.5, 'x'])


if __name__ == '__main__':
    unittest.main()
