# Pattern catalog (condensed from `jev-local/USE-CASES.md` §A)

Full source with every citation and caveat verbatim:
`~/claude-workspaces/Rick_RnD/prototypes/jev-local/USE-CASES.md`. Every pattern is
engine-agnostic — the same three typed primitives (Choice / Score / Noul, see below)
regardless of whether the backend is the official Jev API or a local engine
(`references/engines.md`). The caller — never the model — turns the probability into
an action; Jev/System-One only ever emits a typed answer to a typed question.

## The three primitives

A request carries a `state` (the text/JSON the question is about) and one or more
typed `questions`:
- **Choice** — pick among named options; returns a probability per option +
  confidence.
- **Score** — a position on an ordinal scale; returns a distribution + confidence.
- **Noul** — a calibrated P(true) for one yes/no statement.

"Type-safe" means the output can't fall outside the declared schema — it says
nothing about whether the *answer* is correct. Treat every verdict as a probability
to threshold, not a fact.

## 1. Parallel / speculative fan-out
Ask several independent questions against the *same* state in one call; each is
scored independently so one answer never becomes hidden context for another.
Measured **12.2x cheaper, 10.0x faster** than N separate calls on a ~54k-char
document (TypeSafe's own `parallel_questions` cookbook), because the state's tokens
dominate cost and are paid once, not N times. **Caveat:** the benefit shrinks on
small documents (fixed per-request overhead dominates) — only ask questions you'll
actually act on, unused answers still cost input tokens.

## 2. Confidence-threshold gating / System1→System2→human cascade
Deterministic code applies hard rules first → Jev answers parallel micro-questions →
high-confidence/low-risk goes down an automatic path, medium confidence escalates to
the full agent, low confidence or high risk escalates to a human. The illustrative
0.30-0.70 uncertain band (TypeSafe's own cookbook) is a starting point, not a
guarantee — **tune thresholds on your own labeled data** (see `eval.md`). An
independent community test of a Jev→DeepSeek cascade improved one classification
task and gave zero gain on another while raising cost — **verify the fallback
actually fixes the gate's errors before trusting the cascade.**

## 3. Model router
Jev classifies intent/domain/difficulty/risk; code picks: rule engine, small model,
frontier model, or human. One measured demo: 70% of a 12-task set never needed the
top-tier model. **Caveat:** router errors are silent — a misrouted "easy" task that
actually needed the expensive model degrades quietly unless verification (pattern 8)
also runs downstream.

## 4. Skill / tool router
A first fast pass narrows a large skill/tool catalog to a few candidates (or "none
apply"); only the narrowed set enters the agent's context. One measured demo: found
the right skill among ~145 in ~5s total vs. a full model's ~30s *per test*.
**Caveat:** narrows recall, not just noise — a genuinely relevant but oddly-worded
skill can get filtered out; add an "escalate if nothing scores confidently" fallback.

## 5. Action gate / guardrail before a tool call
Before a destructive or scoped action (shell, email send, delete, financial
transaction), Jev scores destructiveness / permission-scope / data-exposure /
intent-alignment in parallel; code allows, blocks, or asks for confirmation.
**Explicit caveat in the source material:** "semantic control must never replace
permissions, allowlists, and deterministic validations" — this is a second layer on
top of hard permissions, never a replacement for them.

## 6. RAG context filter / reranker
Jev scores each retrieved passage on relevance, contradiction with other evidence,
and presence of a hidden instruction (prompt-injection defense); code drops or
quarantines low scorers before the passage reaches the reasoning model. **Caveat:**
an independent test found Jev's reranking competitive with *a* specialized reranker
but not superior, and worse than a mature in-house classifier on a narrow,
already-owned task — Jev doesn't automatically beat a good existing classifier.

## 7. Output / citation verifier
After generation, separate Noul questions check: does the cited passage support the
claim? does the output follow policy? is anything requested missing? was sensitive
data exposed? Only doubtful outputs loop back to the agent or a human. **Caveat:**
this is a probabilistic check, not proof — treat a "pass" as evidence, not a
guarantee, on anything gold-critical.

## 8. Trace evaluator / LLM-as-judge replacement
A full agent trace (task + tool calls + final answer) is submitted as `state`; Jev
estimates need-for-review, severity, and failure mode in one call. Reported as
"cheaper, faster, and more consistent than LLM-as-judge" for continuous online
triage. **Caveat:** good for massive, cheap, continuous triage of runs to flag for
review — not a substitute for a real LLM's explanation of *why* a complex run
failed.

## 9. Extraction cascade
A cheap extractor (regex or a small model) proposes candidate values; Jev
selects/verifies among them (never free-generates, to avoid invented values);
uncertain cases escalate to the full model. **Caveat:** only safe when Jev is
choosing *among presented candidates*, not inventing a value.

## 10. Bounded function-calling
Function name and each closed-enum argument become separate Choice/Noul questions;
code keeps control of dispatch. **Caveat:** only covers "which function, which enum
arg" — open-ended arguments (free text to write) still go to the full model.

## 11. Critique-then-revise loop
Jev doesn't explain its verdict, but flags *which dimension* failed (e.g., missing
citation, unclear scope); code turns that into a targeted instruction for the full
agent — a tighter loop than free-form self-critique.

## 12. Weighted composite score
Combine several Noul/Score answers with code-side weights into one downstream
signal, e.g. `quality = 0.4*a + 0.4*b + 0.2*(1-c)`, or feed the probabilities as
features into a classical ML model when labels exist. **Caveat:** weights are a
modeling choice made in code — they need the same tuning-on-real-data discipline as
any threshold.

## 13. Self-consistency / stability check
Before trusting a threshold in production, re-run the same rubric N times (the
cookbook uses n=15) with a fresh unique ID per draw (to defeat caching) and measure
per-question standard deviation. TypeSafe's cookbook reports mean per-question
stdev 0.0102 — tighter than every tested LLM baseline. **Caveat, stated directly in
the source:** high consistency does *not* prove correctness — it only proves the
model isn't flip-flopping; genuinely disputed ground truth can still exist on
ambiguous cases even when the model is perfectly stable.

## 14. Context-compaction gate (agent-harness pattern)
Instead of an LLM summarizing old tool calls, ask two Noul questions per old call:
"keep this call at all?" and "keep the result verbatim or trim it?" — kept content is
never rewritten. **Caveat, from the builder's own test:** in 5 real coding sessions
at a conservative confidence setting, 0% of context was actually dropped — flagged as
"good idea, not proven yet."

## 15. Component-selection UI generation ("safety by construction")
Jev picks which pre-built components a screen needs from a closed catalog — it
structurally cannot hallucinate a component that doesn't exist. Generalizes beyond
UI: any place you have a closed catalog (skills, templates, canned replies) is a
candidate for "select, don't generate."

## 16. Browser/computer-use action selection + small-model handoff
Jev picks the DOM element + action from a numbered list of clickable elements (not
pixels); a small separate model only fills in actual typed text. One source
confirmed explicitly: "Jev alone was unable to actually type things in" — the
handoff to a text-capable model is required, not optional.

## 17. Classify-then-generate chaining ("qualify cheap, generate expensive")
Jev does high-volume qualification/scoring on the full set; the expensive model only
runs deep research or drafts on the subset Jev flagged. **Caveat, stated explicitly
by one source:** "Jev on its own doesn't analyze things for you... you get analysis
from the data if you're strategic with the questions" — it extracts structured
signal, it does not summarize or draw conclusions.

## 18. Pre-filter cascade to open local classifiers (JevBench ecosystem)
Instead of the paid API, several open replicas expose the same wire-format pattern
locally — this is exactly `decider`/`semif`/`laya`, see `engines.md`. **Universal
caveat across every open replica tested:** none handle multi-hop reasoning or date
arithmetic well; a narrow LoRA-tuned model can drop sharply outside its training
distribution (one tested ~90% on its own holdout → ~44% on JevBench's "hard" tier).

---

Patterns most useful as a starting point (see SKILL.md's pattern picker table):
**pre-filter/router (4, 18)**, **confidence-gated cascade (3)**, **parallel fan-out
(1)**, **weighted composite (13)**, and **judge/verification (7, 8)** for
double-checking output before it's trusted. **Action gate (6)** is the one to reach
for before any risky tool call — always as a second layer over deterministic
permissions, never instead of them.
