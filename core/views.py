import csv
import json
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .booking_services import BookingConflict, create_booking, create_maintenance_block
from .customer_services import get_or_link_customer_for_user
from .forms import (
    AttendanceForm,
    CustomerBookingForm,
    ExpenseForm,
    MaintenanceBlockForm,
    OfflineBookingForm,
    PaymentForm,
    RegistrationForm,
)
from .models import AdvancePaymentSetting, AcademyBatch, Attendance, AuditLog, Booking, Customer, Expense, Facility, MaintenanceBlock, Payment, PaymentOrder, Student
from .payment_gateway import PaymentGatewayError, create_order, verify_payment_signature, verify_webhook_signature


logger = logging.getLogger(__name__)


def create_booking_payment_order(booking, amount=None):
    amount = booking.advance_required if amount is None else amount
    amount_paise = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    order_id = create_order(
        amount_paise=amount_paise,
        receipt=f"{booking.booking_id}-{uuid.uuid4().hex[:8]}",
    )
    with transaction.atomic():
        payment_order = PaymentOrder.objects.create(
            booking=booking,
            order_id=order_id,
            amount_paise=amount_paise,
        )
        booking.payment_order_id = order_id
        booking.save(update_fields=["payment_order_id"])
    return payment_order


def record_captured_payment(*, order_id, payment_id, amount_paise):
    payment_order = PaymentOrder.objects.filter(order_id=order_id).first()
    if not payment_order:
        raise PaymentGatewayError("Razorpay order is not associated with a booking.")
    if amount_paise != payment_order.amount_paise:
        raise PaymentGatewayError("Captured amount does not match the Razorpay order.")

    amount = Decimal(amount_paise) / Decimal(100)
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=payment_order.booking_id)
        existing_payment = Payment.objects.filter(reference=payment_id).first()
        if existing_payment:
            if existing_payment.booking_id != booking.pk or existing_payment.amount != amount:
                raise PaymentGatewayError("Payment ID is already associated with a different payment.")
            return booking
        if booking.payment_order_id != order_id:
            raise PaymentGatewayError("This Razorpay order is no longer active.")
        if booking.status == Booking.Status.PENDING_PAYMENT:
            if amount not in (booking.advance_required, booking.total_amount):
                raise PaymentGatewayError("Captured amount does not match the advance or booking total.")
        elif booking.status == Booking.Status.CONFIRMED:
            if amount <= 0 or amount > booking.balance_due:
                raise PaymentGatewayError("Captured amount exceeds the outstanding booking balance.")
        else:
            raise PaymentGatewayError("This booking cannot accept a payment.")

        Payment.objects.create(
            booking=booking,
            customer=booking.customer,
            amount=amount,
            method=Booking.PaymentMethod.ONLINE,
            reference=payment_id,
        )
        booking.advance_paid += amount
        booking.payment_method = Booking.PaymentMethod.ONLINE
        booking.payment_reference = payment_id
        update_fields = ["advance_paid", "payment_method", "payment_reference"]
        if booking.status == Booking.Status.PENDING_PAYMENT:
            booking.status = Booking.Status.CONFIRMED
            update_fields.append("status")
        booking.save(update_fields=update_fields)
        AuditLog.objects.create(
            action="payment.recorded",
            entity="booking",
            record_id=booking.booking_id,
            new_value={"amount": str(amount), "method": Booking.PaymentMethod.ONLINE, "source": "razorpay"},
        )
    return booking


def get_customer_for_user(user):
    if not user or not user.is_authenticated:
        return None

    mobile = getattr(user, "username", "")
    if not mobile:
        return None

    return get_or_link_customer_for_user(user, mobile)


def home(request):
    facilities = Facility.objects.filter(is_active=True)
    upcoming = []
    customer = get_customer_for_user(request.user) if request.user.is_authenticated else None
    if customer:
        upcoming = customer.bookings.filter(status=Booking.Status.CONFIRMED, start_at__gte=timezone.now())[:3]
    return render(request, "core/home.html", {"facilities": facilities, "upcoming": upcoming})


