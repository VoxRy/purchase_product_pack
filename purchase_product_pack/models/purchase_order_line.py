# Copyright 2023 Camptocamp SA
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl)

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import first
from odoo.tools.float_utils import float_round


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"
    _parent_name = "pack_parent_line_id"

    pack_type = fields.Selection(
        related="product_id.pack_type",
    )
    pack_component_price = fields.Selection(
        related="product_id.pack_component_price",
    )

    # Fields for common packs
    pack_depth = fields.Integer(
        "Depth", help="Depth of the product if it is part of a pack."
    )
    pack_parent_line_id = fields.Many2one(
        "purchase.order.line",
        "Pack",
        help="The pack that contains this product.",
    )
    pack_child_line_ids = fields.One2many(
        "purchase.order.line", "pack_parent_line_id", "Lines in pack"
    )
    pack_modifiable = fields.Boolean(help="The parent pack is modifiable")

    do_no_expand_pack_lines = fields.Boolean(
        compute="_compute_do_no_expand_pack_lines",
        help="This is a technical field in order to check if pack lines has to be expanded",
    )

    @api.depends_context("update_prices", "update_pricelist")
    def _compute_do_no_expand_pack_lines(self):
        do_not_expand = self.env.context.get("update_prices") or self.env.context.get(
            "update_pricelist", False
        )
        self.update(
            {
                "do_no_expand_pack_lines": do_not_expand,
            }
        )

    def expand_pack_line(self, write=False):
        """
        Expand a purchase order line that represents a pack.
        This method is used to expand a purchase order line that represents a pack.
        It creates individual purchase order lines for the components of the pack
        and adds them to the purchase order.
        """
        self.ensure_one()
        vals_list = []
        if self.product_id.pack_ok and self.pack_type == "detailed":
            for subline in self.product_id.get_pack_lines():
                vals = subline.get_purchase_order_line_vals(self, self.order_id)
                if write:
                    existing_subline = first(
                        self.pack_child_line_ids.filtered(
                            lambda child: child.product_id == subline.product_id
                        )
                    )
                    # if subline already exists we update, if not we create
                    if existing_subline:
                        if self.do_no_expand_pack_lines:
                            vals.pop("product_uom_qty", None)
                        existing_subline.write(vals)
                    elif not self.do_no_expand_pack_lines:
                        vals_list.append(vals)
                else:
                    vals_list.append(vals)
            if vals_list:
                self.create(vals_list)

    @api.model_create_multi
    def create(self, vals_list):
        new_vals = []
        res = self.browse()
        prod_ids = [vals["product_id"] for vals in vals_list]
        products = self.env["product.product"].browse(prod_ids)
        for line_vals, product in zip(vals_list, products):
            if product and product.pack_ok and product.pack_type != "non_detailed":
                line = super().create([line_vals])
                line.expand_pack_line()
                res |= line
            else:
                new_vals.append(line_vals)
        res |= super().create(new_vals)
        return res

    def write(self, vals):
        res = super().write(vals)
        if "product_id" in vals or "product_qty" in vals:
            for record in self:
                record.expand_pack_line(write=True)
        return res

    @api.onchange(
        "product_id",
        "product_uom_qty",
        "product_uom",
        "price_unit",
        "name",
        "taxes_id",
    )
    def check_pack_line_modify(self):
        """Do not let to edit a purchase order line if this one belongs to pack"""
        if self._origin.pack_parent_line_id and not self._origin.pack_modifiable:
            raise UserError(
                _(
                    "You can not change this line because is part of a pack"
                    " included in this order"
                )
            )

    def action_open_parent_pack_product_view(self):
        domain = [
            ("id", "in", self.mapped("pack_parent_line_id").mapped("product_id").ids)
        ]
        return {
            "name": _("Parent Product"),
            "type": "ir.actions.act_window",
            "res_model": "product.product",
            "view_type": "form",
            "view_mode": "tree,form",
            "domain": domain,
        }

    @api.onchange("product_qty", "product_uom", "company_id")
    def _onchange_quantity(self):
        """Odoo 15 sets ``price_unit`` through this onchange (not through a
        compute like in v16). We extend it to overwrite the pack parent line
        price with the totalized cost of its components, so a pack whose vendor
        price lives on the components does not end up with a 0 unit price.
        """
        res = super()._onchange_quantity()
        self._apply_pack_price_unit()
        return res

    def _apply_pack_price_unit(self):
        """Recompute ``price_unit`` for pack parent lines from their component
        costs. Only affects products flagged as packs (``pack_cost_compute``
        returns nothing for regular products and for pack components)."""
        for line in self:
            if not line.product_id or line.invoice_lines:
                continue

            prices = line.product_id.pack_cost_compute(line)
            # If not prices, this is not a pack line: keep the standard price.
            if not prices:
                continue
            cost = prices[line.product_id.id]

            params = {"order_id": line.order_id}
            seller = line.product_id._select_seller(
                partner_id=line.partner_id,
                quantity=line.product_qty,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom,
                params=params,
            )
            currency = seller.currency_id if seller else line.product_id.currency_id
            po_line_uom = line.product_uom or line.product_id.uom_po_id

            price_unit = line.env["account.tax"]._fix_tax_included_price_company(
                line.product_id.uom_id._compute_price(cost, po_line_uom),
                line.product_id.supplier_taxes_id,
                line.taxes_id,
                line.company_id,
            )
            if currency and line.currency_id and currency != line.currency_id:
                price_unit = currency._convert(
                    price_unit,
                    line.currency_id,
                    line.company_id or self.env.company,
                    line.date_order or fields.Date.today(),
                    False,
                )
            line.price_unit = float_round(
                price_unit,
                precision_digits=max(
                    line.currency_id.decimal_places,
                    self.env["decimal.precision"].precision_get("Product Price"),
                ),
            )
