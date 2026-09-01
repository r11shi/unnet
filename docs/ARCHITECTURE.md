# Architecture

## The shape of the problem

Reconciliation looks like a matching problem and is really a **conservation**
problem. The question is not "which order goes with which line" — that is the
easy half. The question is "does every rupee that left the customer's account
arrive in the merchant's, and if not, exactly where did it go".

That framing decides most of what follows. Conservation is arithmetic over
integers, so the core is deterministic and the money is integers. Conservation
can be *proved*, so every payout is derived twice and the two derivations must
agree. And conservation fails in specific, nameable ways, so the output is not a
score but a taxonomy.

## Pipeline

```
merchant_ledger.csv ─┐
settlement_recon.csv ─┼─▶ 1 INGEST ─▶ 2 MATCH ─▶ 3 NETTING ─▶ 4 RESOLVE ─▶ 5 REPORT
bank_statement.csv  ─┘
                        schema map   tiers 1-3   two-way      subset-sum   dashboard
                        + narration  (rules)     proof        then model   audit, Ask
                        (model, gated)           (rules)      (verified)
```

Every stage writes to the same append-only audit log, and every stage may only
record a conclusion through `ReconContext.record_match` or
`record_exception` — which write to the trail as a side effect. Nothing can
reach the output without also reaching the trail, because there is no other way
to get there.

---

## 1. Ingest

**Canonical schema** (`unnet/core/models.py`) mirrors Razorpay's own
settlement-recon field names: `entity_id`, `type`, `debit`, `credit`, `fee`,
`tax`, `settlement_id`, `settlement_utr`, `on_hold`, `credit_type`, `dispute_id`
and the rest. Staying on their vocabulary means the synthetic fixtures and the
live adapter (`unnet/ingest/razorpay_live.py`) land in the same shape, and
anyone who knows Razorpay's reports can read this schema with no translation.

**Money is `int` paise everywhere.** There is no float on the matching path.
`unnet/core/money.py` parses the shapes real exports contain — `"1,234.50"`,
`"(1234.50)"`, `"Rs. 99"`, `"500.00 Cr"` — and rounds half-up, because Python's
`round()` uses banker's rounding and a payment gateway does not.

**Column mapping** is a two-step contract (`unnet/ingest/mapping.py`):

1. `heuristic_map` resolves headers through an alias table, after normalising
   away punctuation and case.
2. If required fields are still missing, `ModelSchemaMapper` proposes a mapping.
3. Either way, `validate_spec` dry-runs the mapping against real rows before
   anything uses it — a named column must exist, a date column must parse as a
   date, a money column must parse as money.

A model-proposed mapping that fails validation is discarded for the heuristic.
The worst case of a bad model answer is a wasted call.

**UTR extraction** (`unnet/ingest/narration.py`) is regex-first. The UTR is the
only hard link between the two systems and it lives inside free text:

```
NEFT-KKBKH14156891582-RAZORPAY SOFTWARE PVT LTD-HDFC0000123-PAYOUT
```

The regex explicitly rejects IFSC codes (`[A-Z]{4}0[A-Z0-9]{6}`), which appear in
nearly every NEFT narration and are the single most common false UTR.

---

## 2. Match — three tiers, all deterministic

**Tier 1: bank credit ↔ payout** (`engine/tier1.py`)

| Rule | Basis | Confidence |
| --- | --- | ---: |
| `T1.UTR_EXACT` | UTR from the narration matches the settlement's UTR | 1000 |
| `T1.AMOUNT_DATE_EXACT` | unique payout, identical amount, inside a 3-day window | 900 |
| `T1.AMOUNT_NEAR` | unique payout within ₹50, difference raised as a residual | 700 |

**Tier 2: settlement line ↔ order** (`engine/tier2.py`) — by `payment_id`, then
`order_id`, then amount + method + capture time. The fuzzy rule buckets orders
by exact paise so it stays linear; comparing 1,500 orders against 1,600 lines
pairwise is a minute of wall clock for nothing.

**Tier 3: reversal ↔ original payment** (`engine/tier3.py`) — this is where
naive reconciliation quietly goes wrong. A refund or chargeback does not adjust
the original payment; it is deducted from a *later* payout. Money leaves in one
cycle for a sale that closed in another, so anyone matching cycle-by-cycle sees
two unexplained holes instead of one linked pair.

### The rule that matters most

**Every fuzzy rule requires a unique candidate and refuses when more than one
fits.** Not "pick the closest" — refuse.

