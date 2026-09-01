# Decisions

Why each significant choice was made, including the ones I would revisit and the
ones I think are wrong but shipped anyway.

---

## 1. AI Finance Controller, not Revenue Recovery

**Chosen because the field is thinnest and the rubric is the most honest.**

Surveying what was already public before starting: Revenue Recovery had multiple
finished projects live (RazorRecover AI, RecoverAI on Vercel) — dunning is the
obvious pick. The Open Track submissions I could find were generic and unrelated
to payments. Agentic commerce is the 2026 hype pick. Reconciliation is
unglamorous, needs domain knowledge, and I could find no public submission.

But scarcity alone would be a bad reason. The stronger one is that this track's
bar — *"close one finance-ops loop across a 50+ record batch, reporting its match
rate and the exceptions it could not resolve"* — is the only one where the
numbers can be **honest by construction**. Synthetic data means real ground
truth, which means a real false-match rate rather than a demo.

---

## 2. The matching core is deterministic, and that is the AI decision

The brief warns that forcing an LLM where a rule would do better will be marked
down. Reconciliation is an equality problem over integers. A model is worse at
`==` than `==` is, cannot be audited, cannot be reproduced, and costs a network
round trip per comparison.

So the model is confined to three places where a rule genuinely cannot go, each
one gated by something deterministic. **Choosing not to use AI for the bulk of
the pipeline is the AI judgment being exercised**, not an absence of one.

