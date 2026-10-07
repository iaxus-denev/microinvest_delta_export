# -*- coding: utf-8 -*-
"""Payment-to-document allocation adapter.

Implements the assignment's §8 priority chain for discovering exactly how
much of a given ``account.payment`` was allocated to which document
(invoice/credit note), in the document's own currency:

1. An explicit, already-existing client allocation mechanism, if the
   current project has one (none exists in this vanilla build - the hook
   method below returns nothing by default, documented in README as a
   pending real-client verification point).
2. ``account.partial.reconcile`` - the standard Odoo reconciliation
   records created whenever a payment is actually matched against one or
   more invoice/credit-note lines. This covers normal single and grouped
   payments, including historical ones, as long as they were reconciled
   through the standard mechanism.
3. A small technical snapshot (``delta.payment.allocation.snapshot``),
   populated in the very same transaction as payment registration (see
   the ``account.payment.register`` override below), for the rare case
   where tier 2 yields nothing usable.

If neither tier yields a usable, exact split for a payment that is
actually linked to more than one document, an explicit error is raised -
this adapter never infers an allocation from the residual amount, from
the partner/name/amount/memo, or from ``payment.invoice_ids`` membership
alone (that M2M carries no per-invoice amount).
"""
from __future__ import annotations

from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

RECEIVABLE_PAYABLE_TYPES = ('asset_receivable', 'liability_payable')


class DeltaPaymentAllocationSnapshot(models.Model):
    """Technical record: exact amount of one payment allocated to one
    document, captured at payment-registration time (tier 3 of the
    adapter). Never used as a business/accounting document.
    """
    _name = 'delta.payment.allocation.snapshot'
    _description = "Microinvest Delta - payment allocation snapshot"

    company_id = fields.Many2one('res.company', required=True, index=True)
    payment_id = fields.Many2one(
        'account.payment', required=True, index=True, ondelete='cascade')
    move_id = fields.Many2one(
        'account.move', required=True, index=True, ondelete='cascade',
        help="The invoice/credit note this part of the payment was "
             "allocated to.")
    allocated_amount = fields.Monetary(
        currency_field='company_currency_id', required=True,
        help="Allocated amount in company currency, signed per the "
             "assignment's convention (positive for invoices, negative "
             "for credit notes).")
    company_currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', store=True)

    _sql_constraints = [
        (
            'delta_payment_allocation_snapshot_unique',
            'unique(payment_id, move_id)',
            "An allocation snapshot for this payment/document pair "
            "already exists.",
        ),
    ]


class AccountPaymentRegisterDeltaSnapshot(models.TransientModel):
    """Capture the exact payment->document allocation decided by the
    standard payment registration wizard, in the same transaction, right
    after the standard reconciliation calls run. This is the minimal
    "small technical snapshot" the assignment explicitly allows, used
    only as a tier-3 fallback by :mod:`delta_payment_allocation`.
    """
    _inherit = 'account.payment.register'

    def _reconcile_payments(self, to_process, edit_mode=False):
        result = super()._reconcile_payments(to_process, edit_mode=edit_mode)
        self._delta_snapshot_allocations(to_process)
        return result

    def _delta_snapshot_allocations(self, to_process):
        Snapshot = self.env['delta.payment.allocation.snapshot'].sudo()
        for vals in to_process:
            payment = vals.get('payment')
            to_reconcile = vals.get('to_reconcile')
            if not payment or not to_reconcile:
                continue
            # After ``super()``, use the partial reconciliations actually
            # created by Odoo. ``to_reconcile`` line balances are document
            # balances, not necessarily the portion paid by this payment.
            pay_lines = payment.move_id.line_ids.filtered(
                lambda line: line.account_type in RECEIVABLE_PAYABLE_TYPES)
            by_move = defaultdict(lambda: 0.0)
            for partial in pay_lines.matched_debit_ids | pay_lines.matched_credit_ids:
                if partial.debit_move_id in pay_lines:
                    counterpart = partial.credit_move_id
                    sign = -1.0
                elif partial.credit_move_id in pay_lines:
                    counterpart = partial.debit_move_id
                    sign = 1.0
                else:
                    continue
                if counterpart.move_id != payment.move_id:
                    by_move[counterpart.move_id] += sign * partial.amount
            for move, amount in by_move.items():
                amount = move.company_currency_id.round(amount)
                if move.company_currency_id.is_zero(amount):
                    continue
                existing = Snapshot.search([
                    ('payment_id', '=', payment.id),
                    ('move_id', '=', move.id),
                ], limit=1)
                if existing:
                    continue
                Snapshot.create({
                    'company_id': payment.company_id.id,
                    'payment_id': payment.id,
                    'move_id': move.id,
                    'allocated_amount': amount,
                })


