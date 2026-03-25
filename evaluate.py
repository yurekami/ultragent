#!/usr/bin/env python3
"""
UltrAgent Evaluation Harness

Three-tier evaluation:
1. Structural (automated, instant) — format, consistency, bloat detection
2. LLM-Judge (cheap, fast) — an evaluator agent scores the diff
3. Task-Based (expensive, high signal) — run actual coding tasks with modified config

Usage:
    python evaluate.py structural <gen_id>
    python evaluate.py prepare-judge <gen_id>   # Outputs context for LLM-judge
    python evaluate.py benchmark-list            # List available benchmarks
    python evaluate.py benchmark-prep <task_id>  # Prepare a benchmark task for execution
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ULTRAGENT_DIR = Path.home() / ".claude" / "ultragent"
GENERATIONS_DIR = ULTRAGENT_DIR / "generations"
BENCHMARKS_DIR = ULTRAGENT_DIR / "benchmarks"


# ─── Structural Evaluation ───────────────────────────────────────────────────

def eval_structural(gen_id: str) -> dict:
    """
    Automated structural scoring. Checks:
    - File sizes (penalize bloat)
    - Markdown validity
    - Internal consistency (cross-references)
    - Prompt quality heuristics
    """
    gen_dir = GENERATIONS_DIR / gen_id / "snapshot"
    if not gen_dir.exists():
        return {"error": f"snapshot not found for {gen_id}", "score": 0.0}

    results = {
        "files": {},
        "issues": [],
        "warnings": [],
        "metrics": {},
    }

    total_score = 0.0
    file_count = 0

    for root, _dirs, files in os.walk(gen_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue

            fpath = Path(root) / fname
            rel = fpath.relative_to(gen_dir).as_posix()
            content = fpath.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
            file_score = 1.0
            file_issues = []

            # ── Size checks ──
            if len(lines) > 800:
                file_issues.append(f"too long ({len(lines)} lines, max 800)")
                file_score -= 0.15
            elif len(lines) > 500:
                file_issues.append(f"getting long ({len(lines)} lines)")
                file_score -= 0.05

            if len(content) < 50:
                file_issues.append("suspiciously short")
                file_score -= 0.2

            # ── Structure checks ──
            has_heading = bool(re.search(r"^#\s+", content, re.MULTILINE))
            if not has_heading:
                file_issues.append("no top-level heading")
                file_score -= 0.1

            # ── Quality heuristics ──
            # Penalize vague instructions
            vague_patterns = [
                r"\bdo your best\b",
                r"\btry to\b",
                r"\bif possible\b",
                r"\bmaybe\b.*\bshould\b",
            ]
            vague_count = sum(
                len(re.findall(p, content, re.IGNORECASE))
                for p in vague_patterns
            )
            if vague_count > 3:
                file_issues.append(f"too many vague instructions ({vague_count})")
                file_score -= 0.1

            # Penalize excessive repetition
            sentences = re.split(r"[.!?]\s+", content)
            if len(sentences) > 10:
                unique_ratio = len(set(sentences)) / len(sentences)
                if unique_ratio < 0.7:
                    file_issues.append(f"high repetition (unique ratio: {unique_ratio:.2f})")
                    file_score -= 0.1

            # Reward specificity (concrete examples, code blocks)
            code_blocks = len(re.findall(r"```", content))
            tables = len(re.findall(r"\|.*\|.*\|", content))
            examples = code_blocks + tables
            if examples > 0:
                file_score = min(file_score + 0.05, 1.0)

            file_score = max(0, min(1, file_score))
            results["files"][rel] = {
                "lines": len(lines),
                "size_bytes": len(content.encode("utf-8")),
                "score": round(file_score, 3),
                "issues": file_issues,
            }
            results["issues"].extend(f"{rel}: {i}" for i in file_issues)
            total_score += file_score
            file_count += 1

    # ── Cross-file consistency ──
    # Check that agent names referenced in CLAUDE.md exist as agent files
    claude_md = gen_dir / "CLAUDE.md"
    if claude_md.exists():
        claude_content = claude_md.read_text(encoding="utf-8")
        agent_dir = gen_dir / "agents"
        if agent_dir.exists():
            existing_agents = {f.stem for f in agent_dir.glob("*.md")}
            # Find agent references in CLAUDE.md
            referenced = set(re.findall(r"`(\w[\w-]+)`", claude_content))
            # Check if referenced names that look like agents exist
            for ref in referenced:
                if ref in existing_agents:
                    continue  # Good, exists
                # Don't flag non-agent references
                if ref.endswith("-reviewer") or ref.endswith("-guide") or ref.endswith("-runner"):
                    if ref not in existing_agents:
                        results["warnings"].append(f"CLAUDE.md references agent '{ref}' but no agents/{ref}.md found")

    # ── Aggregate ──
    avg_score = round(total_score / file_count, 4) if file_count > 0 else 0
    issue_penalty = min(len(results["issues"]) * 0.02, 0.3)
    final_score = round(max(0, avg_score - issue_penalty), 4)

    results["metrics"] = {
        "file_count": file_count,
        "avg_file_score": avg_score,
        "issue_count": len(results["issues"]),
        "warning_count": len(results["warnings"]),
        "final_score": final_score,
    }

    return results


# ─── LLM-Judge Context Preparation ──────────────────────────────────────────

def prepare_judge_context(gen_id: str) -> str:
    """
    Prepare the context for LLM-judge evaluation.
    Returns a formatted string to be sent to an evaluator agent.
    """
    gen_dir = GENERATIONS_DIR / gen_id

    parts = []
    parts.append(f"# Generation {gen_id} — Evaluation Context\n")

    # Patch
    patch_file = gen_dir / "patch.diff"
    if patch_file.exists():
        patch = patch_file.read_text(encoding="utf-8")
        parts.append("## Changes (Patch)\n```diff\n" + patch + "\n```\n")
    else:
        parts.append("## Changes\nNo patch file found (this may be the initial generation).\n")

    # MetaAgent reasoning
    reasoning_file = gen_dir / "meta_reasoning.md"
    if reasoning_file.exists():
        reasoning = reasoning_file.read_text(encoding="utf-8")
        parts.append("## MetaAgent Reasoning\n" + reasoning + "\n")

    # Structural scores
    scores_file = gen_dir / "scores.json"
    if scores_file.exists():
        scores = json.loads(scores_file.read_text(encoding="utf-8"))
        parts.append("## Current Scores\n```json\n" + json.dumps(scores, indent=2) + "\n```\n")

    # Parent info
    meta_file = gen_dir / "metadata.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        parent_id = meta.get("parent_gen_id")
        if parent_id:
            parent_scores_file = GENERATIONS_DIR / parent_id / "scores.json"
            if parent_scores_file.exists():
                parent_scores = json.loads(parent_scores_file.read_text(encoding="utf-8"))
                parts.append("## Parent Scores (" + parent_id + ")\n```json\n" + json.dumps(parent_scores, indent=2) + "\n```\n")

    # Snapshot summary (file list with sizes)
    snapshot_dir = gen_dir / "snapshot"
    if snapshot_dir.exists():
        parts.append("## Genome Files\n")
        for root, _dirs, files in os.walk(snapshot_dir):
            for fname in sorted(files):
                fpath = Path(root) / fname
                rel = fpath.relative_to(snapshot_dir).as_posix()
                size = fpath.stat().st_size
                parts.append(f"- `{rel}` ({size} bytes)")
        parts.append("")

    # Evaluation rubric with skeptical calibration and few-shot examples
    parts.append("""## Evaluation Rubric

