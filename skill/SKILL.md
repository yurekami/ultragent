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

### Step 1b: Check Auto-Evolve Queue

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py pending-evolves
```

If there are queued evolves, use the HIGHEST-PRIORITY queued agent file as the
focus file instead of suggest-focus. The queue contains real failure data from
normal usage — this is more valuable than structural scoring.

After processing, drain the queue:
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py drain-queue
```

### Step 2: Select Parent & Suggest Focus File

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py select-parent
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py suggest-focus
```

If the queue overrode the focus file, skip suggest-focus and use the queued target.

### Step 3: Prepare Workspace(s)

Check competition mode:
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py competition
```

**If competition enabled (default: 3 agents)**:
Create one workspace per competitor:
```bash
WORK_A=$(mktemp -d)  # simplifier
WORK_B=$(mktemp -d)  # exemplifier
WORK_C=$(mktemp -d)  # aligner
for d in $WORK_A $WORK_B $WORK_C; do
  cp -r ~/.claude/ultragent/generations/<parent_id>/snapshot/* "$d/"
done
```

**If competition disabled**: single workspace as before.

### Step 3b: Load Proven Skills

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py skills
```

Include the skills list in the MetaAgent context as "Proven Skills."

### Step 4: Run MetaAgents (Competition Mode)

Spawn **ALL competitors IN PARALLEL** using multiple Agent tool calls in a single message.
Each gets the same base context but a DIFFERENT strategy directive.

For EACH competitor, spawn an **Opus agent** (`subagent_type: executor`, `model: opus`) with:

```
<system>
{contents of meta_prompt.md}
</system>

<context>
## Your Strategy
{strategy directive for THIS competitor — e.g., simplifier/exemplifier/aligner}
You are competing against other agents. Execute YOUR strategy well.

## Proven Skills (from evolution history)
{output of ua.py skills — patterns that worked in prior generations}
Use these if they apply to your current file.

## Research Directives
{contents of program.md}

## Focus File
{output of suggest-focus}
Modify ONLY this file (focused mutation mode).

## Archive Status
{output of ua.py status}

## Recent Experiments (results.tsv)
{output of ua.py results 10}

## Real-World Trajectories for {focus_file}
{output of: python ua.py trajectories 20 | filtered for focus_file}
If no trajectories exist for this file, say "No trajectory data available."
If trajectories exist, this is the HIGHEST-VALUE context — prioritize fixing
the failure patterns shown here over generic improvements.

## Auto-Evolve Queue Reason (if applicable)
{the reason from the queue entry, if this evolve was auto-triggered}

## Current Genome: {focus_file}
{full contents of the focus file from parent snapshot}

## Iterations Left: {remaining_cycles}
</context>

<task>
1. FIRST: Write sprint_contract.md in {WORK_DIR} with your proposal
   (target file, hypothesis, planned changes, expected impact, success criteria, risk)
