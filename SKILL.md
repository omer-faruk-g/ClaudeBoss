---
name: claudeboss
description: ClaudeBoss - Claude is the boss, local Ollama models are the workers. Through the claudeboss MCP server (boss_delegate, boss_batch, boss_apply, boss_status) a worker writes a whole file into .claudeboss/out/, a placeholder check plus a verify command gate it with auto-retry, and Claude gets a short summary instead of the code, reviews the diff and applies it. Use when a task needs a large, mechanically checkable file (unit tests, boilerplate, fixtures, docstrings, simple modules), when several such files can be written in parallel, or when the user says claudeboss, işçi, ollama işçi, yerel model, worker, patron.
---

# ClaudeBoss

Claude plans, specs, reviews and decides. Local Ollama workers type. The workers are small (3B on a
6 GB GPU), so quality comes from the gate, not from the worker: **no code that failed its check
reaches the project without force=true**. Zero errors from a 3B model is not a promise this skill makes.

## 1. Delegate or do it yourself

Delegate only when **all** hold:
- the output is a new or fully rewritten file of about 60+ lines;
- a command can check it (compile, import, tests);
- the spec is much shorter than the output (spec under ~1/4 of the output size).

Do it yourself when: a small edit to an existing file, architecture, security-sensitive code, a subtle
bug, anything needing more than ~14K chars of reference files, or the worker failed the same task once.
Reason: every delegation costs the spec (Claude output tokens), the summary, and the review read.
Below that size Claude writing it is cheaper.

## 2. Spec rules (against drift)

- **Star topology.** Every worker gets its spec from Claude. Never chain one worker's raw output into
  another worker; a later task may use an earlier file only after it was applied, passed via `files`.
- The spec is self-contained: target file, exact names and signatures, allowed imports, framework,
  edge and error cases. The worker sees only the spec and `files`.
- Few-shot through `files`: pass 1-2 existing project files as the style reference. The server reads them;
  Claude does not read them for this.
- One file per task. Split big jobs into files that can each be checked alone.
- **KEY POINT + examples** for any rule that is easy to get wrong: one sentence naming the trick (not
  the code) and 3-5 input -> output examples incl. the error cases. E.g. roman: "compute the value,
  then raise ValueError if to_roman(value) != s"; RLE: "counts can have several digits, validate with
  re.fullmatch first"; Turkish slug: "map 'İ' before lower()". Benchmark 2026-11-11: the three tasks
  7b failed with plain specs (slug, roman, RLE) all passed on the first try with this, same hidden checkers.

## 3. Calls

- Always pass `cwd` (absolute project root) and a `verify` command.
  - Python module: `{python} -m py_compile {out}`
  - Python tests: `{python} -m pytest {out} -q` (staged files shadow the project on PYTHONPATH)
  - JavaScript: `node --check {out}`; tests: the project's runner on `{out}`
- `boss_batch` for independent files in parallel. Each task has its own target; the server refuses a shared one.
- Run `boss_status` once per session before the first batch: is Ollama up, is the model pulled, how much VRAM is free.

## 4. Review and apply

- PASS proves it runs, not that it meets the spec. Review the **diff only**:
  `git diff --no-index -- <target> .claudeboss/out/<target>` (new file: Read the staged file with a limit).
- Apply only with `boss_apply`; never re-type worker code with Write (that pays the output tokens back).
- boss_apply refuses UNVERIFIED/FAIL output, a staged file edited after its check, and a target changed
  since the task began. Use force=true only after reviewing that exact case.
- A wrong-but-passing file: fix the spec or the test and re-run once; if it fails again, write it yourself.

## 5. Failure and limits

- The server retries with the error text (default 2, max 5) and stops when the same error comes back
  twice. After a FAIL do not loop: split the spec, or write it yourself (ClaudPer circuit breaker).
- A tester worker's tests follow ClaudeG test integrity: they must test the spec, never be weakened to pass.
- Worker model (benchmark 2026-11-11, 8 tasks with hidden checkers): qwen2.5-coder:7b passed 5/8, 3b passed 3/8.
  Default is 7b via `~/.claude/claudeboss.json` (`model`, `max_parallel 1`); 7b sits 84% on GPU at any num_ctx
  (~22-24 tok/s) and cannot share 6 GB with claude-mem, so the two evict each other (cold load 40-60 s).
  Pass `"model": "qwen2.5-coder:3b"` per task when speed matters more than quality.
- Hardware (GTX 1660 Ti 6 GB, 16 GB RAM): qwen2.5-coder:3b ~2.5 GB at ctx 6144. claude-mem's
  claude-mem-llama also sits on this GPU (3.1 GB). If a result says GPU below 100%, lower
  `max_parallel` or `num_ctx` in `~/.claude/claudeboss.json`; Ollama otherwise spills to CPU and slows
  down badly.
- More parallel workers = one model with more slots, not more models. Three different 3B models do not
  fit next to claude-mem on 6 GB.

## 6. Measure

`boss_status` shows the pass rate, how much the workers wrote and how much summary text Claude got.
Low pass rate (under ~60%) means specs too large or the model too weak: add KEY POINT + examples
to the spec first, then delegate less.

## Known minuses and what covers them

| Minus | Cover |
|---|---|
| Drift between workers | star topology, self-contained specs, applied files only |
| Wrong code from a small model | placeholder check + verify + retry with the error + apply gate |
| Claude's review cost | summary instead of code, diff-only review, apply without re-typing |
| Heat, lag, VRAM | max_parallel 2, keep_alive 2m, num_ctx 6144, GPU share on each result |
| Slowness | timeout 300 s, retry cap, delegate only large files |
| Sync conflicts | one in-flight task per target, base-hash check, backups in .claudeboss/backup |
| Not zero errors | stated plainly; the gate blocks unchecked code, it does not make the worker smarter |

## Config (`~/.claude/claudeboss.json`, optional)

`model`, `max_parallel`, `num_ctx`, `num_predict`, `temperature`, `keep_alive`, `timeout`, `retries`,
`verify_timeout`, `max_context_chars`, `ollama_url`. Defaults are in `scripts/boss_core.py`.

## Precedence

- ClaudPer's plan-and-approve gate (2+ files or 150+ lines) applies to the plan before delegating.
- Office may pick roles; for mechanical files the Devs/Testers it picks can run as ClaudeBoss workers.
- ClaudeLimit: tool results stay small; this server returns summaries for that reason.
