# Hoops Forward

Hoops Forward is a sandbox donation experience for a fictional US nonprofit that funds youth basketball programs. The prototype demonstrates a complete donation journey backed by Juspay Hyperswitch: persistent donation intent, idempotent payment-attempt creation, hosted checkout, and authoritative backend verification.

## Stack

- Python 3.12
- FastAPI and Jinja2
- HTTPX for Hyperswitch API calls
- SQLAlchemy with SQLite locally and PostgreSQL in hosted environments
- Plain CSS and minimal browser JavaScript
- Vercel Python runtime

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>.

Add the full sandbox API key to `.env`. Never commit `.env` or paste the key into browser code.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `HYPERSWITCH_API_KEY` | Yes for checkout | Secret sandbox API key used only by FastAPI |
| `HYPERSWITCH_BASE_URL` | No | Defaults to `https://sandbox.hyperswitch.io` |
| `HYPERSWITCH_PROFILE_ID` | No | Business profile to use for checkout |
| `HYPERSWITCH_WEBHOOK_SECRET` | Required for webhooks | Rotated payment-response hash key used only for exact-body HMAC-SHA512 verification |
| `PUBLIC_BASE_URL` | Recommended on Vercel | Public deployment origin used for the payment return URL |
| `DATABASE_URL` | Required for durable hosted records | PostgreSQL connection URL; local development defaults to ignored SQLite data |
| `OPERATIONS_ADMIN_USER` | No | Prototype operations username; defaults to `operator` |
| `OPERATIONS_ADMIN_TOKEN` | Required for operations | Strong prototype credential stored only in environment configuration |

Vercel falls back to ephemeral SQLite only to keep preview deployments bootable. Set a managed PostgreSQL `DATABASE_URL` before treating a deployment as durable; `/health` reports the active persistence mode.

## Payment integrity

- `Donation` stores the donor's stable contribution intent.
- `PaymentAttempt` stores each execution against Hyperswitch; failed retries remain attached to the same donation.
- The browser return carries only internal opaque IDs and initially renders a verification state.
- The backend retrieves the bound Hyperswitch payment, validates payment ID, metadata binding, amount, and currency, then persists the result.
- Confirmation pages read persisted backend state and never trust redirect status parameters.
- Payment creation is protected by a database-unique idempotency key, so repeated form submission reuses the same checkout.
- Signed webhooks are stored before idempotent processing and can complete a donation when the browser never returns.
- A unique financial-effect key ensures success, refund, and dispute events cannot change the ledger twice.

## Webhooks and operations

Rotate the payment-response hash key that appeared in the development screenshot before enabling webhooks. Put the replacement only in local/Vercel environment configuration; never commit it. Configure Hyperswitch to deliver supported events to:

```text
https://YOUR_DEPLOYMENT/webhooks/hyperswitch
```

The handler verifies the exact request body with HMAC-SHA512 and a constant-time comparison. Invalid signatures are rejected without storing an event. Valid events use a durable inbox, duplicate-event protection, ordering checks, guarded transitions, and authoritative retrieval when the payload is ambiguous.

`/operations` requires HTTP Basic authentication. It is a focused investigation view with masked donor display, payment timeline, ledger, exception rules, and an explicit reconcile action for a pending attempt. It never displays donor email.

## Test cards

Use only in the Hyperswitch sandbox:

- Success: `4242 4242 4242 4242`
- Decline: `4000 0000 0000 0002`
- Any future expiry and any three-digit CVC

## Test

```bash
pytest
```

## Scope

The prototype implements one-time USD card donations, hosted checkout, authoritative payment verification, signed webhook recovery, append-only ledger effects, and protected payment operations. It handles provider-confirmed refund and dispute events but intentionally does not expose a refund-creation UI. ACH, recurring gifts, settlement/fee ingestion, emailed receipts, and multi-organization payouts remain deliberately deferred.

The seeded recipient is fictional and marked `demo_only`; the application does not represent contributions as tax-deductible or issue charitable tax receipts. Campaign media must be fictional or used only with documented guardian approval.
