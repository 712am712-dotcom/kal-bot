# Prompt Engineering Guide — Kal

Reference for improving Haiku prompt quality across the intelligence pipeline.

---

## Core Techniques

### 1. Role Prompting

Give the model a specific expert identity before any instruction. The role frames
every inference the model makes — it affects vocabulary, priorities, and the
level of specificity in the output.

**Pattern:**
```
You are a [specific expert] who has [concrete achievement].
Your job is to [specific task] for [specific audience].
```

**Applied in Kal:** `_haiku_attention_signal` in `attention_engine.py`
```
You are an expert AI content strategist who has grown AI education pages
to 1M+ followers. Your job is to identify which AI news stories will
perform best as Instagram carousel content for a beginner audience...
```

**Why it works:** "Content strategist" activates distribution-aware reasoning.
"1M+ followers" anchors the model to proven, not theoretical, strategies.
"Beginner audience" prevents jargon-heavy output.

---

### 2. Chain-of-Thought (CoT)

Force step-by-step reasoning before the final answer. Especially effective for
scoring and classification tasks where the model benefits from working through
evidence before committing to a number.

**Pattern:**
```
Think step by step:
1. What is this story actually about?
2. Why would a beginner care?
3. What makes it timely?
Then give your final score and classification.
```

**When to apply in Kal:**
- Signal scoring in `_score_topic` — currently pure heuristic, CoT can catch
  nuance the keyword list misses
- Format classification (A/B/C) in `_haiku_attention_signal` — reasoning
  before committing reduces misclassification
- Pattern report in `_haiku_pattern_report` — identifying the dominant
  pattern benefits from evidence enumeration before conclusion

**Cost note:** CoT increases output tokens by ~2–3×. Use selectively on
high-stakes calls (pattern report, final signal selection), not every article.

---

### 3. Self-Consistency

Generate N independent outputs for the same input, then select by majority
vote or highest score. Reduces variance in creative tasks like hook writing.

**Pattern:**
```
Generate 3 different hooks for this story. Score each 1–10 on:
- Clarity for a beginner
- Curiosity / scroll-stopping power
- Specificity (no vague claims)

Return only the highest-scoring hook and its score.
```

**When to apply in Kal:**
- `hook` field in `_haiku_attention_signal` — single-pass hook generation
  is the highest-variance output in the signal pipeline
- Diminishing returns beyond 3 options; 3 is the sweet spot for cost/quality

**Cost note:** Generates 3× the hook tokens but max_tokens stays the same
since the model self-selects. Net increase ~30–50 tokens per call.

---

### 4. Few-Shot Examples

Include 2–3 labelled examples of good output directly in the prompt. The model
pattern-matches to the examples rather than inferring format from description.

**Pattern:**
```
Examples of high-performing signals:

EXAMPLE 1:
Topic: OpenAI launches o3 reasoning model
Format: B (tool/product)
Hook: The AI that just beat PhD-level math tests
Why now: new_release
Angle: economic_impact
Score: 9

EXAMPLE 2:
Topic: EU AI Act enforcement begins
Format: C (news/event)
Hook: Europe just made AI compliance mandatory for businesses
Why now: trending
Angle: conflict
Score: 7

Now classify the following topic:
```

**When to apply in Kal:**
- Format classification (A/B/C) — examples eliminate the most common
  misclassification (C labelled as A)
- Hook generation — examples anchor tone and length better than word-count
  instructions alone
- Keep examples < 6 months old so they reflect current AI landscape

---

## Application Priority

| Technique | Apply to | Expected gain | Cost increase |
|---|---|---|---|
| Role prompting | All Haiku calls | High — immediate quality lift | Zero |
| Few-shot (format) | `_haiku_attention_signal` | Medium — reduces A/B/C errors | ~80 tokens |
| Self-consistency (hook) | `_haiku_attention_signal` | Medium — better hooks | ~50 tokens |
| Chain-of-thought | `_haiku_pattern_report` | Medium — better pattern ID | ~100 tokens |

Role prompting has zero cost increase and highest immediate impact — apply first.

---

## Prompt Review Checklist

Before shipping any new Haiku prompt:

- [ ] Does the system/opening line give a specific expert role?
- [ ] Is the target audience named explicitly?
- [ ] Are enum fields (format, why_now, angle) listed with definitions in the prompt?
- [ ] Is there at least one concrete example of good output?
- [ ] Is max_tokens set tight enough to prevent rambling?
- [ ] Does the JSON schema match exactly what the code expects?
