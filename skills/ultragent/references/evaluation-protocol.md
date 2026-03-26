# Evaluation Protocol — Steps 6-8

Ensemble pairwise evaluation with diverse judge personalities.
Read this when the evolve loop reaches the evaluation phase.

## Step 6: Evaluate (Single Score)

**Structural eval (instant):**
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../evaluate.py structural $NEXT_GEN
```

**Ensemble Pairwise Evaluation (AI Scientist + Scientific Taste):**
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../evaluate.py prepare-judge $NEXT_GEN
```

Spawn **3 independent judge agents IN PARALLEL** (single message, 3 Agent calls).
Each is a `code-reviewer` (model: sonnet) with the **same judge context** but
**different evaluation personalities** (Agent Laboratory ReviewersAgent pattern):

| Judge | Personality | Focus |
|-------|------------|-------|
| Judge 1 (Behavioral) | "You are a pragmatic reviewer focused on real-world agent behavior. Evaluate whether the changes would actually improve how the agent performs in practice. Prioritize concrete behavioral improvements over cosmetic changes." | Does this change how the agent would act in a real scenario? |
| Judge 2 (Quality) | "You are a code quality reviewer focused on clarity, conciseness, and structural soundness. Evaluate whether the prompt is well-organized, free of redundancy, and follows best practices for agent instructions." | Is this well-written, concise, and structurally sound? |
| Judge 3 (Philosophy) | "You are an alignment reviewer focused on consistency with the user's CLAUDE.md philosophy: verification-first, immutability, anti-genie rules. Evaluate whether the changes strengthen or weaken alignment with these principles." | Does this align with the verification-first philosophy? |

Each judge receives: the same pairwise context (parent vs child) + their unique personality prompt.
Each must READ BOTH files (parent + child snapshot of the focus file).
Each returns JSON: `{preferred, confidence, score_delta, key_reason, ...}`

This diversity reduces groupthink — a change that's behaviorally useful but structurally messy
will get split votes, properly reflecting the tradeoff.

**Aggregate via majority vote:**
```python
from evaluate import ensemble_aggregate, compute_pairwise_aggregate
ensemble = ensemble_aggregate([judge_a, judge_b, judge_c])
# ensemble.votes = {"child": 2, "parent": 1} → child wins
aggregate = compute_pairwise_aggregate(structural_score, ensemble, parent_aggregate)
```

Unanimous (3-0) = high confidence. Split (2-1) = medium, log dissent.

Write scores to `generations/$NEXT_GEN/scores.json` then:
```bash
PYTHONIOENCODING=utf-8 python ${CLAUDE_SKILL_DIR}/../../ua.py create-gen <parent_id> \
  ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/patch.diff \
  ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/scores.json \
  ${CLAUDE_SKILL_DIR}/../../generations/$NEXT_GEN/meta_reasoning.md
```

## Step 7: Binary Keep/Discard Decision

Get the score and compare to best:

```python
new_score = scores["aggregate"]
best_score = metadata["best_score"]

if new_score > best_score:
    # KEEP — commit to frontier
    ua.py keep $NEXT_GEN
else:
    # DISCARD — log and move on
    ua.py discard $NEXT_GEN "score {new_score} <= best {best_score}"
```

## Step 8: Report to User

For KEEP:
```
[KEEP] gen_0003 score=0.847 > best=0.812
  Changed: agents/executor.md
  Reason: Added concrete code examples for immutable patterns
  Frontier: committed
  Want to promote to live config? (y/n)
```

For DISCARD:
```
[DISCARD] gen_0003 score=0.798 <= best=0.812
  Changed: agents/executor.md
  Logged to results.tsv. Moving to next cycle.
```
