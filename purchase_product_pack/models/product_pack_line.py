# Copyright 2023 Camptocamp SA
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl)

from odoo import models


class ProductPackLine(models.Model):
    _inherit = "product.pack.line"

    def get_purchase_order_line_vals(self, line, order):
        self.ensure_one()
        quantity = self.quantity * line.product_qty
        line_vals = {
            "order_id": order.id,
            "sequence": line.sequence,
            "product_id": self.product_id.id or False,
            "pack_parent_line_id": line.id,
            "pack_depth": line.pack_depth + 1,
            "company_id": order.company_id.id,
            "pack_modifiable": line.product_id.pack_modifiable,
        }
        pol = line.new(line_vals)
        pol.onchange_product_id()
        pol.product_qty = quantity
        pol._onchange_quantity()
        pol.onchange_product_id_warning()
        vals = pol._convert_to_write(pol._cache)
        # On purchase orders we keep the computed unit price on every component
        # line (we do not force it to 0.0 for "totalized"/"ignored" packs) so
        # each purchased component carries its own price and no line is left at
        # 0.00, which would be rejected by "no zero unit price" validations.
        vals.update(
            {
                "name": "{}{}".format(
                    "> " * (line.pack_depth + 1), pol.product_id.name
                ),
            }
        )
        return vals

    def get_seller_cost(self, line):
        """This function returns the cost of pack lines if they has seller or not"""
        self.ensure_one()
        if line:
            params = {"order_id": line.order_id}
            pack_line_seller = self.product_id._select_seller(
                partner_id=line.partner_id,
                quantity=self.quantity,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom,
                params=params,
            )
            return (
                pack_line_seller.price * self.quantity
                if pack_line_seller
                else self.product_id.standard_price * self.quantity
            )
        return self.product_id.standard_price * self.quantity
