"""The order chain's milestones, told to the owner in plain words, and the cash-on-delivery invoice at delivery (owner's rule,
2026-11-16). Called from the site sender's round (ecom_sync) after the automatic label/draft step and the COD-paid step; only in
a real round (act=True), never in a dry run.

  1. New order       → news: "<owner>, a new order came in: #…, cash on delivery, … Ft. The invoice draft and the GLS label are
                       ready." (or which step is held and which field is missing, never the data itself)
  2. GLS picked it up → news: "GLS picked up the parcel of #…"
  3. Delivered, COD  → the invoice, which was only a draft until now, may be issued and sent. The autonomy table decides:
                       issue_invoice at level 2 or more: issued and e-mailed here (order_flow.invoice_issue), then news;
                       below that (default, level 1): one approval item per order, "#…: the customer took the parcel. May I issue
                       and send the invoice?", carried out after the owner's GO with the exact command in the item.
                       Before delivery a COD invoice is never issued: order_flow.cod_gate refuses it, whoever asks.
     Returned, COD   → news: the parcel went back, the invoice stays a draft.
  4. COD transferred → news when cod_paid set the order paid (auto); its own approval item otherwise. Amount differences,
                       unclear pairings and late transfers are told once each.
A PAID (card, transfer) order keeps its old path: no delivery question, its invoice goes the way it always did.

No back-fill: the first real run writes brain/order_events.json "since"; only orders created (and for the invoice question:
parcels delivered) after it are handled. COD_INVOICE_SINCE (ISO date) asks for older deliveries on purpose.
Switches (.env): ORDER_NOTICES=off stops the news; COD_INVOICE_ON_DELIVERY=off stops the delivery invoice step entirely.
Only order numbers, payment type, amounts and states are written or said: never a customer's name, address, phone or e-mail."""
import os, re, sys, json, pathlib, datetime as dt, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "core"))
from cbase import ConnectorError
import cod_paid, order_auto
import notices, texts

ROOT = HERE.parent
BRAIN = ROOT / "brain"
STATE = BRAIN / "order_events.json"   # {"since": iso, "invoice": {order name: {...}}}
RETRY = 1800                          # a failed automatic issue is tried again after 30 minutes
LATE_NEWS = 3600                      # a new order whose automatic steps never ran is told anyway after an hour
ACTION = "issue_invoice"


def _on(name, default="on"):
    return (os.getenv(name, default) or default).strip().lower() not in ("off", "no", "0", "false")


def _gate(action):
    try:
        import autonomy
        return autonomy.Config().allowed(action).level
    except Exception:
        return 0  # cannot read the table: fail closed


