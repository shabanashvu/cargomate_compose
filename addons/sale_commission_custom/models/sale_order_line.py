from odoo import fields, exceptions, models, api, _
from datetime import timedelta
from odoo.exceptions import AccessError, UserError
from itertools import groupby


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    agent = fields.Many2one(
        comodel_name="res.partner",
        domain="[('agent', '=', True)]",
        ondelete="restrict"
    )
    commission_line = fields.Many2one(
        comodel_name="sale.commission",
        ondelete="restrict",
        related="agent.commission",
        invisible="True",
        store="True"
    )
    agent_amount = fields.Monetary(
        string="Commission",
        compute="_compute_agent_amount",
        store=True
    )

    def _get_commission_amount(self, commission, subtotal, product, quantity):
        """Get the commission amount for the data given. To be called by
        compute methods of children models.
        """
        self.ensure_one()
        if product.commission_free or not commission:
            return 0.0
        if commission.amount_base_type == 'net_amount':
            # If subtotal (sale_price * quantity) is less than
            # standard_price * quantity, it means that we are selling at
            # lower price than we bought, so set amount_base to 0
            subtotal = max([
                0, subtotal - product.standard_price * quantity,
            ])
        if commission.commission_type == 'fixed':
            return subtotal * (commission.fix_qty / 100.0)
        elif commission.commission_type == 'section':
            return commission.calculate_section(subtotal)

    @api.depends('product_uom_qty', 'discount', 'price_unit', 'tax_id', 'agent')
    def _compute_agent_amount(self):
        """
        Compute the agent amounts of the SO line.
        """
        for line in self:
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            taxes = line.tax_id.compute_all(price, line.order_id.currency_id, line.product_uom_qty,
                                            product=line.product_id, partner=line.order_id.partner_shipping_id)
            line.agent_amount = line._get_commission_amount(
                        line.agent.commission, taxes['total_excluded'],
                        line.product_id, line.product_uom_qty,
                        )

    @api.onchange('agent')
    def onchange_agent(self):
        self.commission_line = self.agent.commission

    def _prepare_invoice_line(self, **optional_values):
        self.ensure_one()
        result = super(SaleOrderLine, self)._prepare_invoice_line(agent=self.agent.id)
        # result.update({'agent': self.agent.id})
        return result


