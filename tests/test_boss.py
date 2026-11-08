# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
import boss_core as core  # noqa: E402

GOOD = "```python\ndef add(a, b):\n    return a + b\n\nassert add(2, 3) == 5\n```"
BAD = "```python\ndef add(a, b)\n    return a + b\n```"
def rd(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


HOLE = "```python\ndef add(a, b):\n    # TODO\n    pass\n```"


class Fake:
    """Scripted worker: returns replies in order and records the messages it saw."""

    def __init__(self, *replies, delay=0.0):
        self.replies, self.calls, self.delay = list(replies), [], delay

    def __call__(self, messages, cfg, model):
        self.calls.append([dict(m) for m in messages])
        time.sleep(self.delay)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return r, "stop"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claudeboss-test-")
        self.cfg = dict(core.DEFAULTS)
        self._stats, self._gpu = core.STATS_PATH, core.gpu_share
        core.STATS_PATH = os.path.join(self.dir, "stats.jsonl")
        core.gpu_share = lambda cfg, model: None

    def tearDown(self):
        core.STATS_PATH, core.gpu_share = self._stats, self._gpu
        shutil.rmtree(self.dir, ignore_errors=True)

    def task(self, **kw):
        t = {"spec": "write add(a, b)", "target": "calc.py", "cwd": self.dir, "verify": "{python} {out}"}
        t.update(kw)
        return t


class TestExtract(unittest.TestCase):
    def test_longest_fence_wins(self):
        self.assertEqual(core.extract_code("```\nx\n```\ntext\n```py\nlonger = 1\n```"), "longer = 1\n")

    def test_no_fence_uses_whole_reply(self):
        self.assertEqual(core.extract_code("  y = 2  "), "y = 2\n")

    def test_placeholder_found_with_line(self):
        self.assertIn("line 2", core.placeholder_problem("a = 1\n# TODO later\n"))
        self.assertIsNone(core.placeholder_problem("total = sum(xs)\n"))


class TestRunTask(Base):
    def test_pass_stages_file_and_meta(self):
        ok, text = core.run_task(self.task(), self.cfg, Fake(GOOD))
        self.assertTrue(ok, text)
        self.assertTrue(text.startswith("PASS calc.py"))
        out = os.path.join(self.dir, ".claudeboss", "out", "calc.py")
        self.assertIn("return a + b", rd(out))
        self.assertEqual(json.loads(rd(out + ".meta.json"))["status"], "PASS")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "calc.py")))  # project untouched
        self.assertNotIn("return a + b", text)  # Claude gets a summary, not the code

    def test_retry_feeds_the_error_back(self):
        fake = Fake(BAD, GOOD)
        ok, text = core.run_task(self.task(), self.cfg, fake)
        self.assertTrue(ok, text)
        self.assertIn("2 attempt(s)", text)
        self.assertIn("SyntaxError", fake.calls[1][-1]["content"])

    def test_same_error_twice_stops(self):
        fake = Fake(BAD)
        ok, text = core.run_task(self.task(retries=5), self.cfg, fake)
        self.assertFalse(ok)
        self.assertIn("same error twice", text)
        self.assertEqual(len(fake.calls), 2)

    def test_placeholder_is_rejected_and_retried(self):
        ok, text = core.run_task(self.task(), self.cfg, Fake(HOLE, GOOD))
        self.assertTrue(ok, text)
        self.assertIn("2 attempt(s)", text)

    def test_cut_reply_is_not_accepted(self):
        def cut(messages, cfg, model):
            return GOOD, "length"
        ok, text = core.run_task(self.task(), self.cfg, cut)
        self.assertFalse(ok)
        self.assertIn("num_predict", text)

    def test_no_verify_is_unverified(self):
        ok, text = core.run_task(self.task(verify=None), self.cfg, Fake(GOOD))
        self.assertFalse(ok)
        self.assertTrue(text.startswith("UNVERIFIED"))

    def test_target_outside_project_is_refused(self):
        ok, text = core.run_task(self.task(target="../evil.py"), self.cfg, Fake(GOOD))
        self.assertFalse(ok)
        self.assertIn("escapes", text)

    def test_context_files_go_to_worker(self):
        with open(os.path.join(self.dir, "ref.py"), "w") as f:
            f.write("STYLE = 'snake_case'\n")
        fake = Fake(GOOD)
        core.run_task(self.task(files=["ref.py"]), self.cfg, fake)
        self.assertIn("STYLE = 'snake_case'", fake.calls[0][1]["content"])

    def test_oversized_context_is_refused(self):
        with open(os.path.join(self.dir, "big.py"), "w") as f:
            f.write("x" * (self.cfg["max_context_chars"] + 1))
        ok, text = core.run_task(self.task(files=["big.py"]), self.cfg, Fake(GOOD))
        self.assertFalse(ok)
        self.assertIn("max_context_chars", text)

    def test_worker_down_reports_cleanly(self):
        def down(messages, cfg, model):
            raise OSError("connection refused")
        ok, text = core.run_task(self.task(), self.cfg, down)
        self.assertFalse(ok)
        self.assertIn("Is Ollama running", text)

    def test_same_target_in_flight_is_refused(self):
        slow = Fake(GOOD, delay=0.6)
        res = {}
        th = threading.Thread(target=lambda: res.update(a=core.run_task(self.task(), self.cfg, slow)))
        th.start()
        time.sleep(0.2)
        ok, text = core.run_task(self.task(), self.cfg, Fake(GOOD))
        th.join()
        self.assertFalse(ok)
        self.assertIn("already writing", text)
        self.assertTrue(res["a"][0])


