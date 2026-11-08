# ClaudeBoss

Claude Code is the boss; local Ollama models are the workers. A small MCP server (Python stdlib,
no npm) lets Claude hand a whole file to a local model, gate it mechanically, and apply it without
re-typing it.

```
Claude ──spec──► boss_delegate / boss_batch ──► Ollama worker(s) ──► .claudeboss/out/<target>
   ▲                                              │  placeholder check + verify, retry with the error
   └──── 3-line summary (PASS/FAIL, lines, s) ◄───┘
Claude reviews the diff ──► boss_apply (refuses unchecked or conflicting output, keeps a backup)
```

## Install

1. `ollama pull qwen2.5-coder:3b`
2. Register the server in `~/.claude.json` (user scope):

```json
"mcpServers": {
  "claudeboss": {
    "type": "stdio",
    "command": "<python.exe>",
    "args": ["-I", "<home>/.claude/skills/claudeboss/scripts/boss_server.py"]
  }
}
```

3. Start a new Claude Code session; the `boss_*` tools appear.

## Check

- Unit tests: `python -m unittest discover -s tests` (23 tests, fake worker + verify timeout)
- Live: `python scripts/e2e_check.py` (real Ollama, real MCP stdio handshake, 2 parallel workers)

## Tools

| Tool | Does |
|---|---|
| `boss_delegate` | one worker task: spec + target + reference files + verify |
| `boss_batch` | several tasks in parallel (`max_parallel`, default 2) |
| `boss_apply` | staged PASS file → target, with backup and conflict checks |
| `boss_status` | Ollama, model, loaded models / GPU share, VRAM, config, pass-rate stats |

Rules for when to delegate and how to write specs are in `SKILL.md`.

License: GPL-3.0-or-later.
