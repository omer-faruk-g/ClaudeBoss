#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live end-to-end check: starts boss_server.py over stdio like Claude Code does, then
boss_status + a 2-task boss_batch against the real Ollama on a throwaway project, then boss_apply.

  python e2e_check.py [project_dir]   (default ~/.claudeboss-e2e-tmp, deleted afterwards)
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CALC = '''"""Small calculator module used by the ClaudeBoss end-to-end check."""


def safe_div(a, b):
    """Return a / b; raise ValueError when b is 0."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def clamp(x, lo, hi):
    """Clamp x into [lo, hi]; raise ValueError when lo > hi."""
    if lo > hi:
        raise ValueError("lo > hi")
    return max(lo, min(x, hi))
'''


def rpc(p, mid, method, params=None):
    p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n").encode())
    p.stdin.flush()
    while True:
        d = json.loads(p.stdout.readline())
        if d.get("id") == mid:
            return d


def text(d):
    return d["result"]["content"][0]["text"]


def main():
    proj = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.claudeboss-e2e-tmp"))
    os.makedirs(os.path.join(proj, "tests"), exist_ok=True)
    with open(os.path.join(proj, "calc.py"), "w", encoding="utf-8") as f:
        f.write(CALC)
    p = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "boss_server.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=proj)
    try:
        init = rpc(p, 1, "initialize", {"protocolVersion": "2025-06-18"})
        print("initialize:", init["result"]["serverInfo"])
        print("tools:", [t["name"] for t in rpc(p, 2, "tools/list")["result"]["tools"]])
        print("--- boss_status\n" + text(rpc(p, 3, "tools/call", {"name": "boss_status", "arguments": {}})))
        tasks = [
            {"role": "tester", "target": "tests/test_calc.py", "files": ["calc.py"],
             "spec": "Write unittest tests for calc.py (import calc). Test safe_div: normal, negative, float, "
                     "b == 0 raises ValueError. Test clamp: inside, below, above, lo == hi, lo > hi raises "
                     "ValueError. End with: if __name__ == '__main__': unittest.main()",
             "verify": "{python} {out}"},
            {"role": "coder", "target": "slug.py",
             "spec": "Write slug.py with def slugify(text: str) -> str: lowercase, Turkish letters mapped "
                     "(ç->c, ğ->g, ı->i, ö->o, ş->s, ü->u, also upper case), every run of non [a-z0-9] chars "
                     "becomes one '-', no leading or trailing '-'. Stdlib only. At the bottom add: "
                     "if __name__ == '__main__': assert slugify('Çok Güzel  Şey!') == 'cok-guzel-sey'; "
                     "assert slugify('  --A_b--  ') == 'a-b'; print('ok')",
             "verify": "{python} {out}"},
        ]
        t0 = time.time()
        r = rpc(p, 4, "tools/call", {"name": "boss_batch", "arguments": {"tasks": tasks, "cwd": proj}})
        print(f"--- boss_batch ({round(time.time() - t0)} s wall)\n" + text(r))
        for i, t in enumerate(tasks):
            a = rpc(p, 10 + i, "tools/call", {"name": "boss_apply", "arguments": {"target": t["target"], "cwd": proj}})
            print("apply:", text(a))
        print("--- boss_status\n" + text(rpc(p, 20, "tools/call", {"name": "boss_status", "arguments": {}})))
        ok = not r["result"]["isError"]
    finally:
        p.stdin.close()
        p.wait(timeout=10)
        p.stdout.close()
        if len(sys.argv) < 2:
            shutil.rmtree(proj, ignore_errors=True)
    print("E2E", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
