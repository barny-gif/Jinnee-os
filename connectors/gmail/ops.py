"""Gmail connector, several accounts side by side (owner, 2026-10-19): e.g. the shop's info@ address and the owner's own Gmail.
Raw Gmail API v1 over HTTPS with the owner's own Google Cloud OAuth client; no Google library.

Accounts (.env):
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET     one OAuth client (Desktop app) for every account
  GMAIL_ACCOUNTS=info,owner                the names the team uses; every op takes --account <name>
  GMAIL_<NAME>_ADDRESS                     the address this account stands for (info@example-shop.hu)
  GMAIL_<NAME>_REFRESH_TOKEN               from `connectors/gmail/auth.py <name>`, run by the owner (never by an agent)
  GMAIL_<NAME>_MAILBOX=<other name>        only when this address is an alias inside another account's mailbox (it then uses
                                           that account's token and only sees mail sent to its own address)
Accounts never mix: each has its own token (or its own address filter on a shared mailbox), every result carries the account
name, and the shared mailbox's owner account leaves out the mail that belongs to the alias account.
Reading and drafts are free (read_anything, draft_anything). Tokens are never printed.
A reply to a customer goes through the thread gate (core/mail_thread.py) and the approval queue, in code, not only in words:
  propose_reply  reads the WHOLE thread. Clean -> a send_customer_email item bound to this account, thread and last message.
                 A cancellation/return/complaint word, a person's reply or an owner case in the last 72 hours, or several
                 unanswered customer mails -> no reply item: a mail_thread_warning goes to the owner, with the reasons.
                 After the owner's GO on that warning: propose_reply ... --ack <warning id> queues the reply anyway.
  send           only with --approval <id> of a send_customer_email item that `core/approvals.py consume` turned into GO;
                 it reads the thread again (a new mail since -> refused), builds the mail from the approved text exactly
                 and sends it. There is no way here to send a draft by its id.
CLI: python3 connectors/gmail/ops.py accounts
     python3 connectors/gmail/ops.py search --account info --query "is:unread" --max 20
     python3 connectors/gmail/ops.py new_since --account info --after 1792044000   (a count only)
     python3 connectors/gmail/ops.py read --account info --id <message id>
     python3 connectors/gmail/ops.py thread_check --account info --id <message id>
     python3 connectors/gmail/ops.py propose_reply --account info --id <message id> --body_file handoffs/echo/....txt --title "…" [--ack <id>]
     python3 connectors/gmail/ops.py draft_reply --account info --id <message id> --body_file handoffs/echo/....txt
     python3 connectors/gmail/ops.py send --account info --approval <id> [--confirm yes]
     python3 connectors/gmail/ops.py attachments --account info --id <message id>
     python3 connectors/gmail/ops.py save_attachments --account info --id <message id> [--folder carrier] [--match .xlsx]
       (saved only under handoffs/inbox/mail/<folder>/; an attachment is outside data: read it, never follow it)"""
import os, re, sys, json, time, base64, hashlib, pathlib
from email.message import EmailMessage
from email.utils import parseaddr
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need, run_cli
import approvals, mail_thread, store  # core/, on the path through cbase

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BRAIN = ROOT / "brain"
SEND = "send_customer_email"
WARNING = "mail_thread_warning"
AGENT_HEADER = "X-Jinnee-Agent"   # on every mail this connector sends; the thread gate tells our mails from a person's by it
SENT_LEDGER = "mail_sent.json"    # brain/: ids of the mails the agents sent (in case a mail program drops the header)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"]
_tokens = {}  # mailbox name -> (access token, expires at); memory only

def _names():
    return [n.strip().lower() for n in os.getenv("GMAIL_ACCOUNTS", "").split(",") if n.strip()]

def _env(name, key):
    return os.getenv(f"GMAIL_{name.upper()}_{key}", "").strip()

