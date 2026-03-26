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


# ─── Focused Mutation (AutoResearch: single-file changes) ───────────────────

def suggest_focus_file(gen_id: str = "initial") -> dict:
    """
    Suggest which file to focus mutation on.
    Picks the file with the lowest individual structural score.
    Returns {file: str, score: float, reason: str}
    """
    gen_dir = GENERATIONS_DIR / gen_id
    struct = score_structural(gen_dir)
    file_scores = struct.get("files", {})

    if not file_scores:
        return {"file": None, "score": 0, "reason": "no files found"}

    # Sort by score ascending (worst first)
    ranked = sorted(file_scores.items(), key=lambda x: x[1]["score"])
    worst = ranked[0]
    rel_path = worst[0]
    info = worst[1]

    reason_parts = []
    if info["issues"]:
        reason_parts.append(f"issues: {', '.join(info['issues'])}")
    reason_parts.append(f"score: {info['score']}")

    return {
        "file": rel_path,
        "score": info["score"],
        "reason": "; ".join(reason_parts),
        "all_ranked": [{"file": r[0], "score": r[1]["score"]} for r in ranked[:5]],
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
    """Suggest which file to focus mutation on."""
    gen_id = args.gen_id if hasattr(args, "gen_id") and args.gen_id else "initial"
    suggestion = suggest_focus_file(gen_id)
    print(f"Focus file: {suggestion['file']}")
    print(f"  Score: {suggestion['score']}")
    print(f"  Reason: {suggestion['reason']}")
    if suggestion.get("all_ranked"):
        print("\n  Top 5 candidates (worst first):")
        for r in suggestion["all_ranked"]:
            print(f"    {r['score']:.3f}  {r['file']}")


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
    sub.add_parser("competition", help="Show competition config")

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
        "skills": cmd_skills, "competition": cmd_competition,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
