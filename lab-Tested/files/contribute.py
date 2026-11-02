from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import quote

import qrcode
from qrcode.image.svg import SvgPathImage
from flask import Response, render_template, request

from files.ansible import app

UPI_ID = "manohar.shetty10-4@okaxis"
PAYEE_NAME = "Manohar Shetty"
NOTE = "Learning Labs contribution"


def _clean_amount(raw):
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value < 1 or value > 100000:
        return None
    return f"{value:.2f}"


def _upi_link(amount=None):
    link = f"upi://pay?pa={UPI_ID}&pn={quote(PAYEE_NAME)}&cu=INR&tn={quote(NOTE)}"
    if amount:
        link += f"&am={amount}"
    return link


@app.route("/contribute")
def contribute():
    return render_template("contribute.html", upi_id=UPI_ID, payee=PAYEE_NAME)


@app.route("/contribute/qr")
def contribute_qr():
    amount = _clean_amount(request.args.get("amount"))
    img = qrcode.make(_upi_link(amount), image_factory=SvgPathImage, box_size=10, border=2)
    buf = BytesIO()
    img.save(buf)
    return Response(
        buf.getvalue(),
        mimetype="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )