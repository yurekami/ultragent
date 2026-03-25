# UltrAgent Research Program

> You're not editing agent prompts directly. You're programming the instructions
> for the AI that edits agent prompts. This file IS the research org code.
> — Adapted from Karpathy's AutoResearch philosophy

## Objective

Minimize `val_score` (lower = more issues found by structural eval, inverted to
maximize quality). The single optimization target is the **aggregate score**
returned by `evaluate.py`. Higher is better.

## Current Research Focus

### Phase 1: Low-Hanging Fruit (Generations 1-10)
- Fix structural issues: 15+ agent files missing top-level headings
- Reduce file length violations (4 files >500 lines)
- Ensure every agent has concrete examples and code blocks
- Add missing cross-references between agents and CLAUDE.md

### Phase 2: Prompt Quality (Generations 11-25)
- Improve agent prompt specificity (replace vague instructions with concrete ones)
- Add decision trees for common scenarios
- Strengthen error handling guidance in agent prompts
- Ensure all agents reference the user's coding philosophy (immutability, TDD, verification-first)

### Phase 3: Architecture (Generations 26-50)
- Optimize agent delegation boundaries
- Reduce redundancy across agent prompts
- Improve skill orchestration patterns
- Evolve select_parent.py for better exploration/exploitation balance

## Signal Priority (highest to lowest)

1. **Trajectory data** — Real failures from normal usage. If trajectories show an agent
   failing repeatedly, that agent gets priority over structural scoring.
2. **Auto-evolve queue** — Hook-detected failure patterns. 3+ failures auto-queues evolve.
3. **Structural scoring** — Format, headings, length issues. Easy wins but low impact.
4. **LLM-Judge scoring** — Subjective quality assessment. Useful but synthetic.

## Constraints

- **Tool call budget**: 12 calls max per MetaAgent run. Write full file in ONE call.
- **Immutable files**: `evaluate.py`, `benchmarks/`, `ua.py` — never modify these
- **Evolvable meta-files**: `program.md`, `select_parent.py`, `meta_prompt.md`
- **Single metric**: One aggregate score decides keep/discard. No multi-objective confusion.
- **Focused mutation**: ONE file per generation. Auto-evolve queue overrides suggest-focus.
- **Trajectory-first**: When trajectory data exists for the focus file, prioritize fixing
  the failure patterns shown in trajectories over generic improvements.

## What NOT To Try

- Don't add emoji to agent prompts (user preference)
- Don't remove security checks or validation requirements
- Don't bloat prompts beyond 800 lines
- Don't add meta-commentary about being an AI
- Don't contradict the user's coding philosophy (immutability, verification-first, anti-genie rules)
- Don't try to game structural scoring by just adding headings without improving content

## Success Criteria

A generation is KEPT if its aggregate score > current best aggregate score.
Everything else is DISCARDED (git reset, logged in results.tsv).

The frontier (git history) tracks only accepted improvements.
results.tsv tracks EVERYTHING — kept, discarded, and crashed.

## Lessons Learned

_Updated automatically as generations accumulate. Check results.tsv for patterns._

- (None yet — first cycle pending)

## How to Iterate on This File

After reviewing overnight results in `results.tsv`:
1. Identify which experiment types yielded improvements
2. Identify which experiment types consistently fail
3. Update "Current Research Focus" with refined priorities
4. Add failure patterns to "What NOT To Try"
5. Add successful patterns to a new "What Works" section
