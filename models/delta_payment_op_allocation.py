# -*- coding: utf-8 -*-
"""Explicit payment-to-OP allocation records.

Vanilla Odoo has no standard relation from ``account.payment`` to a delivery
based OP.  This technical record is therefore the only accepted fallback for
an OP cash payment: a client integration must create it from its real payment
flow.  The export never infers it from a partner, amount, memo, or order.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import _


class DeltaPaymentOpAllocation(models.Model):
    _name = 'delta.payment.op.allocation'
    _description = 'Microinvest Delta - payment to OP allocation'

    company_id = fields.Many2one('res.company', required=True, index=True)
    payment_id = fields.Many2one(
        'account.payment', required=True, index=True, ondelete='cascade')
    op_reference_id = fields.Many2one(
        'delta.op.reference', required=True, index=True, ondelete='cascade')
    allocated_amount = fields.Monetary(
        required=True, currency_field='company_currency_id',
        help='Exact company-currency amount allocated by the client process.')
    company_currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', store=True)

    _sql_constraints = [
        (
            'delta_payment_op_allocation_unique',
            'unique(payment_id, op_reference_id)',
            'An allocation for this payment and OP reference already exists.',
        ),
    ]

    @api.constrains('company_id', 'payment_id', 'op_reference_id', 'allocated_amount')
    def _check_exact_op_allocation(self):
        for allocation in self:
            if allocation.payment_id.company_id != allocation.company_id or \
                    allocation.op_reference_id.company_id != allocation.company_id:
                raise ValidationError(_(
                    'Payment, OP reference, and allocation must belong to the same company.'))
            expected_partner = allocation.op_reference_id.sale_order_id.partner_invoice_id.commercial_partner_id
            if allocation.payment_id.partner_id.commercial_partner_id != expected_partner:
                raise ValidationError(_(
                    'The payment customer must match the OP reference customer.'))
            if allocation.company_currency_id.is_zero(allocation.allocated_amount):
                raise ValidationError(_('An OP payment allocation must have a non-zero exact amount.'))
