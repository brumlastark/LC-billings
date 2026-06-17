"""
Stáhne Meta Ads faktury za minulý měsíc a odešle je e-mailem účetnímu.

Vyžaduje tyto env proměnné:
  META_ACCESS_TOKEN   – System User token s oprávněním `ads_management`
  META_AD_ACCOUNT_ID  – ID reklamního účtu (s/bez prefixu `act_`).
                        Když je nastavený, používáme transactions endpoint.
  META_BUSINESS_ID    – ID Business Manageru (fallback pro business_invoices).
  SMTP_HOST           – např. smtp.gmail.com
  SMTP_PORT           – 587 (STARTTLS) nebo 465 (SMTPS)
  SMTP_USER           – přihlašovací jméno k SMTP
  SMTP_PASS           – heslo / app password
  ACCOUNTANT_EMAIL    – kam poslat faktury

Volitelně:
  GRAPH_API_VERSION         – výchozí v21.0
  EMAIL_FROM                – výchozí stejné jako SMTP_USER
  EMAIL_FROM_NAME           – jméno odesílatele (volitelné)
  PERIOD_OVERRIDE           – YYYY-MM, vynutí jiné období (jinak minulý měsíc)
  FILTER_PAID_ONLY          – "1"/"0" (default 1): jen uhrazené faktury
  FILTER_CREDIT_CARD_ONLY   – "1"/"0" (default 1): jen kartou placené
  DEBUG_DUMP                – "1": vypíše JSON první faktury pro odladění filtrů
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import requests

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def get_env(name: str, *, required: bool = True, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Chybí povinná env proměnná: {name}")
    return value or ""


def previous_month_range(today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """První a poslední den minulého měsíce."""
    today = today or dt.date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - dt.timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev, last_of_prev


def parse_period_override(value: str) -> tuple[dt.date, dt.date]:
    year_str, month_str = value.split("-", 1)
    year, month = int(year_str), int(month_str)
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last_day)


def fetch_billing_items(
    *,
    mode: str,
    entity_id: str,
    access_token: str,
    start: dt.date,
    end: dt.date,
) -> list[dict]:
    """Načte faktury / transakce z Meta Marketing API.

    mode='ad_account' -> GET /act_<id>/transactions (jednotlivé Visa platby
                          s VAT invoice ID)
    mode='business'   -> GET /<business_id>/business_invoices (měsíční
                          vyúčtování)
    """
    if mode == "ad_account":
        acc = entity_id if entity_id.startswith("act_") else f"act_{entity_id}"
        url = f"{GRAPH_BASE}/{acc}/transactions"
        fields = [
            "id",
            "time",
            "status",
            "payment_option",
            "billing_reason",
            "billing_period",
            "billing_amount",
            "vat_invoice_id",
            "tax_invoice_id",
            "charge_type",
            "product_type",
            "transaction_type",
            "download_uri",
        ]
    else:
        url = f"{GRAPH_BASE}/{entity_id}/business_invoices"
        fields = [
            "id",
            "invoice_id",
            "billing_period",
            "billing_period_from",
            "billing_period_to",
            "due_date",
            "amount_due",
            "currency",
            "download_uri",
            "invoice_date",
            "payment_status",
            "payment_method",
            "funding_source",
            "funding_source_details",
            "payment_account",
            "billing_reason",
            "type",
        ]

    params: dict | None = {
        "access_token": access_token,
        "fields": ",".join(fields),
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "limit": 100,
    }

    items: list[dict] = []
    while url:
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code >= 400:
            raise SystemExit(f"Meta API chyba {resp.status_code}: {resp.text}")
        body = resp.json()
        items.extend(body.get("data", []))
        url = body.get("paging", {}).get("next")
        params = None  # next URL má parametry zapečené
    return items


# Zpětně kompatibilní alias pro starší kód
def fetch_invoices(business_id, access_token, start, end):
    return fetch_billing_items(
        mode="business",
        entity_id=business_id,
        access_token=access_token,
        start=start,
        end=end,
    )


def _parse_iso_date(value: object) -> dt.date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def invoice_matches_period(invoice: dict, start: dt.date, end: dt.date) -> bool:
    """Lokální filtr: leží billing_period faktury v požadovaném měsíci?

    Meta API filtruje pomocí start_time/end_time podle invoice_date, který
    je typicky 1.–2. den NÁSLEDUJÍCÍHO měsíce. Proto fetchneme širší okno
    a tady to dofiltrujeme přesně.
    """
    # 1) primárně billing_period_from / billing_period_to
    bp_from = _parse_iso_date(invoice.get("billing_period_from"))
    bp_to = _parse_iso_date(invoice.get("billing_period_to"))
    if bp_from and bp_to:
        # překryv intervalů [bp_from, bp_to] a [start, end]
        return not (bp_to < start or bp_from > end)
    if bp_from:
        return start <= bp_from <= end

    # 2) řetězec billing_period typu "2026-05" nebo "May 2026"
    bp = str(invoice.get("billing_period") or "")
    if bp:
        if bp.startswith(start.strftime("%Y-%m")):
            return True
        if start.strftime("%m/%Y") in bp or start.strftime("%Y-%m") in bp:
            return True

    # 3) transakce: pole `time` (ISO datetime kdy transakce proběhla)
    tx_time = _parse_iso_date(invoice.get("time"))
    if tx_time:
        return start <= tx_time <= end

    # 4) poslední fallback: invoice_date musí ležet do měsíce po `end`
    inv_date = _parse_iso_date(invoice.get("invoice_date"))
    if inv_date:
        max_invoice_date = end + dt.timedelta(days=20)
        return start <= inv_date <= max_invoice_date

    # bez dat radši nevyhazujeme - DEBUG_DUMP to odhalí
    return True


PAID_STATUSES = {"PAID", "PAID_IN_FULL", "FULLY_PAID", "SETTLED"}
CREDIT_CARD_KEYWORDS = ("credit_card", "credit card", "creditcard", "card")


def is_paid(invoice: dict) -> bool:
    # `payment_status` na business_invoices, `status` na transactions
    raw = invoice.get("payment_status") or invoice.get("status") or ""
    status = str(raw).upper().strip()
    if not status:
        return False
    if "UNPAID" in status or status.startswith("NOT_") or "PARTIAL" in status:
        return False
    if "FAIL" in status or "DECLIN" in status or "PENDING" in status:
        return False
    return status in PAID_STATUSES or status.startswith("PAID") or status == "SUCCESS"


def _collect_payment_strings(invoice: dict) -> list[str]:
    """Posbírá všechny stringy z faktury, které mohou popisovat způsob platby.
    Meta API to vrací v různých polích podle regionu/typu účtu."""
    out: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)

    for key in (
        "payment_method",
        "payment_option",         # transactions endpoint
        "payment_option_string",  # transactions endpoint
        "funding_source",
        "funding_source_details",
        "payment_account",
    ):
        if key in invoice:
            visit(invoice[key])
    return out


def is_credit_card(invoice: dict) -> bool:
    """Best-effort: True, pokud nějaké pole platební metody zmiňuje kartu."""
    haystack = " ".join(_collect_payment_strings(invoice)).lower()
    return any(kw in haystack for kw in CREDIT_CARD_KEYWORDS)


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def download_invoice_pdf(
    invoice: dict,
    access_token: str,
    out_dir: Path,
) -> Path | None:
    download_uri = invoice.get("download_uri")
    if not download_uri:
        print(f"  ! Faktura {invoice.get('id')} nemá download_uri – přeskakuji.")
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(download_uri, headers=headers, timeout=120, allow_redirects=True)
    if resp.status_code >= 400:
        print(f"  ! Stažení {invoice.get('id')} selhalo: HTTP {resp.status_code}")
        return None

    inv_id = (
        invoice.get("vat_invoice_id")
        or invoice.get("invoice_id")
        or invoice.get("id")
        or "invoice"
    )
    date_source = (
        invoice.get("billing_period")
        or invoice.get("invoice_date")
        or invoice.get("time")
        or ""
    )
    safe_period = (
        str(date_source)[:10]
        .replace("/", "-")
        .replace(":", "-")
        .replace(" ", "_")
    )
    name = (
        f"meta_invoice_{safe_period}_{inv_id}.pdf"
        if safe_period
        else f"meta_invoice_{inv_id}.pdf"
    )
    path = out_dir / name
    path.write_bytes(resp.content)
    return path


def send_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_addr: str,
    from_name: str | None,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[Path],
) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    for p in attachments:
        msg.add_attachment(
            p.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=p.name,
        )

    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)


def main() -> int:
    access_token = get_env("META_ACCESS_TOKEN")
    ad_account_id = get_env("META_AD_ACCOUNT_ID", required=False, default="")
    business_id = get_env("META_BUSINESS_ID", required=False, default="")
    if not ad_account_id and not business_id:
        raise SystemExit("Nastav buď META_AD_ACCOUNT_ID, nebo META_BUSINESS_ID.")
    if ad_account_id:
        mode = "ad_account"
        entity_id = ad_account_id
    else:
        mode = "business"
        entity_id = business_id
    print(f"Režim: {mode} (entity_id={entity_id!r})")
    smtp_host = get_env("SMTP_HOST")
    smtp_port = int(get_env("SMTP_PORT", default="587") or "587")
    smtp_user = get_env("SMTP_USER")
    smtp_pass = get_env("SMTP_PASS")
    accountant = get_env("ACCOUNTANT_EMAIL")
    from_addr = get_env("EMAIL_FROM", required=False, default=smtp_user)
    from_name = get_env("EMAIL_FROM_NAME", required=False, default="") or None

    period_override = os.environ.get("PERIOD_OVERRIDE")
    if period_override:
        start, end = parse_period_override(period_override)
    else:
        start, end = previous_month_range()
    period_label = start.strftime("%m/%Y")

    filter_paid = env_bool("FILTER_PAID_ONLY", default=True)
    filter_card = env_bool("FILTER_CREDIT_CARD_ONLY", default=True)
    debug_dump = env_bool("DEBUG_DUMP", default=False)

    # Fetchujeme širší okno – Meta vystaví fakturu typicky 1.–2. den
    # NÁSLEDUJÍCÍHO měsíce, takže start_time/end_time = přesný měsíc
    # by ji minul. Lokálně pak dofiltrujeme podle billing_period.
    fetch_start = (start.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    last_day_next = calendar.monthrange(
        end.year + (1 if end.month == 12 else 0),
        1 if end.month == 12 else end.month + 1,
    )[1]
    fetch_end = dt.date(
        end.year + (1 if end.month == 12 else 0),
        1 if end.month == 12 else end.month + 1,
        last_day_next,
    )

    print(
        f"Načítám Meta Ads {mode} (API okno {fetch_start} – {fetch_end}, "
        f"cíl období {start} – {end})…"
    )
    invoices = fetch_billing_items(
        mode=mode,
        entity_id=entity_id,
        access_token=access_token,
        start=fetch_start,
        end=fetch_end,
    )
    print(f"API vrátilo {len(invoices)} položek.")

    if debug_dump:
        print("---- DEBUG_DUMP: RAW JSON všech faktur ----")
        print(json.dumps(invoices, indent=2, ensure_ascii=False))
        print("-------------------------------------------")

    # 1) lokální filtr na billing_period
    in_period: list[dict] = []
    for inv in invoices:
        if invoice_matches_period(inv, start, end):
            in_period.append(inv)
        else:
            print(
                f"  – přeskakuji {inv.get('invoice_id') or inv.get('id')}: "
                f"billing_period={inv.get('billing_period')!r} "
                f"({inv.get('billing_period_from')!r}–"
                f"{inv.get('billing_period_to')!r}) "
                f"mimo cílové období"
            )
    print(f"V cílovém období {start} – {end}: {len(in_period)} faktur(a).")

    # 2) filtr na zaplacené + kartu
    filtered: list[dict] = []
    for inv in in_period:
        inv_id = inv.get("invoice_id") or inv.get("id")
        if filter_paid and not is_paid(inv):
            print(
                f"  – přeskakuji {inv_id}: payment_status="
                f"{inv.get('payment_status')!r} (není uhrazeno)"
            )
            continue
        if filter_card and not is_credit_card(inv):
            print(
                f"  – přeskakuji {inv_id}: nevypadá na platbu kartou "
                f"(payment_method={inv.get('payment_method')!r})"
            )
            continue
        filtered.append(inv)

    print(
        f"Po filtru zbývá {len(filtered)}/{len(in_period)} faktur "
        f"(paid_only={filter_paid}, card_only={filter_card})."
    )

    out_dir = Path("invoices")
    out_dir.mkdir(exist_ok=True)

    attachments: list[Path] = []
    missing_uri: list[dict] = []
    for inv in filtered:
        if not inv.get("download_uri"):
            missing_uri.append(inv)
            continue
        path = download_invoice_pdf(inv, access_token, out_dir)
        if path:
            print(f"  ✓ Staženo {path.name} ({path.stat().st_size} B)")
            attachments.append(path)

    if missing_uri:
        print(
            f"!! {len(missing_uri)} položek prošlo filtry, ale Meta nevrátila "
            f"`download_uri` – PDF přes API nelze stáhnout. VAT invoice IDs: "
            + ", ".join(
                str(i.get("vat_invoice_id") or i.get("id")) for i in missing_uri
            )
        )

    if not attachments:
        print("Žádné faktury ke stažení – posílám pouze notifikační e-mail.")
        send_email(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_pass=smtp_pass,
            from_addr=from_addr,
            from_name=from_name,
            to_addr=accountant,
            subject=f"Meta Ads – žádné faktury za {period_label}",
            body=(
                f"Ahoj,\n\n"
                f"automatická kontrola Meta Ads účtu proběhla, ale za období "
                f"{start} – {end} se nenašly žádné faktury.\n\n"
                "— invoice bot"
            ),
            attachments=[],
        )
        return 0

    body = (
        f"Ahoj,\n\n"
        f"v příloze posílám faktury z Meta Ads za období {start} – {end} "
        f"(celkem {len(attachments)} ks).\n\n"
        "— invoice bot"
    )
    send_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        from_addr=from_addr,
        from_name=from_name,
        to_addr=accountant,
        subject=f"Meta Ads faktury – {period_label}",
        body=body,
        attachments=attachments,
    )
    print(f"Odesláno {len(attachments)} faktur(y) na {accountant}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
