# Engine reference — measured performance, failure modes, and residency

Full source: `~/claude-workspaces/Rick_RnD/prototypes/jev-local/` — `README.md` (install
and start/stop), `bench/results/20260925-seq/summary.md` (three-local-engine cross-engine
benchmark), `bench/results/20260925-openrouter/summary.md` (the official Jev re-run
against the same three benchmark sets, **verified live**, 230/230 calls OK, 2026-09-25),
`pilot1-wiki-intent/results/20260925-pilot1/summary.md` (real-corpus pilot),
`USE-CASES.md` §D/§E (where-not-to-use findings).

## The official Jev (`openrouter` backend, `typesafe/jev-1.13`) — VERIFIED LIVE

Confirmed working end-to-end 2026-09-25 (from the VPS, `ssh hetzner-root`, using an
OpenRouter key): **`POST https://openrouter.ai/api/alpha/decisions`**, header
`Authorization: Bearer $JEV_OPENROUTER_API_KEY` (fallback `$OPENROUTER_API_KEY`), body
`{"model": "typesafe/jev-1.13", "state": <text>, "questions": {<same noul/choice/score
question objects as the local /v1/systemone format>}}`. Response:
`{model, answers: {<qid>: {type, noul | choice+probabilities+confidence | score}},
usage: {input_tokens, output_tokens, cost}, id, provider}` — same `answers` shape as
the local engines, plus a real per-call `usage.cost` in USD (`scripts/jev.py` extracts
and tracks this; see `--max-spend`). This is a **different endpoint and body shape**
than the local engines'/`typesafe` direct API's `/v1/systemone` — `scripts/jev.py`
handles the difference, callers don't. **Error rate on the verified run: 0/230 (0%)** —
no timeouts, no 4xx/5xx, no retries needed.

**Accuracy — the clear leader almost everywhere tested**, against the three local
engines on the identical 230-item, 3-dataset benchmark:

| Engine | JevBench accuracy | French (`fr_set`) accuracy | Board-card `internal_set` accuracy |
|---|---:|---:|---:|
| **jev113 (official)** | **0.793** | **0.950** | 0.421 |
| decider | 0.673 | 0.825 | 0.434 |
| semif | 0.687 | 0.900 | 0.276 |
| laya | 0.527 | 0.525 | 0.395 |

By JevBench difficulty tier, jev113's lead widens on the harder items (easy 1.000,
original 0.809, hard 0.699 — beating decider's 0.702/0.521 and semif's 0.660/0.589 by
10-18 points on original/hard) while staying at flat, tight latency the whole time
(p50/p95 ~0.48-0.59s on every tier) — decider's p95 balloons from 0.26s to 5.93s and
semif's to **18.3s** on the hard tier; jev113 has no such tail. Cost: **$0.025 per
1,000 decisions** (`$0.0077` for the entire 230-item/310-decision verified run — 1.5%
of the $0.50 cap used for that run), negligible next to accuracy/latency as a factor
for anything short of very high volume.