class SaleOrderMain(models.Model):
    _inherit = "sale.order"

    @api.depends('order_line.agent_amount')
    def _compute_commission_total(self):
        for record in self:
            record.commission_total = 0.0
            for line in record.order_line:
                record.commission_total += line.agent_amount
                
    def create_invoice_new(self,grouped=False):
        context = self._context.copy()
        res = self.with_context(context)._create_invoices()
        return res
    
    def _create_invoices(self, grouped=False, final=False, date=None):
        """
        Create the invoice associated to the SO.
        :param grouped: if True, invoices are grouped by SO id. If False, invoices are grouped by
                        (partner_invoice_id, currency)
        :param final: if True, refunds will be generated if necessary
        :returns: list of created invoices
        """
        if not self.env['account.move'].check_access_rights('create', False):
            try:
                self.check_access_rights('write')
                self.check_access_rule('write')
            except AccessError:
                return self.env['account.move']

        # 1) Create invoices.
        invoice_vals_list = []
        invoice_item_sequence = 0 # Incremental sequencing to keep the lines order on the invoice.
        for order in self:
            order = order.with_company(order.company_id)
            current_section_vals = None
            down_payments = order.env['sale.order.line']

            invoice_vals = order._prepare_invoice()
            invoiceable_lines = order._get_invoiceable_lines(final)

            if not any(not line.display_type for line in invoiceable_lines):
                continue

            invoice_line_vals = []
            down_payment_section_added = False
            for line in invoiceable_lines:
                if not down_payment_section_added and line.is_downpayment:
                    # Create a dedicated section for the down payments
                    # (put at the end of the invoiceable_lines)
                    invoice_line_vals.append(
                        (0, 0, order._prepare_down_payment_section_line(
                            sequence=invoice_item_sequence,
                        )),
                    )
                    down_payment_section_added = True
                    invoice_item_sequence += 1
                invoice_line_vals.append(
                    (0, 0, line._prepare_invoice_line(
                        sequence=invoice_item_sequence,
                    )),
                )
                invoice_item_sequence += 1

            invoice_vals['invoice_line_ids'] += invoice_line_vals
            invoice_vals_list.append(invoice_vals)

        if not invoice_vals_list:
            raise self._nothing_to_invoice_error()

        # 2) Manage 'grouped' parameter: group by (partner_id, currency_id).
        if not grouped:
            new_invoice_vals_list = []
            invoice_grouping_keys = self._get_invoice_grouping_keys()
            invoice_vals_list = sorted(
                invoice_vals_list,
                key=lambda x: [
                    x.get(grouping_key) for grouping_key in invoice_grouping_keys
                ]
            )
            for grouping_keys, invoices in groupby(invoice_vals_list, key=lambda x: [x.get(grouping_key) for grouping_key in invoice_grouping_keys]):
                origins = set()
                payment_refs = set()
                refs = set()
                ref_invoice_vals = None
                for invoice_vals in invoices:
                    if not ref_invoice_vals:
                        ref_invoice_vals = invoice_vals
                    else:
                        ref_invoice_vals['invoice_line_ids'] += invoice_vals['invoice_line_ids']
                    origins.add(invoice_vals['invoice_origin'])
                    payment_refs.add(invoice_vals['payment_reference'])
                    refs.add(invoice_vals['ref'])
                ref_invoice_vals.update({
                    'ref': ', '.join(refs)[:2000],
                    'invoice_origin': ', '.join(origins),
                    'payment_reference': len(payment_refs) == 1 and payment_refs.pop() or False,
                })
                new_invoice_vals_list.append(ref_invoice_vals)
            invoice_vals_list = new_invoice_vals_list

        # 3) Create invoices.

        # As part of the invoice creation, we make sure the sequence of multiple SO do not interfere
        # in a single invoice. Example:
        # SO 1:
        # - Section A (sequence: 10)
        # - Product A (sequence: 11)
        # SO 2:
        # - Section B (sequence: 10)
        # - Product B (sequence: 11)
        #
        # If SO 1 & 2 are grouped in the same invoice, the result will be:
        # - Section A (sequence: 10)
        # - Section B (sequence: 10)
        # - Product A (sequence: 11)
        # - Product B (sequence: 11)
        #
        # Resequencing should be safe, however we resequence only if there are less invoices than
        # orders, meaning a grouping might have been done. This could also mean that only a part
        # of the selected SO are invoiceable, but resequencing in this case shouldn't be an issue.
        if len(invoice_vals_list) < len(self):
            SaleOrderLine = self.env['sale.order.line']
            for invoice in invoice_vals_list:
                sequence = 1
                for line in invoice['invoice_line_ids']:
                    line[2]['sequence'] = SaleOrderLine._get_invoice_line_sequence(new=sequence, old=line[2]['sequence'])
                    sequence += 1

        # Manage the creation of invoices in sudo because a salesperson must be able to generate an invoice from a
        # sale order without "billing" access rights. However, he should not be able to create an invoice from scratch.
        moves = self.env['account.move'].sudo().with_context(default_move_type='out_invoice', check_move_validity=False).create(invoice_vals_list)
        # 4) Some moves might actually be refunds: convert them if the total amount is negative
        # We do this after the moves have been created since we need taxes, etc. to know if the total
        # is actually negative or not
        if final:
            moves.sudo().filtered(lambda m: m.amount_total < 0).action_switch_invoice_into_refund_credit_note()
        for move in moves:
            move.message_post_with_view('mail.message_origin_link',
                values={'self': move, 'origin': move.line_ids.mapped('sale_line_ids.order_id')},
                subtype_id=self.env.ref('mail.mt_note').id
            )
        return moves


