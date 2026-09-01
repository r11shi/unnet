# Five-minute pitch — script and shot list

The brief says to record it like you are explaining the build to an engineer, not
a recruiter. So: architecture and trade-offs, no product-marketing voice, and the
uncomfortable parts stated rather than skipped.

**Before recording:** `make demo`, then have the dashboard open on Overview,
`docs/METRICS.md` open in a second tab, and `unnet/agents/verifier.py` open in an
editor.

---

## 0:00 — 0:40 · The problem, with a real number on screen

> A Razorpay T+2 settlement lands in a merchant's bank account as **one** NEFT
> credit covering hundreds of orders — net of MDR, 18% GST on that MDR, refunds,
> chargebacks and adjustments. The merchant's books have the gross orders.
> Nothing links the two but a UTR buried in a bank narration string.
>
> Razorpay's own 2026 merchant playbook names the two failure modes: missing
> UTRs, so payouts can't be matched to credits, and unexplained deductions
> finance can't trace. Right now a person unpacks that by hand in a spreadsheet
> every week.
>
> This is Unnet. It takes those three files and un-nets that single credit back
> to the order, to the paisa.

*Shot: the Overview screen. Let the tiles land — ₹1.54 crore reconciled, 1,580
links, 129 exceptions, 950 milliseconds.*

---

## 0:40 — 1:30 · The signature visual

*Shot: Un-netting tab. Pick a payout with refunds and a chargeback.*

> One bank credit, ₹4,20,176.13. Here's where it came from: ₹4,86,948 of gross
> sales, less ₹4,762 of MDR, less ₹857 of GST **on that MDR** — not on the
> transaction — less ₹61,152 of refunds.
>
> And here's the part I care about. That figure is derived two independent ways:
> once from the report's credit and debit columns, once from amount and fee
> dispatched by entity type. They have to agree. Two derivations that agree is a
> proof; one derivation is just an assertion.
>
> Then the bank credit. Residual: zero.

*Point at the Proof panel — four rows, same number, ₹0.00 residual.*

> One note on the chart: the y-axis doesn't start at zero, and it says so at the
> bottom. Gross is seven lakh and MDR is five thousand — on a zero-based axis
> every deduction, which is the whole point of the chart, is a one-pixel line.

---

## 1:30 — 2:30 · Where the AI is, and where it deliberately isn't

> The brief says if you force an LLM into a problem a rule solves better, you get
> marked down. So let me be direct about this, because it's the main design
> decision in the project.
>
> **No model touches the matching, the arithmetic, the netting proof, or the fee
> and GST recomputation.** Reconciliation is an equality problem over integers. A
> model is worse at `==` than `==` is, can't be audited, and can't be reproduced.
> Money is integer paise end to end — there's no float anywhere on the matching
> path.
>
> A model is used in exactly three places, each gated by something
> deterministic. Schema mapping, because every bank exports a different layout
> and that alias table can't be finished — but the mapping it proposes is
> dry-run parsed against real rows before anything uses it. Exception triage,
> which I'll show you. And natural-language questions, which become a single
> read-only SELECT that's displayed next to the answer.
>
> And critically: **exact search runs before the model does.**

*Shot: Exceptions tab, the resolved UNMATCHED_BANK_CREDIT at the top.*

> This bank credit matched no payout, because the bank posted two payouts as one
> line. That's closed by bounded subset-sum — two settlements summing to exactly
> that credit. No model. If arithmetic can close an exception, arithmetic closes
> it. That cost me a flashier demo and I think it's the right call.

---

## 2:30 — 3:20 · The verifier — the failure case

*Shot: `unnet/agents/verifier.py` in the editor.*

> So where does a model earn its place? When a payout of ₹8,36,364 arrives as
> ₹8,36,352, that ₹11.80 is in **no table** — it's the bank's own NEFT charge,
> which appears nowhere in the settlement report. Search can't find what it
> doesn't know to look for. A model that's seen Indian bank statements proposes
> "₹10 charge plus 18% GST".
>
> It stays a hypothesis until it sums exactly. This file is the gate. A model may
> *propose* a decomposition. It may not *post* one.
>
> The components must sum to the residual exactly, in integer paise. It must not
> cite a settlement that doesn't exist, or cite one twice, or claim money already
> reconciled elsewhere, or restate a real payout's amount to make the total work
> — that last one is the subtle case, because the arithmetic passes and it's
> still wrong.
>
> **There's no tolerance.** Fifty paise is nothing to a person skimming a
> summary and everything to the books. A decomposition that's fifty paise out
> isn't a slightly wrong answer — it's evidence the reasoning behind it was
> wrong. When it's rejected, the exception stays open, and the analyst sees the
> proposal *and* the verdict, because they should see what was tried and why it
> didn't hold.

*Shot: `pytest tests/test_verifier.py -q` — twelve adversarial proposals, all
refused.*

---

## 3:20 — 4:20 · The numbers, including the bad one

*Shot: `docs/METRICS.md`.*

> Everything here is scored against a ground-truth file the engine never reads.
> On the standard fixtures: 100% match rate, zero wrong links, 3,173 records in
> under a second.
>
> I don't think you should be impressed by that. I generated the data and built
> the engine against it — 100% is a statement about internal consistency, not
> production accuracy.
>
> **This** is the number worth reading.

*Scroll to the robustness table.*

> Same engine, but 35% of the gateway identifiers are gone — which isn't
> hypothetical; Razorpay's recon report genuinely leaves those blank for some
> entities. Match rate falls from 100% to 65%. **False-match rate goes from 0% to
> 0.19%.** Recall collapses by a third; precision barely moves.
>
> That's deliberate. Every fuzzy rule requires a *unique* candidate and refuses
> when more than one fits. With no identifier, hundreds of small UPI orders share
> an amount and a minute, and the honest answer is we don't know which is which
> — so those go to the exception queue.
>
> Picking the nearest and moving on would have given me a much prettier match
> rate and a ledger nobody should trust. In reconciliation a missed link costs an
> analyst five minutes; a wrong link attributes money to the wrong order and gets
> found months later by an auditor. Those aren't worth trading at parity.

---

## 4:20 — 5:00 · Honesty, and what's next

> Two things I want to be straight about.
>
> One: the model layer is built, wired and unit-tested, but the committed metrics
> were produced with **no provider configured**. So the ablation's "rules + model"
> column currently equals the rules column, and the run reports the AI layer as
> unexercised rather than pretending. Nothing in that metrics file is a model
> result a model didn't actually produce. Point it at a local llama.cpp server or
> a Gemini key and it fills in — that's one environment variable, not a code
> change.
>
> Two: `docs/FAILURES.md` lists seven bugs I hit, including two in the *fixture
> generator* — one that silently dropped refunds and corrupted the ground truth,
> which is the dangerous kind, because the engine looks wrong and your instinct
> is to fix the engine.
>
> Everything runs offline. `git clone`, `make demo` — Python, no API key, no
> node, no npm install. The dashboard is one static HTML file.
>
> The next thing I'd build is incremental runs: timing differences are already
> marked rolled-forward, but nothing yet reads the previous run and carries them
> in. That's what a real merchant needs first.

---

## Notes for recording

- Have `make demo` already finished. Don't record an install.
- Slow down on the Proof panel and the robustness table. Those are the two moments
  that carry the whole argument.
- If asked to cut, cut section 1 (problem) — not section 4 (the bad number).
- Say "MDR", "UTR", "T+2" without explaining them. The audience is Razorpay.
