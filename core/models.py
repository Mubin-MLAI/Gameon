from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class Customer(models.Model):
	user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="gameon_customer")
	name = models.CharField(max_length=120)
	mobile = models.CharField(max_length=20, unique=True)
	email = models.EmailField(blank=True)
	notes = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["name"]

	def __str__(self):
		return f"{self.name} ({self.mobile})"


class Facility(models.Model):
	class Sport(models.TextChoices):
		FOOTBALL = "football", "Football"
		CRICKET = "cricket", "Cricket"
		NETS = "nets", "Cricket nets"
		OTHER = "other", "Other"

	name = models.CharField(max_length=120)
	sport = models.CharField(max_length=20, choices=Sport.choices)
	description = models.TextField(blank=True)
	hourly_rate = models.DecimalField(max_digits=9, decimal_places=2, validators=[MinValueValidator(0)])
	opening_time = models.TimeField(default="05:00")
	closing_time = models.TimeField(default="23:00")
	min_duration_minutes = models.PositiveSmallIntegerField(default=60)
	max_duration_minutes = models.PositiveSmallIntegerField(default=240)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ["name"]

	def __str__(self):
		return self.name


class AdvancePaymentSetting(models.Model):
	class Mode(models.TextChoices):
		PERCENTAGE = "percentage", "Percentage"
		FIXED = "fixed", "Fixed amount"

	mode = models.CharField(
		max_length=12,
		choices=Mode.choices,
		default=Mode.PERCENTAGE,
		help_text="This advance rule applies to online bookings at every facility.",
	)
	value = models.DecimalField(
		max_digits=9,
		decimal_places=2,
		default=Decimal("25.00"),
		validators=[MinValueValidator(Decimal("0.01"))],
		help_text="Percentage when the mode is Percentage; INR when the mode is Fixed amount.",
	)

	class Meta:
		verbose_name = "advance payment setting"
		verbose_name_plural = "advance payment settings"

	def clean(self):
		super().clean()
		if self.mode == self.Mode.PERCENTAGE and self.value > Decimal("100.00"):
			raise ValidationError({"value": "A percentage advance cannot exceed 100%."})

	@classmethod
	def get_solo(cls):
		setting, _ = cls.objects.get_or_create(pk=1)
		return setting

	def save(self, *args, **kwargs):
		self.pk = 1
		super().save(*args, **kwargs)

	def calculate_amount(self, total_amount):
		if self.mode == self.Mode.PERCENTAGE:
			amount = total_amount * self.value / Decimal("100")
		else:
			amount = min(total_amount, self.value)
		return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

	def __str__(self):
		return f"{self.get_mode_display()}: {self.value}"


class MaintenanceBlock(models.Model):
	facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name="blocks")
	start_at = models.DateTimeField()
	end_at = models.DateTimeField()
	reason = models.CharField(max_length=160)
	created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["start_at"]

	def __str__(self):
		return f"{self.facility}: {self.reason}"


class Booking(models.Model):
	class Source(models.TextChoices):
		ONLINE = "online", "Online"
		OFFLINE = "offline", "Offline"

	class Status(models.TextChoices):
		PENDING_PAYMENT = "pending_payment", "Pending payment"
		CONFIRMED = "confirmed", "Confirmed"
		CANCELLED = "cancelled", "Cancelled"

	class PaymentMethod(models.TextChoices):
		CASH = "cash", "Cash"
		UPI = "upi", "UPI"
		BANK = "bank", "Bank transfer"
		ONLINE = "online", "Online gateway"

	booking_id = models.CharField(max_length=12, unique=True, editable=False)
	customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="bookings")
	facility = models.ForeignKey(Facility, on_delete=models.PROTECT, related_name="bookings")
	start_at = models.DateTimeField()
	end_at = models.DateTimeField()
	source = models.CharField(max_length=10, choices=Source.choices)
	status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONFIRMED)
	total_amount = models.DecimalField(max_digits=9, decimal_places=2, validators=[MinValueValidator(0)])
	advance_required = models.DecimalField(max_digits=9, decimal_places=2, default=0, validators=[MinValueValidator(0)])
	advance_paid = models.DecimalField(max_digits=9, decimal_places=2, default=0, validators=[MinValueValidator(0)])
	payment_order_id = models.CharField(max_length=64, blank=True)
	payment_expires_at = models.DateTimeField(null=True, blank=True)
	payment_method = models.CharField(max_length=10, choices=PaymentMethod.choices, blank=True)
	payment_reference = models.CharField(max_length=100, blank=True)
	notes = models.TextField(blank=True)
	created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["start_at"]
		indexes = [models.Index(fields=["facility", "status", "start_at", "end_at"])]

	def save(self, *args, **kwargs):
		if not self.booking_id:
			import uuid

			self.booking_id = uuid.uuid4().hex[:10].upper()
		super().save(*args, **kwargs)

	@property
	def balance_due(self):
		return self.total_amount - self.advance_paid

	def __str__(self):
		return self.booking_id