class SaleCommissionSettlementInherit(models.Model):
    _inherit = 'sale.commission.settlement'

    @api.model
    def create(self, vals):
        if vals.get('name', 'New') == 'New':
            vals['name'] = self.env['ir.sequence'].next_by_code('commission.settlement') or 'New'
        result = super(SaleCommissionSettlementInherit, self).create(vals)
        return result

    customer_invoice_id = fields.Many2one('account.move', string='Related Customer Invoice', readonly=True)
    name = fields.Char(string='Settlement Number', readonly=True, required=True, copy=False, default='New')

    def action_cancel(self):
        for x in self:
            if x.state == 'invoiced':
                if x.invoice.state not in ('draft', 'cancel'):
                    raise exceptions.Warning(_('Cannot cancel a settlement having validated Vendor Bill.'))
                else:
                    x.write({'state': 'cancel'})
            else:
                x.write({'state': 'cancel'})

    def action_invoice(self):
        for settlement in self:
            if settlement.state == 'settled':
                bill_obj = self.env['account.move']
                bill_lines_obj = self.env['account.move.line']
                lines = settlement.lines
                if lines:
                    vendor_bill = bill_obj.create({
                        'partner_id': settlement.agent.id,
                        'move_type': 'in_invoice',
                        'company_id': settlement.company_id.id,
                        'invoice_origin': settlement.name,
                    })
                    for line in lines:
                        inv_line = bill_lines_obj.new({
                            'product_id': line.invoice_line.product_id.id,
                            'quantity': 1,
                            'price_unit': line.settled_amount,
                            'move_id': vendor_bill.id,
                        })
                        inv_line._onchange_product_id()
                        invoice_line_vals = inv_line._convert_to_write(inv_line._cache)
                        bill_lines_obj.create(invoice_line_vals)
                    settlement.update({
                        'state': 'invoiced',
                        'invoice': vendor_bill.id,
                    })
                else:
                    raise UserError(_('No Settlement lines available to be invoiced'))
            else:
                raise UserError(_('Settlement is not in "Settled" state'))


    #
    #
    #
    #
    # def _default_journal(self):
    #     return self.env['account.journal'].search(
    #         [('type', '=', 'purchase')])[:1]
    #
    # def _default_refund_journal(self):
    #     return self.env['account.journal'].search(
    #         [('type', '=', 'purchase_refund')])[:1]
    #
    # def _default_settlements(self):
    #     return self.env.context.get('settlement_ids', [])
    #
    # def _default_from_settlement(self):
    #     return bool(self.env.context.get('settlement_ids'))
    #
    # journal = fields.Many2one(
    #     comodel_name='account.journal', required=True,
    #     domain="[('type', '=', 'purchase')]",
    #     default=_default_journal)
    # company_id = fields.Many2one(
    #     comodel_name='res.company',
    #     related='journal.company_id',
    #     readonly=True
    # )
    # product = fields.Many2one(
    #     string='Product for invoicing',
    #     comodel_name='product.product')
    # settlements = fields.Many2many(
    #     comodel_name='sale.commission.settlement',
    #     relation="sale_commission_make_invoice_settlement_rel",
    #     column1='wizard_id', column2='settlement_id',
    #     domain="[('state', '=', 'settled'),('agent_type', '=', 'agent'),"
    #            "('company_id', '=', company_id)]",
    #     default=_default_settlements)
    # from_settlement = fields.Boolean(default=_default_from_settlement)
    # date = fields.Date()
    #
    # @api.multi
    # def button_create(self):
    #     self.ensure_one()
    #     if not self.settlements:
    #         self.settlements = self.env['sale.commission.settlement'].search([
    #             ('state', '=', 'settled'),
    #             ('agent_type', '=', 'agent'),
    #             ('company_id', '=', self.journal.company_id.id)
    #         ])
    #     self.settlements.make_invoices(
    #         self.journal, self.product, date=self.date)
    #     # go to results
    #     if len(self.settlements):
    #         return {
    #             'name': _('Created Invoices'),
    #             'type': 'ir.actions.act_window',
    #             'views': [[False, 'list'], [False, 'form']],
    #             'res_model': 'account.invoice',
    #             'domain': [
    #                 ['id', 'in', [x.invoice.id for x in self.settlements]],
    #             ],
    #         }
    #     else:
    #         return {'type': 'ir.actions.act_window_close'}
    #
    #
    #


