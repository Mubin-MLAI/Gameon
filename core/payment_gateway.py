import base64
import hashlib
import hmac
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


logger = logging.getLogger(__name__)
RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"


class PaymentGatewayError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def create_order(*, amount_paise: int, receipt: str) -> str:
    if not is_configured():
        raise PaymentGatewayError("Online payments are not configured. Please contact the facility.")

    credentials = f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()
    authorization = base64.b64encode(credentials).decode("ascii")
    payload = json.dumps({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
    }).encode("utf-8")
    request = Request(
        RAZORPAY_ORDERS_URL,
        data=payload,
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        logger.error("Razorpay order creation failed with HTTP %s: %s", error.code, response_text)
        raise PaymentGatewayError("Could not start the online payment. Please try again.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        logger.exception("Could not create Razorpay order")
        raise PaymentGatewayError("Could not connect to the payment service. Please try again.") from error

    if not isinstance(result, dict):
        logger.error("Razorpay order response was not a JSON object")
        raise PaymentGatewayError("The payment service returned an invalid response. Please try again.")
    order_id = result.get("id")
    if not isinstance(order_id, str) or not order_id:
        logger.error("Razorpay order response did not include an order ID")
        raise PaymentGatewayError("The payment service returned an invalid response. Please try again.")
    return order_id


def verify_payment_signature(*, order_id: str, payment_id: str, signature: str) -> bool:
    if not is_configured():
        return False
    message = f"{order_id}|{payment_id}".encode("utf-8")
    expected_signature = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)


def verify_webhook_signature(*, body: bytes, signature: str) -> bool:
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not webhook_secret or not signature:
        return False
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)
