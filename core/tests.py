from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from .booking_services import BookingConflict, create_booking, create_maintenance_block
from .forms import RegistrationForm
from .models import Booking, Customer, Facility, MaintenanceBlock, Payment, PaymentOrder


class BookingAvailabilityTests(TestCase):
	def setUp(self):
		self.facility = Facility.objects.create(
			name="Football Ground",
			sport=Facility.Sport.FOOTBALL,
			hourly_rate=Decimal("3000.00"),
		)
		self.start_at = timezone.make_aware(datetime(2026, 10, 6, 18, 0))
		self.booking_data = {
			"facility_id": self.facility.pk,
			"start_at": self.start_at,
			"duration_minutes": 60,
			"customer_name": "Asha Rao",
			"customer_mobile": "9876543210",
		}

	def test_overlapping_booking_is_rejected(self):
		created = create_booking(**self.booking_data)

		with self.assertRaises(BookingConflict):
			create_booking(
				**{**self.booking_data, "start_at": self.start_at + timedelta(minutes=30), "customer_mobile": "9876543211"}
			)

		self.assertEqual(created.total_amount, Decimal("3000.00"))

	def test_maintenance_block_rejects_booking(self):
		MaintenanceBlock.objects.create(
			facility=self.facility,
			start_at=self.start_at,
			end_at=self.start_at + timedelta(hours=1),
			reason="Pitch maintenance",
		)

		with self.assertRaises(BookingConflict):
			create_booking(**self.booking_data)

	def test_maintenance_block_cannot_overlap_a_confirmed_booking(self):
		create_booking(**self.booking_data)

		with self.assertRaises(BookingConflict):
			create_maintenance_block(
				facility_id=self.facility.pk,
				start_at=self.start_at,
				end_at=self.start_at + timedelta(hours=1),
				reason="Maintenance",
			)

	def test_elapsed_slots_are_not_available(self):
		response = self.client.get(reverse("availability"), {
			"facility": self.facility.pk,
			"date": timezone.localdate().isoformat(),
			"duration": 60,
		})
		slot = next(slot for slot in response.json()["slots"] if slot["start"] == "05:00")
		self.assertFalse(slot["available"])


