import gc
import logging
import os
import re
import tempfile

from fastapi import FastAPI, Request, Response

from . import telegram, listing, model_utils, pricing, django_client
from .state import get_draft, clear_draft, persist, get_last_update_id, set_last_update_id

log = logging.getLogger(__name__)
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

TELEGRAM_MAX_BYTES = 20 * 1024 * 1024  # hard limit on Telegram's Bot API

HELP_TEXT = (
    "*Add a new product:*\n"
    "Send photo(s), optionally an .stl/.3mf file and a video, then a short "
    "description -- or just type /generate to have the AI write one from "
    "the photos alone. Reply 'yes' to publish.\n\n"
    "*Manage existing products:*\n"
    "/list [search] — see products with their IDs\n"
    "/edit <id> — change an existing product\n"
    "/delete <id> — remove a product\n\n"
    "*If something breaks:*\n"
    "/reset — clears whatever's in progress and starts clean"
)


@app.post("/webhook")
async def receive_update(request: Request):
    if WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != WEBHOOK_SECRET:
            return Response(status_code=403)

    chat_id = None
    try:
        body = await request.json()

        # Deduplicate BEFORE any processing. Telegram redelivers the same
        # update_id if it doesn't get a fast 200 back (network blips, slow
        # AI calls, a host restart -- anything). Marking it seen up front,
        # not after successful processing, means even a crash mid-request
        # can't turn into a reprocessing loop.
        update_id = body.get("update_id")
        if update_id is not None:
            if update_id <= get_last_update_id():
                return {"ok": True}  # already handled (or in progress) -- skip silently
            set_last_update_id(update_id)

        msg = body.get("message")
        if not msg:
            return {"ok": True}  # edited messages, other update types -> ignore
        chat_id = msg["chat"]["id"]
        result = await _handle_message(msg, chat_id)
        persist()
        return result
    except Exception as e:
        # Last-resort safety net: NOTHING should ever go unanswered or leave
        # the bot stuck. Log it, tell the user, always return 200 to Telegram
        # (so it doesn't endlessly retry the same failing update).
        log.exception("Unhandled error in webhook")
        if chat_id is not None:
            try:
                await telegram.send_text(
                    chat_id,
                    f"Something went wrong: {e}\n\nSend /reset if the bot seems stuck.",
                )
            except Exception:
                log.exception("Even the error message failed to send")
        persist()
        return {"ok": True}
    finally:
        # Photo/video/3D-file handling all build byte buffers in memory for
        # this request -- force them released right away instead of waiting
        # for Python's next GC cycle, since the free-tier host is memory-
        # capped and the next request could arrive immediately.
        gc.collect()


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

    # --- /reset works ALWAYS, regardless of what state the draft is in ---
    if "text" in msg and msg["text"].strip().lower() == "/reset":
        clear_draft(sender)
        await telegram.send_text(chat_id, "Reset. Send photos to start a new product, or /list to manage existing ones.")
        return {"ok": True}

    if "text" in msg and msg["text"].strip().lower() in ("/start", "/help"):
        await telegram.send_text(chat_id, HELP_TEXT)
        return {"ok": True}

    if "text" in msg and msg["text"].strip().lower() == "/generate":
        if not draft.images:
            await telegram.send_text(chat_id, "Send at least one product photo first, then /generate.")
            return {"ok": True}
        try:
            images_bytes = _read_files(draft.images)
            draft.generated = listing.generate_listing(images_bytes, "", model_stats=draft.stl_stats)
        except Exception:
            await telegram.send_text(chat_id, "Both AI providers failed just now — try /generate again in a moment.")
            return {"ok": True}
        draft.status = "awaiting_confirmation"
        await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats, draft.video_path, draft.manual_price))
        return {"ok": True}

    # --- Photo received: stash it and keep collecting ---
    if "photo" in msg:
        file_id = msg["photo"][-1]["file_id"]  # last = largest size
        try:
            media_bytes = await telegram.download_file(file_id)
        except Exception as e:
            await telegram.send_text(chat_id, f"Couldn't download that photo: {e}")
            return {"ok": True}
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(media_bytes)
        draft.images.append(path)
        await telegram.send_text(
            chat_id,
            f"Got photo #{len(draft.images)}. Send more, the STL/3MF file, "
            f"a product video, a short description, or /generate to have "
            f"the AI write one from the photos alone.",
        )
        return {"ok": True}

    # --- 3D model file received (STL or 3MF, sent as a Document) ---
    if "document" in msg and msg["document"]["file_name"].lower().endswith((".stl", ".3mf")):
        filename = msg["document"]["file_name"]
        file_id = msg["document"]["file_id"]
        file_size = msg["document"].get("file_size", 0)

        size_error = _check_telegram_size(file_size)
        if size_error:
            await telegram.send_text(chat_id, size_error)
            return {"ok": True}

        try:
            media_bytes = await telegram.download_file(file_id)
        except Exception as e:
            await telegram.send_text(chat_id, f"Couldn't download that file from Telegram: {e}")
            return {"ok": True}

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

    # --- Product video received (as a native video message OR a video file) ---
    is_video_doc = "document" in msg and (
        msg["document"].get("mime_type", "").startswith("video/")
        or msg["document"]["file_name"].lower().endswith((".mp4", ".mov", ".webm", ".avi"))
    )
    if "video" in msg or is_video_doc:
        if "video" in msg:
            file_id = msg["video"]["file_id"]
            file_size = msg["video"].get("file_size", 0)
        else:
            file_id = msg["document"]["file_id"]
            file_size = msg["document"].get("file_size", 0)

        size_error = _check_telegram_size(file_size)
        if size_error:
            await telegram.send_text(chat_id, size_error)
            return {"ok": True}

        try:
            media_bytes = await telegram.download_file(file_id)
        except Exception as e:
            await telegram.send_text(chat_id, f"Couldn't download that video: {e}")
            return {"ok": True}

        fd, path = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(fd, "wb") as f:
            f.write(media_bytes)
        draft.video_path = path
        await telegram.send_text(
            chat_id,
            "Got the product video. Send photos/STL/description whenever you're ready.",
        )
        return {"ok": True}

    # --- Text message ---
    if "text" in msg:
        text = msg["text"].strip()
        lower = text.lower()

        # --- Commands for managing EXISTING products, available any time ---
        if lower == "/list" or lower.startswith("/list "):
            query = text[6:].strip() if len(text) > 5 else ""
            try:
                result = await django_client.list_products(query)
            except Exception as e:
                await telegram.send_text(chat_id, f"Couldn't fetch product list: {e}")
                return {"ok": True}
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
                f"send new photos/video to replace them, or reply 'cancel'.",
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

            # Pure price change -- handle directly, instant and exact,
            # never left to the AI or overridden by weight-based pricing.
            price_override = _extract_price_override(text)
            if price_override is not None and lower.strip().startswith(("change price", "price", "update price", "set price")):
                draft.manual_price = price_override
                await telegram.send_text(
                    chat_id,
                    _format_summary(draft.generated, draft.stl_stats, draft.video_path, draft.manual_price)
                    + "\n\n(Editing existing product — reply 'yes' to save.)",
                )
                return {"ok": True}

            try:
                images_bytes = _read_files(draft.images) if draft.images else []
                draft.generated = listing.generate_listing(
                    images_bytes, text, edit_instruction=text, previous=draft.generated,
                    model_stats=draft.stl_stats,
                )
            except Exception as ai_error:
                # Both AI providers genuinely failed (e.g. rate limited) --
                # fall back to simple keyword parsing rather than leaving
                # the user stuck, but only for the few patterns it can
                # actually handle.
                try:
                    draft.generated = _apply_text_edit_without_ai(draft.generated, text)
                except Exception:
                    await telegram.send_text(
                        chat_id,
                        f"AI is unavailable right now ({ai_error}). Try again in a moment, "
                        f"or a simple edit like 'change price to 999'.",
                    )
                    return {"ok": True}
            await telegram.send_text(
                chat_id,
                _format_summary(draft.generated, draft.stl_stats, draft.video_path, draft.manual_price)
                + "\n\n(Editing existing product — reply 'yes' to save.)",
            )
            return {"ok": True}

        # --- Awaiting confirmation on an already-generated new-product draft ---
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

            # Pure price change -- handle directly, instant and exact,
            # never left to the AI or overridden by weight-based pricing.
            price_override = _extract_price_override(text)
            if price_override is not None and lower.strip().startswith(("change price", "price", "update price", "set price")):
                draft.manual_price = price_override
                await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats, draft.video_path, draft.manual_price))
                return {"ok": True}

            # Otherwise treat it as an edit instruction
            images_bytes = _read_files(draft.images)
            try:
                draft.generated = listing.generate_listing(
                    images_bytes, draft.note, edit_instruction=text, previous=draft.generated,
                    model_stats=draft.stl_stats,
                )
            except Exception:
                await telegram.send_text(chat_id, "Both AI providers failed just now — try that change again in a moment.")
                return {"ok": True}
            await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats, draft.video_path, draft.manual_price))
            return {"ok": True}

        # --- This text is the product note -> generate the draft now ---
        if not draft.images:
            await telegram.send_text(
                chat_id,
                "Send at least one product photo first, then your description.\n\n"
                "(Or /list, /edit <id>, /delete <id> to manage existing products, /help for everything.)",
            )
            return {"ok": True}

        draft.note = text
        images_bytes = _read_files(draft.images)
        try:
            draft.generated = listing.generate_listing(images_bytes, draft.note, model_stats=draft.stl_stats)
        except Exception:
            await telegram.send_text(chat_id, "Both AI providers failed just now — send your description again in a moment.")
            return {"ok": True}
        draft.status = "awaiting_confirmation"
        await telegram.send_text(chat_id, _format_summary(draft.generated, draft.stl_stats, draft.video_path, draft.manual_price))
        return {"ok": True}

    return {"ok": True}


