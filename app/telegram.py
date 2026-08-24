import os
import httpx

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE = f"https://api.telegram.org/bot{TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{TOKEN}"


async def send_text(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=30,
        )
        r.raise_for_status()


async def download_file(file_id: str) -> bytes:
    """Telegram downloads are two-step: resolve file_id -> file_path, then fetch it."""
    async with httpx.AsyncClient() as client:
        meta = await client.get(f"{API_BASE}/getFile", params={"file_id": file_id}, timeout=30)
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]

        file_resp = await client.get(f"{FILE_BASE}/{file_path}", timeout=60)
        file_resp.raise_for_status()
        return file_resp.content


async def set_webhook(url: str) -> dict:
    """Convenience helper — call once to register the webhook (or just use curl, see README)."""
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/setWebhook", json={"url": url}, timeout=30)
        r.raise_for_status()
        return r.json()