class InvoiceLine(models.Model):
    _inherit = 'account.move.line'

    agent = fields.Many2one(
        comodel_name="res.partner",
        domain="[('agent', '=', True)]",
        ondelete="restrict",
        required=False,
    )
    commission = fields.Many2one(
        comodel_name="sale.commission",
        ondelete="restrict",
        required=False,
        related="agent.commission",
        invisible="True",
        store="True"
    )
    agent_amount = fields.Monetary(
        string="Commission Amount",
        compute="_compute_agent_amount",
        store=True,
    )
    settled = fields.Boolean('Settled?', default=False, readonly=True)

    # Fields to be overriden with proper source (via related or computed field)
    # currency_id = fields.Many2one(comodel_name='res.currency')
    @api.depends('price_subtotal')
    def _compute_agent_amount(self):
        for line in self:
            # inv_line = line.object_id
            line.agent_amount = line._get_commission_amount(
                line.agent.commission, line.price_subtotal,
                line.product_id, line.quantity,
            )
            # Refunds commissions are negative
            if 'refund' in line.move_id.move_type:
                line.agent_amount = -line.agent_amount

    def _get_commission_amount(self, commission, subtotal, product, quantity):
        """Get the commission amount for the data given. To be called by
        compute methods of children models.
        """
        self.ensure_one()
        if product.commission_free or not commission:
            return 0.0
        if commission.amount_base_type == 'net_amount':
            # If subtotal (sale_price * quantity) is less than
            # standard_price * quantity, it means that we are selling at
            # lower price than we bought, so set amount_base to 0
            subtotal = max([
                0, subtotal - product.standard_price * quantity,
            ])
        if commission.commission_type == 'fixed':
            return subtotal * (commission.fix_qty / 100.0)
        elif commission.commission_type == 'section':
            return commission.calculate_section(subtotal)