class DeltaPaymentAllocationAdapter(models.AbstractModel):
    """Stateless helper exposing :meth:`get_allocations`, the single entry
    point :mod:`delta_export_service` uses to resolve payment->document
    allocations. Kept as its own abstract model (rather than free
    functions) so a future client-specific module can inherit and extend
    :meth:`_get_client_allocations` without touching this module.
    """
    _name = 'delta.payment.allocation.adapter'
    _description = "Microinvest Delta - payment allocation adapter"

    # -- tier 1 -----------------------------------------------------
    def _get_client_allocations(self, payment):
        """Hook for an explicit, already-existing client allocation
        mechanism. Returns ``None`` if the current project has none
        (the default, vanilla-Odoo case) - documented in README as a
        pending real-client verification point. A client-specific
        extension module should override this and return a list of
        ``(move, allocated_amount)`` tuples (company-currency, signed)
        when it can resolve them with certainty.
        """
        return None

    # -- tier 2 -------------------------------------------------------
    def _get_reconcile_allocations(self, payment):
        """Derive allocations from standard ``account.partial.reconcile``
        records against the payment's own receivable/payable lines.
        Returns a list of ``(move, allocated_amount)`` tuples, company
        currency, signed (positive=invoice, negative=credit note), or an
        empty list if the payment has no such reconciliation at all.
        """
        if not payment.move_id:
            return []
        pay_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_type in RECEIVABLE_PAYABLE_TYPES
        )
        if not pay_lines:
            return []
        partials = pay_lines.matched_debit_ids | pay_lines.matched_credit_ids
        by_move = defaultdict(lambda: 0.0)
        for partial in partials:
            if partial.debit_move_id in pay_lines:
                counterpart = partial.credit_move_id
                sign = -1.0
            elif partial.credit_move_id in pay_lines:
                counterpart = partial.debit_move_id
                sign = 1.0
            else:  # pragma: no cover - defensive
                continue
            move = counterpart.move_id
            if not move or move.id == payment.move_id.id:
                continue
            by_move[move] += sign * partial.amount
        allocations = []
        currency = payment.company_id.currency_id
        for move, amount in by_move.items():
            rounded = currency.round(amount)
            if currency.is_zero(rounded):
                continue
            allocations.append((move, rounded))
        return allocations

    # -- tier 3 ---------------------------------------------------------
    def _get_snapshot_allocations(self, payment):
        """Return invoice/credit-note snapshots captured at registration."""
        snapshots = self.env['delta.payment.allocation.snapshot'].sudo().search([
            ('payment_id', '=', payment.id),
        ])
        return [(s.move_id, s.allocated_amount) for s in snapshots]

    def _get_op_allocations(self, payment):
        """Return only explicitly persisted payment-to-OP allocations.

        Odoo has no standard payment-to-delivery relation.  These records are
        created by a real client flow or migration; this method never guesses
        an OP from payment text, partner, order, or amount.
        """
        allocations = self.env['delta.payment.op.allocation'].sudo().search([
            ('payment_id', '=', payment.id),
        ])
        return [(allocation.op_reference_id, allocation.allocated_amount)
                for allocation in allocations]

    # -- public entry point ----------------------------------------------
    @api.model
    def get_allocations(self, payment):
        """Return exact ``(account.move|delta.op.reference, amount)`` pairs.

        A client adapter wins completely. Otherwise invoice targets use the
        reconciliation source first and only then the registration snapshot;
        explicitly persisted OP allocations are added independently because
        they cannot be discovered by ``account.partial.reconcile``.
        """
        client_allocations = self._get_client_allocations(payment)
        if client_allocations:
            self._validate_allocations(payment, client_allocations)
            return client_allocations

        invoice_allocations = self._get_reconcile_allocations(payment)
        if not invoice_allocations:
            invoice_allocations = self._get_snapshot_allocations(payment)
        op_allocations = self._get_op_allocations(payment)
        allocations = invoice_allocations + op_allocations
        if allocations:
            self._validate_allocations(payment, allocations)
            return allocations

        if payment.invoice_ids:
            raise UserError(_(
                "Плащане '%(name)s' (дата %(date)s) е свързано с документ, "
                "но няма точно разпределение (няма реално осчетоводено "
                "съответствие или техническа снимка). Моля "
                "осчетоводете/съгласувайте плащането стандартно в Odoo преди "
                "експорт.",
                name=payment.name or payment.id,
                date=payment.date,
            ))
        return []

    def _validate_allocations(self, payment, allocations):
        currency = payment.company_id.currency_id
        total = sum(abs(amount) for _move, amount in allocations)
        available = abs(payment.amount_company_currency_signed) \
            if payment.amount_company_currency_signed else payment.amount
        if currency.compare_amounts(total, available) > 0:
            raise UserError(_(
                "Сумата на разпределените части (%(total)s) на плащане "
                "'%(name)s' надвишава наличната сума на плащането "
                "(%(available)s).",
                total=total, name=payment.name or payment.id,
                available=available,
            ))
        targets = [target for target, _amount in allocations]
        keys = [(target._name, target.id) for target in targets]
        if len(keys) != len(set(keys)):
            raise UserError(_(
                "Плащане '%(name)s' има повече от едно разпределение към "
                "един и същ документ - това би довело до двойно броене.",
                name=payment.name or payment.id,
            ))
        for target in targets:
            if target._name == 'account.move':
                if target.state != 'posted' or target.move_type not in ('out_invoice', 'out_refund'):
                    raise UserError(_(
                        "Плащане '%(name)s' е свързано с неподдържан документ "
                        "'%(doc)s'. Допускат се само осчетоводени клиентски "
                        "фактури и кредитни известия.",
                        name=payment.name or payment.id, doc=target.display_name,
                    ))
                partner = target.commercial_partner_id
                target_name = target.name
            elif target._name == 'delta.op.reference':
                partner = target.sale_order_id.partner_invoice_id.commercial_partner_id
                target_name = target.document_number
            else:
                raise UserError(_(
                    "Плащане '%(name)s' има неподдържана цел на "
                    "разпределение '%(target)s'.",
                    name=payment.name or payment.id, target=target.display_name,
                ))
            if target.company_id != payment.company_id:
                raise UserError(_(
                    "Документ '%(doc)s' принадлежи на друга фирма спрямо "
                    "плащане '%(name)s'.",
                    doc=target_name, name=payment.name or payment.id,
                ))
            if partner != payment.partner_id.commercial_partner_id:
                raise UserError(_(
                    "Документ '%(doc)s' принадлежи на друг клиент спрямо "
                    "плащане '%(name)s'.",
                    doc=target_name, name=payment.name or payment.id,
                ))
