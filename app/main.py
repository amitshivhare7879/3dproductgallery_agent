import os
import tempfile
from fastapi import FastAPI, Request, Response

from . import telegram, listing, model_utils, pricing, django_client
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
    try:
        return await _handle_message(msg, chat_id)
    except Exception as e:
        # Last-resort safety net: never let an unhandled crash go silent.
        await telegram.send_text(chat_id, f"Something went wrong: {e}")
        return {"ok": True}


async def _handle_message(msg: dict, chat_id: int):
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
            f"Got photo #{len(draft.images)}. Send more, the STL/3MF file, "
            f"or a short description whenever you're ready.",
        )
        return {"ok": True}

    # --- 3D model file received (STL or 3MF, sent as a Document in Telegram) ---
    if "document" in msg and msg["document"]["file_name"].lower().endswith((".stl", ".3mf")):
        filename = msg["document"]["file_name"]
        file_id = msg["document"]["file_id"]
        media_bytes = await telegram.download_file(file_id)
        suffix = ".3mf" if filename.lower().endswith(".3mf") else ".stl"
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(media_bytes)
        draft.stl_path = path
        try:
            draft.stl_stats = model_utils.analyze_model(path, filename)
        except Exception as e:
            await telegram.send_text(chat_id, f"Couldn't read that 3D file: {e}")
            return {"ok": True}
        stats = draft.stl_stats
        w, d, h = stats["dims_cm"]
        await telegram.send_text(
            chat_id,
            f"Got the {suffix.upper().lstrip('.')} file. Dimensions {w} x {d} x {h} cm, "
            f"estimated weight ~{stats['weight_g']}g. Send a short description "
            f"when ready, e.g. 'red dragon miniature, fantasy category'.",
        )
        return {"ok": True}

    # --- Text message ---
    if "text" in msg:
        text = msg["text"].strip()
        lower = text.lower()

        # --- Commands for managing EXISTING products, available any time ---
        if lower == "/list" or lower.startswith("/list "):
            query = text[6:].strip() if len(text) > 5 else ""
            result = await django_client.list_products(query)
            products = result.get("products", [])
            if not products:
                await telegram.send_text(chat_id, "No products found.")
            else:
                lines = [f"#{p['id']} — {p['name']} — ₹{p['price']} ({p['category']})" for p in products]
                await telegram.send_text(chat_id, "\n".join(lines) + "\n\nUse /edit <id> or /delete <id>.")
            return {"ok": True}

        if lower.startswith("/edit "):
            try:
                product_id = int(text.split()[1])
            except (IndexError, ValueError):
                await telegram.send_text(chat_id, "Usage: /edit <product_id> — get the id from /list.")
                return {"ok": True}
            try:
                product = await django_client.get_product(product_id)
            except Exception as e:
                await telegram.send_text(chat_id, f"Couldn't find that product: {e}")
                return {"ok": True}
            clear_draft(sender)
            draft = get_draft(sender)
            draft.status = "editing"
            draft.edit_target_id = product_id
            draft.generated = {
                "name": product["name"],
                "description": product["description"],
                "category": product["category"],
                "suggested_price": product["price"],
            }
            await telegram.send_text(
                chat_id,
                f"Editing #{product_id}: *{product['name']}* — ₹{product['price']} ({product['category']})\n\n"
                f"Tell me what to change (e.g. 'change price to 999', 'update description to ...'), "
                f"send new photos to replace the image, or reply 'cancel'.",
            )
            return {"ok": True}

        if lower.startswith("/delete "):
            try:
                product_id = int(text.split()[1])
            except (IndexError, ValueError):
                await telegram.send_text(chat_id, "Usage: /delete <product_id> — get the id from /list.")
                return {"ok": True}
            clear_draft(sender)
            draft = get_draft(sender)
            draft.status = "confirming_delete"
            draft.edit_target_id = product_id
            await telegram.send_text(chat_id, f"Delete product #{product_id}? Reply 'yes' to confirm, or 'cancel'.")
            return {"ok": True}

        # --- Confirming a delete ---
        if draft.status == "confirming_delete":
            if lower in CONFIRM_WORDS:
                try:
                    result = await django_client.delete_product(draft.edit_target_id)
                except Exception as e:
                    await telegram.send_text(chat_id, f"Delete failed: {e}")
                    clear_draft(sender)
                    return {"ok": True}
                await telegram.send_text(chat_id, f"Deleted #{result['deleted_id']} — {result['deleted_name']}.")
            else:
                await telegram.send_text(chat_id, "Cancelled, nothing deleted.")
            clear_draft(sender)
            return {"ok": True}

        # --- Editing an existing product ---
        if draft.status == "editing":
            if lower in CONFIRM_WORDS:
                try:
                    result = await django_client.edit_product(draft.edit_target_id, draft)
                except Exception as e:
                    await telegram.send_text(
                        chat_id,
                        f"Save failed: {e}\n\nStill here — reply 'yes' again once it's fixed, or 'cancel'.",
                    )
                    return {"ok": True}
                await telegram.send_text(chat_id, f"Saved! {result.get('url', '')}")
                clear_draft(sender)
                return {"ok": True}

            if lower in CANCEL_WORDS:
                clear_draft(sender)
                await telegram.send_text(chat_id, "Cancelled, no changes saved.")
                return {"ok": True}

            try:
                if draft.images:
                    images_bytes = [open(p, "rb").read() for p in draft.images]
                    draft.generated = listing.generate_listing(
                        images_bytes, text, edit_instruction=text, previous=draft.generated,
                        model_stats=draft.stl_stats,
                    )
                else:
                    draft.generated = _apply_text_edit_without_ai(draft.generated, text)
            except Exception as e:
                await telegram.send_text(chat_id, str(e) or "Couldn't process that change — try rephrasing it.")
                return {"ok": True}
            await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats) + "\n\n(Editing existing product — reply 'yes' to save.)")
            return {"ok": True}

        # Case 1: awaiting confirmation on an already-generated draft
        if draft.status == "awaiting_confirmation":
            if lower in CONFIRM_WORDS:
                try:
                    result = await django_client.publish_product(draft)
                except Exception as e:
                    await telegram.send_text(
                        chat_id,
                        f"Publish failed: {e}\n\nThe draft is still here — reply 'yes' again once it's fixed, or 'cancel' to discard.",
                    )
                    return {"ok": True}
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
                    images_bytes, draft.note, edit_instruction=text, previous=draft.generated,
                    model_stats=draft.stl_stats,
                )
            except Exception:
                await telegram.send_text(chat_id, "Both AI providers failed just now — try that change again in a moment.")
                return {"ok": True}
            await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats))
            return {"ok": True}

        # Case 2: this text is the product note -> generate the draft now
        if not draft.images:
            await telegram.send_text(
                chat_id,
                "Send at least one product photo first, then your description.\n\n"
                "(Or use /list, /edit <id>, /delete <id> to manage existing products.)",
            )
            return {"ok": True}

        draft.note = text
        images_bytes = [open(p, "rb").read() for p in draft.images]
        try:
            draft.generated = listing.generate_listing(images_bytes, draft.note, model_stats=draft.stl_stats)
        except Exception:
            await telegram.send_text(chat_id, "Both AI providers failed just now — send your description again in a moment.")
            return {"ok": True}
        draft.status = "awaiting_confirmation"
        await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats))
        return {"ok": True}

    return {"ok": True}


