import io
import os
import uuid

from flask import current_app, request
from flask_login import current_user
from werkzeug.utils import secure_filename

from app import db
from app.models import ActivityLog


# ---------------------------------------------------------------------------
# Numbering helpers
# ---------------------------------------------------------------------------
def new_sale_number():
    return "SALE-" + uuid.uuid4().hex[:8].upper()


def new_reservation_number():
    return "RES-" + uuid.uuid4().hex[:6].upper()


def new_queue_number():
    """Sequential queue number that resets conceptually per day (based on count)."""
    from app.models import Sale
    from datetime import date

    today = date.today()
    count = Sale.query.filter(
        Sale.is_walkin.is_(True), db.func.date(Sale.created_at) == today
    ).count()
    return f"Q-{count + 1:03d}"


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
def log_activity(action):
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        entry = ActivityLog(
            user_id=user_id,
            action=action,
            ip_address=request.remote_addr if request else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


# ---------------------------------------------------------------------------
# Secure file upload
# ---------------------------------------------------------------------------
def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]


def save_upload(file_storage, subfolder=""):
    """Safely persist an uploaded file, returns the stored relative filename."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        raise ValueError("File type not allowed")
    safe_name = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex[:10]}_{safe_name}"
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], subfolder)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, unique_name)
    file_storage.save(dest)
    return os.path.join(subfolder, unique_name) if subfolder else unique_name


# ---------------------------------------------------------------------------
# Inventory deduction
# ---------------------------------------------------------------------------
def deduct_inventory_for_sale_item(sale_item, product):
    from app.models import InventoryTransaction

    if not product or not product.inventory_item_id:
        return
    inv_item = product.inventory_item
    if not inv_item:
        return
    qty_to_deduct = (product.deduct_qty or 1.0) * sale_item.quantity
    inv_item.quantity = max(0, (inv_item.quantity or 0) - qty_to_deduct)
    txn = InventoryTransaction(
        item_id=inv_item.id,
        type="sale_deduction",
        quantity=qty_to_deduct,
        reference=str(sale_item.sale_id),
        note=f"Sold: {product.name}",
        user_id=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(txn)


def check_low_stock_and_notify():
    from app.models import InventoryItem
    from app.notifications.helpers import notify

    low_items = InventoryItem.query.filter(
        InventoryItem.quantity <= InventoryItem.low_stock_threshold
    ).all()
    for item in low_items:
        notify(
            "low_inventory",
            f"Low stock: {item.name} ({item.quantity} {item.unit} left)",
            related_id=item.id,
            dedupe=True,
        )


# ---------------------------------------------------------------------------
# Receipt PDF (thermal 80mm) with QR code
# ---------------------------------------------------------------------------
def generate_receipt_pdf(sale, restaurant_name="Sitio Verde Buffet Restaurant"):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    import qrcode

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=(80 * mm, 180 * mm))
    width = 80 * mm

    y = 170 * mm
    p.setFont("Helvetica-Bold", 11)
    p.drawCentredString(width / 2, y, restaurant_name)
    y -= 5 * mm
    p.setFont("Helvetica", 8)
    p.drawCentredString(width / 2, y, "Official Receipt")
    y -= 4 * mm
    p.line(5 * mm, y, 75 * mm, y)
    y -= 5 * mm

    p.setFont("Helvetica", 8)
    p.drawString(5 * mm, y, f"Receipt #: {sale.sale_number}")
    y -= 4 * mm
    if sale.reservation_id:
        p.drawString(5 * mm, y, f"Reservation #: {sale.reservation.reservation_number}")
        y -= 4 * mm
    p.drawString(5 * mm, y, f"Date: {sale.created_at.strftime('%Y-%m-%d %H:%M')}")
    y -= 4 * mm
    cashier_name = sale.cashier.name if sale.cashier else "N/A"
    p.drawString(5 * mm, y, f"Cashier: {cashier_name}")
    y -= 4 * mm
    p.drawString(5 * mm, y, f"Customer: {sale.customer_name or 'Walk-in'}")
    y -= 4 * mm
    if sale.table:
        p.drawString(5 * mm, y, f"Table: {sale.table.name}")
        y -= 4 * mm
    if sale.queue_number:
        p.drawString(5 * mm, y, f"Queue #: {sale.queue_number}")
        y -= 4 * mm
    y -= 2 * mm
    p.line(5 * mm, y, 75 * mm, y)
    y -= 5 * mm

    p.setFont("Helvetica-Bold", 8)
    p.drawString(5 * mm, y, "Item")
    p.drawString(42 * mm, y, "Qty")
    p.drawString(50 * mm, y, "Price")
    p.drawString(62 * mm, y, "Amount")
    y -= 4 * mm
    p.setFont("Helvetica", 8)
    # Each guest category (Adult/Senior/PWD/Kids/PWD Kids/Free) and Other
    # Charges is its own SaleItem row, so Amount is always the row's own
    # Qty x Unit Price — never a unit price standing in for the line total.
    for item in sale.items:
        label = item.product_name
        p.drawString(5 * mm, y, label[:22])
        if item.is_other_charge:
            p.drawString(42 * mm, y, "-")
            p.drawString(50 * mm, y, "-")
        else:
            p.drawString(42 * mm, y, str(item.quantity))
            p.drawString(50 * mm, y, f"{item.price:.2f}")
        p.drawString(62 * mm, y, f"{item.line_total:.2f}")
        y -= 4 * mm

    y -= 2 * mm
    p.line(5 * mm, y, 75 * mm, y)
    y -= 5 * mm

    # Down payment already made on the reservation (verified, sourced from the
    # actual SalePayment row recorded at final billing — NOT the raw
    # reservation.down_payment field, which only holds what was recorded at
    # booking time and can go stale if payments were added/verified later).
    down_payment_amount = sum(
        pay.amount for pay in sale.payments if pay.method == "Down Payment"
    )

    p.setFont("Helvetica", 8)
    p.drawString(5 * mm, y, f"Subtotal: PHP {sale.subtotal:.2f}")
    y -= 4 * mm
    if sale.discount:
        p.drawString(5 * mm, y, f"Discount ({sale.discount_type}): -PHP {sale.discount:.2f}")
        y -= 4 * mm
    if down_payment_amount:
        p.drawString(5 * mm, y, f"Less Down Payment: -PHP {down_payment_amount:.2f}")
        y -= 4 * mm
    p.setFont("Helvetica-Bold", 9)
    balance_due = max(0.0, sale.total - down_payment_amount)
    total_label = "BALANCE DUE" if down_payment_amount else "TOTAL"
    p.drawString(5 * mm, y, f"{total_label}: PHP {balance_due:.2f}")
    y -= 5 * mm
    p.setFont("Helvetica", 8)

    for pay in sale.payments:
        # A reference number only ever belongs to a non-cash payment (GCash,
        # Bank Transfer, etc.) — if one is stored on a Cash row (e.g. a
        # cashier typed something into the Ref # field before switching the
        # method back to Cash), never print it.
        ref = f" ({pay.reference_number})" if (pay.reference_number and pay.method != "Cash") else ""
        p.drawString(5 * mm, y, f"{pay.method}: PHP {pay.amount:.2f}{ref}")
        y -= 4 * mm

    # Tendered/change is a cash-register concept — it only makes sense when the
    # current payment method is Cash. GCash/Bank Transfer/Credit Card have no
    # "change," and mixing the historical down payment into this figure (as the
    # old code did) produced a nonsensical number regardless of method.
    current_payment = next(
        (pay for pay in sale.payments if pay.method != "Down Payment"), None
    )
    if current_payment and current_payment.method == "Cash" and sale.amount_tendered:
        p.drawString(5 * mm, y, f"Cash Tendered: PHP {sale.amount_tendered:.2f}")
        y -= 4 * mm
        p.drawString(5 * mm, y, f"Change: PHP {sale.change:.2f}")
        y -= 4 * mm

    y -= 3 * mm
    # QR code linking to the sale number for verification
    try:
        qr = qrcode.QRCode(box_size=2, border=1)
        qr.add_data(f"SITIOVERDE|{sale.sale_number}|{sale.total:.2f}")
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        from reportlab.lib.utils import ImageReader

        qr_size = 22 * mm
        p.drawImage(
            ImageReader(qr_buf),
            (width - qr_size) / 2,
            y - qr_size,
            width=qr_size,
            height=qr_size,
        )
        y -= qr_size + 3 * mm
    except Exception:
        pass

    p.setFont("Helvetica-Oblique", 8)
    p.drawCentredString(width / 2, y, "Thank you for dining with us!")

    p.showPage()
    p.save()
    buf.seek(0)
    return buf