def _check_telegram_size(file_size: int) -> str | None:
    """Returns an error message if the file exceeds Telegram's bot download
    limit, else None. Checked upfront using Telegram's own reported size,
    to avoid a confusing 400 error from actually attempting the download."""
    if file_size and file_size > TELEGRAM_MAX_BYTES:
        mb = file_size / (1024 * 1024)
        return (
            f"That file is {mb:.1f}MB — Telegram's bot API only allows downloading "
            f"files up to 20MB. Try re-exporting with a lower mesh resolution/"
            f"compression, or split it, and send that instead."
        )
    return None


def _read_files(paths: list[str]) -> list[bytes]:
    data = []
    for p in paths:
        with open(p, "rb") as f:
            data.append(f.read())
    return data


def _extract_price_override(instruction: str) -> float | None:
    """
    Detects an explicit price change like 'change price to 149' and returns
    the number, or None if the instruction isn't purely a price change.
    Checked BEFORE calling the AI so a price request is always exact and
    instant, never subject to the AI reinterpreting it -- and critically,
    never silently overridden by the weight-based price calculation.
    """
    match = re.search(r"price\s*(?:to|=|:)?\s*₹?\s*(\d+(?:\.\d+)?)", instruction.lower())
    return float(match.group(1)) if match else None


def _apply_text_edit_without_ai(current: dict, instruction: str) -> dict:
    """
    Handles simple edits (price/name/description/category) via keyword
    parsing instead of a full AI call, since Gemini/Groq both need at least
    one image and we don't want to force a re-upload just to tweak a price.
    """
    updated = dict(current or {})
    lower = instruction.lower()

    price_match = re.search(r"price\s*(?:to|=|:)?\s*₹?\s*(\d+(?:\.\d+)?)", lower)
    if price_match:
        updated["suggested_price"] = float(price_match.group(1))
        return updated

    for field_name, keyword in [("name", "name"), ("description", "description"), ("category", "category")]:
        if keyword in lower:
            parts = re.split(r"\bto\b|:", instruction, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                updated[field_name] = parts[1].strip()
                return updated

    raise ValueError(
        "Couldn't understand that edit. Try: 'change price to 999', "
        "'update name to X', 'update description to X', or send a new photo."
    )


def _format_summary(gen: dict, stl_stats: dict | None = None, video_path: str | None = None,
                     manual_price: float | None = None) -> str:
    price = pricing.compute_price(stl_stats, gen.get("suggested_price"), manual_price)

    if manual_price is not None:
        w_d_h = stl_stats["dims_cm"] if stl_stats and stl_stats.get("weight_g") else None
        dims_line = f"Dimensions: {w_d_h[0]} x {w_d_h[1]} x {w_d_h[2]} cm\n" if w_d_h else ""
        price_line = f"Price: ₹{price} (set manually)"
    elif stl_stats and stl_stats.get("weight_g"):
        w, d, h = stl_stats["dims_cm"]
        dims_line = f"Dimensions: {w} x {d} x {h} cm | Weight: ~{stl_stats['weight_g']}g\n"
        price_line = f"Price: ₹{price} (computed from print weight)"
    else:
        dims_line = ""
        price_line = f"Suggested price: ₹{price} (AI's guess, no 3D file to go on)"

    video_line = "Video: attached\n" if video_path else ""

    return (
        f"*{gen.get('name')}*\n"
        f"{gen.get('description')}\n\n"
        f"{dims_line}"
        f"{video_line}"
        f"Category: {gen.get('category')}\n"
        f"{price_line}\n\n"
        f"Reply 'yes' to publish, or describe a change (e.g. 'change price to 899')."
    )
