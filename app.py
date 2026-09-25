from __future__ import annotations

import os
from pathlib import Path
import hmac
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from core.database import init_database, persistence_mode, session_scope  # noqa: E402
from domain.states import PaymentStatus
from models.donation import CAMPAIGNS, DonationValidationError, parse_donation
from services.donation_service import create_donation_intent, get_donation
from services.hyperswitch import HyperswitchProvider
from services.payment_provider import PaymentProviderError
from services.payment_service import (
    PaymentOperationError,
    create_or_reuse_payment_attempt,
    get_bound_attempt,
    prepare_payment_retry,
    verify_payment_attempt,
)
from services.operations_service import (
    build_operations_overview,
    donation_operations_detail,
    donor_display,
)
from services.reconciliation_service import reconcile_payment_attempt
from services.webhook_service import WebhookProcessingError, store_and_process_webhook

init_database()

app = FastAPI(
    title="Hoops Forward",
    description="Youth basketball donation checkout using Juspay Hyperswitch sandbox.",
    version="0.2.0",
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
operations_security = HTTPBasic(auto_error=False)


def require_operations_access(
    credentials: HTTPBasicCredentials | None = Depends(operations_security),
) -> str:
    expected_password = os.getenv("OPERATIONS_ADMIN_TOKEN", "").strip()
    expected_username = os.getenv("OPERATIONS_ADMIN_USER", "operator").strip()
    if not expected_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operations access is not configured.",
        )
    valid = bool(credentials) and hmac.compare_digest(
        credentials.username.encode(), expected_username.encode()
    ) and hmac.compare_digest(
        credentials.password.encode(), expected_password.encode()
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operations credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return expected_username


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"campaigns": CAMPAIGNS, "configured": HyperswitchProvider().configured},
    )


@app.post("/review", response_class=HTMLResponse)
async def review(
    request: Request,
    campaign_id: str = Form(...),
    amount: str = Form(...),
    donor_name: str = Form(""),
    donor_email: str = Form(...),
    anonymous: bool = Form(False),
    cover_fees: bool = Form(False),
) -> HTMLResponse:
    try:
        donation_input = parse_donation(
            campaign_id=campaign_id,
            amount=amount,
            donor_name=donor_name,
            donor_email=donor_email,
            anonymous=anonymous,
            cover_fees=cover_fees,
        )
    except DonationValidationError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "campaigns": CAMPAIGNS,
                "configured": HyperswitchProvider().configured,
                "error": str(exc),
                "form": {
                    "campaign_id": campaign_id,
                    "amount": amount,
                    "donor_name": donor_name,
                    "donor_email": donor_email,
                    "anonymous": anonymous,
                    "cover_fees": cover_fees,
                },
            },
            status_code=422,
        )

    with session_scope() as session:
        donation = create_donation_intent(session, donation_input)
        session.commit()

    return templates.TemplateResponse(
        request,
        "review.html",
        {"donation": donation, "configured": HyperswitchProvider().configured},
    )


@app.post("/payments", response_model=None)
async def create_payment(request: Request, donation_id: str = Form(...)):
    provider = HyperswitchProvider()
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        base_url = str(request.base_url).rstrip("/")

    try:
        with session_scope() as session:
            donation = get_donation(session, donation_id)
            attempt = await create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base=base_url,
                provider=provider,
            )
            checkout_url = attempt.checkout_url
    except (PaymentProviderError, PaymentOperationError, LookupError) as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=502 if isinstance(exc, PaymentProviderError) else 409,
        )

    if not checkout_url:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "The payment checkout is still being prepared."},
            status_code=409,
        )
    return RedirectResponse(checkout_url, status_code=303)


@app.post("/donations/{donation_id}/retry", response_model=None)
async def retry_payment(
    request: Request,
    donation_id: str,
    attempt_id: str = Form(...),
):
    provider = HyperswitchProvider()
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        base_url = str(request.base_url).rstrip("/")
    try:
        with session_scope() as session:
            donation = prepare_payment_retry(session, donation_id, attempt_id)
            session.commit()
            attempt = await create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base=base_url,
                provider=provider,
            )
            checkout_url = attempt.checkout_url
    except (PaymentProviderError, PaymentOperationError) as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=502 if isinstance(exc, PaymentProviderError) else 409,
        )
    if not checkout_url:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "The retry checkout is still being prepared."},
            status_code=409,
        )
    return RedirectResponse(checkout_url, status_code=303)


@app.get("/payment/return", response_class=HTMLResponse)
async def payment_return(
    request: Request,
    donation_id: str = "",
    attempt_id: str = "",
) -> HTMLResponse:
    try:
        with session_scope() as session:
            donation, attempt = get_bound_attempt(session, donation_id, attempt_id)
            context = {
                "donation_id": donation.id,
                "attempt_id": attempt.id,
                "donation_reference": donation.id,
            }
    except PaymentOperationError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(request, "verifying.html", context)


