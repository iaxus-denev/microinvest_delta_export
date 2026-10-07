# -*- coding: utf-8 -*-
"""Orchestrates the Microinvest Delta ``Import.txt`` export.

Gathers rows from three Odoo-side sources - posted sales invoices/credit
notes, cash "OP" sales derived from fulfilled-but-not-invoiced delivery
quantities, and cash customer payments - converts each into the 16-field
protocol row via :mod:`delta_export_bundle.lib.protocol`, sorts them per
the assignment's deterministic sort key, and encodes the final CP1251
payload.

Design notes (see README.md for the full rationale):

* All monetary math is done with :class:`decimal.Decimal` to avoid binary
  float drift; quantities are converted via ``Decimal(str(x))`` from the
  float values the ORM returns.
* Cost (code 8) is *never* recomputed from ``standard_price`` or any
  other live costing field - only the already-computed, historical
  ``stock.move.value`` is read, exactly as validated in the Odoo 19
  ``stock_account`` source.
* A sale order line's delivered-but-not-yet-invoiced quantity (and its
  proportional share of each contributing move's cost) is determined by
  walking the line's *entire* invoicing history (regardless of period,
  per the assignment's explicit requirement) in chronological order
  against its entire delivery history, FIFO-style, so that no quantity
  or cost is ever counted twice across repeated exports.
* Every validation failure is collected with full document context and
  raised together as a single :class:`UserError` at the very end -
  never a partial file.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from odoo.exceptions import UserError
from odoo.tools import html2plaintext
from odoo.tools.translate import _

from ..lib import protocol

SOFIA_TZ = ZoneInfo('Europe/Sofia')
UTC_TZ = ZoneInfo('UTC')

OP_CODE_PRIORITY = {'2': 0, '8': 1, '10': 2}

SALE_MOVE_TYPES = ('out_invoice', 'out_refund')


def _dec(value) -> Decimal:
    """Convert a float/int/str/Decimal ORM value to :class:`Decimal`
    safely (via its string representation, to avoid binary float noise
    leaking into money/quantity math).
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _cround(currency, value: Decimal) -> Decimal:
    """Round a :class:`Decimal` monetary amount per ``currency``'s own
    rounding rules. ``res.currency.round()`` expects/returns a float, so
    the Decimal is converted there and back without losing precision
    beyond the currency's own rounding.
    """
    return _dec(currency.round(float(value)))


def _ccompare(currency, value1: Decimal, value2: Decimal) -> int:
    """Decimal-safe wrapper around ``res.currency.compare_amounts()``."""
    return currency.compare_amounts(float(value1), float(value2))


@dataclass
class _Row:
    """One not-yet-encoded protocol row plus its sort key."""
    sort_date: date_cls
    sort_doc_key: str
    sort_priority: int
    sort_sub_key: object
    fields: list = field(default_factory=list)

    def sort_key(self):
        return (self.sort_date, self.sort_doc_key, self.sort_priority, str(self.sort_sub_key))


