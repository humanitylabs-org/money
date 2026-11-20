"""Money is now Finances with app id finances (renamed_from: [money], money#28): old /money/ addresses still work."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from test_alerts import ROOT, load


class RenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.m = load(cls.tmp.name)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.m.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def get(self, path, host=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_port, timeout=10)
        c.request('GET', path, headers={'Host': host or f'127.0.0.1:{self.m.PORT}'})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader('Location'), body

    def test_manifest_moves_the_id_and_names_the_old_one(self):
        md = (ROOT / 'wizard.md').read_text()
        self.assertRegex(md, r'(?m)^id: finances$')
        self.assertRegex(md, r'(?m)^renamed_from: \[money\]$')
        self.assertRegex(md, r'(?m)^name: Finances$')
        cfg = json.loads((ROOT / 'runtipi' / 'config.json').read_text())
        self.assertEqual((cfg['id'], cfg['name']), ('finances', 'Finances'))
        self.assertEqual(self.m.APP_ID, 'finances')
        self.assertRegex((ROOT / 'skill' / 'SKILL.md').read_text(), r'(?m)^name: finances$')

    def test_health_and_page_answer_as_finances(self):
        st, _, body = self.get('/api/health')
        self.assertEqual((st, json.loads(body)['app']), (200, 'finances'))
        st, _, body = self.get('/finances/')
        self.assertEqual(st, 200)
        self.assertIn(b'<title>Finances</title>', body)

    def test_old_pages_redirect_to_finances(self):
        for old, new in (('/money', '/finances/'), ('/money/', '/finances/'), ('/money/?open=tx1', '/finances/?open=tx1'),
                         ('/money/index.html', '/finances/index.html')):
            st, loc, _ = self.get(old)
            self.assertEqual((st, loc), (302, new), old)

    def test_old_api_paths_still_answer(self):
        st, loc, body = self.get('/money/api/health')
        self.assertEqual((st, loc, json.loads(body)['app']), (200, None, 'finances'))

    def test_old_container_name_is_still_a_trusted_host(self):
        st, _, _ = self.get('/api/health', host=f'money:{self.m.PORT}')
        self.assertEqual(st, 200)
        st, _, _ = self.get('/api/health', host='elsewhere:1')
        self.assertEqual(st, 403)

    def test_stripped_old_address_moves_in_the_browser(self):
        boot = (ROOT / 'web' / 'boot.js').read_text()
        self.assertIn("fetch('/finances/api/health'", boot)
        self.assertTrue(re.search(r"location\.replace\('/finances'", boot))


if __name__ == '__main__':
    unittest.main()
