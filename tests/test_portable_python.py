"""
The portable Python that setup.bat downloads is the *embeddable* build. Its python311._pth
replaces the normal path setup, so the folder holding server.py is NOT on sys.path and a
plain "import importer" fails with ModuleNotFoundError — while every stdlib import still works.

This reproduces that by running server.py with the script's own directory removed from
sys.path, which is the closest a normal Python gets to the embedded layout.

Run: python -m unittest discover tests
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PortablePythonTests(unittest.TestCase):
    def test_server_imports_when_its_folder_is_not_on_sys_path(self):
        """What start.bat does under the embeddable build: run server.py from elsewhere."""
        code = (
            "import sys, runpy;"
            # drop every entry pointing at the app folder, as python311._pth effectively does
            f"sys.path[:] = [p for p in sys.path if p not in ('', {str(ROOT)!r})];"
            f"sys.argv = ['server.py'];"
            # import the module without running the server
            f"spec = __import__('importlib.util', fromlist=['util']).spec_from_file_location('server', {str(ROOT / 'server.py')!r});"
            "mod = __import__('importlib.util', fromlist=['util']).module_from_spec(spec);"
            "spec.loader.exec_module(mod);"
            "print('imported', bool(mod.importer.template_csv()))"
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              cwd=str(Path.home()))  # a different working directory too
        self.assertEqual(done.returncode, 0, f"server.py failed to import:\n{done.stderr}")
        self.assertIn("imported True", done.stdout)


if __name__ == "__main__":
    unittest.main()
