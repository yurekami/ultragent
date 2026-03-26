# UltrAgent

**Self-improving agent configuration for Claude Code.**

Give it your agent prompts. Go to sleep. Wake up to better agents.

UltrAgent runs an autonomous evolutionary loop on your Claude Code configuration — agent prompts, rules, and skills. Each cycle: analyze, edit one file, evaluate with 3 diverse judges, keep or discard. Real-world trajectory data drives what gets optimized. The system improves itself while improving your agents.

## Quick Start

### As a Plugin (recommended)

```bash
git clone https://github.com/yurekami/ultragent.git
claude --plugin-dir ./ultragent
```

Then in Claude Code:

```
/ultragent:ultragent init          # Initialize (snapshot your current agent configs)
/ultragent:ultragent evolve 5      # Run 5 evolution cycles
/ultragent:ultragent status        # See progress
```

### As a Standalone Skill

```bash
git clone https://github.com/yurekami/ultragent.git
cp -r ultragent ~/.claude/ultragent
mkdir -p ~/.claude/skills/ultragent
cp ultragent/skills/ultragent/SKILL.md ~/.claude/skills/ultragent/SKILL.md
cp -r ultragent/skills/ultragent/references ~/.claude/skills/ultragent/

cd ~/.claude && PYTHONIOENCODING=utf-8 python ultragent/ua.py init
```

## How It Works

```
Normal Claude Code usage
  → Hooks capture agent success/failure automatically
  → 3+ failures for same agent → auto-queue evolve

/ultragent evolve
  → Amdahl's law picks highest-impact file (usage × headroom × responsiveness)
  → Analysis Loop (Sonnet): investigate + write sprint contract
  → Editor (Opus): execute the contract — edit ONE file
  → Smoke test → 3 diverse judges (behavioral/quality/philosophy) → majority vote
  → score > best? KEEP : DISCARD
  → Stuck? → REFINE (3 discards) → PIVOT (5) → ESCALATE (2 pivots)
  → Session reflection → feed insight into next cycle
  → Loop continues

/ultragent promote gen_0012
  → Apply best generation to live ~/.claude/ config
  → Your agents perform better
```

Each experiment takes ~5 minutes. That's ~12/hour, ~100 overnight.

## Architecture

Built from patterns across 7 research projects:

