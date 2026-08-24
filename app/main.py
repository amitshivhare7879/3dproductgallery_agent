import os
import tempfile
from fastapi import FastAPI, Request, Response

from . import telegram, listing, stl_utils, django_client
from .state import get_draft, clear_draft

app = FastAPI()

# Optional but recommended: set this to a random string, and pass the same
# value as `secret_token` when you call setWebhook (see README). Telegram
# will echo it back in this header on every webhook call, so you can verify
# requests actually came from Telegram.
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

# Comma-separated Telegram numeric user IDs allowed to use the bot.
# Leave empty during first setup to find your own ID (it'll be echoed back
# in the "unauthorized" reply the bot sends), then lock it down.
ALLOWED_SENDERS = {s for s in os.environ.get("ALLOWED_SENDERS", "").split(",") if s}

CONFIRM_WORDS = {"yes", "confirm", "publish", "ok", "okay"}
CANCEL_WORDS = {"no", "cancel", "stop"}


@app.post("/webhook")
async def receive_update(request: Request):
    if WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != WEBHOOK_SECRET:
            return Response(status_code=403)

    body = await request.json()
    msg = body.get("message")
    if not msg:
        return {"ok": True}  # edited messages, other update types -> ignore

    chat_id = msg["chat"]["id"]
    sender = str(msg["from"]["id"])

    if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
        await telegram.send_text(
            chat_id,
            f"Not authorized. Your Telegram user ID is {sender} — "
            f"add it to ALLOWED_SENDERS if this is you.",
        )
        return {"ok": True}

    draft = get_draft(sender)

    # --- Photo received: stash it and keep collecting ---
    if "photo" in msg:
        # Telegram sends multiple sizes; the last one is the largest.
        file_id = msg["photo"][-1]["file_id"]
        media_bytes = await telegram.download_file(file_id)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(media_bytes)
        draft.images.append(path)
        await telegram.send_text(
            chat_id,
            f"Got photo #{len(draft.images)}. Send more, the STL file, "
            f"or a short description whenever you're ready.",
        )
        return {"ok": True}

    # --- STL file received (sent as a Document in Telegram) ---
    if "document" in msg and msg["document"]["file_name"].lower().endswith(".stl"):
        file_id = msg["document"]["file_id"]
        media_bytes = await telegram.download_file(file_id)
        fd, path = tempfile.mkstemp(suffix=".stl")
        with os.fdopen(fd, "wb") as f:
            f.write(media_bytes)
        draft.stl_path = path
        draft.stl_stats = stl_utils.analyze_stl(path)
        stats = draft.stl_stats
        await telegram.send_text(
            chat_id,
            f"Got the STL. Dimensions ~{stats['dims_cm']} cm, "
            f"estimated weight ~{stats['weight_g']}g. Send a short description "
            f"when ready, e.g. 'red dragon miniature, fantasy category'.",
        )
        return {"ok": True}

    # --- Text message ---
    if "text" in msg:
        text = msg["text"].strip()
        lower = text.lower()

        # Case 1: awaiting confirmation on an already-generated draft
        if draft.status == "awaiting_confirmation":
            if lower in CONFIRM_WORDS:
                result = await django_client.publish_product(draft)
                await telegram.send_text(chat_id, f"Published! {result.get('url', '')}")
                clear_draft(sender)
                return {"ok": True}

            if lower in CANCEL_WORDS:
                clear_draft(sender)
                await telegram.send_text(chat_id, "Cancelled. Send photos to start a new product.")
                return {"ok": True}

            # Otherwise treat it as an edit instruction
            images_bytes = [open(p, "rb").read() for p in draft.images]
            try:
                draft.generated = listing.generate_listing(
                    images_bytes, draft.note, edit_instruction=text, previous=draft.generated
                )
            except Exception:
                await telegram.send_text(chat_id, "Both AI providers failed just now — try that change again in a moment.")
                return {"ok": True}
            await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats))
            return {"ok": True}

        # Case 2: this text is the product note -> generate the draft now
        if not draft.images:
            await telegram.send_text(chat_id, "Send at least one product photo first, then your description.")
            return {"ok": True}

        draft.note = text
        images_bytes = [open(p, "rb").read() for p in draft.images]
        try:
            draft.generated = listing.generate_listing(images_bytes, draft.note)
        except Exception:
            await telegram.send_text(chat_id, "Both AI providers failed just now — send your description again in a moment.")
            return {"ok": True}
        draft.status = "awaiting_confirmation"
        await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats))
        return {"ok": True}

    return {"ok": True}


def _format_summary(gen: dict, stl_stats: dict | None = None) -> str:
    if stl_stats and stl_stats.get("weight_g"):
        price_line = f"Price: computed from STL weight (~{stl_stats['weight_g']}g) at publish time"
    else:
        price_line = f"Suggested price: ₹{gen.get('suggested_price')} (Gemini's guess, no STL weight to go on)"
    return (
        f"*{gen.get('name')}*\n"
        f"{gen.get('description')}\n\n"
        f"Category: {gen.get('category')}\n"
        f"{price_line}\n\n"
        f"Reply 'yes' to publish, or describe a change (e.g. 'change price to 899')."
    )