def _read(path):
    try: d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    return d if isinstance(d, dict) else {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(path)


def _ts(value):
    try: d = dt.datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError: return None
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()


def _iso(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def money(n):
    return f"{int(n or 0):,}".replace(",", " ") + " Ft"


def owner_name():
    """config/business.yaml business.owner_name, optional: "Barni, a new order came in" instead of "New order"."""
    try:
        import business
        v = (business.load().get("business") or {}).get("owner_name")
        return str(v).strip() if isinstance(v, (str, int)) and str(v).strip() else ""
    except Exception:
        return ""


def _sale(o):
    """Cancelled, Shopify test, 0 Ft and tagged (barter / gift / test…) orders are never told about."""
    return not (o.get("cancelled_at") or o.get("test") or cod_paid._tagged(o) or (cod_paid.forint(o.get("total_price")) or 0) <= 0)


def _pay(o): return texts.t("order.pay_cod" if cod_paid.is_cod(o) else "order.pay_paid")


def _invoice_doc(name, invoices, kind):
    """The Billingo document for this order: kind 'draft' or 'issued'. '#1001' never matches a comment about '#10015'."""
    for d in invoices or []:
        if not re.search(re.escape(name) + r"(?!\d)", d.get("comment") or ""): continue
        if (d.get("type") == "draft") == (kind == "draft"): return d
    return None


# ---------------------------------------------------------------------------------------------------------- 1. new order
def _step(key, enabled, done, held, rec):
    """→ 'done' | 'manual' | ('held', fields) | 'error' | None (not tried yet)."""
    if not enabled: return "manual"
    if done: return "done"
    if held: return ("held", held)
    if (rec or {}).get("error"): return "error"
    return None


def new_order_text(o, label, draft):
    name, cod = o.get("name") or "", cod_paid.is_cod(o)
    if label == "manual" and draft == "manual": steps = [texts.t("order.step_manual")]
    elif label == "done" and draft == "done": steps = [texts.t("order.steps_both")]
    else:
        steps = []
        for which, st in (("draft", draft), ("label", label)):
            if st == "done": steps.append(texts.t(f"order.step_{which}_done"))
            elif st == "error": steps.append(texts.t(f"order.step_{which}_error"))
            elif isinstance(st, tuple): steps.append(texts.t(f"order.step_{which}_held", fields=", ".join(st[1])))
        if "manual" in (label, draft): steps.append(texts.t("order.step_manual"))
    if cod: steps.append(texts.t("order.cod_note"))
    who = owner_name()
    fields = dict(order=name, payment=_pay(o), amount=money(cod_paid.forint(o.get("total_price"))), steps=" ".join(steps))
    return texts.t("order.new_named", owner=who, **fields) if who else texts.t("order.new", **fields)


def _new_order(o, auto, uploaded, auto_state, now):
    name = o.get("name") or ""
    rec = auto_state.get(name) or {}
    held = {}
    for h in (auto or {}).get("held", {}).get(name, []):
        step, _, field = h.partition(": ")  # order_auto: "label: <field>", "invoice: <field>", "order: <field>" (both steps)
        for which in {"label": ("label",), "invoice": ("draft",)}.get(step, ("label", "draft")):
            held.setdefault(which, []).append(field or step)
    enabled = (auto or {}).get("enabled") or {}
    label = _step("label", enabled.get("label"), name in (uploaded or {}) or (rec.get("label") or {}).get("at"), held.get("label"), rec.get("label"))
    draft = _step("draft", enabled.get("draft"), (rec.get("draft") or {}).get("at"), held.get("draft"), rec.get("draft"))
    if None in (label, draft):
        if now - (_ts(o.get("created_at")) or now) < LATE_NEWS: return None  # its automatic steps run this round or the next
        label, draft = label or "manual", draft or "manual"
    return new_order_text(o, label, draft)


# ------------------------------------------------------------------------------------------- 3. COD delivered → invoice
def _item_text(name, amount, draft_id):
    command = f"python3 connectors/order_flow.py invoice_issue --draft_id {draft_id} --confirm yes --send yes"
    return texts.t("order.invoice_question", order=name, amount=amount) + "\n\n" + texts.t("order.invoice_item", command=command)


def _book():
    import approvals
    return approvals.Book(BRAIN, ROOT)


def _flow():
    spec = importlib.util.spec_from_file_location("events_order_flow", HERE / "order_flow.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def cod_invoice(o, ship, invoices, auto_state, state, now, say, book=None, flow=None, gate=None):
    """One delivered COD order. → 'issued' | 'queued' | 'no_draft' | 'already' | 'wait' | 'error' | None (nothing to do).
    state: brain/order_events.json["invoice"], changed in place. say(key, text): queues the news."""
    name = o.get("name") or ""
    amount = money(cod_paid.forint(o.get("total_price")))
    rec = state.get(name) or {}
    if rec.get("invoice") or rec.get("approval_id") or rec.get("already"): return None  # handled once
    if rec.get("error") and now - rec.get("tried", 0) < RETRY: return "wait"
    if invoices is None: return "wait"  # Billingo did not answer: unknown, try next round
    if _invoice_doc(name, invoices, "issued"):
        state[name] = {"already": True, "at": _iso(now)}; return "already"
    draft_id = ((auto_state.get(name) or {}).get("draft") or {}).get("draft_id") or (_invoice_doc(name, invoices, "draft") or {}).get("id")
    if not draft_id:
        say(f"invoice_no_draft:{name}", texts.t("order.invoice_no_draft", order=name)); return "no_draft"
    if (gate or _gate)(ACTION) >= 2:
        flow = flow or _flow()
        try: r = flow.invoice_issue(str(draft_id), confirm="yes", send="yes")
        except ConnectorError as e:
            state[name] = {"error": str(e)[:300], "tried": int(now), "draft_id": draft_id}
            say(f"invoice_failed:{name}", texts.t("order.invoice_issue_failed", order=name, why=str(e)[:200]))
            return "error"
        number = r.get("invoice") or "?"
        state[name] = {"invoice": number, "draft_id": draft_id, "at": _iso(now), "by": "auto", "sent": bool(r.get("sent"))}
        say(f"invoice_issued:{name}", texts.t("order.invoice_issued", order=name, invoice=number))
        return "issued"
    try:
        it, _ = (book or _book()).add("ledger", ACTION, texts.t("order.invoice_question", order=name, amount=amount),
                                      summary=f"{name} {amount}", text=_item_text(name, amount, draft_id),
                                      payload={"order": name, "draft_id": str(draft_id), "rule": "cod_invoice_on_delivery"},
                                      ref=f"{name.lstrip('#')}_cod_atvette")
    except Exception as e:  # level 0 (forbidden) or the queue cannot be written: said in the round's errors, tried next round
        state[name] = {"error": f"{type(e).__name__}: {e}"[:300], "tried": int(now), "draft_id": draft_id}
        return "error"
    state[name] = {"approval_id": it["id"], "draft_id": str(draft_id), "at": _iso(now)}
    return "queued"


# ---------------------------------------------------------------------------------------------------------------- the round
def _since(d, now):
    env = os.getenv("COD_INVOICE_SINCE", "").strip()
    start = _ts(d.get("since"))
    if start is None: d["since"] = _iso(now); start = now
    return start, (_ts(env) if env else None)


def run(raw, shipping, uploaded, auto, cod, invoices, now, act=False, book=None, flow=None, gate=None, brain=None):
    """raw: Shopify orders (ecom_sync's 'every'). shipping: brain/gls_status.json. auto: order_auto.run output. cod: cod_paid.run
    output. invoices: Billingo's list (None = not answered). → {"news": [keys], "invoice": {"queued", "issued", "no_draft",
    "errors"}}. act=False: nothing at all is written or queued."""
    brain = pathlib.Path(brain) if brain else BRAIN
    out = {"news": [], "invoice": {"queued": [], "issued": [], "no_draft": [], "errors": []}}
    if not act or raw is None: return out
    tell = _on("ORDER_NOTICES")
    def say(key, text):
        if tell and notices.add(brain, key, text, now=now): out["news"].append(key)
    path = brain / STATE.name
    d = _read(path); inv_state = d.setdefault("invoice", {})
    since, back = _since(d, now)
    auto_state = _read(order_auto.STATE)
    for o in raw:
        name = o.get("name") or ""
        if not name or not _sale(o): continue
        created = _ts(o.get("created_at")) or 0
        ship = (shipping or {}).get(name) or {}
        fresh = created >= since
        if fresh and not notices.known(brain, f"new_order:{name}"):
            text = _new_order(o, auto, uploaded, auto_state, now)
            if text: say(f"new_order:{name}", text)
        amount = money(cod_paid.forint(o.get("total_price")))
        if fresh and ship.get("handed_at") and ship.get("state") in ("handed_to_gls", "in_transit"):
            say(f"picked_up:{name}", texts.t("order.picked_up", order=name, payment=_pay(o), amount=amount))
        if not cod_paid.is_cod(o): continue  # a PAID order: its invoice goes the old way
        delivered_at = _ts(ship.get("delivered_at") or ship.get("status_at"))
        recent = fresh or (delivered_at is not None and delivered_at >= (back if back is not None else since))
        if ship.get("state") == "returned" and recent:
            say(f"returned:{name}", texts.t("order.returned_cod", order=name, amount=amount))
        if not recent or not _on("COD_INVOICE_ON_DELIVERY") or o.get("financial_status") not in ("pending", "paid"): continue
        if ship.get("state") != "delivered" or str(ship.get("code") or "05") != "05": continue
        r = cod_invoice(o, ship, invoices, auto_state, inv_state, now, say, book=book, flow=flow, gate=gate)
        if r in ("queued", "issued", "no_draft"): out["invoice"][r].append(name)
        elif r == "error": out["invoice"]["errors"].append(f"[COD invoice {name}] {inv_state.get(name, {}).get('error')}")
    for row in (cod or {}).get("marked_rows") or []:
        say(f"cod_paid:{row['name']}", texts.t("order.cod_paid", order=row["name"], amount=money(row.get("total"))))
    for issue in (cod or {}).get("issues") or []:
        kind = issue.get("kind")
        if kind in ("mismatch", "unclear", "late"):
            say(f"cod_{kind}:{issue['order']}", texts.t(f"order.cod_{kind}", order=issue["order"], days=cod_paid.warn_days()))
    _write(path, d)
    return out
