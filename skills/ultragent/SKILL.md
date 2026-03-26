---
name: ultragent
description: "Self-improving agent config evolution. Use when the user wants to evolve, optimize, or improve their Claude Code agent prompts, rules, and skills. Handles evolve loops, retro analysis, promote/rollback, status, and all evolution management. Also triggers on: evolve agents, self-improve, evolve config, optimize agents."
argument-hint: "[evolve N | status | retro | promote <gen_id> | rollback | memory | ...]"
allowed-tools: Bash(python *), Read, Grep, Glob, Agent
---

# UltrAgent — Self-Improving Agent Configuration

Autonomous evolutionary loop for Claude Code agent prompts, rules, and skills.
Combines patterns from 7 research projects: HyperAgents, AutoResearch, Agentic Researcher,
DeerFlow, codex-autoresearch, AutoKernel, Agent Laboratory, OpenClaw, and DeepTutor.

## Live Status

!`PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py status 2>/dev/null || echo "Not initialized. Run: PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py init"`

## Command Routing

Route `$ARGUMENTS` to the appropriate action:

| Command | Action |
|---------|--------|
| (empty) or `status` | Show status: `ua.py status` |
| `evolve [N]` | Run N evolution cycles — see [evolve protocol](references/evolve-protocol.md) |
| `retro` | Run retrospective: `ua.py retro` |
| `promote <gen_id>` | Show diff + apply to live config: `ua.py diff $1 && ua.py promote $1` |
| `rollback` | Revert to previous config: `ua.py rollback` |
| `frontier` | Show git frontier: `ua.py frontier` |
| `results [N]` | Show last N experiments: `ua.py results $1` |
| `lineage` | Show evolution tree: `ua.py lineage` |
| `diff <gen_id>` | Show generation's patch: `ua.py diff $1` |
| `init` | First-time setup: `ua.py init` |
| `memory` | Show structured evolution memory: `ua.py memory` |
| `stuck-check` | Check if evolution is stuck: `ua.py stuck-check` |
| `fingerprint` | Protocol fingerprint check: `ua.py fingerprint` |
| `spawn-log [gen_id]` | Show spawn depth tracking: `ua.py spawn-log $1` |
| `reflections` | Show in-session reflections: `ua.py reflections` |
| `circuit` | Circuit breaker status: `ua.py circuit` |
| `context [estimate\|assemble]` | Context engine: `context_engine.py $1` |
| `benchmark [task_id]` | Run benchmarks: `evaluate.py benchmark-prep $1` |

All commands use: `PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../<script>`

## Paths

The plugin root is two levels up from the skill directory (`${CLAUDE_SKILL_DIR}/../../`).
All Python scripts live at the plugin root.

```
PLUGIN_ROOT = ${CLAUDE_SKILL_DIR}/../..    (resolved at runtime)
UA_PY       = $PLUGIN_ROOT/ua.py
EVAL_PY     = $PLUGIN_ROOT/evaluate.py
PROGRAM     = $PLUGIN_ROOT/program.md
META        = $PLUGIN_ROOT/meta_prompt.md
```

## Quick Actions

### Init
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py init
```

### Evolve (the core loop)

For the full evolve protocol (Steps 1-9), read [references/evolve-protocol.md](references/evolve-protocol.md).

Summary: For each cycle:
1. Read research directives (`program.md`)
2. Select parent + suggest focus file (Amdahl's law impact scoring)
3. Prepare workspace (copy parent snapshot)
4. **Analysis Loop** (Sonnet): investigate + write sprint contract (DeepTutor dual-loop)
5. **Editor** (Opus): execute the sprint contract (with circuit breaker check)
6. Validate output + error recovery + smoke test
7. **Evaluate**: structural + 3 diverse judges (behavioral/quality/philosophy)
8. **Keep/Discard**: score > best = keep, else discard
9. **Stuck recovery**: 3 discards = REFINE, 5 = PIVOT
10. Cleanup & loop

### Retro
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py retro
```
Analyzes results.tsv + trajectories + lessons → updates program.md + evolution_memory.json.

### Promote
**Always show diff and ask confirmation:**
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py diff <gen_id>
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py promote <gen_id>
```

## Key Principles

1. **Single metric decides** — aggregate score > best? Keep. Else discard. No exceptions.
2. **Immutable evaluation** — evaluate.py and benchmarks/ are NEVER modified.
3. **program.md is the control surface** — Human iterates research directives, not code.
4. **Focused mutation** — One file per generation. Cleaner signal.
5. **Log everything** — results.tsv captures kept + discarded + crashed.
6. **Git frontier** — Only accepted improvements. Clean evolutionary history.
7. **Self-referential** — MetaAgent can edit program.md, meta_prompt.md, select_parent.py.
8. **Simplicity wins** — Removing bloat and getting equal score is a valid improvement.
9. **Never auto-promote** — Always confirm before applying to live config.
10. **Budget awareness** — 5 min per generation, ~12/hour, ~100 overnight.

## Detailed Protocol References

Load these ONLY when executing the relevant command:

- **[Evolve Protocol](references/evolve-protocol.md)** — Full Steps 1-5b: analysis loop, editor, error recovery, smoke test
- **[Evaluation Protocol](references/evaluation-protocol.md)** — Steps 6-8: diverse judges, scoring, keep/discard
- **[Stuck Recovery](references/stuck-recovery.md)** — Steps 8b-9: PIVOT/REFINE, fingerprint check, overnight mode
