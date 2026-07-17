"""
FastAPI entry-point for the WhatsApp Web automation system.

Run with:
    python -m uvicorn main:app --host 0.0.0.0 --port 8000
Or:
    python main.py
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, field_validator

from whatsapp import wa_client

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SESSION_FILE: Path = Path(__file__).parent / "session" / "whatsapp_auth.json"
TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
UPLOADS_DIR: Path = Path(__file__).parent / "uploads"


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class MessageRequest(BaseModel):
    """Schema for a single outbound message."""

    phone: str
    message: str

    @field_validator("phone", mode="before")
    @classmethod
    def sanitize_phone(cls, value: str) -> str:
        """Strip formatting characters and validate the phone number."""
        cleaned = re.sub(r"[+\s\-()\u00a0]", "", str(value))
        if not cleaned.isdigit():
            raise ValueError(
                "Phone number must contain only digits "
                "(after stripping +, spaces, dashes, parentheses)."
            )
        if len(cleaned) < 7 or len(cleaned) > 15:
            raise ValueError(
                "Phone number must be between 7 and 15 digits (with country code)."
            )
        return cleaned

    @field_validator("message", mode="before")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Ensure the message body is not empty and within WhatsApp limits."""
        stripped = str(value).strip()
        if not stripped:
            raise ValueError("Message must not be empty.")
        if len(stripped) > 4096:
            raise ValueError("Message must not exceed 4096 characters.")
        return stripped


class BulkMessageRequest(BaseModel):
    """Schema for sending multiple messages in one request."""

    messages: list[MessageRequest]

    @field_validator("messages", mode="before")
    @classmethod
    def validate_list_size(cls, value: list) -> list:
        """Ensure between 1 and 50 messages."""
        if not value or len(value) < 1:
            raise ValueError("At least 1 message is required.")
        if len(value) > 50:
            raise ValueError("Maximum 50 messages per bulk request.")
        return value


class MessageResponse(BaseModel):
    """Schema for a single message result."""

    status: str
    to: str
    message: Optional[str] = None
    error: Optional[str] = None


class BulkMessageResponse(BaseModel):
    """Schema for bulk send results."""

    total: int
    sent: int
    failed: int
    results: list[MessageResponse]


# ---------------------------------------------------------------------------
# Lifespan — browser start / stop
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the WhatsApp browser session across the server's lifetime."""
    # ── Startup ──
    print("[Server] Starting WhatsApp client...")
    await wa_client.start(headless=False)  # Change to True after first QR scan
    yield
    # ── Shutdown ──
    print("[Server] Shutting down...")
    await wa_client.close()


app = FastAPI(
    title="WhatsApp Automation API",
    description="Send WhatsApp messages via a REST API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS Middleware — allow all origins for development
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Global Exception Handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler so unhandled errors always return JSON."""
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_ui() -> HTMLResponse:
    """Serve the WhatsApp automation dashboard."""
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI template not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# QR Code Endpoints
# ---------------------------------------------------------------------------

@app.get("/qr")
async def get_qr_image() -> Response:
    """Return a PNG screenshot of the WhatsApp QR code."""
    screenshot = await wa_client.get_qr_screenshot()
    if screenshot is None:
        raise HTTPException(status_code=503, detail="Browser not available.")
    return Response(
        content=screenshot,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/qr-status")
async def qr_status() -> dict:
    """Return the current QR / login status for the UI to poll."""
    return {
        "ready": wa_client.is_ready,
        "needs_qr": wa_client.needs_qr,
        "session_file_exists": SESSION_FILE.exists(),
    }


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Simple liveness probe — always returns 200."""
    return {"status": "ok", "whatsapp_ready": wa_client.is_ready}


@app.get("/status")
async def status() -> dict:
    """Report whether the WhatsApp session is authenticated and ready."""
    return {
        "ready": wa_client.is_ready,
        "session_file_exists": SESSION_FILE.exists(),
        "info": "WhatsApp Web automation running",
    }


@app.post("/send-message", response_model=MessageResponse)
async def send_message(req: MessageRequest) -> MessageResponse:
    """Send a single WhatsApp message.

    The message text is URL-encoded before being passed to the browser
    client, since whatsapp.py builds the send URL with the raw text
    as a query parameter.
    """
    if not wa_client.is_ready:
        raise HTTPException(
            status_code=503,
            detail="WhatsApp session not ready. Please wait for QR scan.",
        )

    # URL-encode the message so spaces & special chars don't break the URL.
    encoded_message = quote(req.message, safe="")
    result = await wa_client.send_message(req.phone, encoded_message)

    if result["status"] == "failed":
        raise HTTPException(status_code=500, detail=result["error"])

    return MessageResponse(**result)


@app.post("/send-bulk", response_model=BulkMessageResponse)
async def send_bulk(body: BulkMessageRequest) -> BulkMessageResponse:
    """Send multiple WhatsApp messages sequentially.

    Each message is sent with a 2-second gap to avoid rate-limiting.
    The browser cannot handle parallel navigation, so messages are
    processed one at a time.
    """
    if not wa_client.is_ready:
        raise HTTPException(
            status_code=503,
            detail="WhatsApp session not ready. Please wait for QR scan.",
        )

    results: list[MessageResponse] = []
    sent_count = 0
    failed_count = 0

    for idx, req in enumerate(body.messages):
        encoded_message = quote(req.message, safe="")
        result = await wa_client.send_message(req.phone, encoded_message)
        response = MessageResponse(**result)
        results.append(response)

        if result["status"] == "sent":
            sent_count += 1
        else:
            failed_count += 1

        # Brief pause between messages (skip after the last one).
        if idx < len(body.messages) - 1:
            await asyncio.sleep(2)

    return BulkMessageResponse(
        total=len(results),
        sent=sent_count,
        failed=failed_count,
        results=results,
    )


@app.post("/send-document")
async def send_document(
    phone: str = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...),
) -> dict:
    """Send a document/file to a WhatsApp number.

    Accepts multipart form data with phone, optional caption, and file.
    """
    if not wa_client.is_ready:
        raise HTTPException(
            status_code=503,
            detail="WhatsApp session not ready. Please wait for QR scan.",
        )

    # Validate phone.
    import re as _re
    cleaned_phone = _re.sub(r"[+\s\-()\u00a0]", "", phone)
    if not cleaned_phone.isdigit() or len(cleaned_phone) < 7 or len(cleaned_phone) > 15:
        raise HTTPException(status_code=422, detail="Invalid phone number format.")

    # Save uploaded file to temp directory.
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    file_path = UPLOADS_DIR / file.filename
    try:
        contents = await file.read()
        file_path.write_bytes(contents)

        result = await wa_client.send_document(
            cleaned_phone, str(file_path), caption=caption
        )

        if result["status"] == "failed":
            raise HTTPException(status_code=500, detail=result["error"])

        return result

    finally:
        # Clean up temp file.
        if file_path.exists():
            file_path.unlink()


@app.post("/restart-session")
async def restart_session() -> dict:
    """Clear the saved session and restart the browser for a fresh QR scan."""
    # Delete stored session file.
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        print("[Server] 🗑️  Deleted session file.")

    # Mark client as not ready and shut down the browser.
    wa_client.is_ready = False
    await wa_client.close()

    # Restart the browser — user will need to scan QR again.
    await wa_client.start(headless=False)

    return {"message": "Session cleared. Please scan QR code in the browser."}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
