from django.contrib import admin

# Register your models here.
from .models import AdvancePaymentSetting, AcademyBatch, Attendance, AuditLog, Booking, BookingNotification, Customer, Expense, Facility, MaintenanceBlock, Payment, Student

@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
	list_display = ["name", "sport", "hourly_rate", "opening_time", "closing_time", "is_active"]
	list_filter = ["sport", "is_active"]
	search_fields = ["name"]

class PaymentInline(admin.TabularInline):
	model = Payment
	extra = 0
	can_delete = False
	fields = ["received_at", "direction", "amount", "method", "reference", "recorded_by"]
	readonly_fields = fields

	def has_add_permission(self, request, obj=None):
		return False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
	list_display = ["booking_id", "customer", "facility", "start_at", "source", "status", "total_amount", "advance_paid", "balance_due", "next_payment_amount", "payment_method"]
	list_filter = ["source", "status", "facility", "start_at"]
	search_fields = ["booking_id", "customer__name", "customer__mobile", "facility__name", "payment_reference"]
	ordering = ["-created_at"]
	date_hierarchy = "start_at"
	list_per_page = 50
	readonly_fields = ["booking_id", "created_at", "balance_due"]
	inlines = [PaymentInline]

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False

	def get_readonly_fields(self, request, obj=None):
		return [
			field.name for field in self.model._meta.fields
			if field.name != "next_payment_amount"
		] + ["balance_due"]


@admin.register(AdvancePaymentSetting)
class AdvancePaymentSettingAdmin(admin.ModelAdmin):
	list_display = ["mode", "value"]
	fields = ["mode", "value"]

	def has_add_permission(self, request):
		return super().has_add_permission(request) and not AdvancePaymentSetting.objects.exists()

	def has_delete_permission(self, request, obj=None):
		return False

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
	list_display = ["name", "mobile", "email", "created_at"]
	search_fields = ["name", "mobile", "email"]

@admin.register(MaintenanceBlock)
class MaintenanceBlockAdmin(admin.ModelAdmin):
	list_display = ["facility", "start_at", "end_at", "reason", "created_by"]
	list_filter = ["facility"]

	def has_add_permission(self, request):
		return False

	def get_readonly_fields(self, request, obj=None):
		return [field.name for field in self.model._meta.fields]

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
	list_display = ["received_at", "booking", "customer", "direction", "purpose", "amount", "method", "reference", "recorded_by"]
	list_filter = ["direction", "purpose", "method", "received_at"]
	search_fields = ["booking__booking_id", "customer__name", "customer__mobile", "reference", "recorded_by__username"]
	readonly_fields = ["received_at"]


@admin.register(BookingNotification)
class BookingNotificationAdmin(admin.ModelAdmin):
	list_display = ["title", "user", "booking", "created_at", "read_at"]
	list_filter = ["created_at", "read_at"]
	search_fields = ["title", "message", "user__username", "booking__booking_id"]
	readonly_fields = ["user", "booking", "title", "message", "created_at", "read_at"]

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
	list_display = ["spent_at", "category", "description", "amount", "method", "entered_by"]
	list_filter = ["category", "method", "spent_at"]
	search_fields = ["description"]

@admin.register(AcademyBatch)
class AcademyBatchAdmin(admin.ModelAdmin):
	list_display = ["name", "sport", "coach_name", "monthly_fee", "capacity", "is_active"]
	list_filter = ["sport", "is_active"]
	search_fields = ["name", "coach_name"]

@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
	list_display = ["name", "batch", "contact_number", "joined_on", "status"]
	list_filter = ["batch", "status"]
	search_fields = ["name", "guardian_name", "contact_number"]

@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
	list_display = ["date", "student", "status", "marked_by"]
	list_filter = ["date", "status", "student__batch"]
	search_fields = ["student__name"]

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
	list_display = ["created_at", "user", "action", "entity", "record_id"]
	list_filter = ["action", "entity", "created_at"]
	search_fields = ["record_id", "user__username"]
	readonly_fields = ["user", "action", "entity", "record_id", "previous_value", "new_value", "created_at"]

	def has_add_permission(self, request):
		return False
