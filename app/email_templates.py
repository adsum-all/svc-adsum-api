"""Professional, design-system HTML e-mails for ADSUM.

Email clients are limited, so everything is inline-styled, table-based and uses
web-safe fallbacks while honouring the ADSUM palette (indigo #2a4fad, navy) and
display/mono type. One builder renders both a code e-mail and a notification.
"""
# ruff: noqa: E501 - HTML e-mail templates have unavoidably long inline-styled lines.
from __future__ import annotations

BG = "#eef1f6"
INK = "#16181d"
MUT = "#5b6170"
ACC = "#2a4fad"
ACC2 = "#1d3470"
LINE = "#e3e5ea"
FONT_UI = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
FONT_DISPLAY = "'Space Grotesk', 'IBM Plex Sans', Helvetica, Arial, sans-serif"
FONT_MONO = "'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace"


def _shell(inner: str, preheader: str) -> str:
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
<td style="width:40px;height:40px;background:rgba(255,255,255,.16);border-radius:10px;text-align:center;vertical-align:middle;font-family:{FONT_DISPLAY};font-weight:700;font-size:22px;color:#fff;">A</td>
<td style="padding-left:12px;font-family:{FONT_DISPLAY};font-weight:700;font-size:18px;letter-spacing:2px;color:#ffffff;">ADSUM<span style="display:block;font-family:{FONT_MONO};font-weight:500;font-size:10px;letter-spacing:1.5px;color:#c7d4f2;">SACERDOCE ROYAL</span></td>
</tr></table>
</td></tr>
{inner}
<tr><td style="padding:18px 28px 26px;border-top:1px solid {LINE};">
<p style="margin:0;font-family:{FONT_UI};font-size:11px;line-height:1.6;color:#9498a1;">Vous recevez cet e-mail dans le cadre de votre compte ADSUM (Sacerdoce Royal). Si vous n'êtes pas à l'origine de cette demande, ignorez ce message. Ne partagez jamais vos codes.</p>
</td></tr>
</table>
<p style="margin:14px 0 0;font-family:{FONT_MONO};font-size:10px;color:#9498a1;">ADSUM, plateforme d'adhésion et de présence du Sacerdoce Royal, Abidjan.</p>
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
    return _shell(inner, f"Votre code ADSUM : {code}")


def render_temp_password_email(temp_password: str, validity: str = "72 heures", app_url: str = "https://adsum-web-membre.pages.dev") -> str:
    inner = f"""\
<tr><td style="padding:30px 28px 6px;">
<h1 style="margin:0 0 8px;font-family:{FONT_DISPLAY};font-weight:700;font-size:21px;color:{INK};">Bienvenue dans ADSUM</h1>
<p style="margin:0 0 20px;font-family:{FONT_UI};font-size:14px;line-height:1.6;color:{MUT};">Votre compte membre a été créé par l'administration. Voici votre mot de passe temporaire pour vous connecter et finaliser votre inscription.</p>
<div style="margin:0 0 16px;padding:16px 18px;background:#f4f6fb;border:1px solid {LINE};border-radius:12px;text-align:center;">
<div style="font-family:{FONT_UI};font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{MUT};margin-bottom:6px;">Mot de passe temporaire</div>
<div style="font-family:{FONT_MONO};font-weight:600;font-size:24px;letter-spacing:3px;color:{ACC};">{temp_password}</div>
</div>
<p style="margin:0 0 18px;font-family:{FONT_UI};font-size:12.5px;line-height:1.6;color:{MUT};">Il est valable <b style="color:{INK};">{validity}</b>. À la première connexion, vous devrez le remplacer par votre propre mot de passe (avec un code de validation envoyé par e-mail). Passé ce délai, contactez l'administration pour en obtenir un nouveau.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 4px;"><tr>
<td style="background:{ACC};border-radius:11px;"><a href="{app_url}" style="display:inline-block;padding:12px 22px;font-family:{FONT_UI};font-weight:600;font-size:14px;color:#ffffff;text-decoration:none;">Accéder à mon espace</a></td></tr></table>
</td></tr>"""
    return _shell(inner, "Votre mot de passe temporaire ADSUM")


def render_notification_email(titre: str, corps: str, cta_label: str | None = None, cta_url: str | None = None) -> str:
    cta = ""
    if cta_label and cta_url:
        cta = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0 4px;"><tr>'
            f'<td style="background:{ACC};border-radius:11px;"><a href="{cta_url}" '
            f'style="display:inline-block;padding:12px 22px;font-family:{FONT_UI};font-weight:600;font-size:14px;'
            f'color:#ffffff;text-decoration:none;">{cta_label}</a></td></tr></table>'
        )
    inner = f"""\
<tr><td style="padding:30px 28px 8px;">
<h1 style="margin:0 0 10px;font-family:{FONT_DISPLAY};font-weight:700;font-size:20px;color:{INK};">{titre}</h1>
<p style="margin:0;font-family:{FONT_UI};font-size:14px;line-height:1.65;color:{MUT};">{corps}</p>
{cta}
</td></tr>"""
    return _shell(inner, titre)
