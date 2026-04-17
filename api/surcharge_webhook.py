import json
import os

import stripe
from http.server import BaseHTTPRequestHandler

stripe.api_key = os.environ["SURCHARGE_STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["SURCHARGE_STRIPE_WEBHOOK_SECRET"]

SURCHARGE_PRODUCT_ID = os.environ.get("SURCHARGE_PRODUCT_ID", "prod_TwsauvTg8JPMTs")
SURCHARGE_RATE = 0.03


# ─────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────

def get_payment_method_type_from_subscription(subscription):
    pm_id = subscription.get("default_payment_method")
    if pm_id:
        if isinstance(pm_id, dict):
            return pm_id["type"]
        pm = stripe.PaymentMethod.retrieve(pm_id)
        return pm["type"]

    customer = stripe.Customer.retrieve(
        subscription["customer"],
        expand=["invoice_settings.default_payment_method"]
    )
    invoice_pm = customer.get("invoice_settings", {}).get("default_payment_method")
    if invoice_pm:
        if isinstance(invoice_pm, str):
            pm = stripe.PaymentMethod.retrieve(invoice_pm)
            return pm["type"]
        return invoice_pm["type"]

    default_source = customer.get("default_source")
    if default_source:
        if isinstance(default_source, str):
            source = stripe.Customer.retrieve_source(customer["id"], default_source)
        else:
            source = default_source
        return "us_bank_account" if source["object"] == "bank_account" else source["object"]

    return None


def get_payment_method_type_from_invoice(invoice):
    sub_id = invoice.get("subscription")
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(
                sub_id,
                expand=["default_payment_method"]
            )
            pm = sub.get("default_payment_method")
            if pm and isinstance(pm, dict):
                return pm.get("type")
        except stripe.error.StripeError as e:
            print(f"Could not retrieve subscription {sub_id}: {e}")

    customer_id = invoice.get("customer")
    if customer_id:
        try:
            customer = stripe.Customer.retrieve(
                customer_id,
                expand=["invoice_settings.default_payment_method"]
            )
            pm = customer.get("invoice_settings", {}).get("default_payment_method")
            if pm and isinstance(pm, dict):
                return pm.get("type")
        except stripe.error.StripeError as e:
            print(f"Could not retrieve customer {customer_id}: {e}")

    return None


def find_surcharge_item(subscription):
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            return item
    return None


def invoice_already_has_surcharge(invoice):
    for line in invoice.get("lines", {}).get("data", []):
        price = line.get("price") or {}
        if price.get("product") == SURCHARGE_PRODUCT_ID:
            return True
    return False


def calculate_surcharge_cents(subscription):
    total = 0
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            continue
        total += (price.get("unit_amount") or 0) * (item.get("quantity") or 1)
    return round(total * SURCHARGE_RATE)


def get_or_create_surcharge_price(amount_cents, interval):
    existing = stripe.Price.list(product=SURCHARGE_PRODUCT_ID, active=True, limit=100)
    for p in existing["data"]:
        if (
            p["unit_amount"] == amount_cents
            and p.get("recurring", {}).get("interval") == interval
            and p["currency"] == "usd"
        ):
            return p["id"]

    new_price = stripe.Price.create(
        product=SURCHARGE_PRODUCT_ID,
        unit_amount=amount_cents,
        currency="usd",
        recurring={"interval": interval},
        nickname=f"CC Surcharge ${amount_cents / 100:.2f}/{interval}",
    )
    return new_price["id"]


# ─────────────────────────────────────────────
# Subscription updated / customer updated handlers
# (existing logic — payment method change on subscription)
# ─────────────────────────────────────────────

def add_surcharge_to_subscription(subscription):
    if find_surcharge_item(subscription):
        print(f"[{subscription['id']}] Surcharge already present — skipping.")
        return

    surcharge_cents = calculate_surcharge_cents(subscription)
    if surcharge_cents <= 0:
        print(f"[{subscription['id']}] Surcharge amount is 0 — skipping.")
        return

    primary_item = next(
        (i for i in subscription["items"]["data"]
         if i["price"]["product"] != SURCHARGE_PRODUCT_ID),
        None
    )
    interval = primary_item["price"].get("recurring", {}).get("interval", "month") if primary_item else "month"
    price_id = get_or_create_surcharge_price(surcharge_cents, interval)

    stripe.SubscriptionItem.create(
        subscription=subscription["id"],
        price=price_id,
        quantity=1,
        proration_behavior="none",
    )
    print(f"[{subscription['id']}] Surcharge ADDED — ${surcharge_cents / 100:.2f}/{interval}")


def remove_surcharge_from_subscription(subscription):
    surcharge_item = find_surcharge_item(subscription)
    if not surcharge_item:
        print(f"[{subscription['id']}] No surcharge item found — nothing to remove.")
        return

    stripe.SubscriptionItem.delete(
        surcharge_item["id"],
        proration_behavior="none",
    )
    print(f"[{subscription['id']}] Surcharge REMOVED")


def handle_subscription_updated(event):
    new_sub = event["data"]["object"]
    previous = event["data"].get("previous_attributes", {})

    if "default_payment_method" not in previous:
        print(f"[{new_sub['id']}] No PM change, ignoring.")
        return

    sub = stripe.Subscription.retrieve(
        new_sub["id"],
        expand=["items.data.price"]
    )
    pm_type = get_payment_method_type_from_subscription(sub)
    print(f"[{sub['id']}] PM changed → type: {pm_type}")

    if pm_type == "card":
        add_surcharge_to_subscription(sub)
    else:
        remove_surcharge_from_subscription(sub)


def handle_customer_updated(event):
    customer = event["data"]["object"]
    previous = event["data"].get("previous_attributes", {})

    if "invoice_settings" not in previous and "default_source" not in previous:
        print(f"[cus: {customer['id']}] No PM change, ignoring.")
        return

    subscriptions = stripe.Subscription.list(
        customer=customer["id"],
        status="active",
        limit=100,
        expand=["data.items.data.price"]
    )

    for sub in subscriptions["data"]:
        if sub.get("default_payment_method"):
            print(f"[{sub['id']}] Has own PM, skipping.")
            continue

        pm_type = get_payment_method_type_from_subscription(sub)
        print(f"[{sub['id']}] Customer PM changed → effective type: {pm_type}")

        if pm_type == "card":
            add_surcharge_to_subscription(sub)
        else:
            remove_surcharge_from_subscription(sub)


# ─────────────────────────────────────────────
# Invoice created handler
# (new logic — add surcharge line item on draft invoice)
# ─────────────────────────────────────────────

def handle_invoice_created(invoice):
    invoice_id = invoice["id"]

    if invoice["status"] != "draft":
        print(f"Skipping {invoice_id} — not a draft")
        return

    if not invoice.get("subscription"):
        print(f"Skipping {invoice_id} — not a subscription invoice")
        return

    if invoice.get("collection_method") != "charge_automatically":
        print(f"Skipping {invoice_id} — not autopay")
        return

    if invoice_already_has_surcharge(invoice):
        print(f"Skipping {invoice_id} — surcharge already present")
        return

    pm_type = get_payment_method_type_from_invoice(invoice)
    print(f"Invoice {invoice_id} payment method: {pm_type}")

    if pm_type != "card":
        print(f"Skipping {invoice_id} — payment method is {pm_type}, no surcharge")
        return

    post_tax_total = invoice.get("total", 0)
    if post_tax_total <= 0:
        print(f"Skipping {invoice_id} — zero or negative total")
        return

    surcharge_amount = round(post_tax_total * SURCHARGE_RATE)
    if surcharge_amount <= 0:
        print(f"Skipping {invoice_id} — surcharge rounds to zero")
        return

    print(f"Adding surcharge of ${surcharge_amount/100:.2f} to {invoice_id} "
          f"(3% of ${post_tax_total/100:.2f})")

    try:
        stripe.InvoiceItem.create(
            customer=invoice["customer"],
            invoice=invoice_id,
            amount=surcharge_amount,
            currency=invoice.get("currency", "usd"),
            description="Credit Card Processing Fee (3%)",
            metadata={"surcharge": "true", "rate": "0.03"}
        )
        print(f"✓ Surcharge added to {invoice_id}")
    except stripe.error.StripeError as e:
        print(f"✗ Failed to add surcharge to {invoice_id}: {e}")


# ─────────────────────────────────────────────
# Vercel handler
# ─────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # Vercel may lowercase headers — check both variants
        sig_header = (
            self.headers.get("stripe-signature")
            or self.headers.get("Stripe-Signature")
        )

        if not sig_header:
            print("ERROR: No stripe-signature header found")
            print(f"Available headers: {dict(self.headers)}")
            self._respond(400, {"error": "Missing stripe-signature header"})
            return

        try:
            event = stripe.Webhook.construct_event(
                raw_body, sig_header, WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError as e:
            print(f"ERROR: Signature verification failed: {e}")
            print(f"Webhook secret starts with: {WEBHOOK_SECRET[:10]}...")
            self._respond(400, {"error": str(e)})
            return

        print(f"Received event: {event['type']} [{event['id']}]")

        try:
            if event["type"] == "customer.subscription.updated":
                handle_subscription_updated(event)
            elif event["type"] == "customer.updated":
                handle_customer_updated(event)
            elif event["type"] == "invoice.created":
                handle_invoice_created(event["data"]["object"])
            else:
                print(f"Unhandled event type: {event['type']}")
        except Exception as e:
            print(f"X Failed: {e}")

        self._respond(200, {"received": True})

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