def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            user.username = form.cleaned_data["mobile"]
            user.email = form.cleaned_data["email"]
            user.first_name = form.cleaned_data["name"]
            user.save()
            customer = get_or_link_customer_for_user(
                user,
                mobile=form.cleaned_data["mobile"],
            )
            if customer is None:
                customer = Customer.objects.create(
                    user=user,
                    name=form.cleaned_data["name"],
                    mobile=form.cleaned_data["mobile"],
                    email=form.cleaned_data["email"],
                )
            customer.user = user
            customer.name = form.cleaned_data["name"]
            customer.email = form.cleaned_data["email"]
            customer.save(update_fields=["user", "name", "email"])
        login(request, user)
        return redirect("home")
    return render(request, "core/form_page.html", {"form": form, "title": "Create your account", "eyebrow": "PLAYER ACCOUNT"})


@login_required
def book_facility(request):
    customer = get_customer_for_user(request.user)
    if not customer:
        if request.user.is_staff:
            messages.error(request, "Staff accounts do not book facilities from the customer portal.")
            return redirect("dashboard")
        customer = Customer.objects.create(
            user=request.user,
            name=request.user.get_full_name() or request.user.username,
            mobile=request.user.username,
            email=request.user.email or "",
        )

    form = CustomerBookingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        advance_setting = AdvancePaymentSetting.get_solo()
        facility = form.cleaned_data["facility"]
        booking_total = (
            facility.hourly_rate * Decimal(form.cleaned_data["duration_minutes"]) / Decimal(60)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        advance_required = advance_setting.calculate_amount(booking_total)
        payment_expires_at = timezone.now() + timedelta(minutes=15)
        try:
            booking = create_booking(
                facility_id=facility.pk,
                start_at=form.cleaned_data["start_at"],
                duration_minutes=form.cleaned_data["duration_minutes"],
                customer_name=customer.name,
                customer_mobile=customer.mobile,
                customer_email=customer.email,
                source=Booking.Source.ONLINE,
                status=Booking.Status.PENDING_PAYMENT if advance_required else Booking.Status.CONFIRMED,
                advance_required=advance_required,
                payment_expires_at=payment_expires_at if advance_required else None,
            )
        except (BookingConflict, ValueError) as error:
            form.add_error(None, str(error))
        else:
            if advance_required:
                try:
                    create_booking_payment_order(booking)
                except PaymentGatewayError as error:
                    messages.error(request, str(error))
                    return redirect("booking_history")
                return redirect("booking_payment", booking_id=booking.booking_id)
            return redirect("booking_confirmation", booking_id=booking.booking_id)
    return render(request, "core/form_page.html", {"form": form, "title": "Reserve a slot", "eyebrow": "ONLINE BOOKING"})


@login_required
def booking_confirmation(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id, customer__user=request.user)
    if booking.status == Booking.Status.PENDING_PAYMENT:
        return redirect("booking_payment", booking_id=booking.booking_id)
    return render(request, "core/booking_confirmation.html", {"booking": booking})


@login_required
def booking_history(request):
    customer = get_customer_for_user(request.user)
    bookings = customer.bookings.select_related("facility").all() if customer else Booking.objects.none()
    return render(request, "core/booking_history.html", {"bookings": bookings})


@login_required
def customer_payment_history(request):
    customer = get_customer_for_user(request.user)
    payments = (
        Payment.objects.filter(customer=customer)
        .select_related("booking", "booking__facility", "recorded_by")
        .order_by("-received_at")
        if customer
        else Payment.objects.none()
    )
    return render(request, "core/payment_history.html", {"payments": payments})


@login_required
def booking_notifications(request):
    customer = get_customer_for_user(request.user)
    notifications = []
    return render(request, "core/booking_notifications.html", {"notifications": notifications, "customer": customer})


@login_required
def booking_payment(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id, customer__user=request.user)
    if booking.status != Booking.Status.PENDING_PAYMENT:
        return redirect("booking_confirmation", booking_id=booking.booking_id)
    if booking.payment_expires_at and booking.payment_expires_at <= timezone.now():
        return render(request, "core/razorpay_checkout.html", {
            "booking": booking,
            "advance": booking.advance_required,
            "balance_due": booking.total_amount - booking.advance_required,
            "payment_error": "This payment hold has expired. Please book another available time.",
        })

    payment_order = PaymentOrder.objects.filter(booking=booking).order_by("-created_at", "-pk").first()
    if not payment_order or payment_order.order_id != booking.payment_order_id:
        try:
            payment_order = create_booking_payment_order(booking)
        except PaymentGatewayError as error:
            return render(request, "core/razorpay_checkout.html", {
                "booking": booking,
                "advance": booking.advance_required,
                "balance_due": booking.total_amount - booking.advance_required,
                "payment_error": str(error),
            })

    checkout_options = {
        "key": settings.RAZORPAY_KEY_ID,
        "amount": payment_order.amount_paise,
        "currency": "INR",
        "name": "GameOn",
        "description": f"Booking {booking.booking_id}",
        "order_id": payment_order.order_id,
        "prefill": {
            "name": booking.customer.name,
            "email": booking.customer.email,
            "contact": booking.customer.mobile,
        },
    }
    return render(request, "core/razorpay_checkout.html", {
        "booking": booking,
        "advance": booking.advance_required,
        "balance_due": booking.total_amount - booking.advance_required,
        "checkout_type": "advance",
        "payment_amount": booking.advance_required,
        "checkout_options": checkout_options,
        "verify_url": reverse("verify_razorpay_payment", args=[booking.booking_id]),
        "order_url": reverse("retry_razorpay_order", args=[booking.booking_id]),
    })


@login_required
def booking_balance_payment(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id, customer__user=request.user)
    if booking.status != Booking.Status.CONFIRMED or booking.balance_due <= 0:
        return redirect("booking_confirmation", booking_id=booking.booking_id)

    amount_paise = int((booking.balance_due * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    payment_order = PaymentOrder.objects.filter(
        booking=booking,
        order_id=booking.payment_order_id,
        amount_paise=amount_paise,
    ).first()
    if not payment_order:
        try:
            payment_order = create_booking_payment_order(booking, amount=booking.balance_due)
        except PaymentGatewayError as error:
            return render(request, "core/razorpay_checkout.html", {
                "booking": booking,
                "advance": booking.advance_paid,
                "balance_due": booking.balance_due,
                "checkout_type": "balance",
                "payment_amount": booking.balance_due,
                "payment_error": str(error),
            })

    checkout_options = {
        "key": settings.RAZORPAY_KEY_ID,
        "amount": payment_order.amount_paise,
        "currency": "INR",
        "name": "GameOn",
        "description": f"Booking balance {booking.booking_id}",
        "order_id": payment_order.order_id,
        "prefill": {
            "name": booking.customer.name,
            "email": booking.customer.email,
            "contact": booking.customer.mobile,
        },
    }
    return render(request, "core/razorpay_checkout.html", {
        "booking": booking,
        "advance": booking.advance_paid,
        "balance_due": booking.balance_due,
        "checkout_type": "balance",
        "payment_amount": booking.balance_due,
        "checkout_options": checkout_options,
        "verify_url": reverse("verify_razorpay_payment", args=[booking.booking_id]),
        "order_url": reverse("retry_razorpay_balance_order", args=[booking.booking_id]),
    })


@login_required
def verify_razorpay_payment(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id, customer__user=request.user)
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    order_id = request.POST.get("razorpay_order_id", "")
    payment_id = request.POST.get("razorpay_payment_id", "")
    signature = request.POST.get("razorpay_signature", "")
    payment_order = PaymentOrder.objects.filter(booking=booking, order_id=order_id).first()
    if (
        not payment_id
        or not signature
        or not payment_order
        or booking.payment_order_id != order_id
        or not verify_payment_signature(order_id=order_id, payment_id=payment_id, signature=signature)
    ):
        return JsonResponse({"error": "Payment could not be verified."}, status=400)

    try:
        booking = record_captured_payment(
            order_id=order_id,
            payment_id=payment_id,
            amount_paise=payment_order.amount_paise,
        )
    except PaymentGatewayError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({
        "status": "ok",
        "redirect_url": reverse("booking_confirmation", args=[booking.booking_id]),
    })


@login_required
def retry_razorpay_order(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id, customer__user=request.user)
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if booking.status != Booking.Status.PENDING_PAYMENT:
        return JsonResponse({"error": "This booking is no longer awaiting payment."}, status=400)
    if booking.payment_expires_at and booking.payment_expires_at <= timezone.now():
        return JsonResponse({"error": "This payment hold has expired. Please book another available time."}, status=400)
    payment_type = request.POST.get("payment_type", "advance")
    if payment_type == "advance":
        amount = booking.advance_required
    elif payment_type == "full":
        amount = booking.total_amount
    else:
        return JsonResponse({"error": "Choose the advance or full booking amount."}, status=400)
    try:
        payment_order = create_booking_payment_order(booking, amount=amount)
    except PaymentGatewayError as error:
        return JsonResponse({"error": str(error)}, status=502)
    return JsonResponse({
        "order_id": payment_order.order_id,
        "amount_paise": payment_order.amount_paise,
    })


@login_required
def retry_razorpay_balance_order(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id, customer__user=request.user)
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if booking.status != Booking.Status.CONFIRMED or booking.balance_due <= 0:
        return JsonResponse({"error": "This booking has no balance awaiting payment."}, status=400)
    try:
        amount = Decimal(request.POST.get("amount", ""))
    except (ArithmeticError, ValueError):
        return JsonResponse({"error": "Enter a valid payment amount."}, status=400)
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > booking.balance_due
        or amount != amount.quantize(Decimal("0.01"))
    ):
        return JsonResponse({"error": "Payment must be a positive amount up to the outstanding balance, with at most two decimal places."}, status=400)
    try:
        payment_order = create_booking_payment_order(booking, amount=amount)
    except PaymentGatewayError as error:
        return JsonResponse({"error": str(error)}, status=502)
    return JsonResponse({
        "order_id": payment_order.order_id,
        "amount_paise": payment_order.amount_paise,
    })


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body=request.body, signature=signature):
        return JsonResponse({"error": "Invalid webhook signature."}, status=400)

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid webhook payload."}, status=400)

    event = payload.get("event")
    if event != "payment.captured":
        return JsonResponse({"status": "ignored", "event": event}, status=200)

    event_payload = payload.get("payload")
    payment_payload = event_payload.get("payment") if isinstance(event_payload, dict) else None
    payment = payment_payload.get("entity") if isinstance(payment_payload, dict) else None
    if not isinstance(payment, dict):
        return JsonResponse({"error": "Missing captured payment details."}, status=400)
    order_id = payment.get("order_id")
    payment_id = payment.get("id")
    amount_paise = payment.get("amount")
    if (
        not isinstance(order_id, str)
        or not isinstance(payment_id, str)
        or not isinstance(amount_paise, int)
        or isinstance(amount_paise, bool)
        or payment.get("currency") != "INR"
        or payment.get("status") != "captured"
    ):
        return JsonResponse({"error": "Invalid captured payment details."}, status=400)

    try:
        booking = record_captured_payment(
            order_id=order_id,
            payment_id=payment_id,
            amount_paise=amount_paise,
        )
    except PaymentGatewayError as error:
        logger.error("Ignoring invalid Razorpay captured-payment webhook: %s", error)
        return JsonResponse({"error": str(error)}, status=400)

    return JsonResponse({"status": "ok", "booking_id": booking.booking_id})


def parse_booking_date(raw_date):
    if not raw_date:
        raise ValueError("No date provided")

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw_date, fmt).date()
        except ValueError:
            continue

    raise ValueError("Unsupported date format")


