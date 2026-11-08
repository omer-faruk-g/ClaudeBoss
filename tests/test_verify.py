# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import boss_core as core  # noqa: E402


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claudeboss-verify-")
        self.cfg = dict(core.DEFAULTS, verify_timeout=3)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_timeout_kills_the_whole_tree(self):
        t0 = time.time()
        rc, text = core.run_verify('{python} -c "import time; time.sleep(60)"', "x", self.dir, self.cfg)
        self.assertEqual(rc, 124)
        self.assertIn("timed out", text)
        self.assertLess(time.time() - t0, 20)

    def test_child_gets_no_stdin(self):
        rc, text = core.run_verify('{python} -c "import sys; print(repr(sys.stdin.read()))"', "x", self.dir, self.cfg)
        self.assertEqual(rc, 0, text)
        self.assertIn("''", text)


if __name__ == "__main__":
    unittest.main()
