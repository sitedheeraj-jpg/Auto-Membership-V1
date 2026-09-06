# Auto Membership Telegram Bot

This is a clean replacement for the uploaded source. It uses:

- Python 3.12
- `python-telegram-bot` 21.10 async API
- MongoDB through Motor
- Dynamic UPI QR codes with the VC Payment API
- Admin panel for channels, descriptions, plans, prices, durations, and support contact
- Single-use, time-limited Telegram invite links
- Join detection, invite revocation, expiry reminders, and automatic removal after expiry

## Railway deployment

1. Create a Railway service from this folder or from the included `Dockerfile`.
2. Add the variables in `.env.example` under Railway **Variables**.
3. Set `BOT_TOKEN`, `OWNER_ID`, and `DATABE_URL` at minimum. `ADMIN_IDS` can contain comma-separated additional Telegram user IDs.
4. Add the bot as an administrator in every sales channel. It needs permission to:
   - invite users through invite links;
   - manage invite links;
   - restrict/ban members;
   - receive `chat_member` updates.
5. Start the service. Send `/start` to the bot as the owner, open **Admin panel**, add a channel by numeric ID, then add its plans.

`DATABE_URL` is intentionally supported because it is the variable name from the request. `DATABASE_URL` is also accepted.

## Important payment/API note

The bot sends `api_key`, `order_id`, and `amount` as query parameters to the configured payment endpoint. It accepts common success/status/transaction field names and validates order ID, amount tolerance, and transaction replay.

The supplied configuration defaults to a 15-minute payment window. Set `PAYMENT_MAX_MINUTES=20` in Railway if you want a 20-minute window.

## Telegram button colors

Telegram's inline-button background colors are controlled by the Telegram client/theme rather than by bot code. The UI uses red/green/blue emoji prefixes so the action colors remain visible consistently across clients.

## Security

Do not commit a real MongoDB URI or payment credential. The credentials included in the original request are now exposed in chat history; rotate the MongoDB password and any provider key before production, then place the replacement values only in Railway Variables.