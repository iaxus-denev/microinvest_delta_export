# -*- coding: utf-8 -*-
import base64
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

from ..models.delta_export_service import DeltaExportService

FILENAME = 'Import.txt'


class DeltaExportWizard(models.TransientModel):
    _name = 'delta.export.wizard'
    _description = 'Microinvest Delta - Export Import.txt'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    date_from = fields.Date(
        string='From', required=True,
        default=lambda self: date.today().replace(day=1),
    )
    date_to = fields.Date(
        string='To', required=True,
        default=lambda self: date.today(),
    )

    def _ensure_allowed_company(self):
        self.ensure_one()
        if self.company_id not in self.env.user.company_ids:
            raise UserError(_(
                "Нямате достъп до избраната компания %(company)s.",
                company=self.company_id.display_name,
            ))

    def action_generate(self):
        self.ensure_one()
        self._ensure_allowed_company()

        env = self.env(context=dict(self.env.context, allowed_company_ids=[self.company_id.id]))
        service = DeltaExportService(env, self.company_id, self.date_from, self.date_to)
        payload = service.generate()

        attachment = self.env['ir.attachment'].sudo().create({
            'name': FILENAME,
            'type': 'binary',
            'datas': base64.b64encode(payload),
            'mimetype': 'text/plain',
            'res_model': self._name,
            'res_id': self.id,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'self',
        }
