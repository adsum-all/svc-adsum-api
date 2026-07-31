"""Professional, design-system HTML e-mails, in the organisation's own identity.

Email clients are limited, so everything is inline-styled, table-based and uses
web-safe fallbacks. One builder renders both a code e-mail and a notification.

The name, the city, the signature and the palette are read from the organisation's
settings rather than written here. They used to be literals, which was true enough
while one organisation used the platform and stops being true the moment a second
one does: a parish would send its members mail headed ADSUM, signed Sacerdoce
Royal, and footed with an address in Abidjan. The defaults reproduce exactly what
was here, so nothing changes for the organisation running today.
"""
# ruff: noqa: E501 - HTML e-mail templates have unavoidably long inline-styled lines.
from __future__ import annotations

import html as _html

from .marque import marque

BG = "#eef1f6"
INK = "#16181d"
MUT = "#5b6170"
LINE = "#e3e5ea"
FONT_UI = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
FONT_DISPLAY = "'Space Grotesk', 'IBM Plex Sans', Helvetica, Arial, sans-serif"
FONT_MONO = "'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace"


def _shell(inner: str, preheader: str) -> str:
    m = marque()
    # The palette travels with the identity: a message arriving in somebody else's
    # colours does not look like a message from their organisation.
    ACC, ACC2 = m.couleur, m.couleur_sombre
    return f"""\
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<style>@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600&display=swap');</style>
</head>
<body style="margin:0;padding:0;background:{BG};">
<span style="display:none;max-height:0;overflow:hidden;opacity:0;color:{BG};">{preheader}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:32px 16px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border:1px solid {LINE};border-radius:18px;overflow:hidden;">
<tr><td style="background:linear-gradient(135deg,{ACC},{ACC2});padding:22px 28px;">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="width:40px;height:40px;background:rgba(255,255,255,.16);border-radius:10px;text-align:center;vertical-align:middle;font-family:{FONT_DISPLAY};font-weight:700;font-size:22px;color:#fff;">{_html.escape(m.initiale)}</td>
<td style="padding-left:12px;font-family:{FONT_DISPLAY};font-weight:700;font-size:18px;letter-spacing:2px;color:#ffffff;">{_html.escape(m.marque)}<span style="display:block;font-family:{FONT_MONO};font-weight:500;font-size:10px;letter-spacing:1.5px;color:#c7d4f2;">{_html.escape(m.nom.upper())}</span></td>
</tr></table>
</td></tr>
{inner}
<tr><td style="padding:18px 28px 26px;border-top:1px solid {LINE};">
<p style="margin:0;font-family:{FONT_UI};font-size:11px;line-height:1.6;color:#9498a1;">Vous recevez cet e-mail dans le cadre de {_html.escape(m.mention_compte())}. Si vous n'êtes pas à l'origine de cette demande, ignorez ce message. Ne partagez jamais vos codes.</p>
</td></tr>
</table>
<p style="margin:14px 0 0;font-family:{FONT_MONO};font-size:10px;color:#9498a1;">{_html.escape(m.pied())}</p>
</td></tr></table>
</body></html>"""


def render_code_email(code: str, intro: str, validity: str = "quelques minutes") -> str:
    boxes = "".join(
        f'<td style="padding:0 4px;"><div style="width:40px;height:52px;line-height:52px;text-align:center;'
        f'background:#f4f6fb;border:1px solid {LINE};border-radius:10px;font-family:{FONT_MONO};font-weight:600;'
        f'font-size:24px;color:{INK};">{ch}</div></td>'
        for ch in code
    )
    inner = f"""\
<tr><td style="padding:30px 28px 6px;">
<h1 style="margin:0 0 8px;font-family:{FONT_DISPLAY};font-weight:700;font-size:21px;color:{INK};">Votre code de vérification</h1>
<p style="margin:0 0 22px;font-family:{FONT_UI};font-size:14px;line-height:1.6;color:{MUT};">{intro}</p>
<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 18px;"><tr>{boxes}</tr></table>
<p style="margin:0;text-align:center;font-family:{FONT_UI};font-size:12.5px;color:{MUT};">Ce code est valable {validity}.</p>
</td></tr>"""
    return _shell(inner, f"Votre code {marque().marque} : {code}")


