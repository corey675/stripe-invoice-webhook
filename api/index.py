import json
import os
from datetime import datetime, timezone
from calendar import monthrange
from http.server import BaseHTTPRequestHandler

import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]


def is_last_day_of_month(dt: datetime) -> bool:
    last_day = monthrange(dt.year, dt.month)[1]
    return dt.day == last_day


def first_of_next_month_ts(dt: datetime) -> int:
    if dt.month == 12:
        next_month = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
    return int(next_month.timestamp())


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        sig_header = self.headers.get("stripe-signature")

        try:
            event = stripe.Webhook.construct_event(
                raw_body, sig_header, WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError as e:
            self._respond(400, {"error": str(e)})
            return

        if event["type"] == "invoice.created":
            invoice = event["data"]["object"]

            if invoice["status"] == "draft":
                effective_ts = invoice.get("effective_at") or invoice["created"]
                effective_dt = datetime.fromtimestamp(effective_ts, tz=timezone.utc)

                if is_last_day_of_month(effective_dt):
                    new_ts = first_of_next_month_ts(effective_dt)
                    print(f"Shifting invoice {invoice['id']} from "
                          f"{effective_dt.date()} to "
                          f"{datetime.fromtimestamp(new_ts, tz=timezone.utc).date()}")
                    try:
                        stripe.Invoice.modify(invoice["id"], effective_at=new_ts)
                        print(f"✓ Updated invoice {invoice['id']}")
                    except stripe.error.StripeError as e:
                        print(f"✗ Failed: {e}")

        self._respond(200, {"received": True})

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
