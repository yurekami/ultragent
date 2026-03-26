#!/usr/bin/env python3
"""
UltrAgent for Claude Code — Archive & Generation Manager
Integrates Meta FAIR's UltrAgent + Karpathy's AutoResearch patterns.

Usage:
    python ua.py init                    # Initialize (genome + frontier + results.tsv)
    python ua.py status                  # Show archive + frontier summary
    python ua.py select-parent [strat]   # Pick next parent generation
    python ua.py create-gen <parent> <patch_file> <scores_json> [reasoning_file]
    python ua.py keep <gen_id>           # Accept: commit to frontier, log keep
    python ua.py discard <gen_id> [reason] # Reject: log discard, don't touch frontier
    python ua.py promote <gen_id>        # Apply generation to live ~/.claude/
    python ua.py rollback                # Revert to previous promoted generation
    python ua.py frontier                # Show git frontier (accepted improvements only)
    python ua.py results [N]             # Show last N results from results.tsv
    python ua.py suggest-focus [gen_id]  # Suggest which file to mutate next
    python ua.py lineage [gen_id]        # Show evolution tree
    python ua.py diff <gen_id>           # Show generation's patch
    python ua.py archive                 # Dump full archive
    python ua.py score <gen_id>          # Run structural scoring on a generation
    python ua.py snapshot [dest]         # Snapshot current genome
    python ua.py capture <agent> <outcome> <description>  # Capture a trajectory
    python ua.py trajectories [N]        # Show recent trajectories
    python ua.py queue-evolve <agent_file> [reason]       # Queue an auto-evolve
    python ua.py pending-evolves         # Show queued evolves
    python ua.py drain-queue             # Process and clear the evolve queue
"""

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── Paths ───────────────────────────────────────────────────────────────────

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
ULTRAGENT_DIR = CLAUDE_DIR / "ultragent"
ARCHIVE_FILE = ULTRAGENT_DIR / "archive.jsonl"
METADATA_FILE = ULTRAGENT_DIR / "metadata.json"
GENERATIONS_DIR = ULTRAGENT_DIR / "generations"
BENCHMARKS_DIR = ULTRAGENT_DIR / "benchmarks"
CONFIG_FILE = ULTRAGENT_DIR / "config.json"
FRONTIER_DIR = ULTRAGENT_DIR / "frontier"
RESULTS_FILE = ULTRAGENT_DIR / "results.tsv"
PROGRAM_FILE = ULTRAGENT_DIR / "program.md"
TRAJECTORIES_FILE = ULTRAGENT_DIR / "trajectories.jsonl"
EVOLVE_QUEUE_FILE = ULTRAGENT_DIR / "evolve_queue.jsonl"
LESSONS_FILE = ULTRAGENT_DIR / "lessons.jsonl"
RETRO_DIR = ULTRAGENT_DIR / "retro_reports"
EVOLUTION_MEMORY_FILE = ULTRAGENT_DIR / "evolution_memory.json"

# ─── Genome Definition ───────────────────────────────────────────────────────

GENOME_INCLUDE = [
    "agents/*.md",
    "rules/*.md",
    "skills/omc-learned/*/SKILL.md",
    "CLAUDE.md",
]

GENOME_EXCLUDE = [
    "settings.json",
    "settings.local.json",
    "credentials*",
    "*.key",
    "*.pem",
    "projects/",
    "ultragent/",
    "memory/",
    "statsig/",
]


# ─── Utilities ───────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def genome_files() -> list[Path]:
    """Collect all genome files from ~/.claude/ matching include patterns."""
    import fnmatch as _fnmatch
    files = []
    for pattern in GENOME_INCLUDE:
        matched = glob.glob(str(CLAUDE_DIR / pattern), recursive=True)
        files.extend(Path(p) for p in matched)

    result = []
    for f in sorted(set(files)):
        rel = f.relative_to(CLAUDE_DIR).as_posix()
        excluded = False
        for exc in GENOME_EXCLUDE:
            if exc.endswith("/"):
                if rel.startswith(exc) or rel.startswith(exc.rstrip("/")):
                    excluded = True
                    break
            elif "*" in exc:
                if _fnmatch.fnmatch(rel, exc):
                    excluded = True
                    break
            elif rel == exc or rel.startswith(exc + "/"):
                excluded = True
                break
        if not excluded:
            result.append(f)
    return result


def snapshot_genome(dest_dir: Path) -> dict[str, str]:
    """Copy genome files to dest_dir. Returns file hashes."""
    hashes = {}
    for src in genome_files():
        rel = src.relative_to(CLAUDE_DIR)
        dst = dest_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        hashes[rel.as_posix()] = file_hash(src)
    return hashes


def restore_genome(src_dir: Path) -> list[str]:
    """Restore genome files from snapshot to ~/.claude/."""
    restored = []
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            src = Path(root) / fname
            rel = src.relative_to(src_dir)
            dst = CLAUDE_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel.as_posix())
    return restored


def compute_diff(parent_dir: Path, child_dir: Path) -> str:
    """Compute unified diff between two genome snapshots."""
    import difflib
    diffs = []
    all_files = set()
    for d in (parent_dir, child_dir):
        if d.exists():
            for root, _dirs, files in os.walk(d):
                for f in files:
                    rel = Path(root, f).relative_to(d).as_posix()
                    all_files.add(rel)

    for rel in sorted(all_files):
        pf = parent_dir / rel
        cf = child_dir / rel
        pl = pf.read_text(encoding="utf-8").splitlines(keepends=True) if pf.exists() else []
        cl = cf.read_text(encoding="utf-8").splitlines(keepends=True) if cf.exists() else []
        if pl != cl:
            diff = difflib.unified_diff(pl, cl, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
            diffs.append("\n".join(diff))
    return "\n\n".join(diffs) if diffs else ""


def _git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command, return stdout. Raises on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd or FRONTIER_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 and "nothing to commit" not in result.stdout:
        # Don't fail on "nothing to commit"
        if result.returncode != 0 and "nothing to commit" not in result.stderr:
            pass  # Non-fatal for many git commands
    return result.stdout.strip()


# ─── Archive ─────────────────────────────────────────────────────────────────

def archive_read() -> list[dict]:
    if not ARCHIVE_FILE.exists():
        return []
    entries = []
    for line in ARCHIVE_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            entries.append(json.loads(line))
    return entries


def archive_append(entry: dict) -> None:
    with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def archive_best(entries: list[dict] | None = None) -> dict | None:
    entries = entries or archive_read()
    valid = [e for e in entries if e.get("valid", True)]
    return max(valid, key=lambda e: e.get("score", 0)) if valid else None


def archive_get(gen_id: str, entries: list[dict] | None = None) -> dict | None:
    entries = entries or archive_read()
    for e in entries:
        if e["gen_id"] == gen_id:
            return e
    return None


# ─── Metadata ────────────────────────────────────────────────────────────────

def metadata_read() -> dict:
    if not METADATA_FILE.exists():
        return {}
    return json.loads(METADATA_FILE.read_text(encoding="utf-8"))


def metadata_write(data: dict) -> None:
    METADATA_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "max_generations": 50,
    "parent_selection_strategy": "score_child_prop",
    "meta_agent_model": "opus",
    "eval_agent_model": "sonnet",
    "meta_agent_max_tool_calls": 40,
    "auto_promote_threshold": None,
    "time_budget_seconds": 300,
    "focused_mutation": True,
    "single_score_mode": True,
    "scoring_weights": {
        "structural": 0.2,
        "llm_judge": 0.5,
        "task_based": 0.3,
    },
    "genome_include": GENOME_INCLUDE,
    "genome_exclude": GENOME_EXCLUDE,
}


def config_read() -> dict:
    if not CONFIG_FILE.exists():
        return {**DEFAULT_CONFIG}
    stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {**DEFAULT_CONFIG, **stored}


