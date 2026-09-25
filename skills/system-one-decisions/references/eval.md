# Gold-set and calibration recipe

The one finding that motivates every step here: on the fleet's own board-card corpus,
every local engine scored *below* the trivial "always predict the majority class"
baseline (0.85) zero-shot (`references/engines.md` — base-rate blindness). A model that
sounds reasonable can still be a net negative versus doing nothing. Skipping this recipe
is how that ships unnoticed.

## 1. Build a gold set from your own domain

Don't reuse a generic benchmark (JevBench, the French set) as your gold set for a fleet
decision — those measure the engines in general, not your specific question against your
specific data. Build ~50+ labeled `(item, question, correct_answer)` triples from real
examples of the decision you're automating:
- For `choice`/`enum` questions: the correct option per item.
- For `noul`/bool questions: true/false per item.
- For `score`: the correct position on the scale (or an acceptable range).

Pilot 1's wiki-intent gold set was 46 pages — its own summary explicitly flags that as
small, with several categories having only 1-3 examples whose per-category metrics swing
wildly. Treat ~50 as a floor, not a target; more is better, and stratify so every answer
category has a handful of examples, not just the common ones.

Format as JSONL, one object per gold item:
```json
{"id": "email-0042", "state": {"body": "..."}, "question_id": "bucket", "gold": "actionnable"}
```

## 2. Run the draft in shadow mode

Use `scripts/jev.py ask` against the gold items (or, better, a larger unlabeled batch
that includes the gold items) — never act on the output yet, just log it:
```bash
scripts/jev.py ask --backend local-decider --questions questions.json \
  --items gold.jsonl --out results.jsonl
```

## 3. Calibrate the threshold

```bash
scripts/jev.py eval --results results.jsonl --gold gold.jsonl --out report.json
```
`eval` computes, per question:
- **Precision / recall / F1** at a scanned range of thresholds (0.05 increments,
  matching the approach used in pilot 1) — report the threshold that maximizes F1 on
  your gold set, but treat it as a starting point (in-sample selection on ~50 examples
  is not a validated cutoff — see pilot 1's own "weaknesses" section for why).
- **ECE (expected calibration error, 10-bin)** — how well the model's stated confidence
  matches its actual accuracy. Lower is better; pilot 1 saw 0.059-0.079 on its gold set,
  the main benchmark saw 0.033-0.387 across engines/datasets. A badly-calibrated model
  can still be useful if you threshold on raw accuracy, but ECE tells you whether "0.8
  confidence" really means "right 80% of the time" — important if you plan to use the
  probability itself (e.g. for a weighted composite, pattern 12) rather than just a
  threshold.
- **P@1** — for choice questions, whether the top-ranked option is correct.
- **The trivial baseline** — accuracy of "always predict the most common gold label" —
  printed alongside every result. **A model that doesn't beat this on your gold set is
  not ready to gate real work**, regardless of how good it looks on JevBench or any other
  general benchmark.

## 4. Compare against the current method, not just the baseline

Beating the trivial baseline is necessary, not sufficient. If the dept already has a
rule (Step 0 in SKILL.md) or a full-agent judgment call, run that same gold set through
the current method too and compare all three: trivial baseline < Jev draft ≤ current
method is a real risk — don't ship a Jev step that's merely "better than nothing" if the
thing it's replacing is already better than the Jev draft.

## 5. Set the cascade band, then keep watching it

Once you have a calibrated threshold, decide the escalation band (pattern 2 in
`patterns.md`) — the middle range where the model's answer isn't trusted alone and
falls back to the current (more expensive) method. Start conservative (wider band) and
narrow it only as you accumulate more gold examples confirming the model's calibration
holds on real traffic, not just the original gold set. Re-run `eval` periodically on a
fresh sample of real decisions (with human-checked labels) — a threshold tuned once can
drift as real traffic differs from the gold set it was calibrated on (pilot 1's own
"provisional negatives" caveat: a gold set built from what's *currently* labeled can
itself be systematically biased against catching genuinely new cases).

## Worked precedent: pilot 1's own gold evaluation

`pilot1-wiki-intent/results/20260925-pilot1/summary.md` ran exactly this recipe on a real
question (which of 12 operator-intents does each wiki page serve) — read it end to end
as a worked example, including its "Checker addendum," which found a simple metadata
rule beat every engine on the department-classification sub-question (P=0.88), directly
illustrating SKILL.md's Step 0.
