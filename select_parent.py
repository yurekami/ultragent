"""
UltrAgent — Self-Improvable Parent Selection

This file is part of the evolvable genome. The MetaAgent CAN and SHOULD
modify this file to improve the parent selection strategy.

The default implementation uses score_child_prop: score-weighted probability
with exponential penalty for already-explored parents. This balances
exploitation (high-scoring parents) with exploration (under-explored parents).

To implement a new strategy, modify select_parent() below. The function
receives the full archive and must return a valid gen_id.

Available data per archive entry:
  - gen_id: str
  - parent_gen_id: str | None
  - score: float (0-1 aggregate)
  - scores: {structural, llm_judge, task_based, aggregate}
  - valid: bool
  - created_at: ISO timestamp
  - promoted: bool
  - patch_files_count: int
"""

import math
import random


def select_parent(archive: list[dict], strategy: str = "score_child_prop") -> str:
    """
    Select the best parent generation to branch from.

    Args:
        archive: List of archive entries (from archive.jsonl)
        strategy: Selection strategy name

    Returns:
        gen_id of the selected parent
    """
    valid = [e for e in archive if e.get("valid", True)]
    if not valid:
        return "initial"

    if strategy == "best":
        return max(valid, key=lambda e: e.get("score", 0))["gen_id"]

    elif strategy == "latest":
        return valid[-1]["gen_id"]

    elif strategy == "random":
        return random.choice(valid)["gen_id"]

    elif strategy == "score_prop":
        return _score_proportional(valid)

    elif strategy == "score_child_prop":
        return _score_child_proportional(valid, archive)

    elif strategy == "ucb1":
        # Upper Confidence Bound — balances exploitation and exploration
        return _ucb1_selection(valid, archive)

    elif strategy == "novelty":
        # Prefer parents whose children had the most diverse patches
        return _novelty_selection(valid, archive)

    else:
        # Fallback: best
        return max(valid, key=lambda e: e.get("score", 0))["gen_id"]


def _score_proportional(valid: list[dict]) -> str:
    """Sigmoid-weighted probability proportional to score."""
    weights = []
    for e in valid:
        s = e.get("score", 0)
        w = 1 / (1 + math.exp(-5 * (s - 0.5)))
        weights.append(w)

    total = sum(weights)
    if total == 0:
        return random.choice(valid)["gen_id"]

    r = random.random() * total
    cumulative = 0
    for entry, w in zip(valid, weights):
        cumulative += w
        if r <= cumulative:
            return entry["gen_id"]
    return valid[-1]["gen_id"]


def _score_child_proportional(valid: list[dict], archive: list[dict]) -> str:
    """Score-weighted with exponential penalty for already-explored parents."""
    child_counts = {}
    for e in archive:
        pid = e.get("parent_gen_id")
        if pid:
            child_counts[pid] = child_counts.get(pid, 0) + 1

    weights = []
    for e in valid:
        s = e.get("score", 0)
        children = child_counts.get(e["gen_id"], 0)
        w = s * math.exp(-0.5 * children)
        weights.append(w)

    total = sum(weights)
    if total == 0:
        return random.choice(valid)["gen_id"]

    r = random.random() * total
    cumulative = 0
    for entry, w in zip(valid, weights):
        cumulative += w
        if r <= cumulative:
            return entry["gen_id"]
    return valid[-1]["gen_id"]


def _ucb1_selection(valid: list[dict], archive: list[dict]) -> str:
    """UCB1: balance mean reward with exploration bonus."""
    total_gens = len(archive)
    child_counts = {}
    for e in archive:
        pid = e.get("parent_gen_id")
        if pid:
            child_counts[pid] = child_counts.get(pid, 0) + 1

    best_id = valid[0]["gen_id"]
    best_ucb = -1

    for e in valid:
        s = e.get("score", 0)
        n = child_counts.get(e["gen_id"], 0) + 1  # +1 to avoid log(0)
        exploration = math.sqrt(2 * math.log(total_gens + 1) / n)
        ucb = s + exploration
        if ucb > best_ucb:
            best_ucb = ucb
            best_id = e["gen_id"]

    return best_id


def _novelty_selection(valid: list[dict], archive: list[dict]) -> str:
    """Prefer parents that produced diverse offspring (by patch size variance)."""
    child_patches: dict[str, list[int]] = {}
    for e in archive:
        pid = e.get("parent_gen_id")
        if pid:
            child_patches.setdefault(pid, []).append(e.get("patch_files_count", 0))

    # Parents with no children get maximum novelty score
    novelty_scores = {}
    for e in valid:
        gid = e["gen_id"]
        patches = child_patches.get(gid, [])
        if not patches:
            novelty_scores[gid] = float("inf")  # Unexplored = maximum novelty
        else:
            mean_p = sum(patches) / len(patches)
            variance = sum((p - mean_p) ** 2 for p in patches) / len(patches)
            novelty_scores[gid] = variance + (1.0 / (len(patches) + 1))

    best_id = max(valid, key=lambda e: novelty_scores.get(e["gen_id"], 0))["gen_id"]
    return best_id