def availability(request):
    try:
        facility = Facility.objects.get(pk=request.GET.get("facility"), is_active=True)
        selected_date = parse_booking_date(request.GET.get("date", ""))
        duration_minutes = int(request.GET.get("duration", facility.min_duration_minutes))
        if not facility.min_duration_minutes <= duration_minutes <= facility.max_duration_minutes:
            raise ValueError
    except (Facility.DoesNotExist, TypeError, ValueError):
        return JsonResponse({"slots": []})
    slots = []
    cursor = datetime.combine(selected_date, facility.opening_time)
    closing = datetime.combine(selected_date, facility.closing_time)
    while cursor + timedelta(minutes=duration_minutes) <= closing:
        start_at = timezone.make_aware(cursor)
        end_at = start_at + timedelta(minutes=duration_minutes)
        occupied = start_at <= timezone.now() or Booking.objects.filter(
            facility=facility,
            start_at__lt=end_at,
            end_at__gt=start_at,
        ).filter(
            Q(status=Booking.Status.CONFIRMED)
            | Q(status=Booking.Status.PENDING_PAYMENT, payment_expires_at__gt=timezone.now())
        ).exists() or MaintenanceBlock.objects.filter(
            facility=facility,
            start_at__lt=end_at,
            end_at__gt=start_at,
        ).exists()
        slots.append({"start": cursor.strftime("%H:%M"), "available": not occupied})
        cursor += timedelta(minutes=facility.min_duration_minutes)
    return JsonResponse({"slots": slots})


