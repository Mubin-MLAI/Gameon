import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import Booking, Payment


logger = logging.getLogger(__name__)
MSG91_FLOW_URL = "https://control.msg91.com/api/v5/flow"


def _send_booking_email(booking: Booking) -> None:
    recipient = booking.customer.email.strip()
    if not recipient:
        logger.info("Booking %s has no customer email; email notice skipped", booking.booking_id)
        return

    local_start = timezone.localtime(booking.start_at)
    payments = Payment.objects.filter(booking=booking).order_by("received_at")
    payment_details = "\n".join(
        (
            f"- INR {payment.amount:.2f} via {payment.method} on "
            f"{timezone.localtime(payment.received_at):%d %b %Y, %I:%M %p}; "
            f"reference: {payment.reference or 'Not provided'}"
        )
        for payment in payments
    ) or "No payments recorded."
    body = (
        f"Hello {booking.customer.name},\n\n"
        f"Your booking {booking.booking_id} is confirmed.\n"
        f"Facility: {booking.facility.name}\n"
        f"Date: {local_start:%d %b %Y}\n"
        f"Time: {local_start:%I:%M %p}–{timezone.localtime(booking.end_at):%I:%M %p}\n"
        f"Total booking amount: INR {booking.total_amount:.2f}\n"
        f"Paid so far: INR {booking.advance_paid:.2f}\n"
        f"Balance due: INR {booking.balance_due:.2f}\n"
        f"Payment records:\n{payment_details}\n\n"
        "Sign in to your GameOn account and open My bookings to view or print your digital receipt."
    )
    try:
        sent = send_mail(
            subject=f"GameOn booking confirmed · {booking.booking_id}",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Email delivery failed for booking %s", booking.booking_id)
        return
    if sent != 1:
        logger.error("Email backend did not accept booking notice for %s", booking.booking_id)


def _normalized_mobile(mobile: str) -> str:
    digits = "".join(character for character in mobile if character.isdigit())
    country_code = "".join(character for character in settings.MSG91_COUNTRY_CODE if character.isdigit())
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if country_code and not digits.startswith(country_code):
        digits = f"{country_code}{digits}"
    return digits


def _send_booking_sms(booking: Booking) -> None:
    if not settings.MSG91_AUTH_KEY or not settings.MSG91_BOOKING_TEMPLATE_ID:
        logger.info("MSG91 is not configured; SMS notice skipped for booking %s", booking.booking_id)
        return
    mobile = _normalized_mobile(booking.customer.mobile)
    if not mobile:
        logger.error("Customer mobile is invalid for booking %s; SMS notice not sent", booking.booking_id)
        return

    local_start = timezone.localtime(booking.start_at)
    payload = {
        "template_id": settings.MSG91_BOOKING_TEMPLATE_ID,
        "recipients": [{
            "mobiles": mobile,
            "VAR1": booking.customer.name,
            "VAR2": booking.booking_id,
            "VAR3": booking.facility.name,
            "VAR4": local_start.strftime("%d %b %Y"),
            "VAR5": local_start.strftime("%I:%M %p"),
            "VAR6": f"{booking.advance_paid:.2f}",
            "VAR7": f"{booking.balance_due:.2f}",
        }],
    }
    request = Request(
        MSG91_FLOW_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "authkey": settings.MSG91_AUTH_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        logger.exception("MSG91 SMS delivery failed for booking %s", booking.booking_id)
        return
    if isinstance(result, dict) and str(result.get("type", "")).lower() == "error":
        logger.error("MSG91 rejected booking notice for %s: %s", booking.booking_id, result.get("message", "unknown error"))


def send_booking_notifications(booking_id: str) -> None:
    try:
        booking = Booking.objects.select_related("customer", "facility").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        logger.error("Cannot send booking notices: booking %s no longer exists", booking_id)
        return

    _send_booking_email(booking)
    _send_booking_sms(booking)
