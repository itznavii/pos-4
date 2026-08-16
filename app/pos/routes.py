from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, send_file, abort, flash, redirect, url_for
from flask_login import login_required, current_user

from app import db, csrf
from app.decorators import admin_required
from app.models import (
    Product,
    Sale,
    SaleItem,
    SalePayment,
    RestaurantTable,
    Customer,
    InventoryTransaction,
)
from app.utils import (
    new_sale_number,
    new_queue_number,
    log_activity,
    deduct_inventory_for_sale_item,
    check_low_stock_and_notify,
    generate_receipt_pdf,
)
from app.notifications.helpers import notify

pos = Blueprint("pos", __name__)

SENIOR_PWD_RATE = 0.20
METHODS_REQUIRING_REFERENCE = {"gcash", "bank transfer"}


@pos.route("/")
@login_required
def index():
    buffet_products = Product.query.filter_by(available=True, is_buffet=True).all()
    tables = RestaurantTable.query.order_by(RestaurantTable.name).all()
    return render_template("pos/index.html", buffet_products=buffet_products, tables=tables)


@pos.route("/api/buffet-products")
@login_required
def api_buffet_products():
    products = Product.query.filter_by(available=True, is_buffet=True).all()
    results = [_product_json(p) for p in products]
    return jsonify(results)


def _product_json(p):
    tiers = {t.tier: t.price for t in p.buffet_tiers}
    return {
        "id": p.id,
        "name": p.name,
        "is_buffet": p.is_buffet,
        "buffet_tiers": tiers,
    }


@pos.route("/api/next-queue-number")
@login_required
def api_next_queue():
    return jsonify({"queue_number": new_queue_number()})


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
@pos.route("/checkout", methods=["POST"])
@login_required
@csrf.exempt
def checkout():
    data = request.get_json(force=True)

    product = Product.query.filter_by(id=data.get("product_id"), is_buffet=True).first()
    if not product:
        return jsonify({"error": "Select a valid buffet package"}), 400

    adult = int(data.get("adult", 0) or 0)
    senior = int(data.get("senior", 0) or 0)
    pwd = int(data.get("pwd", 0) or 0)
    kids = int(data.get("kids", 0) or 0)
    free = int(data.get("free", 0) or 0)
    total_pax = adult + senior + pwd + kids + free
    if total_pax <= 0:
        return jsonify({"error": "Enter at least 1 guest"}), 400

    adult_price = product.tier_price("adult")
    senior_price = product.tier_price("senior")
    pwd_price = product.tier_price("pwd")
    kids_price = product.tier_price("kids")

    subtotal = (
        adult * adult_price
        + senior * senior_price
        + pwd * pwd_price
        + kids * kids_price
        + free * 0
    )
    buffet_informational_discount = (adult_price - senior_price) * senior + (
        adult_price - pwd_price
    ) * pwd

    item = SaleItem(
        product_id=product.id,
        product_name=product.name,
        quantity=total_pax,
        price=adult_price,
        line_total=subtotal,
        is_buffet=True,
        buffet_adult=adult,
        buffet_senior=senior,
        buffet_pwd=pwd,
        buffet_kids=kids,
        buffet_free=free,
    )
    sale_items = [(item, product)]

    promo_percent = float(data.get("promo_discount_percent", 0) or 0)
    promo_percent = max(0.0, min(100.0, promo_percent))
    promo_amount = subtotal * (promo_percent / 100.0)

    total_discount = promo_amount
    taxable = max(0.0, subtotal - total_discount)
    total = round(taxable, 2)

    payments = data.get("payments", [])
    amount_tendered = sum(float(p.get("amount", 0) or 0) for p in payments)

    if not payments or amount_tendered <= 0:
        return jsonify({"error": "Please enter at least one payment before completing the sale."}), 400

    for p in payments:
        method = (p.get("method") or "").strip()
        if method.lower() in METHODS_REQUIRING_REFERENCE and not (p.get("reference_number") or "").strip():
            return jsonify({"error": f"A reference number is required for {method} payments."}), 400

    if amount_tendered + 0.01 < total:
        return jsonify({"error": f"Payment (₱{amount_tendered:.2f}) is less than the total bill (₱{total:.2f})."}), 400

    is_walkin = bool(data.get("is_walkin", False))
    table_id = data.get("table_id") or None
    customer_id = data.get("customer_id") or None
    customer_name = data.get("customer_name") or ("Walk-in" if is_walkin else "Guest")
    reservation_id = data.get("reservation_id") or None

    requires_approval = total_discount > 1000.0 and not current_user.is_admin()

    sale = Sale(
        sale_number=new_sale_number(),
        customer_id=customer_id,
        customer_name=customer_name,
        is_walkin=is_walkin,
        queue_number=new_queue_number() if is_walkin else None,
        table_id=table_id,
        reservation_id=reservation_id,
        subtotal=round(subtotal, 2),
        discount=round(total_discount + buffet_informational_discount, 2),
        discount_type="promo" if total_discount else "none",
        vat=0,
        total=total,
        amount_tendered=amount_tendered,
        change=max(0.0, round(amount_tendered - total, 2)),
        status="open" if requires_approval else "completed",
        requires_approval=requires_approval,
        cashier_id=current_user.id,
    )
    db.session.add(sale)
    db.session.flush()

    for item, product in sale_items:
        item.sale_id = sale.id
        db.session.add(item)
        if not requires_approval:
            deduct_inventory_for_sale_item(item, product)

    for p in payments:
        db.session.add(
            SalePayment(
                sale_id=sale.id,
                method=p.get("method", "Cash"),
                amount=float(p.get("amount", 0) or 0),
                reference_number=p.get("reference_number"),
            )
        )

    if table_id:
        table = RestaurantTable.query.get(table_id)
        if table:
            table.status = "occupied"

    db.session.commit()

    if requires_approval:
        notify(
            "discount_approval",
            f"Sale {sale.sale_number} needs approval: discount PHP {total_discount:.2f}",
            related_id=sale.id,
        )
        log_activity(f"Created sale {sale.sale_number} pending approval (discount PHP {total_discount:.2f})")
        return jsonify({"sale_id": sale.id, "requires_approval": True, "sale_number": sale.sale_number})

    check_low_stock_and_notify()
    log_activity(f"Completed sale {sale.sale_number} (PHP {total:.2f})")
    return jsonify({"sale_id": sale.id, "requires_approval": False, "sale_number": sale.sale_number, "total": total})


