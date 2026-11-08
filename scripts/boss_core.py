#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeBoss core: Claude is the boss, local Ollama models are the workers (stdlib only).

Design against the known failure modes of a boss/worker setup:
  drift       star topology: every worker gets its spec from Claude directly, never from another worker
  bad code    mechanical gate: placeholder check + the verify command, retried with the error, stop on
              the same error twice; only PASS output may be applied without force
  token cost  the worker writes to a staging file; Claude gets a few summary lines, not the code
  conflicts   one in-flight task per target; apply refuses when the target changed since the task began
  heat/VRAM   bounded parallelism, short keep_alive, VRAM check, GPU share reported on every result
"""
import concurrent.futures as cf
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".claude", "claudeboss.json")
STATS_PATH = os.path.join(HOME, ".claude", "claudeboss-stats.jsonl")
STAGE = ".claudeboss"

DEFAULTS = {
    "ollama_url": "http://localhost:11434",
    "model": "qwen2.5-coder:3b",
    "max_parallel": 2,
    "num_ctx": 6144,
    "num_predict": 2048,
    "temperature": 0.2,
    "keep_alive": "2m",
    "timeout": 300,
    "retries": 2,
    "verify_timeout": 180,
    "max_context_chars": 14000,
}

ROLES = {
    "coder": "Write complete, working code. Keep the names, signatures and style of the reference files.",
    "tester": "Write runnable unit tests for the given code. Cover normal, edge and error cases. "
              "Import the code under test by its module name; do not copy it into the test file.",
    "doc": "Write the requested documentation or docstrings. Be accurate to the reference files; invent nothing.",
}
SYSTEM = ("You are a {role} worker in a team led by a senior engineer. {rule} Follow the spec exactly. "
          "No TODO, no placeholders, no 'rest of code' comments. Reply with ONLY the complete content of "
          "the file {target} inside ONE fenced code block, nothing before or after it.")
PLACEHOLDER = re.compile(r"\b(TODO|FIXME)\b|rest of (the )?code|your code here|implement (this|me)\b"
                         r"|^\s*\.\.\.\s*$", re.I | re.M)
FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)

_inflight = set()
_inflight_lock = threading.Lock()
_stats_lock = threading.Lock()


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def inside(root, rel):
    """Absolute path of rel under root, or ValueError when it escapes root."""
    root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, p]) != root:
        raise ValueError(f"path escapes the project: {rel}")
    return p


def staged_path(cwd, target):
    return inside(os.path.join(cwd, STAGE, "out"), target)


def extract_code(text):
    """Longest fenced block, else the whole reply (a worker sometimes skips the fence)."""
    blocks = FENCE.findall(text or "")
    code = max(blocks, key=len) if blocks else (text or "").strip()
    return code if code.endswith("\n") else code + "\n"


def placeholder_problem(code):
    m = PLACEHOLDER.search(code)
    if not m:
        return None
    line = code.count("\n", 0, m.start()) + 1
    return f"placeholder at line {line}: {m.group(0).strip()!r}. Write the real code instead."


def read_context(files, cwd, limit):
    parts, used = [], 0
    for rel in files or []:
        p = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise ValueError(f"cannot read context file {rel}: {e.strerror}")
        room = limit - used
        if room <= 0:
            raise ValueError(f"context files exceed max_context_chars={limit}; pass fewer or smaller files")
        if len(text) > room:
            raise ValueError(f"{rel} is {len(text)} chars, only {room} left of max_context_chars={limit}; "
                             "pass a smaller file or split the task")
        used += len(text)
        parts.append(f"--- reference file: {rel} ---\n{text}")
    return "\n\n".join(parts)


def ollama_chat(messages, cfg, model):
    body = json.dumps({
        "model": model, "messages": messages, "stream": False, "keep_alive": cfg["keep_alive"],
        "options": {"num_ctx": cfg["num_ctx"], "num_predict": cfg["num_predict"],
                    "temperature": cfg["temperature"]},
    }).encode("utf-8")
    req = urllib.request.Request(cfg["ollama_url"] + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
        d = json.loads(r.read().decode("utf-8"))
    if d.get("error"):
        raise RuntimeError(d["error"])
    return d.get("message", {}).get("content", ""), d.get("done_reason")


def http_json(cfg, path, timeout=5):
    with urllib.request.urlopen(cfg["ollama_url"] + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def gpu_share(cfg, model):
    """Percent of the model held in VRAM, from /api/ps; None when unknown."""
    try:
        for m in http_json(cfg, "/api/ps").get("models", []):
            if m.get("name") == model or m.get("model") == model:
                return round(100 * m.get("size_vram", 0) / max(1, m.get("size", 1)))
    except (OSError, ValueError):
        pass
    return None


def vram():
    """(used_mb, total_mb) of the first NVIDIA GPU, or None."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        used, total = out.stdout.splitlines()[0].split(",")
        return int(used), int(total)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def run_verify(cmd, out, cwd, cfg):
    stage_root = os.path.join(cwd, STAGE, "out")
    cmd = cmd.replace("{out}", f'"{out}"').replace("{python}", f'"{sys.executable}"')
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (stage_root, cwd, env.get("PYTHONPATH")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    # stdin=DEVNULL: the child must not inherit the MCP stdin pipe (a Store-Python child hangs on it)
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        so, se = p.communicate(timeout=cfg["verify_timeout"])
    except subprocess.TimeoutExpired:
        kill_tree(p)
        return 124, f"verify timed out after {cfg['verify_timeout']} s"
    text = (so + b"\n" + se).decode("utf-8", errors="replace").strip()
    return p.returncode, "\n".join(text.splitlines()[-20:])


def kill_tree(p):
    """Kill the shell and its children; killing only the shell leaves the pipes open on Windows."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        p.kill()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def norm_err(err):
    """Error text without numbers that change between runs (times, addresses)."""
    return re.sub(r"\d+(\.\d+)?", "N", err or "")


def record(entry):
    entry["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    with _stats_lock:
        try:
            with open(STATS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def run_task(task, cfg, chat=ollama_chat):
    """Run one worker task; returns (ok, summary_text)."""
    cwd = os.path.abspath(task.get("cwd") or os.getcwd())
    target = task.get("target")
    spec = (task.get("spec") or "").strip()
    if not target or not spec:
        return False, "FAIL: 'spec' and 'target' are required"
    role = task.get("role") if task.get("role") in ROLES else "coder"
    model = task.get("model") or cfg["model"]
    retries = max(0, min(int(task.get("retries", cfg["retries"])), 5))
    try:
        target_abs = inside(cwd, target)
        out = staged_path(cwd, target)
        context = read_context(task.get("files"), cwd, cfg["max_context_chars"])
    except ValueError as e:
        return False, f"FAIL {target}: {e}"
    key = os.path.normcase(target_abs)
    with _inflight_lock:
        if key in _inflight:
            return False, f"FAIL {target}: another worker is already writing this target"
        _inflight.add(key)
    try:
        return _run(task, cfg, chat, cwd, target, target_abs, out, spec, role, model, retries, context)
    finally:
        with _inflight_lock:
            _inflight.discard(key)


def _run(task, cfg, chat, cwd, target, target_abs, out, spec, role, model, retries, context):
    base_sha = sha(target_abs)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    gi = os.path.join(cwd, STAGE, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("*\n")
    user = spec + ("\n\n" + context if context else "")
    messages = [{"role": "system", "content": SYSTEM.format(role=role, rule=ROLES[role], target=target)},
                {"role": "user", "content": user}]
    t0, last, status, err, attempts, code, vline = time.time(), None, "FAIL", "", 0, "", ""
    for attempts in range(1, retries + 2):
        try:
            reply, reason = chat(messages, cfg, model)
        except (OSError, RuntimeError, ValueError) as e:
            err = f"worker call failed: {e}. Is Ollama running and is model {model} pulled?"
            break
        cut = reason == "length"
        code = extract_code(reply)
        with open(out, "w", encoding="utf-8", newline="") as f:
            f.write(code)
        err = placeholder_problem(code) or ("reply was cut at num_predict; output is incomplete" if cut else "")
        if not err and task.get("verify"):
            rc, tail = run_verify(task["verify"], out, cwd, cfg)
            err = "" if rc == 0 else f"verify exit {rc}:\n{tail}"
            vline = (tail.splitlines() or [""])[-1] if rc == 0 else ""
        if not err:
            status = "PASS" if task.get("verify") else "UNVERIFIED"
            break
        if last is not None and norm_err(err) == last:
            err += "\n(same error twice: stopped retrying)"
            break
        last = norm_err(err)
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": f"The check failed:\n{err}\n\nFix it. Reply with the complete "
                                                 f"corrected file {target} in one fenced code block."}]
    secs = round(time.time() - t0)
    meta = {"target": target, "base_sha": base_sha, "status": status, "model": model, "role": role,
            "attempts": attempts, "out_sha": sha(out)}
    with open(out + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    gpu = gpu_share(cfg, model)
    lines = code.count("\n")
    head = (f"{status} {target} | role {role} | {attempts} attempt(s) | {secs} s | {lines} lines | "
            f"staged {os.path.relpath(out, cwd)}" + (f" | GPU {gpu}%" if gpu is not None else ""))
    if status == "PASS" and vline:
        head += f"\nverify: {vline}"
    elif status == "UNVERIFIED":
        head += "\nno verify command: review the staged file, then boss_apply with force=true"
    elif err:
        head += "\n" + err
    if gpu is not None and gpu < 100:
        head += f"\nwarning: only {gpu}% of {model} is on the GPU (slow); lower max_parallel or num_ctx"
    record({"target": target, "role": role, "model": model, "status": status, "attempts": attempts,
            "secs": secs, "worker_chars": len(code), "returned_chars": len(head)})
    return status == "PASS", head


def run_batch(tasks, cfg, chat=ollama_chat, cwd=None):
    if not tasks:
        return False, "FAIL: no tasks"
    for t in tasks:
        if cwd and not t.get("cwd"):
            t["cwd"] = cwd
    seen = set()
    for t in tasks:
        k = os.path.normcase(os.path.abspath(os.path.join(t.get("cwd") or os.getcwd(), t.get("target") or "")))
        if k in seen:
            return False, f"FAIL: two tasks write the same target {t.get('target')}; give each worker its own file"
        seen.add(k)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=max(1, int(cfg["max_parallel"]))) as ex:
        results = list(ex.map(lambda t: run_task(t, cfg, chat), tasks))
    ok = all(r[0] for r in results)
    n = sum(r[0] for r in results)
    head = f"batch: {n}/{len(results)} PASS | wall {round(time.time() - t0)} s | max_parallel {cfg['max_parallel']}"
    return ok, head + "\n" + "\n".join(r[1] for r in results)


def apply(target, cwd=None, force=False):
    cwd = os.path.abspath(cwd or os.getcwd())
    try:
        target_abs = inside(cwd, target)
        out = staged_path(cwd, target)
    except ValueError as e:
        return False, f"FAIL: {e}"
    try:
        with open(out + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return False, f"FAIL: nothing staged for {target}"
    if meta.get("status") != "PASS" and not force:
        return False, f"REFUSED {target}: status {meta.get('status')}; fix it, or review and pass force=true"
    if sha(out) != meta.get("out_sha") and not force:
        return False, f"REFUSED {target}: the staged file changed after the worker's check; re-run verify or force"
    if sha(target_abs) != meta.get("base_sha") and not force:
        return False, f"REFUSED {target}: it changed since the task started; re-run the task or force"
    note = ""
    if os.path.exists(target_abs):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = inside(os.path.join(cwd, STAGE, "backup"), target) + "." + stamp
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(target_abs, bak)
        note = f" | backup {os.path.relpath(bak, cwd)}"
    os.makedirs(os.path.dirname(target_abs), exist_ok=True)
    shutil.copyfile(out, target_abs)
    os.remove(out + ".meta.json")
    os.remove(out)
    return True, f"APPLIED {target}{note}"


def status(cfg):
    lines = []
    try:
        ver = http_json(cfg, "/api/version").get("version")
        names = [m["name"] for m in http_json(cfg, "/api/tags").get("models", [])]
        have = cfg["model"] in names or cfg["model"] + ":latest" in names
        lines.append(f"ollama {ver} up | worker model {cfg['model']}: {'pulled' if have else 'MISSING, run: ollama pull ' + cfg['model']}")
        for m in http_json(cfg, "/api/ps").get("models", []):
            g = round(100 * m.get("size_vram", 0) / max(1, m.get("size", 1)))
            lines.append(f"loaded: {m['name']} {m.get('size', 0) / 2**30:.1f} GB, {g}% GPU, ctx {m.get('context_length', '?')}")
    except (OSError, ValueError, KeyError) as e:
        lines.append(f"ollama DOWN at {cfg['ollama_url']} ({e}); start the Ollama app")
    v = vram()
    if v:
        lines.append(f"VRAM {v[0]}/{v[1]} MB used")
    lines.append(f"config: max_parallel {cfg['max_parallel']}, num_ctx {cfg['num_ctx']}, "
                 f"keep_alive {cfg['keep_alive']}, retries {cfg['retries']}")
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError):
        rows = []
    if rows:
        p = sum(r["status"] == "PASS" for r in rows)
        wc = sum(r["worker_chars"] for r in rows)
        rc = sum(r["returned_chars"] for r in rows)
        lines.append(f"stats: {len(rows)} tasks, {p} PASS ({round(100 * p / len(rows))}%), workers wrote "
                     f"~{round(wc / 4000, 1)}K tokens, Claude got ~{round(rc / 4000, 1)}K tokens of summaries")
    return "\n".join(lines)