@app.post("/api/donations/{donation_id}/attempts/{attempt_id}/verify")
async def verify_payment(donation_id: str, attempt_id: str) -> JSONResponse:
    try:
        with session_scope() as session:
            result = await verify_payment_attempt(
                session=session,
                donation_id=donation_id,
                attempt_id=attempt_id,
                provider=HyperswitchProvider(),
            )
    except (PaymentProviderError, PaymentOperationError) as exc:
        return JSONResponse(
            {"status": "verification_error", "message": str(exc)}, status_code=502
        )
    return JSONResponse(
        {
            "status": result.status.value,
            "destination": result.destination,
        }
    )


@app.get("/donations/{donation_id}/confirmation", response_class=HTMLResponse)
async def donation_confirmation(
    request: Request,
    donation_id: str,
    attempt_id: str = "",
) -> HTMLResponse:
    try:
        with session_scope() as session:
            donation, attempt = get_bound_attempt(session, donation_id, attempt_id)
            status = PaymentStatus(attempt.status)
            if status == PaymentStatus.SUCCEEDED:
                template = "success.html"
            elif status in {
                PaymentStatus.FAILED,
                PaymentStatus.CANCELLED,
                PaymentStatus.EXPIRED,
            }:
                template = "failed.html"
            else:
                template = "processing.html"
            context = {
                "payment_id": attempt.hyperswitch_payment_id or "Pending",
                "attempt_id": attempt.id,
                "status": status.value,
                "donation_reference": donation.id,
                "donation": donation,
                "campaign": donation.campaign,
                "tax_disclosure": donation.organization.tax_disclosure,
            }
    except (PaymentOperationError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(request, template, context)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "payments": "configured" if HyperswitchProvider().configured else "not_configured",
        "persistence": persistence_mode(),
    }


@app.get("/operations", response_class=HTMLResponse)
async def operations_dashboard(
    request: Request,
    _operator: str = Depends(require_operations_access),
) -> HTMLResponse:
    with session_scope() as session:
        overview = build_operations_overview(session)
    return templates.TemplateResponse(
        request,
        "operations.html",
        {"overview": overview, "money": lambda cents: f"${cents / 100:,.2f}"},
    )


@app.get("/operations/donations/{donation_id}", response_class=HTMLResponse)
async def operations_donation_detail(
    request: Request,
    donation_id: str,
    notice: str = "",
    _operator: str = Depends(require_operations_access),
) -> HTMLResponse:
    try:
        with session_scope() as session:
            donation, attempts, events, ledger = donation_operations_detail(
                session, donation_id
            )
            context = {
                "donation": donation,
                "attempts": attempts,
                "events": events,
                "ledger": ledger,
                "donor_display": donor_display(donation),
                "notice": notice,
            }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(request, "operation_detail.html", context)


@app.post("/operations/donations/{donation_id}/attempts/{attempt_id}/reconcile")
async def operations_reconcile_attempt(
    donation_id: str,
    attempt_id: str,
    _operator: str = Depends(require_operations_access),
):
    try:
        with session_scope() as session:
            result = await reconcile_payment_attempt(
                session=session,
                donation_id=donation_id,
                attempt_id=attempt_id,
                provider=HyperswitchProvider(),
            )
        notice = f"Reconciled as {result.status.value}."
    except (PaymentProviderError, PaymentOperationError, ValueError) as exc:
        notice = f"Reconciliation failed: {exc}"
    return RedirectResponse(
        f"/operations/donations/{donation_id}?notice={quote(notice)}",
        status_code=303,
    )


@app.post("/webhooks/hyperswitch")
async def hyperswitch_webhook(request: Request) -> JSONResponse:
    provider = HyperswitchProvider()
    raw_payload = await request.body()
    signature = request.headers.get("x-webhook-signature-512", "")
    if not provider.verify_webhook(raw_payload, signature):
        return JSONResponse({"received": False, "error": "invalid_signature"}, status_code=401)
    try:
        payload = provider.parse_webhook(raw_payload)
        with session_scope() as session:
            result = await store_and_process_webhook(
                session=session,
                provider=provider,
                payload=payload,
                raw_payload=raw_payload,
            )
    except WebhookProcessingError as exc:
        return JSONResponse(
            {"received": False, "error": str(exc)}, status_code=400
        )
    except PaymentProviderError:
        return JSONResponse(
            {"received": False, "error": "provider_temporarily_unavailable"},
            status_code=503,
        )
    return JSONResponse(
        {
            "received": True,
            "event_id": result.event_id,
            "processing_status": result.status,
            "duplicate": result.duplicate,
        }
    )