class TestBatch(Base):
    def test_parallel_wall_time(self):
        self.cfg["max_parallel"] = 2
        fake = Fake(GOOD, delay=0.8)
        t0 = time.time()
        ok, text = core.run_batch([self.task(target="a.py"), self.task(target="b.py")], self.cfg, fake)
        self.assertTrue(ok, text)
        self.assertLess(time.time() - t0, 1.5)
        self.assertIn("2/2 PASS", text)

    def test_duplicate_target_refused(self):
        ok, text = core.run_batch([self.task(), self.task()], self.cfg, Fake(GOOD))
        self.assertFalse(ok)
        self.assertIn("same target", text)


class TestApply(Base):
    def test_apply_pass_with_backup(self):
        with open(os.path.join(self.dir, "calc.py"), "w") as f:
            f.write("old\n")
        core.run_task(self.task(), self.cfg, Fake(GOOD))
        ok, text = core.apply("calc.py", self.dir)
        self.assertTrue(ok, text)
        self.assertIn("return a + b", rd(os.path.join(self.dir, "calc.py")))
        self.assertIn("backup", text)

    def test_unverified_needs_force(self):
        core.run_task(self.task(verify=None), self.cfg, Fake(GOOD))
        self.assertFalse(core.apply("calc.py", self.dir)[0])
        self.assertTrue(core.apply("calc.py", self.dir, force=True)[0])

    def test_target_changed_since_task_is_refused(self):
        core.run_task(self.task(), self.cfg, Fake(GOOD))
        with open(os.path.join(self.dir, "calc.py"), "w") as f:
            f.write("someone else wrote this\n")
        ok, text = core.apply("calc.py", self.dir)
        self.assertFalse(ok)
        self.assertIn("changed since the task started", text)

    def test_staged_file_edited_after_check_is_refused(self):
        core.run_task(self.task(), self.cfg, Fake(GOOD))
        with open(os.path.join(self.dir, ".claudeboss", "out", "calc.py"), "a") as f:
            f.write("broken(\n")
        ok, text = core.apply("calc.py", self.dir)
        self.assertFalse(ok)
        self.assertIn("after the worker's check", text)


class TestServer(unittest.TestCase):
    def test_protocol(self):
        p = subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "boss_server.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "boss_apply", "arguments": {"target": "nope.py", "cwd": tempfile.gettempdir()}}},
                {"jsonrpc": "2.0", "id": 4, "method": "bogus"}]
        p.stdin.write(b"".join((json.dumps(m) + "\n").encode() for m in msgs))
        p.stdin.flush()
        got = {}
        while len(got) < 4:
            d = json.loads(p.stdout.readline())
            got[d["id"]] = d
        p.stdin.close()
        p.wait(timeout=10)
        p.stdout.close()
        self.assertEqual(got[1]["result"]["serverInfo"]["name"], "claudeboss")
        self.assertEqual(got[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({t["name"] for t in got[2]["result"]["tools"]},
                         {"boss_delegate", "boss_batch", "boss_apply", "boss_status"})
        self.assertTrue(got[3]["result"]["isError"])
        self.assertEqual(got[4]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
