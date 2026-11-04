"""Money's own alerts: a bank that needs the owner and large charges, each told once, no agent involved."""
import datetime as dt
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(data_dir):
    os.environ['DATA_DIR'] = data_dir
    spec = importlib.util.spec_from_file_location('money_server_' + str(time.time_ns()), ROOT / 'app' / 'server.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        self.posts = []
        self.ok = True
        c = self.m.db()
        today = dt.date.today()
        with c:
            self.m._set(c, 'source', 'wizard-simplefin')
            c.execute("INSERT INTO accounts VALUES ('wizard-simplefin','a1','Chase','Sapphire','1234','credit','USD',-100,?)", (today.isoformat(),))
        c.close()
        self.today = today

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, title, message, priority='default', tags=''):
        if self.ok:
            self.posts.append((title, message, priority, tags))
        return self.ok

    def tx(self, tid, amount, merchant, days_ago=0, category=None):
        c = self.m.db()
        with c:
            c.execute('INSERT INTO tx(source,id,account,date,amount,description,merchant,source_category,status) VALUES (?,?,?,?,?,?,?,?,?)',
                      ('wizard-simplefin', tid, 'a1', (self.today - dt.timedelta(days=days_ago)).isoformat(), amount, merchant, merchant, category, 'posted'))
        c.close()

    def run_check(self, now=None):
        return self.m.check_alerts(post=self.post, now=now)

    def test_first_run_is_a_baseline_then_new_large_charge_alerts_once(self):
        self.tx('t1', -900, 'Old Big Store')
        r = self.run_check()
        self.assertTrue(r['baseline']); self.assertEqual(self.posts, [])
        self.tx('t2', -750, 'Apple Store')
        self.tx('t3', -20, 'Coffee')
        self.run_check()
        self.assertEqual(len(self.posts), 1)
        title, msg, prio, _ = self.posts[0]
        self.assertEqual(title, 'Large charge'); self.assertIn('Apple Store', msg); self.assertIn('750.00', msg)
        self.assertEqual(prio, 'default')
        self.run_check()
        self.assertEqual(len(self.posts), 1)   # never twice

    def test_pending_then_posted_copy_is_the_same_charge(self):
        self.run_check()
        self.tx('p1', -600, 'Hotel')
        self.run_check()
        self.tx('p2', -600, 'Hotel')   # the posted copy gets a new id
        self.run_check()
        self.assertEqual(len(self.posts), 1)

    def test_transfers_and_old_charges_and_money_in_are_ignored(self):
        self.run_check()
        c = self.m.db()
        with c:
            c.execute("INSERT OR REPLACE INTO categories(name,pos,kind) VALUES ('Credit Card Payment',99,'transfer')")
        c.close()
        self.tx('x1', -5000, 'Autopay', category='Credit Card Payment')
        c = self.m.db()
        with c:
            c.execute("INSERT INTO edits(source,id,reviewed,category) VALUES ('wizard-simplefin','x1',0,'Credit Card Payment')")
        c.close()
        self.tx('x2', -900, 'Long ago', days_ago=10)
        self.tx('x3', 2000, 'Salary')
        self.run_check()
        self.assertEqual(self.posts, [])

    def test_threshold_and_off_switch(self):
        self.run_check()
        self.m.alerts_change({'action': 'set', 'large': 1000})
        self.tx('s1', -900, 'Below new size')
        self.run_check()
        self.assertEqual(self.posts, [])
        self.m.alerts_change({'action': 'set', 'on': False})
        self.tx('s2', -5000, 'Huge')
        self.assertEqual(self.run_check()['on'], False)
        self.assertEqual(self.posts, [])
        with self.assertRaises(self.m.Problem):
            self.m.alerts_change({'action': 'set', 'large': 0})

    def test_failed_send_is_retried_next_time(self):
        self.run_check()
        self.tx('f1', -800, 'Flight')
        self.ok = False
        self.assertEqual(self.run_check()['failed'], 1)
        self.ok = True
        self.run_check()
        self.assertEqual(len(self.posts), 1)

    def test_bank_problem_alerts_once_as_needs_you_and_again_after_reminder_gap(self):
        c = self.m.db()
        with c:
            c.execute("UPDATE accounts SET balance_at=?", ((self.today - dt.timedelta(days=6)).isoformat(),))
        c.close()
        now = time.time()
        self.run_check(now)
        self.assertEqual(len(self.posts), 1)
        title, msg, prio, tags = self.posts[0]
        self.assertIn('Chase stopped updating', title); self.assertEqual(prio, 'high'); self.assertEqual(tags, 'needs-you')
        self.run_check(now + 3600)
        self.assertEqual(len(self.posts), 1)
        self.run_check(now + 4 * 86400)
        self.assertEqual(len(self.posts), 2)

    def test_fixed_problem_alerts_again_if_it_comes_back(self):
        c = self.m.db()
        with c:
            self.m._set(c, 'last_error', 'SimpleFIN said no')
        c.close()
        now = time.time()
        self.run_check(now)
        c = self.m.db()
        with c:
            self.m._set(c, 'last_error', None)
            self.m._set(c, 'last_sync', self.m._now())
        c.close()
        self.run_check(now + 60)
        c = self.m.db()
        with c:
            self.m._set(c, 'last_error', 'SimpleFIN said no')
        c.close()
        self.run_check(now + 120)
        self.assertEqual(len(self.posts), 2)

    def test_notify_owner_speaks_the_notifications_mcp(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'content': [], 'isError': False}}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.m.NOTIFY_URL = f'http://127.0.0.1:{srv.server_port}/mcp'
            self.assertTrue(self.m.notify_owner('Large charge', 'x', 'default'))
            args = got[0]['params']['arguments']
            self.assertEqual(got[0]['params']['name'], 'notify'); self.assertEqual(args['app'], 'money')
            self.m.NOTIFY_URL = 'http://127.0.0.1:9/mcp'   # nothing there: a failure, not an exception
            self.assertFalse(self.m.notify_owner('t', 'x'))
        finally:
            srv.shutdown()

    def test_open_link_prefers_the_listed_shell_port_over_a_bare_host(self):
        os.environ['WIZARD_TRUSTED_ORIGINS'] = 'https://box.ts.net,https://box.ts.net:8444,https://box.ts.net:8796'
        try:
            self.assertEqual(self.m._open_link(), 'https://box.ts.net:8444/wizard-os-home/?open=money')
        finally:
            os.environ.pop('WIZARD_TRUSTED_ORIGINS')
        self.assertEqual(self.m._open_link(), '')

    def test_mcp_lists_and_runs_money_alerts(self):
        names = [t['name'] for t in self.m.TOOLS]
        self.assertIn('money_alerts', names)
        r = self.m.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'money_alerts', 'arguments': {'action': 'get'}}})
        self.assertEqual(r['result']['structuredContent'], {'on': True, 'large': 500.0})


if __name__ == '__main__':
    unittest.main()