class AccountInvoiceInherit(models.Model):
    _inherit = "account.move"
    
    settlement_id = fields.Many2one('sale.commission.settlement','Settlement')

    @api.depends('invoice_line_ids.agent_amount')
    def _compute_commission_total(self):
        for record in self:
            record.commission_total = 0.0
            for line in record.invoice_line_ids:
                record.commission_total += line.agent_amount

    def action_post(self):
        self.ensure_one()
        # res = super(AccountInvoiceInherit, self).action_post()
        for inv in self:
            if inv.move_type == 'out_invoice':
                settlement_obj = self.env['sale.commission.settlement']
                settlement_line_obj = self.env['sale.commission.settlement.line']
                inv_lines = inv.invoice_line_ids
                agents_id = inv_lines.mapped('agent')
                for agent in agents_id:
                    agent_lines = inv_lines.filtered(lambda r: r.agent.id == agent.id)
                    if agent_lines:
                        flag = agent_lines.mapped('settled')
                        if False in flag:
                            settlement = settlement_obj.create({
                                'agent': agent.id,
                                'date_from': inv.invoice_date if inv.invoice_date else fields.Date.context_today(self),
                                'date_to': inv.invoice_date if inv.invoice_date else fields.Date.context_today(self),
                                'company_id': inv.company_id.id,
                                'customer_invoice_id': inv.id,
                            })
                            for lines in agent_lines:
                                if not lines.settled:
                                    settlement_line_obj.create({
                                        'settlement': settlement.id,
                                        'date': inv.invoice_date if inv.invoice_date else fields.Date.context_today(self),
                                        'invoice_line': lines.id,
                                        'agent': agent.id,
                                        'settled_amount': lines.agent_amount
                                    })
                                    lines.update({
                                        'settled': True
                                    })
                            if settlement:
                                if settlement.state == 'settled':
                                    bill_obj = self.env['account.move']
                                    bill_lines_obj = self.env['account.move.line']
                                    settlement_lines = settlement.lines
                                    journal = self.env['account.journal'].search([('type', '=', 'purchase')])[:1]
                                    if settlement_lines:
                                        inv_lines_ids = []
                                        for line in settlement_lines:
                                            inv_lines_ids.append((0, 0, {
                                                'product_id': line.invoice_line.product_id.id,
                                                'quantity': 1,
                                                'exclude_from_invoice_tab': False,
                                                'price_unit': line.settled_amount,
                                                'tax_ids': [(5, _, _)],
                                            }))
                                        vendor_bill = bill_obj.with_context(check_move_validity=False).create({
                                            'partner_id': settlement.agent.id,
                                            'move_type': 'in_invoice',
                                            'company_id': settlement.company_id.id,
                                            'invoice_origin': settlement.name,
                                            'journal_id': journal.id,
                                            'invoice_date': settlement.date_from,
                                            'invoice_line_ids': inv_lines_ids,
                                            'state':'draft',
                                            'settlement_id':settlement.id
                                        })
                                        # vendor_bill._onchange_partner_id()
                                        # vendor_bill._onchange_journal_id()
                                        # vendor_bill_obj = vendor_bill._convert_to_write(vendor_bill._cache)
                                        # vendor_bill_id = bill_obj.create(vendor_bill_obj)
                                        # for line in settlement_lines:
                                        #     inv_line = bill_lines_obj.new({
                                        #         'product_id': line.invoice_line.product_id.id,
                                        #         'quantity': 1,
                                        #         'move_id': vendor_bill.id,
                                        #         'exclude_from_invoice_tab': False,
                                        #     })
                                        #     inv_line._onchange_product_id()
                                        #     invoice_line_vals = inv_line._convert_to_write(inv_line._cache)
                                        #     invoice_line_vals.update({
                                        #         'price_unit': line.settled_amount,
                                        #         'tax_ids': [(5, _, _)],
                                        #     })
                                        #     bill_lines_obj.with_context(check_move_validity=False).create(invoice_line_vals)
                                        # vendor_bill.action_post()
                                        settlement.update({
                                            'state': 'invoiced',
                                            'invoice': vendor_bill.id,
                                        })
                                    else:
                                        raise UserError(_('No Settlement lines available to be invoiced'))
        return super(AccountInvoiceInherit, self).action_post()
        # return res

    def action_view_customer_invoices(self):
        self.ensure_one()
        return {
            'name': _('Commission Settlements'),
            'view_mode': 'tree,form',
            'res_model': 'sale.commission.settlement',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'domain': [('customer_invoice_id', '=', self.id)],
        }

    def _get_invoice_count(self):
        count = self.env['sale.commission.settlement'].search_count([('customer_invoice_id', '=', self.id)])
        self.count_invoices = count

    count_invoices = fields.Integer('Invoice Count', compute='_get_invoice_count')


class SaleCommissionSettleInherit(models.TransientModel):
    _inherit = 'sale.commission.make.settle'

    def action_settle(self):
        self.ensure_one()
        invoice_lines = self.env['account.move.line']
        settlement_obj = self.env['sale.commission.settlement']
        settlement_line_obj = self.env['sale.commission.settlement.line']
        settlement_ids = []
        if not self.agents:
            self.agents = self.env['res.partner'].search(
                [('agent', '=', True)])
        date_to = self.date_to
        for agent in self.agents:
            sett_from = self._get_period_start(agent, date_to)
            sett_to = self._get_next_period_date(agent, sett_from) - timedelta(days=1)
            agent_lines = invoice_lines.search([('agent', '=', agent.id),
                                                ('settled', '=', False),
                                                ('move_id.invoice_date', '<=', sett_to),
                                                ('move_id.invoice_date', '>=', sett_from)], order='create_date')
            if agent_lines:
                settlement = self._get_settlement(agent, self.env.user.company_id, sett_from, sett_to)
                if not settlement:
                    settlement = settlement_obj.create(
                        self._prepare_settlement_vals(
                            agent, self.env.user.company_id, sett_from, sett_to))
                settlement_ids.append(settlement.id)
                for line in agent_lines:
                    if line.move_id.state != 'posted':
                        continue
                    settlement_line_obj.create({
                        'settlement': settlement.id,
                        'date': line.move_id.invoice_date,
                        'invoice_line': line.id,
                        'agent': line.agent.id,
                        'settled_amount': line.agent_amount
                    })
                    line.update({
                        'settled': True
                    })
        if len(settlement_ids):
            return {
                'name': _('Created Settlements'),
                'type': 'ir.actions.act_window',
                'views': [[False, 'list'], [False, 'form']],
                'res_model': 'sale.commission.settlement',
                'domain': [['id', 'in', settlement_ids]],
            }
        else:
            return {'type': 'ir.actions.act_window_close'}


