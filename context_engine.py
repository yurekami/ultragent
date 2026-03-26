#!/usr/bin/env python3
"""
UltrAgent Context Engine — Budget-aware context assembly for MetaAgent.

Inspired by OpenClaw's pluggable context engine lifecycle:
  bootstrap → ingest → assemble(tokenBudget, model) → compact

Instead of dumping everything into the MetaAgent prompt, the context engine:
1. Assigns priority levels to each context section
2. Estimates token cost per section
3. Assembles sections within a budget, dropping lowest-priority content first
4. Compresses verbose sections when budget is tight

Usage:
    python context_engine.py assemble [--budget TOKENS] [--model opus|sonnet]
    python context_engine.py estimate                    # show token estimates per section
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ─── Paths ───────────────────────────────────────────────────────────────────

HOME = Path.home()
ULTRAGENT_DIR = HOME / ".claude" / "ultragent"
PROGRAM_FILE = ULTRAGENT_DIR / "program.md"
RESULTS_FILE = ULTRAGENT_DIR / "results.tsv"
LESSONS_FILE = ULTRAGENT_DIR / "lessons.jsonl"
EVOLUTION_MEMORY_FILE = ULTRAGENT_DIR / "evolution_memory.json"
META_PROMPT_FILE = ULTRAGENT_DIR / "meta_prompt.md"

# ─── Token Estimation ────────────────────────────────────────────────────────

# Rough estimate: 1 token ≈ 4 characters for English text/markdown
CHARS_PER_TOKEN = 4

# Model context budgets (tokens reserved for MetaAgent context block)
# These are NOT the model's full context — just the budget for the <context> block
MODEL_CONTEXT_BUDGETS = {
    "opus": 50000,      # Opus has 1M context, allocate 50K for context block
    "sonnet": 20000,    # Sonnet has 200K, allocate 20K for context block
    "haiku": 10000,     # Haiku — tight budget
}


def estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


# ─── Priority Levels ─────────────────────────────────────────────────────────

# Higher number = higher priority = last to be dropped
PRIORITY_CRITICAL = 100    # Never drop: focus file, scoring rules
PRIORITY_HIGH = 80         # Drop only under extreme pressure: research directives, lessons
PRIORITY_MEDIUM = 60       # Drop when budget tight: archive status, evolution memory
PRIORITY_LOW = 40          # Drop first: full results history, verbose lessons
PRIORITY_OPTIONAL = 20     # Include only if space allows: extra context


# ─── Context Sections ────────────────────────────────────────────────────────

class ContextSection:
    """A single section of the MetaAgent's context block."""

    def __init__(
        self,
        name: str,
        header: str,
        content: str,
        priority: int,
        compressible: bool = False,
        compress_ratio: float = 0.5,
    ):
        self.name = name
        self.header = header
        self.content = content
        self.priority = priority
        self.compressible = compressible
        self.compress_ratio = compress_ratio
        self._compressed = False

    @property
    def full_text(self) -> str:
        return f"## {self.header}\n{self.content}"

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.full_text)

    def compress(self) -> "ContextSection":
        """Return a compressed version of this section."""
        if not self.compressible or self._compressed:
            return self

        lines = self.content.strip().split("\n")
        target_lines = max(3, int(len(lines) * self.compress_ratio))

        # Keep first line (usually a summary) + last N lines (most recent)
        if len(lines) <= target_lines:
            return self

        compressed_lines = (
            lines[:2]
            + [f"... ({len(lines) - target_lines} lines compressed) ..."]
            + lines[-(target_lines - 3):]
        )
        compressed = ContextSection(
            name=self.name,
            header=self.header + " (compressed)",
            content="\n".join(compressed_lines),
            priority=self.priority,
            compressible=False,
        )
        compressed._compressed = True
        return compressed


# ─── Section Builders ────────────────────────────────────────────────────────

def build_research_directives() -> ContextSection | None:
    """Load program.md as research directives."""
    if not PROGRAM_FILE.exists():
        return None
    content = PROGRAM_FILE.read_text(encoding="utf-8")
    return ContextSection(
        name="research_directives",
        header="Research Directives",
        content=content,
        priority=PRIORITY_HIGH,
        compressible=True,
        compress_ratio=0.4,
    )