def staff_required(view):
    return user_passes_test(lambda user: user.is_authenticated and user.is_staff, login_url="login")(view)


@staff_required
def dashboard(request):
    today = timezone.localdate()
    day_start = timezone.make_aware(datetime.combine(today, time.min))
    day_end = day_start + timedelta(days=1)
    bookings = Booking.objects.filter(start_at__lt=day_end, end_at__gt=day_start)
    payments = Payment.objects.filter(received_at__gte=day_start, received_at__lt=day_end)
    expenses = Expense.objects.filter(spent_at__gte=day_start, spent_at__lt=day_end)
    context = {
        "today": today,
        "bookings": bookings.select_related("facility", "customer").order_by("start_at"),
        "booking_count": bookings.filter(status=Booking.Status.CONFIRMED).count(),
        "online_count": bookings.filter(source=Booking.Source.ONLINE).count(),
        "offline_count": bookings.filter(source=Booking.Source.OFFLINE).count(),
        "revenue": payments.filter(direction=Payment.Direction.RECEIVED).aggregate(total=Sum("amount"))["total"] or Decimal("0"),
        "expenses_total": expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0"),
        "pending": Booking.objects.filter(status=Booking.Status.CONFIRMED).aggregate(total=Sum("total_amount"))["total"] or Decimal("0"),
        "facilities": Facility.objects.filter(is_active=True).count(),
        "students": Student.objects.filter(status=Student.Status.ACTIVE).count(),
    }
    return render(request, "core/dashboard.html", context)


