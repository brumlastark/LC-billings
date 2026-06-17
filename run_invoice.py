"""
Stáhne Meta Ads transakční faktury (uhrazené, kartou) z Ads Manageru
přes browser automation (Playwright) a pošle je e-mailem účetní.

Vyžaduje env:
  META_AD_ACCOUNT_ID  – ID reklamního účtu (např. 2667399533472772, bez prefixu)
  FB_COOKIES_JSON     – JSON pole cookies z přihlášené FB session
                        (export z Chrome extension "Cookie-Editor",
                        formát "Export → JSON")
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS – SMTP konfig (Gmail app password)
  ACCOUNTANT_EMAIL    – komu posílat faktury

Volitelné:
  ALERT_EMAIL         – kam posílat warningy (default = SMTP_USER)
  PERIOD_OVERRIDE     – YYYY-MM, vynutí jiné období (jinak minulý měsíc)
  HEADLESS            – "0" pro non-headless (pro debugging), default "1"
  DEBUG_DUMP          – "1" pro screenshoty a HTML dumpy (přílohy alertu)
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
import time
import traceback
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

ADS_MANAGER_BILLING_URL = "https://business.facebook.com/billing_hub/payment_activity"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)
COOKIE_WARN_DAYS = 14  # když nejstarší zbývající expirace < tohle, pošli varování

DEBUG_ARTIFACTS: list[Path] = []  # screenshoty a html dumpy pro alert emaily


# ---------- Pomocné ----------


def get_env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Chybí povinná env proměnná: {name}")
    return value or ""


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def previous_month_range(today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    today = today or dt.date.today()
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - dt.timedelta(days=1)
    return last_of_prev.replace(day=1), last_of_prev


def parse_period_override(value: str) -> tuple[dt.date, dt.date]:
    y, m = value.split("-")
    y, m = int(y), int(m)
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


# ---------- Cookies ----------


def load_cookies(cookies_json: str) -> list[dict]:
    try:
        data = json.loads(cookies_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"FB_COOKIES_JSON není validní JSON: {e}")
    if not isinstance(data, list):
        raise SystemExit("FB_COOKIES_JSON musí být JSON pole.")

    same_site_map = {
        "unspecified": "None",
        "no_restriction": "None",
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
    }
    out: list[dict] = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            continue
        cookie: dict = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            cookie["expires"] = int(exp)
        if "httpOnly" in c:
            cookie["httpOnly"] = bool(c["httpOnly"])
        if "secure" in c:
            cookie["secure"] = bool(c["secure"])
        if c.get("sameSite") is not None:
            cookie["sameSite"] = same_site_map.get(
                str(c["sameSite"]).lower(), "Lax"
            )
        out.append(cookie)
    return out


def cookie_expiration_status(
    cookies: list[dict],
) -> tuple[dt.datetime | None, int | None]:
    """Vrátí (nejbližší expiration datetime, days_left). None pokud nelze určit."""
    now = dt.datetime.now(dt.timezone.utc)
    soonest: dt.datetime | None = None
    # Soustředíme se na sessionové cookies, které drží přihlášení (c_user, xs)
    key_names = {"c_user", "xs", "datr", "sb"}
    for c in cookies:
        if c.get("name") not in key_names:
            continue
        exp = c.get("expires")
        if not exp:
            continue
        try:
            ts = dt.datetime.fromtimestamp(int(exp), tz=dt.timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        if soonest is None or ts < soonest:
            soonest = ts
    if soonest is None:
        return None, None
    days = (soonest - now).days
    return soonest, days


# ---------- Browser ----------


def open_browser(
    playwright, cookies: list[dict], *, headless: bool
) -> tuple[Browser, BrowserContext, Page]:
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="Europe/Prague",
    )
    context.add_cookies(cookies)
    page = context.new_page()
    return browser, context, page


def capture_debug(page: Page, label: str) -> None:
    out = Path("debug")
    out.mkdir(exist_ok=True)
    png = out / f"{label}.png"
    html = out / f"{label}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
        DEBUG_ARTIFACTS.append(png)
    except Exception as e:
        print(f"  ! screenshot selhal: {e}")
    try:
        html.write_text(page.content(), encoding="utf-8")
        DEBUG_ARTIFACTS.append(html)
    except Exception as e:
        print(f"  ! html dump selhal: {e}")


def is_logged_in(page: Page) -> bool:
    """Heuristika: po navigaci na billing hub – nejsme přesměrováni na login?"""
    page.goto(ADS_MANAGER_BILLING_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    url_lower = page.url.lower()
    if "/login" in url_lower or "login.php" in url_lower:
        return False
    if page.locator("input[name='email'][type='text']").count() > 0:
        return False
    if page.locator("input[name='pass']").count() > 0:
        return False
    return True


def navigate_to_transactions(
    page: Page, ad_account_id: str, start: dt.date, end: dt.date
) -> None:
    """Otevře filtrovaný pohled na transakce za dané období."""
    asset = ad_account_id.replace("act_", "")
    url = (
        f"{ADS_MANAGER_BILLING_URL}"
        f"?asset_id={asset}"
        f"&date_preset=custom"
        f"&start_date={start.isoformat()}"
        f"&end_date={end.isoformat()}"
    )
    print(f"   GET {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # počkat na první vyrenderování dat
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)


def extract_transaction_rows(page: Page) -> list[dict]:
    """Vytáhne řádky tabulky transakcí.

    Selektory jsou hádané podle aktuálního Meta UI – pokud se rozbije,
    capture_debug nám pošle screenshot, podle něj doladíme."""
    rows = page.locator("[role='row']")
    count = rows.count()
    print(f"   nalezeno {count} řádků s role='row'")
    transactions: list[dict] = []
    # první řádek je často header – vezmeme všechny a zfiltrujeme prázdné
    for i in range(count):
        try:
            row = rows.nth(i)
            text = row.inner_text(timeout=2000).strip()
            if not text:
                continue
            cells = [c.strip() for c in text.split("\n") if c.strip()]
            transactions.append({"raw_cells": cells})
        except Exception:
            continue
    return transactions


def download_pdfs(page: Page, out_dir: Path) -> list[Path]:
    out_dir.mkdir(exist_ok=True)
    downloaded: list[Path] = []
    # Heuristika: hledáme tlačítka s aria-label obsahujícím "Download" nebo "Stáhnout"
    selectors = [
        "[aria-label*='Download'][role='button']",
        "[aria-label*='Stáhnout'][role='button']",
        "a[aria-label*='Download']",
        "a[download]",
    ]
    buttons = []
    for sel in selectors:
        loc = page.locator(sel)
        cnt = loc.count()
        if cnt:
            print(f"   selector {sel!r} → {cnt}× match")
            buttons = [loc.nth(i) for i in range(cnt)]
            break

    if not buttons:
        print("   ! nenašel jsem žádné download tlačítko (selectory neodpovídají UI)")
        return downloaded

    for i, button in enumerate(buttons):
        try:
            with page.expect_download(timeout=30000) as info:
                button.scroll_into_view_if_needed()
                button.click()
            d = info.value
            fname = d.suggested_filename or f"meta_invoice_{i+1}.pdf"
            path = out_dir / fname
            d.save_as(str(path))
            downloaded.append(path)
            print(f"   ✓ {fname}")
        except Exception as e:
            print(f"   ! download #{i+1} selhal: {e}")
    return downloaded


# ---------- Email ----------


def _smtp_send(msg: EmailMessage, host: str, port: int, user: str, password: str) -> None:
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(user, password)
            s.send_message(msg)


def send_html_email(
    *,
    smtp: dict,
    from_addr: str,
    to_addr: str,
    subject: str,
    html: str,
    plain: str | None = None,
    attachments: list[Path] | None = None,
) -> None:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(plain or "Otevři prosím v klientovi s HTML.")
    msg.add_alternative(html, subtype="html")
    for p in attachments or []:
        try:
            sub = "pdf" if p.suffix.lower() == ".pdf" else "octet-stream"
            maintype = "application"
            if p.suffix.lower() == ".png":
                maintype, sub = "image", "png"
            elif p.suffix.lower() == ".html":
                maintype, sub = "text", "html"
            msg.add_attachment(
                p.read_bytes(), maintype=maintype, subtype=sub, filename=p.name
            )
        except Exception as e:
            print(f"   ! příloha {p.name} selhala: {e}")
    _smtp_send(msg, smtp["host"], smtp["port"], smtp["user"], smtp["pass"])


def send_alert(
    *, smtp: dict, alert_addr: str, subject: str, body: str,
    attachments: list[Path] | None = None,
) -> None:
    msg = EmailMessage()
    msg["From"] = smtp["user"]
    msg["To"] = alert_addr
    msg["Subject"] = f"[invoice bot] {subject}"
    msg.set_content(body)
    for p in attachments or []:
        try:
            ext = p.suffix.lower()
            if ext == ".png":
                maintype, sub = "image", "png"
            elif ext == ".html":
                maintype, sub = "text", "html"
            else:
                maintype, sub = "application", "octet-stream"
            msg.add_attachment(
                p.read_bytes(), maintype=maintype, subtype=sub, filename=p.name
            )
        except Exception as e:
            print(f"   ! alert příloha {p.name} selhala: {e}")
    _smtp_send(msg, smtp["host"], smtp["port"], smtp["user"], smtp["pass"])


def render_html_table(transactions: list[dict], period_label: str) -> str:
    if not transactions:
        rows_html = (
            "<tr><td colspan='5' style='padding:12px;color:#666'>"
            "(žádné transakce v tomto období)</td></tr>"
        )
    else:
        rows_html = ""
        for tx in transactions:
            cells = tx.get("raw_cells", [])
            padded = cells + [""] * (5 - len(cells)) if len(cells) < 5 else cells[:5]
            rows_html += "<tr>" + "".join(
                f"<td style='padding:8px;border:1px solid #ddd'>{c}</td>"
                for c in padded
            ) + "</tr>"

    return f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Ahoj,</p>
<p>v příloze posílám faktury z Meta Ads za období <b>{period_label}</b>
(jen uhrazené, kartou).</p>
<table style="border-collapse:collapse;font-size:13px">
  <thead><tr style="background:#f5f5f5">
    <th style="padding:8px;border:1px solid #ddd;text-align:left">Datum / ID</th>
    <th style="padding:8px;border:1px solid #ddd">Částka</th>
    <th style="padding:8px;border:1px solid #ddd">Platba</th>
    <th style="padding:8px;border:1px solid #ddd">Stav</th>
    <th style="padding:8px;border:1px solid #ddd">VAT invoice ID</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="color:#888;font-size:12px">— invoice bot</p>
</body></html>"""