def build_focus_file(focus_path: str, snapshot_dir: Path | None = None) -> ContextSection | None:
    """Load the focus file content — CRITICAL priority, never dropped."""
    if not focus_path:
        return None

    # Try snapshot dir first, then live config
    file_path = None
    if snapshot_dir:
        candidate = snapshot_dir / focus_path
        if candidate.exists():
            file_path = candidate
    if not file_path:
        candidate = HOME / ".claude" / focus_path
        if candidate.exists():
            file_path = candidate

    if not file_path or not file_path.exists():
        return ContextSection(
            name="focus_file",
            header=f"Current Genome: {focus_path}",
            content=f"(File not found: {focus_path})",
            priority=PRIORITY_CRITICAL,
        )

    content = file_path.read_text(encoding="utf-8")
    return ContextSection(
        name="focus_file",
        header=f"Current Genome: {focus_path}",
        content=content + "\n\nNOTE: This file is PRE-LOADED. Do NOT waste tool calls re-reading it.",
        priority=PRIORITY_CRITICAL,
    )


def build_archive_status() -> ContextSection | None:
    """Build archive status summary."""
    try:
        sys.path.insert(0, str(ULTRAGENT_DIR))
        from ua import metadata_read, archive_read, config_read
        meta = metadata_read()
        if not meta:
            return None

        cfg = config_read()
        entries = archive_read()
        total = meta.get("total_generations", 0)
        kept = meta.get("total_kept", 0)
        discarded = meta.get("total_discarded", 0)
        crashed = meta.get("total_crashed", 0)
        best_score = meta.get("best_score", 0)
        best_gen = meta.get("best_gen_id", "N/A")

        lines = [
            f"Generations: {total} | Kept: {kept} | Discarded: {discarded} | Crashed: {crashed}",
            f"Best: {best_gen} (score: {best_score:.4f})",
            f"Selection: {cfg.get('parent_selection_strategy', 'best')}",
            f"Consecutive discards: {meta.get('consecutive_discards', 0)}",
            f"Pivot count: {meta.get('pivot_count', 0)}",
        ]

        return ContextSection(
            name="archive_status",
            header="Archive Status",
            content="\n".join(lines),
            priority=PRIORITY_MEDIUM,
        )
    except Exception:
        return None


def build_recent_results(n: int = 10) -> ContextSection | None:
    """Build recent results summary."""
    try:
        sys.path.insert(0, str(ULTRAGENT_DIR))
        from ua import results_read
        rows = results_read(n)
        if not rows:
            return None

        lines = [
            f"{'status':>8}  {'gen_id':>12}  {'score':>8}  {'best':>8}  {'focus_file':<30}  description",
            "-" * 90,
        ]
        for r in rows:
            status = r.get("status", "?")
            marker = {"keep": " KEEP", "discard": " DISC", "crash": "CRASH",
                       "refine": "REFNE", "pivot": "PIVOT"}.get(status, "  ???")
            lines.append(
                f"{marker:>8}  {r.get('gen_id', '?'):>12}  {r.get('score', '?'):>8}  "
                f"{r.get('best_score', '?'):>8}  {r.get('focus_file', ''):.<30}  "
                f"{r.get('description', '')[:40]}"
            )

        return ContextSection(
            name="recent_results",
            header="Recent Experiments (results.tsv)",
            content="\n".join(lines),
            priority=PRIORITY_LOW,
            compressible=True,
            compress_ratio=0.5,
        )
    except Exception:
        return None


def build_lessons(focus_file: str = "") -> ContextSection | None:
    """Build lessons for the focus file."""
    if not LESSONS_FILE.exists():
        return None

    try:
        entries = []
        for line in LESSONS_FILE.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                entries.append(json.loads(line))

        if focus_file:
            relevant = [l for l in entries if l.get("focus_file") == focus_file]
        else:
            relevant = entries[-10:]

        if not relevant:
            return ContextSection(
                name="lessons",
                header=f"Lessons for {focus_file}" if focus_file else "Lessons",
                content="No prior lessons for this file.",
                priority=PRIORITY_HIGH,
            )

        lines = ["These are REAL outcomes from prior attempts. DO NOT repeat failed approaches.\n"]
        for l in relevant[-8:]:
            icon = "+" if l.get("outcome") == "keep" else "-"
            lines.append(
                f"[{icon}] {l.get('gen_id', '?')} | {l.get('strategy', '?')} | "
                f"{l.get('lesson', '')[:120]}"
            )

        return ContextSection(
            name="lessons",
            header=f"Lessons for {focus_file} (MetaClaw: what NOT to do)",
            content="\n".join(lines),
            priority=PRIORITY_HIGH,
            compressible=True,
            compress_ratio=0.6,
        )
    except Exception:
        return None