**Board-card `internal_set` remains hard for everyone, jev113 included** (0.421,
barely different from decider's 0.434, both far below the JevBench/French numbers) —
this is the same corpus base-rate skew problem described under "Base-rate blindness"
below; it is a property of that specific task (85% of gold cards are one department),
not an artifact of one backend, local or official.

**Regulated-advice detection: the one place jev113's edge really matters.** See
"Failure modes" below — jev113 caught 3/3 true positives with 0 false positives on the
French compliance question, where decider and semif each missed all 3. This makes
jev113 markedly more useful as a **flag-for-human-review** signal than any local
engine — it does not make it, or any backend, trusted to *decide* a regulated-advice
question (see SKILL.md's use/don't-use checklist — this stays a NEVER).

**Data-residency rule (Joris, Telegram msg 9715, 2026-09-25):** internal Bubble Invest
fleet data (board cards, wiki, internal mail/ops, dept missions) may use this backend
now — "for now it's internal use so it's ok." **External client data** (client
deliverables e.g. Gefineo/Delahaye, PEP-France/OpenSanctions screening subjects, or
anything processed on behalf of a client) **stays local-only** until EU residency and a
DPA are confirmed in writing — see the GDPR note further down, which is otherwise
unchanged by this clearance.

## The three local engines

| Engine | Model | Backend | Where it's strong | Where it's weak |
|---|---|---|---|---|
| `decider` (`local-decider`, best local all-rounder) | `Mapika/decider-2b` (Qwen3.5-2B-derived) | PyTorch/MPS, float16 | Best all-rounder OF THE LOCAL THREE: near-best accuracy on every tested set, fastest + most consistent tail latency of the two decoder engines (p50 0.31-3.3s, p95 0.6-4.8s), best-calibrated on JevBench (ECE 0.087). Still meaningfully behind the official Jev on accuracy (table above) and the free, zero-data-leaves-the-machine pick for EXTERNAL client data. | "On the fence"/indecisive per one independent review; weaker than semif on French; no cost but no accuracy edge either |
| `semif` (`local-semif`) | `Qwen/Qwen3.5-4B`, MLX 4-bit in-memory quantized, direct-logit readout | MLX (Apple Silicon only) | Most accurate on French (0.900 accuracy, 1.000 macro-F1, best calibration of the three: Brier 0.003/0.095, ECE 0.040) | Heavy latency tail on long context — p95 up to 14.7s on JevBench's "hard" tier (~3,700 input tokens); ~24.8s/page across 12 questions in the wiki pilot |
| `laya` (`local-laya`) | ModernBERT/mmBERT encoder, non-autoregressive | PyTorch | 5-50x faster than the other two on every set (p50 0.03-0.47s) — the pick for a cheap high-volume pre-filter | Weakest accuracy overall (0.527 JevBench, 0.525 French) — never the final call |

All three: zero per-token cost after the one-time ~14 GB model download, zero data
leaves the machine (all bind `127.0.0.1` only — verified via `lsof`), passed supply-chain
vetting (no `trust_remote_code=True`, no live `eval`/`exec` in the installed code path —
see `jev-local/VETTING.md`). **Mac-only today**: decider is MPS/PyTorch, semif is MLX —
neither is proven to port cleanly to the Linux VPS fleet; laya (standard
PyTorch/transformers) is the most likely of the three to run CPU-only on a VPS, but this
was not tested. Only one engine binds its port at a time on a 16 GB Mac — `jev.py start`
enforces this.

## Failure modes (read before trusting any verdict)

**Governance/process over-trigger.** In the wiki-intent pilot, two process-flavored
intents (`operator-control-charter`, `verify-fleet-standard-first`) were the most
over-predicted labels on the unlinked corpus (38 and 24 confident proposals respectively)
despite one of them having only 3 gold positives and 0.222 precision on gold — generic
"process/governance" language pattern-matches a lot of ordinary ops content that isn't
really about that specific charter.

**Cross-department content bleed.** A sales-domain wiki page was scored 0.823 for a
fund/portfolio-governance intent — shared vocabulary ("track", "decision", risk-adjacent
framing) pulled the verdict toward the wrong department. Watch for this whenever
different depts' text shares generic operational vocabulary.

**Regulated-advice miss (the most important failure mode in this whole reference).** On
a French compliance test set (the `is_regulated_advice` noul question, "does this
request fall under regulated investment advice," 8 items / 3 true positives —
`bench/results/20260925-seq/{decider,semif}_fr_set.jsonl` and, for the verified
official run, `bench/results/20260925-openrouter/jev113_fr_set.jsonl`): at the standard
0.5 threshold, **decider missed all 3** (p_yes = 0.15, 0.05, 0.08 — nowhere near the
boundary) and **semif also missed all 3** (p_yes = 0.47, 0.25, 0.41), each still
correct on all 5 negatives (5/8 overall). **laya — the weakest local engine on every
other measure — caught all 3** (p_yes = 0.76, 0.75, 0.86). **The verified official Jev
(`jev113`) also caught all 3, with a wider and cleaner margin** (p_yes = 0.92, 0.77,
0.93 on the true positives vs. 0.01-0.09 on the 5 negatives — 8/8 correct, 0 false
positives). The "best all-rounder" local engine (decider) failed completely on the
single highest-stakes question type in the set, while the weakest local engine (laya)
and the official Jev both got it right. **Never pick one engine/threshold fleet-wide
from aggregate accuracy alone — validate per question-type, especially anything
regulatory, before trusting a verdict for that type — and treat even a strong
detector's "yes" as a flag for a human, never as the decision itself** (see SKILL.md's
use/don't-use checklist).

**Base-rate blindness (the finding that opens this skill).** On the real internal fleet
board-card corpus (85% skewed toward one department), a trivial "always predict the
majority class" rule scores 0.85 accuracy — and every LOCAL engine scored *below* that
zero-shot (decider 0.450, laya 0.400, semif 0.175 on the 40-card dept-classification
task). The verified official Jev is not immune either: **jev113 scored 0.421** on the
same `internal_set` task (its `dept`/`risk`/`is_bugfix` triple, 76 gold-scored pairs) —
barely different from decider's 0.434 and still well below the 0.85 baseline. All four
engines are content classifiers, not corpus-aware priors; none exploit skew unless
corrected with few-shot criteria text or a calibration pass. **This is why `jev.py
eval` always reports the trivial baseline next to the model's own score, regardless of
backend.**

**Universal open-replica limit.** None of the three handle multi-hop reasoning or date
arithmetic well. A narrow LoRA-tuned model (a fourth variant tested in the research,
`Nimble`) dropped from ~90% on its own holdout to ~44% on JevBench's "hard" tier — strong
only inside its training distribution.

## FR vs EN accuracy gap

| Engine | FR accuracy | EN accuracy (mean, JevBench+internal) | Gap (EN − FR) |
|---|---:|---:|---:|
| jev113 (official) | 0.950 | 0.607 | −0.343 |
| decider | 0.825 | 0.554 | −0.271 |
| laya | 0.525 | 0.461 | −0.064 |
| semif | 0.900 | 0.481 | −0.419 |

jev113's French accuracy (0.950) is the highest of any engine on any dataset in this
whole benchmark, and it has the best calibration on `fr_set` of the four (Brier
0.068/0.018, ECE 0.042) — second only to semif's near-perfect but far slower `fr_set`
score. Its EN−FR gap is still the widest of the four in absolute terms, purely because
its FR number is so high, not because its EN number is weak.

## Official API — `openrouter` (verified) vs `typesafe` (unverified, direct)

**`openrouter`** (see the dedicated section above for the full verified results):
`typesafe/jev-1.13`, endpoint `POST https://openrouter.ai/api/alpha/decisions`, model
field + state + questions in the body, **$0.025/1,000 decisions** measured, 70-500ms
typical latency (flat, no tail — p50 ~0.5s / p95 ~0.6s measured on 230 real calls).
Speed/cost multipliers claimed elsewhere anywhere from 25x to 444x vs. comparable LLMs
depending on source and workload — **treat every such multiplier as source-dependent,
not a fixed constant**; the measured numbers in this file are what to actually plan
around.

**`typesafe`** (direct API, `POST <base_url>/v1/systemone`, no OpenRouter in the path):
same underlying model per TypeSafe's docs (`jev-1.13.0`/`jev-latest`), **$0.042/million
input tokens, output tokens free** per TypeSafe's own pricing page, limits 250,000
tokens/sec and 1,200 requests/min (early access, subject to change), context 64k
tokens/request (32k of which can be `state`). **Not independently verified against a
live account in this build** (no key/network available) — `scripts/jev.py` implements
it on the same assumed `/v1/systemone` shape as the local engines, but confirm against
a real key before relying on it in production. Prefer `openrouter` (verified) unless
you have a specific reason to go direct.

**GDPR / EU residency — unconfirmed, not settled** (unchanged by the internal-use
clearance below). TypeSafe's own risk documentation flags data residency and
sub-processor terms as things that must be verified, not facts already established.
This research did not locate a citable statement that the OpenRouter `typesafe/jev-1.13`
route excludes EU regions. TypeSafe does state it doesn't train on client
inputs/outputs and offers zero-retention for Enterprise customers (the verified
`openrouter` run's own API response confirmed `data_policy: {training: false,
retainsPrompts: false}`), but the model architecture and training-data composition
remain undisclosed, and residency/sub-processor terms specifically are still
unconfirmed. Note also: TypeSafe's own ToS forbids running comparative benchmarks
against the official Jev API — something we could not do with the official API but
could do freely with the local engines (and did, for the three-local-engine
benchmark).

**Data-residency rule in force (Joris, Telegram msg 9715, 2026-09-25 — "for now it's
internal use so it's ok"):**
- **INTERNAL Bubble Invest data** (board cards, wiki, internal mail/ops, dept
  missions) **may use `openrouter` now.**
- **EXTERNAL client data** (client deliverables e.g. Gefineo/Delahaye,
  PEP-France/OpenSanctions screening subjects, anything processed on behalf of a
  client) **stays local-only** (`local-decider`/`local-semif`/`local-laya`) until EU
  residency and a DPA are confirmed in writing. This is the line the rest of this
  paragraph's caveats still protect — the internal-use clearance does not touch it.

## Local engine start/stop (for reference — `jev.py start/stop` wraps this)

```bash
export HF_HOME=<jev-local-dir>/hf-cache
LAYA_HOST=127.0.0.1 LAYA_PORT=8781 laya/.venv/bin/laya-serve &
DECIDER_MODEL=Mapika/decider-2b decider/.venv/bin/uvicorn decider.serve:app \
  --app-dir decider-src --host 127.0.0.1 --port 8782 &
SEMIF_MLX_BITS=4 semif/.venv/bin/python bench/semif_adapter_server.py &   # binds 8783
```
Verify localhost-only binding: `lsof -iTCP -sTCP:LISTEN -P | grep -E "8781|8782|8783"`
must show `localhost:87..`, never `*:87..` or `0.0.0.0:87..`.
