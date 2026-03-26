# Stuck Recovery & Long-Running Sessions

PIVOT/REFINE escalation, protocol fingerprint re-anchoring, and overnight mode.
Read this for multi-cycle evolves or when the loop gets stuck.

## Step 8b: Stuck Recovery Check (codex-autoresearch PIVOT/REFINE)

After keep/discard and before the next cycle, check if the loop is stuck:

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py stuck-check
```

**Graduated escalation ladder:**

| Trigger | Action | What Changes |
|---------|--------|-------------|
| 3 consecutive discards | **REFINE** | Switch to next-worst focus file, reset discard counter |
| 5 consecutive discards | **PIVOT** | Change parent selection strategy (rotate through ucb1→novelty→best→random), reset counters |
| 2 PIVOTs without keep | **ESCALATE** | Rotate competition strategies, update program.md with dead-end lessons |
| 3 PIVOTs without keep | **SOFT BLOCKER** | Warn, attempt self-referential changes (meta_prompt.md, select_parent.py) |
| Any single KEEP | **RESET** | All counters reset to zero |

**Auto-apply in the evolve loop:**

```python
stuck = check_stuck_recovery()  # via ua.py stuck-check

if stuck["action"] == "refine":
    # Auto-apply: switch focus file
    apply_refine()  # via: python ua.py apply-refine (not a CLI command — called internally)
    print(f"[REFINE] Switching focus. Next cycle targets: {stuck['recommendation']}")
    # Continue to next cycle with new focus

elif stuck["action"] == "pivot":
    # Auto-apply: change parent selection strategy
    apply_pivot()  # via: python ua.py apply-pivot
    print(f"[PIVOT] Strategy abandoned. New parent selection: {result['new_strategy']}")
    # Continue to next cycle with new strategy

elif stuck["action"] == "escalate":
    # Auto-apply: rotate competition strategies + update program.md
    print(f"[ESCALATE] {stuck['recommendation']}")
    # Run retro to update program.md with new insights
    # Continue with bolder changes

elif stuck["action"] == "soft_blocker":
    print(f"[WARNING] {stuck['pivot_count']} PIVOTs without improvement.")
    print(f"Evolution may need manual intervention or radically different targets.")
    # Continue with self-referential file targets (meta_prompt.md, select_parent.py)
```

A single KEEP anywhere in the loop resets all stuck counters automatically (handled by `ua.py keep`).

## Step 9: Cleanup & Loop

```bash
rm -rf $WORK_DIR
```

If N > 1, loop to Step 1.

## Protocol Fingerprint Check (codex-autoresearch Re-Anchoring)

Every **10 generations** in a multi-cycle evolve, perform a zero-cost self-check.
Also trigger after any context compaction warning.

### The 10-Point Checklist

Before the next cycle, internally verify these 10 items (yes/no, no tool calls):

1. MetaAgent has a 12-tool-call hard limit (aim for 4-6)
2. Smoke test runs BEFORE LLM-Judge (Step 5b before Step 6)
3. Keep/discard is binary: score > best → keep, else → discard
4. Competition mode spawns 3 parallel agents (simplifier, exemplifier, aligner)
5. Focus file content is PRE-LOADED in MetaAgent context (never re-read)
6. Stuck recovery: 3 discards → REFINE, 5 → PIVOT (Step 8b)
7. Ensemble evaluation uses 3 independent judge agents with majority vote
8. Sprint contract is MANDATORY before MetaAgent edits any file
9. Only ONE file is modified per generation (focused mutation)
10. Results are logged to results.tsv for EVERY generation (kept + discarded + crashed)

### On Failure

If ANY item cannot be recalled or feels uncertain:

1. Re-read these files from disk:
   - `~/.claude/ultragent/skill/SKILL.md` (this file — the evolve protocol)
   - `~/.claude/ultragent/meta_prompt.md` (MetaAgent instructions)
   - `~/.claude/ultragent/program.md` (research directives)
2. Tag the next generation's description with `[RE-ANCHOR]` in results.tsv
3. Continue the evolve loop from Step 1

### Compaction-Adaptive Frequency

| Compaction Events | Check Frequency |
|-------------------|----------------|
| 0 (default) | Every 10 generations |
| 1 | Every 5 generations |
| 2+ | Every generation until a KEEP occurs |

Track compaction events by watching for context length warnings in the session.

## Overnight Mode

For `evolve 100` or large N:
- Run the loop autonomously
- Don't prompt user between cycles
- Only stop for: KEEP results (ask promote), errors, budget exhaustion
- **Run Protocol Fingerprint Check every 10 generations** (or more frequently after compaction)
- **Run stuck recovery check (Step 8b) after every keep/discard decision**
- At the end, show summary: kept/discarded/crashed counts, best score progression