def build_evolution_memory() -> ContextSection | None:
    """Build structured evolution memory context."""
    if not EVOLUTION_MEMORY_FILE.exists():
        return ContextSection(
            name="evolution_memory",
            header="Evolution Memory",
            content="No evolution memory yet. Run /ultragent retro after 5+ generations.",
            priority=PRIORITY_OPTIONAL,
        )

    try:
        mem = json.loads(EVOLUTION_MEMORY_FILE.read_text(encoding="utf-8"))
        ctx = mem.get("evolution_context", {})
        facts = mem.get("facts", [])

        lines = [
            f"Phase: {ctx.get('phase', '?')} | Trend: {ctx.get('score_trend', '?')} | "
            f"Keep rate: {ctx.get('keep_rate', 0):.0%}",
        ]

        # File insights (compact)
        fi = mem.get("file_insights", {})
        if fi:
            lines.append("\nFile insights:")
            for f, insight in sorted(fi.items(), key=lambda x: x[1].get("responsiveness", 0), reverse=True)[:5]:
                lines.append(
                    f"  {f}: {insight.get('responsiveness', 0):.0%} keep, "
                    f"{insight.get('total_attempts', 0)} attempts"
                    + (f", best via {insight['best_strategy']}" if insight.get("best_strategy") else "")
                )

        # Facts (most important)
        if facts:
            lines.append("\nKey facts:")
            for fact in facts[:5]:
                lines.append(f"  [{fact.get('category', '?')}] {fact['content'][:100]}")

        return ContextSection(
            name="evolution_memory",
            header="Evolution Memory (structured insights from retro)",
            content="\n".join(lines),
            priority=PRIORITY_MEDIUM,
            compressible=True,
            compress_ratio=0.5,
        )
    except Exception:
        return None


def build_iterations_left(remaining: int) -> ContextSection:
    """Build iterations remaining context."""
    return ContextSection(
        name="iterations_left",
        header=f"Iterations Left: {remaining}",
        content=f"{remaining} cycles remaining. Budget ambition accordingly.",
        priority=PRIORITY_MEDIUM,
    )


def build_focus_suggestion(suggestion: dict) -> ContextSection | None:
    """Build focus file suggestion context."""
    if not suggestion or not suggestion.get("file"):
        return None

    lines = [
        f"Target: {suggestion['file']}",
        f"Modify ONLY this file (focused mutation mode).",
    ]
    if suggestion.get("impact_score"):
        lines.append(f"Impact score: {suggestion['impact_score']} (Amdahl's law weighted)")
    if suggestion.get("reason"):
        lines.append(f"Reason: {suggestion['reason']}")

    return ContextSection(
        name="focus_suggestion",
        header="Focus File",
        content="\n".join(lines),
        priority=PRIORITY_CRITICAL,
    )


# ─── Assembly Engine ─────────────────────────────────────────────────────────

def assemble_context(
    sections: list[ContextSection],
    token_budget: int,
    model: str = "opus",
) -> dict:
    """
    Assemble context sections within a token budget.

    Strategy (OpenClaw-inspired):
    1. Sort by priority (highest first)
    2. Include all CRITICAL sections unconditionally
    3. For remaining budget, add sections in priority order
    4. If over budget, compress compressible sections (highest priority compressed last)
    5. If still over budget, drop lowest-priority sections

    Returns: {
        "context": str,           # The assembled context block
        "sections_included": [...],
        "sections_dropped": [...],
        "sections_compressed": [...],
        "total_tokens": int,
        "budget": int,
        "budget_used_pct": float,
    }
    """
    # Sort by priority descending
    sorted_sections = sorted(sections, key=lambda s: s.priority, reverse=True)

    included = []
    dropped = []
    compressed = []
    total_tokens = 0

    # Pass 1: Include all sections, track total
    for section in sorted_sections:
        included.append(section)
        total_tokens += section.tokens

    # Pass 2: If over budget, compress compressible sections (lowest priority first)
    if total_tokens > token_budget:
        # Sort included by priority ascending for compression (compress low-priority first)
        for i, section in enumerate(sorted(included, key=lambda s: s.priority)):
            if total_tokens <= token_budget:
                break
            if section.compressible and not section._compressed:
                old_tokens = section.tokens
                new_section = section.compress()
                token_savings = old_tokens - new_section.tokens
                if token_savings > 0:
                    # Replace in included list
                    idx = included.index(section)
                    included[idx] = new_section
                    total_tokens -= token_savings
                    compressed.append(section.name)

    # Pass 3: If still over budget, drop lowest-priority non-critical sections
    if total_tokens > token_budget:
        # Sort by priority ascending (drop lowest first)
        to_drop = []
        for section in sorted(included, key=lambda s: s.priority):
            if total_tokens <= token_budget:
                break
            if section.priority < PRIORITY_CRITICAL:
                to_drop.append(section)
                total_tokens -= section.tokens

        for section in to_drop:
            included.remove(section)
            dropped.append(section.name)

    # Build the final context string (in priority order — critical first)
    included.sort(key=lambda s: s.priority, reverse=True)
    context_parts = [section.full_text for section in included]
    context = "\n\n".join(context_parts)

    return {
        "context": context,
        "sections_included": [s.name for s in included],
        "sections_dropped": dropped,
        "sections_compressed": compressed,
        "total_tokens": estimate_tokens(context),
        "budget": token_budget,
        "budget_used_pct": round(estimate_tokens(context) / token_budget * 100, 1) if token_budget > 0 else 0,
    }


