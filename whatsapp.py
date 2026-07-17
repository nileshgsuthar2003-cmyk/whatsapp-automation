"""
WhatsApp Web browser automation client using Playwright.

Handles browser lifecycle, QR-code authentication, session persistence,
and message delivery through the WhatsApp Web interface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SESSION_DIR: Path = Path(__file__).parent / "session"
AUTH_FILE: Path = SESSION_DIR / "whatsapp_auth.json"

WHATSAPP_URL = "https://web.whatsapp.com"
CHAT_LIST_SELECTOR = '[data-testid="chat-list"]'

# Multiple selectors for the send button — WhatsApp Web changes these often.
SEND_BTN_SELECTORS = [
    '[data-testid="send"]',
    'span[data-icon="send"]',
    'button[aria-label="Send"]',
    '[aria-label="Send"]',
]


class WhatsAppClient:
    """Singleton-style WhatsApp Web automation client.

    Manages a single Chromium browser instance, handles QR-code login,
    persists session cookies to disk, and exposes an async method to
    send messages to any phone number.
    """

    def __init__(self) -> None:
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.playwright: Optional[Playwright] = None
        self.is_ready: bool = False
        self.needs_qr: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._login_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, headless: bool = False) -> None:
        """Launch the browser and navigate to WhatsApp Web.

        Args:
            headless: If *True* the browser runs without a visible window.
                      Set to *False* on first run so the QR code is visible.
        """
        print("[WhatsApp] Starting Playwright browser …")
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

        self.playwright = await async_playwright().start()

        # If a saved session exists, inject it as the browser-context storage state.
        storage_state = str(AUTH_FILE) if AUTH_FILE.exists() else None

        browser_context_options: dict = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if storage_state:
            browser_context_options["storage_state"] = storage_state

        self.browser = await self.playwright.chromium.launch(headless=headless)
        context = await self.browser.new_context(**browser_context_options)
        self.page = await context.new_page()

        print(f"[WhatsApp] Navigating to {WHATSAPP_URL} …")
        await self.page.goto(WHATSAPP_URL, wait_until="domcontentloaded")

        # Start login check in background so the API is available immediately.
        self._login_task = asyncio.create_task(self._wait_for_login())

    @staticmethod
    async def _block_heavy_resources(route) -> None:
        """Abort requests for images, media, and fonts to speed up navigation."""
        blocked = {"image", "media", "font"}
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()

    async def _wait_for_login(self) -> None:
        """Wait for the chat list to appear, prompting a QR scan if necessary."""
        try:
            # Quick check — does an existing session restore instantly?
            await self.page.wait_for_selector(CHAT_LIST_SELECTOR, timeout=8_000)
            print("[WhatsApp] ✅ Session restored — already logged in.")
            self.is_ready = True
            self.needs_qr = False
            return
        except PlaywrightTimeout:
            pass

        # Session did not restore; user must scan the QR code.
        self.needs_qr = True
        print("[WhatsApp] 📱 Please scan the QR code …")
        try:
            await self.page.wait_for_selector(CHAT_LIST_SELECTOR, timeout=120_000)
        except PlaywrightTimeout:
            print("[WhatsApp] ❌ QR scan timed out after 120 seconds.")
            return

        # Persist the authenticated session so QR is only needed once.
        await self.page.context.storage_state(path=str(AUTH_FILE))
        print(f"[WhatsApp] 💾 Session saved to {AUTH_FILE}")
        self.is_ready = True
        self.needs_qr = False

    # ------------------------------------------------------------------
    # QR Screenshot
    # ------------------------------------------------------------------

    async def get_qr_screenshot(self) -> Optional[bytes]:
        """Capture a screenshot of the QR code area from WhatsApp Web.

        Returns:
            PNG bytes of the QR code element, or full page if QR element not found.
            None if the page is not available.
        """
        if not self.page:
            return None

        try:
            # Try to capture just the QR canvas element.
            qr_selectors = [
                'canvas[aria-label="Scan this QR code to link a device!"]',
                'div[data-testid="qrcode"] canvas',
                'canvas[role="img"]',
                'div._akau canvas',
                'canvas',
            ]
            for selector in qr_selectors:
                element = await self.page.query_selector(selector)
                if element:
                    return await element.screenshot(type="png")

            # Fallback: capture the full page.
            return await self.page.screenshot(type="png", full_page=False)
        except Exception as exc:
            print(f"[WhatsApp] Screenshot error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_message(self, phone: str, message: str) -> dict:
        """Send a text message to the given phone number.

        Args:
            phone: Digits-only phone number **with** country code (e.g. ``919876543210``).
            message: The text content to send.

        Returns:
            A dict with ``status`` (``"sent"`` | ``"failed"``), ``to``, and ``message`` or ``error``.

        Raises:
            RuntimeError: If the client has not finished logging in yet.
        """
        if not self.is_ready:
            raise RuntimeError("WhatsApp client is not ready. Wait for QR scan.")

        async with self._lock:
            try:
                send_url = (
                    f"{WHATSAPP_URL}/send?phone={phone}"
                    f"&text={message}"
                )
                print(f"[WhatsApp] Sending to {phone} …")

                # Block heavy resources during message navigation for speed.
                await self.page.route("**/*", self._block_heavy_resources)

                # 1. Navigate — domcontentloaded is more reliable than commit
                #    for WhatsApp's SPA behaviour
                await self.page.goto(send_url, wait_until="domcontentloaded", timeout=20_000)

                # 2. Wait for the compose box FIRST (appears before send button)
                #    This unblocks the send button wait much faster
                compose_selector = (
                    '[data-testid="conversation-compose-box-input"], '
                    'div[contenteditable="true"][role="textbox"]'
                )
                try:
                    await self.page.wait_for_selector(compose_selector, timeout=12_000)
                except PlaywrightTimeout:
                    raise PlaywrightTimeout(f"Chat did not load for {phone}")

                # 3. Now look for send button — it appears right after compose box
                combined_selector = ", ".join(SEND_BTN_SELECTORS)
                sent = False
                try:
                    btn = await self.page.wait_for_selector(
                        combined_selector, timeout=8_000
                    )
                    if btn:
                        await btn.click()
                        sent = True
                except PlaywrightTimeout:
                    pass

                # 4. Enter fallback
                if not sent:
                    print("[WhatsApp] No send button — trying Enter key …")
                    input_box = await self.page.query_selector(compose_selector)
                    if input_box:
                        await input_box.press("Enter")
                        sent = True
                    else:
                        raise PlaywrightTimeout("Could not find compose box.")

                # 5. Reduced sleep — message is already queued by the time click fires
                await asyncio.sleep(0.3)

                print(f"[WhatsApp] Sent to {phone}")

                # Remove route blocking so QR screenshots work normally.
                await self.page.unroute("**/*", self._block_heavy_resources)

                return {"status": "sent", "to": phone, "message": message}

            except PlaywrightTimeout as exc:
                error_msg = (
                    f"Timed out — {phone} may be invalid or not on WhatsApp."
                )
                print(f"[WhatsApp] {error_msg}")
                try:
                    await self.page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
                except Exception:
                    pass
                return {"status": "failed", "error": error_msg, "to": phone}

            except Exception as exc:
                error_msg = str(exc)
                print(f"[WhatsApp] Error: {error_msg}")
                self.is_ready = False
                try:
                    await self.page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
                    self.is_ready = True
                except Exception:
                    pass
                return {"status": "failed", "error": error_msg, "to": phone}

    # ------------------------------------------------------------------
    # Document Sending
    # ------------------------------------------------------------------

    async def send_document(
        self, phone: str, file_path: str, caption: str = ""
    ) -> dict:
        """Send a document/file to the given phone number.

        Args:
            phone: Digits-only phone number with country code.
            file_path: Absolute path to the file to send.
            caption: Optional caption text for the document.

        Returns:
            A dict with ``status``, ``to``, and ``file`` or ``error``.
        """
        if not self.is_ready:
            raise RuntimeError("WhatsApp client is not ready. Wait for QR scan.")

        async with self._lock:
            try:
                send_url = f"{WHATSAPP_URL}/send?phone={phone}"
                print(f"[WhatsApp] 📎 Sending document to {phone} …")

                await self.page.goto(send_url, wait_until="domcontentloaded", timeout=20_000)

                # Wait for compose box to confirm chat loaded.
                compose_selector = (
                    '[data-testid="conversation-compose-box-input"], '
                    'div[contenteditable="true"][role="textbox"]'
                )
                try:
                    await self.page.wait_for_selector(compose_selector, timeout=12_000)
                except PlaywrightTimeout:
                    raise PlaywrightTimeout(f"Chat did not load for {phone}")

                # Click the attachment / paperclip button.
                attach_selectors = [
                    '[data-testid="clip"]',
                    'span[data-icon="clip"]',
                    'span[data-icon="plus"]',
                    '[aria-label="Attach"]',
                    '[title="Attach"]',
                ]
                attached = False
                for sel in attach_selectors:
                    btn = await self.page.query_selector(sel)
                    if btn:
                        await btn.click()
                        attached = True
                        break

                if not attached:
                    raise PlaywrightTimeout("Could not find attachment button.")

                await asyncio.sleep(0.5)

                # Find the document file input and set the file.
                # WhatsApp Web has hidden <input type="file"> elements
                # that appear after clicking the attach button.
                file_input_selectors = [
                    'input[data-testid="attach-document"][type="file"]',
                    'input[accept="*"][type="file"]',
                    'li button[aria-label="Document"] + input[type="file"]',
                    'input[type="file"]',
                ]
                file_input = None
                for sel in file_input_selectors:
                    inputs = await self.page.query_selector_all(sel)
                    for inp in inputs:
                        # Check if the input accepts documents (not just images).
                        accept = await inp.get_attribute("accept") or ""
                        if accept == "*" or not accept or "image" not in accept:
                            file_input = inp
                            break
                    if file_input:
                        break

                if not file_input:
                    # Fallback: just grab the first file input.
                    file_input = await self.page.query_selector('input[type="file"]')

                if not file_input:
                    raise PlaywrightTimeout("Could not find file input element.")

                await file_input.set_input_files(file_path)
                print(f"[WhatsApp] 📄 File selected: {file_path}")

                # Wait for the file preview / send dialog to appear.
                await asyncio.sleep(2)

                # Add caption if provided.
                if caption:
                    caption_selectors = [
                        'div[data-testid="media-caption-input-container"] div[contenteditable="true"]',
                        'div[contenteditable="true"][role="textbox"][data-tab]',
                        '.copyable-area div[contenteditable="true"]',
                    ]
                    for sel in caption_selectors:
                        caption_box = await self.page.query_selector(sel)
                        if caption_box:
                            await caption_box.click()
                            await caption_box.type(caption, delay=20)
                            break

                # Click the send button in the preview dialog.
                send_selectors = [
                    '[data-testid="send"]',
                    'span[data-icon="send"]',
                    '[aria-label="Send"]',
                ]
                combined = ", ".join(send_selectors)
                try:
                    send_btn = await self.page.wait_for_selector(combined, timeout=8_000)
                    if send_btn:
                        await send_btn.click()
                except PlaywrightTimeout:
                    # Try pressing Enter as fallback.
                    await self.page.keyboard.press("Enter")

                await asyncio.sleep(2)

                print(f"[WhatsApp] ✅ Document sent to {phone}")
                return {
                    "status": "sent",
                    "to": phone,
                    "file": Path(file_path).name,
                }

            except PlaywrightTimeout as exc:
                error_msg = str(exc) if str(exc) else f"Timed out — {phone} may be invalid or not on WhatsApp."
                print(f"[WhatsApp] {error_msg}")
                try:
                    await self.page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
                except Exception:
                    pass
                return {"status": "failed", "error": error_msg, "to": phone}

            except Exception as exc:
                error_msg = str(exc)
                print(f"[WhatsApp] Error: {error_msg}")
                self.is_ready = False
                try:
                    await self.page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
                    self.is_ready = True
                except Exception:
                    pass
                return {"status": "failed", "error": error_msg, "to": phone}

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Gracefully shut down the browser and Playwright."""
        print("[WhatsApp] Shutting down …")
        try:
            if self.browser:
                await self.browser.close()
        except Exception as exc:
            print(f"[WhatsApp] Warning during browser close: {exc}")
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as exc:
            print(f"[WhatsApp] Warning during playwright stop: {exc}")
        self.is_ready = False
        print("[WhatsApp] 🛑 Shutdown complete.")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
wa_client: WhatsAppClient = WhatsAppClient()