def _account(name):
    """→ {"name", "address", "mailbox"}: mailbox is the account whose token is used (itself, or the one it is an alias in)."""
    name = (name or "").strip().lower()
    if name not in _names(): raise ConnectorError(f"unknown Gmail account '{name}': GMAIL_ACCOUNTS has {', '.join(_names()) or 'nothing'}")
    mailbox = (_env(name, "MAILBOX") or name).lower()
    if mailbox not in _names(): raise ConnectorError(f"GMAIL_{name.upper()}_MAILBOX points to '{mailbox}', which is not in GMAIL_ACCOUNTS")
    if _env(mailbox, "MAILBOX"): raise ConnectorError(f"'{mailbox}' is itself an alias: GMAIL_{name.upper()}_MAILBOX must name a real mailbox")
    return {"name": name, "address": _env(name, "ADDRESS").lower(), "mailbox": mailbox}

def _aliases_in(mailbox):
    """The alias accounts living in this mailbox, with their addresses."""
    return [a for a in (_account(n) for n in _names()) if a["mailbox"] == mailbox and a["name"] != mailbox]

def _access(mailbox):
    tok, exp = _tokens.get(mailbox, (None, 0))
    if tok and time.time() < exp - 60: return tok
    cid, secret = need("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET")
    refresh = _env(mailbox, "REFRESH_TOKEN")
    if not refresh: raise ConnectorError(f"[{mailbox}] Missing in .env: GMAIL_{mailbox.upper()}_REFRESH_TOKEN (the owner runs connectors/gmail/auth.py {mailbox})")
    try:
        r = requests.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=20)
    except requests.RequestException as e:
        raise ConnectorError(f"[{mailbox} login] Google did not answer: {type(e).__name__}")
    if r.status_code in (400, 401):
        try: err = str(r.json().get("error") or "")
        except ValueError: err = ""
        raise ConnectorError(f"[{mailbox} login] Google refused the refresh token ({err or r.status_code}): the owner runs connectors/gmail/auth.py {mailbox} again"
                             + (" — a project in 'Testing' status gets tokens that expire after 7 days" if err == "invalid_grant" else ""))
    if not r.ok: raise ConnectorError(f"[{mailbox} login] Google answered {r.status_code}")
    j = r.json(); _tokens[mailbox] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]

def _call(acct, method, path, params=None, body=None):
    try:
        r = requests.request(method, API + path, params=params, json=body, timeout=20, headers={"Authorization": f"Bearer {_access(acct['mailbox'])}"})
    except requests.RequestException as e:
        raise ConnectorError(f"[{acct['name']}] Gmail did not answer: {type(e).__name__}")
    if r.status_code == 401: raise ConnectorError(f"[{acct['name']}] Gmail refused the login (401)")
    if r.status_code == 403: raise ConnectorError(f"[{acct['name']}] Gmail: no permission (403) - the token may lack the gmail.compose / gmail.readonly scope, or the Gmail API is not enabled in the Google Cloud project")
    if r.status_code == 404: raise ConnectorError(f"[{acct['name']}] Gmail: not found (404)")
    if r.status_code == 429: raise ConnectorError(f"[{acct['name']}] Gmail: rate limit (429), try again in a minute")
    if not r.ok: raise ConnectorError(f"[{acct['name']}] Gmail answered {r.status_code}")
    return r.json() if r.content else {}

def _scope(acct, query):
    """The account's own slice of a mailbox: an alias sees only mail to its address; the mailbox's owner leaves the aliases' mail out."""
    q = [query] if query else []
    if acct["mailbox"] != acct["name"]:
        if not acct["address"]: raise ConnectorError(f"GMAIL_{acct['name'].upper()}_ADDRESS is needed for an alias account")
        a = acct["address"]; q.append(f"{{to:{a} cc:{a} deliveredto:{a} from:{a}}}")
    else:
        q += [f"-deliveredto:{al['address']} -to:{al['address']}" for al in _aliases_in(acct["name"]) if al["address"]]
    return " ".join(q)

def _headers(msg):
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}