def _apply_text_edit_without_ai(current: dict, instruction: str) -> dict:
    """
    Handles simple edits (price/name/description/category) via keyword
    parsing instead of a full AI call, since Gemini/Groq both need at least
    one image and we don't want to force a re-upload just to tweak a price.
    """
    updated = dict(current or {})
    lower = instruction.lower()

    import re
    price_match = re.search(r"price\s*(?:to|=|:)?\s*₹?\s*(\d+(?:\.\d+)?)", lower)
    if price_match:
        updated["suggested_price"] = float(price_match.group(1))
        return updated

    for field, keyword in [("name", "name"), ("description", "description"), ("category", "category")]:
        if keyword in lower:
            # take everything after "to"/":" as the new value
            parts = re.split(r"\bto\b|:", instruction, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                updated[field] = parts[1].strip()
                return updated

    raise ValueError(
        "Couldn't understand that edit. Try: 'change price to 999', "
        "'update name to X', 'update description to X', or send a new photo."
    )


def _format_summary(gen: dict, stl_stats: dict | None = None) -> str:
    price = pricing.compute_price(stl_stats, gen.get("suggested_price"))

    if stl_stats and stl_stats.get("weight_g"):
        w, d, h = stl_stats["dims_cm"]
        dims_line = f"Dimensions: {w} x {d} x {h} cm | Weight: ~{stl_stats['weight_g']}g\n"
        price_line = f"Price: ₹{price} (computed from print weight)"
    else:
        dims_line = ""
        price_line = f"Suggested price: ₹{price} (AI's guess, no 3D file to go on)"

    return (
        f"*{gen.get('name')}*\n"
        f"{gen.get('description')}\n\n"
        f"{dims_line}"
        f"Category: {gen.get('category')}\n"
        f"{price_line}\n\n"
        f"Reply 'yes' to publish, or describe a change (e.g. 'change price to 899')."
    )
