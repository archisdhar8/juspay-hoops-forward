# Hoops Forward

Hoops Forward is a sandbox donation experience for a fictional US nonprofit that funds youth basketball programs. The prototype demonstrates a complete donation journey backed by Juspay Hyperswitch: campaign selection, server-side amount validation, hosted checkout, and authoritative payment verification.

## Stack

- Python 3.12
- FastAPI and Jinja2
- HTTPX for Hyperswitch API calls
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
| `PUBLIC_BASE_URL` | Recommended on Vercel | Public deployment origin used for the payment return URL |

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

The prototype implements one-time USD card donations and a hosted payment page. Recurring gifts, ACH, emailed tax receipts, durable donor records, refunds, and webhook-driven reconciliation are intentionally deferred and should be discussed in the architecture decision document.
