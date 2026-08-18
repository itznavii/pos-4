from datetime import datetime
import json

from flask import Blueprint, render_template, request, jsonify, send_file, abort, flash, redirect, url_for
from flask_login import login_required, current_user

from app import db, csrf
from app.decorators import admin_required
from app.models import (
    Product,
    Sale,
    SaleItem,
    SalePayment,
    SaleAttachment,
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
    save_upload,
)
from app.notifications.helpers import notify

pos = Blueprint("pos", __name__)

SENIOR_PWD_RATE = 0.20
METHODS_REQUIRING_REFERENCE = {"gcash"}


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
    # Accepts multipart/form-data (or regular form-encoded) so a
    # proof-of-payment file can be attached: field "payload" holds the
    # JSON-encoded sale data, field "proof_file" holds the optional/required
    # attachment. Falls back to a plain JSON body (no file) for compatibility.
    if "payload" in request.form:
        data = json.loads(request.form.get("payload", "{}"))
        proof_file = request.files.get("proof_file")
    else:
        data = request.get_json(force=True)
        proof_file = None

    product = Product.query.filter_by(id=data.get("product_id"), is_buffet=True).first()
    if not product:
        return jsonify({"error": "Select a valid buffet package"}), 400

    adult = int(data.get("adult", 0) or 0)
    senior = int(data.get("senior", 0) or 0)
    pwd = int(data.get("pwd", 0) or 0)
    kids = int(data.get("kids", 0) or 0)
    pwd_kids = int(data.get("pwd_kids", 0) or 0)
    free = int(data.get("free", 0) or 0)
    other_charges = max(0.0, float(data.get("other_charges", 0) or 0))

    # Other Charges is a manually-entered peso amount, NOT a guest — it must
    # never be counted toward total pax/guest count.
    total_pax = adult + senior + pwd + kids + pwd_kids + free
    if total_pax <= 0 and other_charges <= 0:
        return jsonify({"error": "Enter at least 1 guest or an Other Charges amount"}), 400

    adult_price = product.tier_price("adult")
    senior_price = product.tier_price("senior")
    pwd_price = product.tier_price("pwd")
    kids_price = product.tier_price("kids")
    pwd_kids_price = product.tier_price("pwd_kids")

    # Bill calculation order: Adult + Senior + PWD + Kids + PWD Kids + Other
    # Charges = Subtotal. Each guest category becomes its own SaleItem line
    # (qty x unit price = amount) so the receipt never has to show a unit
    # price standing in for a multi-guest line total.
    tier_lines = [
        ("adult", "Adult", adult, adult_price, "buffet_adult"),
        ("senior", "Senior Citizen", senior, senior_price, "buffet_senior"),
        ("pwd", "PWD", pwd, pwd_price, "buffet_pwd"),
        ("kids", "Kids", kids, kids_price, "buffet_kids"),
        ("pwd_kids", "PWD Kids", pwd_kids, pwd_kids_price, "buffet_pwd_kids"),
    ]

    sale_items = []
    subtotal = 0.0
    for tier_key, label, count, price, buffet_field in tier_lines:
        if count <= 0:
            continue
        line_total = round(count * price, 2)
        subtotal += line_total
        kwargs = {
            "product_id": product.id,
            "product_name": f"{product.name} - {label}",
            "quantity": count,
            "price": price,
            "line_total": line_total,
            "is_buffet": True,
            buffet_field: count,
        }
        item = SaleItem(**kwargs)
        sale_items.append((item, product))

    if free > 0:
        # Free guests (Kids Below 3 ft / Birthday Celebrant) always ₱0 — kept
        # as their own line for guest-count reporting, contributes no revenue.
        item = SaleItem(
            product_id=product.id,
            product_name=f"{product.name} - Free (Below 3ft / Birthday)",
            quantity=free,
            price=0,
            line_total=0,
            is_buffet=True,
            buffet_free=free,
        )
        sale_items.append((item, product))

    # The buffet is ONE order to the kitchen, not one ticket per guest
    # category — splitting Adult/Senior/PWD/Kids/PWD Kids into separate
    # SaleItem rows (for accurate per-category billing/receipt lines) would
    # otherwise fragment a single order into several kitchen tickets that
    # each need to be marked ready/served independently. Only the first
    # buffet line stays "preparing"; the rest are marked complete immediately
    # so the kitchen queue still shows exactly one ticket per buffet order.
    first_buffet_seen = False
    for item, _product in sale_items:
        if item.is_buffet:
            if not first_buffet_seen:
                first_buffet_seen = True
                # This is the one line the kitchen actually sees, so it must
                # state the FULL party size — not just its own category's
                # count — or the kitchen would only know about e.g. the
                # Adult guests and under-prepare for the rest of the party.
                item.product_name = f"{product.name} (Party of {total_pax})"
            else:
                item.kitchen_status = "completed"

    if other_charges > 0:
        subtotal += other_charges
        # Other Charges is a billing line (service fee, corkage, etc.), not
        # food — it must never appear in the kitchen queue.
        item = SaleItem(
            product_id=product.id,
            product_name="Other Charges",
            quantity=1,
            price=other_charges,
            line_total=other_charges,
            is_buffet=False,
            is_other_charge=True,
            kitchen_status="completed",
        )
        sale_items.append((item, product))

    subtotal = round(subtotal, 2)

    # Discount Type selector: Percentage (%) or Fixed Amount (₱). A fixed
    # amount is NEVER reinterpreted as a percentage, and vice versa.
    discount_type_input = (data.get("discount_type") or "percentage").strip().lower()
    discount_value = float(data.get("discount_value", 0) or 0)
    if discount_type_input == "fixed":
        discount_amount = max(0.0, min(discount_value, subtotal))
        discount_type_label = "Fixed Amount"
    else:
        discount_type_input = "percentage"
        discount_value = max(0.0, min(100.0, discount_value))
        discount_amount = subtotal * (discount_value / 100.0)
        discount_type_label = f"{discount_value:g}%"

    discount_amount = round(discount_amount, 2)
    total = round(max(0.0, subtotal - discount_amount), 2)

    payments = data.get("payments", [])
    amount_tendered_input = sum(float(p.get("amount", 0) or 0) for p in payments)

    if not payments or amount_tendered_input <= 0:
        return jsonify({"error": "Please enter at least one payment before completing the sale."}), 400

    needs_proof = False
    for p in payments:
        method = (p.get("method") or "").strip()
        if method.lower() in METHODS_REQUIRING_REFERENCE:
            if not (p.get("reference_number") or "").strip():
                return jsonify({"error": f"A reference number is required for {method} payments."}), 400
            needs_proof = True

    if needs_proof and not (proof_file and proof_file.filename):
        return jsonify({"error": "Please attach proof of payment (screenshot/receipt) for GCash payments."}), 400

    if amount_tendered_input + 0.01 < total:
        return jsonify({"error": f"Payment (₱{amount_tendered_input:.2f}) is less than the total bill (₱{total:.2f})."}), 400

    # Change only makes sense for a Cash payment — GCash/Bank Transfer/Credit
    # Card never generate cash change. If the sale mixes Cash with another
    # method, change is computed only against the Cash portion versus what's
    # still owed after the non-cash payments are applied.
    cash_paid = sum(float(p.get("amount", 0) or 0) for p in payments if (p.get("method") or "").strip().lower() == "cash")
    non_cash_paid = amount_tendered_input - cash_paid
    remaining_after_non_cash = max(0.0, total - non_cash_paid)
    change = max(0.0, round(cash_paid - remaining_after_non_cash, 2)) if cash_paid > 0 else 0.0

    attachment_filename = None
    if proof_file and proof_file.filename:
        try:
            attachment_filename = save_upload(proof_file, subfolder="sales")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    is_walkin = bool(data.get("is_walkin", False))
    table_id = data.get("table_id") or None
    customer_id = data.get("customer_id") or None
    customer_name = data.get("customer_name") or ("Walk-in" if is_walkin else "Guest")
    reservation_id = data.get("reservation_id") or None

    requires_approval = discount_amount > 1000.0 and not current_user.is_admin()

    sale = Sale(
        sale_number=new_sale_number(),
        customer_id=customer_id,
        customer_name=customer_name,
        is_walkin=is_walkin,
        queue_number=new_queue_number() if is_walkin else None,
        table_id=table_id,
        reservation_id=reservation_id,
        subtotal=subtotal,
        discount=discount_amount,
        discount_type=discount_type_label,
        vat=0,
        total=total,
        amount_tendered=amount_tendered_input,
        change=change,
        status="open" if requires_approval else "completed",
        requires_approval=requires_approval,
        cashier_id=current_user.id,
    )
    db.session.add(sale)
    db.session.flush()

    for item, product_ref in sale_items:
        item.sale_id = sale.id
        db.session.add(item)
        if not requires_approval:
            deduct_inventory_for_sale_item(item, product_ref)

    for p in payments:
        db.session.add(
            SalePayment(
                sale_id=sale.id,
                method=p.get("method", "Cash"),
                amount=float(p.get("amount", 0) or 0),
                reference_number=p.get("reference_number"),
            )
        )

    if attachment_filename:
        db.session.add(SaleAttachment(sale_id=sale.id, filename=attachment_filename))

    if table_id:
        table = RestaurantTable.query.get(table_id)
        if table:
            table.status = "occupied"

    db.session.commit()

    if requires_approval:
        notify(
            "discount_approval",
            f"Sale {sale.sale_number} needs approval: discount PHP {discount_amount:.2f}",
            related_id=sale.id,
        )
        log_activity(f"Created sale {sale.sale_number} pending approval (discount PHP {discount_amount:.2f})")
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
