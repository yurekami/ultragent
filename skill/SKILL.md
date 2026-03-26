---
name: ultragent
description: "Self-improving agent config evolution. UltrAgent (Meta FAIR) + AutoResearch (Karpathy): evolve agent prompts, rules, skills with archive tracking, git frontier, keep/discard scoring, focused mutations."
triggers:
  - ultragent
  - evolve agents
  - self-improve
  - evolve config
  - ultragent
---

# UltrAgent x AutoResearch for Claude Code

Self-referential self-improving agent configuration system.
Combines Meta FAIR's UltrAgent (evolutionary archive, self-referential improvement)
with Karpathy's AutoResearch (fixed time budget, single metric, keep/discard, program.md).

## Paths

```
HA      = ~/.claude/ultragent
HA_PY   = $HA/ua.py
EVAL_PY = $HA/evaluate.py
PROGRAM = $HA/program.md
META    = $HA/meta_prompt.md
```

## Commands

| Input | Action |
|-------|--------|
| `/ultragent` or `/ultragent status` | Show status |
| `/ultragent evolve [N]` | Run N evolution cycles (default: 1) |
| `/ultragent promote <gen_id>` | Apply generation to live config |
| `/ultragent rollback` | Rollback to previous config |
| `/ultragent frontier` | Show git frontier (accepted only) |
| `/ultragent results [N]` | Show last N results (everything) |
| `/ultragent lineage` | Show evolution tree |
| `/ultragent diff <gen_id>` | Show what changed |
| `/ultragent benchmark [task_id]` | Run benchmarks |
| `/ultragent init` | First-time setup |

## Init

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py init
```

## Status

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py status
```

## Evolve — The Core Loop

This implements the tight AutoResearch experiment loop with UltrAgent' evolutionary memory. For each cycle:

### Step 1: Read Research Directives

Read `~/.claude/ultragent/program.md` — this contains current research priorities,
what to try, what NOT to try, and lessons learned.

