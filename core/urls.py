from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("book/", views.book_facility, name="book_facility"),
    path("bookings/<str:booking_id>/pay/", views.booking_payment, name="booking_payment"),
    path("bookings/<str:booking_id>/balance/pay/", views.booking_balance_payment, name="booking_balance_payment"),
    path("payments/history/", views.customer_payment_history, name="customer_payment_history"),
    path("payments/razorpay/verify/<str:booking_id>/", views.verify_razorpay_payment, name="verify_razorpay_payment"),
    path("payments/razorpay/retry/<str:booking_id>/", views.retry_razorpay_order, name="retry_razorpay_order"),
    path("payments/razorpay/retry-balance/<str:booking_id>/", views.retry_razorpay_balance_order, name="retry_razorpay_balance_order"),
    path("webhooks/payments/", views.razorpay_webhook, name="razorpay_webhook"),
    path("payments/razorpay/webhook/", views.razorpay_webhook, name="razorpay_webhook_legacy"),
    path("bookings/", views.booking_history, name="booking_history"),
    path("notifications/", views.booking_notifications, name="booking_notifications"),
    path("bookings/<str:booking_id>/", views.booking_confirmation, name="booking_confirmation"),
    path("availability/", views.availability, name="availability"),
    path("manage/", views.dashboard, name="dashboard"),
    path("manage/calendar/", views.staff_calendar, name="calendar"),
    path("manage/bookings/new/", views.offline_booking, name="offline_booking"),
    path("manage/bookings/<str:booking_id>/cancel/", views.cancel_booking, name="cancel_booking"),
    path("manage/bookings/<str:booking_id>/payment/", views.collect_payment, name="collect_payment"),
    path("manage/blocks/new/", views.create_block, name="create_block"),
    path("manage/blocks/<int:block_id>/release/", views.release_block, name="release_block"),
    path("manage/expenses/", views.expenses, name="expenses"),
    path("manage/academy/", views.academy, name="academy"),
    path("manage/reports/", views.monthly_report, name="monthly_report"),
]