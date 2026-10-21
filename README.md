# GameOn

GameOn is a Django sports-facility booking and operations MVP. Customer bookings and staff-created offline bookings share one transactional service and one availability database. Maintenance blocks are checked by that same service.

## Run locally

```powershell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/` for customer booking. The Django admin at `/admin/` manages facilities, pricing, customers, bookings, payments, expenses, academy batches, students, and attendance. A staff user (`is_staff=True`) can open `/manage/` for operations, calendar, payments, attendance, and reports. Create customer accounts at `/register/`; the mobile number is their login ID.

## Included workflows

- Customer accounts, facility browsing, online booking, confirmation, and booking history. Customers can pay the configured advance or full booking total at checkout, then pay any partial or all of the remaining balance online.
- Razorpay checkout for an admin-configurable booking advance (percentage or fixed amount), with a 15-minute slot hold, signature-verified browser response, and captured-payment webhook reconciliation.
- Booking confirmation through an in-app notification inbox, email, and SMS through MSG91 Flow templates.
- Authenticated customer payment history and digital booking receipts with itemized payment records and a print / save-as-PDF action. Staff-recorded payments appear in the admin ledger and the customer's payment history.
- Staff dashboard, calendar, offline booking, booking cancellation, payment collection, and maintenance blocks.
- Payment and expense ledger, daily dashboard totals, monthly summary, and CSV export.
- Academy batches and student records in Django admin, plus an attendance screen for staff.
- Audit entries for booking creation/cancellation, payments, expenses, and facility blocks.
- Demo facilities: Football Arena, Cricket Ground, and Cricket Nets.

## Production notes

This is a development-ready foundation, not a deployed production system. Before deployment, set `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and `DJANGO_DEBUG=0`; configure a production database, HTTPS, backups, and provider credentials as deployment environment variables. Never commit live credentials.

### Payment and notification configuration

- For local setup, copy `.env.example` to `.env` in the project root and fill in the values locally. Django loads `.env` at startup; `.env` is ignored by Git. Restart `runserver` after editing it.
- Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` to the Razorpay keys for your account. Use Razorpay test-mode keys while developing.
- Set `RAZORPAY_WEBHOOK_SECRET` to the webhook signing secret created in the Razorpay Dashboard. Configure the webhook URL as `https://<your-ngrok-host>/webhooks/payments/` (or your production domain), subscribe to `payment.captured`, and use the same secret in `.env`. The endpoint verifies `X-Razorpay-Signature`, matches captured order and amount to a GameOn payment order, and records each payment once.
- For local webhook testing, start Django on port 8000, run `ngrok http 8000`, then add the hostname printed by ngrok (without `https://` or a trailing slash) to `DJANGO_ALLOWED_HOSTS` in `.env`, comma-separated with `localhost,127.0.0.1`. Restart Django after changing `.env`, and enter `https://<your-ngrok-host>/webhooks/payments/` as the webhook URL in Razorpay. The temporary ngrok hostname changes between sessions unless you use a reserved domain.
- Configure the global advance rule in Django admin under **Advance payment settings**. It defaults to 25%; choose percentage or a fixed INR amount. Percentage values must be between 0.01% and 100%; a fixed advance is capped at the booking total. The required amount is rounded to paise and snapshotted when an online booking is started. The chosen slot is held for 15 minutes while checkout is in progress. A booking is confirmed and its payment ledger entry recorded after checkout signature verification or the verified `payment.captured` webhook.
- Booking receipts and Django admin record booking total, each payment amount and method, automatically recorded payment date/time, transaction/reference, amount paid to date, and remaining balance.
- Configure SMTP using `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` (default `1`), `EMAIL_USE_SSL` (default `0`), and `DEFAULT_FROM_EMAIL`. In development, Django writes email to the console by default.
- Configure MSG91 using `MSG91_AUTH_KEY`, `MSG91_BOOKING_TEMPLATE_ID`, and `MSG91_COUNTRY_CODE` (default `91`). Create and approve an MSG91 Flow template whose placeholders follow this order: customer name (`VAR1`), booking ID (`VAR2`), facility (`VAR3`), date (`VAR4`), start time (`VAR5`), amount paid to date (`VAR6`), and balance due (`VAR7`). Ensure your DLT registration and template support these variables.
- Email is skipped when the customer has no email address; SMS is skipped when MSG91 credentials/template ID are absent. Email includes booking totals and itemized payment time, method, and references; in-app customers can open and print/save their receipt as PDF. Delivery failures are logged and do not undo a confirmed booking.

OTP login, WhatsApp integration, automated refunds, daily closing, and distinct staff roles remain unimplemented. The receipt can be saved as a PDF from the browser print dialog; server-generated PDF/Excel exports are not implemented. The current login is password-based, and Django staff access is a single permission tier; create explicit role permissions before using this as a multi-role production system. SQLite and its row-lock behavior are suitable for local development, not high-concurrency booking traffic; use PostgreSQL and test concurrent reservations before launch.