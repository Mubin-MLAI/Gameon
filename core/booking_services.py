from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .customer_services import find_or_create_customer
from .models import AuditLog, Booking, Facility, MaintenanceBlock, Payment


class BookingConflict(Exception):
    pass


@transaction.atomic
def create_booking(
    *,
    facility_id,
    start_at,
    duration_minutes,
    customer_name,
    customer_mobile,
    customer_email="",
    source=Booking.Source.ONLINE,
    advance_paid=Decimal("0.00"),
    advance_required=Decimal("0.00"),
    status=Booking.Status.CONFIRMED,
    payment_expires_at=None,
    payment_method="",
    payment_reference="",
    notes="",
    created_by=None,
):
    facility = Facility.objects.select_for_update().get(pk=facility_id, is_active=True)
    if duration_minutes < facility.min_duration_minutes or duration_minutes > facility.max_duration_minutes:
        raise ValueError("Duration is outside this facility's booking limits.")

    end_at = start_at + timedelta(minutes=duration_minutes)
    if start_at.date() != end_at.date() or start_at.time() < facility.opening_time or end_at.time() > facility.closing_time:
        raise ValueError("The requested time is outside this facility's operating hours.")

    overlaps = Booking.objects.filter(
        facility=facility,
        start_at__lt=end_at,
        end_at__gt=start_at,
    ).filter(
        Q(status=Booking.Status.CONFIRMED)
        | Q(status=Booking.Status.PENDING_PAYMENT, payment_expires_at__gt=timezone.now())
    ).exists()
    blocked = MaintenanceBlock.objects.filter(
        facility=facility,
        start_at__lt=end_at,
        end_at__gt=start_at,
    ).exists()
    if overlaps or blocked:
        raise BookingConflict("That time is no longer available.")

    booking_value = facility.hourly_rate * Decimal(duration_minutes) / Decimal(60)
    if advance_paid < 0 or advance_paid > booking_value:
        raise ValueError("Advance payment must be between zero and the booking total.")
    if advance_paid and not payment_method:
        payment_method = Booking.PaymentMethod.CASH

    customer = find_or_create_customer(
        mobile=customer_mobile,
        name=customer_name,
        email=customer_email,
    )
    booking = Booking.objects.create(
        customer=customer,
        facility=facility,
        start_at=start_at,
        end_at=end_at,
        source=source,
        status=status,
        total_amount=booking_value,
        advance_required=advance_required,
        advance_paid=advance_paid,
        payment_expires_at=payment_expires_at,
        payment_method=payment_method,
        payment_reference=payment_reference,
        notes=notes,
        created_by=created_by,
    )
    if advance_paid:
        Payment.objects.create(
            booking=booking,
            customer=customer,
            amount=advance_paid,
            method=payment_method or Booking.PaymentMethod.CASH,
            reference=payment_reference,
            recorded_by=created_by,
        )
    AuditLog.objects.create(
        user=created_by,
        action="booking.created",
        entity="booking",
        record_id=booking.booking_id,
        new_value={"source": source, "facility": facility.name, "start_at": start_at.isoformat()},
    )
    return booking


@transaction.atomic
def create_maintenance_block(*, facility_id, start_at, end_at, reason, created_by=None):
    facility = Facility.objects.select_for_update().get(pk=facility_id, is_active=True)
    if end_at <= start_at:
        raise ValueError("End time must be after start time.")
    overlaps_booking = Booking.objects.filter(
        facility=facility,
        status=Booking.Status.CONFIRMED,
        start_at__lt=end_at,
        end_at__gt=start_at,
    ).exists()
    if overlaps_booking:
        raise BookingConflict("This time already has a confirmed booking.")
    return MaintenanceBlock.objects.create(
        facility=facility,
        start_at=start_at,
        end_at=end_at,
        reason=reason,
        created_by=created_by,
    )