#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeBoss MCP server (stdio, newline-delimited JSON-RPC 2.0, stdlib only).

Each tools/call runs on its own thread, so parallel calls from Claude run in parallel here too
(bounded by Ollama itself); boss_batch adds its own max_parallel pool.
"""
import json
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boss_core as core  # noqa: E402

VERSION = "1.0.0"
TASK = {
    "type": "object",
    "properties": {
        "spec": {"type": "string", "description": "Self-contained spec: what to write, exact names and "
                                                  "signatures, framework, edge cases. The worker sees nothing else."},
        "target": {"type": "string", "description": "Project-relative file the output is for"},
        "role": {"type": "string", "enum": ["coder", "tester", "doc"]},
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Reference files the server reads for the worker (Claude does not read them)"},
        "verify": {"type": "string", "description": "Shell check run in cwd; {out} = staged file, {python} = "
                                                    "Python. Staged files shadow the project on PYTHONPATH."},
        "retries": {"type": "integer", "minimum": 0, "maximum": 5},
        "model": {"type": "string"},
        "cwd": {"type": "string", "description": "Absolute project root"},
    },
    "required": ["spec", "target", "cwd"],
}
TOOLS = [
    {"name": "boss_delegate",
     "description": "Have a local Ollama worker write one file into .claudeboss/out/, gate it with a placeholder "
                    "check + the verify command (auto-retry with the error), and return a short summary, not the code.",
     "inputSchema": TASK},
    {"name": "boss_batch",
     "description": "Run several boss_delegate tasks in parallel (max_parallel); each task needs its own target.",
     "inputSchema": {"type": "object", "properties": {"tasks": {"type": "array", "items": TASK},
                                                      "cwd": {"type": "string"}},
                     "required": ["tasks"]}},
    {"name": "boss_apply",
     "description": "Copy a staged PASS file to its target (backup kept). Refuses UNVERIFIED/FAIL output, a staged "
                    "file edited after its check, or a target changed since the task began, unless force=true.",
     "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "cwd": {"type": "string"},
                                                      "force": {"type": "boolean"}},
                     "required": ["target", "cwd"]}},
    {"name": "boss_status",
     "description": "Ollama up?, worker model pulled?, loaded models and GPU share, VRAM, config, pass-rate stats.",
     "inputSchema": {"type": "object", "properties": {}}},
]
INSTRUCTIONS = ("ClaudeBoss: local workers for large, mechanically checkable files (tests, boilerplate, docs). "
                "Load the claudeboss skill before the first call.")

_out_lock = threading.Lock()


def send(obj):
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def call_tool(name, args):
    cfg = core.load_config()
    if name == "boss_delegate":
        return core.run_task(args, cfg)
    if name == "boss_batch":
        return core.run_batch(args.get("tasks") or [], cfg, cwd=args.get("cwd"))
    if name == "boss_apply":
        return core.apply(args.get("target"), args.get("cwd"), bool(args.get("force")))
    if name == "boss_status":
        return True, core.status(cfg)
    raise KeyError(name)


def handle_call(mid, params):
    try:
        ok, text = call_tool(params.get("name"), params.get("arguments") or {})
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text", "text": text}], "isError": not ok}})
    except KeyError as e:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {e}"}})
    except Exception as e:  # report, never kill the server
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": f"FAIL: {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"}],
            "isError": True}})


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return  # notification (initialized, cancelled): nothing to answer
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "claudeboss", "version": VERSION},
            "instructions": INSTRUCTIONS}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=handle_call, args=(mid, params), daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main():
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        handle(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
