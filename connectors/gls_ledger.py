"""Carrier ledger from the carrier's own e-mails (GLS Hungary): what it collected as cash on delivery, what it kept against its
invoices (compensation), what it transferred, and which invoice is still open. A balance at the end: does the carrier owe us COD
money, or do we owe it an invoice? Only collected (paid) parcels count.

Sources, read from a mailbox (connectors/mailbox_imap.py, reading only):
  - COD detail spreadsheet (subject "Utanvet reszletezo …"): per parcel the COD reference, delivery date, amount.
  - Invoice + settlement spreadsheet (subject "… Számla és Számlamelléklet (<invoice no>)"): per parcel the fee, net.
  - Compensation letter PDF (subject "Kompenzációs értesítő"): COD | compensated | transferred | invoice | invoice amount | remaining | date.
  - Payment reminder PDF ("Fiz. emlekezteto"): overdue invoice numbers and amounts.
Name and address columns are never read out: only references, parcel numbers, amounts and dates are kept.
State: brain/gls_ledger.json (processed message uids + parsed records). Settings (.env):
  GLS_LEDGER_ACCOUNT=<IMAP account name>   GLS_LEDGER_SENDER=gls-hungary.com   GLS_LEDGER_SINCE=2026-10-01   GLS_INVOICE_VAT=0.27
CLI: python3 connectors/gls_ledger.py sync        (reads new mails, saves attachments, updates the state)
     python3 connectors/gls_ledger.py balance     (the summary; no mail access)
     python3 connectors/gls_ledger.py mark_paid_invoice --invoice HU00000001   (the owner paid it himself)
The site sender (ecom_sync) runs sync at most once an hour when GLS_LEDGER_ACCOUNT is set."""
import os, re, sys, io, json, zipfile, pathlib, datetime as dt, xml.etree.ElementTree as ET
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, run_cli

STATE = HERE.parent / "brain" / "gls_ledger.json"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

def _num(s):
    s = str(s or "").strip().replace(" ", " ")
    if re.fullmatch(r"-?\d+(\.\d+)?", s): return float(s)
    s2 = s.replace(" ", "").replace(".", "").replace(",", ".") if re.fullmatch(r"-?[\d .]+(,\d+)?", s) else s
    try: return float(s2)
    except ValueError: return None

def _excel_date(v):
    n = _num(v)
    if n is None: return str(v or "")[:10] or None
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(n))).isoformat()

def sheets(data):
    """{sheet name: rows} from xlsx bytes; shared and inline strings resolved."""
    z = zipfile.ZipFile(io.BytesIO(data)); shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
    out = {}
    for name in sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        rows = []
        for r in ET.fromstring(z.read(name)).iter(NS + "row"):
            vals = []
            for c in r.findall(NS + "c"):
                v = c.find(NS + "v"); t = c.get("t")
                txt = (v.text if v is not None else "") or "".join(x.text or "" for x in c.iter(NS + "t"))
                if t == "s" and txt.isdigit() and int(txt) < len(shared): txt = shared[int(txt)]
                vals.append(txt)
            rows.append(vals)
        out[re.search(r"sheet\d+", name).group(0)] = rows
    return out

def _col(header, *starts):
    for i, h in enumerate(header):
        if any(h.strip().lower().startswith(s.lower()) for s in starts): return i
    return None

def parse_cod_detail(data):
    """→ [{ref, parcel, delivered, amount}] from the COD detail spreadsheet (header row found by its column names)."""
    rows = next(iter(sheets(data).values()), [])
    hi = next((i for i, r in enumerate(rows) if _col(r, "Csomagszám") is not None and _col(r, "Utánvét összeg") is not None), None)
    if hi is None: raise ConnectorError("[COD detail] header row not found")
    h = rows[hi]; ip, ir, idt, ia = _col(h, "Csomagszám"), _col(h, "Utánvét hivatkozás"), _col(h, "Kiszállítási dátum"), _col(h, "Utánvét összeg")
    out = []
    for r in rows[hi + 1:]:
        if len(r) <= ia or not str(r[ip] if ip < len(r) else "").strip().isdigit(): continue  # the closing total row has no parcel
        out.append({"ref": (r[ir] if ir is not None and ir < len(r) else "").strip(), "parcel": r[ip].strip(),
                    "delivered": (r[idt] if idt is not None and idt < len(r) else "")[:10], "amount": _num(r[ia]) or 0.0})
    return out

