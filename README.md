# UltrAgent

**Self-referential self-improving agent configuration for Claude Code.**

UltrAgent evolves your Claude Code agent prompts, rules, and skills through an autonomous evolutionary loop. It combines ideas from three sources:

- [**HyperAgents**](https://github.com/facebookresearch/Hyperagents) (Meta FAIR) — Evolutionary archive, self-referential improvement, parent selection strategies
- [**AutoResearch**](https://github.com/karpathy/autoresearch) (Karpathy) — Fixed time budgets, single-metric keep/discard, `program.md` as research org code, `results.tsv` logging
- [**Hermes Agent**](https://github.com/NousResearch/hermes-agent) (Nous Research) — Real-world trajectory capture, auto-evolve hooks, closed learning loops
- [**Harness Design**](https://www.anthropic.com/engineering/harness-design) (Anthropic) — GAN-inspired generator/evaluator separation, skeptical LLM-Judge with few-shot calibration, sprint contracts

## How It Works

```
Normal Claude Code usage
  --> Agent succeeds/fails/gets corrected
  --> Hook captures trajectory automatically
  --> 3+ failures --> auto-queue evolve
  --> /ultragent evolve
      --> Select parent from evolutionary archive
      --> Check auto-evolve queue for real failure data
      --> MetaAgent (Opus) writes sprint contract (proposal)
      --> MetaAgent edits ONE agent file (focused mutation)
      --> Structural eval (instant) + Skeptical LLM-Judge (Sonnet)
      --> score > best? KEEP (commit to git frontier) : DISCARD
      --> Log EVERYTHING to results.tsv
  --> Promote best generation to live config
  --> Agent performs better
  --> Loop continues
```

## Architecture

```
~/.claude/ultragent/
|-- ua.py                  # Core CLI (20+ commands)
|-- evaluate.py            # 3-tier eval (structural + skeptical LLM-Judge + benchmarks)
|-- select_parent.py       # Self-improvable parent selection (6 strategies)
|-- meta_prompt.md         # MetaAgent system prompt (self-referential)
|-- program.md             # Research directives (YOUR control surface)
|-- config.json            # System parameters
|-- archive.jsonl          # Full generation archive
|-- results.tsv            # ALL experiments (kept + discarded + crashed)
|-- trajectories.jsonl     # Real-world agent usage data
|-- evolve_queue.jsonl     # Auto-queued evolves from failure detection
|-- frontier/              # Git repo -- only accepted improvements
|-- generations/           # Per-generation snapshots, diffs, reasoning
|-- benchmarks/            # Task-based evaluation suite
|-- hooks/                 # Claude Code hooks for auto-trajectory capture
`-- skill/SKILL.md         # /ultragent skill for Claude Code
```

## Quick Start

### 1. Install

```bash
# Copy to your Claude Code config
cp -r ultragent ~/.claude/ultragent

# Copy the skill
mkdir -p ~/.claude/skills/omc-learned/ultragent
cp skill/SKILL.md ~/.claude/skills/omc-learned/ultragent/SKILL.md

# Initialize (snapshots your current agent configs as gen_initial)
cd ~/.claude && PYTHONIOENCODING=utf-8 python ultragent/ua.py init
```

### 2. Install Hooks (Optional but Recommended)

Add to `~/.claude/settings.json` under `hooks.PostToolUse`:

```json
{
  "matcher": "tool == \"Agent\"",
  "hooks": [
    {
      "type": "command",
      "command": "bash ~/.claude/ultragent/hooks/post_tool_trajectory.sh"
    }
  ]
}
```

This auto-captures agent usage trajectories and queues evolves on repeated failures.

### 3. Evolve

```bash
# In Claude Code, run:
/ultragent evolve       # One evolution cycle
/ultragent evolve 10    # Ten cycles
/ultragent status       # See archive + metrics
/ultragent frontier     # Git log of accepted improvements
/ultragent results 20   # Full experiment log
/ultragent promote gen_0003  # Apply best gen to live config
```

## Key Concepts

### The Genome

Your "genome" is the set of agent configuration files that UltrAgent evolves:

| Path | Contents |
|------|----------|
| `agents/*.md` | 28 agent prompts (executor, debugger, planner, etc.) |
| `rules/*.md` | 8 rule files (coding style, testing, security, etc.) |
| `skills/omc-learned/*/SKILL.md` | Skill definitions |
| `CLAUDE.md` | Master config with delegation rules |

### Evolutionary Loop

Each generation:
1. **Select parent** — Pick the best-scoring prior generation (6 selection strategies)
2. **Sprint contract** — MetaAgent proposes what to change before editing
3. **Focused mutation** — Edit ONE file per generation (cleaner signal)
4. **Evaluate** — Structural scoring + skeptical LLM-Judge with few-shot calibration
5. **Keep/Discard** — Single score decides. Binary. No multi-objective confusion.
6. **Log everything** — `results.tsv` captures kept + discarded + crashed

### Trajectory-Driven Evolution

The highest-value signal comes from real usage, not synthetic benchmarks:

```bash
# Automatically captured by hooks during normal Claude Code use:
python ua.py trajectories

# Output:
# agents/executor.md     total=12  failures=4  rate=33%
# agents/debugger.md     total=8   failures=1  rate=12%
```

The MetaAgent prioritizes fixing agents with high real-world failure rates.

### Skeptical LLM-Judge

The evaluator is calibrated to be tough, not generous:

- **Few-shot examples** anchor scoring (weak=0.52, moderate=0.73, strong=0.88)
- Surface-level changes (adding headings, reformatting) score LOW
- Genuine behavioral improvements (concrete examples, decision trees) score HIGH
- Sprint contract execution is scored — overpromise/underdeliver is penalized

### Self-Referential Improvement

The MetaAgent can edit:
- `select_parent.py` — improve how it picks parents
- `program.md` — improve research directives that guide itself
- `meta_prompt.md` — improve its own instructions

The system doesn't just produce better agents — it gets better at producing better agents.

## Commands

| Command | Description |
|---------|-------------|
| `ua.py init` | Initialize (snapshot genome + create frontier) |
| `ua.py status` | Show archive + metrics |
| `ua.py evolve` | *(via skill)* Run evolution cycle |
| `ua.py select-parent [strategy]` | Pick parent generation |
| `ua.py suggest-focus [gen_id]` | Suggest file to mutate |
| `ua.py create-gen <parent> [patch] [scores] [reasoning]` | Register new generation |
| `ua.py keep <gen_id>` | Accept: commit to frontier |
| `ua.py discard <gen_id> [reason]` | Reject: log and move on |
| `ua.py promote <gen_id>` | Apply to live `~/.claude/` |
| `ua.py rollback` | Revert to previous promotion |
| `ua.py frontier` | Git log of accepted improvements |
| `ua.py results [N]` | Show last N experiments |
| `ua.py lineage [gen_id]` | Show evolution tree |
| `ua.py diff <gen_id>` | Show generation's patch |
| `ua.py capture <agent> <outcome> <desc>` | Capture trajectory |
| `ua.py trajectories [N]` | Show trajectory summary |
| `ua.py queue-evolve <agent_file> [reason]` | Queue auto-evolve |
| `ua.py pending-evolves` | Show queued evolves |
| `ua.py drain-queue` | Process evolve queue |

## Design Principles

1. **Single metric decides** — `score > best? KEEP : DISCARD`. No exceptions.
2. **Immutable evaluation** — `evaluate.py` and benchmarks are never modified by the MetaAgent.
3. **program.md is the control surface** — You iterate research directives, not code.
4. **Focused mutation** — One file per generation. Clean attribution.
5. **Log everything** — `results.tsv` captures all attempts. Discards contain signal.
6. **Trajectories > benchmarks** — Real usage data is 10x more valuable than synthetic scores.
7. **Skeptical evaluator** — Assume mediocre until proven otherwise.
8. **Sprint contracts** — Propose before implementing. Score execution against proposal.
9. **Simplicity wins** — Removing bloat while maintaining quality is the highest-scoring outcome.
10. **Self-referential** — The improvement process itself can be improved.

## Inspirations & References

- [HyperAgents: Self-referential self-improving agents](https://arxiv.org/abs/2603.19461) — Zhang et al., Meta FAIR, 2026
- [AutoResearch: Autonomous AI research](https://github.com/karpathy/autoresearch) — Andrej Karpathy, 2026
- [Hermes Agent: Self-improving AI agent platform](https://github.com/NousResearch/hermes-agent) — Nous Research
- [Harness Design for Long-Running Development](https://www.anthropic.com/engineering/harness-design) — Prithvi Rajasekaran, Anthropic, 2026
- [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) — Anthropic, 2024

## License

MIT