# ---------- Main ----------


def main() -> int:
    ad_account_id = get_env("META_AD_ACCOUNT_ID")
    cookies_json = get_env("FB_COOKIES_JSON")
    smtp = {
        "host": get_env("SMTP_HOST"),
        "port": int(get_env("SMTP_PORT", default="587") or "587"),
        "user": get_env("SMTP_USER"),
        "pass": get_env("SMTP_PASS"),
    }
    accountant = get_env("ACCOUNTANT_EMAIL")
    alert_addr = get_env("ALERT_EMAIL", required=False, default=smtp["user"])
    headless = env_bool("HEADLESS", default=True)
    debug = env_bool("DEBUG_DUMP", default=False)

    if os.environ.get("PERIOD_OVERRIDE"):
        start, end = parse_period_override(os.environ["PERIOD_OVERRIDE"])
    else:
        start, end = previous_month_range()
    period_label = start.strftime("%m/%Y")
    print(f"Cílové období: {start} – {end} ({period_label})")

    cookies = load_cookies(cookies_json)
    print(f"Načteno {len(cookies)} cookies.")

    soonest_exp, days_left = cookie_expiration_status(cookies)
    if soonest_exp:
        print(
            f"Klíčové cookies expirují nejdřív {soonest_exp.date().isoformat()} "
            f"(za {days_left} dní)."
        )
    else:
        print("Z cookies se nepodařilo přečíst expiraci.")

    out_dir = Path("invoices")

    try:
        with sync_playwright() as pw:
            browser, context, page = open_browser(pw, cookies, headless=headless)
            try:
                print("Ověřuji přihlášení…")
                if not is_logged_in(page):
                    print("❌ Cookies nefungují – přesměrováno na login.")
                    capture_debug(page, "login_failure")
                    send_alert(
                        smtp=smtp,
                        alert_addr=alert_addr,
                        subject="FB cookies neplatné, je potřeba je obnovit",
                        body=(
                            "Ahoj,\n\nautomatický invoice bot selhal: FB session "
                            "cookies už nefungují (přesměrováno na login).\n\n"
                            "Co s tím:\n"
                            "1) V Chrome se přihlas na https://business.facebook.com\n"
                            "2) Otevři rozšíření 'Cookie-Editor' "
                            "(chrome web store)\n"
                            "3) Export → JSON → zkopíruj\n"
                            "4) GitHub repo → Settings → Secrets → Actions → "
                            "uprav FB_COOKIES_JSON\n"
                            "5) Spusť workflow ručně (Run workflow)\n\n"
                            "Screenshoty toho, co bot viděl, jsou v příloze.\n\n"
                            "— invoice bot"
                        ),
                        attachments=DEBUG_ARTIFACTS,
                    )
                    return 1

                # Pre-warning: cookies fungují, ale brzy vyprší
                if days_left is not None and days_left < COOKIE_WARN_DAYS:
                    send_alert(
                        smtp=smtp,
                        alert_addr=alert_addr,
                        subject=f"FB cookies vyprší za {days_left} dní",
                        body=(
                            f"Ahoj,\n\nFB session cookies vyprší "
                            f"{soonest_exp.date().isoformat()} (za {days_left} dní).\n"
                            "Než přijde příští plánovaný run (2. v měsíci), prosím:\n"
                            "1) V Chrome se přihlas na business.facebook.com\n"
                            "2) Cookie-Editor → Export → JSON\n"
                            "3) Aktualizuj GitHub Secret FB_COOKIES_JSON\n\n"
                            "Run dnes proběhne normálně, tohle je jen heads-up.\n\n"
                            "— invoice bot"
                        ),
                    )

                print("Naviguji do Payments → filtrované období…")
                navigate_to_transactions(page, ad_account_id, start, end)

                if debug:
                    capture_debug(page, "transactions_page")

                transactions = extract_transaction_rows(page)
                print(f"Extrahováno {len(transactions)} řádků z tabulky.")

                attachments = download_pdfs(page, out_dir)
                print(f"Staženo {len(attachments)} PDF.")

                if not attachments:
                    capture_debug(page, "no_downloads")
                    send_alert(
                        smtp=smtp,
                        alert_addr=alert_addr,
                        subject="bot prošel, ale nestáhl žádné PDF",
                        body=(
                            f"Ahoj,\n\ninvoice bot doběhl, ale nestáhl žádné PDF "
                            f"za období {period_label}.\n\n"
                            f"Možné důvody:\n"
                            f" – za období opravdu nebyly žádné kartové transakce\n"
                            f" – Meta UI změnila selektory pro Download tlačítko\n"
                            f" – CAPTCHA / anti-bot blok\n\n"
                            f"V příloze screenshot a HTML stránky – podle toho "
                            f"doladíme selektory.\n\n"
                            f"Účetní zatím NIC neposílám.\n\n— invoice bot"
                        ),
                        attachments=DEBUG_ARTIFACTS,
                    )
                    return 0

                html = render_html_table(transactions, period_label)
                send_html_email(
                    smtp=smtp,
                    from_addr=smtp["user"],
                    to_addr=accountant,
                    subject=f"Meta Ads faktury – {period_label}",
                    html=html,
                    plain=(
                        f"V příloze faktury z Meta Ads za {period_label} "
                        f"({len(attachments)} ks)."
                    ),
                    attachments=attachments,
                )
                print(f"Odesláno na {accountant}: {len(attachments)} PDF.")
                return 0

            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as e:
        tb = traceback.format_exc()
        print(f"!! Neočekávaná chyba: {e}\n{tb}")
        send_alert(
            smtp=smtp,
            alert_addr=alert_addr,
            subject="invoice bot vyhodil výjimku",
            body=f"Stack trace:\n\n{tb}",
            attachments=DEBUG_ARTIFACTS,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