@pos.route("/approvals")
@login_required
@admin_required
def approvals():
    pending = Sale.query.filter_by(requires_approval=True, status="open").order_by(Sale.created_at).all()
    return render_template("pos/approvals.html", pending=pending)


@pos.route("/approve/<int:sale_id>", methods=["POST"])
@login_required
@admin_required
def approve_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    sale.status = "completed"
    sale.approved_by_id = current_user.id
    for item in sale.items:
        deduct_inventory_for_sale_item(item, item_product(item))
    db.session.commit()
    check_low_stock_and_notify()
    log_activity(f"Approved discount for sale {sale.sale_number}")
    flash(f"Sale {sale.sale_number} approved.", "success")
    return redirect(url_for("pos.approvals"))


def item_product(item):
    return Product.query.get(item.product_id)


@pos.route("/void/<int:sale_id>", methods=["POST"])
@login_required
@admin_required
def void_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    reason = request.form.get("reason", "No reason given")
    if sale.status == "voided":
        flash("Sale already voided.", "warning")
        return redirect(request.referrer or url_for("main.dashboard"))

    # reverse inventory deductions tied to this sale
    txns = InventoryTransaction.query.filter_by(reference=str(sale.id), type="sale_deduction").all()
    for t in txns:
        if t.item:
            t.item.quantity = (t.item.quantity or 0) + t.quantity

    sale.status = "voided"
    sale.void_reason = reason
    sale.voided_by_id = current_user.id
    if sale.table_id:
        table = RestaurantTable.query.get(sale.table_id)
        if table:
            table.status = "cleaning"
    db.session.commit()
    log_activity(f"Voided sale {sale.sale_number}: {reason}")
    flash(f"Sale {sale.sale_number} voided and inventory restocked.", "success")
    return redirect(request.referrer or url_for("main.dashboard"))


@pos.route("/receipt/<int:sale_id>")
@login_required
def receipt(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    pdf = generate_receipt_pdf(sale)
    return send_file(pdf, mimetype="application/pdf", download_name=f"{sale.sale_number}.pdf")


@pos.route("/history")
@login_required
def history():
    sales = Sale.query.order_by(Sale.created_at.desc()).limit(200).all()
    return render_template("pos/history.html", sales=sales)


@pos.route("/sale/<int:sale_id>")
@login_required
def sale_detail(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    return render_template("pos/sale_detail.html", sale=sale)
