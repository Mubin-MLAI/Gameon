from django.conf import settings
from django.db import transaction

from .models import Booking, Customer, Payment


def normalize_mobile(mobile):
    digits = "".join(character for character in mobile if character.isdigit())
    country_code = "".join(character for character in settings.MSG91_COUNTRY_CODE if character.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if country_code and digits.startswith(country_code) and len(digits) == len(country_code) + 10:
        digits = digits[len(country_code):]
    return digits


def find_customers_by_mobile(mobile):
    normalized = normalize_mobile(mobile)
    if not normalized:
        return []
    return [
        customer
        for customer in Customer.objects.all().only("id", "mobile", "user_id").order_by("id")
        if normalize_mobile(customer.mobile) == normalized
    ]


def reconcile_customer_records(customer):
    matching_customers = find_customers_by_mobile(customer.mobile)
    for duplicate in matching_customers:
        if duplicate.pk == customer.pk or duplicate.user_id:
            continue
        Booking.objects.filter(customer=duplicate).update(customer=customer)
        Payment.objects.filter(customer=duplicate).update(customer=customer)


def find_or_create_customer(*, mobile, name, email=""):
    matching_customers = find_customers_by_mobile(mobile)
    customer = next((item for item in matching_customers if item.user_id), None)
    if customer is None and matching_customers:
        customer = matching_customers[0]
    if customer is None:
        return Customer.objects.create(name=name, mobile=mobile, email=email)
    if not customer.name and name:
        customer.name = name
    if not customer.email and email:
        customer.email = email
    customer.save(update_fields=["name", "email"])
    return customer


@transaction.atomic
def get_or_link_customer_for_user(user, mobile):
    customer = getattr(user, "gameon_customer", None)
    matching_customers = find_customers_by_mobile(mobile)
    if customer is None:
        customer = next((item for item in matching_customers if item.user_id == user.pk), None)
    if customer is None:
        customer = next((item for item in matching_customers if item.user_id is None), None)
        if customer is None:
            return None
        customer.user = user
        customer.save(update_fields=["user"])
    reconcile_customer_records(customer)
    return customer
