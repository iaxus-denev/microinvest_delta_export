# Microinvest Delta Export

Odoo 19 module that generates `Import.txt`, the fixed-width pipe-separated
TXT file consumed by Microinvest Delta's import, from a single Odoo
company's sales data for a selected period.

Depends only on `account`, `sale_stock`, `stock_account` — no Point of
Sale, no custom configuration profile, no export history/status model.
Every export is a full, deterministic re-computation for the chosen
period; running it twice on unchanged data produces a byte-identical
file.

## What gets exported

For the selected `[date_from, date_to]` (inclusive) period, for the
selected company:

1. **Sales invoices and credit notes** (`account.move`, posted,
   `out_invoice`/`out_refund`, `invoice_date` in period) — one `code 2`
   row per actual VAT rate on the document (20% → tax code `16`, 9% →
   `20`, 0% → `21`), plus one combined `code 8` cost row for the whole
   invoice (never for credit notes).
2. **Cash "OP" sales** — on-account cash sales derived from validated
   outgoing customer pickings whose delivered quantity has not (yet)
   been invoiced. One persistent `delta.op.reference` number per
   `(company, sale_order, picking)`, generated once and never reused or
   regenerated on repeated exports.
3. **Cost of goods sold** (`code 8`) — read directly from the already
   computed, historical `stock.move.value` (set once when the picking
   is validated). Never recomputed from `standard_price`, FIFO/SVL, or
   any other live costing field, so a later change to the product's
   cost never changes a past export.
4. **Cash customer payments** (`code 10`) — `account.payment`,
   `partner_type='customer'`, cash journal, `state in ('in_process',
   'paid')`, `date` in period. Each row uses the payment's own date and
   the allocation adapter described below to find which document(s) it
   actually paid and how much.

## TXT protocol (`lib/protocol.py`)

`Import.txt`, Windows-1251 (`cp1251`) encoding, no BOM, 16 pipe-separated
fields per line, CRLF after every line including the last one. Any
character that cannot be represented in `cp1251` raises a specific
document+field error — it is never silently replaced (e.g. with `?`).
Internal `|`, CR, LF and tab characters are sanitized to a single space
before a field is emitted, so the 16-field/line structure is never
corrupted by free-text content (partner names, notes, addresses).

Money fields are always rendered with exactly two decimal places
(`123.00`, never `123` or `123.0`), `-0.00` is normalized to `0.00`, and
all monetary math is done in `decimal.Decimal` to avoid binary-float
drift. Field 16 (real VAT) is the literal string `0` (no decimals) on
cost rows (`code 8`) and `0.00` (with decimals) on sale/payment rows —
this distinction is intentional and tested (see `test_c05_zero_vat`).

## Explicit field-mapping decisions

The assignment specification explicitly leaves several field sources
open to be connected to whatever the *current* client project already
uses, rather than invented. **No client project is targeted by this
build** (confirmed with the requester — see `docs/IMPLEMENTATION_QUESTIONS.md`
in the sibling `delta_export_assignment_bundle` repo for the full
decision record). The mappings below were reviewed and **confirmed by
the client's consultant** (see `docs/CONSULTANT_QUESTIONNAIRE_answers.md`):
official document numbers are purely numeric in the real project,
`res.partner.x_itc_liable_person` is the real client's MOL field,
`account.move.partner_bank_id`/`narration` are the right bank/note
sources, `res.partner.company_registry` is the right EIK source, cash
payments in the real process always go through standard Odoo
reconciliation (no payments bypass reconciliation), and OP-without-invoice
sales are always converted to a regular invoice before being paid in
cash in the real process (so the dedicated `delta.payment.op.allocation`
explicit-link model remains available for completeness/defense-in-depth,
but is not expected to be the primary path in the real client process).
The access-rights group remains the module's own standalone group (no
existing client group to mirror).