### Step 2: Select Parent & Suggest Focus File

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py select-parent
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py suggest-focus
```

### Step 3: Prepare Workspace

```bash
WORK_DIR=$(mktemp -d)
cp -r ~/.claude/ultragent/generations/<parent_id>/snapshot/* "$WORK_DIR/"
```

### Step 4: Run MetaAgent (Time-Boxed, 5 Minutes)

Spawn an **Opus agent** (`subagent_type: executor`, `model: opus`) with:

```
<system>
{contents of meta_prompt.md}
</system>

<context>
## Research Directives
{contents of program.md}

## Focus File
{output of suggest-focus}
Modify ONLY this file (focused mutation mode).

## Archive Status
{output of ua.py status}

## Recent Experiments (results.tsv)
{output of ua.py results 10}

## Lessons for {focus_file} (MetaClaw: what NOT to do)
{lessons from lessons.jsonl filtered for this focus file}
These are REAL outcomes from prior attempts. DO NOT repeat failed approaches.
If empty: "No prior lessons for this file."

## Current Genome: {focus_file}
{full contents of the focus file from parent snapshot}

## Iterations Left: {remaining_cycles}
</context>

<task>
Analyze the focus file and make targeted improvements to maximize the aggregate score.
Work in: {WORK_DIR}
The genome files are already there. Edit the focus file.
After editing, write meta_reasoning.md in {WORK_DIR}.
</task>
```

Use `mode: "bypassPermissions"` — it's working on copies.

**IMPORTANT**: If focused_mutation is enabled, only send the focus file contents
to reduce context and cost. The MetaAgent should only modify that one file.

### Step 4b: Validate Output + Debug Retry (AI Scientist Pattern)

After MetaAgent completes, validate:

```bash
# Check file persisted and changed
wc -l $WORK_DIR/agents/<focus_file>
diff ~/.claude/ultragent/generations/<parent_id>/snapshot/agents/<focus_file> $WORK_DIR/agents/<focus_file> | head -5
```

If the file is UNCHANGED (no diff) or MISSING:
- This is a crash (sandbox write issue or MetaAgent produced nothing)
- **DEBUG RETRY**: Spawn a SMALL retry agent (sonnet, not opus — cheaper):
  ```
  "Your previous attempt to modify {focus_file} failed — the file is unchanged.
   The file content is: {full file content from parent snapshot}
   Apply these changes from the sprint contract: {contract summary}
   Write the COMPLETE file using: cat > {path} << 'ENDOFFILE' ... ENDOFFILE
   You have 3 tool calls max."
  ```
- If retry succeeds (file now different) → continue to Step 5
- If retry also fails → DISCARD with reason "crashed after debug retry"
- **Max 1 retry per candidate** (AI Scientist uses 3, but diminishing returns for markdown)

### Step 5: Capture Diff & Create Generation

After MetaAgent completes:

```bash
# Compute diff
python -c "
from pathlib import Path; import sys
sys.path.insert(0, str(Path.home() / '.claude' / 'ultragent'))
from ha import compute_diff, GENERATIONS_DIR
diff = compute_diff(GENERATIONS_DIR / '<parent_id>' / 'snapshot', Path('<WORK_DIR>'))
Path('<WORK_DIR>/patch.diff').write_text(diff)
print(f'Diff size: {len(diff)} chars')
"
```

If no diff, skip (MetaAgent made no changes). Log as crashed in results.tsv.

Copy workspace to generation directory:

```bash
NEXT_GEN=$(python -c "
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude' / 'ultragent'))
from ha import metadata_read; print(f'gen_{metadata_read().get(\"next_gen_number\",1):04d}')
")
mkdir -p ~/.claude/ultragent/generations/$NEXT_GEN/snapshot
cp -r $WORK_DIR/* ~/.claude/ultragent/generations/$NEXT_GEN/snapshot/ 2>/dev/null
cp $WORK_DIR/patch.diff ~/.claude/ultragent/generations/$NEXT_GEN/ 2>/dev/null
cp $WORK_DIR/meta_reasoning.md ~/.claude/ultragent/generations/$NEXT_GEN/ 2>/dev/null
```

### Step 6: Evaluate (Single Score)

**Structural eval (instant):**
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/evaluate.py structural $NEXT_GEN
```

**Ensemble Pairwise Evaluation (AI Scientist + Scientific Taste):**
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/evaluate.py prepare-judge $NEXT_GEN
```

Spawn **3 independent judge agents IN PARALLEL** (single message, 3 Agent calls).
Each is a `code-reviewer` (model: sonnet) with the same judge context.
Each must READ BOTH files (parent + child snapshot of the focus file).
Each returns JSON: `{preferred, confidence, score_delta, key_reason, ...}`

**Aggregate via majority vote:**
```python
from evaluate import ensemble_aggregate, compute_pairwise_aggregate
ensemble = ensemble_aggregate([judge_a, judge_b, judge_c])
# ensemble.votes = {"child": 2, "parent": 1} → child wins
aggregate = compute_pairwise_aggregate(structural_score, ensemble, parent_aggregate)
```

Unanimous (3-0) = high confidence. Split (2-1) = medium, log dissent.

Write scores to `generations/$NEXT_GEN/scores.json` then:
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py create-gen <parent_id> \
  ~/.claude/ultragent/generations/$NEXT_GEN/patch.diff \
  ~/.claude/ultragent/generations/$NEXT_GEN/scores.json \
  ~/.claude/ultragent/generations/$NEXT_GEN/meta_reasoning.md
```

### Step 7: Binary Keep/Discard Decision

Get the score and compare to best:

```python
new_score = scores["aggregate"]
best_score = metadata["best_score"]

if new_score > best_score:
    # KEEP — commit to frontier
    ua.py keep $NEXT_GEN
else:
    # DISCARD — log and move on
    ua.py discard $NEXT_GEN "score {new_score} <= best {best_score}"
```

### Step 8: Report to User

For KEEP:
```
[KEEP] gen_0003 score=0.847 > best=0.812
  Changed: agents/executor.md
  Reason: Added concrete code examples for immutable patterns
  Frontier: committed
  Want to promote to live config? (y/n)
```

For DISCARD:
```
[DISCARD] gen_0003 score=0.798 <= best=0.812
  Changed: agents/executor.md
  Logged to results.tsv. Moving to next cycle.
```

### Step 9: Cleanup & Loop

```bash
rm -rf $WORK_DIR
```

If N > 1, loop to Step 1.

## Overnight Mode

For `evolve 100` or large N:
- Run the loop autonomously
- Don't prompt user between cycles
- Only stop for: KEEP results (ask promote), errors, budget exhaustion
- At the end, show summary: kept/discarded/crashed counts, best score progression

## Frontier

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py frontier
```

Shows git log of accepted improvements only. This is the evolutionary "winning streak."

## Results

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py results 20
```

Shows EVERYTHING — kept, discarded, crashed. Like AutoResearch's `results.tsv`.
Analyze patterns: which file changes improve scores? Which consistently fail?

## Promote

**Always show diff and ask confirmation:**
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py diff <gen_id>
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py promote <gen_id>
```

## Key Principles

1. **Single metric decides** — aggregate score > best? Keep. Else discard. No exceptions.
2. **Immutable evaluation** — evaluate.py and benchmarks/ are NEVER modified. Like prepare.py.
3. **program.md is the control surface** — Human iterates research directives, not code.
4. **Focused mutation** — One file per generation. Cleaner signal.
5. **Log everything** — results.tsv captures kept + discarded + crashed. The discards contain signal.
6. **Git frontier** — Only accepted improvements. Clean evolutionary history.
7. **Self-referential** — MetaAgent can edit program.md, meta_prompt.md, select_parent.py.
8. **Simplicity wins** — Removing bloat and getting equal score is a valid improvement.
9. **Never auto-promote** — Always confirm before applying to live config.
10. **Budget awareness** — 5 min per generation, ~12/hour, ~100 overnight.