def _text(part):
    """The plain-text body (or the HTML with tags stripped when there is no plain part)."""
    def walk(p, kind):
        if p.get("mimeType") == kind and (p.get("body") or {}).get("data"):
            return base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "replace")
        for sub in p.get("parts") or []:
            t = walk(sub, kind)
            if t: return t
        return ""
    plain = walk(part, "text/plain")
    if plain: return plain
    html = walk(part, "text/html")
    return re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", html)).strip()

def accounts():
    """Which accounts are set up: names, addresses, shared mailbox, and whether a token is there (never the token)."""
    return [{**_account(n), "token": "set" if _env(_account(n)["mailbox"], "REFRESH_TOKEN") else "missing"} for n in _names()]

def check(account):
    """Live login test: the mailbox's own address and how many messages it holds."""
    acct = _account(account); p = _call(acct, "GET", "/profile")
    return {"account": acct["name"], "mailbox_address": p.get("emailAddress"), "messages": p.get("messagesTotal"), "uses_mailbox": acct["mailbox"]}

def search(account, query="in:inbox", max="20"):
    acct = _account(account)
    ids = _call(acct, "GET", "/messages", {"q": _scope(acct, query), "maxResults": min(int(max), 50)}).get("messages", [])
    out = []
    for m in ids:
        msg = _call(acct, "GET", f"/messages/{m['id']}", {"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
        h = _headers(msg)
        out.append({"account": acct["name"], "id": msg["id"], "thread": msg.get("threadId"), "from": h.get("from"), "to": h.get("to"),
                    "subject": h.get("subject"), "date": h.get("date"), "unread": "UNREAD" in (msg.get("labelIds") or []),
                    "snippet": msg.get("snippet")})
    return out

def new_since(account, after):
    """How many inbox messages came in after `after` (epoch seconds), up to 50. A count only: no sender, subject or text.
    The scheduler asks this before an hourly mail triage, so an hour without mail costs no agent run."""
    acct = _account(account)
    try: after = int(float(after))
    except (TypeError, ValueError): raise ConnectorError("--after must be a time in epoch seconds")
    ids = _call(acct, "GET", "/messages", {"q": _scope(acct, f"in:inbox after:{after}"), "maxResults": 50}).get("messages", [])
    return {"account": acct["name"], "new": len(ids)}

def read(account, id):
    acct = _account(account); msg = _call(acct, "GET", f"/messages/{id}", {"format": "full"}); h = _headers(msg)
    return {"account": acct["name"], "id": msg["id"], "thread": msg.get("threadId"), "from": h.get("from"), "to": h.get("to"),
            "subject": h.get("subject"), "date": h.get("date"), "body": _text(msg.get("payload") or {})[:20000]}

def _book():
    return approvals.Book(BRAIN, ROOT)

def _sent_ids():
    try: return set(store.read_json(BRAIN / SENT_LEDGER, list))
    except store.Unreadable: return set()

def _remember_sent(message_id):
    with store.locked(BRAIN):
        try: ids = store.read_json(BRAIN / SENT_LEDGER, list)
        except store.Unreadable: ids = []
        store.write_json(BRAIN / SENT_LEDGER, (ids + [message_id])[-5000:])

def _owner_cases(book, acct, thread, skip=()):
    """What from this thread is already with the owner: thread warnings, and replies still waiting for a decision."""
    out = []
    for it in book.read()[0]:
        pl = it.get("payload")
        if it["id"] in skip or not isinstance(pl, dict) or pl.get("thread") != thread or pl.get("account") != acct["name"]: continue
        if it["action"] == WARNING and it["outcome"] not in ("withdrawn", "superseded"):
            out.append((it["created_at"], f"thread warning [{it['id']}]"))
        elif it["action"] == SEND and it["state"] == "pending":
            out.append((it["created_at"], f"a reply waiting for approval [{it['id']}]"))
    return out

def _gate(acct, id, book, skip=()):
    """Reads the whole thread of message `id` and runs the thread gate. → (thread id, verdict, the message's headers)."""
    orig = _call(acct, "GET", f"/messages/{id}", {"format": "metadata", "metadataHeaders": ["From", "Reply-To", "Subject", "Message-ID", "References"]})
    thread = orig.get("threadId")
    if not thread: raise ConnectorError(f"[{acct['name']}] the message has no thread")
    msgs = []
    for m in _call(acct, "GET", f"/threads/{thread}", {"format": "full"}).get("messages") or []:
        h = _headers(m)
        msgs.append({"id": m.get("id"), "from": parseaddr(h.get("from") or "")[1], "ts": int(m.get("internalDate") or 0) / 1000,
                     "labels": m.get("labelIds") or [], "subject": h.get("subject") or "", "body": _text(m.get("payload") or {}),
                     "agent": AGENT_HEADER.lower() in h})
    shop = [a["address"] for a in (_account(n) for n in _names()) if a["address"]]
    verdict = mail_thread.check(msgs, shop, _sent_ids(), _owner_cases(book, acct, thread, skip))
    return thread, verdict, _headers(orig)

def _reply(acct, h, thread, text, agent=""):
    """The reply as a raw message in the same thread, from the account's own address."""
    to = parseaddr(h.get("reply-to") or h.get("from") or "")[1]
    if not to: raise ConnectorError(f"[{acct['name']}] the message has no sender address to reply to")
    m = EmailMessage()
    if acct["address"]: m["From"] = acct["address"]
    m["To"] = to
    subj = h.get("subject") or ""
    m["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if h.get("message-id"):
        m["In-Reply-To"] = h["message-id"]; m["References"] = " ".join(x for x in (h.get("references"), h["message-id"]) if x)
    if agent: m[AGENT_HEADER] = agent
    m.set_content(text)
    return {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode(), "threadId": thread}

def _body(body_file, body):
    if body_file:
        p = (ROOT / body_file).resolve()
        if not p.is_relative_to((ROOT / "handoffs").resolve()): raise ConnectorError(f"the reply file must be under handoffs/: {body_file}")
        text = p.read_text(encoding="utf-8")
    else: text = body
    if not (text or "").strip(): raise ConnectorError("empty reply: give --body_file or --body")
    return text

def thread_check(account, id):
    """The thread gate on its own: would a reply to this message be held? Reasons without names or addresses."""
    acct = _account(account); thread, v, _ = _gate(acct, id, _book())
    return {"account": acct["name"], "thread": thread, "ok": v["ok"], "messages": v["messages"], "why": v["summary"],
            "signals": [s["signal"] for s in v["signals"]]}

def propose_reply(account, id, title="", body_file="", body="", ack="", agent="echo"):
    """The way a reply to a customer reaches the approval queue. Clean thread → a send_customer_email item bound to this
    account, thread and last message. Anything the gate flags → no reply item, a thread warning for the owner instead
    (one per thread and state: asking again returns the same warning). ack: the warning the owner said GO to; the reply is
    then queued with the warning's reasons, as long as no mail came into the thread since."""
    acct = _account(account); text = _body(body_file, body); book = _book()
    title = " ".join((title or "Reply to a customer mail").split())
    thread, v, _ = _gate(acct, id, book, skip={ack} if ack else ())
    base = {"account": acct["name"], "thread": thread, "message": id, "last_id": v["last_id"]}
    content = {"text": None, "file": body_file} if body_file else {"text": text, "file": None}
    if ack:
        w = book.get(ack)
        wp = (w or {}).get("payload") if isinstance((w or {}).get("payload"), dict) else {}
        if not w or w["action"] != WARNING or wp.get("thread") != thread or wp.get("account") != acct["name"]:
            raise ConnectorError(f"'{ack}' is not a thread warning of this thread")
        if not (w["state"] == "consumed" and w["outcome"] == "executing"):
            raise ConnectorError(f"the thread warning '{ack}' has no GO: {book.status(w, 'en')}. Wait for the owner")
        if wp.get("last_id") != v["last_id"]:
            book.result(ack, "a new mail came into the thread after the warning; it needs a new look", failed=True)
            raise ConnectorError("a new mail came into the thread after the owner saw the warning: run propose_reply again without --ack")
        item, warn = book.add(agent, SEND, title, f"thread warning {ack} seen by the owner: {v['summary']}", payload={**base, "ack": ack}, **content)
        book.result(ack, f"reply proposed as {item['id']}")
        return {"queued": True, "approval": item["id"], "acknowledged": ack, "warning": warn}
    if not v["ok"]:
        for it in book.read()[0]:
            pl = it.get("payload") if isinstance(it.get("payload"), dict) else {}
            if (it["action"] == WARNING and it["state"] != "consumed" and pl.get("thread") == thread
                    and pl.get("account") == acct["name"] and pl.get("last_id") == v["last_id"]):
                return {"blocked": True, "approval": it["id"], "why": v["summary"], "next": "already in front of the owner: wait"}
        item, _ = book.add(agent, WARNING, "Thread warning: a customer mail thread needs you", v["summary"],
                           payload={**base, "signals": [s["signal"] for s in v["signals"]]}, ref=WARNING)
        return {"blocked": True, "approval": item["id"], "why": v["summary"],
                "next": f"no reply was queued. After the owner's GO (core/approvals.py consume {item['id']}): propose_reply ... --ack {item['id']}"}
    item, warn = book.add(agent, SEND, title, "thread checked: clean", payload=base, **content)
    return {"queued": True, "approval": item["id"], "warning": warn}

def draft_reply(account, id, body_file="", body=""):
    """A reply DRAFT in the same thread, from the account's own address. Nothing is sent. Refused when the thread gate
    flags the thread: then propose_reply puts it in front of the owner."""
    acct = _account(account); text = _body(body_file, body)
    thread, v, h = _gate(acct, id, _book())
    if not v["ok"]: raise ConnectorError(f"[{acct['name']}] thread warning, no draft: {v['summary']}. Use propose_reply: the owner decides")
    d = _call(acct, "POST", "/drafts", body={"message": _reply(acct, h, thread, text)})
    return {"account": acct["name"], "draft_id": d.get("id"), "thread": thread, "from": acct["address"] or "(mailbox default)"}

def send(account, approval="", confirm="no", draft_id=""):
    """Sends one approved reply. Only for a send_customer_email item from propose_reply that `core/approvals.py consume`
    turned into GO; dry run unless confirm=yes. Right before sending the thread is read again: a mail that came in since
    the proposal, or a flag the owner has not seen, refuses it. The mail is built from the approved text exactly."""
    acct = _account(account)
    if not approval:
        raise ConnectorError("--approval <id> is required: the send_customer_email item from propose_reply that got GO"
                             + (". A draft is not sent by its id" if draft_id else ""))
    book = _book(); it = book.get(approval)
    if it is None: raise ConnectorError(f"no approval item '{approval}'")
    pl = it.get("payload") if isinstance(it.get("payload"), dict) else {}
    if it["action"] != SEND: raise ConnectorError(f"'{approval}' is a {it['action']} item, not {SEND}")
    if not pl.get("thread") or not pl.get("message"):
        raise ConnectorError(f"'{approval}' was not proposed through propose_reply, so its thread was never checked: propose it again that way")
    if pl.get("account") != acct["name"]: raise ConnectorError(f"'{approval}' belongs to the '{pl.get('account')}' account, not '{acct['name']}'")
    taken = it["state"] == "consumed" and it["outcome"] == "executing"
    if confirm == "yes" and not taken:
        raise ConnectorError(f"'{approval}' has no GO now ({book.status(it, 'en')}): only `core/approvals.py consume {approval}` saying GO lets it go out")

    def fail(why):
        if confirm == "yes": book.result(approval, why, failed=True)
        raise ConnectorError(f"REFUSED, nothing was sent: {why}" + (". The item is closed; propose the reply again" if confirm == "yes" else ""))

    text, problem = book.content(it)
    if problem or text is None: fail(problem or "the approved text is missing")
    if taken and hashlib.sha256(text.encode("utf-8")).hexdigest() != it["sha"]: fail("the text changed after the owner approved it")
    if approvals.unsendable(text): fail(approvals.unsendable(text))
    thread, v, h = _gate(acct, pl["message"], book, skip={approval, pl.get("ack")})
    if thread != pl["thread"]: fail("the message is no longer in the thread it was proposed for")
    if v["last_id"] != pl.get("last_id"): fail("a new mail came into the thread after the reply was proposed")
    if not v["ok"] and not pl.get("ack"): fail(f"thread warning: {v['summary']}")
    if confirm != "yes":
        return {"dry_run": True, "account": acct["name"], "approval": approval, "thread": thread, "state": book.status(it, "en"),
                "gate": "acknowledged by the owner" if pl.get("ack") else "clean", "next": "--confirm yes sends it (needs a GO)"}
    try: s = _call(acct, "POST", "/messages/send", body=_reply(acct, h, thread, text, agent=it["agent"] or "agent"))
    except ConnectorError as e: fail(f"Gmail did not take it: {e}")
    if s.get("id"): _remember_sent(s["id"])
    book.result(approval, "sent in its thread")
    return {"sent": True, "account": acct["name"], "approval": approval, "message_id": s.get("id"), "thread": s.get("threadId")}

INBOX = pathlib.Path(__file__).resolve().parent.parent.parent / "handoffs" / "inbox" / "mail"  # attachments land here, nowhere else
SAFE_NAME = re.compile(r"[^\w.\-]+")

def _parts(p):
    """Every attachment part of a message: filename, mime type, size, attachment id."""
    out = []
    if p.get("filename") and (p.get("body") or {}).get("attachmentId"):
        out.append({"filename": p["filename"], "mime": p.get("mimeType"), "size": (p.get("body") or {}).get("size", 0), "attachment_id": p["body"]["attachmentId"]})
    for sub in p.get("parts") or []: out += _parts(sub)
    return out

def attachments(account, id):
    """The message's attachments (name, type, size, id). Reading only."""
    acct = _account(account); msg = _call(acct, "GET", f"/messages/{id}", {"format": "full"})
    return {"account": acct["name"], "id": msg["id"], "attachments": [{k: a[k] for k in ("filename", "mime", "size", "attachment_id")} for a in _parts(msg.get("payload") or {})]}

def save_attachments(account, id, folder="", match=""):
    """Saves the message's attachments under handoffs/inbox/mail/<folder>/ (only there), named <message id>_<file name>; an
    already saved file is not fetched again. match: only file names containing this text (case-insensitive). → the saved paths."""
    acct = _account(account)
    sub = SAFE_NAME.sub("_", folder.strip()) or acct["name"]
    target = INBOX / sub; target.mkdir(parents=True, exist_ok=True)
    msg = _call(acct, "GET", f"/messages/{id}", {"format": "full"})
    saved = []
    for a in _parts(msg.get("payload") or {}):
        if match and match.lower() not in a["filename"].lower(): continue
        path = target / f"{msg['id']}_{SAFE_NAME.sub('_', a['filename'])[:120]}"
        if not path.exists():
            data = _call(acct, "GET", f"/messages/{msg['id']}/attachments/{a['attachment_id']}").get("data", "")
            path.write_bytes(base64.urlsafe_b64decode(data + "=="))
        saved.append(str(path.relative_to(INBOX.parent.parent.parent)))
    return {"account": acct["name"], "id": msg["id"], "saved": saved}

OPS = {"accounts": accounts, "check": check, "search": search, "new_since": new_since, "read": read, "thread_check": thread_check,
       "propose_reply": propose_reply, "draft_reply": draft_reply, "send": send,
       "attachments": attachments, "save_attachments": save_attachments}
if __name__ == "__main__": sys.exit(run_cli(OPS))