This is why the false-match rate stays at 0.19% when a third of the identifiers
are removed, while recall falls by a third. In reconciliation a missed link costs
an analyst five minutes in a queue; a wrong link silently attributes money to the
wrong order, survives into the books, and is found months later by an auditor.
Those two errors are not worth trading against each other at parity, and any
system reporting a single "accuracy" number has hidden the distinction.

---

## 3. Netting — the proof

Razorpay documents a payout as
`Payment − Adjustment − Tax − Fee − Transfer + Refunds`. We compute it two ways
and require agreement:

1. From `credit − debit`, which is unambiguous about direction.
2. From `amount` and `fee`, dispatched by entity type.

Two derivations that agree is a proof. One derivation is an assertion. When they
disagree, the settlement report is internally inconsistent and that is worth
saying out loud rather than picking whichever number looks nicer.

MDR and GST are then **recomputed from a rate card** rather than trusted:

```python
expected_mdr = apply_bps(line.amount_paise, rate_bps[line.method])
expected_gst = gst_on(charged_mdr)          # 18% of the fee, not the transaction
```

so an incorrectly billed fee surfaces as `FEE_MISMATCH` instead of disappearing
into the total. GST is only checked once the fee it is levied on is known good —
otherwise one error reports as two. This matters commercially: GST on MDR is
eligible for input tax credit *only* if it is a separate, correct line item.

---

## 4. Resolve — search first, model second

Order is the whole argument here.

**`subset_sum_resolve`** (`agents/resolvers.py`) runs first. If an unmatched bank
credit is the exact sum of some combination of unmatched payouts, bounded
exhaustive search finds it — smallest combination first, so a credit that is
genuinely one payout is never explained as three that happen to add up. This
closes the consolidated-credit case (a bank posting two NEFTs as one line)
without any model at all.

The search is **exact, never nearest-fit**. A credit that is *nearly* the sum of
three payouts is not evidence that it is those three payouts.

**`TriageAgent`** (`agents/triage.py`) is consulted only on what survives. What
search cannot do is invent a quantity that appears in no table: when a payout of
₹8,36,364.23 arrives as ₹8,36,352.43, no combination of known records explains
the missing ₹11.80, because the bank's own NEFT charge was never in the data. A
model that has seen Indian bank statements proposes "₹10 NEFT charge plus 18%
GST" — a hypothesis, from domain knowledge, about a number that is not in the
input.

It stays a hypothesis until it sums exactly.

### The verifier — three verdicts, not two

`agents/verifier.py` is the load-bearing object in this project, and v1 got it
wrong in the way most "AI + verifier" designs do: it treated **arithmetic
consistency as financial truth**. Components that summed to the residual were
accepted, which meant a model could invent a sub-₹500 "bank charge" and have the
invention recorded as verified fact.

A ₹11.80 shortfall is equally satisfied by a NEFT charge plus GST, by a single
adjustment, or by two unrelated fees. Arithmetic cannot distinguish them.

| Verdict | Condition | Consequence |
| --- | --- | --- |
| `RESOLVED_VERIFIED` | every component **read back from a ledger row** at verify time; sums exactly; no rival combination | auto-close |
| `HYPOTHESIS` | sums exactly, but contains an invented component **or** another combination also fits | never auto-closed; routed to a human with the hypothesis |
| `REJECTED` | citation or arithmetic fails | stays open, reason recorded |

Rejection also covers: a reference that does not exist, one cited twice, one
already reconciled elsewhere, a **restated amount** (the subtle attack — a
doctored figure can make the total come out right, so arithmetic alone passes
it), and an unmodelled component above ₹500, which is balancing the books rather
than explaining them.

**Provenance** is what separates the first two verdicts. A reference string is a
name; evidence is a name plus the row it was read back from at verification
time. Without the read-back, "verified" means "the model quoted an id from a
list we sent it".

### Untrusted input

`agents/untrusted.py`. Bank narration is chosen by a payer — a UPI remark
travels through the statement into a prompt. It is fenced, labelled as
payer-written, and stripped of fence-breakers and invisible characters.

Prompt hygiene narrows the attack; it does not end it. The structural defence
is that the model cannot write to the ledger, cannot cite a record that does not
exist, and cannot close anything without provenance. The fixtures carry a live
injection attempt and a test asserting the verdict is unchanged.

### The agent loop