def parse_settlement(data):
    """→ {invoice, date, parcels: [{ref, parcel, net}], cod_fees_net, net} from the invoice's settlement spreadsheet."""
    sh = sheets(data); s1 = sh.get("sheet1") or []
    if not s1: raise ConnectorError("[settlement] empty")
    h = s1[0]; iinv, idate, ip, ir, it = (_col(h, "Számlaszám"), _col(h, "Számla kiállítás"), _col(h, "Csomagszám"),
                                         _col(h, "Ügyfél hivatkozás"), _col(h, "VÉGÖSSZEG"))
    if None in (iinv, ip, it): raise ConnectorError("[settlement] columns not found")
    parcels = [{"ref": (r[ir] if ir is not None and ir < len(r) else "").strip(), "parcel": r[ip].strip(), "net": _num(r[it]) or 0.0}
               for r in s1[1:] if len(r) > it and r[ip].strip().isdigit()]
    fees = 0.0
    s3 = sh.get("sheet3") or []
    if s3:
        jt = _col(s3[0], "VÉGÖSSZEG")
        if jt is not None: fees = sum(_num(r[jt]) or 0.0 for r in s3[1:] if len(r) > jt and _num(r[jt]) is not None)
    inv = next((r[iinv] for r in s1[1:] if len(r) > iinv and r[iinv]), "")
    date = next((_excel_date(r[idate]) for r in s1[1:] if idate is not None and len(r) > idate and r[idate]), None)
    return {"invoice": inv, "date": date, "parcels": parcels, "cod_fees_net": round(fees, 2),
            "net": round(sum(p["net"] for p in parcels) + fees, 2)}

AMT = r"(\d{1,3}(?: \d{3})*|\d+)"
def parse_compensation(text):
    """→ [{cod, compensated, transferred, invoice, invoice_amount, remaining, date}] from the compensation letter's text."""
    out = []
    for m in re.finditer(AMT + r"\s+" + AMT + r"\s+" + AMT + r"\s+(HU\d+)\s+" + AMT + r"\s+" + AMT + r"\s+(\d{4}\.\d{2}\.\d{2})", text):
        n = [_num(m.group(i)) for i in (1, 2, 3, 5, 6)]
        out.append({"cod": n[0], "compensated": n[1], "transferred": n[2], "invoice": m.group(4), "invoice_amount": n[3],
                    "remaining": n[4], "date": m.group(7).replace(".", "-")})
    return out

def parse_reminder(text):
    """→ [{invoice, date, amount}] overdue invoices from a payment reminder's text."""
    return [{"invoice": m.group(1), "date": m.group(2).replace(".", "-"), "amount": _num(m.group(3).replace(".", " "))}
            for m in re.finditer(r"(HU\d{8})\s+(\d{4}\.\d{2}\.\d{2})\s+\w+\s+HUF\s+([\d.]+)", text)]

def _read():
    try: d = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError): d = {}
    for k, v in (("seen", []), ("cod", {}), ("invoices", {}), ("compensations", []), ("reminders", {})): d.setdefault(k, v)
    return d

def _write(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(STATE)

def _pdf_text(path):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)

