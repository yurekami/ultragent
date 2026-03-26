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
| `/ultragent retro` | Run retrospective: analyze patterns, update program.md |
| `/ultragent stuck-check` | Check if evolution is stuck, recommend recovery action |
| `/ultragent fingerprint` | Show protocol fingerprint check items for re-anchoring |

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

### Step 2: Select Parent & Suggest Focus File (Amdahl's Law)

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py select-parent
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py suggest-focus
```

The focus suggestion uses **Amdahl's law impact scoring** (inspired by AutoKernel's
multi-kernel orchestrator). Instead of just picking the lowest-scoring file, it
computes `impact = usage × failure_bonus × headroom × responsiveness × recency`:

- **Usage weight**: Trajectory frequency — optimize agents used most often
- **Failure bonus**: Agents with high real-world failure rates get priority
- **Headroom**: Room to improve (1.0 - structural_score)
- **Responsiveness**: Files that responded to past evolution get boosted; stuck files get deprioritized
- **Recency**: Files tried in the last 3 generations get penalized to avoid re-grinding

A file used 40% of the time with 20% headroom has **more impact** than a rarely-used
file with 50% headroom — the Amdahl's law insight.

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

## Evolution Memory (structured insights from retro)
{contents of ~/.claude/ultragent/evolution_memory.json, if it exists}
If empty: "No evolution memory yet. Run /ultragent retro after 5+ generations."
This contains per-file insights, strategy win rates, and extracted facts.
Use file_insights for your focus file to understand what has been tried.

## Current Genome: {focus_file}
{full contents of the focus file from parent snapshot}

NOTE: All content above is PRE-LOADED. Do NOT waste tool calls re-reading these files.
The focus file is RIGHT HERE. program.md is RIGHT HERE. Read only files NOT listed above.

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

### Step 4b: Validate Output + Error Recovery (DeerFlow + AI Scientist Pattern)

After MetaAgent completes, validate with multi-level checks:

```bash
# Level 1: Does the file exist and is it non-empty?
test -s $WORK_DIR/<focus_file_path> && echo "EXISTS" || echo "MISSING_OR_EMPTY"

# Level 2: Is it different from parent?
diff -q ~/.claude/ultragent/generations/<parent_id>/snapshot/<focus_file_path> $WORK_DIR/<focus_file_path> 2>/dev/null
# "Files differ" = good, "identical" or error = problem

# Level 3: Quick structural sanity (is it valid markdown with headings?)
head -5 $WORK_DIR/<focus_file_path>
grep -c "^#" $WORK_DIR/<focus_file_path>
```

**Failure classification and targeted recovery:**

| Symptom | Diagnosis | Recovery |
|---------|-----------|----------|
| File MISSING | Write tool used instead of Bash, or wrong path | Retry with explicit Bash heredoc and correct path |
| File EXISTS but EMPTY (0 bytes) | Heredoc delimiter was indented, or content had unquoted delimiter | Retry with different delimiter (`ULTRAEOF`) |
| File EXISTS but IDENTICAL to parent | MetaAgent made no changes (read-only mode bug) | Retry with sprint contract + explicit "you MUST modify the file" |
| File EXISTS, different, but NO headings | Content was corrupted or replaced with non-markdown | Retry with parent content + targeted patch |
| File EXISTS, different, has headings | SUCCESS — proceed to Step 5 | No retry needed |

**Error Recovery Protocol:**

For MISSING or EMPTY files:
- **Retry agent** (sonnet, 3 tool calls max):
  ```
  "File write failed for {focus_file}. The file is {MISSING|EMPTY}.

   Common causes:
   - Write/Edit tool was used instead of Bash (sandbox doesn't persist)
   - Heredoc delimiter collision (content contained ENDOFFILE)
   - Wrong path (check $WORK_DIR is correct)

   Parent content: {first 50 lines of parent file}
   Sprint contract changes: {contract hypothesis + planned changes}

   Write the COMPLETE modified file using:
   cat > {exact_path} << 'ULTRAEOF'
   {content here}
   ULTRAEOF

   Then verify: wc -l {exact_path}"
  ```

For IDENTICAL files (no changes made):
- **Retry agent** (sonnet, 3 tool calls max):
  ```
  "MetaAgent read but did not modify {focus_file}. The file is identical to parent.

   You MUST make changes. The sprint contract proposed:
   {contract planned changes section}

   Current file ({N} lines): {full parent content}

   Apply the changes and write the COMPLETE file using:
   cat > {exact_path} << 'ULTRAEOF'
   {modified content}
   ULTRAEOF"
  ```

- If retry succeeds (file now different + non-empty) → continue to Step 5
- If retry also fails → DISCARD with specific reason:
  - "crashed: file missing after retry"
  - "crashed: file empty after retry"
  - "crashed: no changes after retry"
- **Max 1 retry per candidate** (diminishing returns for markdown)
- **Log crash type** in results.tsv description for retro pattern analysis

### Step 4c: Efficiency Check (DeerFlow Loop Detection)

After MetaAgent completes, optionally check for wasted tool calls by examining
the MetaAgent's output. If the agent:
- Read files that were already in context (the focus file, program.md)
- Made >8 tool calls for a single-file mutation
- Read the same file twice

Record this in the generation metadata for retro analysis:
```bash
# If MetaAgent used too many calls (detected from agent output)
# Add efficiency note to meta_reasoning.md
echo "## Efficiency Note" >> $WORK_DIR/meta_reasoning.md
echo "Tool calls observed: {N}. Budget: 12. Wasted: {list}" >> $WORK_DIR/meta_reasoning.md
```

This data feeds into future retro reports and evolution memory.

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

### Step 5b: Smoke Test (Agentic Researcher Tier 1 Gate)

Before the expensive LLM-Judge, run a quick sanity check:

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/evaluate.py smoke-test $NEXT_GEN
```

If smoke test **FAILS** (fatal issues like empty files, drastic truncation, destroyed structure):
- DISCARD immediately with reason from the smoke test output
- Skip LLM-Judge entirely (saves cost)
- Log as `crash` in results.tsv

If smoke test **PASSES** (possibly with warnings):
- Continue to Step 6
- Include any warnings in the judge context for awareness

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

### Step 8b: Stuck Recovery Check (codex-autoresearch PIVOT/REFINE)

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

### Step 9: Cleanup & Loop

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

## Retro — Automated Retrospective

```bash
PYTHONIOENCODING=utf-8 python ~/.claude/ultragent/ua.py retro
```

Analyzes evolution history and auto-updates program.md:
- **Score trend**: improving / flat / stalled
- **File performance**: which files respond to evolution, which are stuck
- **Strategy performance**: which competition strategies win most
- **Agent health**: trajectory-based failure rate analysis
- **Auto-generated sections in program.md**:
  - `## Evolution Status` — current best, keep rate, trend
  - `## What Works` — files with high keep rates
  - `## What Doesn't Work` — files that consistently fail to improve
- Saves full report to `retro_reports/retro_NNN.md`

Run after every 5-10 generations to close the learning loop.
