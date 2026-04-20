import json
import os
from http.server import BaseHTTPRequestHandler

import stripe

stripe.api_key = os.environ["SURCHARGE_STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["SURCHARGE_STRIPE_WEBHOOK_SECRET"]
SURCHARGE_PRODUCT_ID = os.environ.get("SURCHARGE_PRODUCT_ID", "prod_TwsauvTg8JPMTs")
SURCHARGE_RATE = 0.03


def get_payment_method_type(subscription):
    """Get payment method type from subscription or customer default."""
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


def find_surcharge_item(subscription):
    """Check if subscription already has a surcharge item."""
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            return item
    return None


def calculate_surcharge_cents(subscription):
    """Calculate 3% of subscription total excluding surcharge items."""
    total = 0
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            continue
        total += (price.get("unit_amount") or 0) * (item.get("quantity") or 1)
    return round(total * SURCHARGE_RATE)


def get_or_create_surcharge_price(amount_cents, interval):
    """Find existing surcharge price or create a new one."""
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


def add_surcharge_to_subscription(sub):
    """Add surcharge as subscription line item."""
    if find_surcharge_item(sub):
        print(f"[{sub['id']}] Surcharge already present — skipping.")
        return
    surcharge_cents = calculate_surcharge_cents(sub)
    if surcharge_cents <= 0:
        print(f"[{sub['id']}] Surcharge is 0 — skipping.")
        return
    primary_item = next(
        (i for i in sub["items"]["data"] if i["price"]["product"] != SURCHARGE_PRODUCT_ID),
        None
    )
    interval = primary_item["price"].get("recurring", {}).get("interval", "month") if primary_item else "month"
    price_id = get_or_create_surcharge_price(surcharge_cents, interval)
    stripe.SubscriptionItem.create(
        subscription=sub["id"],
        price=price_id,
        quantity=1,
        proration_behavior="none",
    )
    print(f"[{sub['id']}] Surcharge ADDED — ${surcharge_cents / 100:.2f}/{interval}")


def remove_surcharge_from_subscription(sub):
    """Remove surcharge item from subscription."""
    surcharge_item = find_surcharge_item(sub)
    if not surcharge_item:
        print(f"[{sub['id']}] No surcharge item — nothing to remove.")
        return
    stripe.SubscriptionItem.delete(surcharge_item["id"], proration_behavior="none")
    print(f"[{sub['id']}] Surcharge REMOVED")


def handle_invoice_created(invoice):
    """
    Handle invoice.created — fires for every new subscription invoice.
    If invoice is draft, add surcharge as invoice item.
    If already finalized (charge_automatically first invoice), 
    add surcharge to the subscription so it applies to next charge.
    """
    invoice_id = invoice["id"]
    sub_id = invoice.get("subscription")

    if not sub_id:
        print(f"Skipping {invoice_id} — not a subscription invoice")
        return

    if invoice.get("collection_method") != "charge_automatically":
        print(f"Skipping {invoice_id} — not autopay")
        return

    # Get the full subscription with items
    sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
    pm_type = get_payment_method_type(sub)
    print(f"Invoice {invoice_id} — subscription {sub_id} — payment method: {pm_type}")

    if pm_type != "card":
        print(f"Skipping {invoice_id} — payment method is {pm_type}, no surcharge")
        return

    if invoice["status"] == "draft":
        # Invoice is still editable — add surcharge as invoice item
        post_tax_total = invoice.get("total", 0)
        if post_tax_total <= 0:
            print(f"Skipping {invoice_id} — zero total")
            return
        surcharge_amount = round(post_tax_total * SURCHARGE_RATE)
        print(f"Adding surcharge of ${surcharge_amount/100:.2f} to draft invoice {invoice_id}")
        try:
            stripe.InvoiceItem.create(
                customer=invoice["customer"],
                invoice=invoice_id,
                amount=surcharge_amount,
                currency=invoice.get("currency", "usd"),
                description="Credit Card Processing Fee (3%)",
                metadata={"surcharge": "true"}
            )
            print(f"✓ Surcharge added to invoice {invoice_id}")
        except stripe.error.StripeError as e:
            print(f"✗ Failed to add invoice surcharge: {e}")
    else:
        # Invoice already finalized — add surcharge to subscription instead
        # so it appears on every future charge
        print(f"Invoice {invoice_id} already finalized — adding surcharge to subscription {sub_id}")
        add_surcharge_to_subscription(sub)


def handle_subscription_created(event):
    """New subscription created — add surcharge if card."""
    sub = stripe.Subscription.retrieve(
        event["data"]["object"]["id"],
        expand=["items.data.price"]
    )
    if sub.get("collection_method") != "charge_automatically":
        print(f"[{sub['id']}] Not autopay — skipping.")
        return
    pm_type = get_payment_method_type(sub)
    print(f"[{sub['id']}] New subscription — payment method: {pm_type}")
    if pm_type == "card":
        add_surcharge_to_subscription(sub)
    else:
        print(f"[{sub['id']}] Not a card — no surcharge.")


def handle_subscription_updated(event):
    """Payment method changed on existing subscription."""
    new_sub = event["data"]["object"]
    previous = event["data"].get("previous_attributes", {})
    if "default_payment_method" not in previous:
        print(f"[{new_sub['id']}] No PM change — ignoring.")
        return
    sub = stripe.Subscription.retrieve(new_sub["id"], expand=["items.data.price"])
    pm_type = get_payment_method_type(sub)
    print(f"[{sub['id']}] PM changed → type: {pm_type}")
    if pm_type == "card":
        add_surcharge_to_subscription(sub)
    else:
        remove_surcharge_from_subscription(sub)


def handle_customer_updated(event):
    """Customer default payment method changed."""
    customer = event["data"]["object"]
    previous = event["data"].get("previous_attributes", {})
    if "invoice_settings" not in previous and "default_source" not in previous:
        print(f"[cus: {customer['id']}] No PM change — ignoring.")
        return
    subscriptions = stripe.Subscription.list(
        customer=customer["id"], status="active", limit=100,
        expand=["data.items.data.price"]
    )
    for sub in subscriptions["data"]:
        if sub.get("default_payment_method"):
            print(f"[{sub['id']}] Has own PM — skipping.")
            continue
        pm_type = get_payment_method_type(sub)
        print(f"[{sub['id']}] Customer PM changed → type: {pm_type}")
        if pm_type == "card":
            add_surcharge_to_subscription(sub)
        else:
            remove_surcharge_from_subscription(sub)


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        sig_header = self.headers.get("stripe-signature")

        try:
            event = stripe.Webhook.construct_event(raw_body, sig_header, WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError as e:
            print(f"Signature error: {e}")
            self._respond(400, {"error": str(e)})
            return

        print(f"Event: {event['type']} [{event['id']}]")

        try:
            if event["type"] == "customer.subscription.created":
                handle_subscription_created(event)
            elif event["type"] == "customer.subscription.updated":
                handle_subscription_updated(event)
            elif event["type"] == "customer.updated":
                handle_customer_updated(event)
            elif event["type"] == "invoice.created":
                handle_invoice_created(event["data"]["object"])
            else:
                print(f"Unhandled: {event['type']}")
        except Exception as e:
            print(f"ERROR: {e}")

        self._respond(200, {"received": True})

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