def ingest(d, subject, files, uid):
    """One mail's saved attachments into the state. files: [pathlib.Path]."""
    vat = float(os.getenv("GLS_INVOICE_VAT", "0.27") or 0.27)
    for f in files:
        name = f.name.lower()
        if name.endswith(".xlsx") and "settlement" in name:
            s = parse_settlement(f.read_bytes())
            prev = d["invoices"].get(s["invoice"], {})
            d["invoices"][s["invoice"]] = {**prev, "date": s["date"], "parcels": len(s["parcels"]), "net": s["net"],
                                           "gross": prev.get("gross") or round(s["net"] * (1 + vat)),
                                           "per_parcel_net": [p["net"] for p in s["parcels"]], "refs": [p["ref"] for p in s["parcels"]]}
        elif name.endswith(".xlsx") and re.search(r"_huf_\d{8}", name):
            for r in parse_cod_detail(f.read_bytes()):
                d["cod"][r["parcel"]] = {"ref": r["ref"], "delivered": r["delivered"], "amount": r["amount"], "report_uid": uid}
        elif name.endswith(".pdf") and "kompenz" in subject.lower():
            for c in parse_compensation(_pdf_text(f)):
                if c not in d["compensations"]: d["compensations"].append(c)
                inv = d["invoices"].setdefault(c["invoice"], {})
                inv["gross"] = c["invoice_amount"] if not inv.get("compensated") else inv.get("gross")
                inv["compensated"] = round((inv.get("compensated") or 0) + c["compensated"], 2); inv["remaining"] = c["remaining"]
        elif name.endswith(".pdf") and "emlekezteto" in (subject + name).lower().replace("é", "e"):
            for r in parse_reminder(_pdf_text(f)):
                d["reminders"][r["invoice"]] = r
                inv = d["invoices"].setdefault(r["invoice"], {})
                if not inv.get("compensated"): inv["gross"] = r["amount"]  # the carrier's own figure wins over net × VAT

