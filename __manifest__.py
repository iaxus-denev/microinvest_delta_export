# -*- coding: utf-8 -*-
{
    'name': "Microinvest Delta Export",
    'summary': "Export sales invoices, credit notes, cash OP sales, stock cost "
               "and cash payments to the Microinvest Delta Import.txt format",
    'description': """
Microinvest Delta TXT Export
=============================

Generates an ``Import.txt`` file (Windows-1251 / cp1251, pipe-separated,
16 fields, CRLF line endings) for import into Microinvest Delta, covering:

* Posted sales invoices and credit notes (``account.move``)
* Cash "OP" sales based on fulfilled, non-invoiced delivery quantities
* Stock cost of goods sold (from ``stock.move.value``, never recomputed)
* Cash customer payments, with exact payment-to-document allocation

See ``README.md`` for the full field mapping and the explicit, documented
decisions taken where the Microinvest Delta assignment specification left
a client-specific choice open (official document number source, "MOL"
field, bank/account text, note text, and the payment-allocation adapter).

This module intentionally does not keep any export history, status, or
configuration profile: every export is a fresh, deterministic, full
re-computation for the selected period.
""",
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'author': 'Iaxus EOOD',
    'website': 'https://github.com/iaxus-denev/microinvest_delta_export',
    'license': 'LGPL-3',
    'depends': [
        'account',
        'sale_stock',
        'stock_account',
    ],
    'data': [
        'security/delta_export_security.xml',
        'security/ir.model.access.csv',
        'data/delta_sequence.xml',
        'views/delta_export_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
