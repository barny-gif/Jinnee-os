"""The thread gate: before any reply to a customer is proposed or sent, the WHOLE mail thread is read, not only the last mail.

Why (a shop owner's real case, 2026-11): a correct "where is my parcel" template reply nearly went to a customer who had
cancelled the order earlier in the same thread, in several mails, and whom a person from the shop had already answered.
The reply was true on its own and wrong in its place. So the reply is held, and the owner decides, when the thread shows:

  (a) a cancellation, return, refund or complaint word in a customer's mail (Hungarian and English, with or without
      accents; quoted earlier mails are left out so the shop's own words are not counted twice)
  (b) within the last 72 hours: a reply from the shop that the agents did not send (a person answered), or a case from
      this thread already put in front of the owner (a thread warning, or a reply still waiting for approval)
  (c) the customer wrote more than one mail since the shop's last reply

Any one of them -> no ready reply; a "thread warning" goes to the owner instead. The reasons name the signal, the message
(its place in the thread and its time) and the word found: never the customer's name, address or the mail's text.
Pure logic, no network: connectors/gmail/ops.py reads the thread and calls `check`."""
import re, time, unicodedata

WINDOW = 72 * 3600
# Word starts, compared after lower-casing and dropping accents. A false alarm costs the owner a tap; a miss can cost a customer.
WORDS = {
    "cancellation": ("lemond", "visszamond", "storn", "cancel"),
    "return/refund": ("visszakuld", "visszavisz", "visszautal", "visszaterit", "penzvissza", "elall", "return", "refund",
                      "money back", "chargeback"),
    "complaint": ("panasz", "reklamac", "reklamal", "fogyasztovedel", "complain", "dispute"),
}
ESCALATION = ("eszkal", "escalat")  # the shop's own side writing that a case went up
QUOTE_START = re.compile(r"^(?:-{2,}\s*(?:original message|eredeti uzenet|forwarded message|tovabbitott uzenet)"
                         r"|.*\b(?:wrote|irta)\s*(?:\([^)]*\))?\s*:\s*$|from:\s|felado:\s)")


def plain(text):
    """Lower case, accents dropped (ő -> o, á -> a), so 'Lemondás', 'lemondas' and 'LEMONDÁS' read the same."""
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def own_words(body):
    """The mail's own text: quoted lines ('> …') and everything from 'On … wrote:' / '… írta:' down are left out."""
    out = []
    for line in str(body or "").splitlines():
        p = plain(line).strip()
        if QUOTE_START.match(p): break
        if not p.startswith(">"): out.append(line)
    return "\n".join(out)


def find(text, stems):
    """The first stem that starts a word in the text, or ''."""
    t = plain(text)
    for s in stems:
        if re.search(r"(?<![a-z0-9])" + re.escape(s), t): return s
    return ""


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def check(messages, shop, agent_ids=(), owner_cases=(), now=None, window=WINDOW):
    """messages: [{"id", "from" (address), "ts" (epoch s), "labels", "subject", "body", "agent" (bool: one of ours)}].
    shop: the shop's own addresses. agent_ids: ids of mails the agents sent. owner_cases: [(ts, "what")] put in front of
    the owner from this thread. → {"ok", "signals": [{"signal", "message", "detail"}], "summary", "messages", "last_id"}."""
    now = time.time() if now is None else now
    shop = {a.strip().lower() for a in shop if a and a.strip()}
    msgs = sorted(messages, key=lambda m: (m.get("ts") or 0))
    n, signals = len(msgs), []

    def is_shop(m):
        return (m.get("from") or "").strip().lower() in shop or "SENT" in (m.get("labels") or [])

    def at(i, m, who):
        return f"message {i}/{n} ({who}, {when(m.get('ts'))})"

    last_shop = 0
    for i, m in enumerate(msgs, 1):
        recent = (m.get("ts") or 0) >= now - window
        if is_shop(m):
            last_shop = i
            ours = bool(m.get("agent")) or m.get("id") in set(agent_ids)
            if recent and not ours:
                signals.append({"signal": "human_reply", "message": i, "detail": f"a person from the shop already replied: {at(i, m, 'shop')}"})
            word = find(own_words(m.get("body")), ESCALATION)
            if recent and word:
                signals.append({"signal": "escalated", "message": i, "detail": f"the shop wrote that it was escalated ('{word}'): {at(i, m, 'shop')}"})
            continue
        text = f"{m.get('subject') or ''}\n{own_words(m.get('body'))}"
        for kind, stems in WORDS.items():
            word = find(text, stems)
            if word:
                signals.append({"signal": kind.split("/")[0], "message": i, "detail": f"{kind} word ('{word}'): {at(i, m, 'customer')}"})
    for ts, what in owner_cases:
        if (ts or 0) >= now - window:
            signals.append({"signal": "escalated", "message": None, "detail": f"already with the owner ({what}, {when(ts)})"})
    unanswered = [i for i in range(last_shop + 1, n + 1)]
    if len(unanswered) > 1:
        since = "the shop's last reply" if last_shop else "the start of the thread, with no reply"
        signals.append({"signal": "several_unanswered", "message": unanswered[-1],
                        "detail": f"the customer wrote {len(unanswered)} mails since {since} (messages {unanswered[0]}-{unanswered[-1]}/{n})"})
    summary = "; ".join(s["detail"] for s in signals)
    return {"ok": not signals and n > 0, "signals": signals, "summary": summary or ("the thread is empty" if not n else "clean"),
            "messages": n, "last_id": msgs[-1].get("id") if msgs else None}