@staff_required
def staff_calendar(request):
    raw_date = request.GET.get("date", "")
    try:
        selected_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        selected_date = timezone.localdate()
    start_at = timezone.make_aware(datetime.combine(selected_date, time.min))
    end_at = start_at + timedelta(days=1)
    return render(request, "core/calendar.html", {
        "selected_date": selected_date,
        "facilities": Facility.objects.filter(is_active=True),
        "bookings": Booking.objects.filter(start_at__lt=end_at, end_at__gt=start_at).select_related("facility", "customer"),
        "blocks": MaintenanceBlock.objects.filter(start_at__lt=end_at, end_at__gt=start_at).select_related("facility"),
    })


@staff_required
def offline_booking(request):
    form = OfflineBookingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            booking = create_booking(
                facility_id=data["facility"].pk,
                start_at=data["start_at"],
                duration_minutes=data["duration_minutes"],
                customer_name=data["customer_name"],
                customer_mobile=data["customer_mobile"],
                customer_email=data["customer_email"],
                source=Booking.Source.OFFLINE,
                advance_paid=data["advance_paid"],
                payment_method=data["payment_method"],
                payment_reference=data["payment_reference"],
                notes=data["notes"],
                created_by=request.user,
            )
        except (BookingConflict, ValueError) as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f"Offline booking {booking.booking_id} created.")
            return redirect("calendar")
    return render(request, "core/form_page.html", {"form": form, "title": "Create offline booking", "eyebrow": "FRONT DESK"})


