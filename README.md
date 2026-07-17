# WhatsApp Web Automation API

A production-ready WhatsApp messaging automation system built with **Python**, **Playwright**, and **FastAPI**.

Scan the QR code once → session is saved → send messages via REST API.

---

## Features

- 🔐 **One-time QR scan** — session cookies are persisted to `session/whatsapp_auth.json`
- 🚀 **REST API** — send single or bulk messages via HTTP
- 🧠 **Concurrency-safe** — async lock prevents overlapping browser navigations
- 🛡️ **Structured errors** — every failure returns clean JSON, never a raw 500
- 🔄 **Session restart** — clear session and re-scan QR without restarting the server
- 🌐 **CORS enabled** — ready for frontend integration

---

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install the Chromium browser for Playwright
playwright install chromium
```

---

## First Run (QR Scan Required)

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

A Chromium window will open showing the WhatsApp Web QR code.
Scan it with your phone → the session is saved automatically.

---

## Subsequent Runs (Headless)

Once the session is saved, you can run headless.
In `main.py`, change the lifespan startup line:

```python
await wa_client.start(headless=True)
```

> **Note:** After scanning QR for the first time, change `headless=False` to `headless=True` in the lifespan function so future restarts run silently in the background.

---

## API Reference

### `GET /health`

Simple liveness probe — always returns 200.

```json
{ "status": "ok", "whatsapp_ready": true }
```

**PowerShell:**
```powershell
Invoke-RestMethod http://localhost:8000/health
```

**Bash:**
```bash
curl http://localhost:8000/health
```

---

### `GET /status`

Check whether the WhatsApp session is authenticated.

```json
{ "ready": true, "session_file_exists": true, "info": "WhatsApp Web automation running" }
```

**PowerShell:**
```powershell
Invoke-RestMethod http://localhost:8000/status
```

**Bash:**
```bash
curl http://localhost:8000/status
```

---

### `POST /send-message`

Send a single message.

**PowerShell:**
```powershell
Invoke-RestMethod -Uri http://localhost:8000/send-message -Method POST -ContentType "application/json" -Body '{"phone": "919876543210", "message": "Hello from the API!"}'
```

**Bash:**
```bash
curl -X POST http://localhost:8000/send-message \
  -H "Content-Type: application/json" \
  -d '{"phone": "919876543210", "message": "Hello from the API!"}'
```

**Response (success):**
```json
{ "status": "sent", "to": "919876543210", "message": "Hello%20from%20the%20API%21" }
```

---

### `POST /send-bulk`

Send multiple messages sequentially (2s gap between each). Maximum 50 messages per request.

**PowerShell:**
```powershell
Invoke-RestMethod -Uri http://localhost:8000/send-bulk -Method POST -ContentType "application/json" -Body '{"messages": [{"phone": "919876543210", "message": "First message"}, {"phone": "14155551234", "message": "Second message"}]}'
```

**Bash:**
```bash
curl -X POST http://localhost:8000/send-bulk \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"phone": "919876543210", "message": "First"}, {"phone": "14155551234", "message": "Second"}]}'
```

**Response:**
```json
{
  "total": 2,
  "sent": 2,
  "failed": 0,
  "results": [
    { "status": "sent", "to": "919876543210", "message": "First" },
    { "status": "sent", "to": "14155551234", "message": "Second" }
  ]
}
```

---

### `POST /restart-session`

Clear the saved session and open a new browser for QR scan. Useful when session expires.

**PowerShell:**
```powershell
Invoke-RestMethod -Uri http://localhost:8000/restart-session -Method POST
```

**Bash:**
```bash
curl -X POST http://localhost:8000/restart-session
```

**Response:**
```json
{ "message": "Session cleared. Please scan QR code in the browser." }
```

---

## Phone Number Format

| Country | Format         | Example        |
|---------|----------------|----------------|
| India   | `91XXXXXXXXXX` | `919876543210` |
| US      | `1XXXXXXXXXX`  | `14155551234`  |
| UK      | `44XXXXXXXXXX` | `447911123456` |

**Rules:** No `+`, no spaces, no dashes — digits only, with country code.

---

## Project Structure

```
whatsapp-automation/
├── main.py               # FastAPI app & endpoints
├── whatsapp.py           # Playwright browser client
├── session/              # Auto-created — stores auth cookies
│   └── whatsapp_auth.json
├── requirements.txt
└── README.md
```

---

## Error Handling

| Scenario                      | HTTP Code | Detail                                                  |
|-------------------------------|-----------|----------------------------------------------------------|
| QR not yet scanned            | 503       | `WhatsApp session not ready. Please wait for QR scan.`   |
| Invalid / non-WhatsApp number | 500       | `Timed out waiting for send button …`                    |
| Browser crash                 | 500       | Error string from the exception                          |
| Invalid phone format          | 422       | Pydantic validation error                                |
| Empty message                 | 422       | `Message must not be empty.`                             |
| Message too long              | 422       | `Message must not exceed 4096 characters.`               |
| Bulk > 50 messages            | 422       | `Maximum 50 messages per bulk request.`                  |
| Unhandled error               | 500       | `{"error": "Internal server error", "detail": "..."}` |
