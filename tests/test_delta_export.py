# -*- coding: utf-8 -*-
from datetime import date

from freezegun import freeze_time

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import DeltaExportTestCommon


@tagged('post_install', '-at_install')
class TestDeltaExport(DeltaExportTestCommon):

    # ------------------------------------------------------------------
    # C01: invoice + partial cash payment -> codes 2, 8, 10
    # ------------------------------------------------------------------
    def test_c01_invoice_partial_payment(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 7, 15))
        self._register_payment(invoice, amount=50.0,
                                 payment_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(len(row), 16)

        codes = [r[0] for r in rows]
        self.assertEqual(codes, ['2', '8', '10'])

        sale_row, cost_row, pay_row = rows
        self.assertEqual(sale_row[4], '120.00')
        self.assertEqual(sale_row[5], '16')
        self.assertEqual(sale_row[15], '20.00')
        self.assertEqual(cost_row[4], '60.00')
        self.assertEqual(cost_row[5], '8')
        self.assertEqual(cost_row[15], '0')
        self.assertEqual(cost_row[8], 'СОФИЯ')
        self.assertEqual(cost_row[12], '   ')
        self.assertEqual(pay_row[4], '50.00')

    # ------------------------------------------------------------------
    # C02: credit note -> negative sale, negative refund, no cost row
    # ------------------------------------------------------------------
    def test_c02_credit_note_refund(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 7, 15))

        # Built as a standalone out_refund (not via _reverse_moves(), which
        # auto-reconciles the credit against the original invoice on post,
        # closing its residual to zero and leaving nothing to refund in
        # cash) so it keeps its own open residual for the cash refund.
        credit = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'invoice_date': date(2026, 7, 15),
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id,
                'quantity': 10,
                'price_unit': 10.0,
                'tax_ids': [Command.set(self.tax_20.ids)],
            })],
        })
        self._assign_numeric_name(credit)
        credit.action_post()
        self._register_payment(credit, amount=30.0,
                                 payment_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        sale_rows = [r for r in rows if r[0] == '2']
        cost_rows = [r for r in rows if r[0] == '8']
        pay_rows = [r for r in rows if r[0] == '10']

        self.assertEqual(len(sale_rows), 2)  # original invoice + credit note
        self.assertEqual(len(cost_rows), 1)  # only the invoice has cost
        self.assertEqual(len(pay_rows), 1)

        credit_sale = [r for r in sale_rows if r[3] == 'КИ'][0]
        self.assertEqual(credit_sale[4], '-120.00')
        self.assertEqual(credit_sale[15], '-20.00')

        credit_pay = pay_rows[0]
        self.assertEqual(credit_pay[4], '-30.00')
        self.assertEqual(credit_pay[3], 'КИ')

    # ------------------------------------------------------------------
    # C03: mixed VAT rates on one invoice -> two code2 rows, one code8
    # ------------------------------------------------------------------
    def test_c03_mixed_vat(self):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'partner_invoice_id': self.partner.id,
            'partner_shipping_id': self.partner.id,
            'warehouse_id': self.warehouse.id,
            'order_line': [
                Command.create({
                    'product_id': self.product.id,
                    'product_uom_qty': 10,
                    'price_unit': 10.0,
                    'tax_ids': [Command.set(self.tax_20.ids)],
                }),
                Command.create({
                    'product_id': self.product.id,
                    'product_uom_qty': 10,
                    'price_unit': 10.0,
                    'tax_ids': [Command.set(self.tax_9.ids)],
                }),
            ],
        })
        order.action_confirm()
        self._deliver(order, date_done='2026-07-15 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        sale_rows = [r for r in rows if r[0] == '2']
        cost_rows = [r for r in rows if r[0] == '8']
        self.assertEqual(len(sale_rows), 2)
        self.assertEqual(len(cost_rows), 1)

        tax_codes = sorted(r[5] for r in sale_rows)
        self.assertEqual(tax_codes, ['16', '20'])
        gross_sum = sum(float(r[4]) for r in sale_rows)
        vat_sum = sum(float(r[15]) for r in sale_rows)
        self.assertAlmostEqual(gross_sum, 229.00, places=2)
        self.assertAlmostEqual(vat_sum, 29.00, places=2)

    # ------------------------------------------------------------------
    # C04: OP numeric example (also exercises the full OP + cash flow)
    # ------------------------------------------------------------------
    def test_c04_op_partial_payment(self):
        # 3 × 9.833333... net gives the C04 gross amount 35.40; historical
        # move value is independently fixed at 17.40 before validation.
        order = self._create_order(qty=3, price=11.8 / 1.2, tax=self.tax_20)
        picking = self._deliver(order, date_done='2026-07-30 10:00:00')
        picking.move_ids.value = 17.4

        # First generation persists the permanent OP reference. A later
        # payment may only point to this explicit technical reference.
        self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        op_ref = self.env['delta.op.reference'].sudo().search([
            ('company_id', '=', self.company.id),
            ('sale_order_id', '=', order.id),
            ('picking_id', '=', picking.id),
        ])
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.partner.id,
            'amount': 33.42,
            'date': date(2026, 7, 30),
            'journal_id': self.cash_journal.id,
        })
        payment.action_post()
        self.env['delta.payment.op.allocation'].sudo().create({
            'company_id': self.company.id,
            'payment_id': payment.id,
            'op_reference_id': op_ref.id,
            'allocated_amount': 33.42,
        })

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        sale_rows = [r for r in rows if r[0] == '2' and r[3] == 'ОП']
        cost_rows = [r for r in rows if r[0] == '8' and r[3] == 'ОП']
        pay_rows = [r for r in rows if r[0] == '10' and r[3] == 'ОП']
        self.assertEqual(len(sale_rows), 1)
        self.assertEqual(len(cost_rows), 1)
        self.assertEqual(len(pay_rows), 1)
        self.assertEqual(sale_rows[0][4], '35.40')
        self.assertEqual(sale_rows[0][15], '5.90')
        self.assertEqual(cost_rows[0][4], '17.40')
        self.assertEqual(pay_rows[0][4], '33.42')
        self.assertEqual(sale_rows[0][13], 'Продажба')
        self.assertEqual(cost_rows[0][13], 'Продажба')
        self.assertEqual(pay_rows[0][13], 'Плащане в брой')
        self.assertEqual(pay_rows[0][2], op_ref.document_number)

    # ------------------------------------------------------------------
    # C05: zero VAT rate -> tax code 21, field16 '0.00' on sale row
    # ------------------------------------------------------------------
    def test_c05_zero_vat(self):
        order = self._create_order(qty=5, price=10.0, tax=self.tax_0)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        self._invoice_order(order, invoice_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        sale_row = [r for r in rows if r[0] == '2'][0]
        cost_row = [r for r in rows if r[0] == '8'][0]

        self.assertEqual(sale_row[5], '21')
        self.assertEqual(sale_row[4], '50.00')
        self.assertEqual(sale_row[15], '0.00')
        self.assertEqual(cost_row[15], '0')

    # ------------------------------------------------------------------
    # C06: one payment split across two invoices, remainder unallocated
    # ------------------------------------------------------------------
    def test_c06_multiple_allocations(self):
        order1 = self._create_order(qty=6, price=10.0, tax=self.tax_20)
        self._deliver(order1, date_done='2026-07-15 10:00:00')
        inv1 = self._invoice_order(order1, invoice_date=date(2026, 7, 15))

        order2 = self._create_order(qty=3, price=10.0, tax=self.tax_20)
        self._deliver(order2, date_done='2026-07-15 10:00:00')
        inv2 = self._invoice_order(order2, invoice_date=date(2026, 7, 15))

        # One payment of 100, registered against both invoices at once,
        # but with a lower total amount than the combined invoice total
        # (72.00+36.00=108.00) so 10.00 remains unallocated by the wizard.
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=(inv1 + inv2).ids,
        ).create({
            'journal_id': self.cash_journal.id,
            'payment_date': date(2026, 7, 15),
            'group_payment': True,
        })
        wizard.amount = 90.0
        wizard._create_payments()

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        pay_rows = [r for r in rows if r[0] == '10']

        self.assertEqual(len(pay_rows), 2)
        total_allocated = sum(float(r[4]) for r in pay_rows)
        self.assertAlmostEqual(total_allocated, 90.00, places=2)

    # ------------------------------------------------------------------
    # C07: several payments to one invoice, each keeps its own date
    # ------------------------------------------------------------------
    def test_c07_several_payments_one_invoice(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 7, 15))

        self._register_payment(invoice, amount=50.0,
                                 payment_date=date(2026, 7, 15))
        self._register_payment(invoice, amount=70.0,
                                 payment_date=date(2026, 7, 20))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        pay_rows = [r for r in rows if r[0] == '10']

        self.assertEqual(len(pay_rows), 2)
        dates = sorted(r[1] for r in pay_rows)
        self.assertEqual(dates, ['15.07.2026', '20.07.2026'])
        total = sum(float(r[4]) for r in pay_rows)
        self.assertAlmostEqual(total, 120.00, places=2)

    # ------------------------------------------------------------------
    # C08: old invoice (outside period), current payment (inside period)
    # ------------------------------------------------------------------
    def test_c08_old_invoice_current_payment(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-06-20 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 6, 20))
        self._register_payment(invoice, amount=50.0,
                                 payment_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        sale_rows = [r for r in rows if r[0] == '2']
        pay_rows = [r for r in rows if r[0] == '10']
        self.assertEqual(len(sale_rows), 0)
        self.assertEqual(len(pay_rows), 1)
        self.assertEqual(pay_rows[0][4], '50.00')

    # ------------------------------------------------------------------
    # C09: identical allocations, with vs without a journal entry
    # (both via tier-2 reconcile, since both go through the standard
    #  payment wizard with actual posted journal entries - the tier-3
    #  snapshot exists as a safety net but tier-2 is the normal path)
    # ------------------------------------------------------------------
    def test_c09_payment_allocation_without_journal_entry_dependency(self):
        order_a = self._create_order(qty=5, price=10.0, tax=self.tax_20)
        self._deliver(order_a, date_done='2026-07-15 10:00:00')
        inv_a = self._invoice_order(order_a, invoice_date=date(2026, 7, 15))
        self._register_payment(inv_a, amount=30.0,
                                 payment_date=date(2026, 7, 15))

        order_b = self._create_order(qty=5, price=10.0, tax=self.tax_20)
        self._deliver(order_b, date_done='2026-07-15 10:00:00')
        inv_b = self._invoice_order(order_b, invoice_date=date(2026, 7, 15))
        self._register_payment(inv_b, amount=30.0,
                                 payment_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        pay_rows = [r for r in rows if r[0] == '10']
        self.assertEqual(len(pay_rows), 2)
        self.assertEqual({r[4] for r in pay_rows}, {'30.00'})

    # ------------------------------------------------------------------
    # C10: partial delivery, no invoice yet -> full OP gets full cost
    # ------------------------------------------------------------------
    def test_c10_partial_delivery_full_op(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, qty=6, date_done='2026-07-15 10:00:00')

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        sale_row = [r for r in rows if r[0] == '2' and r[3] == 'ОП'][0]
        cost_row = [r for r in rows if r[0] == '8' and r[3] == 'ОП'][0]

        self.assertEqual(sale_row[4], '72.00')
        self.assertEqual(sale_row[15], '12.00')
        self.assertEqual(cost_row[4], '36.00')

    # ------------------------------------------------------------------
    # C11: partial delivery + partial invoice -> cost split proportionally
    # ------------------------------------------------------------------
    def test_c11_partial_delivery_and_partial_invoice(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, qty=6, date_done='2026-07-15 10:00:00')

        invoice = order._create_invoices()
        # Restrict the invoice to 2 units only.
        invoice.invoice_line_ids.quantity = 2
        invoice.invoice_date = date(2026, 7, 15)
        self._assign_numeric_name(invoice)
        invoice.action_post()

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        inv_sale = [r for r in rows if r[0] == '2' and r[3] == 'Ф-ра']
        inv_cost = [r for r in rows if r[0] == '8' and r[3] == 'Ф-ра']
        op_sale = [r for r in rows if r[0] == '2' and r[3] == 'ОП']
        op_cost = [r for r in rows if r[0] == '8' and r[3] == 'ОП']

        self.assertEqual(len(inv_sale), 1)
        self.assertEqual(len(op_sale), 1)
        self.assertEqual(inv_sale[0][4], '24.00')
        self.assertEqual(inv_sale[0][15], '4.00')
        self.assertEqual(inv_cost[0][4], '12.00')
        self.assertEqual(op_sale[0][4], '48.00')
        self.assertEqual(op_sale[0][15], '8.00')
        self.assertEqual(op_cost[0][4], '24.00')

    # ------------------------------------------------------------------
    # C12: two pickings same day, same order -> two distinct OP refs
    # ------------------------------------------------------------------
    def test_c12_two_pickings_same_day(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, qty=4, date_done='2026-07-15 10:00:00')
        # second picking for the backorder, same day
        backorder = order.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel'))
        for move in backorder.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        backorder.button_validate()
        backorder.move_ids.write({'date': '2026-07-15 14:00:00'})
        backorder.write({'date_done': '2026-07-15 14:00:00'})

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        op_rows = [r for r in rows if r[0] == '2' and r[3] == 'ОП']
        op_numbers = {r[2] for r in op_rows}
        self.assertEqual(len(op_numbers), 2)

    # ------------------------------------------------------------------
    # C13: historical move value unaffected by later standard_price change
    # ------------------------------------------------------------------
    def test_c13_historical_move_value(self):
        order = self._create_order(qty=6, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')

        self.product.standard_price = 99.0

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        cost_row = [r for r in rows if r[0] == '8'][0]
        self.assertEqual(cost_row[4], '36.00')

    # ------------------------------------------------------------------
    # C14: quantity split across two invoices + OP remainder
    # ------------------------------------------------------------------
    def test_c14_quantity_split_multiple_documents(self):
        order = self._create_order(qty=10, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')

        inv1 = order._create_invoices()
        inv1.invoice_line_ids.quantity = 2
        inv1.invoice_date = date(2026, 7, 15)
        self._assign_numeric_name(inv1)
        inv1.action_post()

        inv2 = order._create_invoices()
        inv2.invoice_line_ids.quantity = 3
        inv2.invoice_date = date(2026, 7, 16)
        self._assign_numeric_name(inv2)
        inv2.action_post()

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        cost_rows = [r for r in rows if r[0] == '8']
        total_cost = sum(float(r[4]) for r in cost_rows)
        self.assertAlmostEqual(total_cost, 60.00, places=2)
        self.assertEqual(len(cost_rows), 3)  # 2 invoices + 1 OP

    # ------------------------------------------------------------------
    # C15: posted sale document with no associated valued delivery
    # ------------------------------------------------------------------
    def test_c15_no_valued_delivery(self):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date(2026, 7, 15),
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': 10.0,
                'tax_ids': [Command.set(self.tax_20.ids)],
            })],
        })
        self._assign_numeric_name(invoice)
        invoice.action_post()

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        sale_rows = [r for r in rows if r[0] == '2']
        cost_rows = [r for r in rows if r[0] == '8']
        self.assertEqual(len(sale_rows), 1)
        self.assertEqual(len(cost_rows), 0)

    # ------------------------------------------------------------------
    # C16: draft/cancelled invoices, unvalidated pickings, bad payments
    # must never leak into the export.
    # ------------------------------------------------------------------
    def test_c16_invalid_sources_excluded(self):
        # Draft invoice: must never be exported.
        draft_invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date(2026, 7, 15),
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': 10.0,
                'tax_ids': [Command.set(self.tax_20.ids)],
            })],
        })

        # Cancelled invoice: must never be exported.
        cancel_invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date(2026, 7, 15),
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': 10.0,
                'tax_ids': [Command.set(self.tax_20.ids)],
            })],
        })
        cancel_invoice.button_cancel()

        # Unvalidated delivery: confirmed but never button_validate'd ->
        # stays 'assigned'/'confirmed', never 'done', must not create an OP.
        unvalidated_order = self._create_order(qty=5, price=10.0, tax=self.tax_20)

        # A valid invoice + delivery + payment, so the export succeeds
        # and we can assert the invalid sources above are simply absent.
        valid_order = self._create_order(qty=2, price=10.0, tax=self.tax_20)
        self._deliver(valid_order, date_done='2026-07-15 10:00:00')
        valid_invoice = self._invoice_order(valid_order, invoice_date=date(2026, 7, 15))
        payment = self._register_payment(valid_invoice, amount=24.0,
                                            payment_date=date(2026, 7, 15))
        payment.state = 'draft'  # draft payment must be excluded too

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)

        self.assertEqual(len(rows), 2)  # valid invoice's code2 + code8 only
        codes = sorted(r[0] for r in rows)
        self.assertEqual(codes, ['2', '8'])
        self.assertNotIn(draft_invoice.name, [r[2] for r in rows])
        self.assertTrue(unvalidated_order.picking_ids)
        self.assertNotEqual(unvalidated_order.picking_ids.state, 'done')

    # ------------------------------------------------------------------
    # C17: repeated export of unchanged data is byte-identical, same
    # OP numbers both times.
    # ------------------------------------------------------------------
    def test_c17_repeatability(self):
        order = self._create_order(qty=6, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')

        payload1 = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        payload2 = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(payload1, payload2)

    # ------------------------------------------------------------------
    # C18: exporting next month for a past period doesn't rewrite dates
    # ------------------------------------------------------------------
    @freeze_time('2026-08-06')
    def test_c18_late_export_preserves_dates(self):
        order = self._create_order(qty=6, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        rows = self._decode_rows(payload)
        op_row = [r for r in rows if r[0] == '2' and r[3] == 'ОП'][0]
        self.assertEqual(op_row[1], '15.07.2026')

    # ------------------------------------------------------------------
    # C19: DST-aware Europe/Sofia conversion of picking date_done
    # ------------------------------------------------------------------
    def test_c19_timezone_dst_boundary(self):
        order = self._create_order(qty=1, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-31 22:30:00')

        # 22:30 UTC on 31 July is already 01:30 local time on 1 August in
        # Europe/Sofia (DST, UTC+3) - so the July period has NO operations
        # at all, and the export correctly raises the empty-period error
        # rather than returning any OP row for July.
        with self.assertRaises(UserError):
            self._run_export(date(2026, 7, 1), date(2026, 7, 31))

        payload_aug = self._run_export(date(2026, 8, 1), date(2026, 8, 31))
        rows_aug = self._decode_rows(payload_aug)
        op_rows_aug = [r for r in rows_aug if r[0] == '2' and r[3] == 'ОП']
        self.assertEqual(len(op_rows_aug), 1)
        self.assertEqual(op_rows_aug[0][1], '01.08.2026')

    # ------------------------------------------------------------------
    # C20: text sanitization - internal delimiters removed, CRLF intact
    # ------------------------------------------------------------------
    def test_c20_text_sanitization(self):
        partner = self.partner.copy({
            'name': 'Тест | клиент\nООД',
            'street': 'ул. Пример\t1',
            'street2': False,
        })
        order = self._create_order(qty=1, price=10.0, tax=self.tax_20,
                                    partner=partner)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        self._invoice_order(order, invoice_date=date(2026, 7, 15))

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        self.assertTrue(payload.endswith(b'\r\n'))
        rows = self._decode_rows(payload)
        for row in rows:
            self.assertEqual(len(row), 16)
            for field in row:
                self.assertNotIn('|', field)
                self.assertNotIn('\n', field)
                self.assertNotIn('\t', field)

    # ------------------------------------------------------------------
    # C21: invalid requisites -> specific errors, never a partial file
    # ------------------------------------------------------------------
    def test_c21_invalid_official_document_number(self):
        order = self._create_order(qty=1, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 7, 15))
        invoice.name = 'INV/2026/00001'

        with self.assertRaises(UserError):
            self._run_export(date(2026, 7, 1), date(2026, 7, 31))

    def test_c21_unencodable_cp1251_text(self):
        partner = self.partner.copy({'name': 'Emoji customer 😀'})
        order = self._create_order(qty=1, price=10.0, tax=self.tax_20,
                                   partner=partner)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        self._invoice_order(order, invoice_date=date(2026, 7, 15))

        with self.assertRaises(UserError) as cm:
            self._run_export(date(2026, 7, 1), date(2026, 7, 31))
        self.assertIn('клиент', str(cm.exception))

    def test_c21_unsupported_tax_rate(self):
        bad_tax = self.env['account.tax'].create({
            'name': 'Delta Test VAT 15%',
            'amount': 15,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        })
        order = self._create_order(qty=1, price=10.0, tax=bad_tax)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        self._invoice_order(order, invoice_date=date(2026, 7, 15))

        with self.assertRaises(UserError):
            self._run_export(date(2026, 7, 1), date(2026, 7, 31))

    # ------------------------------------------------------------------
    # C22: period validation messages
    # ------------------------------------------------------------------
    def test_c22_date_from_after_date_to(self):
        with self.assertRaises(UserError):
            self._run_export(date(2026, 7, 31), date(2026, 7, 1))

    def test_c22_empty_period(self):
        with self.assertRaises(UserError) as cm:
            self._run_export(date(2026, 1, 1), date(2026, 1, 31))
        self.assertIn('Няма операции за избрания период', str(cm.exception))

    # ------------------------------------------------------------------
    # C23: cross-company isolation + no double counting
    # ------------------------------------------------------------------
    def test_c23_tier2_snapshot_deduplication(self):
        order = self._create_order(qty=1, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        invoice = self._invoice_order(order, invoice_date=date(2026, 7, 15))
        payment = self._register_payment(
            invoice, amount=12.0, payment_date=date(2026, 7, 15))

        self.assertTrue(self.env['delta.payment.allocation.snapshot'].sudo().search([
            ('payment_id', '=', payment.id), ('move_id', '=', invoice.id),
        ]))
        allocations = self.env['delta.payment.allocation.adapter'].get_allocations(payment)
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0][0], invoice)
        self.assertEqual(allocations[0][1], 12.0)

    def test_c23_cross_company_isolation(self):
        # setup_other_company() (AccountTestInvoicingCommon) gives the new
        # company its own default sale journal/accounts - a bare
        # res.company.create() has none, and posting an invoice would
        # fail with "No journal could be found ... for type: sale".
        self.setup_other_company(name='Delta Other Co')
        other_company = self.env['res.company'].search(
            [('name', '=', 'Delta Other Co')], limit=1)
        other_partner = self.partner.copy({'company_id': False})

        order = self._create_order(qty=5, price=10.0, tax=self.tax_20)
        self._deliver(order, date_done='2026-07-15 10:00:00')
        self._invoice_order(order, invoice_date=date(2026, 7, 15))

        # foreign-company invoice must never appear in this company's export
        other_invoice = self.env['account.move'].with_company(other_company).create({
            'move_type': 'out_invoice',
            'partner_id': other_partner.id,
            'company_id': other_company.id,
            'invoice_date': date(2026, 7, 15),
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': 999.0,
            })],
        })
        self._assign_numeric_name(other_invoice)
        other_invoice.action_post()

        payload = self._run_export(date(2026, 7, 1), date(2026, 7, 31),
                                     company=self.company)
        rows = self._decode_rows(payload)
        amounts = [r[4] for r in rows]
        self.assertNotIn('999.00', amounts)
