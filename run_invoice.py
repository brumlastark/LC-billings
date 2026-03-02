#!/usr/bin/env python3
import os, sys, logging, requests, smtplib
from email.message import EmailMessage

# -------------------- konfigurace --------------------
TOKEN           = os.getenv("META_ACCESS_TOKEN")
BUSINESS_ID     = os.getenv("META_BUSINESS_ID")   # <--- čisté číslo!
SMTP_HOST       = os.getenv("SMTP_HOST")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER       = os.getenv("SMTP_USER")
SMTP_PASS       = os.getenv("SMTP_PASS")
ACCOUNTANT_MAIL = os.getenv("ACCOUNTANT_EMAIL")

if not all([TOKEN, BUSINESS_ID, SMTP_HOST, SMTP_USER, SMTP_PASS, ACCOUNTANT_MAIL]):
    sys.exit("❌ Missing required environment variables – check your GitHub Secrets.")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler()])


# -------------------- 1️⃣ fetch invoices --------------------
def fetch_paid_cc_invoices() -> list[dict]:
    url = f"https://graph.facebook.com/v18.0/{BUSINESS_ID}/billing_invoices"
    params = {
        "access_token": TOKEN,
        "status": "PAID",
        "fields": "id,invoice_number,amount,currency,payment_method,download_url,issue_date",
    }
    resp = requests.get(url, params=params, timeout=30)

    if resp.status_code != 200:
        # Vypíšeme celou odpověď – pomůže při ladění
        logging.error(
            f"Meta API returned {resp.status_code}. "
            f"Response body: {resp.text}"
        )
        resp.raise_for_status()   # vyvolá HTTPError, který zachytíme v main()
    data = resp.json().get("data", [])
    cc_invoices = [inv for inv in data if inv.get("payment_method") == "CREDIT_CARD"]
    logging.info(f"✅ Found {len(cc_invoices)} paid‑by‑card invoices.")
    return cc_invoices


# -------------------- 2️⃣ download PDF --------------------
def download_invoice(invoice: dict) -> str:
    dl_url = invoice["download_url"]
    r = requests.get(dl_url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    r.raise_for_status()
    filename = f"{invoice['invoice_number']}_{invoice['issue_date']}.pdf"
    with open(filename, "wb") as f:
        f.write(r.content)
    logging.info(f"📥 Downloaded {filename}")
    return filename


# -------------------- 3️⃣ send e‑mail --------------------
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


# -------------------- main --------------------
def main() -> None:
    try:
        invoices = fetch_paid_cc_invoices()
        for inv in invoices:
            pdf = download_invoice(inv)
            send_email(pdf, inv)
    except Exception:
        logging.exception("🚨 Unexpected error")
        sys.exit(1)


if __name__ == "__main__":
    main()