Propose → verify → on a **rejection**, receive the verifier's exact signed delta
and revise → stop. Bounded to two attempts. A `HYPOTHESIS` is terminal (it
already sums; a retry reaches the same place) and so is an abstention.

Every step is appended to an `agent_trace` on the exception, so
`exception → action → components → verdict → delta → terminal state` is
reconstructable from the database alone.

Measured on the fixtures: **one call per exception, zero retries.** The retry
path is real and tested by forcing it with a stub model, but deterministic
candidate generation runs first, so by the time a model is consulted there is
usually one sensible answer. That is reported rather than dressed up.

## 5. Report

**Audit trail** — `recon_audit`, append-only, sequence-numbered per run so
decisions replay in the order they were taken (a timestamp alone does not
guarantee that at sub-millisecond resolution). Model decisions record
`source:model:prompt_hash`, so the exact input behind a decision is recoverable;
a model name alone is not reproducible.

**Dashboard** — one static HTML file, no build step. Six views, hand-written SVG
charts. A reviewer needs Python and nothing else.

**Ask** — common finance questions answered by parameterised queries with no
model. Anything else becomes a single read-only `SELECT`, validated against a
table allowlist and shown next to the answer.

---

## The model layer

```
LLMClient
  ├─ cassette replay      committed recordings; offline, reproducible, default
  ├─ LocalBackend         llama.cpp / Ollama / LM Studio — no API key
  ├─ GeminiBackend        structured output via responseSchema
  └─ GroqBackend          OpenAI-compatible fallback
```

Three failure modes, all handled:

1. **No key at all** — cassette replay serves recorded responses, so `make demo`
   reproduces the published numbers with no account and no network.
2. **Free-tier rate limits** — a circuit breaker trips after three consecutive
   failures and the run degrades to rules. A reconciliation that dies two thirds
   of the way through because a quota ran out is worse than one that finishes on
   rules alone and says so.
3. **A model returning nonsense** — not this layer's problem. The verifier
   handles it. This layer's only job is to return parsed JSON or admit it could
   not.

Switching from a local model to a hosted one is a change of
`UNNET_LLM_PROVIDER`, not a change of code. The agents talk to `LLMClient` and
never to a backend, and every proposal goes through the same verifier regardless
of which model produced it — which is what makes a small local model safe to use
here in a way it would not be in a pipeline that trusted output.

## Evaluation

`unnet/evaluation/generator.py` writes the fixtures **and** a ground-truth file
recording which order produced which line, which lines went into which payout,
and which credit paid it. The engine never reads it.

`unnet/evaluation/score.py` grades links against that truth and reports
**false-match rate as the headline**, not accuracy.

The truth file separates two things that are easy to conflate:

- `expected_exceptions` — breaks that were injected and should be reported.
- `hard_cases` — links deliberately made hard to find, but still findable.
  Hiding a UTR does not create a break; the money is right, only the obvious link
  is gone. A run that recovers it another way has done better, not worse, and
  grading those as exceptions the engine "should" have raised would penalise it
  for succeeding.

---

## 6. Case files — closing the loop

`engine/casefile.py`. The track asks for an agent that *closes* a loop;
detecting and reporting is the first quarter of one.

Every unresolved exception becomes a case with an **owner** (the party who can
actually fix it), an **ask**, an **evidence pack**, and a **stable key**:

```python
case_key = sha256(f"{code}|{subject_kind}|{subject_id}")[:16]
```

Derived from what the problem *is*, not from a row id — every run re-parses the
sources and allocates new ids, so identity has to survive that or the loop never
closes. Run 1 raises and routes; `unnet resolve <key>` settles one; run 2
reports it settled and does not raise it again. Cases still open keep their
original first-seen run, so ageing is real.

Routing is a lookup table, not a model call. The owner of a `FEE_MISMATCH` is
always Razorpay support.

Money is reported **identified**, never recovered, and split four ways that are
never summed — claimable, at risk, bookkeeping, contestable loss. A chargeback
already lost and a fee a supplier owes back are not the same rupee.

## Observability — three artefacts, deliberately

**Audit** (`recon_audit`): what was decided, by which rule or model, on what
evidence, with what the verifier said. Model decisions carry
`source:model:prompt_hash`, so the exact input behind a decision is recoverable.

**Trace** (`agent_trace` on the exception): how the agent got there, step by step.

**Metrics** (`docs/METRICS.md`): how well it did.

Nothing beyond those three. The test is whether a reader can reconstruct one
decision end to end from the database, and they can.