def render_temp_password_email(
    temp_password: str,
    validity: str = "72 heures",
    app_url: str | None = None,
) -> str:
    # The button opens the member's own portal. Fixed to one deployment, every
    # new member of every other organisation received a link to a portal where
    # their account does not exist.
    if app_url is None:
        from .information_diffusion import portail_url

        app_url = portail_url()
    ACC = marque().couleur
    inner = f"""\
<tr><td style="padding:30px 28px 6px;">
<h1 style="margin:0 0 8px;font-family:{FONT_DISPLAY};font-weight:700;font-size:21px;color:{INK};">Bienvenue dans {_html.escape(marque().marque)}</h1>
<p style="margin:0 0 20px;font-family:{FONT_UI};font-size:14px;line-height:1.6;color:{MUT};">Votre compte membre a été créé par l'administration. Voici votre mot de passe temporaire pour vous connecter et finaliser votre inscription.</p>
<div style="margin:0 0 16px;padding:16px 18px;background:#f4f6fb;border:1px solid {LINE};border-radius:12px;text-align:center;">
<div style="font-family:{FONT_UI};font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{MUT};margin-bottom:6px;">Mot de passe temporaire</div>
<div style="font-family:{FONT_MONO};font-weight:600;font-size:24px;letter-spacing:3px;color:{ACC};">{temp_password}</div>
</div>
<p style="margin:0 0 18px;font-family:{FONT_UI};font-size:12.5px;line-height:1.6;color:{MUT};">Il est valable <b style="color:{INK};">{validity}</b>. À la première connexion, vous devrez le remplacer par votre propre mot de passe (avec un code de validation envoyé par e-mail). Passé ce délai, contactez l'administration pour en obtenir un nouveau.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 4px;"><tr>
<td style="background:{ACC};border-radius:11px;"><a href="{app_url}" style="display:inline-block;padding:12px 22px;font-family:{FONT_UI};font-weight:600;font-size:14px;color:#ffffff;text-decoration:none;">Accéder à mon espace</a></td></tr></table>
</td></tr>"""
    return _shell(inner, f"Votre mot de passe temporaire {marque().marque}")


def render_anniversaire_email(titre: str, corps: str, image_url: str | None = None, site: str | None = None, signature: str | None = None) -> str:
    ACC = marque().couleur
    """A festive, sober birthday message on the design system. The sign-off uses the
    admin-configured signature (no hard-coded organisation name)."""
    titre = _html.escape(titre)
    corps = _html.escape(corps).replace("\n", "<br>")
    sig = _html.escape(signature) if signature else ""
    image = (
        f'<tr><td style="padding:0 28px 4px;"><img src="{_html.escape(image_url, quote=True)}" alt="" '
        f'style="width:100%;max-height:200px;object-fit:cover;border-radius:14px;display:block;"></td></tr>'
        if image_url
        else ""
    )
    inner = f"""\
<tr><td align="center" style="padding:34px 28px 6px;">
<div style="height:4px;width:52px;margin:0 auto 14px;border-radius:2px;background:{ACC};"></div>
<h1 style="margin:0 0 10px;font-family:{FONT_DISPLAY};font-weight:700;font-size:23px;color:{INK};">{titre}</h1>
</td></tr>
{image}
<tr><td style="padding:8px 28px 10px;">
<p style="margin:0;font-family:{FONT_UI};font-size:14.5px;line-height:1.7;color:{MUT};text-align:center;">{corps}</p>
{f'<p style="margin:18px 0 0;text-align:center;font-family:{FONT_DISPLAY};font-weight:600;font-size:15px;color:{ACC};">{sig}</p>' if sig else ""}
{f'<p style="margin:4px 0 0;text-align:center;font-family:{FONT_MONO};font-size:11px;color:#9498a1;">{site}</p>' if site else ""}
</td></tr>"""
    return _shell(inner, titre)


def _signature_block(signature: str | None, site: str | None) -> str:
    ACC = marque().couleur
    if not signature and not site:
        return ""
    sig = f'<p style="margin:16px 0 0;font-family:{FONT_DISPLAY};font-weight:600;font-size:14px;color:{ACC};">{signature}</p>' if signature else ""
    lien = ""
    if site:
        href = site if site.startswith("http") else f"https://{site}"
        lien = f'<p style="margin:4px 0 0;font-family:{FONT_MONO};font-size:11px;color:#9498a1;"><a href="{href}" style="color:#9498a1;text-decoration:none;">{site}</a></p>'
    return sig + lien


def render_notification_email(
    titre: str,
    corps: str,
    cta_label: str | None = None,
    cta_url: str | None = None,
    signature: str | None = None,
    site: str | None = None,
) -> str:
    ACC = marque().couleur
    # Escape every dynamic value before it enters the HTML: titles, bodies and CTA
    # labels can carry member/event text, so raw interpolation would allow HTML
    # injection in e-mail clients. Body newlines are turned into line breaks after
    # escaping, so formatting is kept without opening an injection.
    titre_s = _html.escape(titre)
    corps_s = _html.escape(corps).replace("\n", "<br>")
    cta = ""
    if cta_label and cta_url:
        cta = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0 4px;"><tr>'
            f'<td style="background:{ACC};border-radius:11px;"><a href="{_html.escape(cta_url, quote=True)}" '
            f'style="display:inline-block;padding:12px 22px;font-family:{FONT_UI};font-weight:600;font-size:14px;'
            f'color:#ffffff;text-decoration:none;">{_html.escape(cta_label)}</a></td></tr></table>'
        )
    inner = f"""\
<tr><td style="padding:30px 28px 8px;">
<h1 style="margin:0 0 10px;font-family:{FONT_DISPLAY};font-weight:700;font-size:20px;color:{INK};">{titre_s}</h1>
<p style="margin:0;font-family:{FONT_UI};font-size:14px;line-height:1.65;color:{MUT};">{corps_s}</p>
{cta}
{_signature_block(signature, site)}
</td></tr>"""
    return _shell(inner, titre_s)
