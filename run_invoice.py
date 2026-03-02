#!/usr/bin/env python3
import os
import sys
import logging
import requests
import smtplib
from email.message import EmailMessage

# ----------------------------------------------------------------------
# Configuration – read from environment (GitHub Secrets)
# ----------------------------------------------------------------------
TOKEN           = os.getenv("META_ACCESS_TOKEN")
BUSINESS_ID     = os.getenv("META_BUSINESS_ID")
SMTP_HOST       = os.getenv("SMTP_HOST")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER       = os.getenv("SMTP_USER")
SMTP_PASS       = os.getenv("SMTP_PASS")
ACCOUNTANT_MAIL = os.getenv("ACCOUNTANT_EMAIL")

# Verify that everything we need is present
required = [TOKEN, BUSINESS_ID, SMTP_HOST, SMTP_USER, SMTP_PASS, ACCOUNTANT_MAIL]
if not all(required):
    sys.exit("❌ Missing one or more required environment variables. Check your GitHub Secrets.")

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)

# ----------------------------------------------------------------------
# 1️⃣ Fetch paid invoices that were paid by credit card
# ----------------------------------------------------------------------
def fetch_paid_cc_invoices() -> list[dict]:
    url = f"https://graph.facebook.com/v18.0/1966180710068586/billing_invoices"
    params = {
        "access_token": TOKEN,
        "status": "PAID",
        "fields": "id,invoice_number,amount,currency,payment_method,download_url,issue_date",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    invoices = resp.json().get("data", [])
    cc_invoices = [inv for inv in invoices if inv.get("payment_method") == "CREDIT_CARD"]
    logging.info(f"✅ Found {len(cc_invoices)} paid‑by‑card invoices.")
    return cc_invoices

# ----------------------------------------------------------------------
# 2️⃣ Download a single PDF invoice
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
# 3️⃣ Send the PDF via e‑mail
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
# Main driver
# ----------------------------------------------------------------------
def main() -> None:
    try:
        invoices = fetch_paid_cc_invoices()
        for inv in invoices:
            pdf_file = download_invoice(inv)
            send_email(pdf_file, inv)
    except Exception as exc:
        logging.exception("🚨 Unexpected error")
        sys.exit(1)


if __name__ == "__main__":
    main()