The full argument, and where the model does earn its place, is in
[ARCHITECTURE.md](ARCHITECTURE.md#4-resolve--search-first-model-second).

---

## 3. Exact search runs before the model

The consolidated-credit case (a bank posting two payouts as one line) could be
handed to a model. It is instead handed to bounded subset-sum, which is exact,
instant, free, and reproducible.

This cost me the flashier demo — "watch the AI figure out the bank consolidated
two payouts" is a better five seconds of video than "watch `itertools.combinations`
figure it out". I think shipping the search is the correct engineering call and
that the tension is worth naming rather than hiding.

The model is left with the case search genuinely cannot solve: a residual whose
explanation requires a quantity in no table.

---

## 4. Money is integer paise, with no float anywhere

`0.1 + 0.2 != 0.3` is not an acceptable failure mode when the output is "your
payout was short by this much". Rates are the only place a percentage appears and
they convert to paise via integer rounding immediately.

Half-up rounding is implemented by hand because Python's `round()` uses banker's
rounding (`round(2.5) == 2`) and `//` floors toward negative infinity. Neither
matches what a payment gateway does to a fee. `tests/test_money.py` asserts this
explicitly, including the behaviour we are deliberately *not* using.

---

## 5. Ambiguity is refused, not resolved

Every fuzzy rule requires a unique candidate. Two candidates means no match and
an exception.

This is the single decision the results turn on. It costs 34 points of recall
when identifiers are missing — and it is why precision does not move. A missed
link costs an analyst five minutes; a wrong link silently attributes money to the
wrong order and is found months later by an auditor. Trading those at parity to
improve a headline number would be the wrong call for the user, however much
better it would look in this README.

---

## 6. Arithmetic consistency is not financial truth — corrected in v2

**This is the decision I got wrong the first time, and it is the most important
one in the project.**

v1's verifier accepted any proposal whose components summed to the residual. I
described that as "the model may propose, it may not post", which sounded
rigorous and was not: a component of kind `bank_charge` had nothing to look up,
so a model could **invent a number under ₹500 and have the invention pass as
verified fact**. Hallucinated evidence, laundered.

The flaw is conceptual, not a bug. A ₹11.80 shortfall is equally satisfied by a
₹10 NEFT charge plus ₹1.80 GST, by one ₹11.80 adjustment, or by two unrelated
fees. Only one of those happened. Arithmetic cannot say which, and a system that
records the model's guess as fact is lying to an auditor.

v2 splits the verdict three ways. `RESOLVED_VERIFIED` requires every component to
be **read back out of a ledger row at verification time** — a reference string is
a name, evidence is a name plus the row it came from. `HYPOTHESIS` covers a
proposal that sums exactly but rests on an invented component, or where a second
combination also fits; it is never auto-closed and goes to a human *with* the
guess attached. `REJECTED` is everything else.

The cost is real and worth naming: with inventions barred from auto-resolution,
**the model now closes almost nothing on arithmetic**. Live, it auto-closed zero
exceptions. That is the correct number, and a v1-shaped system would have
reported two "AI-resolved" exceptions on the same data.

---

## 7. No tolerance in the verifier

A proposal is rejected if it is off by one paisa. Not "off by more than a
rupee" — one paisa.

The argument for a tolerance is that a ₹0.50 discrepancy is noise. The argument
against, which I find decisive: a decomposition that is ₹0.50 out is not a
slightly wrong answer, it is *evidence that the reasoning behind it was wrong*.
The number being small tells you about the size of the error, not the size of the
mistake. Accepting it posts a wrong number to books that people file taxes from.

Real rounding drift is handled where it belongs — classified as `ROUNDING` by a
rule, with the ±5 paise threshold visible in `tier1.py`, not hidden in a
verifier's slack.

---

## 8. The dashboard is one HTML file with no build step

I planned a Vite + React + Tailwind + Recharts SPA and changed my mind.

Against React: a reviewer would need node and `npm install`, or I would commit a
`dist/` bundle they cannot read and have to take on trust. For six views reading
JSON, the framework earns nothing.

For the single file: `git clone && make demo` needs Python and nothing else. The
charts are hand-written SVG, which is ~90 lines and gave exact control over the
one chart that matters.

**What this costs:** no component reuse, no type checking on the front end, and
`index.html` is ~1,000 lines. At three times the number of views I would want a
framework. At this size the trade is clearly worth it.

---

## 9. The waterfall's y-axis does not start at zero

Gross is ~₹7,00,000 and MDR is ~₹5,000. On a zero-based axis every deduction —
the entire point of the chart — renders as a one-pixel hairline.

Truncating an axis is normally a way to mislead, so: the axis is framed on the
band the money actually moves through, gridlines carry real rupee values, and the
chart says *"axis starts at ₹4,08,157, not zero — otherwise the deductions are
invisible"* in its own footer. This is the standard treatment for a waterfall
whose start and end are close together.

---

## 10. Ground truth separates "expected exceptions" from "hard cases"

Hiding a UTR from a bank narration does not create a reconciliation break — the
money is right, only the obvious link is gone. If the engine recovers it by
amount and date, it has done *better*, not worse.

An earlier version filed those under `expected_exceptions`, which meant the
scorer penalised the engine for succeeding. They are now `hard_cases`, carrying a
`solvable_by_rules` flag. This is a scoring-honesty decision, not a cosmetic one:
the first version would have understated the result and misdescribed what the
system does.

---

## 11. Defects are injected by count, not by probability

Over 21 payouts a 2% per-batch defect probability routinely produces a fixture
with **zero** instances of that defect. An exception class the fixture never
contains is one the metrics can never exercise, and I hit this twice —
`SHORT_CREDIT` and `ORPHAN_SETTLEMENT_LINE` both came out empty on the first
seed, silently making the results look better than they were.

Batch-level defects are now drawn by `rng.sample` at a computed count, from
disjoint pools so one payout never carries two money defects at once (which would
make the expected residual for either one wrong).

---

## 12. The offline default is cassette replay, not a stub

The model layer defaults to replaying committed recordings. It would have been
easy to ship a "stub provider" that produces correct answers via a local
heuristic and label the output as a model result. That would make the metrics a
work of fiction.

Instead: a cassette is only ever a recording of what a model actually returned,
and when there is no cassette and no provider the run says so and degrades to
rules. The published ablation currently shows no model lift **because no model
was run**, and the README says that plainly rather than implying otherwise.

---

## 13. `expire_on_commit=False` on the session

The engine builds a whole run in memory, persists it, then reports on it. With
SQLAlchemy's default, every attribute touched after the commit triggers a
re-query, and any object read after the session closes raises
`DetachedInstanceError` — which is exactly what happened the first time the CLI
tried to print a summary.

---

## 14. The agent loop is shallow, and the step count is published

A two-attempt loop that retries only on a *rejection*, feeding the verifier's
exact signed delta into the next prompt. Not eight tools and a general ReAct
loop.

The reason is that deterministic candidate generation runs first, so by the time
a model is consulted the search space is already small. An agent that re-rolls
the dice on the same evidence is not investigating.

The honest part: measured on the fixtures, **model calls per exception are
`[1, 1, 1]` with zero retries**. The retry path is covered by tests that force it
with a stub model, but this data never exercised it. That number is published in
`METRICS.md` because reporting one-step behaviour as multi-step reasoning would
be the easiest lie in this project to tell — and a reviewer who reads the trace
would catch it in a minute.

## 15. Routing is a table, not a model call

The owner of a `FEE_MISMATCH` is always Razorpay support. A short credit is
always the bank's to answer for. Asking a model to make that call would spend a
token, add latency, and be less reliable than a dict.

This matters more than it sounds: it is the difference between "we used AI for
the workflow" and "we used AI where a rule could not go". The published routing
accuracy is labelled honestly as a consequence — since the map is fixed, it
measures whether the right *exception code* was assigned, not an independent
judgement.

## 16. Money is "identified", never "recovered"

Nothing in this system recovers money. Recovery is a bank crediting funds back,
which is not an event Unnet can observe.

The four buckets — claimable, at risk, bookkeeping, contestable loss — are also
never summed, and `summarise()` deliberately exposes no combined total. A
chargeback already lost and a mis-billed fee a supplier owes back are not the
same rupee, and adding them produces a headline number that is larger than
anything demonstrated. That is the overclaim a finance reviewer catches first.

## Things I would do differently with more than four days

- **Persist the netting breakdowns as rows, not as JSON in `run.notes`.** It
  works and it is small, but it is not queryable, and the Ask agent cannot reach
  it.
- **The `matches_by_rule` bar chart is dominated by one rule** (1,459 vs 15) and
  the small bars are unreadable. A log scale or a split view would be better.
- **Tier 2's fuzzy rule only buckets on exact paise.** An order whose amount
  differs from the settled line by a rounding paisa will never be considered at
  all. A tolerance bucket would raise recall in the messy profile, and would need
  care not to raise the false-match rate with it.
- **No incremental runs.** `TIMING_DIFFERENCE` items are marked as rolled
  forward, but nothing yet reads the previous run and carries them in. That is
  the obvious next feature and the one a real merchant would need first.
- **The Q&A intent layer is regex.** It is honest about being regex, and it
  covers the questions finance actually asks weekly, but it will not generalise.
