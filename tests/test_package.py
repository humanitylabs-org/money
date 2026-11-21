"""Finances runs from its source files (#37): the start command stays small however big the app gets."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('money_package', ROOT / 'tools' / 'package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class RunFromSource(unittest.TestCase):
    def test_recipe_is_run_from_source(self):
        self.assertEqual(package.problems(ROOT), [])

    def test_compose_stays_small(self):
        compose = (ROOT / 'runtipi' / 'docker-compose.yml').read_text()
        self.assertLess(len(compose), 8192)
        self.assertNotIn('b64decode', compose)

    def test_inlined_compose_is_rejected(self):
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name in ('wizard.md', 'runtipi/docker-compose.yml', 'app/server.py', 'web/index.html', 'web/app.js'):
                (d / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / name, d / name)
            c = d / 'runtipi' / 'docker-compose.yml'
            c.write_text(c.read_text().replace('    command: [python, /app/app/server.py]\n',
                                               "    command:\n    - python\n    - -c\n    - \"" + 'x' * 110000 + "\"\n"))
            bad = package.problems(d)
            self.assertTrue(any('inlines' in b or 'character line' in b for b in bad), bad)
            self.assertTrue(any('/app/app/server.py' in b for b in bad), bad)


if __name__ == '__main__':
    unittest.main()