@staff_required
def create_block(request):
    form = MaintenanceBlockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            block = create_maintenance_block(
                facility_id=data["facility"].pk,
                start_at=data["start_at"],
                end_at=data["end_at"],
                reason=data["reason"],
                created_by=request.user,
            )
        except (BookingConflict, ValueError) as error:
            form.add_error(None, str(error))
            return render(request, "core/form_page.html", {"form": form, "title": "Block facility time", "eyebrow": "MAINTENANCE"})
        AuditLog.objects.create(user=request.user, action="facility.blocked", entity="maintenance", record_id=str(block.pk), new_value={"reason": block.reason})
        messages.success(request, "Unavailable time added to the live calendar.")
        return redirect("calendar")
    return render(request, "core/form_page.html", {"form": form, "title": "Block facility time", "eyebrow": "MAINTENANCE"})


@staff_required
def release_block(request, block_id):
    block = get_object_or_404(MaintenanceBlock, pk=block_id)
    if request.method == "POST":
        AuditLog.objects.create(user=request.user, action="facility.block_released", entity="maintenance", record_id=str(block.pk), previous_value={"reason": block.reason})
        block.delete()
        messages.success(request, "Facility time released.")
    return redirect("calendar")


@staff_required
def cancel_booking(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id)
    if request.method == "POST" and booking.status == Booking.Status.CONFIRMED:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])
        AuditLog.objects.create(user=request.user, action="booking.cancelled", entity="booking", record_id=booking.booking_id, previous_value={"status": Booking.Status.CONFIRMED}, new_value={"status": Booking.Status.CANCELLED})
        messages.success(request, f"Booking {booking.booking_id} cancelled.")
    return redirect("calendar")


@staff_required
def collect_payment(request, booking_id):
    booking = get_object_or_404(Booking, booking_id=booking_id)
    form = PaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
            if booking.status != Booking.Status.CONFIRMED:
                form.add_error(None, "Payments can only be recorded for confirmed bookings.")
            elif data["amount"] > booking.balance_due:
                form.add_error("amount", "Amount cannot exceed the outstanding balance.")
            else:
                Payment.objects.create(booking=booking, customer=booking.customer, amount=data["amount"], method=data["method"], reference=data["reference"], recorded_by=request.user)
                booking.advance_paid += data["amount"]
                booking.payment_method = data["method"]
                booking.payment_reference = data["reference"]
                booking.save(update_fields=["advance_paid", "payment_method", "payment_reference"])
                AuditLog.objects.create(user=request.user, action="payment.recorded", entity="booking", record_id=booking.booking_id, new_value={"amount": str(data["amount"]), "method": data["method"]})
                messages.success(request, "Payment recorded.")
                return redirect("calendar")
    return render(request, "core/form_page.html", {"form": form, "title": f"Collect payment · {booking.booking_id}", "eyebrow": "BOOKING PAYMENT", "balance": booking.balance_due})


