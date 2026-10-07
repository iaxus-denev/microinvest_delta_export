# -*- coding: utf-8 -*-
import itertools
from datetime import date

from odoo import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.delta_export_service import DeltaExportService


class DeltaExportTestCommon(AccountTestInvoicingCommon):
    """Shared fixtures for Microinvest Delta export tests.

    Builds a minimal, controlled sales/inventory/accounting setup:
    one storable, real-time-valuated product; one Bulgarian customer
    with a VAT/EIK/address matching the acceptance_cases.json synthetic
    party; 20%/9%/0% sale taxes; a cash payment journal; and a helper
    to deliver sale order lines + validate pickings for OP generation.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.country_id = cls.env.ref('base.bg')
        cls.currency = cls.company.currency_id

        # AccountTestInvoicingCommon only sets up accounting/product
        # fixtures; our module depends on sale_stock + stock_account,
        # so the acting test user also needs Sales + Stock rights to
        # create/confirm sale orders and validate deliveries.
        cls.env.user.group_ids |= (
            cls.env.ref('sales_team.group_sale_manager')
            | cls.env.ref('stock.group_stock_manager')
            | cls.env.ref('account.group_account_invoice')
        )

        cls.tax_20 = cls.env['account.tax'].create({
            'name': 'Delta Test VAT 20%',
            'amount': 20,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })
        cls.tax_9 = cls.env['account.tax'].create({
            'name': 'Delta Test VAT 9%',
            'amount': 9,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })
        cls.tax_0 = cls.env['account.tax'].create({
            'name': 'Delta Test VAT 0%',
            'amount': 0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })

        cls.partner = cls.partner_a.copy({
            'name': 'Тестов клиент ООД',
            'vat': 'BG123456786',
            'company_registry': '123456786',
            'city': 'СОФИЯ',
            'street': 'ул. Пример',
            'street2': '1',
            'country_id': cls.env.ref('base.bg').id,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Delta Test Product',
            'type': 'consu',
            'is_storable': True,
            'categ_id': cls.stock_account_product_categ().id,
            'standard_price': 6.0,
            'list_price': 10.0,
            'taxes_id': [Command.set(cls.tax_20.ids)],
        })

        cls.cash_journal = cls.env['account.journal'].create({
            'name': 'Delta Test Cash',
            'type': 'cash',
            'code': 'DCASH',
            'company_id': cls.company.id,
        })

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1,
        )

        # Delta requires a purely-numeric, <=10-digit official document
        # number (account.move.name). Odoo's own default invoice
        # sequence produces values like 'INV/2026/00001', which is NOT
        # numeric - in a real client project this would be solved by
        # configuring that journal's sequence to be purely numeric.
        # For these tests we simulate that numeric sequence directly.
        cls._invoice_number_seq = itertools.count(1)

    @classmethod
    def stock_account_product_categ(cls):
        stock_valuation_account = cls.env['account.account'].create({
            'name': 'Delta Test Stock Valuation',
            'code': 'DSTOCKVAL',
            'reconcile': True,
            'account_type': 'asset_current',
            'company_ids': [Command.set(cls.company.ids)],
        })
        return cls.env['product.category'].create({
            'name': 'Delta Test Category',
            'property_valuation': 'real_time',
            'property_cost_method': 'fifo',
            'property_stock_valuation_account_id': stock_valuation_account.id,
        })

    # ------------------------------------------------------------------
    # order / delivery / invoice helpers
    # ------------------------------------------------------------------
    def _create_order(self, qty=1, price=10.0, tax=None, product=None,
                       partner=None, date_order=None, **values):
        partner = partner or self.partner
        vals = {
            'partner_id': partner.id,
            'partner_invoice_id': partner.id,
            'partner_shipping_id': partner.id,
            'warehouse_id': self.warehouse.id,
            'order_line': [Command.create({
                'product_id': (product or self.product).id,
                'product_uom_qty': qty,
                'price_unit': price,
                'tax_ids': [Command.set((tax or self.tax_20).ids)],
            })],
        }
        if date_order:
            vals['date_order'] = date_order
        vals.update(values)
        order = self.env['sale.order'].create(vals)
        order.action_confirm()
        return order

    def _deliver(self, order, qty=None, date_done=None):
        """Validate (all or the given quantity of) the order's outgoing
        picking(s). Returns the validated picking."""
        picking = order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        for move in picking.move_ids:
            move.quantity = qty if qty is not None else move.product_uom_qty
            move.picked = True
        # skip_backorder: a partial delivery (qty < ordered qty) would
        # otherwise return a backorder-confirmation wizard action instead
        # of actually validating the picking - tests that specifically
        # want a second picking for the remainder create/validate it
        # explicitly themselves (see test_c12).
        picking.with_context(skip_backorder=True).button_validate()
        if date_done:
            picking.move_ids.write({'date': date_done})
            picking.write({'date_done': date_done})
        return picking

    def _invoice_order(self, order, invoice_date=None, post=True):
        invoice = order._create_invoices()
        if invoice_date:
            invoice.invoice_date = invoice_date
        if post:
            self._assign_numeric_name(invoice)
            invoice.action_post()
        return invoice

    def _assign_numeric_name(self, move):
        """Force a purely-numeric official document number, simulating a
        client journal configured with a numeric-only sequence (Delta's
        required document-number format)."""
        move.name = str(next(self._invoice_number_seq)).zfill(10)

    def _register_payment(self, moves, amount=None, payment_date=None,
                           journal=None):
        """Register a cash payment against one or more posted moves using
        the standard Odoo payment wizard (account.payment.register).

        ``journal_id`` must be set in the initial ``create()`` call, not
        afterwards - setting it after creation re-triggers the wizard's
        ``_compute_amount`` (depends on ``currency_id``, which depends on
        ``journal_id``) and silently resets ``amount`` back to the full
        residual, discarding any explicit partial ``amount`` passed in.
        """
        wizard_vals = {'journal_id': (journal or self.cash_journal).id}
        if payment_date:
            wizard_vals['payment_date'] = payment_date
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=moves.ids,
        ).create(wizard_vals)
        if amount is not None:
            wizard.amount = amount
        return wizard._create_payments()

    def _run_export(self, date_from, date_to, company=None):
        service = DeltaExportService(
            self.env, company or self.company, date_from, date_to,
        )
        return service.generate()

    @staticmethod
    def _decode_rows(payload):
        """Decode a generated cp1251 payload into a list of field-lists,
        one per line (CRLF-terminated, last empty split dropped)."""
        text = payload.decode('cp1251')
        lines = text.split('\r\n')
        if lines and lines[-1] == '':
            lines = lines[:-1]
        return [line.split('|') for line in lines]
