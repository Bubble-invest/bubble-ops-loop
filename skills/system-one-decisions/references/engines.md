# Engine reference — measured performance, failure modes, and residency

Full source: `~/claude-workspaces/Rick_RnD/prototypes/jev-local/` — `README.md` (install
and start/stop), `bench/results/20260925-seq/summary.md` (cross-engine benchmark),
`pilot1-wiki-intent/results/20260925-pilot1/summary.md` (real-corpus pilot),
`USE-CASES.md` §D/§E (where-not-to-use findings, official API details).

## The three local engines

| Engine | Model | Backend | Where it's strong | Where it's weak |
|---|---|---|---|---|
| `decider` (`local-decider`, **default**) | `Mapika/decider-2b` (Qwen3.5-2B-derived) | PyTorch/MPS, float16 | Best all-rounder: near-best accuracy on every tested set, fastest + most consistent tail latency of the two decoder engines (p50 0.31-3.3s, p95 0.6-4.8s), best-calibrated on JevBench (ECE 0.087) | "On the fence"/indecisive per one independent review; weaker than semif on French |
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
a French compliance test set (`bench/results/20260925-seq/{decider,semif}_fr_set.jsonl`,
the noul question "does this request fall under regulated investment advice," 3 true
positives): at the standard 0.5 threshold, **decider missed all 3** (p_yes = 0.15, 0.05,
0.08 — nowhere near the boundary) and **semif also missed all 3** (p_yes = 0.47, 0.25,
0.41). **laya — the weakest engine on every other measure — caught all 3** (p_yes = 0.76,
0.75, 0.86). The "best all-rounder" engine failed completely on the single highest-stakes
question type in the set, while the "weak, fast-but-shallow" engine got it right.
**Never pick one engine/threshold fleet-wide from aggregate accuracy alone — validate per
question-type, especially anything regulatory, before trusting a verdict for that type.**

**Base-rate blindness (the finding that opens this skill).** On the real internal fleet
board-card corpus (85% skewed toward one department), a trivial "always predict the
majority class" rule scores 0.85 accuracy — and every engine scored *below* that
zero-shot (decider 0.450, laya 0.400, semif 0.175 on the 40-card dept-classification
task). All three are content classifiers, not corpus-aware priors; they don't exploit
skew unless corrected with few-shot criteria text or a calibration pass. **This is why
`jev.py eval` always reports the trivial baseline next to the model's own score.**

**Universal open-replica limit.** None of the three handle multi-hop reasoning or date
arithmetic well. A narrow LoRA-tuned model (a fourth variant tested in the research,
`Nimble`) dropped from ~90% on its own holdout to ~44% on JevBench's "hard" tier — strong
only inside its training distribution.

## FR vs EN accuracy gap

| Engine | FR accuracy | EN accuracy (mean, JevBench+internal) | Gap (EN − FR) |
|---|---:|---:|---:|
| decider | 0.825 | 0.554 | −0.271 |
| laya | 0.525 | 0.461 | −0.064 |
| semif | 0.900 | 0.481 | −0.419 |

## Official API (`openrouter` / `typesafe` backends)

`typesafe/jev-1.13` (alias `jev-1.13.0`/`jev-latest`), endpoint `POST /v1/systemone`.
**$0.042/million input tokens, output tokens free**; latency 70-500ms; limits 250,000
tokens/sec and 1,200 requests/min (early access, subject to change); context 64k
tokens/request, 32k of which can be `state`. Reachable via the direct API, Vercel's AI
Gateway, or OpenRouter — any VPS dept can call it like any other hosted LLM API, no
special networking needed. Speed/cost multipliers claimed anywhere from 25x to 444x
depending on source and workload — **treat every multiplier as source-dependent, not a
fixed constant**, and benchmark your own use case rather than trusting a headline number.

**GDPR / EU residency — unconfirmed, not settled.** TypeSafe's own risk documentation
flags data residency and sub-processor terms as things that must be verified, not facts
already established. This research did not locate a citable statement that the
OpenRouter `typesafe/jev-1.13` route excludes EU regions. TypeSafe does state it doesn't
train on client inputs/outputs and offers zero-retention for Enterprise customers, but
the model architecture and training-data composition remain undisclosed. **Until
residency is confirmed in writing: no Jade/client personal data, KYC/AML, or patrimoine
specifics goes to the official API** — use a local engine, or a minimized/pseudonymized
state, for anything sensitive. Note also: TypeSafe's own ToS forbids running comparative
benchmarks against the official Jev API — something we could not do with the official
API but could do freely with the local engines (and did, for this research).

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