class SaleCommissionAnalysisReportInherit(models.Model):
    _inherit = "sale.commission.analysis.report"

    def _select(self):
        select_str = """
            SELECT MIN(ail.id) AS id,
            ai.partner_id AS partner_id,
            ai.state AS invoice_state,
            ai.invoice_date AS date_invoice,
            ail.company_id AS company_id,
            rp.id AS agent_id,
            pt.categ_id AS categ_id,
            ail.product_id AS product_id,
            pt.uom_id AS uom_id,
            SUM(ail.quantity) AS quantity,
            AVG(ail.price_unit) AS price_unit,
            SUM(ail.price_subtotal) AS price_subtotal,
            SUM(ail.price_subtotal) AS price_subtotal_signed,
            AVG(sc.fix_qty) AS percentage,
            SUM(ail.agent_amount) AS amount,
            ail.id AS invoice_line_id,
            ail.settled AS settled,
            ail.commission AS commission_id
        """
        return select_str

    def _from(self):
        from_str = """
            account_move_line ail
            JOIN account_move ai ON ai.id = ail.move_id
            LEFT JOIN sale_commission sc ON sc.id = ail.commission
            LEFT JOIN product_product pp ON pp.id = ail.product_id
            INNER JOIN product_template pt ON pp.product_tmpl_id = pt.id
            LEFT JOIN res_partner rp ON ail.agent = rp.id
        """
        return from_str

    def _group_by(self):
        group_by_str = """
            GROUP BY ai.partner_id,
            ai.state,
            ai.invoice_date,
            ail.company_id,
            rp.id,
            pt.categ_id,
            ail.product_id,
            pt.uom_id,
            ail.id,
            ail.settled,
            ail.commission
        """
        return group_by_str


class SaleOrderCommissionAnalysisReportInherit(models.Model):
    _inherit = "sale.order.commission.analysis.report"

    def _select(self):
        select_str = """
            SELECT MIN(sol.id) AS id,
            so.partner_id AS partner_id,
            so.state AS order_state,
            so.date_order AS date_order,
            sol.company_id AS company_id,
            sol.salesman_id AS salesman_id,
            rp.id AS agent_id,
            pt.categ_id AS categ_id,
            sol.product_id AS product_id,
            pt.uom_id AS uom_id,
            SUM(sol.product_uom_qty) AS quantity,
            AVG(sol.price_unit) AS price_unit,
            SUM(sol.price_subtotal) AS price_subtotal,
            AVG(sc.fix_qty) AS percentage,
            SUM(sol.agent_amount) AS amount,
            sol.id AS order_line_id,
            sol.commission_line AS commission_id
        """
        return select_str

    def _from(self):
        from_str = """
            sale_order_line sol
            JOIN sale_order so ON so.id = sol.order_id
            LEFT JOIN sale_commission sc ON sc.id = sol.commission_line
            LEFT JOIN product_product pp ON pp.id = sol.product_id
            INNER JOIN product_template pt ON pp.product_tmpl_id = pt.id
            LEFT JOIN res_partner rp ON sol.agent = rp.id
        """
        return from_str

    def _group_by(self):
        group_by_str = """
            GROUP BY so.partner_id,
            so.state,
            so.date_order,
            sol.company_id,
            sol.salesman_id,
            rp.id,
            pt.categ_id,
            sol.product_id,
            pt.uom_id,
            sol.id,
            sol.commission_line
        """
        return group_by_str