You are a SKEPTICAL evaluator. Your job is to find what's WRONG, not praise what's right.
Assume every change is mediocre until proven otherwise. Surface-level improvements
(adding headings, reformatting, boilerplate additions) deserve LOW improvement scores.
Only genuine behavioral improvements (concrete examples that change how an agent acts,
specific guidance that prevents real failure modes, simplification that removes bloat
while preserving value) deserve HIGH scores.

If you catch yourself wanting to give 8+ on improvement, stop and ask: "Would this
change actually make a human developer's experience with this agent measurably better?"
If you can't point to a specific scenario where the change helps, score lower.

Score each dimension 0-10:

### Clarity (0-10, weight 0.20)
Would an agent know EXACTLY what to do in ambiguous situations? Vague advice like
"follow best practices" scores 3-4. Concrete decision trees score 7-8. Tested
behavioral specifications with edge cases score 9-10.

### Specificity (0-10, weight 0.15)
Count the concrete examples. Zero examples = max 4. One example = max 6.
Multiple examples across different scenarios = 7-8. Examples that demonstrate
the BOUNDARY between correct and incorrect behavior = 9-10.

### Actionability (0-10, weight 0.15)
Can an agent act on these instructions WITHOUT asking clarifying questions?
If any instruction requires interpretation or judgment calls not covered by
the prompt, deduct points. "Handle errors appropriately" = 3. "Catch specific
error types X, Y, Z and respond with..." = 8.

