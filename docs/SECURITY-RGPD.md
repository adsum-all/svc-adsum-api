# Security and RGPD posture, ADSUM API

Status of the security controls, the SMS OTP evaluation and the e-mail domain
verification. Kept in the code repository so it stays in step with the API.

## OWASP controls in place

| Control | Implementation |
| --- | --- |
| Password storage | Argon2 (`app/security.py`), never reversible |
| Session tokens | JWT, RS256 by default, `iss`/`aud` claims, short lifetime |
| Brute-force protection | Sliding-window rate limiting on `login`, `premiere-connexion`, `request-otp`, `reset-password` (`app/ratelimit.py`, table `auth_attempt`) |
| Security headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, HSTS, `Permissions-Policy`, COOP (`app/middleware.py`) |
| Access control | Per-role checks (`require_roles`) plus `WHERE` scoping; the app never trusts the client for authorization |
| Transport | HTTPS only (Vercel), HSTS one year |
| Account enumeration | `request-otp` always returns ok; login errors are generic |
| Audit trail | Every mutation writes to the `audit` table (`app/audit.py`) |
| Input validation | Pydantic models on every endpoint; server-side validation only |
| Two-factor | E-mail OTP, stateless HMAC codes with a 10-minute window |

## RGPD rights

| Right | Endpoint / operation |
| --- | --- |
| Access and portability | `GET /api/v1/membres/me/export` returns the member's full data set (profile, documents, attendance, requests, notifications, connections) as JSON |
| Erasure (member request) | `POST /api/v1/membres/me/suppression` files a tracked request for the administration |
| Erasure (execution) | `DELETE /api/v1/admin/membres/{id}` purges child records and both storage buckets (`app/gestion.py`) |
| Retention | `parametre` keys `retention_presence_jours`, `retention_audit_jours`, `inactivite_anonymisation_jours` |
| Notification consent | `preference_notification` lets the member opt out per channel |

Personal data is stored in the EU (Supabase, Paris, eu-west-3). Secrets never
reach the client; storage buckets are private with signed URLs.

## SMS OTP evaluation

Requirement: a free service for SMS one-time codes.

- **Twilio**: the trial credit is one-off and messages carry a mandatory trial
  prefix; recipients must be individually verified. Not viable for real members,
  and not a durable free tier.
- **Vonage**: a small starter credit, then paid per message. No lasting free tier.
- **Free/African-market gateways**: no provider offers a reliable free SMS tier
  for Cote d'Ivoire numbers without KYC and per-message billing.

Conclusion: there is **no genuinely free SMS OTP** for this audience. The system
therefore uses **e-mail OTP**, which is free, already integrated (Brevo/Resend)
and equally strong for the double-validation flows. The code path is provider
agnostic: adding an SMS channel later means implementing one `send_code`
transport in `app/email_gateway.py` and a member phone-verification step; no
schema change is required.

## E-mail domain verification

- Transactional e-mail goes through **Brevo** (default) with **Resend** as a
  fallback, configured by `ADSUM_EMAIL_PROVIDER` and the secrets in
  `.secret/email-providers-secret.json`.
- **Pending**: full domain authentication (SPF, DKIM, DMARC) for the
  organisation's sending domain. Until the domain's DNS is delegated and those
  records are published with the provider, delivery to every member is limited
  to provider-verified senders; verified test addresses receive mail normally.
- Action to close it: publish the SPF/DKIM/DMARC records the provider generates
  for the chosen sending domain, then flip `email_from` to that domain.