| Field | Source | Notes |
|---|---|---|
| Official document number (field 3) | `account.move.name` | Must resolve to a purely numeric string ≤ 10 digits (then zero-padded to 10). **Confirmed by client consultant**: the real project's invoice/credit-note sequence is purely numeric. If a specific journal ever produces a non-numeric name (e.g. `INV/2026/00001`), a specific `UserError` is still raised naming the document — the number is never stripped/mangled to force a fit. |
| MOL ("мол"/responsible person, field 8) | `res.partner.x_itc_liable_person` (document's billing partner, for both invoice/credit-note and OP rows) | **Confirmed by client consultant.** This is a pre-existing custom field on `res.partner` in the client project (not part of this module, not part of standard Odoo). If the field doesn't exist on a given database (e.g. a plain vanilla dev/test install), the export falls back to an empty MOL value rather than erroring — see `DeltaExportService._mol()`. |
| Bank/account (field 13) | `account.move.partner_bank_id.acc_number`, else three spaces (`'   '`) | **Confirmed by client consultant.** OP rows always use three spaces (no analogous field on `sale.order`). |
| Note (field 15) | `html2plaintext(account.move.narration or '')`, else a single space | **Confirmed by client consultant.** OP rows always use a single space. |
| OP partner (billing identity for `code 2`/`code 8` OP rows) | `sale_order.partner_invoice_id` (+ its `commercial_partner_id` for VAT/EIK) | Per the assignment's explicit resolution. |
| Payment partner (field 7/9/10/11/12 on `code 10` rows) | The **linked document's** `partner_id`/`commercial_partner_id` (`move.partner_id`, never `payment.partner_id` directly) | Per the assignment's explicit resolution — a payment's own partner is not used, since it must reflect the document being paid. |
| EIK/company registry fallback (field 12) | `partner.company_registry`, else: strip a leading `BG` from `partner.vat` **only if** the remainder is purely numeric | **Confirmed by client consultant.** Never alters an already-present `company_registry`. |

## Payment allocation adapter (`models/delta_payment_allocation.py`)

Implements the assignment's 3-tier priority chain for determining
exactly how much of a given payment was allocated to which document:

1. **Tier 1 — client adapter hook** (`_get_client_allocations`): returns
   `None` by default (no client project targeted). A future client
   integration can override this single method to plug in an existing,
   already-trusted allocation mechanism, without touching tiers 2–3.
2. **Tier 2 — `account.partial.reconcile`**: the standard Odoo
   reconciliation records created whenever a payment is matched against
   one or more invoice/credit-note lines (covers normal single and
   grouped payments, including historical ones, as long as they were
   reconciled through the standard mechanism).
3. **Tier 3 — technical snapshot** (`delta.payment.allocation.snapshot`):
   populated in the *same transaction* as payment registration, by a
   small override of `account.payment.register._reconcile_payments()`.
   Used only when tier 2 yields nothing for an invoice/credit-note target.

A payment-to-OP link is not available in standard Odoo. The addon therefore
accepts OP payment rows only from an explicit durable
`delta.payment.op.allocation` record. A client integration must create that
record from its actual payment process; the addon never guesses an OP from a
partner, amount, memo, or sale order. Invoice targets use the first non-empty
source (tier 1, then tier 2, then tier 3), so a standard-wizard payment with
both reconciliation and snapshot data is exported once. This is covered by
`test_c23_tier2_snapshot_deduplication`.

If a payment has any linked invoice candidate but no exact reconciliation or
snapshot, the export raises a `UserError` naming that payment. Fully unlinked
payments are omitted. Allocation targets are additionally restricted to
posted `out_invoice`/`out_refund` documents or an explicit OP reference.


## OP (persistent on-account-sale reference)

`models/delta_op_reference.py` — one `delta.op.reference` record per
`(company_id, sale_order_id, picking_id)`, enforced by a SQL unique
constraint. Numbers are drawn from a single `ir.sequence`
(`delta.export.op.reference`, `padding=10`, `company_id=False`).

The sequence is **shared globally across all companies** rather than
one sequence per company. This is a deliberate, documented simplification:
the assignment only requires OP numbers to be globally unique and never
reused/regenerated — it does not require per-company-contiguous
numbering. If a client project later requires per-company-contiguous OP
numbers, this would need one `ir.sequence` per company instead.

Creation is race-safe: `_get_or_create()` searches for an existing
record first, and if none is found, wraps the `create()` call in a
savepoint, catching a `UniqueViolation` (concurrent creation by another
transaction) by re-searching and returning the already-committed row
instead of raising or drawing a second sequence number.

OP dates are computed from `picking.date_done` (stored as naive UTC by
Odoo) converted to Europe/Sofia local time via `zoneinfo.ZoneInfo`,
which correctly handles the DST transition (verified by
`test_c19_timezone_dst_boundary`: 31 July 22:30 UTC is already 1 August
local time in Sofia, due to summer DST, so it belongs to August's
export, not July's).

## Cost allocation (FIFO, shared by invoice and OP cost rows)

`DeltaExportService._line_consumption()` is the single piece of logic
both invoice-cost and OP-leftover cost rows are built from. For a given
sale order line, it:

1. Builds the queue of all its `done` outgoing delivery moves (via
   `sale.order.line._get_outgoing_incoming_moves(strict=True)`), each
   carrying its own already-computed `stock.move.value` and
   `_get_valued_qty()`.
2. Walks **all** posted invoice lines ever created against that order
   line — across all time, not just the export period, so that a
   quantity/cost slice already consumed by an invoice from a previous
   month is never re-counted by this month's OP, and vice-versa.
3. Consumes the delivery-move queue FIFO-style against that invoice
   history; whichever invoice line exactly exhausts a move's remaining
   quantity gets that move's exact residual cost (`move.value -
   already_assigned`), to avoid a lost cent from repeated proportional
   rounding; everyone else gets a proportional slice
   (`move.value * taken_qty / move.original_qty`).
4. Whatever quantity (and its proportional cost) is left over after all
   invoice lines are consumed becomes this period's OP leftover.

This is why cost is always exact to the cent and never double-counted
or invented, across any combination of partial deliveries, partial
invoicing, multiple invoices, and multiple OPs for the same order. A valid
valued move whose stored `value` is zero still produces a code-8 row with
`0.00`; only a document with no applicable valued outgoing move omits it.

## Known limitations / simplifications

These are explicit, intentional scope decisions — not bugs:

* **Multi-currency OP conversion** (`sale.order.currency_id._convert(...)`,
  used only when an order's currency differs from the company currency)
  is implemented per the standard Odoo conversion API, but is **not**
  exercised by any of the 25 automated tests (all acceptance cases in
  `acceptance_cases.json` are single-currency). Treat this path as
  implemented-but-unverified until tested against a real multi-currency
  order.
* `test_c09_payment_allocation_without_journal_entry_dependency` is a
  simplified proxy for the assignment's original case (comparing an
  allocation "with vs. without a journal entry present"): both payments
  constructed in the test go through the standard wizard with real
  posted journal entries (tier-2 path). Constructing a payment that is
  genuinely *not* backed by any journal entry while still being linked
  to a document, through purely standard Odoo flows, was not
  straightforward in a unit test; the test instead proves that two
  independent normal payments both export their own correct allocation.
  **Confirmed by client consultant**: in the real process, cash
  payments are never linked to a document without going through
  standard Odoo reconciliation, so this simplification is not expected
  to be a real-world gap — it's retained purely as defense-in-depth
  (an explicit `UserError` instead of a silent wrong/zero allocation
  if this assumption is ever violated).
* `test_c06_multiple_allocations` uses its own self-consistent amounts
  (two orders of 72.00 and 36.00, one grouped payment of 90.00) rather
  than literally reproducing `acceptance_cases.json`'s abstract
  100/60/30/10 example numbers. The behavior under test (a payment
  insufficient to cover every allocated document in full still yields
  exactly the resolvable rows, with the unallocated remainder silently
  omitted — never invented) is equivalent.
* No test does a literal byte-for-byte comparison against the six
  official fixture files copied into `tests/fixtures/` (`C01`–`C06`).
  The tests instead decode the generated payload and assert on
  individual field values, which is at least as strict for the
  assertions made, but does not prove full-file byte-identity against
  those external fixtures. `test_c17_repeatability` does prove
  byte-identical output across two runs of the *same* service, just not
  against the external reference files.
* The OP sequence is shared globally across companies (see above).
* An invoice line linked to more than one sale-order line is rejected with a
  clear validation error. Core Odoo does not persist a trustworthy per-line
  split for that case; exporting it would duplicate delivery cost. A client
  adapter must provide the exact split before such documents can be exported.

## Permissions

A dedicated `group_delta_export_user` group (implying
`account.group_account_invoice`) is created — no existing client-side
group is reused, since no client project is targeted by this build. The
wizard, the OP reference registry, and the payment-allocation snapshot
are all scoped to this group, plus multi-company `ir.rule` record rules
on the two technical models.

## Usage

Invoicing → (menu under Customers) → Microinvest Delta Export. Pick the
company (if you have access to more than one), a `date_from`/`date_to`
period (inclusive), and click Generate. The file streams directly as an
`Import.txt` download — there is no intermediate history/status record.

* `date_from > date_to` raises a clear `UserError`.
* A period with no exportable operations raises `UserError` with the
  exact message `Няма операции за избрания период` (no partial or
  empty file is ever produced).
* Any validation failure (unsupported tax rate, non-numeric official
  document number, unresolved payment allocation, un-encodable
  character, etc.) is collected across **all** affected documents and
  raised together as a single `UserError` — never a partial export.

## Running the tests

Against a local Odoo 19 instance with this module on the addons path:

```sh
odoo -d <your_test_db> -u microinvest_delta_export --stop-after-init
odoo -d <your_test_db> -u microinvest_delta_export \
    --test-enable --test-tags /microinvest_delta_export \
    --stop-after-init --log-level=test
```

(Avoid `-i ... --test-enable` without `--test-tags`, which would also
run every other installed module's own test suite.)

`tests/test_delta_export.py` covers C01–C23, including literal C04 OP
sale/cost/payment values, CP1251-invalid text, and reconciliation/snapshot
deduplication. C21 and C22 contain separate variants; the scoped suite
currently has 27 post-install tests. The no-journal-entry half of C09 and a
literal 100/60/30/10 C06 fixture remain documented limitations above.
