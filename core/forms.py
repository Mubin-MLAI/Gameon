from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .customer_services import find_customers_by_mobile
from .models import AcademyBatch, Attendance, Booking, Expense, Facility, MaintenanceBlock


class RegistrationForm(UserCreationForm):
    username = forms.CharField(required=False, widget=forms.HiddenInput)
    name = forms.CharField(max_length=120)
    mobile = forms.CharField(max_length=20)
    email = forms.EmailField(required=False)

    class Meta:
        model = User
        fields = ["username", "name", "mobile", "email", "password1", "password2"]

    def clean_mobile(self):
        mobile = self.cleaned_data["mobile"].strip()
        matching_customers = find_customers_by_mobile(mobile)
        if User.objects.filter(username=mobile).exists() or any(customer.user_id for customer in matching_customers):
            raise forms.ValidationError("An account using this mobile number already exists.")
        return mobile


class CustomerBookingForm(forms.Form):
    facility = forms.ModelChoiceField(queryset=Facility.objects.filter(is_active=True))
    start_at = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    duration_minutes = forms.IntegerField(min_value=30, initial=60, widget=forms.NumberInput(attrs={"step": 30}))


class OfflineBookingForm(CustomerBookingForm):
    customer_name = forms.CharField(max_length=120)
    customer_mobile = forms.CharField(max_length=20)
    customer_email = forms.EmailField(required=False)
    advance_paid = forms.DecimalField(min_value=0, decimal_places=2, max_digits=9, initial=0)
    payment_method = forms.ChoiceField(choices=[("", "Not collected")] + list(Booking.PaymentMethod.choices), required=False)
    payment_reference = forms.CharField(max_length=100, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))


class MaintenanceBlockForm(forms.ModelForm):
    start_at = forms.DateTimeField(input_formats=["%Y-%m-%dT%H:%M"], widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    end_at = forms.DateTimeField(input_formats=["%Y-%m-%dT%H:%M"], widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))

    class Meta:
        model = MaintenanceBlock
        fields = ["facility", "start_at", "end_at", "reason"]
        widgets = {
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_at") and cleaned.get("end_at") and cleaned["end_at"] <= cleaned["start_at"]:
            self.add_error("end_at", "End time must be after start time.")
        return cleaned


class PaymentForm(forms.Form):
    amount = forms.DecimalField(min_value=0.01, decimal_places=2, max_digits=9)
    method = forms.ChoiceField(choices=Booking.PaymentMethod.choices)
    reference = forms.CharField(max_length=100, required=False)


class ExpenseForm(forms.ModelForm):
    spent_at = forms.DateTimeField(input_formats=["%Y-%m-%dT%H:%M"], widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))

    class Meta:
        model = Expense
        fields = ["category", "amount", "method", "description", "spent_at"]
        widgets = {"spent_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")}


class AttendanceForm(forms.Form):
    student_id = forms.IntegerField(widget=forms.HiddenInput)
    date = forms.DateField(widget=forms.HiddenInput)
    status = forms.ChoiceField(choices=Attendance.Status.choices)


class BatchForm(forms.ModelForm):
    class Meta:
        model = AcademyBatch
        fields = ["name", "sport", "age_group", "coach_name", "schedule", "monthly_fee", "capacity"]