def config_write(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ─── Results TSV (AutoResearch pattern: log EVERYTHING) ─────────────────────

RESULTS_HEADER = [
    "timestamp", "gen_id", "parent_id", "status", "score", "best_score",
    "focus_file", "files_changed", "duration_s", "description",
]


def results_init() -> None:
    """Create results.tsv with header if missing."""
    if not RESULTS_FILE.exists():
        with open(RESULTS_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(RESULTS_HEADER)


def results_log(entry: dict) -> None:
    """Append a row to results.tsv. Entry keys should match RESULTS_HEADER."""
    results_init()
    row = [entry.get(h, "") for h in RESULTS_HEADER]
    with open(RESULTS_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(row)


def results_read(last_n: int | None = None) -> list[dict]:
    """Read results.tsv, optionally last N entries."""
    if not RESULTS_FILE.exists():
        return []
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
    if last_n:
        rows = rows[-last_n:]
    return rows


# ─── Git Frontier (AutoResearch pattern: only accepted improvements) ────────

def frontier_init(initial_snapshot_dir: Path) -> None:
    """Initialize git repo in frontier/ with initial genome snapshot."""
    genome_dir = FRONTIER_DIR / "genome"
    genome_dir.mkdir(parents=True, exist_ok=True)

    # Copy initial genome
    if initial_snapshot_dir.exists():
        shutil.copytree(initial_snapshot_dir, genome_dir, dirs_exist_ok=True)

    # Init git repo
    _git(["init"], cwd=FRONTIER_DIR)
    _git(["add", "."], cwd=FRONTIER_DIR)
    _git(["commit", "-m", "initial: baseline genome snapshot"], cwd=FRONTIER_DIR)
    print("  Frontier: git repo initialized")


def frontier_commit(gen_id: str, score: float, description: str = "") -> None:
    """Commit current frontier state as an accepted improvement."""
    _git(["add", "."], cwd=FRONTIER_DIR)
    msg = f"{gen_id}: score={score:.4f}"
    if description:
        msg += f" | {description}"
    _git(["commit", "-m", msg], cwd=FRONTIER_DIR)


def frontier_update(snapshot_dir: Path) -> None:
    """Replace frontier genome with new snapshot."""
    genome_dir = FRONTIER_DIR / "genome"
    # Remove old genome files
    if genome_dir.exists():
        shutil.rmtree(genome_dir, ignore_errors=True)
    genome_dir.mkdir(parents=True, exist_ok=True)
    # Copy new genome
    shutil.copytree(snapshot_dir, genome_dir, dirs_exist_ok=True)


def frontier_reset() -> None:
    """Reset frontier to previous commit (discard latest)."""
    _git(["reset", "--hard", "HEAD~1"], cwd=FRONTIER_DIR)


def frontier_log(n: int = 20) -> str:
    """Show git frontier log."""
    return _git(["log", "--oneline", f"-{n}"], cwd=FRONTIER_DIR)


def frontier_diff_last() -> str:
    """Show diff of last accepted change."""
    return _git(["diff", "HEAD~1", "HEAD"], cwd=FRONTIER_DIR)


def frontier_best_score() -> float:
    """Extract best score from latest frontier commit message."""
    msg = _git(["log", "--oneline", "-1"], cwd=FRONTIER_DIR)
    # Parse "gen_XXXX: score=0.1234 | ..."
    if "score=" in msg:
        try:
            score_part = msg.split("score=")[1].split()[0].split("|")[0].strip()
            return float(score_part)
        except (IndexError, ValueError):
            pass
    return 0.0


# ─── Structural Scoring ─────────────────────────────────────────────────────

def score_structural(gen_dir: Path) -> dict[str, Any]:
    """Automated structural scoring with per-file breakdown."""
    import re
    snapshot_dir = gen_dir / "snapshot"
    if not snapshot_dir.exists():
        return {"error": "no snapshot directory", "score": 0.0}

    file_scores = {}
    issues = []
    total_score = 0.0
    file_count = 0

    for root, _dirs, files in os.walk(snapshot_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = Path(root) / fname
            rel = fpath.relative_to(snapshot_dir).as_posix()
            content = fpath.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
            fs = 1.0
            fi = []

            # Size
            if len(lines) > 800:
                fi.append(f"exceeds 800 lines ({len(lines)})")
                fs -= 0.15
            elif len(lines) > 500:
                fi.append(f"getting long ({len(lines)} lines)")
                fs -= 0.05
            if len(content) < 50:
                fi.append("suspiciously short")
                fs -= 0.2

            # Structure
            if not re.search(r"^#\s+", content, re.MULTILINE):
                fi.append("no top-level heading")
                fs -= 0.1

            # Quality heuristics
            vague = sum(len(re.findall(p, content, re.IGNORECASE)) for p in [
                r"\bdo your best\b", r"\btry to\b", r"\bif possible\b", r"\bmaybe\b.*\bshould\b",
            ])
            if vague > 3:
                fi.append(f"vague instructions ({vague})")
                fs -= 0.1

            # Specificity reward
            code_blocks = len(re.findall(r"```", content))
            tables = len(re.findall(r"\|.*\|.*\|", content))
            if code_blocks + tables > 0:
                fs = min(fs + 0.05, 1.0)

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
                fi.append(f"anti-genie violation: {antigenie_count} shortcut patterns")
                fs -= 0.15 * min(antigenie_count, 3)

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
                fs = min(fs + 0.08, 1.0)
            elif verification_count >= 1:
                fs = min(fs + 0.03, 1.0)

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
                fs = min(fs + 0.05, 1.0)

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
                fi.append(f"promise inflation ({overclaim_count} overclaims)")
                fs -= 0.05

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
                fs = min(fs + 0.02, 1.0)

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
                fs = min(fs + 0.03, 1.0)

            fs = max(0, min(1, fs))
            file_scores[rel] = {"score": round(fs, 3), "lines": len(lines), "issues": fi}
            issues.extend(f"{rel}: {i}" for i in fi)
            total_score += fs
            file_count += 1

    avg = round(total_score / file_count, 4) if file_count > 0 else 0
    penalty = min(len(issues) * 0.02, 0.3)
    final = round(max(0, avg - penalty), 4)

    return {
        "files": file_scores,
        "issues": issues,
        "metrics": {
            "file_count": file_count,
            "avg_file_score": avg,
            "issue_count": len(issues),
            "final_score": final,
        },
    }


# ─── Focused Mutation (AutoKernel Amdahl's Law Orchestration) ────────────────

def _compute_file_impact(
    rel_path: str,
    structural_score: float,
    traj_summary: dict,
    evo_memory: dict,
    recent_focus_files: set,
) -> dict:
    """
    Compute Amdahl-style impact score for a genome file.

    Impact = usage_weight × headroom × responsiveness_bonus × recency_factor

    - usage_weight: how often this agent is actually used (from trajectories)
    - headroom: how much room to improve (1.0 - structural_score)
    - responsiveness_bonus: does this file respond to evolution? (from evo memory)
    - recency_factor: penalize files that were just tried (avoid re-grinding)

    Inspired by AutoKernel's orchestrate.py which uses Amdahl's law to prioritize
    kernels by (fraction_of_total_time × improvement_potential).
    """
    # ── Usage weight (trajectory frequency, normalized) ──
    # Higher = agent is used more often = changes here have more real-world impact
    traj = traj_summary.get(rel_path, {})
    traj_total = traj.get("total", 0)
    traj_failures = traj.get("failure", 0) + traj.get("correction", 0)

    # Normalize: max across all agents sets the scale
    all_totals = [v.get("total", 0) for v in traj_summary.values()] if traj_summary else [0]
    max_total = max(all_totals) if all_totals else 1

    if traj_total > 0 and max_total > 0:
        usage_weight = traj_total / max_total
    else:
        # No trajectory data: use a neutral weight (don't penalize, don't boost)
        usage_weight = 0.5

    # Bonus for high failure rate — these agents NEED improvement most
    failure_rate = traj_failures / traj_total if traj_total > 0 else 0
    failure_bonus = 1.0 + (failure_rate * 0.5)  # up to 1.5x for 100% failure rate

    # ── Headroom (1.0 - score = room to improve) ──
    headroom = max(0.05, 1.0 - structural_score)  # floor at 0.05 to avoid zero

    # ── Responsiveness (from evolution memory) ──
    file_insights = evo_memory.get("file_insights", {})
    insight = file_insights.get(rel_path, {})
    responsiveness = insight.get("responsiveness", 0.5)  # default neutral
    total_attempts = insight.get("total_attempts", 0)

    if total_attempts >= 4 and responsiveness == 0:
        # File is stuck — heavy penalty, don't waste more cycles
        responsiveness_factor = 0.1
    elif total_attempts >= 2 and responsiveness > 0.5:
        # File responds well — boost
        responsiveness_factor = 1.0 + (responsiveness * 0.3)
    elif total_attempts == 0:
        # Never tried — neutral with slight exploration bonus
        responsiveness_factor = 1.1
    else:
        responsiveness_factor = 0.5 + responsiveness

    # ── Recency penalty ──
    if rel_path in recent_focus_files:
        recency_factor = 0.3  # heavily penalize just-tried files
    else:
        recency_factor = 1.0

    # ── Composite impact score ──
    impact = usage_weight * failure_bonus * headroom * responsiveness_factor * recency_factor

    return {
        "file": rel_path,
        "impact_score": round(impact, 4),
        "structural_score": structural_score,
        "headroom": round(headroom, 3),
        "usage_weight": round(usage_weight, 3),
        "failure_bonus": round(failure_bonus, 3),
        "failure_rate": round(failure_rate, 3),
        "responsiveness": round(responsiveness_factor, 3),
        "recency_factor": recency_factor,
        "traj_total": traj_total,
        "traj_failures": traj_failures,
        "evo_attempts": total_attempts,
    }


def suggest_focus_file(gen_id: str = "initial") -> dict:
    """
    Suggest which file to focus mutation on using Amdahl's law impact scoring.

    Unlike the naive 'lowest structural score' approach, this considers:
    - Usage frequency (trajectory data) — optimize what's used most
    - Failure rate — prioritize agents that fail in production
    - Structural headroom — room to improve
    - Evolution responsiveness — skip files stuck at local optima
    - Recency — avoid re-grinding the same file

    Returns {file, score, impact_score, reason, all_ranked}
    """
    gen_dir = GENERATIONS_DIR / gen_id
    struct = score_structural(gen_dir)
    file_scores = struct.get("files", {})

    if not file_scores:
        return {"file": None, "score": 0, "reason": "no files found"}

    # Gather signals from trajectories and evolution memory
    traj_summary = trajectories_summary()
    evo_memory = evolution_memory_read()

    # Recent focus files (last 3 results) to avoid re-grinding
    recent_results = results_read(3)
    recent_focus_files = {r.get("focus_file", "") for r in recent_results if r.get("focus_file")}

    # Compute impact for every file
    impact_entries = []
    for rel_path, info in file_scores.items():
        entry = _compute_file_impact(
            rel_path=rel_path,
            structural_score=info["score"],
            traj_summary=traj_summary,
            evo_memory=evo_memory,
            recent_focus_files=recent_focus_files,
        )
        entry["issues"] = info.get("issues", [])
        entry["lines"] = info.get("lines", 0)
        impact_entries.append(entry)

    # Sort by impact_score descending (highest impact first)
    impact_entries.sort(key=lambda x: x["impact_score"], reverse=True)

    best = impact_entries[0]

    # Build reason string
    reason_parts = []
    if best["traj_total"] > 0:
        reason_parts.append(
            f"usage={best['usage_weight']:.0%} ({best['traj_total']} trajectories, "
            f"{best['failure_rate']:.0%} failure rate)"
        )
    reason_parts.append(f"headroom={best['headroom']:.0%}")
    if best["evo_attempts"] > 0:
        reason_parts.append(f"responsiveness={best['responsiveness']:.2f} ({best['evo_attempts']} prior attempts)")
    if best["recency_factor"] < 1.0:
        reason_parts.append("recently tried (penalized)")
    if best["issues"]:
        reason_parts.append(f"issues: {', '.join(best['issues'][:2])}")

    return {
        "file": best["file"],
        "score": best["structural_score"],
        "impact_score": best["impact_score"],
        "reason": "; ".join(reason_parts),
        "all_ranked": [
            {
                "file": e["file"],
                "impact": e["impact_score"],
                "score": e["structural_score"],
                "usage": e["usage_weight"],
                "headroom": e["headroom"],
            }
            for e in impact_entries[:8]
        ],
    }


# ─── Trajectories (Hermes-inspired: capture real usage data) ─────────────────

def trajectory_capture(
    agent_file: str,
    outcome: str,
    description: str,
    task_type: str = "",
    user_correction: str = "",
    error_output: str = "",
) -> dict:
    """
    Record an agent usage trajectory. Called during normal Claude Code sessions
    when an agent succeeds, fails, or gets corrected by the user.

    Args:
        agent_file: Which agent file was involved (e.g., "agents/executor.md")
        outcome: "success" | "failure" | "correction" | "retry"
        description: What happened (1-2 sentences)
        task_type: Category of task (code_gen, debug, review, plan, etc.)
        user_correction: What the user said to correct the agent (if applicable)
        error_output: Error message or failure output (truncated)
    """
    entry = {
        "timestamp": now_iso(),
        "agent_file": agent_file,
        "outcome": outcome,
        "description": description[:500],
        "task_type": task_type,
        "user_correction": user_correction[:500],
        "error_output": error_output[:1000],
    }
    with open(TRAJECTORIES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def trajectories_read(last_n: int | None = None) -> list[dict]:
    """Read trajectories, optionally last N."""
    if not TRAJECTORIES_FILE.exists():
        return []
    entries = []
    for line in TRAJECTORIES_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            entries.append(json.loads(line))
    return entries[-last_n:] if last_n else entries


def trajectories_for_agent(agent_file: str) -> list[dict]:
    """Get all trajectories for a specific agent file."""
    return [t for t in trajectories_read() if t.get("agent_file") == agent_file]


def trajectories_summary() -> dict:
    """Summarize trajectories by agent and outcome."""
    all_t = trajectories_read()
    summary: dict[str, dict[str, int]] = {}
    for t in all_t:
        agent = t.get("agent_file", "unknown")
        outcome = t.get("outcome", "unknown")
        summary.setdefault(agent, {"success": 0, "failure": 0, "correction": 0, "retry": 0, "total": 0})
        summary[agent][outcome] = summary[agent].get(outcome, 0) + 1
        summary[agent]["total"] += 1
    return summary


# ─── Auto-Evolve Queue (Hermes-inspired: evolve when it matters) ────────────

def queue_evolve(agent_file: str, reason: str = "", priority: int = 1) -> dict:
    """
    Queue an evolve cycle targeting a specific agent file.
    Called by hooks when failure patterns are detected.
    """
    entry = {
        "timestamp": now_iso(),
        "agent_file": agent_file,
        "reason": reason[:500],
        "priority": priority,  # 1=normal, 2=high (repeated failures), 3=urgent (user correction)
        "status": "pending",
    }
    with open(EVOLVE_QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def pending_evolves() -> list[dict]:
    """Read all pending evolve queue entries."""
    if not EVOLVE_QUEUE_FILE.exists():
        return []
    entries = []
    for line in EVOLVE_QUEUE_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            e = json.loads(line)
            if e.get("status") == "pending":
                entries.append(e)
    return entries


def drain_queue() -> list[dict]:
    """Mark all pending entries as drained and return them."""
    if not EVOLVE_QUEUE_FILE.exists():
        return []
    entries = []
    updated = []
    for line in EVOLVE_QUEUE_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            e = json.loads(line)
            if e.get("status") == "pending":
                entries.append(e)
                e["status"] = "drained"
                e["drained_at"] = now_iso()
            updated.append(e)
    EVOLVE_QUEUE_FILE.write_text(
        "\n".join(json.dumps(e, default=str) for e in updated) + "\n",
        encoding="utf-8",
    )
    return entries


def evolve_targets_from_queue() -> list[dict]:
    """
    Analyze the queue and trajectories to determine which files to evolve.
    Returns ranked list: [{file, reason, priority, trajectory_count, failure_rate}]
    """
    queued = pending_evolves()
    summary = trajectories_summary()

    # Merge queue entries and trajectory data
    targets: dict[str, dict] = {}
    for q in queued:
        f = q["agent_file"]
        targets.setdefault(f, {"file": f, "reasons": [], "priority": 0, "queued": 0})
        targets[f]["reasons"].append(q.get("reason", ""))
        targets[f]["priority"] = max(targets[f]["priority"], q.get("priority", 1))
        targets[f]["queued"] += 1

    # Enrich with trajectory data
    for f, info in targets.items():
        ts = summary.get(f, {})
        info["trajectory_count"] = ts.get("total", 0)
        info["failure_count"] = ts.get("failure", 0) + ts.get("correction", 0)
        total = ts.get("total", 1)
        info["failure_rate"] = round(info["failure_count"] / total, 2) if total > 0 else 0

    # Sort by priority desc, then failure rate desc
    ranked = sorted(targets.values(), key=lambda x: (x["priority"], x["failure_rate"]), reverse=True)
    return ranked


# ─── Lessons (MetaClaw pattern: what NOT to do and why) ──────────────────────

def lesson_record(
    gen_id: str, focus_file: str, outcome: str, strategy: str, lesson: str,
) -> dict:
    """Record a lesson from a keep/discard decision."""
    entry = {
        "timestamp": now_iso(),
        "gen_id": gen_id,
        "focus_file": focus_file,
        "outcome": outcome,
        "strategy": strategy,
        "lesson": lesson[:500],
    }
    with open(LESSONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def lessons_read() -> list[dict]:
    if not LESSONS_FILE.exists():
        return []
    entries = []
    for line in LESSONS_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            entries.append(json.loads(line))
    return entries


def lessons_for_file(focus_file: str) -> list[dict]:
    """Get lessons relevant to a specific file — injected into MetaAgent context."""
    return [l for l in lessons_read() if l.get("focus_file") == focus_file]


def auto_extract_lesson(gen_id: str, entry: dict, outcome: str, reason: str = "") -> dict | None:
    """Auto-extract a lesson from a keep/discard decision using reasoning + contract."""
    focus_file = entry.get("focus_file", "")
    strategy = entry.get("strategy", "")
    gen_dir = GENERATIONS_DIR / gen_id

    # Build lesson from available artifacts
    parts = []

    # From sprint contract
    contract_file = gen_dir / "sprint_contract.md"
    if contract_file.exists():
        contract = contract_file.read_text(encoding="utf-8")
        # Extract hypothesis
        for line in contract.split("\n"):
            if line.strip() and not line.startswith("#") and len(line.strip()) > 20:
                parts.append(f"Approach: {line.strip()[:200]}")
                break

    # From meta reasoning
    reasoning_file = gen_dir / "meta_reasoning.md"
    if reasoning_file.exists():
        reasoning = reasoning_file.read_text(encoding="utf-8")[:300]
        parts.append(f"Changes: {reasoning.split(chr(10))[0][:200]}")

    if outcome == "keep":
        parts.append(f"Result: KEPT. This approach worked.")
    else:
        parts.append(f"Result: DISCARDED. Reason: {reason}")

    lesson_text = " | ".join(parts) if parts else f"{outcome}: {reason}"

    return lesson_record(gen_id, focus_file, outcome, strategy, lesson_text)


def update_program_lessons() -> None:
    """Update program.md Lessons Learned section with recent lessons."""
    if not PROGRAM_FILE.exists():
        return

    lessons = lessons_read()
    if not lessons:
        return

    content = PROGRAM_FILE.read_text(encoding="utf-8")

    # Build lessons text
    lesson_lines = []
    for l in lessons[-10:]:  # Last 10 lessons
        icon = "+" if l.get("outcome") == "keep" else "-"
        lesson_lines.append(
            f"- [{icon}] `{l.get('gen_id', '?')}` on `{l.get('focus_file', '?')}`: "
            f"{l.get('lesson', '')[:150]}"
        )
    new_section = "\n".join(lesson_lines)

    # Replace the Lessons Learned section
    marker = "## Lessons Learned"
    if marker in content:
        # Find the section and replace everything until the next ## or end
        idx = content.index(marker)
        # Find next section
        rest = content[idx + len(marker):]
        next_section = rest.find("\n## ")
        if next_section == -1:
            # Last section — replace to end
            content = content[:idx] + f"{marker}\n\n{new_section}\n"
        else:
            content = content[:idx] + f"{marker}\n\n{new_section}\n" + rest[next_section:]

        PROGRAM_FILE.write_text(content, encoding="utf-8")


# ─── Skills Registry (Hive pattern: reusable improvement patterns) ───────────

SKILLS_FILE = ULTRAGENT_DIR / "skills.jsonl"


def skill_register(
    pattern_name: str,
    description: str,
    score_delta: float,
    gen_id: str,
    focus_file: str,
    strategy: str = "",
) -> dict:
    """Register a proven improvement pattern from a KEPT generation."""
    entry = {
        "timestamp": now_iso(),
        "pattern_name": pattern_name,
        "description": description[:500],
        "score_delta": round(score_delta, 4),
        "gen_id": gen_id,
        "focus_file": focus_file,
        "strategy": strategy,
        "times_applied": 1,
    }
    with open(SKILLS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def skills_read() -> list[dict]:
    """Read all registered skills."""
    if not SKILLS_FILE.exists():
        return []
    entries = []
    for line in SKILLS_FILE.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            entries.append(json.loads(line))
    return entries


def skills_ranked() -> list[dict]:
    """Return skills sorted by score_delta (best first)."""
    return sorted(skills_read(), key=lambda s: s.get("score_delta", 0), reverse=True)


def auto_extract_skill(gen_id: str, score_delta: float) -> dict | None:
    """
    Auto-extract a skill from a KEPT generation.
    Reads the meta_reasoning.md and sprint_contract.md to determine the pattern.
    """
    gen_dir = GENERATIONS_DIR / gen_id
    entry = archive_get(gen_id)
    if not entry:
        return None

    focus_file = entry.get("focus_file", "")

    # Try to extract pattern from sprint contract
    contract_file = gen_dir / "sprint_contract.md"
    reasoning_file = gen_dir / "meta_reasoning.md"

    description = ""
    pattern_name = "unknown"

    if contract_file.exists():
        contract = contract_file.read_text(encoding="utf-8")
        # Extract hypothesis line
        for line in contract.split("\n"):
            if line.strip().startswith("##") and "Hypothesis" in line:
                continue
            if line.strip() and "hypothesis" not in line.lower() and "##" not in line:
                description = line.strip()
                break
        # Infer pattern name from planned changes
        text = contract.lower()
        if "simplif" in text or "compress" in text or "remov" in text or "reduc" in text:
            pattern_name = "simplify"
        elif "example" in text or "before/after" in text or "concrete" in text:
            pattern_name = "add_examples"
        elif "align" in text or "philosophy" in text or "claude.md" in text:
            pattern_name = "align_philosophy"
        elif "heading" in text:
            pattern_name = "fix_structure"
        elif "failure" in text or "error" in text:
            pattern_name = "add_failure_modes"
        else:
            pattern_name = "improve"
    elif reasoning_file.exists():
        description = reasoning_file.read_text(encoding="utf-8")[:300]

    if not description:
        description = entry.get("description", f"Improvement to {focus_file}")

    return skill_register(
        pattern_name=pattern_name,
        description=description,
        score_delta=score_delta,
        gen_id=gen_id,
        focus_file=focus_file,
        strategy=entry.get("strategy", ""),
    )


# ─── Evolution Memory (DeerFlow-inspired structured memory) ─────────────────

def evolution_memory_read() -> dict:
    """Read the structured evolution memory."""
    if not EVOLUTION_MEMORY_FILE.exists():
        return _evolution_memory_default()
    try:
        return json.loads(EVOLUTION_MEMORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return _evolution_memory_default()


def _evolution_memory_default() -> dict:
    """Default empty evolution memory schema."""
    return {
        "version": "1.0",
        "last_updated": None,
        "evolution_context": {
            "phase": "unknown",
            "score_trend": "unknown",
            "stalled_since": None,
            "best_score": 0,
            "best_gen_id": None,
            "total_generations": 0,
            "keep_rate": 0,
        },
        "file_insights": {},
        "strategy_insights": {},
        "facts": [],
    }


def evolution_memory_write(memory: dict) -> None:
    """Write the structured evolution memory."""
    memory["last_updated"] = now_iso()
    EVOLUTION_MEMORY_FILE.write_text(
        json.dumps(memory, indent=2, default=str),
        encoding="utf-8",
    )


def evolution_memory_update() -> dict:
    """
    Rebuild structured evolution memory from results, trajectories, lessons, archive.
    Called by cmd_retro after analysis. Returns the updated memory.
    """
    memory = _evolution_memory_default()
    meta = metadata_read()
    results = results_read()
    lessons = lessons_read()
    archive = archive_read()
    traj_summary = trajectories_summary()

    total = len(results)
    kept = [r for r in results if r.get("status") == "keep"]
    discarded = [r for r in results if r.get("status") == "discard"]
    crashed = [r for r in results if r.get("status") == "crash"]
    keep_rate = len(kept) / total if total > 0 else 0

    # Score trend detection
    best_scores = []
    for r in results:
        try:
            best_scores.append(float(r.get("best_score", 0)))
        except (ValueError, TypeError):
            pass

    score_trend = "unknown"
    stalled_since = None
    if len(best_scores) >= 3:
        if best_scores[-1] > best_scores[0]:
            score_trend = "improving"
        else:
            score_trend = "flat"
    if len(best_scores) >= 5:
        recent = best_scores[-3:]
        if all(abs(a - b) < 0.001 for a, b in zip(recent, recent[1:])):
            score_trend = "stalled"
            # Find when it stalled
            for i in range(len(best_scores) - 2, 0, -1):
                if abs(best_scores[i] - best_scores[-1]) > 0.001:
                    stalled_r = results[i] if i < len(results) else None
                    if stalled_r:
                        stalled_since = stalled_r.get("gen_id")
                    break

    # Phase detection from program.md
    phase = "unknown"
    if PROGRAM_FILE.exists():
        prog = PROGRAM_FILE.read_text(encoding="utf-8")
        if "Phase 1" in prog and "Phase 2" not in prog.split("Current")[0] if "Current" in prog else True:
            phase = "Phase 1: Low-Hanging Fruit"
        elif "Phase 2" in prog:
            phase = "Phase 2: Prompt Quality"
        elif "Phase 3" in prog:
            phase = "Phase 3: Architecture"

    memory["evolution_context"] = {
        "phase": phase,
        "score_trend": score_trend,
        "stalled_since": stalled_since,
        "best_score": meta.get("best_score", 0),
        "best_gen_id": meta.get("best_gen_id"),
        "total_generations": meta.get("total_generations", 0),
        "keep_rate": round(keep_rate, 3),
    }

    # ── File insights ──
    file_stats: dict[str, dict] = {}
    for r in results:
        f = r.get("focus_file", "")
        if not f:
            continue
        file_stats.setdefault(f, {"kept": 0, "discarded": 0, "crashed": 0, "scores": []})
        status = r.get("status", "")
        if status == "keep":
            file_stats[f]["kept"] += 1
        elif status == "discard":
            file_stats[f]["discarded"] += 1
        elif status == "crash":
            file_stats[f]["crashed"] += 1
        try:
            file_stats[f]["scores"].append(float(r.get("score", 0)))
        except (ValueError, TypeError):
            pass

    for f, stats in file_stats.items():
        total_f = stats["kept"] + stats["discarded"] + stats["crashed"]
        avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
        responsiveness = stats["kept"] / total_f if total_f > 0 else 0

        # Find best strategy for this file from archive
        file_strategies: dict[str, int] = {}
        for e in archive:
            if e.get("focus_file") == f and e.get("status") == "keep":
                s = e.get("strategy", "unknown")
                file_strategies[s] = file_strategies.get(s, 0) + 1
        best_strategy = max(file_strategies, key=file_strategies.get) if file_strategies else None

        # Find last improvement gen
        last_improved = None
        for e in reversed(archive):
            if e.get("focus_file") == f and e.get("status") == "keep":
                last_improved = e.get("gen_id")
                break

        # Collect known issues from lessons
        known_issues = []
        for l in lessons:
            if l.get("focus_file") == f and l.get("outcome") == "discard":
                lesson_text = l.get("lesson", "")
                if len(lesson_text) > 20:
                    known_issues.append(lesson_text[:150])
        known_issues = known_issues[-3:]  # Keep last 3

        # Trajectory health
        traj = traj_summary.get(f, {})
        traj_total = traj.get("total", 0)
        traj_fails = traj.get("failure", 0) + traj.get("correction", 0)
        failure_rate = round(traj_fails / traj_total, 2) if traj_total > 0 else 0

        memory["file_insights"][f] = {
            "responsiveness": round(responsiveness, 3),
            "total_attempts": total_f,
            "kept": stats["kept"],
            "avg_score": round(avg_score, 4),
            "best_strategy": best_strategy,
            "last_improved_gen": last_improved,
            "known_issues": known_issues,
            "trajectory_failure_rate": failure_rate,
            "trajectory_total": traj_total,
        }

    # ── Strategy insights ──
    strategy_stats: dict[str, dict] = {}
    for e in archive:
        s = e.get("strategy", "unknown")
        if s == "unknown":
            continue
        strategy_stats.setdefault(s, {"kept": 0, "discarded": 0, "total": 0, "scores": [], "best_on": []})
        strategy_stats[s]["total"] += 1
        strategy_stats[s]["scores"].append(e.get("score", 0))
        if e.get("status") == "keep":
            strategy_stats[s]["kept"] += 1
            focus = e.get("focus_file", "")
            if focus and focus not in strategy_stats[s]["best_on"]:
                strategy_stats[s]["best_on"].append(focus)
        elif e.get("status") == "discard":
            strategy_stats[s]["discarded"] += 1

    for s, stats in strategy_stats.items():
        win_rate = stats["kept"] / stats["total"] if stats["total"] > 0 else 0
        avg = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
        memory["strategy_insights"][s] = {
            "win_rate": round(win_rate, 3),
            "total_attempts": stats["total"],
            "kept": stats["kept"],
            "avg_score": round(avg, 4),
            "best_on": stats["best_on"][:5],
        }

    # ── Facts (extracted from high-signal patterns) ──
    facts = []

    # Fact: strategy that never wins
    for s, stats in strategy_stats.items():
        if stats["total"] >= 5 and stats["kept"] == 0:
            facts.append({
                "content": f"Strategy '{s}' has never produced a kept generation ({stats['total']} attempts)",
                "category": "strategy",
                "confidence": 0.95,
                "source": "retro_analysis",
            })

    # Fact: file at local optimum
    for f, insight in memory["file_insights"].items():
        if insight["total_attempts"] >= 4 and insight["kept"] == 0:
            facts.append({
                "content": f"File '{f}' appears stuck — 0/{insight['total_attempts']} kept. May need radically different approach.",
                "category": "file_health",
                "confidence": 0.85,
                "source": "retro_analysis",
            })

    # Fact: score plateau
    if score_trend == "stalled" and stalled_since:
        facts.append({
            "content": f"Score has plateaued since {stalled_since}. Consider changing parent selection strategy or targeting different files.",
            "category": "evolution",
            "confidence": 0.9,
            "source": "retro_analysis",
        })

    # Fact: high-failure agents from trajectories
    for agent, counts in traj_summary.items():
        total_a = counts.get("total", 0)
        fails = counts.get("failure", 0) + counts.get("correction", 0)
        if total_a >= 5 and fails / total_a > 0.4:
            facts.append({
                "content": f"Agent '{agent}' has {fails}/{total_a} failure rate in production — high priority for evolution.",
                "category": "agent_health",
                "confidence": 0.9,
                "source": "trajectory_analysis",
            })

    # Fact: what works
    for f, insight in memory["file_insights"].items():
        if insight["responsiveness"] > 0.5 and insight["total_attempts"] >= 3:
            facts.append({
                "content": f"File '{f}' responds well to evolution ({insight['responsiveness']:.0%} keep rate, best via {insight['best_strategy']}).",
                "category": "file_health",
                "confidence": 0.85,
                "source": "retro_analysis",
            })

    memory["facts"] = facts

    evolution_memory_write(memory)
    return memory


# ─── Competition (Hive pattern: multi-agent population search) ───────────────

COMPETITION_STRATEGIES = [
    {
        "name": "simplifier",
        "directive": "Your strategy is SIMPLIFICATION. Remove lines while preserving all information. Compression is your goal. A shorter file with identical behavioral coverage is the ideal outcome. Do NOT add content.",
    },
    {
        "name": "exemplifier",
        "directive": "Your strategy is EXEMPLIFICATION. Add concrete before/after code examples that demonstrate the boundary between correct and incorrect agent behavior. Specificity is your goal. Every example must show a REAL scenario.",
    },
    {
        "name": "aligner",
        "directive": "Your strategy is ALIGNMENT. Connect this file to the user's CLAUDE.md philosophy (verification-first, immutability, anti-genie rules). Add concrete checks, decision trees, or failure modes that reference these principles. Consistency is your goal.",
    },
]


def get_competition_config() -> dict:
    """Get competition configuration."""
    cfg = config_read()
    comp = cfg.get("competition", {})
    return {
        "enabled": comp.get("enabled", True),
        "size": comp.get("size", 3),
        "strategies": comp.get("strategies", COMPETITION_STRATEGIES),
    }


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_init(_args: argparse.Namespace) -> None:
    """Initialize UltrAgent + AutoResearch integrated system."""
    print("Initializing UltrAgent x AutoResearch for Claude Code...")

    # Config
    if not CONFIG_FILE.exists():
        config_write(DEFAULT_CONFIG)
        print(f"  Config: {CONFIG_FILE}")

    # Results.tsv
    results_init()
    print(f"  Results log: {RESULTS_FILE}")

    # gen_initial
    gen_dir = GENERATIONS_DIR / "initial"
    snapshot_dir = gen_dir / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    hashes = snapshot_genome(snapshot_dir)
    print(f"  Captured {len(hashes)} genome files")

    # Structural score
    struct = score_structural(gen_dir)
    initial_score = struct.get("metrics", {}).get("final_score", 0)

    # Gen metadata
    (gen_dir / "metadata.json").write_text(json.dumps({
        "gen_id": "initial",
        "parent_gen_id": None,
        "created_at": now_iso(),
        "file_hashes": hashes,
    }, indent=2, default=str), encoding="utf-8")

    scores = {"structural": initial_score, "llm_judge": None, "task_based": None, "aggregate": initial_score}
    (gen_dir / "scores.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")

    # Archive
    ARCHIVE_FILE.write_text(json.dumps({
        "gen_id": "initial", "parent_gen_id": None, "score": initial_score,
        "scores": scores, "valid": True, "created_at": now_iso(),
        "promoted": True, "status": "keep", "patch_files_count": 0,
    }, default=str) + "\n", encoding="utf-8")

    # Metadata
    metadata_write({
        "current_gen_id": "initial",
        "promoted_gen_id": "initial",
        "best_gen_id": "initial",
        "best_score": initial_score,
        "total_generations": 1,
        "total_kept": 1,
        "total_discarded": 0,
        "total_crashed": 0,
        "next_gen_number": 1,
        "initialized_at": now_iso(),
    })

    # Git frontier
    frontier_init(snapshot_dir)

    # Log initial to results.tsv
    results_log({
        "timestamp": now_iso(),
        "gen_id": "initial",
        "parent_id": "",
        "status": "keep",
        "score": f"{initial_score:.4f}",
        "best_score": f"{initial_score:.4f}",
        "focus_file": "",
        "files_changed": str(len(hashes)),
        "duration_s": "0",
        "description": "baseline genome snapshot",
    })

    print(f"  Structural score: {initial_score:.4f}")
    print(f"  Issues found: {struct.get('metrics', {}).get('issue_count', 0)}")
    print(f"  Frontier: {FRONTIER_DIR}")
    print("  [OK] System initialized. Run '/ultragent evolve' to start.")


def cmd_status(_args: argparse.Namespace) -> None:
    """Show status with AutoResearch metrics."""
    meta = metadata_read()
    entries = archive_read()
    if not entries:
        print("Not initialized. Run: python ua.py init")
        return

    best = archive_best(entries)
    results = results_read()

    print("=== UltrAgent x AutoResearch Status ===")
    print(f"  Generations:  {meta.get('total_generations', 0)}")
    print(f"  Kept:         {meta.get('total_kept', 0)}")
    print(f"  Discarded:    {meta.get('total_discarded', 0)}")
    print(f"  Crashed:      {meta.get('total_crashed', 0)}")
    print(f"  Best:         {meta.get('best_gen_id', 'N/A')} (score: {meta.get('best_score', 0):.4f})")
    print(f"  Promoted:     {meta.get('promoted_gen_id', 'N/A')}")
    print(f"  Initialized:  {meta.get('initialized_at', 'N/A')}")

    # Config summary
    cfg = config_read()
    print(f"\n  Time budget:     {cfg.get('time_budget_seconds', 300)}s")
    print(f"  Focused mutation: {cfg.get('focused_mutation', True)}")
    print(f"  Selection:       {cfg.get('parent_selection_strategy', 'best')}")

    # Last 5 results
    if results:
        print("\n  Recent experiments:")
        for r in results[-8:]:
            status = r.get("status", "?")
            marker = {"keep": "+", "discard": "-", "crash": "!"}
            s = marker.get(status, "?")
            print(f"    [{s}] {r.get('gen_id', '?'):>12}  score={r.get('score', '?'):>8}  "
                  f"best={r.get('best_score', '?'):>8}  {r.get('description', '')[:50]}")


def cmd_keep(args: argparse.Namespace) -> None:
    """Accept a generation: update frontier, log keep."""
    gen_id = args.gen_id
    entry = archive_get(gen_id)
    if not entry:
        print(f"Generation {gen_id} not found in archive.")
        return

    score = entry.get("score", 0)
    meta = metadata_read()
    best_score = meta.get("best_score", 0)

    # Update frontier
    snapshot_dir = GENERATIONS_DIR / gen_id / "snapshot"
    if snapshot_dir.exists():
        frontier_update(snapshot_dir)
        reasoning_file = GENERATIONS_DIR / gen_id / "meta_reasoning.md"
        desc = ""
        if reasoning_file.exists():
            desc = reasoning_file.read_text(encoding="utf-8")[:200].replace("\n", " ")
        frontier_commit(gen_id, score, desc)
        print(f"  Frontier: committed {gen_id}")

    # Update metadata
    if score > best_score:
        meta["best_gen_id"] = gen_id
        meta["best_score"] = score
        print(f"  NEW BEST! {score:.4f} > {best_score:.4f}")
    meta["total_kept"] = meta.get("total_kept", 0) + 1
    # Reset stuck recovery counters (codex-autoresearch PIVOT/REFINE pattern)
    meta["consecutive_discards"] = 0
    meta["pivot_count"] = 0
    metadata_write(meta)

    # Update archive entry status
    entries = archive_read()
    updated = []
    for e in entries:
        if e["gen_id"] == gen_id:
            e["status"] = "keep"
        updated.append(e)
    ARCHIVE_FILE.write_text(
        "\n".join(json.dumps(e, default=str) for e in updated) + "\n",
        encoding="utf-8",
    )

    # Log to results.tsv
    results_log({
        "timestamp": now_iso(),
        "gen_id": gen_id,
        "parent_id": entry.get("parent_gen_id", ""),
        "status": "keep",
        "score": f"{score:.4f}",
        "best_score": f"{max(score, best_score):.4f}",
        "focus_file": entry.get("focus_file", ""),
        "files_changed": str(entry.get("patch_files_count", 0)),
        "duration_s": str(entry.get("duration_s", "")),
        "description": entry.get("description", ""),
    })

    # Record preference pair (Scientific Taste pattern)
    parent_id = entry.get("parent_gen_id", "")
    if parent_id:
        from evaluate import preference_record
        preference_record(
            winner_id=gen_id, loser_id=parent_id,
            focus_file=entry.get("focus_file", ""),
            reason=entry.get("description", "kept: score improved"),
            confidence="high", source="keep",
        )

    # Auto-extract skill from KEPT generation (Hive pattern)
    delta = score - best_score if score > best_score else score - (best_score * 0.95)
    skill = auto_extract_skill(gen_id, delta)
    if skill:
        print(f"  Skill registered: {skill['pattern_name']} (delta={skill['score_delta']:+.4f})")

    # Auto-extract lesson (MetaClaw pattern)
    lesson = auto_extract_lesson(gen_id, entry, "keep")
    if lesson:
        update_program_lessons()
        print(f"  Lesson recorded: {lesson['lesson'][:80]}")

    print(f"  [KEEP] {gen_id} score={score:.4f}")


def cmd_discard(args: argparse.Namespace) -> None:
    """Reject a generation: log discard, don't touch frontier."""
    gen_id = args.gen_id
    reason = args.reason if hasattr(args, "reason") and args.reason else "score below best"
    entry = archive_get(gen_id)
    if not entry:
        print(f"Generation {gen_id} not found in archive.")
        return

    score = entry.get("score", 0)
    meta = metadata_read()
    best_score = meta.get("best_score", 0)

    # Update metadata
    meta["total_discarded"] = meta.get("total_discarded", 0) + 1
    # Increment stuck counter (codex-autoresearch PIVOT/REFINE pattern)
    meta["consecutive_discards"] = meta.get("consecutive_discards", 0) + 1
    metadata_write(meta)

    # Update archive
    entries = archive_read()
    updated = []
    for e in entries:
        if e["gen_id"] == gen_id:
            e["status"] = "discard"
            e["discard_reason"] = reason
        updated.append(e)
    ARCHIVE_FILE.write_text(
        "\n".join(json.dumps(e, default=str) for e in updated) + "\n",
        encoding="utf-8",
    )

    # Log to results.tsv
    results_log({
        "timestamp": now_iso(),
        "gen_id": gen_id,
        "parent_id": entry.get("parent_gen_id", ""),
        "status": "discard",
        "score": f"{score:.4f}",
        "best_score": f"{best_score:.4f}",
        "focus_file": entry.get("focus_file", ""),
        "files_changed": str(entry.get("patch_files_count", 0)),
        "duration_s": str(entry.get("duration_s", "")),
        "description": f"DISCARD: {reason}",
    })

    # Record preference pair (parent wins)
    parent_id = entry.get("parent_gen_id", "")
    if parent_id:
        from evaluate import preference_record
        preference_record(
            winner_id=parent_id, loser_id=gen_id,
            focus_file=entry.get("focus_file", ""),
            reason=f"discarded: {reason}",
            confidence="high", source="discard",
        )

    # Auto-extract lesson (MetaClaw pattern)
    lesson = auto_extract_lesson(gen_id, entry, "discard", reason)
    if lesson:
        update_program_lessons()
        print(f"  Lesson recorded: {lesson['lesson'][:80]}")

    print(f"  [DISCARD] {gen_id} score={score:.4f} (best={best_score:.4f}) reason: {reason}")


def cmd_select_parent(args: argparse.Namespace) -> None:
    """Select parent generation."""
    strategy = args.strategy or config_read().get("parent_selection_strategy", "best")

    select_parent_file = ULTRAGENT_DIR / "select_parent.py"
    if select_parent_file.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("select_parent", select_parent_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        parent_id = mod.select_parent(archive_read(), strategy)
    else:
        parent_id = _builtin_select_parent(archive_read(), strategy)

    print(f"Selected parent: {parent_id} (strategy: {strategy})")
    return parent_id


def _builtin_select_parent(entries: list[dict], strategy: str) -> str:
    import math, random
    valid = [e for e in entries if e.get("valid", True) and e.get("status") != "discard"]
    if not valid:
        valid = [e for e in entries if e.get("valid", True)]
    if not valid:
        return "initial"

    if strategy == "best":
        return max(valid, key=lambda e: e.get("score", 0))["gen_id"]
    elif strategy == "latest":
        return valid[-1]["gen_id"]
    elif strategy == "random":
        return random.choice(valid)["gen_id"]
    elif strategy == "score_child_prop":
        child_counts = {}
        for e in entries:
            pid = e.get("parent_gen_id")
            if pid:
                child_counts[pid] = child_counts.get(pid, 0) + 1
        weights = []
        for e in valid:
            s = e.get("score", 0)
            c = child_counts.get(e["gen_id"], 0)
            weights.append(s * math.exp(-0.5 * c))
        total = sum(weights)
        if total == 0:
            return random.choice(valid)["gen_id"]
        r = random.random() * total
        cum = 0
        for entry, w in zip(valid, weights):
            cum += w
            if r <= cum:
                return entry["gen_id"]
        return valid[-1]["gen_id"]
    else:
        return max(valid, key=lambda e: e.get("score", 0))["gen_id"]


def cmd_create_gen(args: argparse.Namespace) -> None:
    """Create a new generation from parent + patch."""
    meta = metadata_read()
    gen_num = meta.get("next_gen_number", 1)
    gen_id = f"gen_{gen_num:04d}"

    gen_dir = GENERATIONS_DIR / gen_id
    snapshot_dir = gen_dir / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    parent_dir = GENERATIONS_DIR / args.parent / "snapshot"
    if parent_dir.exists():
        shutil.copytree(parent_dir, snapshot_dir, dirs_exist_ok=True)

    patch_content = ""
    if args.patch_file and Path(args.patch_file).exists():
        patch_content = Path(args.patch_file).read_text(encoding="utf-8")
        shutil.copy2(args.patch_file, gen_dir / "patch.diff")

    scores = {"structural": 0, "llm_judge": 0, "task_based": None, "aggregate": 0}
    if args.scores_json and Path(args.scores_json).exists():
        scores = json.loads(Path(args.scores_json).read_text(encoding="utf-8"))

    if args.reasoning_file and Path(args.reasoning_file).exists():
        shutil.copy2(args.reasoning_file, gen_dir / "meta_reasoning.md")

    struct = score_structural(gen_dir)
    if not scores.get("structural"):
        scores["structural"] = struct.get("metrics", {}).get("final_score", 0)

    # Single aggregate score
    weights = config_read().get("scoring_weights", {"structural": 0.2, "llm_judge": 0.5, "task_based": 0.3})
    agg_num = agg_den = 0
    for key, w in weights.items():
        val = scores.get(key)
        if val is not None:
            agg_num += val * w
            agg_den += w
    scores["aggregate"] = round(agg_num / agg_den, 4) if agg_den > 0 else 0

    (gen_dir / "metadata.json").write_text(json.dumps({
        "gen_id": gen_id, "parent_gen_id": args.parent,
        "created_at": now_iso(), "patch_size": len(patch_content),
    }, indent=2, default=str), encoding="utf-8")
    (gen_dir / "scores.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")

    archive_append({
        "gen_id": gen_id, "parent_gen_id": args.parent,
        "score": scores["aggregate"], "scores": scores,
        "valid": True, "created_at": now_iso(),
        "promoted": False, "status": "pending",
        "patch_files_count": 1 if patch_content else 0,
    })

    meta["next_gen_number"] = gen_num + 1
    meta["total_generations"] = meta.get("total_generations", 0) + 1
    meta["current_gen_id"] = gen_id
    metadata_write(meta)

    print(f"Created: {gen_id} (parent: {args.parent}, score: {scores['aggregate']:.4f})")


def cmd_promote(args: argparse.Namespace) -> None:
    """Promote a generation to live ~/.claude/."""
    gen_id = args.gen_id
    gen_dir = GENERATIONS_DIR / gen_id / "snapshot"
    if not gen_dir.exists():
        print(f"Error: {gen_id} snapshot not found")
        sys.exit(1)

    backup_dir = ULTRAGENT_DIR / "backups" / f"pre_promote_{now_iso().replace(':', '-')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot_genome(backup_dir)
    print(f"  Backed up to {backup_dir}")

    restored = restore_genome(gen_dir)
    print(f"  Restored {len(restored)} files from {gen_id}")

    meta = metadata_read()
    meta["promoted_gen_id"] = gen_id
    meta["last_promoted_at"] = now_iso()
    metadata_write(meta)

    entries = archive_read()
    updated = []
    for e in entries:
        if e["gen_id"] == gen_id:
            e["promoted"] = True
            e["promoted_at"] = now_iso()
        updated.append(e)
    ARCHIVE_FILE.write_text(
        "\n".join(json.dumps(e, default=str) for e in updated) + "\n",
        encoding="utf-8",
    )
    print(f"  [OK] Promoted {gen_id} to live config")


def cmd_rollback(_args: argparse.Namespace) -> None:
    """Rollback to most recent backup."""
    backup_root = ULTRAGENT_DIR / "backups"
    if not backup_root.exists():
        print("No backups found.")
        return
    backups = sorted(backup_root.iterdir())
    if not backups:
        print("No backups found.")
        return
    latest = backups[-1]
    restored = restore_genome(latest)
    print(f"Rolled back {len(restored)} files from {latest.name}")
    meta = metadata_read()
    meta["promoted_gen_id"] = f"rollback_{latest.name}"
    metadata_write(meta)


def cmd_frontier(_args: argparse.Namespace) -> None:
    """Show git frontier — only accepted improvements."""
    if not (FRONTIER_DIR / ".git").exists():
        print("Frontier not initialized.")
        return
    log = frontier_log(20)
    print("=== Git Frontier (accepted improvements only) ===")
    print(log if log else "  (empty)")


def cmd_results(args: argparse.Namespace) -> None:
    """Show results.tsv — ALL experiments."""
    n = int(args.n) if hasattr(args, "n") and args.n else 20
    rows = results_read(n)
    if not rows:
        print("No results yet.")
        return

    print(f"=== Last {len(rows)} Experiments ===")
    print(f"{'status':>8}  {'gen_id':>12}  {'score':>8}  {'best':>8}  {'focus_file':<30}  description")
    print("-" * 100)
    for r in rows:
        status = r.get("status", "?")
        marker = {"keep": " KEEP", "discard": " DISC", "crash": "CRASH"}.get(status, "  ???")
        print(f"{marker:>8}  {r.get('gen_id', '?'):>12}  {r.get('score', '?'):>8}  "
              f"{r.get('best_score', '?'):>8}  {r.get('focus_file', ''):.<30}  "
              f"{r.get('description', '')[:40]}")


def cmd_suggest_focus(args: argparse.Namespace) -> None:
    """Suggest which file to focus mutation on (Amdahl's law impact scoring)."""
    gen_id = args.gen_id if hasattr(args, "gen_id") and args.gen_id else "initial"
    suggestion = suggest_focus_file(gen_id)
    print(f"Focus file: {suggestion['file']}")
    print(f"  Impact score: {suggestion.get('impact_score', 'N/A')}")
    print(f"  Structural score: {suggestion['score']}")
    print(f"  Reason: {suggestion['reason']}")
    if suggestion.get("all_ranked"):
        print(f"\n  Top candidates (highest impact first):")
        print(f"    {'impact':>7}  {'score':>6}  {'usage':>6}  {'headroom':>9}  file")
        print(f"    {'-------':>7}  {'------':>6}  {'------':>6}  {'---------':>9}  ----")
        for r in suggestion["all_ranked"]:
            print(f"    {r.get('impact', 0):>7.4f}  {r['score']:>6.3f}  "
                  f"{r.get('usage', 0):>5.0%}  {r.get('headroom', 0):>8.0%}  {r['file']}")


def cmd_lineage(args: argparse.Namespace) -> None:
    """Show evolution tree."""
    entries = archive_read()
    if not entries:
        print("No generations.")
        return

    children: dict[str | None, list[str]] = {}
    entry_map = {}
    for e in entries:
        entry_map[e["gen_id"]] = e
        pid = e.get("parent_gen_id")
        children.setdefault(pid, []).append(e["gen_id"])

    target = args.gen_id if hasattr(args, "gen_id") and args.gen_id else None

    def print_tree(gid: str, prefix: str = "", is_last: bool = True):
        e = entry_map.get(gid, {})
        conn = "`-- " if is_last else "|-- "
        score = e.get("score", 0)
        status = e.get("status", "")
        st = {"keep": "+", "discard": "-", "crash": "!", "pending": "?"}.get(status, " ")
        promoted = " *PROMOTED*" if e.get("promoted") else ""
        hl = " <--" if gid == target else ""
        print(f"{prefix}{conn}[{st}] {gid}  score={score:.3f}{promoted}{hl}")
        child_prefix = prefix + ("    " if is_last else "|   ")
        kids = children.get(gid, [])
        for i, kid in enumerate(kids):
            print_tree(kid, child_prefix, i == len(kids) - 1)

    for i, root in enumerate(children.get(None, [])):
        print_tree(root, "", i == len(children.get(None, [])) - 1)


def cmd_diff(args: argparse.Namespace) -> None:
    """Show generation's patch."""
    gen_dir = GENERATIONS_DIR / args.gen_id
    patch_file = gen_dir / "patch.diff"
    if patch_file.exists():
        print(patch_file.read_text(encoding="utf-8"))
    else:
        entry = archive_get(args.gen_id)
        if not entry:
            print(f"{args.gen_id} not found.")
            return
        pid = entry.get("parent_gen_id")
        if not pid:
            print("Initial generation.")
            return
        diff = compute_diff(GENERATIONS_DIR / pid / "snapshot", gen_dir / "snapshot")
        print(diff if diff else "No differences.")


def cmd_archive(_args: argparse.Namespace) -> None:
    print(json.dumps(archive_read(), indent=2, default=str))


def cmd_score(args: argparse.Namespace) -> None:
    gen_dir = GENERATIONS_DIR / args.gen_id
    if not gen_dir.exists():
        print(f"{args.gen_id} not found.")
        return
    print(json.dumps(score_structural(gen_dir), indent=2))


def cmd_snapshot(args: argparse.Namespace) -> None:
    dest = Path(args.dest) if args.dest else ULTRAGENT_DIR / "snapshots" / now_iso().replace(":", "-")
    dest.mkdir(parents=True, exist_ok=True)
    hashes = snapshot_genome(dest)
    print(f"Snapshot: {len(hashes)} files -> {dest}")


def cmd_capture(args: argparse.Namespace) -> None:
    """Capture a trajectory from agent usage."""
    entry = trajectory_capture(
        agent_file=args.agent,
        outcome=args.outcome,
        description=args.description,
        task_type=getattr(args, "task_type", ""),
        user_correction=getattr(args, "correction", ""),
        error_output=getattr(args, "error", ""),
    )
    print(f"Captured: {entry['agent_file']} [{entry['outcome']}] {entry['description'][:80]}")


def cmd_trajectories(args: argparse.Namespace) -> None:
    """Show recent trajectories."""
    n = int(args.n) if hasattr(args, "n") and args.n else 20
    trajs = trajectories_read(n)
    if not trajs:
        print("No trajectories captured yet.")
        print("Capture with: python ua.py capture <agent_file> <outcome> <description>")
        return

    # Summary first
    summary = trajectories_summary()
    print(f"=== Trajectory Summary ({len(trajectories_read())} total) ===")
    for agent, counts in sorted(summary.items()):
        total = counts["total"]
        fails = counts.get("failure", 0) + counts.get("correction", 0)
        rate = f"{fails/total*100:.0f}%" if total > 0 else "N/A"
        print(f"  {agent:<40s}  total={total:>3}  failures={fails:>3}  rate={rate}")

    print(f"\n  Last {len(trajs)} trajectories:")
    for t in trajs:
        outcome = t.get("outcome", "?")
        marker = {"success": " OK", "failure": "FAIL", "correction": "CORR", "retry": " RTY"}.get(outcome, "  ??")
        ts = t.get("timestamp", "")[:19]
        print(f"    [{marker}] {ts}  {t.get('agent_file', '?'):<35s}  {t.get('description', '')[:45]}")


def cmd_queue_evolve(args: argparse.Namespace) -> None:
    """Queue an evolve cycle for a specific agent file."""
    reason = args.reason if hasattr(args, "reason") and args.reason else "manual queue"
    entry = queue_evolve(args.agent_file, reason)
    print(f"Queued evolve: {entry['agent_file']} (reason: {reason})")

    pending = pending_evolves()
    if len(pending) > 1:
        print(f"  ({len(pending)} total pending evolves)")


def cmd_pending_evolves(_args: argparse.Namespace) -> None:
    """Show queued evolves."""
    pending = pending_evolves()
    if not pending:
        print("No pending evolves.")
        return

    targets = evolve_targets_from_queue()
    print(f"=== Pending Evolves ({len(pending)} queued) ===")
    for t in targets:
        reasons = "; ".join(r for r in t.get("reasons", []) if r)[:60]
        print(f"  P{t['priority']}  {t['file']:<40s}  queued={t['queued']}  "
              f"failures={t.get('failure_count', 0)}  rate={t.get('failure_rate', 0):.0%}  {reasons}")


def cmd_drain_queue(_args: argparse.Namespace) -> None:
    """Process and clear the evolve queue."""
    drained = drain_queue()
    if not drained:
        print("Queue is empty.")
        return
    print(f"Drained {len(drained)} entries:")
    for d in drained:
        print(f"  {d['agent_file']}  reason: {d.get('reason', '')[:60]}")


def cmd_lessons(args: argparse.Namespace) -> None:
    """Show lessons learned from evolution history."""
    lessons = lessons_read()
    if not lessons:
        print("No lessons yet. Lessons auto-record after each keep/discard.")
        return

    focus = args.focus_file if hasattr(args, "focus_file") and args.focus_file else None
    if focus:
        lessons = [l for l in lessons if l.get("focus_file") == focus]
        print(f"=== Lessons for {focus} ({len(lessons)}) ===")
    else:
        print(f"=== All Lessons ({len(lessons)}) ===")

    for l in lessons:
        icon = "+" if l.get("outcome") == "keep" else "-"
        print(f"  [{icon}] {l.get('gen_id', '?'):>10}  {l.get('focus_file', ''):.<40}  {l.get('lesson', '')[:60]}")


def cmd_skills(_args: argparse.Namespace) -> None:
    """Show registered improvement skills (proven patterns)."""
    skills = skills_ranked()
    if not skills:
        print("No skills registered yet. Skills are auto-extracted from KEPT generations.")
        return

    print(f"=== Proven Skills ({len(skills)} registered) ===")
    print(f"{'pattern':<20s}  {'delta':>7s}  {'gen':>10s}  {'file':<35s}  description")
    print("-" * 110)
    for s in skills:
        print(f"  {s.get('pattern_name', '?'):<18s}  {s.get('score_delta', 0):>+.4f}  "
              f"{s.get('gen_id', '?'):>10s}  {s.get('focus_file', ''):<35s}  "
              f"{s.get('description', '')[:40]}")


def cmd_competition(_args: argparse.Namespace) -> None:
    """Show competition configuration."""
    comp = get_competition_config()
    print(f"=== Competition Config ===")
    print(f"  Enabled: {comp['enabled']}")
    print(f"  Size: {comp['size']} agents per generation")
    print(f"  Strategies:")
    for s in comp["strategies"]:
        print(f"    - {s['name']}: {s['directive'][:70]}...")


# ─── Retrospective (Agentic Researcher pattern: structured self-review) ────

def cmd_retro(args: argparse.Namespace) -> None:
    """
    Automated retrospective: analyze evolution history and update program.md.
    Inspired by The Agentic Researcher's /retro command.
    """
    meta = metadata_read()
    if not meta:
        print("Not initialized. Run: python ua.py init")
        return

    results = results_read()
    lessons = lessons_read()
    trajectories = trajectories_read()
    archive = archive_read()
    skills = skills_ranked()

    if len(results) < 2:
        print("Need at least 2 experiments for a retrospective.")
        return

    # ── Gather statistics ──
    total = len(results)
    kept = [r for r in results if r.get("status") == "keep"]
    discarded = [r for r in results if r.get("status") == "discard"]
    crashed = [r for r in results if r.get("status") == "crash"]
    keep_rate = len(kept) / total if total > 0 else 0

    # Per-file analysis
    file_stats: dict[str, dict] = {}
    for r in results:
        f = r.get("focus_file", "")
        if not f:
            continue
        file_stats.setdefault(f, {"kept": 0, "discarded": 0, "crashed": 0, "scores": []})
        status = r.get("status", "")
        if status == "keep":
            file_stats[f]["kept"] += 1
        elif status == "discard":
            file_stats[f]["discarded"] += 1
        elif status == "crash":
            file_stats[f]["crashed"] += 1
        try:
            file_stats[f]["scores"].append(float(r.get("score", 0)))
        except (ValueError, TypeError):
            pass

    # Per-strategy analysis (from archive)
    strategy_stats: dict[str, dict] = {}
    for e in archive:
        s = e.get("strategy", "unknown")
        strategy_stats.setdefault(s, {"kept": 0, "discarded": 0, "total": 0, "scores": []})
        strategy_stats[s]["total"] += 1
        if e.get("status") == "keep":
            strategy_stats[s]["kept"] += 1
        elif e.get("status") == "discard":
            strategy_stats[s]["discarded"] += 1
        strategy_stats[s]["scores"].append(e.get("score", 0))

    # Trajectory analysis
    traj_summary = trajectories_summary()

    # Score progression
    scores = []
    for r in results:
        try:
            scores.append(float(r.get("score", 0)))
        except (ValueError, TypeError):
            pass
    best_scores = []
    for r in results:
        try:
            best_scores.append(float(r.get("best_score", 0)))
        except (ValueError, TypeError):
            pass

    score_trend = "improving" if len(best_scores) >= 3 and best_scores[-1] > best_scores[0] else "flat"
    if len(best_scores) >= 5:
        recent = best_scores[-3:]
        if all(a == b for a, b in zip(recent, recent[1:])):
            score_trend = "stalled"

    # ── Build report ──
    report_lines = [
        f"# Retrospective Report",
        f"",
        f"**Generated:** {now_iso()[:19]}",
        f"**Generations analyzed:** {total}",
        f"**Best score:** {meta.get('best_score', 0):.4f} ({meta.get('best_gen_id', 'N/A')})",
        f"**Score trend:** {score_trend}",
        f"",
        f"## Overview",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total experiments | {total} |",
        f"| Kept | {len(kept)} ({keep_rate:.0%}) |",
        f"| Discarded | {len(discarded)} |",
        f"| Crashed | {len(crashed)} |",
        f"| Best score | {meta.get('best_score', 0):.4f} |",
        f"| Score trend | {score_trend} |",
        f"",
    ]

    # File performance
    report_lines.extend([
        f"## File Performance",
        f"",
        f"| File | Kept | Discarded | Crashed | Avg Score | Trend |",
        f"|------|------|-----------|---------|-----------|-------|",
    ])
    for f, stats in sorted(file_stats.items(), key=lambda x: x[1]["kept"], reverse=True):
        avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
        total_f = stats["kept"] + stats["discarded"] + stats["crashed"]
        success_rate = stats["kept"] / total_f if total_f > 0 else 0
        trend = "good" if success_rate > 0.5 else ("struggling" if success_rate < 0.2 else "mixed")
        report_lines.append(
            f"| `{f}` | {stats['kept']} | {stats['discarded']} | {stats['crashed']} | {avg_score:.3f} | {trend} |"
        )
    report_lines.append("")

    # Strategy performance
    if any(s != "unknown" for s in strategy_stats):
        report_lines.extend([
            f"## Strategy Performance",
            f"",
            f"| Strategy | Total | Kept | Win Rate | Avg Score |",
            f"|----------|-------|------|----------|-----------|",
        ])
        for s, stats in sorted(strategy_stats.items(), key=lambda x: x[1]["kept"], reverse=True):
            if s == "unknown":
                continue
            avg = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
            win_rate = stats["kept"] / stats["total"] if stats["total"] > 0 else 0
            report_lines.append(
                f"| {s} | {stats['total']} | {stats['kept']} | {win_rate:.0%} | {avg:.3f} |"
            )
        report_lines.append("")

    # Agent trajectory health
    if traj_summary:
        report_lines.extend([
            f"## Agent Health (from trajectories)",
            f"",
            f"| Agent | Total | Failures | Failure Rate | Status |",
            f"|-------|-------|----------|--------------|--------|",
        ])
        for agent, counts in sorted(traj_summary.items(), key=lambda x: x[1].get("total", 0), reverse=True):
            total_a = counts.get("total", 0)
            fails = counts.get("failure", 0) + counts.get("correction", 0)
            rate = fails / total_a if total_a > 0 else 0
            status = "healthy" if rate < 0.15 else ("needs attention" if rate < 0.35 else "critical")
            report_lines.append(
                f"| `{agent}` | {total_a} | {fails} | {rate:.0%} | {status} |"
            )
        report_lines.append("")

    # Insights & recommendations
    report_lines.extend([
        f"## Insights",
        f"",
    ])

    # Detect patterns
    insights = []

    # Stalled score
    if score_trend == "stalled":
        insights.append(
            "- **Score stalled**: Last 3 best scores identical. Consider: "
            "switch parent selection strategy (try `ucb1` or `novelty`), "
            "evolve a different file category, or update program.md priorities."
        )

    # High crash rate
    crash_rate = len(crashed) / total if total > 0 else 0
    if crash_rate > 0.3:
        insights.append(
            f"- **High crash rate ({crash_rate:.0%})**: MetaAgent is producing broken output. "
            f"Check meta_prompt.md for clarity on file write instructions."
        )

    # Strategy saturation
    for s, stats in strategy_stats.items():
        if s == "unknown":
            continue
        if stats["total"] >= 5 and stats["kept"] == 0:
            insights.append(
                f"- **Strategy `{s}` never wins**: {stats['total']} attempts, 0 kept. "
                f"Consider removing or replacing this competition strategy."
            )

    # File that always fails
    for f, stats in file_stats.items():
        total_f = stats["kept"] + stats["discarded"] + stats["crashed"]
        if total_f >= 3 and stats["kept"] == 0:
            insights.append(
                f"- **`{f}` always fails**: {total_f} attempts, 0 kept. "
                f"File may be at local optimum or changes are too subtle to score higher."
            )

    # Agents needing evolution (from trajectories)
    for agent, counts in traj_summary.items():
        total_a = counts.get("total", 0)
        fails = counts.get("failure", 0) + counts.get("correction", 0)
        if total_a >= 5 and fails / total_a > 0.35:
            insights.append(
                f"- **Agent `{agent}` high failure rate ({fails}/{total_a})**: "
                f"Queue evolve with `ua.py queue-evolve {agent}`."
            )

    # Low keep rate
    if keep_rate < 0.15 and total >= 10:
        insights.append(
            f"- **Low keep rate ({keep_rate:.0%})**: Most generations are discarded. "
            f"The search space may be too constrained or evaluation too strict."
        )

    if not insights:
        insights.append("- No critical issues detected. Evolution is progressing normally.")

    report_lines.extend(insights)
    report_lines.append("")

    # Program.md update recommendations
    report_lines.extend([
        f"## Recommended program.md Updates",
        f"",
    ])

    # Build "What Works" and "What Doesn't" from lessons
    what_works = [l for l in lessons if l.get("outcome") == "keep"]
    what_fails = [l for l in lessons if l.get("outcome") == "discard"]

    if what_works:
        report_lines.append("### What Works")
        for l in what_works[-5:]:
            report_lines.append(f"- `{l.get('gen_id', '?')}` on `{l.get('focus_file', '?')}`: {l.get('lesson', '')[:120]}")
        report_lines.append("")

    if what_fails:
        report_lines.append("### What Doesn't Work")
        for l in what_fails[-5:]:
            report_lines.append(f"- `{l.get('gen_id', '?')}` on `{l.get('focus_file', '?')}`: {l.get('lesson', '')[:120]}")
        report_lines.append("")

    report_content = "\n".join(report_lines)

    # ── Save report ──
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    retro_num = len(list(RETRO_DIR.glob("retro_*.md"))) + 1
    report_file = RETRO_DIR / f"retro_{retro_num:03d}.md"
    report_file.write_text(report_content, encoding="utf-8")
    print(f"Retrospective saved: {report_file}")

    # ── Update structured evolution memory ──
    evo_mem = evolution_memory_update()
    facts_count = len(evo_mem.get("facts", []))
    files_count = len(evo_mem.get("file_insights", {}))
    print(f"Evolution memory updated: {files_count} file insights, {facts_count} facts")

    # ── Auto-update program.md ──
    if PROGRAM_FILE.exists():
        prog_content = PROGRAM_FILE.read_text(encoding="utf-8")

        # Build What Works section
        works_lines = []
        for f, stats in sorted(file_stats.items(), key=lambda x: x[1]["kept"], reverse=True):
            total_f = stats["kept"] + stats["discarded"] + stats["crashed"]
            if stats["kept"] > 0 and total_f >= 2:
                rate = stats["kept"] / total_f
                works_lines.append(f"- `{f}`: {stats['kept']}/{total_f} kept ({rate:.0%} success rate)")

        fails_lines = []
        for f, stats in sorted(file_stats.items(), key=lambda x: x[1].get("discarded", 0) + x[1].get("crashed", 0), reverse=True):
            total_f = stats["kept"] + stats["discarded"] + stats["crashed"]
            if stats["kept"] == 0 and total_f >= 2:
                fails_lines.append(f"- `{f}`: 0/{total_f} kept — consider deprioritizing or trying radically different approach")

        # Insert/update What Works section
        what_works_marker = "## What Works"
        what_fails_marker = "## What Doesn't Work"
        lessons_marker = "## Lessons Learned"

        new_sections = ""
        if works_lines:
            new_sections += f"\n{what_works_marker}\n\n_Auto-generated by retro (retro #{retro_num})_\n\n" + "\n".join(works_lines) + "\n"
        if fails_lines:
            new_sections += f"\n{what_fails_marker}\n\n_Auto-generated by retro (retro #{retro_num})_\n\n" + "\n".join(fails_lines) + "\n"

        # Place before Lessons Learned or at end
        if what_works_marker in prog_content:
            # Remove old What Works and What Doesn't sections
            clean = prog_content
            for marker in [what_works_marker, what_fails_marker]:
                if marker in clean:
                    idx = clean.index(marker)
                    rest = clean[idx + len(marker):]
                    next_h2 = rest.find("\n## ")
                    if next_h2 == -1:
                        clean = clean[:idx]
                    else:
                        clean = clean[:idx] + rest[next_h2:]
            prog_content = clean

        if new_sections:
            if lessons_marker in prog_content:
                idx = prog_content.index(lessons_marker)
                prog_content = prog_content[:idx] + new_sections.strip() + "\n\n" + prog_content[idx:]
            else:
                prog_content = prog_content.rstrip() + "\n" + new_sections

        # Update score trend in a summary line
        trend_marker = "## Evolution Status"
        trend_content = (
            f"\n{trend_marker}\n\n"
            f"_Auto-updated by retro #{retro_num} ({now_iso()[:10]})_\n\n"
            f"- Best: {meta.get('best_score', 0):.4f} ({meta.get('best_gen_id', 'N/A')})\n"
            f"- Keep rate: {keep_rate:.0%} ({len(kept)}/{total})\n"
            f"- Trend: {score_trend}\n"
            f"- Crash rate: {crash_rate:.0%}\n"
        )
        if trend_marker in prog_content:
            idx = prog_content.index(trend_marker)
            rest = prog_content[idx + len(trend_marker):]
            next_h2 = rest.find("\n## ")
            if next_h2 == -1:
                prog_content = prog_content[:idx] + trend_content.strip() + "\n"
            else:
                prog_content = prog_content[:idx] + trend_content.strip() + "\n" + rest[next_h2:]
        else:
            # Insert at top, after the first blockquote
            first_h2 = prog_content.find("\n## ")
            if first_h2 != -1:
                prog_content = prog_content[:first_h2] + "\n" + trend_content + prog_content[first_h2:]
            else:
                prog_content = trend_content + "\n" + prog_content

        PROGRAM_FILE.write_text(prog_content, encoding="utf-8")
        print(f"program.md updated with evolution status + what works/doesn't")

    # ── Print summary ──
    print(f"\n=== Retrospective #{retro_num} ===")
    print(f"  Experiments: {total} (kept={len(kept)}, discarded={len(discarded)}, crashed={len(crashed)})")
    print(f"  Keep rate: {keep_rate:.0%}")
    print(f"  Score trend: {score_trend}")
    print(f"  Best: {meta.get('best_score', 0):.4f}")
    if insights:
        print(f"\n  Key insights:")
        for i in insights:
            print(f"    {i}")


def cmd_memory(_args: argparse.Namespace) -> None:
    """Show structured evolution memory."""
    mem = evolution_memory_read()
    if not mem.get("last_updated"):
        print("No evolution memory yet. Run: /ultragent retro")
        return

    ctx = mem["evolution_context"]
    print("=== Evolution Memory ===")
    print(f"  Last updated: {mem['last_updated'][:19]}")
    print(f"  Phase: {ctx['phase']}")
    print(f"  Score trend: {ctx['score_trend']}")
    print(f"  Best: {ctx['best_score']:.4f} ({ctx['best_gen_id']})")
    print(f"  Keep rate: {ctx['keep_rate']:.0%}")

    fi = mem.get("file_insights", {})
    if fi:
        print(f"\n  File Insights ({len(fi)} files):")
        for f, insight in sorted(fi.items(), key=lambda x: x[1].get("responsiveness", 0), reverse=True):
            status = "good" if insight["responsiveness"] > 0.4 else ("stuck" if insight["responsiveness"] == 0 and insight["total_attempts"] >= 3 else "mixed")
            print(f"    {f:<40s}  {insight['responsiveness']:.0%} keep  {insight['total_attempts']} attempts  [{status}]"
                  + (f"  best:{insight['best_strategy']}" if insight.get("best_strategy") else ""))

    si = mem.get("strategy_insights", {})
    if si:
        print(f"\n  Strategy Insights ({len(si)} strategies):")
        for s, insight in sorted(si.items(), key=lambda x: x[1].get("win_rate", 0), reverse=True):
            print(f"    {s:<20s}  {insight['win_rate']:.0%} win  {insight['total_attempts']} attempts")

    facts = mem.get("facts", [])
    if facts:
        print(f"\n  Facts ({len(facts)}):")
        for fact in facts:
            conf = fact.get("confidence", 0)
            cat = fact.get("category", "?")
            print(f"    [{cat}] ({conf:.0%}) {fact['content'][:90]}")


# ─── Stuck Recovery (codex-autoresearch PIVOT/REFINE pattern) ──────────────

REFINE_THRESHOLD = 3   # consecutive discards before REFINE
PIVOT_THRESHOLD = 5    # consecutive discards before PIVOT
ESCALATE_THRESHOLD = 2 # PIVOTs without improvement before ESCALATE
SOFT_BLOCKER_THRESHOLD = 3  # PIVOTs without improvement before soft blocker


def check_stuck_recovery() -> dict:
    """
    Check if the evolution loop is stuck and recommend an action.
    Returns: {action, consecutive_discards, pivot_count, reason, recommendation}

    Actions:
      - "continue": no intervention needed
      - "refine": adjust within current strategy (switch focus file)
      - "pivot": abandon strategy (change parent selection, different file category)
      - "escalate": radical change (rotate competition strategies, update program.md)
      - "soft_blocker": warn and attempt self-referential changes
    """
    meta = metadata_read()
    consec = meta.get("consecutive_discards", 0)
    pivots = meta.get("pivot_count", 0)

    result = {
        "action": "continue",
        "consecutive_discards": consec,
        "pivot_count": pivots,
        "reason": "",
        "recommendation": "",
    }

    if pivots >= SOFT_BLOCKER_THRESHOLD:
        result["action"] = "soft_blocker"
        result["reason"] = f"{pivots} PIVOTs without improvement"
        result["recommendation"] = (
            "Evolution may be stuck at a local optimum. "
            "Try self-referential changes (meta_prompt.md, select_parent.py) "
            "or radically different file targets."
        )
    elif pivots >= ESCALATE_THRESHOLD:
        result["action"] = "escalate"
        result["reason"] = f"{pivots} PIVOTs without improvement"
        result["recommendation"] = (
            "Rotate competition strategies. Update program.md with dead-end lessons. "
            "Consider targeting a completely different file category."
        )
    elif consec >= PIVOT_THRESHOLD:
        result["action"] = "pivot"
        result["reason"] = f"{consec} consecutive discards"
        result["recommendation"] = (
            "Abandon current approach. Change parent selection strategy "
            "(try ucb1 or novelty). Target a fundamentally different file."
        )
    elif consec >= REFINE_THRESHOLD:
        result["action"] = "refine"
        result["reason"] = f"{consec} consecutive discards"
        result["recommendation"] = (
            "Adjust within current strategy. Switch to next-worst focus file. "
            "Try a different competition strategy for the same file."
        )

    return result


def apply_refine() -> dict:
    """Apply REFINE: switch focus file to next candidate, log the event."""
    meta = metadata_read()

    # Get current best gen for suggest-focus
    best_gen = meta.get("best_gen_id", "initial")
    suggestion = suggest_focus_file(best_gen)
    all_ranked = suggestion.get("all_ranked", [])

    # Try to pick a DIFFERENT file than the most recent focus
    results = results_read(5)
    recent_files = {r.get("focus_file", "") for r in results if r.get("focus_file")}

    new_focus = None
    for candidate in all_ranked:
        if candidate["file"] not in recent_files:
            new_focus = candidate["file"]
            break

    if not new_focus and len(all_ranked) > 1:
        new_focus = all_ranked[1]["file"]  # Second-worst if all recently tried

    # Log the refine event
    results_log({
        "timestamp": now_iso(),
        "gen_id": "-",
        "parent_id": "",
        "status": "refine",
        "score": "",
        "best_score": f"{meta.get('best_score', 0):.4f}",
        "focus_file": new_focus or "",
        "files_changed": "0",
        "duration_s": "0",
        "description": f"[REFINE] {meta.get('consecutive_discards', 0)} consecutive discards. Switching focus to {new_focus}",
    })

    # Reset consecutive_discards but keep pivot_count
    meta["consecutive_discards"] = 0
    metadata_write(meta)

    # Record lesson
    lesson_record(
        gen_id="-", focus_file=new_focus or "",
        outcome="refine", strategy="refine",
        lesson=f"REFINE after {meta.get('consecutive_discards', 0)} discards. "
               f"Recent files tried: {', '.join(recent_files)}. Now targeting: {new_focus}",
    )

    return {
        "action": "refine",
        "new_focus": new_focus,
        "recent_files": list(recent_files),
    }


def apply_pivot() -> dict:
    """Apply PIVOT: change parent selection strategy, log the event."""
    meta = metadata_read()
    cfg = config_read()

    # Rotate parent selection strategy
    strategies = ["score_child_prop", "ucb1", "novelty", "best", "random"]
    current = cfg.get("parent_selection_strategy", "score_child_prop")
    current_idx = strategies.index(current) if current in strategies else 0
    new_strategy = strategies[(current_idx + 1) % len(strategies)]

    # Update config with new strategy
    cfg["parent_selection_strategy"] = new_strategy
    config_write(cfg)

    # Increment pivot_count
    meta["pivot_count"] = meta.get("pivot_count", 0) + 1
    meta["consecutive_discards"] = 0
    metadata_write(meta)

    # Log the pivot event
    results_log({
        "timestamp": now_iso(),
        "gen_id": "-",
        "parent_id": "",
        "status": "pivot",
        "score": "",
        "best_score": f"{meta.get('best_score', 0):.4f}",
        "focus_file": "",
        "files_changed": "0",
        "duration_s": "0",
        "description": f"[PIVOT] Abandoned strategy '{current}'. Now using '{new_strategy}'. "
                       f"Pivot #{meta.get('pivot_count', 1)}",
    })

    # Record lesson
    lesson_record(
        gen_id="-", focus_file="",
        outcome="pivot", strategy=current,
        lesson=f"PIVOT #{meta.get('pivot_count', 1)}: abandoned '{current}' after "
               f"{meta.get('total_discarded', 0)} total discards. Switching to '{new_strategy}'.",
    )

    return {
        "action": "pivot",
        "old_strategy": current,
        "new_strategy": new_strategy,
        "pivot_count": meta.get("pivot_count", 1),
    }


def cmd_stuck_check(_args: argparse.Namespace) -> None:
    """Check if the evolution loop is stuck and recommend action."""
    result = check_stuck_recovery()

    action = result["action"]
    consec = result["consecutive_discards"]
    pivots = result["pivot_count"]

    if action == "continue":
        print(f"=== Stuck Check: OK ===")
        print(f"  Consecutive discards: {consec} (threshold: {REFINE_THRESHOLD})")
        print(f"  Pivot count: {pivots}")
        print(f"  No intervention needed.")
    else:
        markers = {"refine": "REFINE", "pivot": "PIVOT", "escalate": "ESCALATE", "soft_blocker": "SOFT BLOCKER"}
        print(f"=== Stuck Check: {markers.get(action, action.upper())} ===")
        print(f"  Consecutive discards: {consec}")
        print(f"  Pivot count: {pivots}")
        print(f"  Reason: {result['reason']}")
        print(f"  Recommendation: {result['recommendation']}")

        if action == "refine":
            print(f"\n  To apply: the evolve loop will auto-REFINE at the next cycle.")
        elif action == "pivot":
            print(f"\n  To apply: the evolve loop will auto-PIVOT at the next cycle.")


# ─── Protocol Fingerprint Check (codex-autoresearch Re-Anchoring) ──────────

FINGERPRINT_ITEMS = [
    "MetaAgent has 12-tool-call hard limit (aim for 4-6)",
    "Smoke test runs BEFORE LLM-Judge (Step 5b before Step 6)",
    "Keep/discard is binary: score > best = keep, else = discard",
    "Competition mode: 3 parallel agents (simplifier, exemplifier, aligner)",
    "Focus file is PRE-LOADED in MetaAgent context (never re-read)",
    "Stuck recovery: 3 discards = REFINE, 5 = PIVOT (Step 8b)",
    "Ensemble evaluation: 3 independent judge agents, majority vote",
    "Sprint contract is MANDATORY before MetaAgent edits",
    "ONE file per generation (focused mutation)",
    "Results logged to results.tsv for EVERY generation",
]

FINGERPRINT_INTERVAL_DEFAULT = 10
FINGERPRINT_INTERVAL_COMPACT_1 = 5
FINGERPRINT_INTERVAL_COMPACT_2 = 1


def should_fingerprint_check(generation_number: int, compaction_count: int = 0) -> bool:
    """Determine if a fingerprint check should run at this generation."""
    if compaction_count >= 2:
        return True  # Every generation after 2+ compactions
    elif compaction_count == 1:
        return generation_number % FINGERPRINT_INTERVAL_COMPACT_1 == 0
    else:
        return generation_number % FINGERPRINT_INTERVAL_DEFAULT == 0


def cmd_fingerprint(_args: argparse.Namespace) -> None:
    """Print the Protocol Fingerprint Check items for re-anchoring."""
    meta = metadata_read()
    gen_num = meta.get("next_gen_number", 1) - 1

    print("=== Protocol Fingerprint Check ===")
    print(f"  Current generation: {gen_num}")
    print(f"  Files to re-read on failure:")
    print(f"    - {ULTRAGENT_DIR / 'skill' / 'SKILL.md'}")
    print(f"    - {ULTRAGENT_DIR / 'meta_prompt.md'}")
    print(f"    - {ULTRAGENT_DIR / 'program.md'}")
    print(f"\n  Verify these 10 items (all must be YES):\n")
    for i, item in enumerate(FINGERPRINT_ITEMS, 1):
        print(f"    {i:2d}. {item}")
    print(f"\n  If ANY item is uncertain: re-read the files above,")
    print(f"  tag next generation with [RE-ANCHOR].")
    print(f"\n  Check intervals:")
    print(f"    Default: every {FINGERPRINT_INTERVAL_DEFAULT} generations")
    print(f"    After 1 compaction: every {FINGERPRINT_INTERVAL_COMPACT_1} generations")
    print(f"    After 2+ compactions: every generation")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="UltrAgent x AutoResearch for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize system")
    sub.add_parser("status", help="Show status")

    sel = sub.add_parser("select-parent", help="Select parent generation")
    sel.add_argument("strategy", nargs="?", default=None)

    cg = sub.add_parser("create-gen", help="Create new generation")
    cg.add_argument("parent")
    cg.add_argument("patch_file", nargs="?")
    cg.add_argument("scores_json", nargs="?")
    cg.add_argument("reasoning_file", nargs="?")

    k = sub.add_parser("keep", help="Accept generation (commit to frontier)")
    k.add_argument("gen_id")

    d = sub.add_parser("discard", help="Reject generation")
    d.add_argument("gen_id")
    d.add_argument("reason", nargs="?", default="score below best")

    pr = sub.add_parser("promote", help="Apply generation to live config")
    pr.add_argument("gen_id")

    sub.add_parser("rollback", help="Rollback to last backup")
    sub.add_parser("frontier", help="Show git frontier")

    res = sub.add_parser("results", help="Show results.tsv")
    res.add_argument("n", nargs="?", default="20")

    sf = sub.add_parser("suggest-focus", help="Suggest file to mutate")
    sf.add_argument("gen_id", nargs="?", default="initial")

    lin = sub.add_parser("lineage", help="Show evolution tree")
    lin.add_argument("gen_id", nargs="?")

    df = sub.add_parser("diff", help="Show generation's patch")
    df.add_argument("gen_id")

    sub.add_parser("archive", help="Dump full archive")

    sc = sub.add_parser("score", help="Structural scoring")
    sc.add_argument("gen_id")

    snap = sub.add_parser("snapshot", help="Snapshot genome")
    snap.add_argument("dest", nargs="?", default=None)

    # Trajectory commands
    cap = sub.add_parser("capture", help="Capture agent trajectory")
    cap.add_argument("agent", help="Agent file (e.g., agents/executor.md)")
    cap.add_argument("outcome", choices=["success", "failure", "correction", "retry"])
    cap.add_argument("description", help="What happened")
    cap.add_argument("--task-type", default="", help="Task category")
    cap.add_argument("--correction", default="", help="User's correction text")
    cap.add_argument("--error", default="", help="Error output")

    tj = sub.add_parser("trajectories", help="Show trajectories")
    tj.add_argument("n", nargs="?", default="20")

    qe = sub.add_parser("queue-evolve", help="Queue evolve for agent")
    qe.add_argument("agent_file", help="Agent file to evolve")
    qe.add_argument("reason", nargs="?", default="manual queue")

    sub.add_parser("pending-evolves", help="Show pending evolves")
    sub.add_parser("drain-queue", help="Process evolve queue")
    sub.add_parser("skills", help="Show proven improvement skills")
    ls = sub.add_parser("lessons", help="Show lessons learned")
    ls.add_argument("focus_file", nargs="?", help="Filter by file")
    sub.add_parser("competition", help="Show competition config")
    sub.add_parser("retro", help="Run retrospective analysis")
    sub.add_parser("memory", help="Show structured evolution memory")
    sub.add_parser("stuck-check", help="Check stuck recovery status")
    sub.add_parser("fingerprint", help="Protocol fingerprint check items")

    args = parser.parse_args()

    commands = {
        "init": cmd_init, "status": cmd_status,
        "select-parent": cmd_select_parent, "create-gen": cmd_create_gen,
        "keep": cmd_keep, "discard": cmd_discard,
        "promote": cmd_promote, "rollback": cmd_rollback,
        "frontier": cmd_frontier, "results": cmd_results,
        "suggest-focus": cmd_suggest_focus,
        "lineage": cmd_lineage, "diff": cmd_diff,
        "archive": cmd_archive, "score": cmd_score,
        "snapshot": cmd_snapshot,
        "capture": cmd_capture, "trajectories": cmd_trajectories,
        "queue-evolve": cmd_queue_evolve, "pending-evolves": cmd_pending_evolves,
        "drain-queue": cmd_drain_queue,
        "skills": cmd_skills, "lessons": cmd_lessons, "competition": cmd_competition,
        "retro": cmd_retro,
        "memory": cmd_memory,
        "stuck-check": cmd_stuck_check,
        "fingerprint": cmd_fingerprint,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
