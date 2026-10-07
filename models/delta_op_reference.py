# -*- coding: utf-8 -*-
import logging

from psycopg2 import errorcodes
from psycopg2.errors import UniqueViolation

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

SEQUENCE_CODE = 'delta.export.op.reference'


class DeltaOpReference(models.Model):
    """Persistent registry of permanent "OP" (cash-sale) document numbers.

    One row per (company, sale order, outgoing customer picking) - this is
    the *technical* record the assignment explicitly allows (never a
    parallel accounting/business document). Once a number is generated for
    a given (company, order, picking) triple it is never regenerated, so
    repeated exports for the same data are byte-identical.

    Concurrency: two simultaneous exports racing to create the same OP
    reference are resolved by the DB-level unique constraint below; on a
    ``UniqueViolation`` the create is retried by re-reading the
    already-committed row instead of raising to the user.
    """
    _name = 'delta.op.reference'
    _description = "Microinvest Delta - permanent OP (cash sale) reference"
    _rec_name = 'document_number'

    company_id = fields.Many2one(
        'res.company', required=True, readonly=True, index=True)
    sale_order_id = fields.Many2one(
        'sale.order', required=True, readonly=True, index=True)
    picking_id = fields.Many2one(
        'stock.picking', required=True, readonly=True, index=True)
    operation_date = fields.Date(required=True, readonly=True)
    document_number = fields.Char(
        required=True, readonly=True, size=10,
        help="Zero-padded 10-digit permanent OP number, from the "
             "dedicated 'delta.export.op.reference' sequence.")

    _sql_constraints = [
        (
            'delta_op_reference_unique',
            'unique(company_id, sale_order_id, picking_id)',
            "A permanent OP reference already exists for this "
            "company/sale order/picking combination.",
        ),
    ]

    @api.model
    def _get_or_create(self, company, sale_order, picking, operation_date):
        """Return the (possibly newly-created) OP reference for the given
        (company, sale_order, picking). Safe against concurrent callers:
        if another transaction wins the race to create the row, this
        re-reads and returns the committed row instead of erroring.
        """
        existing = self.search([
            ('company_id', '=', company.id),
            ('sale_order_id', '=', sale_order.id),
            ('picking_id', '=', picking.id),
        ], limit=1)
        if existing:
            return existing

        sequence = self.env['ir.sequence'].with_company(company)
        number = sequence.next_by_code(SEQUENCE_CODE)
        if not number:
            raise UserError(_(
                "Липсва номератор '%(code)s' за постоянните номера на ОП. "
                "Инсталирайте модула коректно (data/delta_sequence.xml).",
                code=SEQUENCE_CODE,
            ))

        try:
            with self.env.cr.savepoint():
                return self.create({
                    'company_id': company.id,
                    'sale_order_id': sale_order.id,
                    'picking_id': picking.id,
                    'operation_date': operation_date,
                    'document_number': number,
                })
        except UniqueViolation:
            # Lost the race: another transaction already created this
            # exact (company, order, picking) reference. Re-read it. The
            # sequence number we drew above is simply not reused anywhere
            # else, so no gap-filling or recycling is needed.
            _logger.info(
                "Delta OP reference race for order=%s picking=%s: "
                "re-reading already-committed reference.",
                sale_order.id, picking.id,
            )
            existing = self.search([
                ('company_id', '=', company.id),
                ('sale_order_id', '=', sale_order.id),
                ('picking_id', '=', picking.id),
            ], limit=1)
            if not existing:  # pragma: no cover - defensive
                raise
            return existing
        except Exception as exc:  # pragma: no cover - defensive
            pgcode = getattr(getattr(exc, 'diag', None), 'sqlstate', None) \
                or getattr(exc, 'pgcode', None)
            if pgcode == errorcodes.UNIQUE_VIOLATION:
                existing = self.search([
                    ('company_id', '=', company.id),
                    ('sale_order_id', '=', sale_order.id),
                    ('picking_id', '=', picking.id),
                ], limit=1)
                if existing:
                    return existing
            raise