| Source | What UltrAgent Uses |
|--------|-------------------|
| [HyperAgents](https://arxiv.org/abs/2603.19461) (Meta FAIR) | Evolutionary archive, self-referential improvement |
| [AutoResearch](https://github.com/karpathy/autoresearch) (Karpathy) | Single-metric keep/discard, `program.md`, `results.tsv` |
| [The Agentic Researcher](https://arxiv.org/abs/2603.15914) (ZIB-IOL) | Commandment-based scoring, smoke test, `/retro` |
| [DeerFlow](https://github.com/bytedance/deer-flow) (ByteDance) | Loop detection, structured memory, error recovery |
| [codex-autoresearch](https://github.com/leo-lilinxiao/codex-autoresearch) | PIVOT/REFINE stuck recovery, protocol fingerprint check |
| [AutoKernel](https://github.com/RightNow-AI/autokernel) (RightNow AI) | Amdahl's law focus selection |
| [Agent Laboratory](https://github.com/SamuelSchmidgall/AgentLaboratory) (JHU) | In-session reflection, diverse judges, role splitting |
| [OpenClaw](https://github.com/openclaw/openclaw) | Budget-aware context engine, spawn depth tracking |
| [DeepTutor](https://github.com/HKUDS/DeepTutor) (HKU) | Dual-loop architecture, circuit breaker, citation-tracked facts |

### Dual-Loop Architecture

```
Orchestrator (SKILL.md evolve loop)
  │
  ├── Analysis Loop (Sonnet, cheap)
  │     └── Produces: investigation_report.md + sprint_contract.md
  │
  ├── Editor (Opus, powerful)
  │     └── Executes the sprint contract on ONE focus file
  │
  ├── 3 Judges IN PARALLEL (Sonnet)
  │     ├── Behavioral: "Does this improve real-world agent behavior?"
  │     ├── Quality: "Is this well-written and concise?"
  │     └── Philosophy: "Does this align with verification-first principles?"
  │
  └── Stuck Recovery
        └── REFINE → PIVOT → ESCALATE → SOFT BLOCKER
```

### Plugin Structure

```
ultragent/
├── .claude-plugin/plugin.json      # Plugin manifest
├── skills/ultragent/
│   ├── SKILL.md                    # Lean entrypoint (110 lines)
│   └── references/                 # Loaded on demand
│       ├── evolve-protocol.md      # Steps 1-5b
│       ├── evaluation-protocol.md  # Steps 6-8
│       └── stuck-recovery.md       # PIVOT/REFINE + overnight
├── hooks/
│   ├── hooks.json                  # Auto-registered trajectory hook
│   └── post_tool_trajectory.sh     # Captures agent success/failure
├── ua.py                           # Core CLI (30+ commands)
├── evaluate.py                     # 3-tier eval (structural + smoke + judges)
├── context_engine.py               # Budget-aware context assembly
├── circuit_breaker.py              # LLM API health monitoring
├── select_parent.py                # Self-improvable parent selection (6 strategies)
├── meta_prompt.md                  # MetaAgent system prompt
├── program.md                      # Research directives (YOUR control surface)
└── config.json                     # System parameters
```

## The Genome

Your "genome" — the files UltrAgent evolves:

| Path | Contents |
|------|----------|
| `agents/*.md` | Agent prompts (executor, debugger, planner, etc.) |
| `rules/*.md` | Rule files (coding style, testing, security, etc.) |
| `skills/omc-learned/*/SKILL.md` | Skill definitions |
| `CLAUDE.md` | Master config with delegation rules |

## Commands

| Command | Description |
|---------|-------------|
| `status` | Archive overview, best score, keep/discard counts |
| `evolve [N]` | Run N evolution cycles (default: 1) |
| `retro` | Retrospective: analyze patterns, update program.md |
| `promote <gen_id>` | Apply generation to live config |
| `rollback` | Revert to previous promotion |
| `frontier` | Git log of accepted improvements |
| `results [N]` | Full experiment log (kept + discarded + crashed) |
| `lineage` | Evolution tree visualization |
| `memory` | Structured evolution memory (file insights, strategy win rates) |
| `stuck-check` | Check if evolution is stuck, recommend recovery |
| `fingerprint` | Protocol re-anchoring checklist |
| `circuit` | Circuit breaker: LLM API health |
| `context [estimate\|assemble]` | Context engine: token budget analysis |
| `spawn-log [gen_id]` | Spawn depth audit trail |
| `reflections` | In-session learning insights |
| `suggest-focus` | Amdahl's law impact-ranked file suggestions |

## Key Features

### Amdahl's Law Focus Selection

Picks the file with highest `usage × failure_bonus × headroom × responsiveness × recency` — optimizing the agent you use most, not just the one with the worst score.

### Trajectory-Driven Evolution

Hooks capture real agent usage during normal Claude Code sessions. The MetaAgent prioritizes fixing agents with high real-world failure rates.

### Diverse Ensemble Evaluation

Three judges with different lenses (behavioral, quality, philosophy) reduce groupthink. A change that's useful but messy gets a proper 2-1 split vote.

### PIVOT/REFINE Stuck Recovery

3 consecutive discards → REFINE (switch focus file). 5 → PIVOT (change parent selection strategy). 2 PIVOTs → ESCALATE. Resets on any KEEP.

### Circuit Breaker

Monitors LLM API health. If Opus is down, pauses the loop instead of burning budget on failed calls. Three-state FSM: closed → open → half-open.

### In-Session Reflection

After every KEEP, generates a structured insight that feeds into the next MetaAgent's context. Within-session compounding — generation 15 knows what worked in generations 8, 10, 12.

### Protocol Fingerprint Check

Every 10 generations, self-checks 10 key protocol rules. If any are forgotten (context drift after compaction), re-reads from disk. Frequency increases after compaction events.

### Budget-Aware Context Engine

Assembles MetaAgent context within token budgets. Priority-based: critical sections (focus file) never dropped, optional sections (old results) compressed or dropped first.

## Design Principles

1. **Single metric decides** — score > best? KEEP : DISCARD. No exceptions.
2. **Immutable evaluation** — evaluate.py and benchmarks are never modified.
3. **program.md is the control surface** — You iterate research directives, not code.
4. **Focused mutation** — One file per generation. Clean attribution.
5. **Log everything** — results.tsv captures all attempts. Discards contain signal.
6. **Trajectories > benchmarks** — Real usage data is 10x more valuable than synthetic scores.
7. **Self-referential** — The system can improve its own improvement process.
8. **Simplicity wins** — Removing bloat while maintaining quality is a valid win.
9. **Sprint contracts** — Propose before implementing. Score execution against proposal.
10. **Budget awareness** — Circuit breaker + context engine prevent waste.

## Inspirations

- [HyperAgents](https://arxiv.org/abs/2603.19461) — Zhang et al., Meta FAIR
- [AutoResearch](https://github.com/karpathy/autoresearch) — Andrej Karpathy
- [The Agentic Researcher](https://arxiv.org/abs/2603.15914) — Zimmer et al., ZIB-IOL
- [DeerFlow](https://github.com/bytedance/deer-flow) — ByteDance
- [codex-autoresearch](https://github.com/leo-lilinxiao/codex-autoresearch) — Leo Li
- [AutoKernel](https://github.com/RightNow-AI/autokernel) — RightNow AI
- [Agent Laboratory](https://arxiv.org/abs/2501.04227) — Schmidgall et al., JHU
- [OpenClaw](https://github.com/openclaw/openclaw)
- [DeepTutor](https://github.com/HKUDS/DeepTutor) — HKU
- [Harness Design](https://www.anthropic.com/engineering/harness-design) — Anthropic
- [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) — Anthropic
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — Nous Research

## License

MIT