class BookingNotification(models.Model):
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="booking_notifications",
	)
	booking = models.OneToOneField(
		Booking,
		on_delete=models.CASCADE,
		related_name="notification",
	)
	title = models.CharField(max_length=160)
	message = models.TextField()
	created_at = models.DateTimeField(auto_now_add=True)
	read_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self):
		return f"{self.title} · {self.user}"


class PaymentOrder(models.Model):
	booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="payment_orders")
	order_id = models.CharField(max_length=64, unique=True)
	amount_paise = models.PositiveIntegerField()
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["created_at"]

	def __str__(self):
		return self.order_id


class Payment(models.Model):
	class Direction(models.TextChoices):
		RECEIVED = "received", "Received"
		REFUNDED = "refunded", "Refunded"

	class Purpose(models.TextChoices):
		BOOKING = "booking", "Facility booking"
		ACADEMY = "academy", "Academy fees"
		OTHER = "other", "Other"

	booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
	customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
	direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.RECEIVED)
	purpose = models.CharField(max_length=10, choices=Purpose.choices, default=Purpose.BOOKING)
	amount = models.DecimalField(max_digits=9, decimal_places=2, validators=[MinValueValidator(0.01)])
	method = models.CharField(max_length=10, choices=Booking.PaymentMethod.choices)
	reference = models.CharField(max_length=100, blank=True)
	received_at = models.DateTimeField(auto_now_add=True)
	recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

	class Meta:
		ordering = ["-received_at"]


class Expense(models.Model):
	class Category(models.TextChoices):
		ELECTRICITY = "electricity", "Electricity"
		MAINTENANCE = "maintenance", "Maintenance"
		SALARIES = "salaries", "Salaries"
		CLEANING = "cleaning", "Cleaning"
		EQUIPMENT = "equipment", "Equipment"
		REPAIRS = "repairs", "Repairs"
		MARKETING = "marketing", "Marketing"
		PURCHASES = "purchases", "Purchases"
		OTHER = "other", "Other"

	category = models.CharField(max_length=20, choices=Category.choices)
	amount = models.DecimalField(max_digits=9, decimal_places=2, validators=[MinValueValidator(0.01)])
	method = models.CharField(max_length=10, choices=Booking.PaymentMethod.choices)
	description = models.CharField(max_length=240)
	spent_at = models.DateTimeField()
	entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-spent_at"]


class AcademyBatch(models.Model):
	name = models.CharField(max_length=120)
	sport = models.CharField(max_length=60)
	age_group = models.CharField(max_length=60, blank=True)
	coach_name = models.CharField(max_length=120, blank=True)
	schedule = models.CharField(max_length=160, blank=True)
	monthly_fee = models.DecimalField(max_digits=9, decimal_places=2, default=0, validators=[MinValueValidator(0)])
	capacity = models.PositiveSmallIntegerField(default=20)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ["name"]

	def __str__(self):
		return self.name


class Student(models.Model):
	class Status(models.TextChoices):
		ACTIVE = "active", "Active"
		INACTIVE = "inactive", "Inactive"

	name = models.CharField(max_length=120)
	date_of_birth = models.DateField(null=True, blank=True)
	guardian_name = models.CharField(max_length=120, blank=True)
	contact_number = models.CharField(max_length=20)
	batch = models.ForeignKey(AcademyBatch, on_delete=models.PROTECT, related_name="students")
	joined_on = models.DateField()
	status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
	notes = models.TextField(blank=True)

	class Meta:
		ordering = ["name"]

	def __str__(self):
		return self.name


class Attendance(models.Model):
	class Status(models.TextChoices):
		PRESENT = "present", "Present"
		ABSENT = "absent", "Absent"
		LEAVE = "leave", "Leave"

	student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="attendance")
	date = models.DateField()
	status = models.CharField(max_length=10, choices=Status.choices)
	marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

	class Meta:
		ordering = ["-date", "student__name"]
		constraints = [models.UniqueConstraint(fields=["student", "date"], name="unique_student_attendance_per_day")]


class AuditLog(models.Model):
	user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
	action = models.CharField(max_length=80)
	entity = models.CharField(max_length=40)
	record_id = models.CharField(max_length=40)
	previous_value = models.JSONField(default=dict, blank=True)
	new_value = models.JSONField(default=dict, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-created_at"]
