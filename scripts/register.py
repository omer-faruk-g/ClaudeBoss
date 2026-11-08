#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add or remove the claudeboss MCP server in ~/.claude.json (user scope).

  python register.py add [--dry-run]     prints the mcpServers diff; writes only without --dry-run
  python register.py remove [--dry-run]
A timestamped backup is written next to ~/.claude.json before any change. Only the
top-level "mcpServers" key is touched; the rest of the file is kept as it is.
"""
import datetime
import json
import os
import shutil
import sys

PATH = os.path.join(os.path.expanduser("~"), ".claude.json")
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boss_server.py").replace("\\", "/")
# A Store Python's sys.executable is the versioned package path, which breaks on update; use its alias.
ALIAS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps",
                     "PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0", "python.exe")
PYTHON = ALIAS if "WindowsApps" in sys.executable and os.path.exists(ALIAS) else sys.executable
ENTRY = {"type": "stdio", "command": PYTHON.replace("\\", "/"), "args": ["-I", SERVER]}


def main(argv):
    if not argv or argv[0] not in ("add", "remove"):
        print(__doc__)
        return 2
    dry = "--dry-run" in argv
    with open(PATH, encoding="utf-8") as f:
        raw = f.read()
    d = json.loads(raw)
    before = dict(d.get("mcpServers") or {})
    after = dict(before)
    if argv[0] == "add":
        after["claudeboss"] = ENTRY
    else:
        after.pop("claudeboss", None)
    print("mcpServers before:", json.dumps(before, indent=2))
    print("mcpServers after: ", json.dumps(after, indent=2))
    if dry or after == before:
        print("dry run, nothing written" if dry else "no change")
        return 0
    bak = PATH + ".bak-claudeboss-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(PATH, bak)
    d["mcpServers"] = after
    tmp = PATH + ".tmp-claudeboss"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    with open(tmp, encoding="utf-8") as f:
        json.load(f)  # never replace the file with invalid JSON
    os.replace(tmp, PATH)
    print("written; backup", bak)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