class OfflineBookingSyncTests(TestCase):
	def setUp(self):
		self.facility = Facility.objects.create(
			name="Football Ground",
			sport=Facility.Sport.FOOTBALL,
			hourly_rate=Decimal("3000.00"),
		)
		self.staff = User.objects.create_user(username="frontdesk", password="not-a-real-secret", is_staff=True)
		self.client.force_login(self.staff)

	def test_offline_booking_reserves_slot_for_customer_availability(self):
		response = self.client.post(reverse("offline_booking"), {
			"facility": self.facility.pk,
			"start_at": "2026-10-06T18:00",
			"duration_minutes": 60,
			"customer_name": "Asha Rao",
			"customer_mobile": "9876543210",
			"customer_email": "",
			"advance_paid": "500.00",
			"payment_method": Booking.PaymentMethod.UPI,
			"payment_reference": "UPI-123",
			"notes": "Walk-in",
		})

		self.assertRedirects(response, reverse("calendar"))
		booking = Booking.objects.get(source=Booking.Source.OFFLINE)
		self.assertEqual(booking.advance_paid, Decimal("500.00"))
		self.assertEqual(Payment.objects.get(booking=booking).amount, Decimal("500.00"))
		availability = self.client.get(reverse("availability"), {
			"facility": self.facility.pk,
			"date": "2026-10-06",
			"duration": 60,
		})
		slot = next(slot for slot in availability.json()["slots"] if slot["start"] == "18:00")
		self.assertFalse(slot["available"])

	def test_offline_booking_form_shows_availability_before_customer_details(self):
		response = self.client.get(reverse("offline_booking"))

		self.assertEqual(response.status_code, 200)
		content = response.content.decode()
		positions = [
			content.index(">Facility</label>"),
			content.index('for="booking-date">Date</label>'),
			content.index("AVAILABLE START TIMES"),
			content.index(">Duration minutes</label>"),
			content.index(">Customer name</label>"),
		]
		self.assertEqual(positions, sorted(positions))
		self.assertContains(response, 'id="availability-slots"')
		self.assertContains(response, "availability/?")

	def test_offline_booking_with_formatted_mobile_appears_in_customer_account(self):
		customer_user = User.objects.create_user(username="9876500123")
		customer = Customer.objects.create(
			user=customer_user,
			name="Linked Customer",
			mobile="9876500123",
		)
		start_at = timezone.localtime(timezone.now() + timedelta(days=2)).replace(
			hour=18, minute=0, second=0, microsecond=0
		)

		response = self.client.post(reverse("offline_booking"), {
			"facility": self.facility.pk,
			"start_at": start_at.strftime("%Y-%m-%dT%H:%M"),
			"duration_minutes": 60,
			"customer_name": "Linked Customer",
			"customer_mobile": "+91 98765 00123",
			"customer_email": "",
			"advance_paid": "500.00",
			"payment_method": Booking.PaymentMethod.UPI,
			"payment_reference": "UPI-OFFLINE-500",
			"notes": "",
		})

		self.assertRedirects(response, reverse("calendar"))
		booking = Booking.objects.get(customer=customer)
		self.assertEqual(booking.source, Booking.Source.OFFLINE)
		self.assertEqual(booking.payments.get().reference, "UPI-OFFLINE-500")
		self.client.force_login(customer_user)
		bookings_response = self.client.get(reverse("booking_history"))
		payments_response = self.client.get(reverse("customer_payment_history"))
		self.assertContains(bookings_response, booking.booking_id)
		self.assertContains(payments_response, booking.booking_id)
		self.assertContains(payments_response, "UPI-OFFLINE-500")

	def test_customer_pages_render(self):
		self.client.logout()
		self.assertEqual(self.client.get(reverse("register")).status_code, 200)
		customer = User.objects.create_user(username="9876500000", password="not-a-real-secret")
		from .models import Customer
		Customer.objects.create(user=customer, name="Customer", mobile="9876500000")
		self.client.force_login(customer)
		self.assertEqual(self.client.get(reverse("book_facility")).status_code, 200)

	def test_staff_recorded_payment_is_visible_in_admin_and_customer_history(self):
		customer_user = User.objects.create_user(username="9876500022")
		customer = Customer.objects.create(
			user=customer_user,
			name="Payment History Customer",
			mobile=customer_user.username,
		)
		now = timezone.now()
		booking = Booking.objects.create(
			customer=customer,
			facility=self.facility,
			start_at=now + timedelta(days=1),
			end_at=now + timedelta(days=1, hours=1),
			source=Booking.Source.ONLINE,
			status=Booking.Status.CONFIRMED,
			total_amount=Decimal("3000.00"),
		)

		response = self.client.post(reverse("collect_payment", args=[booking.booking_id]), {
			"amount": "750.00",
			"method": Booking.PaymentMethod.UPI,
			"reference": "UPI-STAFF-750",
		})

		self.assertRedirects(response, reverse("calendar"))
		payment = Payment.objects.get(booking=booking)
		self.assertEqual(payment.customer, customer)
		self.assertEqual(payment.recorded_by, self.staff)
		booking.refresh_from_db()
		self.assertEqual(booking.advance_paid, Decimal("750.00"))
		self.assertEqual(booking.payment_method, Booking.PaymentMethod.UPI)
		self.assertEqual(booking.payment_reference, "UPI-STAFF-750")

		admin_user = User.objects.create_superuser(username="payment-admin", email="admin@example.com")
		self.client.force_login(admin_user)
		admin_response = self.client.get(reverse("admin:core_payment_changelist"))
		self.assertContains(admin_response, "UPI-STAFF-750")

		self.client.force_login(customer_user)
		history_response = self.client.get(reverse("customer_payment_history"))
		self.assertContains(history_response, booking.booking_id)
		self.assertContains(history_response, "UPI-STAFF-750")
		self.assertContains(history_response, "₹750.00")

	def test_online_payment_history_handles_missing_recorded_by(self):
		customer_user = User.objects.create_user(username="9876500023")
		customer = Customer.objects.create(
			user=customer_user,
			name="Online Payment Customer",
			mobile=customer_user.username,
		)
		Payment.objects.create(
			customer=customer,
			amount=Decimal("300.00"),
			method=Booking.PaymentMethod.ONLINE,
			reference="pay_online_300",
		)
		self.client.force_login(customer_user)

		response = self.client.get(reverse("customer_payment_history"))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "pay_online_300")
		self.assertContains(response, "Online")

	def test_signup_rejects_mobile_already_used_for_login(self):
		User.objects.create_user(username="9000000002", password="not-a-real-secret")
		form = RegistrationForm(data={
			"name": "Another Player",
			"mobile": "9000000002",
			"email": "",
			"password1": "GameOnDemo2026!",
			"password2": "GameOnDemo2026!",
		})

		self.assertFalse(form.is_valid())
		self.assertIn("mobile", form.errors)

	def test_booking_history_for_user_without_customer_renders_empty_state(self):
		self.client.logout()
		plain_user = User.objects.create_user(username="plainuser", password="not-a-real-secret")
		self.client.force_login(plain_user)
		response = self.client.get(reverse("booking_history"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "No bookings yet.")

	def test_booking_history_finds_customer_by_mobile_when_user_link_is_missing(self):
		self.client.logout()
		phone = "9876500123"
		user = User.objects.create_user(username=phone, password="not-a-real-secret")
		customer = Customer.objects.create(name="Mobile Customer", mobile=phone, email="")
		facility = Facility.objects.create(name="Customer Field", sport=Facility.Sport.FOOTBALL, hourly_rate=Decimal("2000.00"))
		Booking.objects.create(
			customer=customer,
			facility=facility,
			start_at=timezone.now() + timedelta(hours=2),
			end_at=timezone.now() + timedelta(hours=3),
			source=Booking.Source.ONLINE,
			total_amount=Decimal("2000.00"),
		)
		self.client.force_login(user)
		response = self.client.get(reverse("booking_history"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Customer Field")

	def test_booking_history_links_formatted_offline_customer_and_payment(self):
		self.client.logout()
		customer = Customer.objects.create(
			name="Formatted Mobile Customer",
			mobile="+91 98765 00234",
		)
		facility = Facility.objects.create(
			name="Formatted Mobile Field",
			sport=Facility.Sport.FOOTBALL,
			hourly_rate=Decimal("2000.00"),
		)
		booking = Booking.objects.create(
			customer=customer,
			facility=facility,
			start_at=timezone.now() + timedelta(days=1),
			end_at=timezone.now() + timedelta(days=1, hours=1),
			source=Booking.Source.OFFLINE,
			status=Booking.Status.CONFIRMED,
			total_amount=Decimal("2000.00"),
			advance_paid=Decimal("500.00"),
		)
		Payment.objects.create(
			booking=booking,
			customer=customer,
			amount=Decimal("500.00"),
			method=Booking.PaymentMethod.CASH,
			reference="CASH-LEGACY-500",
		)
		user = User.objects.create_user(username="9876500234")
		self.client.force_login(user)

		bookings_response = self.client.get(reverse("booking_history"))
		payments_response = self.client.get(reverse("customer_payment_history"))
		receipt_response = self.client.get(reverse("booking_confirmation", args=[booking.booking_id]))

		customer.refresh_from_db()
		self.assertEqual(customer.user, user)
		self.assertContains(bookings_response, booking.booking_id)
		self.assertContains(payments_response, "CASH-LEGACY-500")
		self.assertContains(receipt_response, "CASH-LEGACY-500")

	def test_book_facility_for_user_without_customer_creates_profile_and_renders_form(self):
		self.client.logout()
		plain_user = User.objects.create_user(username="plainbookinguser", password="not-a-real-secret")
		self.client.force_login(plain_user)
		response = self.client.get(reverse("book_facility"))
		self.assertEqual(response.status_code, 200)
		self.assertTrue(Customer.objects.filter(user=plain_user).exists())

	def test_dashboard_shows_bookings_overlapping_today(self):
		self.client.logout()
		staff = User.objects.create_user(username="dashboardstaff", password="not-a-real-secret", is_staff=True)
		self.client.force_login(staff)
		facility = Facility.objects.create(name="Night Arena", sport=Facility.Sport.FOOTBALL, hourly_rate=Decimal("2500.00"))
		customer = Customer.objects.create(name="Night Player", mobile="9000000011")
		yesterday = timezone.localdate() - timedelta(days=1)
		start = timezone.make_aware(datetime.combine(yesterday, datetime.min.time())) + timedelta(hours=22, minutes=30)
		end = start + timedelta(hours=2)
		Booking.objects.create(
			customer=customer,
			facility=facility,
			start_at=start,
			end_at=end,
			source=Booking.Source.OFFLINE,
			total_amount=Decimal("5000.00"),
		)
		response = self.client.get(reverse("dashboard"))
		self.assertContains(response, "Night Arena")

	def test_availability_accepts_ddmmyyyy_date_values(self):
		facility = Facility.objects.create(
			name="Date Format Arena",
			sport=Facility.Sport.FOOTBALL,
			hourly_rate=Decimal("3000.00"),
		)
		response = self.client.get(reverse("availability"), {
			"facility": facility.pk,
			"date": "15-10-2026",
			"duration": 60,
		})
		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()["slots"])

	def test_staff_pages_render(self):
		for route_name in ["dashboard", "calendar", "expenses", "academy", "monthly_report"]:
			with self.subTest(route=route_name):
				self.assertEqual(self.client.get(reverse(route_name)).status_code, 200)


@override_settings(
	RAZORPAY_KEY_ID="rzp_test_key",
	RAZORPAY_KEY_SECRET="test_secret",
	RAZORPAY_WEBHOOK_SECRET="test_webhook_secret",
)
class CustomerRazorpayFlowTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username="9876500001", password="GameOnDemo2026!")
		self.customer = Customer.objects.create(
			user=self.user,
			name="Razorpay Customer",
			mobile=self.user.username,
			email="player@example.com",
		)
		self.facility = Facility.objects.create(
			name="Payment Arena",
			sport=Facility.Sport.FOOTBALL,
			hourly_rate=Decimal("2000.00"),
		)
		self.client.force_login(self.user)

	def create_pending_booking(self):
		start_at = timezone.now() + timedelta(days=1)
		start_at = start_at.replace(hour=18, minute=0, second=0, microsecond=0)
		booking = create_booking(
			facility_id=self.facility.pk,
			start_at=start_at,
			duration_minutes=60,
			customer_name=self.customer.name,
			customer_mobile=self.customer.mobile,
			customer_email=self.customer.email,
			status=Booking.Status.PENDING_PAYMENT,
			advance_required=Decimal("500.00"),
			payment_expires_at=timezone.now() + timedelta(minutes=15),
		)
		booking.payment_order_id = "order_test_123"
		booking.save(update_fields=["payment_order_id"])
		PaymentOrder.objects.create(booking=booking, order_id=booking.payment_order_id, amount_paise=50000)
		return booking

	def create_confirmed_booking_with_balance(self):
		start_at = timezone.now() + timedelta(days=1)
		start_at = start_at.replace(hour=18, minute=0, second=0, microsecond=0)
		return create_booking(
			facility_id=self.facility.pk,
			start_at=start_at,
			duration_minutes=60,
			customer_name=self.customer.name,
			customer_mobile=self.customer.mobile,
			customer_email=self.customer.email,
			status=Booking.Status.CONFIRMED,
			advance_required=Decimal("500.00"),
			advance_paid=Decimal("500.00"),
			payment_method=Booking.PaymentMethod.ONLINE,
			payment_reference="pay_advance_123",
		)

	def post_captured_webhook(self, booking, *, payment_id="pay_webhook_123", amount_paise=50000):
		payload = json.dumps({
			"event": "payment.captured",
			"payload": {
				"payment": {
					"entity": {
						"id": payment_id,
						"order_id": booking.payment_order_id,
						"amount": amount_paise,
						"currency": "INR",
						"status": "captured",
					}
				}
			},
		}).encode()
		signature = hmac.new(b"test_webhook_secret", payload, hashlib.sha256).hexdigest()
		return self.client.post(
			reverse("razorpay_webhook"),
			data=payload,
			content_type="application/json",
			HTTP_X_RAZORPAY_SIGNATURE=signature,
		)

	@patch("core.views.create_order", return_value="order_test_booking")
	def test_new_customer_booking_creates_order_and_redirects_to_checkout(self, create_order_mock):
		start_at = timezone.localtime(timezone.now() + timedelta(days=1)).replace(
			hour=18, minute=0, second=0, microsecond=0
		)
		response = self.client.post(reverse("book_facility"), {
			"facility": self.facility.pk,
			"start_at": start_at.strftime("%Y-%m-%dT%H:%M"),
			"duration_minutes": 60,
		})

		booking = Booking.objects.get(customer=self.customer)
		self.assertEqual(response.status_code, 302)
		self.assertRedirects(response, reverse("booking_payment", args=[booking.booking_id]), fetch_redirect_response=False)
		self.assertEqual(booking.status, Booking.Status.PENDING_PAYMENT)
		self.assertEqual(booking.advance_required, Decimal("500.00"))
		self.assertTrue(PaymentOrder.objects.filter(booking=booking, order_id="order_test_booking").exists())
		create_order_mock.assert_called_once()

	def test_pending_booking_checkout_contains_real_order_details(self):
		booking = self.create_pending_booking()

		response = self.client.get(reverse("booking_payment", args=[booking.booking_id]))

		self.assertEqual(response.status_code, 200)
		self.assertTemplateUsed(response, "core/razorpay_checkout.html")
		self.assertContains(response, 'id="pay-button"')
		self.assertContains(response, 'id="payment-type"')
		self.assertContains(response, "Full booking total")
		self.assertEqual(response.context["checkout_options"]["key"], "rzp_test_key")
		self.assertEqual(response.context["checkout_options"]["order_id"], "order_test_123")
		self.assertEqual(response.context["checkout_options"]["amount"], 50000)

	def test_pending_payment_link_in_booking_history_opens_checkout(self):
		booking = self.create_pending_booking()

		response = self.client.get(reverse("booking_history"))

		self.assertContains(response, reverse("booking_payment", args=[booking.booking_id]))

	def test_confirmed_booking_with_balance_shows_pay_balance_link(self):
		booking = self.create_confirmed_booking_with_balance()

		response = self.client.get(reverse("booking_history"))

		self.assertContains(response, reverse("booking_balance_payment", args=[booking.booking_id]))
		self.assertContains(response, "Pay balance")

	def test_booking_receipt_groups_each_payment_with_labeled_details(self):
		booking = self.create_confirmed_booking_with_balance()
		Payment.objects.create(
			booking=booking,
			customer=self.customer,
			amount=Decimal("250.00"),
			method=Booking.PaymentMethod.ONLINE,
			reference="pay_partial_receipt",
		)
		booking.advance_paid += Decimal("250.00")
		booking.save(update_fields=["advance_paid"])

		response = self.client.get(reverse("booking_confirmation", args=[booking.booking_id]))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "PAYMENT HISTORY")
		self.assertContains(response, "2 payments")
		self.assertContains(response, "Payment date")
		self.assertContains(response, "Transaction reference")
		self.assertContains(response, "pay_partial_receipt")

	@patch("core.views.create_order", return_value="order_test_balance")
	def test_balance_checkout_creates_order_for_remaining_amount(self, create_order_mock):
		booking = self.create_confirmed_booking_with_balance()

		response = self.client.get(reverse("booking_balance_payment", args=[booking.booking_id]))

		self.assertEqual(response.status_code, 200)
		self.assertTemplateUsed(response, "core/razorpay_checkout.html")
		self.assertContains(response, "Pay your balance.")
		self.assertEqual(response.context["checkout_options"]["order_id"], "order_test_balance")
		self.assertEqual(response.context["checkout_options"]["amount"], 150000)
		create_order_mock.assert_called_once()

	def test_verified_balance_payment_records_payment_and_clears_due(self):
		booking = self.create_confirmed_booking_with_balance()
		order_id = "order_test_balance"
		payment_id = "pay_test_balance"
		PaymentOrder.objects.create(booking=booking, order_id=order_id, amount_paise=150000)
		booking.payment_order_id = order_id
		booking.save(update_fields=["payment_order_id"])
		signature = hmac.new(
			b"test_secret",
			f"{order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()

		response = self.client.post(reverse("verify_razorpay_payment", args=[booking.booking_id]), {
			"razorpay_order_id": order_id,
			"razorpay_payment_id": payment_id,
			"razorpay_signature": signature,
		})

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(booking.status, Booking.Status.CONFIRMED)
		self.assertEqual(booking.advance_paid, Decimal("2000.00"))
		self.assertEqual(booking.balance_due, Decimal("0.00"))
		self.assertEqual(Payment.objects.filter(booking=booking).count(), 2)
		self.assertTrue(Payment.objects.filter(booking=booking, reference=payment_id, amount=Decimal("1500.00")).exists())

	def test_verified_partial_balance_payment_reduces_outstanding_balance(self):
		booking = self.create_confirmed_booking_with_balance()
		order_id = "order_test_partial"
		payment_id = "pay_test_partial"
		PaymentOrder.objects.create(booking=booking, order_id=order_id, amount_paise=40050)
		booking.payment_order_id = order_id
		booking.save(update_fields=["payment_order_id"])
		signature = hmac.new(
			b"test_secret",
			f"{order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()

		response = self.client.post(reverse("verify_razorpay_payment", args=[booking.booking_id]), {
			"razorpay_order_id": order_id,
			"razorpay_payment_id": payment_id,
			"razorpay_signature": signature,
		})

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(booking.advance_paid, Decimal("900.50"))
		self.assertEqual(booking.balance_due, Decimal("1099.50"))

	def test_balance_payment_rejects_amount_above_outstanding_balance(self):
		booking = self.create_confirmed_booking_with_balance()
		order_id = "order_test_overpayment"
		payment_id = "pay_test_overpayment"
		PaymentOrder.objects.create(booking=booking, order_id=order_id, amount_paise=150001)
		booking.payment_order_id = order_id
		booking.save(update_fields=["payment_order_id"])
		signature = hmac.new(
			b"test_secret",
			f"{order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()

		response = self.client.post(reverse("verify_razorpay_payment", args=[booking.booking_id]), {
			"razorpay_order_id": order_id,
			"razorpay_payment_id": payment_id,
			"razorpay_signature": signature,
		})

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 400)
		self.assertEqual(booking.advance_paid, Decimal("500.00"))
		self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)

	@patch("core.views.create_order", return_value="order_test_balance_retry")
	def test_balance_checkout_can_create_retry_order(self, create_order_mock):
		booking = self.create_confirmed_booking_with_balance()

		response = self.client.post(
			reverse("retry_razorpay_balance_order", args=[booking.booking_id]),
			{"amount": "1500.00"},
		)

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"order_id": "order_test_balance_retry", "amount_paise": 150000})
		self.assertEqual(booking.payment_order_id, "order_test_balance_retry")
		create_order_mock.assert_called_once()

	@patch("core.views.create_order", return_value="order_test_partial")
	def test_balance_checkout_can_create_partial_payment_order(self, create_order_mock):
		booking = self.create_confirmed_booking_with_balance()

		response = self.client.post(
			reverse("retry_razorpay_balance_order", args=[booking.booking_id]),
			{"amount": "400.50"},
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"order_id": "order_test_partial", "amount_paise": 40050})
		create_order_mock.assert_called_once()

	def test_balance_checkout_rejects_amount_above_due(self):
		booking = self.create_confirmed_booking_with_balance()

		response = self.client.post(
			reverse("retry_razorpay_balance_order", args=[booking.booking_id]),
			{"amount": "1500.01"},
		)

		self.assertEqual(response.status_code, 400)

	def test_pending_payment_hold_blocks_an_overlapping_booking(self):
		booking = self.create_pending_booking()

		with self.assertRaises(BookingConflict):
			create_booking(
				facility_id=self.facility.pk,
				start_at=booking.start_at,
				duration_minutes=60,
				customer_name="Another Customer",
				customer_mobile="9876500002",
			)

	def test_verified_checkout_confirms_booking_and_records_payment(self):
		booking = self.create_pending_booking()
		payment_id = "pay_test_123"
		signature = hmac.new(
			b"test_secret",
			f"{booking.payment_order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()

		response = self.client.post(reverse("verify_razorpay_payment", args=[booking.booking_id]), {
			"razorpay_order_id": booking.payment_order_id,
			"razorpay_payment_id": payment_id,
			"razorpay_signature": signature,
		})

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(booking.status, Booking.Status.CONFIRMED)
		self.assertEqual(booking.advance_paid, Decimal("500.00"))
		self.assertEqual(Payment.objects.get(booking=booking).reference, payment_id)

	def test_invalid_checkout_signature_does_not_confirm_booking(self):
		booking = self.create_pending_booking()

		response = self.client.post(reverse("verify_razorpay_payment", args=[booking.booking_id]), {
			"razorpay_order_id": booking.payment_order_id,
			"razorpay_payment_id": "pay_test_invalid",
			"razorpay_signature": "invalid",
		})

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 400)
		self.assertEqual(booking.status, Booking.Status.PENDING_PAYMENT)
		self.assertFalse(Payment.objects.filter(booking=booking).exists())

	def test_captured_webhook_confirms_booking_and_records_payment(self):
		booking = self.create_pending_booking()

		response = self.post_captured_webhook(booking)

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"status": "ok", "booking_id": booking.booking_id})
		self.assertEqual(booking.status, Booking.Status.CONFIRMED)
		self.assertEqual(booking.advance_paid, Decimal("500.00"))
		self.assertEqual(Payment.objects.get(booking=booking).reference, "pay_webhook_123")

	def test_captured_webhook_replay_does_not_duplicate_payment(self):
		booking = self.create_pending_booking()

		first_response = self.post_captured_webhook(booking)
		replay_response = self.post_captured_webhook(booking)

		self.assertEqual(first_response.status_code, 200)
		self.assertEqual(replay_response.status_code, 200)
		self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)
		booking.refresh_from_db()
		self.assertEqual(booking.advance_paid, Decimal("500.00"))

	def test_invalid_webhook_signature_is_rejected(self):
		booking = self.create_pending_booking()
		payload = json.dumps({"event": "payment.captured"}).encode()

		response = self.client.post(
			reverse("razorpay_webhook"),
			data=payload,
			content_type="application/json",
			HTTP_X_RAZORPAY_SIGNATURE="invalid",
		)

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 400)
		self.assertEqual(booking.status, Booking.Status.PENDING_PAYMENT)
		self.assertFalse(Payment.objects.filter(booking=booking).exists())

	def test_captured_webhook_rejects_mismatched_amount(self):
		booking = self.create_pending_booking()

		response = self.post_captured_webhook(booking, amount_paise=49999)

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 400)
		self.assertEqual(booking.status, Booking.Status.PENDING_PAYMENT)
		self.assertFalse(Payment.objects.filter(booking=booking).exists())

	@patch("core.views.create_order", return_value="order_test_retry")
	def test_pending_checkout_can_create_retry_order(self, create_order_mock):
		booking = self.create_pending_booking()

		response = self.client.post(reverse("retry_razorpay_order", args=[booking.booking_id]))

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"order_id": "order_test_retry", "amount_paise": 50000})
		self.assertEqual(booking.payment_order_id, "order_test_retry")
		create_order_mock.assert_called_once()

	@patch("core.views.create_order", return_value="order_test_full")
	def test_pending_checkout_can_create_order_for_full_booking_total(self, create_order_mock):
		booking = self.create_pending_booking()

		response = self.client.post(
			reverse("retry_razorpay_order", args=[booking.booking_id]),
			{"payment_type": "full"},
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"order_id": "order_test_full", "amount_paise": 200000})
		create_order_mock.assert_called_once()

	def test_verified_full_booking_payment_clears_balance(self):
		booking = self.create_pending_booking()
		order_id = "order_test_full"
		payment_id = "pay_test_full"
		PaymentOrder.objects.create(booking=booking, order_id=order_id, amount_paise=200000)
		booking.payment_order_id = order_id
		booking.save(update_fields=["payment_order_id"])
		signature = hmac.new(
			b"test_secret",
			f"{order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()

		response = self.client.post(reverse("verify_razorpay_payment", args=[booking.booking_id]), {
			"razorpay_order_id": order_id,
			"razorpay_payment_id": payment_id,
			"razorpay_signature": signature,
		})

		booking.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(booking.status, Booking.Status.CONFIRMED)
		self.assertEqual(booking.advance_paid, booking.total_amount)
		self.assertEqual(booking.balance_due, Decimal("0.00"))