def build_metaagent_context(
    focus_file: str = "",
    focus_suggestion: dict | None = None,
    snapshot_dir: Path | None = None,
    remaining_cycles: int = 1,
    model: str = "opus",
    token_budget: int | None = None,
) -> dict:
    """
    Build the complete MetaAgent context block with budget awareness.

    This replaces the flat context dump in SKILL.md Step 4.

    Returns the same dict as assemble_context().
    """
    if token_budget is None:
        token_budget = MODEL_CONTEXT_BUDGETS.get(model, 50000)

    # Build all sections
    sections = []

    s = build_focus_suggestion(focus_suggestion or {"file": focus_file})
    if s:
        sections.append(s)

    s = build_focus_file(focus_file, snapshot_dir)
    if s:
        sections.append(s)

    s = build_research_directives()
    if s:
        sections.append(s)

    s = build_archive_status()
    if s:
        sections.append(s)

    s = build_recent_results(10)
    if s:
        sections.append(s)

    s = build_lessons(focus_file)
    if s:
        sections.append(s)

    s = build_evolution_memory()
    if s:
        sections.append(s)

    sections.append(build_iterations_left(remaining_cycles))

    return assemble_context(sections, token_budget, model)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def cmd_estimate() -> None:
    """Show token estimates for each context section."""
    sections = []

    s = build_research_directives()
    if s:
        sections.append(s)
    s = build_archive_status()
    if s:
        sections.append(s)
    s = build_recent_results(10)
    if s:
        sections.append(s)
    s = build_lessons()
    if s:
        sections.append(s)
    s = build_evolution_memory()
    if s:
        sections.append(s)

    total = sum(s.tokens for s in sections)

    print("=== Context Engine: Token Estimates ===\n")
    print(f"  {'Section':<35} {'Tokens':>8} {'Priority':>10} {'Compressible':>14}")
    print(f"  {'-'*35} {'-'*8} {'-'*10} {'-'*14}")
    for s in sorted(sections, key=lambda x: x.priority, reverse=True):
        comp = "yes" if s.compressible else "no"
        print(f"  {s.name:<35} {s.tokens:>8} {s.priority:>10} {comp:>14}")
    print(f"  {'-'*35} {'-'*8}")
    print(f"  {'TOTAL':<35} {total:>8}")
    print()

    for model, budget in sorted(MODEL_CONTEXT_BUDGETS.items()):
        pct = total / budget * 100 if budget > 0 else 0
        status = "OK" if pct < 80 else ("TIGHT" if pct < 100 else "OVER")
        print(f"  {model}: {total}/{budget} tokens ({pct:.0f}%) [{status}]")


def cmd_assemble(budget: int | None, model: str) -> None:
    """Assemble and print the context."""
    if budget is None:
        budget = MODEL_CONTEXT_BUDGETS.get(model, 50000)

    result = build_metaagent_context(
        focus_file="",
        remaining_cycles=1,
        model=model,
        token_budget=budget,
    )

    print(f"=== Context Assembly ({model}, budget={result['budget']}) ===\n")
    print(f"  Tokens used: {result['total_tokens']} ({result['budget_used_pct']}%)")
    print(f"  Sections included: {', '.join(result['sections_included'])}")
    if result["sections_compressed"]:
        print(f"  Sections compressed: {', '.join(result['sections_compressed'])}")
    if result["sections_dropped"]:
        print(f"  Sections DROPPED: {', '.join(result['sections_dropped'])}")
    print()
    print(result["context"][:2000])
    if len(result["context"]) > 2000:
        print(f"\n  ... ({len(result['context'])} chars total, truncated for display)")


def main():
    parser = argparse.ArgumentParser(description="UltrAgent Context Engine")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("estimate", help="Show token estimates per section")

    asm = sub.add_parser("assemble", help="Assemble context within budget")
    asm.add_argument("--budget", type=int, default=None, help="Token budget override")
    asm.add_argument("--model", default="opus", choices=["opus", "sonnet", "haiku"])

    args = parser.parse_args()

    if args.command == "estimate":
        cmd_estimate()
    elif args.command == "assemble":
        cmd_assemble(args.budget, args.model)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