class DeltaExportService:
    """Stateless-per-call export orchestrator.

    Usage::

        service = DeltaExportService(env, company, date_from, date_to)
        payload = service.generate()  # bytes, or raises UserError
    """

    def __init__(self, env, company, date_from, date_to):
        self.env = env
        self.company = company
        self.date_from = date_from
        self.date_to = date_to
        self.currency = company.currency_id
        self.errors = []  # list[str], collected across the whole run
        self._line_consumption_cache = {}
        self._invalid_multi_sale_invoice_lines = set()

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def generate(self) -> bytes:
        if self.date_from > self.date_to:
            raise UserError(_(
                "Начална дата (%(df)s) е след крайна дата (%(dt)s).",
                df=self.date_from, dt=self.date_to,
            ))

        rows: list[_Row] = []
        rows += self._collect_invoice_rows()
        rows += self._collect_op_rows()
        rows += self._collect_payment_rows()

        if self.errors:
            raise UserError(_(
                "Експортът не може да бъде генериран поради следните "
                "проблеми:\n\n%(issues)s",
                issues='\n'.join('- %s' % e for e in self.errors),
            ))

        if not rows:
            raise UserError(_("Няма операции за избрания период"))

        rows.sort(key=lambda r: r.sort_key())
        lines = [protocol.build_line(r.fields) for r in rows]
        return protocol.encode_payload(lines)

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------
    def _sanitized(self, value) -> str:
        return protocol.sanitize_text(value)

    def _checked_text(self, value, *, document, field_name) -> str | None:
        """Sanitize then CP1251-check a free-text field. On encoding
        failure, append a document+field-specific error and return
        ``None`` (caller must then skip emitting a row for this
        document).
        """
        text = self._sanitized(value)
        try:
            protocol.check_cp1251_encodable(text)
        except protocol.CharEncodingError as exc:
            self.errors.append(_(
                "Документ %(doc)s, поле '%(field)s': %(err)s",
                doc=document, field=field_name, err=str(exc),
            ))
            return None
        return text

    def _validated_document_number(self, move) -> str | None:
        """Return the zero-padded 10-digit official document number for
        ``move`` (sourced from ``account.move.name``, per README), or
        ``None`` (and an appended error) if ``move.name`` is not purely
        numeric or exceeds 10 digits. Never strips characters.
        """
        raw = (move.name or '').strip()
        if not raw or raw == '/' or not raw.isdigit() or len(raw) > 10:
            self.errors.append(_(
                "Документ %(doc)s (запис %(id)s): официалният номер "
                "'%(name)s' не е чисто цифров номер до 10 цифри - не може "
                "да се експортира без да се изкривят данните. Проверете "
                "номерацията на документа.",
                doc=move.display_name, id=move.id, name=raw,
            ))
            return None
        return protocol.format_document_number(raw)

    def _mol(self, partner, *, document, field_name='МОЛ') -> str | None:
        """Resolve the MOL (materially-responsible person, field 8) from
        the client-confirmed existing ``res.partner.x_itc_liable_person``
        field (per the project's consultant Q&A). Falls back to an empty
        string if that field doesn't exist on this database (e.g. a
        vanilla dev/test environment without the client's customization)
        or is unset - never guesses from the document's salesperson.
        """
        value = partner.x_itc_liable_person if 'x_itc_liable_person' in partner._fields else ''
        return self._checked_text(value or '', document=document, field_name=field_name)

    def _eik_and_vat(self, commercial_partner, *, document) -> tuple[str, str] | None:
        """Resolve VAT (field 11) and EIK/company_registry (field 12)
        from ``commercial_partner``. If ``company_registry`` is empty,
        fall back to the VAT number with a leading ``BG`` stripped, but
        only if the result is purely numeric (per the assignment's docx
        clarification) - never alters an existing EIK.
        """
        vat = commercial_partner.vat or ''
        eik = commercial_partner.company_registry or ''
        if not eik and vat:
            candidate = vat[2:] if vat[:2].upper() == 'BG' else vat
            if candidate.isdigit():
                eik = candidate
        vat = self._checked_text(vat, document=document, field_name='ДДС номер')
        eik = self._checked_text(eik, document=document, field_name='ЕИК')
        if vat is None or eik is None:
            return None
        return vat, eik

    # ------------------------------------------------------------------
    # 1. invoices / credit notes
    # ------------------------------------------------------------------
    def _collect_invoice_rows(self) -> list[_Row]:
        moves = self.env['account.move'].sudo().search([
            ('company_id', '=', self.company.id),
            ('move_type', 'in', SALE_MOVE_TYPES),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ], order='invoice_date, id')
        if not moves:
            return []

        # batch-prefetch to avoid O(N^2) queries
        moves.mapped('invoice_line_ids')
        moves.mapped('line_ids.tax_line_id')
        moves.mapped('partner_id')
        moves.mapped('commercial_partner_id')

        rows: list[_Row] = []
        for move in moves:
            rows += self._build_invoice_rows(move)
        return rows

    def _build_invoice_rows(self, move) -> list[_Row]:
        doc_number = self._validated_document_number(move)
        if doc_number is None:
            return []

        groups = self._resolve_invoice_tax_groups(move)
        if groups is None:
            return []  # error already recorded

        partner = move.partner_id
        commercial = move.commercial_partner_id
        identifiers = self._eik_and_vat(commercial, document=move.display_name)
        if identifiers is None:
            return []
        vat, eik = identifiers

        partner_name = self._checked_text(partner.name, document=move.display_name, field_name='клиент')
        mol = self._mol(partner, document=move.display_name)
        city = self._checked_text(partner.city, document=move.display_name, field_name='град')
        address = self._checked_text(
            ' '.join(filter(None, [partner.street, partner.street2])),
            document=move.display_name, field_name='адрес')
        note = self._checked_text(
            html2plaintext(move.narration or '') if move.narration else '',
            document=move.display_name, field_name='бележка')
        if None in (partner_name, mol, city, address, note):
            return []

        bank_value = move.partner_bank_id.acc_number if move.partner_bank_id else ''
        bank = self._checked_text(bank_value, document=move.display_name, field_name='банкова сметка') \
            if bank_value and bank_value.strip() else '   '
        if bank is None:
            return []

        is_credit_note = move.move_type == 'out_refund'
        op_code = protocol.OP_KI if is_credit_note else protocol.OP_FA
        description = protocol.DESC_CREDIT_NOTE if is_credit_note else protocol.DESC_INVOICE

        rows: list[_Row] = []
        for tax_code, gross, vat_amount in groups:
            fields_list = protocol.build_row(
                operation='2',
                date=move.invoice_date,
                document_number=doc_number,
                doc_type=op_code,
                amount=protocol.format_money(gross),
                tax_code=tax_code,
                partner_name=partner_name,
                mol=mol,
                city=city,
                address=address,
                vat=vat,
                company_registry=eik,
                bank=bank,
                description=description,
                note=note or ' ',
                real_vat=protocol.format_money(vat_amount),
            )
            rows.append(_Row(
                sort_date=move.invoice_date,
                sort_doc_key=doc_number,
                sort_priority=OP_CODE_PRIORITY['2'],
                sort_sub_key=tax_code,
                fields=fields_list,
            ))

        if not is_credit_note:
            cost = self._invoice_cost(move)
            if cost is not None:
                fields_list = protocol.build_row(
                    operation='8',
                    date=move.invoice_date,
                    document_number=doc_number,
                    doc_type=op_code,
                    amount=protocol.format_money(cost),
                    tax_code=protocol.TAX_CODE_COST_OR_PAYMENT,
                    partner_name='',
                    mol='',
                    city=city,
                    address='',
                    vat='',
                    company_registry='',
                    bank='   ',
                    description=description,
                    note=' ',
                    real_vat='0',
                )
                rows.append(_Row(
                    sort_date=move.invoice_date,
                    sort_doc_key=doc_number,
                    sort_priority=OP_CODE_PRIORITY['8'],
                    sort_sub_key=protocol.TAX_CODE_COST_OR_PAYMENT,
                    fields=fields_list,
                ))

        return rows

    def _resolve_invoice_tax_groups(self, move):
        """Group ``move``'s product lines by resolved Delta tax code,
        using the already-posted, already-tax-engine-computed line
        balances (company currency) - never approximated.

        Returns a list of ``(tax_code, gross_amount, vat_amount)``
        Decimal tuples, or ``None`` if a validation error was recorded.
        """
        product_lines = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        if not product_lines:
            self.errors.append(_(
                "Документ %(doc)s няма редове с продукти за експорт.",
                doc=move.display_name,
            ))
            return None

        by_tax = defaultdict(lambda: Decimal('0'))
        tax_of_code = {}
        for line in product_lines:
            taxes = line.tax_ids
            if len(taxes) != 1:
                self.errors.append(_(
                    "Документ %(doc)s, ред '%(line)s': редът трябва да "
                    "има точно един данък (намерени: %(n)s).",
                    doc=move.display_name, line=line.name or line.id, n=len(taxes),
                ))
                return None
            tax = taxes[0]
            if tax.amount_type != 'percent':
                self.errors.append(_(
                    "Документ %(doc)s, ред '%(line)s': поддържат се само "
                    "процентни данъци (тип на '%(tax)s' е '%(type)s').",
                    doc=move.display_name, line=line.name or line.id,
                    tax=tax.name, type=tax.amount_type,
                ))
                return None
            try:
                tax_code = protocol.tax_code_for_rate(_dec(tax.amount))
            except protocol.DeltaProtocolError as exc:
                self.errors.append(_(
                    "Документ %(doc)s, ред '%(line)s': %(err)s",
                    doc=move.display_name, line=line.name or line.id, err=str(exc),
                ))
                return None
            by_tax[tax_code] += _dec(line.balance)
            tax_of_code[tax_code] = tax

        groups = []
        total_gross = Decimal('0')
        total_vat = Decimal('0')
        for tax_code, base_balance_sum in by_tax.items():
            tax = tax_of_code[tax_code]
            tax_lines = move.line_ids.filtered(
                lambda l: l.display_type == 'tax' and l.tax_line_id == tax)
            tax_balance_sum = sum(_dec(l.balance) for l in tax_lines) or Decimal('0')
            gross = _cround(self.currency, -(base_balance_sum + tax_balance_sum))
            vat_amount = _cround(self.currency, -tax_balance_sum)
            groups.append((tax_code, gross, vat_amount))
            total_gross += gross
            total_vat += vat_amount

        expected_total = _dec(move.amount_total_signed)
        expected_vat = _dec(move.amount_tax_signed)
        if _ccompare(self.currency, total_gross, expected_total) != 0 or \
                _ccompare(self.currency, total_vat, expected_vat) != 0:
            self.errors.append(_(
                "Документ %(doc)s: сумата по данъчни групи (%(g)s, ДДС "
                "%(v)s) не съвпада с общите суми на документа (%(tg)s, "
                "ДДС %(tv)s).",
                doc=move.display_name, g=total_gross, v=total_vat,
                tg=expected_total, tv=expected_vat,
            ))
            return None

        return groups

    # ------------------------------------------------------------------
    # cost allocation shared machinery (invoices + OP)
    # ------------------------------------------------------------------
    def _line_delivery_moves(self, order_line):
        """Done, outgoing, non-dropshipped stock moves for ``order_line``,
        chronologically ordered by picking completion then move id.
        """
        outgoing, _incoming = order_line._get_outgoing_incoming_moves(strict=True)
        moves = outgoing.filtered(lambda m: m.state == 'done')
        return moves.sorted(key=lambda m: (
            m.picking_id.date_done or m.date, m.picking_id.id, m.id))

    def _line_consumption(self, order_line):
        """Walk ``order_line``'s entire delivery history (all done
        outgoing moves) against its entire *invoice* history (all
        ``out_invoice`` lines, any period - never just the export
        period, so cost/quantity are never reused across exports),
        FIFO, splitting each move's ``value`` exactly between its
        consumers. The final consumer of a given move always receives
        the exact residual (``move.value`` minus whatever was already
        assigned to earlier consumers of that same move), so the sum
        of all consumers' shares for a move equals ``move.value``
        exactly, with no lost cent.

        Returns ``(invoice_cost: dict[move_id -> Decimal], op_leftover:
        list[dict(picking, qty, cost)])`` where ``move_id`` keys are
        ``account.move`` (invoice) records.
        """
        cached = self._line_consumption_cache.get(order_line.id)
        if cached is not None:
            return cached

        moves = self._line_delivery_moves(order_line)
        queue = []
        for move in moves:
            qty = _dec(move._get_valued_qty())
            if qty <= 0:
                continue
            queue.append({
                'move': move,
                'original_qty': qty,
                'remaining_qty': qty,
                'assigned': Decimal('0'),
            })

        invoice_lines = self.env['account.move.line'].sudo().search([
            ('sale_line_ids', 'in', order_line.id),
            ('move_id.state', '=', 'posted'),
            ('move_id.move_type', '=', 'out_invoice'),
            ('display_type', '=', 'product'),
        ], order='move_id, id')
        invoice_lines = invoice_lines.sorted(key=lambda l: (l.move_id.invoice_date, l.move_id.id, l.id))

        invoice_cost = defaultdict(lambda: Decimal('0'))
        idx = 0
        for inv_line in invoice_lines:
            # A single invoice line spanning multiple sale lines has no
            # trustworthy per-line quantity split in core Odoo. Refuse it
            # rather than consuming its full quantity once for every link.
            if len(inv_line.sale_line_ids) != 1:
                if inv_line.id not in self._invalid_multi_sale_invoice_lines:
                    self.errors.append(_(
                        "Фактурен ред '%(line)s' (документ %(doc)s) е "
                        "свързан с %(count)s реда от поръчка. Точното "
                        "разпределение на количеството и себестойността не "
                        "може да се определи без изричен клиентски адаптер.",
                        line=inv_line.name or inv_line.id,
                        doc=inv_line.move_id.display_name,
                        count=len(inv_line.sale_line_ids),
                    ))
                    self._invalid_multi_sale_invoice_lines.add(inv_line.id)
                continue
            # Keep outstanding quantity in the sale-line UoM. Each stock
            # move converts it into its own UoM before cost is allocated.
            qty_needed = _dec(inv_line.product_uom_id._compute_quantity(
                inv_line.quantity, order_line.product_uom_id,
                rounding_method='HALF-UP'))
            while qty_needed > 0 and idx < len(queue):
                entry = queue[idx]
                if entry['remaining_qty'] <= Decimal('0.000001'):
                    idx += 1
                    continue
                available_sale_qty = _dec(entry['move'].product_uom._compute_quantity(
                    float(entry['remaining_qty']), order_line.product_uom_id,
                    rounding_method='HALF-UP'))
                take_sale_qty = min(qty_needed, available_sale_qty)
                take = _dec(order_line.product_uom_id._compute_quantity(
                    float(take_sale_qty), entry['move'].product_uom,
                    rounding_method='HALF-UP'))
                is_final_slice = (take >= entry['remaining_qty'] - Decimal('0.000001'))
                if is_final_slice:
                    slice_cost = _dec(entry['move'].value) - entry['assigned']
                else:
                    slice_cost = _dec(entry['move'].value) * take / entry['original_qty']
                invoice_cost[inv_line.move_id] += slice_cost
                entry['remaining_qty'] -= take
                entry['assigned'] += slice_cost
                qty_needed -= take_sale_qty
                if entry['remaining_qty'] <= Decimal('0.000001'):
                    idx += 1

        op_leftover = []
        for entry in queue:
            if entry['remaining_qty'] <= Decimal('0.000001'):
                continue
            residual_cost = _dec(entry['move'].value) - entry['assigned']
            op_leftover.append({
                'picking': entry['move'].picking_id,
                'qty': entry['remaining_qty'],
                'uom': entry['move'].product_uom,
                'cost': residual_cost,
            })
        result = (invoice_cost, op_leftover)
        self._line_consumption_cache[order_line.id] = result
        return result

    def _invoice_cost(self, move):
        """Total code-8 cost for ``move`` (an ``out_invoice``), summed
        across every sale order line it invoices, via
        :meth:`_line_consumption`. Returns ``None`` (no row) if the
        invoice has no associated valued outgoing move at all -
        distinguished from an actually-zero cost by the absence of any
        contributing entry.
        """
        total = Decimal('0')
        found_any = False
        order_lines = move.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product').mapped('sale_line_ids')
        for order_line in order_lines:
            invoice_cost, _leftover = self._line_consumption(order_line)
            if move in invoice_cost:
                total += invoice_cost[move]
                found_any = True
        if not found_any:
            return None
        return _cround(self.currency, total)

    # ------------------------------------------------------------------
    # 2. OP (cash sale from delivery)
    # ------------------------------------------------------------------
    def _collect_op_rows(self) -> list[_Row]:
        utc_from, utc_to = self._sofia_period_to_utc_bounds()
        pickings = self.env['stock.picking'].sudo().search([
            ('company_id', '=', self.company.id),
            ('state', '=', 'done'),
            ('picking_type_id.code', '=', 'outgoing'),
            ('date_done', '>=', utc_from),
            ('date_done', '<', utc_to),
        ], order='date_done, id')
        if not pickings:
            return []

        orders = pickings.mapped('sale_id').filtered(lambda o: o)
        if not orders:
            return []

        rows: list[_Row] = []
        for order in orders.sorted(key=lambda order: order.id):
            rows += self._build_op_rows_for_order(order, pickings)
        return rows

    def _sofia_period_to_utc_bounds(self):
        start_local = datetime.combine(self.date_from, time.min, tzinfo=SOFIA_TZ)
        end_local = datetime.combine(self.date_to + timedelta(days=1), time.min, tzinfo=SOFIA_TZ)
        return (
            start_local.astimezone(UTC_TZ).replace(tzinfo=None),
            end_local.astimezone(UTC_TZ).replace(tzinfo=None),
        )

    def _op_date(self, picking) -> date_cls:
        dt_utc = picking.date_done.replace(tzinfo=UTC_TZ)
        return dt_utc.astimezone(SOFIA_TZ).date()

    def _build_op_rows_for_order(self, order, period_pickings) -> list[_Row]:
        # picking -> sales groups, cost, and whether a valued move exists.
        per_picking = defaultdict(lambda: {
            'tax_groups': defaultdict(lambda: [Decimal('0'), Decimal('0')]),
            'cost': Decimal('0'),
            'has_cost_move': False,
        })

        for order_line in order.order_line.filtered(lambda line: not line.display_type).sorted(key=lambda line: line.id):
            _invoice_cost, leftover = self._line_consumption(order_line)
            for entry in leftover:
                picking = entry['picking']
                if picking not in period_pickings:
                    continue
                qty = _dec(entry['uom']._compute_quantity(
                    float(entry['qty']), order_line.product_uom_id,
                    rounding_method='HALF-UP',
                ))
                cost = entry['cost']
                taxes = order_line.tax_ids
                if len(taxes) != 1 or taxes[0].amount_type != 'percent':
                    self.errors.append(_(
                        "Поръчка %(order)s, ред '%(line)s': ОП изисква "
                        "точно един процентен данък на реда.",
                        order=order.display_name, line=order_line.name or order_line.id,
                    ))
                    continue
                try:
                    tax_code = protocol.tax_code_for_rate(_dec(taxes[0].amount))
                except protocol.DeltaProtocolError as exc:
                    self.errors.append(_(
                        "Поръчка %(order)s, ред '%(line)s': %(err)s",
                        order=order.display_name, line=order_line.name or order_line.id, err=str(exc),
                    ))
                    continue

                ratio = qty / _dec(order_line.product_uom_qty) if order_line.product_uom_qty else Decimal('0')
                base = _dec(order_line.price_subtotal) * ratio
                vat_amount = _dec(order_line.price_tax) * ratio
                if order.currency_id != self.currency:
                    op_date = self._op_date(picking)
                    base = _dec(order.currency_id._convert(float(base), self.currency, self.company, op_date))
                    vat_amount = _dec(order.currency_id._convert(float(vat_amount), self.currency, self.company, op_date))

                bucket = per_picking[picking]
                bucket['tax_groups'][tax_code][0] += base
                bucket['tax_groups'][tax_code][1] += vat_amount
                bucket['cost'] += cost
                bucket['has_cost_move'] = True

        rows: list[_Row] = []
        for picking, data in sorted(
                per_picking.items(), key=lambda item: (item[0].date_done, item[0].id)):
            if not any(amounts[0] or amounts[1] for amounts in data['tax_groups'].values()) and \
                    not data['has_cost_move']:
                continue
            rows += self._build_op_rows(order, picking, data)
        return rows

    def _build_op_rows(self, order, picking, data) -> list[_Row]:
        op_date = self._op_date(picking)
        op_ref = self.env['delta.op.reference'].sudo()._get_or_create(
            self.company, order, picking, op_date)
        doc_number = op_ref.document_number

        partner = order.partner_invoice_id
        commercial = partner.commercial_partner_id
        identifiers = self._eik_and_vat(commercial, document=order.display_name)
        if identifiers is None:
            return []
        vat, eik = identifiers

        partner_name = self._checked_text(partner.name, document=order.display_name, field_name='клиент')
        mol = self._mol(partner, document=order.display_name)
        city = self._checked_text(partner.city, document=order.display_name, field_name='град')
        address = self._checked_text(
            ' '.join(filter(None, [partner.street, partner.street2])),
            document=order.display_name, field_name='адрес')
        if None in (partner_name, mol, city, address):
            return []

        rows: list[_Row] = []
        for tax_code, (base, vat_amount) in data['tax_groups'].items():
            gross = _cround(self.currency, base + vat_amount)
            vat_rounded = _cround(self.currency, vat_amount)
            if self.currency.is_zero(float(gross)) and self.currency.is_zero(float(vat_rounded)):
                continue
            fields_list = protocol.build_row(
                operation='2',
                date=op_date,
                document_number=doc_number,
                doc_type=protocol.OP_OP,
                amount=protocol.format_money(gross),
                tax_code=tax_code,
                partner_name=partner_name,
                mol=mol,
                city=city,
                address=address,
                vat=vat,
                company_registry=eik,
                bank='   ',
                description=protocol.DESC_SALE,
                note=' ',
                real_vat=protocol.format_money(vat_rounded),
            )
            rows.append(_Row(
                sort_date=op_date, sort_doc_key=doc_number,
                sort_priority=OP_CODE_PRIORITY['2'], sort_sub_key=tax_code,
                fields=fields_list,
            ))

        cost = _cround(self.currency, data['cost'])
        if data['has_cost_move']:
            fields_list = protocol.build_row(
                operation='8',
                date=op_date,
                document_number=doc_number,
                doc_type=protocol.OP_OP,
                amount=protocol.format_money(cost),
                tax_code=protocol.TAX_CODE_COST_OR_PAYMENT,
                partner_name='', mol='', city=city, address='',
                vat='', company_registry='', bank='   ',
                description=protocol.DESC_SALE,
                note=' ', real_vat='0',
            )
            rows.append(_Row(
                sort_date=op_date, sort_doc_key=doc_number,
                sort_priority=OP_CODE_PRIORITY['8'],
                sort_sub_key=protocol.TAX_CODE_COST_OR_PAYMENT,
                fields=fields_list,
            ))
        return rows

    # ------------------------------------------------------------------
    # 3. cash payments
    # ------------------------------------------------------------------
    def _collect_payment_rows(self) -> list[_Row]:
        payments = self.env['account.payment'].sudo().search([
            ('company_id', '=', self.company.id),
            ('partner_type', '=', 'customer'),
            ('journal_id.type', '=', 'cash'),
            ('state', 'in', ('in_process', 'paid')),
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ])
        if not payments:
            return []

        adapter = self.env['delta.payment.allocation.adapter'].sudo()
        rows: list[_Row] = []
        for payment in payments:
            try:
                allocations = adapter.get_allocations(payment)
            except UserError as exc:
                self.errors.append(str(exc))
                continue
            for target, amount in allocations:
                if target._name == 'delta.op.reference':
                    rows += self._build_op_payment_row(payment, target, amount)
                else:
                    rows += self._build_payment_row(payment, target, amount)
        return rows

    def _build_payment_row(self, payment, move, amount) -> list[_Row]:
        doc_number = self._validated_document_number(move)
        if doc_number is None:
            return []

        partner = move.partner_id
        commercial = move.commercial_partner_id
        identifiers = self._eik_and_vat(commercial, document=move.display_name)
        if identifiers is None:
            return []
        vat, eik = identifiers

        partner_name = self._checked_text(partner.name, document=move.display_name, field_name='клиент (плащане)')
        mol = self._mol(partner, document=move.display_name, field_name='МОЛ (плащане)')
        city = self._checked_text(partner.city, document=move.display_name, field_name='град (плащане)')
        address = self._checked_text(
            ' '.join(filter(None, [partner.street, partner.street2])),
            document=move.display_name, field_name='адрес (плащане)')
        if None in (partner_name, mol, city, address):
            return []

        op_code = protocol.OP_KI if move.move_type == 'out_refund' else protocol.OP_FA

        fields_list = protocol.build_row(
            operation='10',
            date=payment.date,
            document_number=doc_number,
            doc_type=op_code,
            amount=protocol.format_money(amount),
            tax_code=protocol.TAX_CODE_COST_OR_PAYMENT,
            partner_name=partner_name,
            mol=mol,
            city=city,
            address=address,
            vat=vat,
            company_registry=eik,
            bank='   ',
            description=protocol.DESC_PAYMENT,
            note=' ',
            real_vat='0.00',
        )
        return [_Row(
            sort_date=payment.date, sort_doc_key=doc_number,
            sort_priority=OP_CODE_PRIORITY['10'], sort_sub_key=payment.id,
            fields=fields_list,
        )]

    def _build_op_payment_row(self, payment, op_ref, amount) -> list[_Row]:
        """Build code-10 for an explicitly linked persistent OP reference."""
        order = op_ref.sale_order_id
        partner = order.partner_invoice_id
        identifiers = self._eik_and_vat(
            partner.commercial_partner_id, document=order.display_name)
        if identifiers is None:
            return []
        vat, eik = identifiers
        partner_name = self._checked_text(
            partner.name, document=order.display_name, field_name='клиент (ОП плащане)')
        mol = self._mol(partner, document=order.display_name, field_name='МОЛ (ОП плащане)')
        city = self._checked_text(
            partner.city, document=order.display_name, field_name='град (ОП плащане)')
        address = self._checked_text(
            ' '.join(filter(None, [partner.street, partner.street2])),
            document=order.display_name, field_name='адрес (ОП плащане)')
        if None in (partner_name, mol, city, address):
            return []
        fields_list = protocol.build_row(
            operation='10', date=payment.date,
            document_number=op_ref.document_number, doc_type=protocol.OP_OP,
            amount=protocol.format_money(amount),
            tax_code=protocol.TAX_CODE_COST_OR_PAYMENT,
            partner_name=partner_name, mol=mol, city=city, address=address,
            vat=vat, company_registry=eik, bank='   ',
            description=protocol.DESC_PAYMENT, note=' ', real_vat='0.00',
        )
        return [_Row(
            sort_date=payment.date, sort_doc_key=op_ref.document_number,
            sort_priority=OP_CODE_PRIORITY['10'], sort_sub_key=payment.id,
            fields=fields_list,
        )]