### Consistency (0-10, weight 0.20)
Does this file contradict any other file in the genome? Does it reference
tools, patterns, or conventions that conflict with CLAUDE.md or rules/*.md?
Even subtle contradictions (e.g., agent says "use arrow functions" while
coding-style.md says "prefer function keyword") = deduct 2-3 points.

### Coverage (0-10, weight 0.15)
What failure modes are NOT addressed? What scenarios would cause the agent
to do the wrong thing? Every unaddressed gap = deduct 1 point. Having a
Failure_Modes_To_Avoid section doesn't count unless the failure modes are
SPECIFIC to this agent's domain, not generic.

### Conciseness (0-10, weight 0.05)
Is every sentence load-bearing? Could you delete any paragraph without
losing information? Boilerplate that restates what the agent would do
anyway = deduct points. A file that grew 2x but only added 1.2x information
scores poorly here.

### Improvement Over Parent (0-10, weight 0.10)
THE HARDEST CRITERION. Score 5 = "changes are roughly neutral."
Below 5 = regression. Above 5 = genuine improvement.
Adding a heading with no other changes = 5.
Adding generic boilerplate = 4.
Adding concrete, actionable examples that change agent behavior = 7-8.
Removing bloat while preserving or improving quality = 8-9.
Fundamental restructuring that makes the agent measurably better = 9-10.

## Calibration Examples

### Example A: WEAK improvement (score 0.52)
A generation that added a top-level heading and reformatted XML tags to markdown,
but made no substantive content changes:
```json
{
  "clarity": 6, "specificity": 4, "actionability": 5, "consistency": 6,
  "coverage": 5, "conciseness": 7, "improvement": 4,
  "aggregate": 0.5200,
  "reasoning": "Changes are cosmetic only. The heading fixes a structural issue but the agent's actual behavior would be identical. No new examples, no new failure modes, no improved specificity. The improvement score reflects that formatting changes don't change outcomes."
}
```

### Example B: MODERATE improvement (score 0.72)
A generation that added 3 concrete before/after code examples and a Philosophy
section aligning with the user's coding principles, but also increased file
length by 90% and introduced some redundancy with existing principles:
```json
{
  "clarity": 8, "specificity": 8, "actionability": 8, "consistency": 8,
  "coverage": 7, "conciseness": 5, "improvement": 7,
  "aggregate": 0.7350,
  "reasoning": "The examples are genuinely useful and would change agent behavior. The Philosophy section creates alignment with project values. However, the file nearly doubled in length while some additions restate what the Core_Principles already covered. Conciseness suffers. A tighter version with the same examples in half the lines would score higher."
}
```

### Example C: STRONG improvement (score 0.88)
A generation that replaced vague instructions with a concrete decision tree,
added a failure mode that prevents a real class of errors, removed 30 lines of
boilerplate while preserving all information, and simplified the output format:
```json
{
  "clarity": 9, "specificity": 9, "actionability": 9, "consistency": 9,
  "coverage": 8, "conciseness": 9, "improvement": 9,
  "aggregate": 0.8850,
  "reasoning": "Every change is load-bearing. The decision tree replaces ambiguity with specificity. The new failure mode addresses a real pattern seen in prior runs. The boilerplate removal makes the prompt tighter without losing information. This is a net simplification that also improves quality -- the ideal outcome."
}
```

### Aggregate Score
Weighted average mapped to 0-1 scale:
sum(dimension_score * weight) / 10

Weights: Clarity=0.20, Specificity=0.15, Actionability=0.15, Consistency=0.20, Coverage=0.15, Conciseness=0.05, Improvement=0.10

Output as JSON:
```json
{
  "clarity": N,
  "specificity": N,
  "actionability": N,
  "consistency": N,
  "coverage": N,
  "conciseness": N,
  "improvement": N,
  "aggregate": N.NNNN,
  "reasoning": "Brief explanation of scores",
  "suggestions": ["Suggestion 1", "Suggestion 2"]
}
```
""")

    return "\n".join(parts)


# ─── Benchmark System ────────────────────────────────────────────────────────

def list_benchmarks() -> list[dict]:
    """List available benchmark tasks."""
    manifest_file = BENCHMARKS_DIR / "manifest.json"
    if not manifest_file.exists():
        return []
    return json.loads(manifest_file.read_text(encoding="utf-8")).get("tasks", [])


def prepare_benchmark(task_id: str) -> dict | None:
    """Load and prepare a benchmark task for execution."""
    task_dir = BENCHMARKS_DIR / task_id
    task_file = task_dir / "task.json"
    if not task_file.exists():
        return None

    task = json.loads(task_file.read_text(encoding="utf-8"))

    # Load setup files
    setup_dir = task_dir / "setup"
    setup_files = {}
    if setup_dir.exists():
        for fpath in setup_dir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(setup_dir).as_posix()
                setup_files[rel] = fpath.read_text(encoding="utf-8", errors="replace")

    # Load test files
    test_dir = task_dir / "tests"
    test_files = {}
    if test_dir.exists():
        for fpath in test_dir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(test_dir).as_posix()
                test_files[rel] = fpath.read_text(encoding="utf-8", errors="replace")

    # Load rubric
    rubric_file = task_dir / "rubric.json"
    rubric = None
    if rubric_file.exists():
        rubric = json.loads(rubric_file.read_text(encoding="utf-8"))

    return {
        "task_id": task_id,
        "task": task,
        "setup_files": setup_files,
        "test_files": test_files,
        "rubric": rubric,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="UltrAgent Evaluation Harness")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("structural", help="Run structural evaluation")
    s.add_argument("gen_id")

    j = sub.add_parser("prepare-judge", help="Prepare LLM-judge context")
    j.add_argument("gen_id")

    sub.add_parser("benchmark-list", help="List benchmarks")

    bp = sub.add_parser("benchmark-prep", help="Prepare benchmark task")
    bp.add_argument("task_id")

    args = parser.parse_args()

    if args.command == "structural":
        result = eval_structural(args.gen_id)
        print(json.dumps(result, indent=2))

    elif args.command == "prepare-judge":
        ctx = prepare_judge_context(args.gen_id)
        print(ctx)

    elif args.command == "benchmark-list":
        tasks = list_benchmarks()
        if not tasks:
            print("No benchmarks found.")
        else:
            for t in tasks:
                print(f"  {t['id']:30s}  {t.get('type', '?'):15s}  {t.get('description', '')}")

    elif args.command == "benchmark-prep":
        result = prepare_benchmark(args.task_id)
        if result:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Benchmark task '{args.task_id}' not found.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