@staff_required
def expenses(request):
    form = ExpenseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        expense = form.save(commit=False)
        expense.entered_by = request.user
        expense.save()
        AuditLog.objects.create(user=request.user, action="expense.created", entity="expense", record_id=str(expense.pk), new_value={"amount": str(expense.amount), "category": expense.category})
        messages.success(request, "Expense recorded.")
        return redirect("expenses")
    rows = Expense.objects.order_by("-spent_at")[:30]
    return render(request, "core/expenses.html", {"form": form, "expenses": rows})


@staff_required
def academy(request):
    students = Student.objects.select_related("batch").filter(status=Student.Status.ACTIVE)
    batches = AcademyBatch.objects.filter(is_active=True)
    attendance_date = timezone.localdate()
    if request.method == "POST":
        form = AttendanceForm(request.POST)
        if form.is_valid():
            student = get_object_or_404(Student, pk=form.cleaned_data["student_id"])
            Attendance.objects.update_or_create(
                student=student,
                date=form.cleaned_data["date"],
                defaults={"status": form.cleaned_data["status"], "marked_by": request.user},
            )
            messages.success(request, f"Attendance saved for {student.name}.")
            return redirect("academy")
    return render(request, "core/academy.html", {"students": students, "batches": batches, "attendance_date": attendance_date})


@staff_required
def monthly_report(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
        start = datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.get_current_timezone())
    except (TypeError, ValueError):
        return HttpResponse("Invalid month", status=400)
    payments = Payment.objects.filter(received_at__gte=start, received_at__lt=end, direction=Payment.Direction.RECEIVED)
    expense_rows = Expense.objects.filter(spent_at__gte=start, spent_at__lt=end)
    booking_rows = Booking.objects.filter(start_at__gte=start, start_at__lt=end)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="gameon-report-{year}-{month:02}.csv"'
        writer = csv.writer(response)
        writer.writerow(["GameOn monthly report", f"{year}-{month:02}"])
        writer.writerow(["Bookings", booking_rows.count()])
        writer.writerow(["Online bookings", booking_rows.filter(source=Booking.Source.ONLINE).count()])
        writer.writerow(["Offline bookings", booking_rows.filter(source=Booking.Source.OFFLINE).count()])
        writer.writerow(["Collections", payments.aggregate(total=Sum("amount"))["total"] or 0])
        writer.writerow(["Expenses", expense_rows.aggregate(total=Sum("amount"))["total"] or 0])
        writer.writerow([])
        writer.writerow(["Payment method", "Amount"])
        for method, label in Booking.PaymentMethod.choices:
            writer.writerow([label, payments.filter(method=method).aggregate(total=Sum("amount"))["total"] or 0])
        writer.writerow([])
        writer.writerow(["Expense category", "Amount"])
        for category, label in Expense.Category.choices:
            writer.writerow([label, expense_rows.filter(category=category).aggregate(total=Sum("amount"))["total"] or 0])
        return response
    context = {
        "year": year,
        "month": month,
        "bookings": booking_rows.count(),
        "online": booking_rows.filter(source=Booking.Source.ONLINE).count(),
        "offline": booking_rows.filter(source=Booking.Source.OFFLINE).count(),
        "cancelled": booking_rows.filter(status=Booking.Status.CANCELLED).count(),
        "collections": payments.aggregate(total=Sum("amount"))["total"] or Decimal("0"),
        "expenses": expense_rows.aggregate(total=Sum("amount"))["total"] or Decimal("0"),
        "outstanding": booking_rows.filter(status=Booking.Status.CONFIRMED).aggregate(total=Sum(F("total_amount") - F("advance_paid")))["total"] or Decimal("0"),
    }
    context["net"] = context["collections"] - context["expenses"]
    return render(request, "core/report.html", context)
