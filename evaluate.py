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

            # ── Commandment-based quality checks ──
            # Anti-genie: penalize patterns that invite shortcut behavior
            antigenie_patterns = [
                r"\b(?:ts-ignore|@ts-ignore|eslint-disable)\b",
                r"\bskip\s+(?:test|check|validation)\b",
                r"\bignore\s+(?:error|warning|failure)\b",
                r"\bany\b\s*(?:type|cast)\b",
            ]
            antigenie_count = sum(
                len(re.findall(p, content, re.IGNORECASE))
                for p in antigenie_patterns
            )
            if antigenie_count > 0:
                file_issues.append(f"anti-genie violation: {antigenie_count} shortcut patterns")
                file_score -= 0.15 * min(antigenie_count, 3)

            # Verification-first: reward explicit verification/testing references
            verification_patterns = [
                r"\bverif(?:y|ied|ication)\b",
                r"\bvalidat(?:e|ion|ed)\b",
                r"\btest(?:ing|s|ed)?\b.*\bbefore\b",
                r"\bcheck\b.*\bbefore\b(?:.*\b(?:commit|push|deploy|claim)\b)",
                r"\bevidence\b",
            ]
            verification_count = sum(
                1 for p in verification_patterns
                if re.search(p, content, re.IGNORECASE)
            )
            if verification_count >= 3:
                file_score = min(file_score + 0.08, 1.0)
            elif verification_count >= 1:
                file_score = min(file_score + 0.03, 1.0)

            # Error recovery: reward explicit "when X fails, do Y" patterns
            recovery_patterns = [
                r"\bwhen\b.*\bfail",
                r"\bif\b.*\berror\b.*\bthen\b",
                r"\bfallback\b",
                r"\brecover(?:y|ing)?\b",
                r"\bretry\b.*\bstrateg",
                r"\bon\s+failure\b",
            ]
            recovery_count = sum(
                1 for p in recovery_patterns
                if re.search(p, content, re.IGNORECASE)
            )
            if recovery_count >= 2:
                file_score = min(file_score + 0.05, 1.0)

            # Promise inflation: penalize overclaiming without evidence
            overclaim_patterns = [
                r"\balways\s+(?:ensures?|guarantees?|works?)\b",
                r"\bnever\s+fails?\b",
                r"\bperfect(?:ly)?\s+(?:handles?|solves?)\b",
                r"\b100%\s+(?:accurate|reliable|safe)\b",
            ]
            overclaim_count = sum(
                len(re.findall(p, content, re.IGNORECASE))
                for p in overclaim_patterns
            )
            if overclaim_count > 2:
                file_issues.append(f"promise inflation ({overclaim_count} overclaims)")
                file_score -= 0.05

            # Immutability awareness: reward mentioning immutable patterns
            immutability_patterns = [
                r"\bimmutab(?:le|ility)\b",
                r"\bnew\s+object\b",
                r"\bspread\s+operator\b",
                r"\bdon'?t\s+mutat",
                r"\bnever\s+mutat",
                r"\bpure\s+function",
            ]
            immutability_count = sum(
                1 for p in immutability_patterns
                if re.search(p, content, re.IGNORECASE)
            )
            if immutability_count >= 1:
                file_score = min(file_score + 0.02, 1.0)

            # Decision trees: reward structured decision guidance
            decision_patterns = [
                r"\bif\b.*\bthen\b.*\belse\b",
                r"\bwhen\s+to\s+use\b",
                r"\bchoose\b.*\bbased\s+on\b",
                r"(?:flowchart|decision\s+tree|checklist)",
            ]
            decision_count = sum(
                1 for p in decision_patterns
                if re.search(p, content, re.IGNORECASE)
            )
            if decision_count >= 2:
                file_score = min(file_score + 0.03, 1.0)

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


# ─── Smoke Test (Agentic Researcher pattern: Tier 1 crash check) ──────────

