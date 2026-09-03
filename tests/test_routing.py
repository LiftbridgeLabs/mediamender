"""Drive the rendered router under node, so navigation is covered by CI.

The inline template JavaScript is otherwise only syntax-checked. These tests
exercise it: a broken route resolver would show up as a blank page in the
browser and pass every Python test.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "router_harness.js"
EXTRACTOR = REPO / "tests" / "render_js.py"


def node_available() -> bool:
    return shutil.which("node") is not None


@unittest.skipUnless(node_available(), "node is required to run the router harness")
class RouterHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mediamender-router-")
        cls.bundle = Path(cls.tmp) / "rendered.js"
        extract = subprocess.run(
            [sys.executable, str(EXTRACTOR), str(cls.bundle)],
            cwd=str(REPO), capture_output=True, text=True,
        )
        if extract.returncode != 0:
            raise AssertionError(f"Could not render the UI:\n{extract.stderr}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_rendered_javascript_parses(self):
        result = subprocess.run(
            ["node", "-e",
             "new Function(require('fs').readFileSync(process.argv[1],'utf8'))",
             str(self.bundle)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_router_behaves(self):
        result = subprocess.run(
            ["node", str(HARNESS), str(self.bundle)],
            cwd=str(REPO), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ROUTER CHECKS PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
