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
        # Section/note lines (display_type set) have no product_id, so use
        # ``.get`` to avoid a KeyError and map products back by id.
        prod_ids = [vals.get("product_id") for vals in vals_list]
        products = self.env["product.product"].browse(pid for pid in prod_ids if pid)
        products_by_id = {product.id: product for product in products}
        for line_vals in vals_list:
            product = products_by_id.get(line_vals.get("product_id"))
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
        """
        This method extends the base '_onchange_quantity' to re-calculate the
        price unit following options on product-pack
        """
        res = super()._onchange_quantity()
        if not self.product_id or self.invoice_lines:
            return res

        params = {"order_id": self.order_id}
        seller = self.product_id._select_seller(
            partner_id=self.partner_id,
            quantity=self.product_qty,
            date=self.order_id.date_order and self.order_id.date_order.date(),
            uom_id=self.product_uom,
            params=params,
        )

        prices = self.product_id.pack_cost_compute(self)
        # If not prices, no need to re-calculate the price units
        if not prices:
            return res
        cost = prices[self.product_id.id]
        # If not seller, use the standard price. It needs a proper currency conversion.
        if not seller:
            unavailable_seller = self.product_id.seller_ids.filtered(
                lambda s: s.name == self.order_id.partner_id
            )
            if (
                not unavailable_seller
                and self.price_unit
                and self.product_uom == self._origin.product_uom
            ):
                # Avoid to modify the price unit if there is no price list
                # for this partner and
                # the line has already one to avoid to override unit price set manually.
                return res
            po_line_uom = self.product_uom or self.product_id.uom_po_id
            # Using new cost to compute the price_unit
            price_unit = self.env["account.tax"]._fix_tax_included_price_company(
                self.product_id.uom_id._compute_price(cost, po_line_uom),
                self.product_id.supplier_taxes_id,
                self.taxes_id,
                self.company_id,
            )
            price_unit = self.product_id.currency_id._convert(
                price_unit,
                self.currency_id,
                self.company_id,
                self.date_order,
                False,
            )
            self.price_unit = float_round(
                price_unit,
                precision_digits=max(
                    self.currency_id.decimal_places,
                    self.env["decimal.precision"].precision_get("Product Price"),
                ),
            )
            return res
        # Using new cost to compute the price unit
        price_unit = (
            self.env["account.tax"]._fix_tax_included_price_company(
                cost,
                self.product_id.supplier_taxes_id,
                self.taxes_id,
                self.company_id,
            )
            if seller
            else 0.0
        )
        price_unit = seller.currency_id._convert(
            price_unit, self.currency_id, self.company_id, self.date_order, False
        )
        price_unit = float_round(
            price_unit,
            precision_digits=max(
                self.currency_id.decimal_places,
                self.env["decimal.precision"].precision_get("Product Price"),
            ),
        )
        self.price_unit = seller.product_uom._compute_price(
            price_unit, self.product_uom
        )
        return res
