# Telegram Product-Add Bot — 3D Product Gallery

Send photos + an STL + a one-line description to your Telegram bot, get back
an auto-drafted product listing, reply "yes" to publish it to
3dproductgallery.in. Everything below runs on free tiers.

## How a product gets added (once live)

1. Send 1+ product photos to the bot
2. Send the `.stl` or `.3mf` file
3. Send a short text note, e.g. "red dragon miniature, fantasy category"
4. Bot replies with a generated name/description (with real dimensions
   baked in)/category/price
5. Reply `yes` to publish, or type a change (e.g. "make it cheaper") to edit,
   or `cancel` to discard

## Managing existing products

- `/list` — shows your last 20 products with id, name, price, category.
  `/list dragon` searches by name.
- `/edit <id>` — pulls up an existing product. Tell it what to change
  ("change price to 999", "update description to ...") or send a new photo
  to replace the image, then reply `yes` to save.
- `/delete <id>` — asks for confirmation, then deletes the product.

## 1. Create the bot (free, ~1 minute)

1. In Telegram, message **@BotFather**.
2. Send `/newbot`, give it a name, then a username ending in `bot`.
3. Copy the token it gives you into `.env` as `TELEGRAM_BOT_TOKEN`.

## 2. Gemini API key (free tier, primary) + Groq key (free tier, fallback)

Get a Gemini key at https://aistudio.google.com/apikey — the free tier's
daily request quota comfortably covers adding a handful of products a day.

Get a free Groq key at https://console.groq.com/keys as a backup — if
Gemini errors or rate-limits, the bot automatically retries the same request
through Groq's vision model instead of failing outright. Groq's vision
model only accepts one image per request (vs. Gemini's several), so on
fallback only your first photo is used — fine for a draft you'll review
anyway.

## 3. Deploy the FastAPI service (free — Render)

1. Push this folder to a GitHub repo.
2. On https://render.com, New → Web Service → connect the repo.
3. Runtime: Python 3. Build command: `pip install -r requirements.txt`.
   Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Choose the **Free** instance type.
5. Add all variables from `.env.example` as environment variables in Render's
   dashboard (real values, not the placeholders). Leave `ALLOWED_SENDERS`
   blank for now.
6. Deploy. Note the public URL Render gives you, e.g.
   `https://your-service.onrender.com`.

Free tier note: the service sleeps after inactivity and takes a few seconds
to wake on the next message — a non-issue for occasional product adds.

## 4. Register the webhook with Telegram

Run this once from your own machine (replace the placeholders), or use the
`set_webhook` helper in `app/telegram.py`:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-service.onrender.com/webhook", "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"}'
```

Use the same values you set in Render's environment variables.

## 5. Find your Telegram user ID and lock the bot down

1. Message your bot anything, e.g. "hi".
2. Since `ALLOWED_SENDERS` is still blank, it'll reply with your numeric
   Telegram user ID.
3. Copy that ID into `ALLOWED_SENDERS` in Render's environment variables and
   redeploy. Now only you can use the bot.

## 6. Add the Django endpoint

1. Copy `django_publish_endpoint.py`'s view into your products app, adjusting
   field names to match your actual `Product` model.
2. Wire the URL: `path("api/products/create", publish_product_api)`.
3. Set `DJANGO_PUBLISH_SECRET` in your Vercel environment variables — must
   match the value in the bot's `.env`.

## Local testing without deploying

Since Telegram requires a public HTTPS webhook, use `ngrok` (free) during
local dev: run `uvicorn app.main:app --reload`, then `ngrok http 8000`, and
point `setWebhook` at the ngrok HTTPS URL while testing.

## What's already matched to your real schema

`django_publish_endpoint.py` and `django_client.py` are written against your
actual `Product` / `ProductImage` models (`name`, `description`, `price`,
`category` restricted to your real dropdown choices, `image` +
`ProductImage` gallery). Price is computed from STL weight × 
`PRICE_PER_GRAM_INR` when an STL is sent, or falls back to Gemini's guess
otherwise — always editable via chat before you confirm.

## Still worth double-checking before going live

- The product detail URL returned after publishing (`/product/<slug>/`) is a
  guess — check your `urls.py` and fix the pattern in
  `django_publish_endpoint.py` if it's different.
- `PRICE_PER_GRAM_INR` (default 8) is a placeholder — set it to your real
  material + printing cost per gram.
- Image upload here posts directly as `multipart/form-data` files, matching
  your `ImageField`/`ImageField` setup — if your R2/`django-storages` config
  needs anything extra (e.g. content-type headers), test with one real
  product first before relying on it.
