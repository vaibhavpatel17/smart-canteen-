"""Payment gateways.

TestGateway     built in. Simulates UPI, card and netbanking so the whole flow works with no account.
                No real money moves. Card numbers are checked and then thrown away (only the last 4 digits are kept).
UpiGateway      REAL money, straight to the canteen owner's own UPI ID (no Razorpay account or business needed).
                The student pays in any UPI app (a upi:// link or a QR code), then types the 12 digit UPI transaction ID.
                The kitchen checks its own UPI app and taps "Payment received". This is the default in live mode.
                Refunds are sent by the owner from the UPI app, and tracked as "refund due" until marked done.
RazorpayGateway pays inside the app, the way food-delivery apps do. The student taps Pay, Razorpay's own window opens
                (UPI apps, cards, netbanking, wallets), and the money is confirmed automatically: by a signature check when
                the student comes back, and by Razorpay's webhook if they close the app after paying. Refunds go back to
                the student's own account automatically. Turned on by RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (in the
                environment or in a .env file next to app.py); RAZORPAY_WEBHOOK_SECRET switches on the webhook.
                Razorpay's *test* keys (rzp_test_...) work with no KYC and move no real money.
                Everything here is tested against a faked Razorpay API; it has not been run against Razorpay itself.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from db import CFG

UPI_RE = re.compile(r"^[a-z0-9._\-]{2,}@[a-z][a-z0-9.]{1,}$")
UTR_RE = re.compile(r"^\d{12}$")
TEST_OTP = "123456"
UPI_APPS = ("Google Pay", "PhonePe", "Paytm")
DECLINED_CARD = "4000000000000002"


class GatewayError(Exception):
    pass


def luhn_ok(number):
    total, alt = 0, False
    for ch in reversed(number):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def card_brand(number):
    if number.startswith("4"):
        return "Visa"
    if re.match(r"^(5[1-5]|2[2-7])", number):
        return "Mastercard"
    if re.match(r"^3[47]", number):
        return "Amex"
    if re.match(r"^(60|65|81|82|508)", number):
        return "RuPay"
    return "Card"


def mask_vpa(vpa):
    name, _, bank = vpa.partition("@")
    return f"{name[:2]}••••@{bank}"


def _fail(msg):
    return {"ok": False, "error": msg}


class TestGateway:
    kind = "test"
    key_id = None
    is_test = True

    def prepare(self, payment_id, amount, receipt):
        return {}

    def charge(self, method, d, amount):
        ref = "pay_" + secrets.token_hex(5)
        if method == "upi":
            app = str(d.get("app", "")).strip()
            if app:      # one tap on a UPI app, like the app list in a real payment window
                if app not in UPI_APPS:
                    return _fail("Choose a UPI app.")
                return {"ok": True, "ref": ref, "label": f"UPI · {app}", "last4": None}
            vpa = str(d.get("vpa", "")).strip().lower()
            if not UPI_RE.match(vpa):
                return _fail("Enter a valid UPI ID, like name@bank.")
            if d.get("approve") is not True or vpa.startswith("fail"):
                return _fail("The request was declined in your UPI app.")
            return {"ok": True, "ref": ref, "label": f"UPI · {mask_vpa(vpa)}", "last4": None}
        if method == "card":
            num = re.sub(r"[ -]", "", str(d.get("number", "")))
            if not (num.isdigit() and 13 <= len(num) <= 19 and luhn_ok(num)):
                return _fail("That card number does not look right. Check it and try again.")
            m = re.match(r"^(\d{2})\s*/\s*(\d{2})$", str(d.get("exp", "")).strip())
            if not m or not 1 <= int(m.group(1)) <= 12:
                return _fail("Enter the expiry as MM/YY.")
            today = date.today()
            if (2000 + int(m.group(2)), int(m.group(1))) < (today.year, today.month):
                return _fail("This card has expired.")
            if not re.match(r"^\d{3,4}$", str(d.get("cvv", ""))):
                return _fail("Enter the 3 digit CVV.")
            if str(d.get("otp", "")).strip() != TEST_OTP:
                return _fail("Incorrect OTP. In test mode the OTP is 123456.")
            if num == DECLINED_CARD:
                return _fail("The bank declined this card (test card for failures).")
            return {"ok": True, "ref": ref, "label": f"{card_brand(num)} •••• {num[-4:]}", "last4": num[-4:]}
        if method == "netbanking":
            bank = str(d.get("bank", ""))
            if bank not in CFG["banks"]:
                return _fail("Choose your bank first.")
            if d.get("approve") is not True:
                return _fail("The bank did not authorise this payment.")
            return {"ok": True, "ref": ref, "label": f"Netbanking · {bank}", "last4": None}
        return _fail("Choose a payment method.")

    def verify(self, *a, **k):
        raise GatewayError("Razorpay is not enabled.")

    def refund(self, payment_row, amount):
        return {"ref": "rfnd_test_" + secrets.token_hex(5)}


class RazorpayGateway:
    kind = "razorpay"

    def __init__(self, key_id, key_secret, webhook_secret=None):
        self.key_id, self.key_secret = key_id, key_secret
        self.webhook_secret = webhook_secret or None
        self.is_test = str(key_id).startswith("rzp_test_")      # test keys move no real money

    def _call(self, path, body=None, method=None):
        """One call to Razorpay's REST API. GET when there is no body, POST otherwise. Raises GatewayError if it fails."""
        auth = base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()
        req = urllib.request.Request(
            "https://api.razorpay.com/v1" + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Basic " + auth}, method=method or ("GET" if body is None else "POST"))
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("description")
            except (ValueError, AttributeError):
                msg = None
            raise GatewayError(msg or f"Razorpay answered {e.code}")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise GatewayError(f"Razorpay could not be reached: {e}")

    def prepare(self, payment_id, amount, receipt):
        o = self._call("/orders", {"amount": int(amount) * 100, "currency": "INR", "receipt": receipt[:40],
                                   "notes": {"canteen_order": receipt, "payment": payment_id}})
        return {"rp_order_id": o["id"]}

    def charge(self, method, d, amount):
        raise GatewayError("Razorpay payments are taken in the Razorpay window.")

    def verify(self, rp_order_id, rp_payment_id, signature):
        """The check Razorpay documents for the student's return: HMAC-SHA256 of 'order_id|payment_id' with the key secret."""
        expected = hmac.new(self.key_secret.encode(), f"{rp_order_id}|{rp_payment_id}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature))

    def verify_webhook(self, raw_body, signature):
        """Razorpay signs the raw webhook body with the webhook secret and sends it in X-Razorpay-Signature."""
        if not self.webhook_secret:
            return False
        expected = hmac.new(self.webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature or ""))

    def settle(self, rp_payment_id, expected_order_id, amount):
        """Ask Razorpay what happened to this payment, and capture it if it is only authorised.
        Returns the payment as Razorpay reports it. Raises GatewayError if it is not a good payment for this order and amount."""
        pay = self._call(f"/payments/{rp_payment_id}")
        if pay.get("order_id") != expected_order_id:
            raise GatewayError("This payment belongs to a different order.")
        if int(pay.get("amount", -1)) != int(amount) * 100 or pay.get("currency", "INR") != "INR":
            raise GatewayError("The amount paid does not match the order.")
        if pay.get("status") == "authorized":
            try:
                pay = self._call(f"/payments/{rp_payment_id}/capture", {"amount": int(amount) * 100, "currency": "INR"})
            except GatewayError:      # the webhook and the student's return can both try to capture: look again before giving up
                pay = self._call(f"/payments/{rp_payment_id}")
        if pay.get("status") != "captured":
            raise GatewayError(f"The payment is {pay.get('status', 'not complete')}.")
        return pay

    def refund(self, payment_row, amount):
        r = self._call(f"/payments/{payment_row['ref']}/refund", {"amount": int(amount) * 100, "speed": "normal"})
        return {"ref": r.get("id", "rfnd")}

    @staticmethod
    def describe(pay):
        """A short label for how the student paid, from Razorpay's payment record (never the full card number)."""
        m = pay.get("method")
        if m == "upi" and pay.get("vpa"):
            return f"UPI · {mask_vpa(str(pay['vpa']).lower())}"
        if m == "card" and isinstance(pay.get("card"), dict):
            c = pay["card"]
            return f"{str(c.get('network') or 'Card').title()} •••• {c.get('last4', '')}".strip()
        if m == "netbanking" and pay.get("bank"):
            return f"Netbanking · {pay['bank']}"
        if m == "wallet" and pay.get("wallet"):
            return f"Wallet · {str(pay['wallet']).title()}"
        return "Online payment (Razorpay)"


def clean_upi_id(raw):
    """Return the UPI ID in lower case, or raise ValueError with a message for the owner."""
    v = str(raw or "").strip().lower()
    if not UPI_RE.match(v):
        raise ValueError("That does not look like a UPI ID. It looks like name@bank, for example vaibhav@okhdfcbank.")
    return v


def upi_params(vpa, name, amount, note):
    """Everything a phone needs to open a UPI app with the payee and amount already filled in."""
    q = urllib.parse.urlencode({"pa": vpa, "pn": name, "am": f"{int(amount):d}.00", "cu": "INR", "tn": note},
                           quote_via=lambda v, safe, enc, err: urllib.parse.quote(v, safe="@"))
    return {"pa": vpa, "pn": name, "am": f"{int(amount):d}.00", "tn": note,
            "link": "upi://pay?" + q,                      # Android: lets the person pick any UPI app
            "gpay": "gpay://upi/pay?" + q, "phonepe": "phonepe://pay?" + q, "paytm": "paytmmp://pay?" + q}   # iPhone: one link per app


class UpiGateway:
    kind = "upi"
    key_id = None
    is_test = False

    def prepare(self, payment_id, amount, receipt):
        return {}

    def charge(self, method, d, amount):
        raise GatewayError("UPI payments are confirmed by the kitchen.")

    def verify(self, *a, **k):
        raise GatewayError("Razorpay is not enabled.")

    def refund(self, payment_row, amount):
        return {"ref": None, "manual": True}     # nothing to call: the owner sends the money back from the UPI app


def load_env_file(path=None):
    """Read KEY=VALUE lines from a .env file next to app.py, so the owner does not have to set environment variables.
    Real environment variables win over the file. Lines starting with # are ignored."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    os.environ.setdefault(k, v)
    except OSError:
        pass


def get_gateway(demo=False):
    kid, sec = (os.environ.get("RAZORPAY_KEY_ID") or "").strip(), (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
    if kid and sec:
        return RazorpayGateway(kid, sec, (os.environ.get("RAZORPAY_WEBHOOK_SECRET") or "").strip())
    return TestGateway() if demo else UpiGateway()
