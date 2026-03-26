# Evolve Protocol — Steps 1-5

The core modify→verify→keep/discard loop. Read this when executing `/ultragent evolve`.

## Step 1: Read Research Directives

Read `${CLAUDE_SKILL_DIR}/../../program.md` — this contains current research priorities,
what to try, what NOT to try, and lessons learned.

## Step 2: Select Parent & Suggest Focus File (Amdahl's Law)

```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py select-parent
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py suggest-focus
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

## Step 3: Prepare Workspace

```bash
WORK_DIR=$(mktemp -d)
cp -r ${CLAUDE_SKILL_DIR}/../../generations/<parent_id>/snapshot/* "$WORK_DIR/"
```

## Step 4a: Analysis Loop — Investigate + Plan (DeepTutor Dual-Loop Pattern)

The **Analysis Loop** (Sonnet, cheap+fast) gathers evidence and produces TWO artifacts:
1. **investigation_report.md** — structured analysis of the focus file
2. **sprint_contract.md** — focused hypothesis + planned changes

This is DeepTutor's dual-loop pattern: separate evidence gathering from execution.
The Analysis Loop builds structured knowledge; the Edit Loop (Step 4) acts on it.

Spawn a **Sonnet agent** (`subagent_type: executor`, `model: sonnet`) with:

```
<system>
You are the Analyst in an UltrAgent evolution cycle. You perform TWO tasks:

TASK 1 — INVESTIGATE: Analyze the focus file and produce investigation_report.md
TASK 2 — PLAN: Based on your investigation, produce sprint_contract.md

You do NOT edit the focus file. You ONLY write these two analysis artifacts.

## Investigation Report Format (investigation_report.md)

```markdown
# Investigation Report: {focus_file}

## Current State Assessment
- Structural score: {from context}
- Trajectory failure rate: {from evolution memory}
- Prior evolution attempts: {count and outcomes from lessons}

## Evidence Gathered
### From Structural Analysis
- {list specific issues found in the file}

### From Evolution History
- {what has been tried before and why it succeeded/failed, with cite_ids}

### From Trajectory Data
- {real-world usage patterns, failure modes}

## Diagnosis
{1-2 sentences: what is the ROOT CAUSE of the file's current limitations?}

## Recommended Changes (ranked by expected impact)
1. {highest-impact change with rationale}
2. {second-highest}
3. {optional third}
```

## Sprint Contract Format (sprint_contract.md)

Use the standard sprint contract format from meta_prompt.md.
Base your hypothesis on the investigation report's diagnosis and #1 recommendation.

Write both files using Bash heredoc. You have 4 tool calls max.
</system>
```

The Analysis Loop receives the FULL context block (all sections). It outputs:
- `$WORK_DIR/investigation_report.md` — structured evidence (saved with generation artifacts)
- `$WORK_DIR/sprint_contract.md` — focused hypothesis from the investigation

**Validate**: Check that BOTH files exist and are non-empty.
If either is missing, retry once with: "Investigation report or contract was empty. Try again."

After validation, copy the investigation report to the generation directory:
```bash
cp $WORK_DIR/investigation_report.md ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/ 2>/dev/null
```

## Step 4: Run Editor (Time-Boxed, 5 Minutes)

The **Editor** (Opus, expensive+powerful) receives the sprint contract + focus file and executes the edit.
It does NOT re-analyze the full context — it trusts the Strategist's contract.

**Circuit Breaker Check (DeepTutor pattern):** Before spawning expensive agents, check API health:
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../circuit_breaker.py check opus
```
If circuit is OPEN (API down), pause the evolve loop and wait for recovery instead of burning budget.
After each agent call, record the outcome:
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../circuit_breaker.py record opus success
# or: record opus failure
```

Spawn an **Opus agent** (`subagent_type: executor`, `model: opus`).

**Spawn Depth Tracking (OpenClaw pattern):** Log both spawns for audit:
```bash
PYTHONIOENCODING=utf-8 python -c "
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude' / 'ultragent'))
from ua import spawn_log_entry
spawn_log_entry('$NEXT_GEN', 'strategist', 1, parent_role='orchestrator', model='sonnet')
spawn_log_entry('$NEXT_GEN', 'editor', 1, parent_role='orchestrator', model='opus', strategy='$STRATEGY')
"
```

Spawn hierarchy (updated):
```
Orchestrator (depth 0, SKILL.md evolve loop)
  ├── Strategist (depth 1, Sonnet, role=strategist) — generates sprint contract
  ├── Editor (depth 1, Opus, role=editor) — executes the edit
  ├── Competitor A (depth 1, Opus, role=competitor, strategy=simplifier)
  ├── Competitor B (depth 1, Opus, role=competitor, strategy=exemplifier)
  ├── Competitor C (depth 1, Opus, role=competitor, strategy=aligner)
  ├── Judge 1 (depth 1, Sonnet, role=judge, personality=behavioral)
  ├── Judge 2 (depth 1, Sonnet, role=judge, personality=quality)
  ├── Judge 3 (depth 1, Sonnet, role=judge, personality=philosophy)
  └── Retry (depth 2, Sonnet, role=retry) — only if Editor crashes
```

Max depth: 2. Leaf agents (judges, retry) CANNOT spawn further agents.

Context block:
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
{contents of ${CLAUDE_SKILL_DIR}/../../evolution_memory.json, if it exists}
If empty: "No evolution memory yet. Run /ultragent retro after 5+ generations."
This contains per-file insights, strategy win rates, and extracted facts.
Use file_insights for your focus file to understand what has been tried.

## Current Genome: {focus_file}
{full contents of the focus file from parent snapshot}

NOTE: All content above is PRE-LOADED. Do NOT waste tool calls re-reading these files.
The focus file is RIGHT HERE. program.md is RIGHT HERE. Read only files NOT listed above.

## Session Reflections (Agent Laboratory pattern: within-session learning)
{last 5 entries from session_reflections.jsonl, if any}
These are insights from SUCCESSFUL changes in THIS session. Build on what works.
If empty: "No reflections yet — first generation of this session."

## Iterations Left: {remaining_cycles}
</context>

<task>
Execute the sprint contract written by the Strategist.

Sprint contract: {contents of $WORK_DIR/sprint_contract.md}

Work in: {WORK_DIR}
The genome files are already there. Edit ONLY the focus file.
Follow the sprint contract's planned changes precisely.
After editing, write meta_reasoning.md in {WORK_DIR}.
Do NOT re-analyze the full context — the Strategist already did that.
Focus your budget on high-quality execution of the proposed changes.
</task>
```

**Context Budget (OpenClaw pattern):** For long evolve sessions, use the context engine
to assemble the `<context>` block within a token budget:

```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../context_engine.py assemble --model opus
```

This auto-compresses low-priority sections and drops optional content when budget is tight.
For standard single-cycle evolves, the full context dump above is fine. For `evolve 50+`,
use the context engine to prevent context drift.

Use `mode: "bypassPermissions"` — it's working on copies.

**IMPORTANT**: If focused_mutation is enabled, only send the focus file contents
to reduce context and cost. The MetaAgent should only modify that one file.

## Step 4b: Validate Output + Error Recovery (DeerFlow + AI Scientist Pattern)

After MetaAgent completes, validate with multi-level checks:

```bash
# Level 1: Does the file exist and is it non-empty?
test -s $WORK_DIR/<focus_file_path> && echo "EXISTS" || echo "MISSING_OR_EMPTY"

# Level 2: Is it different from parent?
diff -q ${CLAUDE_SKILL_DIR}/../../generations/<parent_id>/snapshot/<focus_file_path> $WORK_DIR/<focus_file_path> 2>/dev/null
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

## Step 4c: Efficiency Check (DeerFlow Loop Detection)

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

## Step 5: Capture Diff & Create Generation

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
mkdir -p ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/snapshot
cp -r $WORK_DIR/* ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/snapshot/ 2>/dev/null
cp $WORK_DIR/patch.diff ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/ 2>/dev/null
cp $WORK_DIR/meta_reasoning.md ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/ 2>/dev/null
```

## Step 5b: Smoke Test (Agentic Researcher Tier 1 Gate)

Before the expensive LLM-Judge, run a quick sanity check:

```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../evaluate.py smoke-test $NEXT_GEN
```

If smoke test **FAILS** (fatal issues like empty files, drastic truncation, destroyed structure):
- DISCARD immediately with reason from the smoke test output
- Skip LLM-Judge entirely (saves cost)
- Log as `crash` in results.tsv

If smoke test **PASSES** (possibly with warnings):
- Continue to Step 6
- Include any warnings in the judge context for awareness