2. THEN: Edit the focus file to implement your proposal
3. LAST: Write meta_reasoning.md evaluating your own execution against the contract
Work in: {WORK_DIR}
</task>
```

Use `mode: "bypassPermissions"` — it's working on copies.

**HARD LIMITS for MetaAgent spawn:**
- Include the FULL focus file content in the prompt (so the MetaAgent doesn't need to read it)
- Tell it to write the COMPLETE file in ONE tool call (no incremental edits)
- Budget: 12 tool calls max. Last gen used 30 calls and took 56 minutes.
- If the MetaAgent exceeds ~5 minutes, that's a process failure to note in program.md

**IMPORTANT**: If focused_mutation is enabled, only send the focus file contents
to reduce context and cost. The MetaAgent should only modify that one file.

### Step 4b: Validate Sprint Contract (Quick Gate)

After MetaAgent completes, check if `sprint_contract.md` exists in the workspace.
If missing, log as crashed (MetaAgent didn't follow protocol).

Read the sprint contract. Quick sanity check (you can do this yourself, no agent needed):
- Does it have a clear hypothesis?
- Are planned changes specific (not "improve the file")?
- Is expected impact realistic (not "all dimensions will improve to 10")?

If the contract is obviously weak (vague, unrealistic, or proposes no real changes),
you MAY skip to discard without running the full evaluation. This saves cost.
Log as `discard` with reason "weak sprint contract".

### Step 4c: Competition — Repeat Step 4 for Each Competitor

In competition mode, Steps 4-4b run for EACH competitor:
- Competitor A (simplifier) → WORK_A → sprint_contract_A
- Competitor B (exemplifier) → WORK_B → sprint_contract_B
- Competitor C (aligner) → WORK_C → sprint_contract_C

**CRITICAL**: Spawn all 3 as PARALLEL Agent calls in a single message.
This runs them simultaneously — no sequential waiting.

After all complete, proceed to Step 5 for each valid output.

### Step 5: Capture Diff & Create Generation(s)

**In competition mode**: Do this for EACH competitor that produced a valid diff.
Tag each generation with its strategy in the archive entry.

After MetaAgent completes:

```bash
# Compute diff (only compare genome files, not sprint_contract.md or meta_reasoning.md)
python -c "
from pathlib import Path; import sys
sys.path.insert(0, str(Path.home() / '.claude' / 'ultragent'))
from ha import compute_diff, GENERATIONS_DIR
diff = compute_diff(GENERATIONS_DIR / '<parent_id>' / 'snapshot', Path('<WORK_DIR>'))
Path('<WORK_DIR>/patch.diff').write_text(diff, encoding='utf-8')
print(f'Diff size: {len(diff)} chars')
"
```

If no diff, skip (MetaAgent made no changes). Log as crashed in results.tsv.
Copy sprint_contract.md to the generation directory alongside meta_reasoning.md.

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

**Pairwise LLM-Judge eval (Scientific Taste approach):**
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/evaluate.py prepare-judge $NEXT_GEN
```
Spawn a `code-reviewer` agent (model: sonnet) with the judge context.

The evaluator uses PAIRWISE COMPARISON (not rubric scoring):
- Compares parent version vs child version of the changed file
- Reasons step by step about each change
- Classifies changes as behavioral vs cosmetic
- Checks position-swap consistency
- Includes dynamic few-shot from real preference history
- Returns: preferred (parent/child), confidence, score_delta, key_reason

Tell the evaluator to READ BOTH files:
- Parent file: `~/.claude/ultragent/generations/<parent_id>/snapshot/<focus_file>`
- Child file: `~/.claude/ultragent/generations/$NEXT_GEN/snapshot/<focus_file>`

Parse JSON response. Compute aggregate with:
```python
from evaluate import compute_pairwise_aggregate
aggregate = compute_pairwise_aggregate(structural_score, judge_result, parent_aggregate)
```

Write scores to `generations/$NEXT_GEN/scores.json` then:
```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py create-gen <parent_id> \
  ~/.claude/ultragent/generations/$NEXT_GEN/patch.diff \
  ~/.claude/ultragent/generations/$NEXT_GEN/scores.json \
  ~/.claude/ultragent/generations/$NEXT_GEN/meta_reasoning.md
```

### Step 7: Competition Winner Selection + Keep/Discard

**In competition mode** (3 candidates evaluated):
1. Compare all candidates' score_deltas
2. Pick the candidate with the highest score_delta (the "winner")
3. DISCARD all losers with reason "lost competition to {winner}"
4. Apply keep/discard to the winner only:

```python
# Competition winner selection
candidates = [
    {"gen_id": "gen_A", "score_delta": 0.15, "strategy": "simplifier"},
    {"gen_id": "gen_B", "score_delta": 0.22, "strategy": "exemplifier"},
    {"gen_id": "gen_C", "score_delta": -0.03, "strategy": "aligner"},
]
winner = max(candidates, key=lambda c: c["score_delta"])
# winner = gen_B (exemplifier)

# Discard losers
for c in candidates:
    if c != winner:
        ua.py discard c["gen_id"] f"lost competition to {winner['gen_id']} ({winner['strategy']})"

# Keep/discard winner against current best
if winner_aggregate > best_score:
    ua.py keep winner["gen_id"]   # also auto-registers skill!
else:
    ua.py discard winner["gen_id"] "competition winner still below best"
```

**Single agent mode**: Same as before — one candidate, simple keep/discard.

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
