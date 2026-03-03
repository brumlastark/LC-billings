#!/usr/bin/env python3
import os
import sys
import logging
import requests
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

# ----------------------------------------------------------------------
# 1️⃣ Konfigurace – načteme vše z environment (GitHub Secrets)
# ----------------------------------------------------------------------
TOKEN           = os.getenv("META_ACCESS_TOKEN")      # System‑User token
BUSINESS_ID     = os.getenv("META_BUSINESS_ID")       # čisté číslo, např. 1966180710068586
SMTP_HOST       = os.getenv("SMTP_HOST")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "465"))  # 465 = SSL, 587 = STARTTLS
SMTP_USER       = os.getenv("SMTP_USER")
SMTP_PASS       = os.getenv("SMTP_PASS")
ACCOUNTANT_MAIL = os.getenv("ACCOUNTANT_EMAIL")

# Zkontrolujeme, že máme všechno potřebné
if not all([TOKEN, BUSINESS_ID, SMTP_HOST, SMTP_USER, SMTP_PASS, ACCOUNTANT_MAIL]):
    sys.exit("❌ Missing one or more required environment variables. Check your GitHub Secrets.")

# ----------------------------------------------------------------------
# 2️⃣ Logging – jednoduchý výstup do konzole (GitHub Actions ho zachytí)
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)

# ----------------------------------------------------------------------
# 3️⃣ Pomocná funkce – vrátí první a poslední den aktuálního měsíce
# ----------------------------------------------------------------------
def current_month_range():
    """Vrátí tuple (first_day, last_day) ve formátu YYYY‑MM‑DD pro aktuální měsíc."""
    today = datetime.utcnow()
    first_day = today.replace(day=1).date()
    # první den příštího měsíce
    next_month = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_day = (next_month - timedelta(days=1)).isoformat()
    return first_day.isoformat(), last_day

# ----------------------------------------------------------------------
# 4️⃣ Načti *všechny* zaplacené faktury (bez payment_method filtru)
# ----------------------------------------------------------------------
def fetch_paid_invoices(start_date: str | None = None,
                        end_date:   str | None = None) -> list[dict]:
    """
    Vrátí seznam faktur, které mají status PAID.
    Pokud jsou zadány start_date / end_date, filtruje podle issue_date.
    """
    url = f"https://graph.facebook.com/v25.0/{BUSINESS_ID}/business_invoices"

    params = {
        "access_token": TOKEN,
        "status": "PAID",
        "fields": "id,invoice_number,amount,currency,payment_method,download_url,issue_date",
    }

    # Přidáme datumový filtr jen pokud jsou parametry zadány
    if start_date:
        params["issue_start_date"] = start_date
    if end_date:
        params["issue_end_date"] = end_date

    resp = requests.get(url, params=params, timeout=30)

    if resp.status_code != 200:
        logging.error(f"Meta API returned {resp.status_code}. Body: {resp.text}")
        resp.raise_for_status()

    invoices = resp.json().get("data", [])
    logging.info(f"✅ Retrieved {len(invoices)} PAID invoices from Meta.")
    return invoices

# ----------------------------------------------------------------------
# 5️⃣ Filtruj lokálně jen faktury zaplacené kreditní kartou
# ----------------------------------------------------------------------
def filter_credit_card(invoices: list[dict]) -> list[dict]:
    cc = [inv for inv in invoices if inv.get("payment_method") == "CREDIT_CARD"]
    logging.info(f"🔎 {len(cc)} invoices were paid by CREDIT CARD.")
    return cc

# ----------------------------------------------------------------------
# 6️⃣ Stáhni PDF faktury
# ----------------------------------------------------------------------
def download_invoice(invoice: dict) -> str:
    dl_url = invoice["download_url"]
    r = requests.get(dl_url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    r.raise_for_status()
    filename = f"{invoice['invoice_number']}_{invoice['issue_date']}.pdf"
    with open(filename, "wb") as f:
        f.write(r.content)
    logging.info(f"📥 Downloaded {filename}")
    return filename

# ----------------------------------------------------------------------
# 7️⃣ Odesli PDF e‑mailem
# ----------------------------------------------------------------------
def send_email(pdf_path: str, invoice: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Faktura {invoice['invoice_number']} – {invoice['issue_date']}"
    msg["From"] = SMTP_USER
    msg["To"] = ACCOUNTANT_MAIL

    body = (
        f"Dobrý den,\n\n"
        f"V příloze posílám fakturu {invoice['invoice_number']} z {invoice['issue_date']} "
        f"ve výši {invoice['amount']} {invoice['currency']} (zaplaceno kreditní kartou).\n\n"
        "S pozdravem,\n"
        "Automatizovaný agent"
    )
    msg.set_content(body)

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=os.path.basename(pdf_path),
        )

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    logging.info(f"✉️  Sent e‑mail with attachment {pdf_path}")

# ----------------------------------------------------------------------
# 8️⃣ Hlavní běh skriptu
# ----------------------------------------------------------------------
def main() -> None:
    try:
        # 1️⃣ Získáme faktury za aktuální měsíc (můžeš změnit dle potřeby)
        start, end = current_month_range()
        all_paid = fetch_paid_invoices(start_date=start, end_date=end)

        # 2️⃣ Vyfiltrujeme jen ty, které byly placeny kreditní kartou
        cc_invoices = filter_credit_card(all_paid)

        # 3️⃣ Pro každou fakturu stáhneme PDF a pošleme e‑mail
        for inv in cc_invoices:
            pdf_file = download_invoice(inv)
            send_email(pdf_file, inv)

        if not cc_invoices:
            logging.info("ℹ️ No credit‑card paid invoices found for the selected period.")
    except Exception:
        logging.exception("🚨 Unexpected error")
        sys.exit(1)


if __name__ == "__main__":
    main()
