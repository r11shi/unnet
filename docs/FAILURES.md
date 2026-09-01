# What broke, and how it is handled

The brief asks for one failure handled gracefully. This is that one, plus the
bugs found along the way — because the bugs are the more useful answer to
"what did you actually learn building this".

---

## The failure the system is designed around

**A model proposes a decomposition that is plausible, well-formed, and wrong.**

When a payout of ₹8,36,364.23 arrives as ₹8,36,352.43, the ₹11.80 gap is in no
table — it is the bank's own NEFT charge, which appears nowhere in the settlement
report. A model that has seen Indian bank statements will propose "₹10 NEFT
charge plus 18% GST", and it will sound right.

Sometimes it *is* right. Sometimes it proposes ₹10 plus GST when the bank charged
₹12.50, and the total is out by ₹0.50.

### How it is handled

The proposal never reaches the ledger. `unnet/agents/verifier.py` requires the
components to sum to the residual **exactly, in integer paise**. Off by fifty
paise, the proposal is rejected, the exception stays open with status
`ai_rejected`, and the rejection is written to the audit trail with its reason.

The dashboard then shows the analyst the rejected proposal *and* the verdict:

> **PROPOSED BY** model:gemini:a3f9c21e08b4
> The ₹11.80 shortfall is a bank NEFT charge of ₹10.00 plus 18% GST.
> `neft_fee ₹10.00 + gst_on_fee ₹1.80 = ₹11.80`
> **verifier: rejected_sum_mismatch** — Components sum to 1180 paise but must
> explain 1230 paise — out by −50 paise.

Rejected proposals are kept rather than discarded, because the person picking the
item up should see what was tried and why it did not hold.

### Why there is no tolerance

The tempting fix is a ±₹1 slack, which would make most of these pass. It is the
wrong fix. A decomposition that is ₹0.50 out is not a slightly wrong answer — it
is evidence that the reasoning behind it was wrong. The smallness of the number
tells you about the size of the error, not the size of the mistake. Accepting it
posts a wrong figure into books that people file GST returns from.

`tests/test_verifier.py` holds twelve adversarial proposals, each of which must
be refused. The subtlest is
`test_rejects_a_restated_amount_even_when_the_total_is_right`: a proposal citing
two real payouts, one with a doctored amount, whose total comes out exactly
correct. Arithmetic alone passes it. It is still wrong, and it is rejected.

---

## The second failure: the model layer becomes unavailable mid-run

Free-tier APIs return 429 under load and local servers get killed. A
reconciliation that dies two thirds of the way through because a quota ran out is
worse than one that finishes on rules alone.

`LLMClient` trips a circuit breaker after three consecutive backend failures. The
run continues on rules, the affected exceptions are marked `not_attempted` rather
than silently skipped, and `run.llm_degraded` is set so the dashboard and the
metrics report the degradation instead of quietly publishing a lower number as if
it were the full result.

Verified by construction: the committed metrics were produced with **no model
configured at all**, and the run completes cleanly and says so.

---

## Bugs found while building

Each of these was invisible to unit tests and obvious the moment the whole run
was scored against ground truth. All are now regression-tested.

### A UTR match asserted identity, not amount

`T1.UTR_EXACT` linked a bank credit to a payout by UTR and recorded the amount
delta in its evidence — but only the *fuzzy* rule raised the residual as an
exception. So the most common real case (correct UTR, bank deducted its own
charge) reconciled silently with the shortfall swallowed.

Residual checking is now one pass over every tier-1 match, whatever rule made it.
`test_a_short_credit_is_caught_even_when_the_utr_matched`.

### Duplicate ledger rows blocked their own match

Two rows sharing an `order_id` made every uniqueness lookup ambiguous, so the
matcher refused both copies — and dragged the real settlement line into the
orphan pile with it. One duplicated row cost two matches and produced a spurious
`ORPHAN_SETTLEMENT_LINE`.

Duplicates are now collapsed to a canonical row before matching. Then the fix had
its own bug: surplus copies were claimed by `order_id`, which the canonical row
*shares*, so an order that was both duplicated and risk-held lost its `ON_HOLD`
flag entirely. Deduplication now happens on emission instead.

### Risk-held payments were reported as settled

Razorpay reports a held payment as a line with `on_hold: true`, `settled: false`
and no settlement id. Tier 2 was matching those like any other payment line,
telling the merchant their order had settled when the money was frozen.

Held lines are excluded from matching and surface as `ON_HOLD` instead.

### A bank statement with no credit column validated fine

`REQUIRED_FIELDS` for a bank statement listed only `narration` and `value_date`.
A statement whose credit column failed to map therefore passed validation and
reconciled *nothing* — every payout reported as missing, with no error anywhere
saying why. `credit` is now required.

### The header normaliser did not strip punctuation

`"Withdrawal Amt."` did not match the alias `"withdrawal amt"` because of the
trailing full stop, making a format we already supported look like an unknown
bank. Found while building the robustness profile, where it briefly made the
results look worse — and more interesting — than they actually were.

### The fixture generator silently dropped reversals

Refunds were scheduled on a settlement day, but days outside the batch window
produced no batch, so those reversals vanished. The merchant's ledger recorded a
refund the gateway never showed — an *unlabelled* defect, which corrupts ground
truth in the worst way: the engine correctly reports a break, and the scorer
counts it as a false positive.

Reversal days are now clamped into the window. A generator bug that makes the
engine look wrong is far more dangerous than one that makes it look right,
because the instinct is to "fix" the engine.

### Two defect classes came out empty

`SHORT_CREDIT` and `ORPHAN_SETTLEMENT_LINE` were injected by per-batch
probability. Over 21 batches at 2%, the first seed produced zero of each — so the
metrics reported perfect handling of exceptions the fixture never contained.

Batch-level defects are now drawn by count, from disjoint pools. Detailed in
[DECISIONS.md](DECISIONS.md#10-defects-are-injected-by-count-not-by-probability).

---

## Known limitations

Stated plainly, because a submission that claims none is not being honest.

- **100% on the standard fixtures is a statement about internal consistency,
  not production accuracy.** I generated the data and built the engine against
  it. The number worth reading is the messy profile: 65.4% match, 0.19%
  false-match, where a third of the identifiers are gone.
- **The model layer has not been run against a live model.** It is built,
  wired and unit-tested, and the verifier's rejection paths are covered — but
  the committed metrics were produced with no provider configured, so the
  ablation's model column currently equals the rules column. It says so rather
  than implying a result that was not measured.
- **No incremental runs.** `TIMING_DIFFERENCE` items are marked rolled-forward
  but nothing yet reads the previous run to carry them in.
- **Tier 2's fuzzy rule buckets on exact paise**, so an order differing from its
  settled line by a rounding paisa is never considered as a candidate at all.
- **The Q&A intent layer is regex** and will not generalise beyond the questions
  it names.
