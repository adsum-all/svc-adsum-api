# Notification channels and birthday module

How ADSUM delivers notifications, which channels are free, and what the owner
must provide to activate each one.

## Channels

| Channel | Cost | Status without extra setup | Env vars to activate |
| --- | --- | --- | --- |
| In-app | free | Always on | none |
| E-mail | free tier | On (Brevo/Resend already configured) | existing `ADSUM_EMAIL_*` |
| **Telegram** | **free** | Off until a bot token is set | `ADSUM_TELEGRAM_BOT_TOKEN`, `ADSUM_TELEGRAM_BOT_USERNAME` |
| WhatsApp | paid per message | Off until credentials + approved template | `ADSUM_WHATSAPP_TOKEN`, `ADSUM_WHATSAPP_PHONE_NUMBER_ID`, `ADSUM_WHATSAPP_TEMPLATE_ANNIVERSAIRE`, `ADSUM_WHATSAPP_TEMPLATE_LANG` |
| SMS | depends on provider | Placeholder (never attempted) | `ADSUM_SMS_PROVIDER` + provider keys |

A message is delivered over every channel the member has opted into (see the
member Settings) and that is configured. In-app is always recorded. The code is
provider-agnostic (`app/channels.py`); adding a channel is one `send_*` function.

### Telegram (recommended, free)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username
   ending in `bot`. Copy the token (`123456789:AA...`).
2. Set the Vercel env vars `ADSUM_TELEGRAM_BOT_TOKEN` (the token) and
   `ADSUM_TELEGRAM_BOT_USERNAME` (the username without `@`).
3. Members link their account from the app (Settings, Canaux de réception,
   "Lier"): the app opens `https://t.me/<bot>?start=<token>`, the member presses
   Start, then taps "vérifier la liaison". The bot's `getUpdates` is read once to
   capture the member's `chat_id`; from then on messages are sent to that id.
   Telegram never exposes phone numbers and a bot cannot message a user who has
   not pressed Start first.

### WhatsApp (paid, per message)

WhatsApp Cloud API bills per message for business-initiated sends (birthday,
OTP) since 2025, and requires a verified Meta Business, a WhatsApp Business
Account, a phone number id, a permanent System User token and an **approved**
template. Once those env vars are set, the WhatsApp channel activates
automatically. Members register their number in Settings (opt-in required by
Meta's terms).

## Daily birthday job

- Endpoint: `GET /api/v1/cron/anniversaires`, scheduled by `vercel.json`
  (`0 6 * * *`, 06:00 UTC daily). Vercel injects `Authorization: Bearer
  $CRON_SECRET` when `CRON_SECRET` is set on the project; the endpoint rejects
  calls without it.
- It finds members whose birth day and month are today, renders the
  admin-editable message (`modele_message` key `anniversaire`, `{prenom}`
  placeholder), and delivers it over every opted-in channel. Each member is
  wished once per year (`notification_anniversaire` unique on member+year).
- The administration edits the message and image, and can run it on demand, from
  the back-office ("Souhaits d'anniversaire"). A default festive message is
  seeded.

## SMS OTP note

There is no genuinely free SMS tier for the audience (see `SECURITY-RGPD.md`).
Telegram is the free real-time channel; OTP over Telegram/WhatsApp can be added
on the same abstraction once a member has linked that channel.
