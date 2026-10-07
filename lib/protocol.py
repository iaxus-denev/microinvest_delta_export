# -*- coding: utf-8 -*-
"""Pure (Odoo-free) implementation of the Microinvest Delta ``Import.txt``
line/text protocol.

This module knows nothing about Odoo models. It receives plain Python
values (already resolved by ``models/delta_export_service.py``) and is
responsible only for:

* exact field formatting (dates, money, codes)
* strict CP1251 encodability checking (never silently replace with ``?``)
* sanitizing free text (``|``, CR, LF, tabs -> single space)
* building the final 16-field, ``|``-separated, CRLF-terminated line
* joining lines into the final ``bytes`` payload (CRLF after every line,
  including the last one, no BOM, no header)

Row building (gathering the right Odoo records, grouping by tax, etc.) is
NOT here - keeping this module pure makes it independently unit-testable
against the acceptance fixtures without a database.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, List, Sequence

FIELD_COUNT = 16
SEPARATOR = '|'
LINE_END = '\r\n'
ENCODING = 'cp1251'

OP_FA = 'Ф-ра'
OP_KI = 'КИ'
OP_OP = 'ОП'

DESC_INVOICE = 'Издадена фактура'
DESC_CREDIT_NOTE = 'Кредитно известие'
DESC_SALE = 'Продажба'
DESC_PAYMENT = 'Плащане в брой'

TAX_CODE_20 = '16'
TAX_CODE_9 = '20'
TAX_CODE_0 = '21'
TAX_CODE_COST_OR_PAYMENT = '8'

#: mapping of exact VAT percentage (as Decimal, e.g. Decimal('20')) to the
#: Delta sales-tax code. Only these three rates are supported by the
#: protocol; anything else must raise, never be approximated.
TAX_RATE_TO_CODE = {
    Decimal('20'): TAX_CODE_20,
    Decimal('9'): TAX_CODE_9,
    Decimal('0'): TAX_CODE_0,
}

#: control characters (other than the ones we explicitly handle) that must
#: never reach the output file. Tabs, CR, LF and the field separator are
#: replaced by a single space; everything else in this range is also
#: flattened to a space defensively.
_CONTROL_CHARS_RE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')
_WHITESPACE_RUN_RE = re.compile(r'[ \t]+')


class DeltaProtocolError(Exception):
    """Raised for any protocol-level error (encoding, formatting).

    Callers (``delta_export_service.py``) are expected to catch this,
    attach document context, and aggregate all such errors into a single
    ``UserError`` - this module itself never knows about Odoo documents.
    """


@dataclass(frozen=True)
class CharEncodingError(Exception):
    """Raised when a string contains a character that cannot be encoded
    as CP1251. Carries enough context for the caller to build a precise,
    document+field-specific error message.
    """
    text: str
    position: int
    character: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            "Символ '%s' (позиция %d) не може да се кодира в CP1251: %r"
            % (self.character, self.position, self.text)
        )


def check_cp1251_encodable(text: str) -> None:
    """Raise :class:`CharEncodingError` if ``text`` contains a character
    that CP1251 cannot represent. Never silently replaces or drops it.
    """
    try:
        text.encode(ENCODING, errors='strict')
    except UnicodeEncodeError as exc:
        raise CharEncodingError(
            text=text,
            position=exc.start,
            character=text[exc.start:exc.end],
        ) from exc


def sanitize_text(value: str | None) -> str:
    """Sanitize free text for safe inclusion inside a single Delta field.

    - ``None`` becomes an empty string.
    - the field separator ``|``, CR, LF, tabs, and other control
      characters are replaced by a single space (never dropped, so word
      boundaries are preserved).
    - runs of spaces/tabs are collapsed to one space.
    - leading/trailing whitespace is stripped.

    This does NOT perform CP1251 checking - call :func:`check_cp1251_encodable`
    separately once the final field value is known, so the caller can
    attach document/field context to the error.
    """
    if value is None:
        return ''
    text = value.replace(SEPARATOR, ' ')
    text = text.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    text = _CONTROL_CHARS_RE.sub(' ', text)
    text = _WHITESPACE_RUN_RE.sub(' ', text)
    return text.strip()


def format_date(value) -> str:
    """Format a ``date``/``datetime``-like object as ``dd.MM.yyyy``."""
    return value.strftime('%d.%m.%Y')


def format_document_number(number: str) -> str:
    """Zero-pad an already-validated purely-numeric document number string
    to exactly 10 digits. Validation of "purely numeric, <=10 digits" is
    the caller's responsibility (it needs document context to raise a
    good error); this function only pads.
    """
    return number.zfill(10)


def format_money(value) -> str:
    """Format a monetary amount with exactly 2 decimal digits, using
    ``ROUND_HALF_UP``, normalizing ``-0.00`` to ``0.00``.

    Accepts ``Decimal``, ``float`` or ``int``.
    """
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    quantized = dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if quantized == 0:
        # normalize -0.00 -> 0.00
        quantized = Decimal('0.00')
    return format(quantized, 'f')


def tax_code_for_rate(rate: Decimal) -> str:
    """Return the Delta sales-tax code (16/20/21) for an exact VAT rate.

    Raises :class:`DeltaProtocolError` for any rate other than 20, 9 or 0 -
    the protocol never approximates an unsupported tax rate.
    """
    code = TAX_RATE_TO_CODE.get(rate)
    if code is None:
        raise DeltaProtocolError(
            "Неподдържана данъчна ставка %s%% - очаква се 20%%, 9%% или 0%%"
            % (rate,)
        )
    return code


def build_line(fields: Sequence[str]) -> str:
    """Join exactly :data:`FIELD_COUNT` fields with ``|`` and append CRLF.

    Raises :class:`DeltaProtocolError` if the field count is wrong, or if
    any field value itself contains the separator or a line break (which
    would indicate a bug upstream - sanitize_text() should have already
    removed those).
    """
    if len(fields) != FIELD_COUNT:
        raise DeltaProtocolError(
            "Очакват се точно %d полета, получени %d" % (FIELD_COUNT, len(fields))
        )
    for idx, field in enumerate(fields, start=1):
        if SEPARATOR in field:
            raise DeltaProtocolError(
                "Поле %d съдържа разделителя '%s' след санитизация: %r"
                % (idx, SEPARATOR, field)
            )
        if '\r' in field or '\n' in field:
            raise DeltaProtocolError(
                "Поле %d съдържа прекъсване на ред след санитизация: %r"
                % (idx, field)
            )
    return SEPARATOR.join(fields) + LINE_END


def encode_payload(lines: Iterable[str]) -> bytes:
    """Join already-built lines (each already ending in CRLF) and encode
    the whole payload as CP1251, strictly (no BOM, no replacement).

    Raises :class:`CharEncodingError` with full context if any character
    across the whole payload is not representable - this is a last-resort
    safety net; per-field checking via :func:`check_cp1251_encodable`
    should normally catch the offending value earlier with better context.
    """
    payload = ''.join(lines)
    try:
        return payload.encode(ENCODING, errors='strict')
    except UnicodeEncodeError as exc:
        raise CharEncodingError(
            text=payload,
            position=exc.start,
            character=payload[exc.start:exc.end],
        ) from exc


def build_row(
    *,
    operation: str,
    date,
    document_number: str,
    doc_type: str,
    amount,
    tax_code: str,
    partner_name: str,
    mol: str,
    city: str,
    address: str,
    vat: str,
    company_registry: str,
    bank: str,
    description: str,
    note: str,
    real_vat,
) -> List[str]:
    """Assemble the 16 raw (already-sanitized/formatted) field values for a
    single row, as a list ready for :func:`build_line`.

    This is a thin, order-preserving helper matching the field table in
    the assignment §4; callers pass already-resolved, already-formatted
    strings for every field so this function stays pure and simple.
    """
    return [
        operation,
        format_date(date),
        format_document_number(document_number),
        doc_type,
        amount,
        tax_code,
        partner_name,
        mol,
        city,
        address,
        vat,
        company_registry,
        bank,
        description,
        note,
        real_vat,
    ]
