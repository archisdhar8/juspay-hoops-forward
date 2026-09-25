from __future__ import annotations

import os
from pathlib import Path
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models.donation import CAMPAIGNS, DonationValidationError, parse_donation
from services.hyperswitch import HyperswitchClient, HyperswitchError


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

app = FastAPI(
    title="Hoops Forward",
    description="Youth basketball donation checkout using Juspay Hyperswitch sandbox.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"campaigns": CAMPAIGNS, "configured": HyperswitchClient().configured},
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
        donation = parse_donation(
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
                "configured": HyperswitchClient().configured,
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

    return templates.TemplateResponse(
        request,
        "review.html",
        {"donation": donation, "configured": HyperswitchClient().configured},
    )


@app.post("/payments", response_model=None)
async def create_payment(
    request: Request,
    campaign_id: str = Form(...),
    amount: str = Form(...),
    donor_name: str = Form(""),
    donor_email: str = Form(...),
    anonymous: bool = Form(False),
    cover_fees: bool = Form(False),
):
    try:
        donation = parse_donation(
            campaign_id=campaign_id,
            amount=amount,
            donor_name=donor_name,
            donor_email=donor_email,
            anonymous=anonymous,
            cover_fees=cover_fees,
        )
        donation_reference = f"HF-{secrets.token_hex(4).upper()}"
        base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            base_url = str(request.base_url).rstrip("/")
        return_url = f"{base_url}/payment/return?donation_ref={donation_reference}"
        payment = await HyperswitchClient().create_payment(
            donation=donation,
            donation_reference=donation_reference,
            return_url=return_url,
        )
    except (DonationValidationError, HyperswitchError) as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=502 if isinstance(exc, HyperswitchError) else 422,
        )

    response = RedirectResponse(payment.checkout_url, status_code=303)
    response.set_cookie(
        "hf_payment_id",
        payment.payment_id,
        max_age=60 * 30,
        httponly=True,
        secure=base_url.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/payment/return", response_class=HTMLResponse)
async def payment_return(
    request: Request,
    donation_ref: str = "",
    payment_id: str = "",
) -> HTMLResponse:
    resolved_payment_id = payment_id or request.cookies.get("hf_payment_id", "")
    if not resolved_payment_id:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "message": "We could not identify this payment. Check the Hyperswitch dashboard before retrying."
            },
            status_code=400,
        )

    try:
        payment = await HyperswitchClient().retrieve_payment(resolved_payment_id)
    except HyperswitchError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc), "payment_id": resolved_payment_id},
            status_code=502,
        )

    status = str(payment.get("status", "unknown")).lower()
    if status == "succeeded":
        template = "success.html"
    elif status in {"failed", "cancelled"}:
        template = "failed.html"
    else:
        template = "processing.html"
    metadata = payment.get("metadata") or {}
    reference = metadata.get("donation_reference") or donation_ref or "Pending"
    response = templates.TemplateResponse(
        request,
        template,
        {
            "payment": payment,
            "payment_id": resolved_payment_id,
            "status": status,
            "donation_reference": reference,
            "campaign": CAMPAIGNS.get(metadata.get("campaign_id", "")),
        },
    )
    if status in {"succeeded", "failed", "cancelled"}:
        response.delete_cookie("hf_payment_id")
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "payments": "configured" if HyperswitchClient().configured else "not_configured",
    }