def sync(account="", since=""):
    """Reads new carrier mails (reading only), saves their attachments under handoffs/inbox/mail/gls/, updates brain/gls_ledger.json."""
    import mailbox_imap as mb
    account = account or os.getenv("GLS_LEDGER_ACCOUNT", "").strip()
    if not account: raise ConnectorError("GLS_LEDGER_ACCOUNT is not set (the IMAP account name)")
    since = since or os.getenv("GLS_LEDGER_SINCE", "").strip() or (dt.date.today() - dt.timedelta(days=60)).isoformat()
    d = _read(); new = []
    found = mb.search(account, sender=os.getenv("GLS_LEDGER_SENDER", "gls-hungary.com"), since=since, max="200")["messages"]
    for m in reversed(found):  # oldest first, so a later compensation finds its invoice
        subj = m["subject"] or ""
        if m["uid"] in d["seen"] or not re.search(r"utanvet reszletezo|számla és számlamelléklet|kompenz|emlekezteto|emlékeztető", subj.lower()): continue
        saved = mb.save_attachments(account, m["uid"], folder="gls")["saved"]
        ingest(d, subj, [HERE.parent / p for p in saved], m["uid"])
        d["seen"].append(m["uid"]); new.append(subj.split("\n")[0][:60])
    d["synced_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _write(d)
    return {"new": new, "balance": balance()}

def mark_paid_invoice(invoice, note=""):
    """The owner paid a carrier invoice himself (bank transfer): it is no longer open. Kept in the state, not in .env."""
    if not re.fullmatch(r"[A-Z]{2}\d{6,}", (invoice or "").strip()): raise ConnectorError("--invoice: the carrier's invoice number, e.g. HU00000001")
    d = _read(); d.setdefault("paid", {})[invoice.strip()] = {"at": dt.date.today().isoformat(), "note": note[:200]}
    _write(d); return {"paid": sorted(d["paid"])}

UPLOADED = HERE.parent / "brain" / "gls_uploaded.json"
GLS_STATUS = HERE.parent / "brain" / "gls_status.json"

def _cod_on_the_way(d):
    """COD parcels the carrier delivered (status file) that no COD detail report lists yet: money that will still come."""
    try:
        up = json.loads(UPLOADED.read_text(encoding="utf-8")); st = json.loads(GLS_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    reported = {c.get("ref") for c in d["cod"].values()} | set(d["cod"])
    out = []
    for name, s in st.items():
        rec = up.get(name) if isinstance(up.get(name), dict) else {}
        amount = rec.get("cod_amount") or 0
        if amount and (s or {}).get("state") == "delivered" and name not in reported and str((s or {}).get("parcel_number")) not in reported:
            out.append({"ref": name, "amount": amount, "delivered": (s.get("delivered_at") or "")[:10]})
    return out

def balance(paid=""):
    """The summary: collected COD, kept against invoices, transferred, open invoices and the end balance. paid: invoice numbers
    the owner paid himself, comma separated (also GLS_PAID_INVOICES in .env, or mark_paid_invoice, kept in the state)."""
    d = _read()
    paid_set = {x.strip() for x in (paid or os.getenv("GLS_PAID_INVOICES", "")).split(",") if x.strip()} | set(d.get("paid") or {})
    collected = round(sum(c["amount"] for c in d["cod"].values()), 2)
    compensated = round(sum(c["compensated"] for c in d["compensations"]), 2)
    transferred = round(collected - compensated, 2)
    open_inv = []
    for no, inv in sorted(d["invoices"].items()):
        rest = inv.get("remaining") if inv.get("compensated") is not None and "remaining" in inv else (inv.get("gross") or 0) - (inv.get("compensated") or 0)
        if rest and rest > 0 and no not in paid_set:
            open_inv.append({"invoice": no, "date": inv.get("date"), "open": round(rest, 2), "reminded": no in d["reminders"]})
    owed_to_carrier = round(sum(i["open"] for i in open_inv), 2)
    nets = [n for inv in d["invoices"].values() for n in inv.get("per_parcel_net") or []]
    vat = float(os.getenv("GLS_INVOICE_VAT", "0.27") or 0.27)
    coming = _cod_on_the_way(d); coming_sum = round(sum(c["amount"] for c in coming), 2)
    if not owed_to_carrier: verdict = "nobody owes anything" + (f"; COD still coming: {coming_sum:.0f}" if coming_sum else "")
    elif coming_sum >= owed_to_carrier: verdict = f"open invoices {owed_to_carrier:.0f} will be kept from the COD on the way ({coming_sum:.0f})"
    else: verdict = f"pay the carrier {owed_to_carrier - coming_sum:.0f}: not enough COD on the way to keep it from"
    return {"collected_cod": collected, "kept_for_invoices": compensated, "transferred": transferred,
            "open_invoices": open_inv, "we_owe_carrier": owed_to_carrier, "cod_on_the_way": coming, "cod_on_the_way_total": coming_sum,
            "must_pay": round(max(owed_to_carrier - coming_sum, 0), 2), "verdict": verdict,
            # the timeline for the site: references, amounts and dates only
            "cod_items": sorted(({"ref": c.get("ref"), "delivered": c.get("delivered"), "amount": c.get("amount")} for c in d["cod"].values()),
                                key=lambda x: x["delivered"] or ""),
            "compensations": [{k: c.get(k) for k in ("date", "invoice", "cod", "compensated", "transferred", "remaining")} for c in d["compensations"]],
            "invoices": [{"invoice": no, "date": inv.get("date"), "gross": inv.get("gross"), "compensated": inv.get("compensated") or 0,
                          "paid_by_owner": no in paid_set, "reminded": no in d["reminders"], "parcels": inv.get("parcels")}
                         for no, inv in sorted(d["invoices"].items())],
            "avg_fee_per_parcel_gross": round(sum(nets) / len(nets) * (1 + vat)) if nets else None,
            "parcels_in_invoices": len(nets), "synced_at": d.get("synced_at")}

OPS = {"sync": sync, "balance": balance, "mark_paid_invoice": mark_paid_invoice}
if __name__ == "__main__": sys.exit(run_cli(OPS))