def eval_smoke_test(gen_id: str) -> dict:
    """
    Quick sanity check before expensive LLM-Judge evaluation.
    Catches obviously broken candidates: empty files, drastic truncation,
    destroyed structure, introduced syntax issues.

    Returns:
        {"passed": bool, "issues": [...], "warnings": [...]}

    A generation that fails the smoke test should be DISCARDED immediately
    without wasting an LLM-Judge call.
    """
    gen_dir = GENERATIONS_DIR / gen_id
    snapshot_dir = gen_dir / "snapshot"
    issues = []
    warnings = []

    if not snapshot_dir.exists():
        return {"passed": False, "issues": ["snapshot directory missing"], "warnings": []}

    # Get parent for comparison
    meta_file = gen_dir / "metadata.json"
    parent_id = None
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        parent_id = meta.get("parent_gen_id")

    parent_snapshot = GENERATIONS_DIR / parent_id / "snapshot" if parent_id else None

    for root, _dirs, files in os.walk(snapshot_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue

            fpath = Path(root) / fname
            rel = fpath.relative_to(snapshot_dir).as_posix()
            content = fpath.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")

            # ── Fatal checks (instant discard) ──

            # Empty or near-empty file
            if len(content.strip()) < 20:
                issues.append(f"{rel}: file is empty or near-empty ({len(content)} chars)")
                continue

            # File is just whitespace/newlines
            if not content.strip():
                issues.append(f"{rel}: file contains only whitespace")
                continue

            # ── Parent comparison checks ──
            if parent_snapshot and parent_snapshot.exists():
                parent_file = parent_snapshot / rel
                if parent_file.exists():
                    parent_content = parent_file.read_text(encoding="utf-8", errors="replace")
                    parent_lines = parent_content.split("\n")

                    # Drastic truncation: lost >60% of content
                    if len(parent_lines) > 10:
                        ratio = len(lines) / len(parent_lines)
                        if ratio < 0.4:
                            issues.append(
                                f"{rel}: drastic truncation ({len(parent_lines)} -> {len(lines)} lines, "
                                f"{ratio:.0%} of original)"
                            )
                        elif ratio < 0.6:
                            warnings.append(
                                f"{rel}: significant reduction ({len(parent_lines)} -> {len(lines)} lines)"
                            )

                    # Content completely replaced (less than 10% overlap)
                    if len(parent_lines) > 20 and len(lines) > 20:
                        parent_set = set(l.strip() for l in parent_lines if l.strip())
                        child_set = set(l.strip() for l in lines if l.strip())
                        if parent_set:
                            overlap = len(parent_set & child_set) / len(parent_set)
                            if overlap < 0.10:
                                issues.append(
                                    f"{rel}: content completely replaced "
                                    f"({overlap:.0%} overlap with parent)"
                                )
                            elif overlap < 0.25:
                                warnings.append(
                                    f"{rel}: major rewrite ({overlap:.0%} overlap with parent)"
                                )

            # ── Structural integrity checks ──

            # Lost all markdown structure
            has_any_heading = bool(re.search(r"^#+\s+", content, re.MULTILINE))
            if not has_any_heading and len(lines) > 10:
                issues.append(f"{rel}: no markdown headings found in {len(lines)}-line file")

            # Unclosed code blocks (odd number of ```)
            fence_count = content.count("```")
            if fence_count % 2 != 0:
                warnings.append(f"{rel}: unclosed code fence ({fence_count} markers)")

            # Corrupted content: high ratio of non-printable characters
            printable = sum(1 for c in content if c.isprintable() or c in '\n\r\t')
            if len(content) > 0:
                printable_ratio = printable / len(content)
                if printable_ratio < 0.9:
                    issues.append(
                        f"{rel}: possibly corrupted ({printable_ratio:.0%} printable)"
                    )

            # Extreme bloat: more than 3x parent size
            if parent_snapshot and parent_snapshot.exists():
                parent_file = parent_snapshot / rel
                if parent_file.exists():
                    parent_size = len(parent_file.read_text(encoding="utf-8", errors="replace"))
                    if parent_size > 100 and len(content) > parent_size * 3:
                        warnings.append(
                            f"{rel}: extreme bloat ({len(content)} vs parent {parent_size} chars)"
                        )

    passed = len(issues) == 0
    return {"passed": passed, "issues": issues, "warnings": warnings}


# ─── Preference Pairs (Scientific Taste pattern) ────────────────────────────

PREFERENCES_FILE = ULTRAGENT_DIR / "preference_pairs.jsonl"


def preference_record(
    winner_id: str, loser_id: str, focus_file: str,
    reason: str, confidence: str, source: str = "keep_discard",
) -> dict:
    """Record a preference pair from a keep/discard decision or evaluation."""
    entry = {
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "winner": winner_id,
        "loser": loser_id,
        "focus_file": focus_file,
        "reason": reason[:500],
        "confidence": confidence,
        "source": source,
    }
    with open(PREFERENCES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def preferences_read(last_n: int | None = None) -> list[dict]:
    """Read preference pairs."""
    if not PREFERENCES_FILE.exists():
        return []
    entries = []
    for line in PREFERENCES_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            entries.append(json.loads(line))
    return entries[-last_n:] if last_n else entries


def preferences_for_file(focus_file: str) -> list[dict]:
    """Get preference pairs involving a specific file."""
    return [p for p in preferences_read() if p.get("focus_file") == focus_file]


# ─── Pairwise LLM-Judge (Scientific Taste approach) ─────────────────────────

def prepare_judge_context(gen_id: str) -> str:
    """
    Prepare pairwise comparison context for the LLM-Judge.
    Instead of rubric scoring, the judge compares parent vs child directly.
    Inspired by 'AI Can Learn Scientific Taste' (RLCF).
    """
    gen_dir = GENERATIONS_DIR / gen_id
    parts = []

    parts.append(f"# Pairwise Evaluation: {gen_id}\n")

    # Get parent ID
    parent_id = None
    meta_file = gen_dir / "metadata.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        parent_id = meta.get("parent_gen_id")

    if not parent_id:
        parts.append("No parent — this is the initial generation. Score absolutely.\n")
        return "\n".join(parts)

    # Sprint contract (if exists)
    contract_file = gen_dir / "sprint_contract.md"
    if contract_file.exists():
        contract = contract_file.read_text(encoding="utf-8")
        parts.append("## Sprint Contract (MetaAgent's proposal)\n" + contract + "\n")

    # Patch
    patch_file = gen_dir / "patch.diff"
    if patch_file.exists():
        patch = patch_file.read_text(encoding="utf-8")
        parts.append("## Diff (changes from parent to child)\n```diff\n" + patch[:5000] + "\n```\n")

    # MetaAgent reasoning
    reasoning_file = gen_dir / "meta_reasoning.md"
    if reasoning_file.exists():
        reasoning = reasoning_file.read_text(encoding="utf-8")
        parts.append("## MetaAgent's Self-Assessment\n" + reasoning[:2000] + "\n")

    # Structural scores comparison
    parent_struct = gen_dir_score(parent_id)
    child_struct = gen_dir_score(gen_id)
    parts.append(f"## Structural Scores\n- Parent ({parent_id}): {parent_struct}\n- Child ({gen_id}): {child_struct}\n")

    # Dynamic few-shot from preference history
    prefs = preferences_read(last_n=5)
    if prefs:
        parts.append("## Prior Preference Decisions (from evolution history)\n")
        parts.append("These are REAL keep/discard decisions from prior generations:\n")
        for p in prefs:
            parts.append(f"- **{p['winner']} > {p['loser']}** on `{p.get('focus_file','')}`: {p.get('reason','')[:120]}")
        parts.append("")

    # The pairwise evaluation protocol
    parts.append("""## Pairwise Evaluation Protocol

You are comparing **Version A (parent)** vs **Version B (child)** of an agent prompt file.
Your job is to determine: is the child BETTER than the parent?

Read BOTH versions of the changed file. Then reason step by step:

### Step 1: What changed?
List every concrete difference. Ignore whitespace/formatting.

### Step 2: For each change, ask:
- Does this change how the agent would BEHAVE in a real scenario?
- Can you name a SPECIFIC situation where this change helps?
- Does this change CONTRADICT anything else in the config?
- Is this change LOAD-BEARING or just cosmetic?

### Step 3: Weigh the evidence
- Changes that alter behavior in real scenarios = strong signal
- Cosmetic changes (headings, formatting) = weak signal
- Simplification (fewer lines, same information) = positive signal
- Bloat (more lines, redundant information) = negative signal
- New failure modes addressed = strong positive
- Contradictions introduced = strong negative

### Step 4: Form preference
Would you rather have an agent following Version A or Version B?

### Position-Swap Check
After forming your preference, mentally swap the labels.
If you called them "B vs A" instead of "A vs B", would your preference change?
If yes, you have position bias — correct for it.

## Output Format

Return ONLY this JSON:
```json
{
  "preferred": "child" or "parent",
  "confidence": "high" or "medium" or "low",
  "score_delta": N.NN,
  "key_reason": "One sentence: WHY the preferred version is better",
  "behavioral_changes": ["change 1 that affects behavior", "change 2"],
  "cosmetic_changes": ["change that doesn't affect behavior"],
  "regressions": ["anything that got worse"],
  "position_swap_consistent": true or false,
  "suggestions": ["what would make the child even better"]
}
```

### score_delta Guide
- `-0.5 to -0.1`: child is WORSE (regression)
- `-0.1 to 0.0`: child is slightly worse or neutral
- `0.0`: no meaningful difference
- `0.0 to 0.1`: marginal improvement (cosmetic only)
- `0.1 to 0.3`: moderate improvement (some behavioral changes)
- `0.3 to 0.5`: strong improvement (clear behavioral uplift)
- `0.5+`: exceptional (fundamental quality leap)

Be skeptical. Most changes are in the 0.0 to 0.2 range.
A score_delta > 0.3 requires extraordinary evidence.
""")

    return "\n".join(parts)


def gen_dir_score(gen_id: str) -> str:
    """Get a compact score string for a generation."""
    scores_file = GENERATIONS_DIR / gen_id / "scores.json"
    if scores_file.exists():
        s = json.loads(scores_file.read_text(encoding="utf-8"))
        return f"structural={s.get('structural', '?')}, llm_judge={s.get('llm_judge', '?')}, aggregate={s.get('aggregate', '?')}"
    return "no scores"


def compute_pairwise_aggregate(
    structural_score: float, judge_result: dict, parent_aggregate: float,
) -> float:
    """
    Compute aggregate score from structural + pairwise judge result.
    The pairwise judge returns a score_delta, not an absolute score.
    New aggregate = parent_aggregate + (delta adjusted by confidence).
    """
    delta = judge_result.get("score_delta", 0)
    confidence = judge_result.get("confidence", "medium")
    preferred = judge_result.get("preferred", "parent")

    # Confidence multiplier
    conf_mult = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(confidence, 0.5)

    # If parent is preferred, delta should be negative
    if preferred == "parent" and delta > 0:
        delta = -abs(delta)
    elif preferred == "child" and delta < 0:
        delta = abs(delta)

    # Apply confidence-adjusted delta
    adjusted_delta = delta * conf_mult

    # Structural contribution (small weight, absolute)
    # Blend: 80% preference-based, 20% structural
    pref_score = parent_aggregate + adjusted_delta
    final = 0.8 * pref_score + 0.2 * structural_score

    return round(max(0, min(1, final)), 4)


# ─── Ensemble Evaluation (AI Scientist pattern) ─────────────────────────────

def ensemble_aggregate(judge_results: list[dict]) -> dict:
    """
    Aggregate multiple independent pairwise judge results via majority vote.
    Inspired by AI Scientist's ensemble of 5 reviewers (we use 3).

    Args:
        judge_results: List of pairwise judge outputs, each with:
            preferred, confidence, score_delta, key_reason

    Returns:
        Aggregated result with majority preference, mean delta, and individual votes.
    """
    if not judge_results:
        return {"preferred": "parent", "confidence": "low", "score_delta": 0,
                "key_reason": "no judges", "ensemble_size": 0}

    # Count votes
    child_votes = sum(1 for j in judge_results if j.get("preferred") == "child")
    parent_votes = len(judge_results) - child_votes
    majority = "child" if child_votes > parent_votes else "parent"

    # Aggregate deltas (normalize: parent-preferred deltas should be negative)
    deltas = []
    for j in judge_results:
        d = j.get("score_delta", 0)
        if j.get("preferred") == "parent" and d > 0:
            d = -abs(d)
        elif j.get("preferred") == "child" and d < 0:
            d = abs(d)
        deltas.append(d)

    mean_delta = sum(deltas) / len(deltas) if deltas else 0

    # Aggregate confidence: unanimous = high, majority = medium, split = low
    if child_votes == len(judge_results) or parent_votes == len(judge_results):
        agg_confidence = "high"
    elif abs(child_votes - parent_votes) >= 2:
        agg_confidence = "medium"
    else:
        agg_confidence = "medium"  # 2-1 split is still medium

    # Collect reasons
    reasons = [j.get("key_reason", "") for j in judge_results if j.get("key_reason")]
    majority_reasons = [r for j, r in zip(judge_results, reasons)
                        if j.get("preferred") == majority]

    # Check agreement on regressions
    all_regressions = []
    for j in judge_results:
        all_regressions.extend(j.get("regressions", []))

    return {
        "preferred": majority,
        "confidence": agg_confidence,
        "score_delta": round(mean_delta, 4),
        "key_reason": majority_reasons[0] if majority_reasons else (reasons[0] if reasons else ""),
        "ensemble_size": len(judge_results),
        "votes": {"child": child_votes, "parent": parent_votes},
        "individual_deltas": deltas,
        "unanimous": child_votes == len(judge_results) or parent_votes == len(judge_results),
        "regressions": list(set(all_regressions)),
        "suggestions": list(set(
            s for j in judge_results for s in j.get("suggestions", [])
        ))[:5],
    }


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

    sm = sub.add_parser("smoke-test", help="Quick sanity check before LLM-judge")
    sm.add_argument("gen_id")

    j = sub.add_parser("prepare-judge", help="Prepare LLM-judge context")
    j.add_argument("gen_id")

    sub.add_parser("benchmark-list", help="List benchmarks")

    bp = sub.add_parser("benchmark-prep", help="Prepare benchmark task")
    bp.add_argument("task_id")

    args = parser.parse_args()

    if args.command == "structural":
        result = eval_structural(args.gen_id)
        print(json.dumps(result, indent=2))

    elif args.command == "smoke-test":
        result = eval_smoke_test(args.gen_id)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"Smoke test: {status}")
        for issue in result["issues"]:
            print(f"  [FATAL] {issue}")
        for warning in result["warnings"]:
            print(f"  [WARN]  {warning}")
        if result["passed"] and not result["warnings"]:
            print("  All checks passed. Safe to proceed to LLM-Judge.")

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
