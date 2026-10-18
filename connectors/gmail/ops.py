"""Gmail connector, several accounts side by side (owner, 2026-10-19): the shop's info@montivilag.hu and the owner's own Gmail.
Raw Gmail API v1 over HTTPS with the owner's own Google Cloud OAuth client; no Google library.

Accounts (.env):
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET     one OAuth client (Desktop app) for every account
  GMAIL_ACCOUNTS=info,barni                the names the team uses; every op takes --account <name>
  GMAIL_<NAME>_ADDRESS                     the address this account stands for (info@montivilag.hu)
  GMAIL_<NAME>_REFRESH_TOKEN               from `connectors/gmail/auth.py <name>`, run by the owner (never by an agent)
  GMAIL_<NAME>_MAILBOX=<other name>        only when this address is an alias inside another account's mailbox (it then uses
                                           that account's token and only sees mail sent to its own address)
Accounts never mix: each has its own token (or its own address filter on a shared mailbox), every result carries the account
name, and the shared mailbox's owner account leaves out the mail that belongs to the alias account.
Reading and drafts are free (read_anything, draft_anything). Sending a draft is `send` with --confirm yes, and only after a GO
from the approval queue (send_customer_email). Tokens are never printed.
CLI: python3 connectors/gmail/ops.py accounts
     python3 connectors/gmail/ops.py search --account info --query "is:unread" --max 20
     python3 connectors/gmail/ops.py read --account info --id <message id>
     python3 connectors/gmail/ops.py draft_reply --account info --id <message id> --body_file handoffs/echo/....txt
     python3 connectors/gmail/ops.py send --account info --draft_id <id> [--confirm yes]"""
import os, re, sys, json, time, base64, pathlib
from email.message import EmailMessage
from email.utils import parseaddr
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need, run_cli

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

def read(account, id):
    acct = _account(account); msg = _call(acct, "GET", f"/messages/{id}", {"format": "full"}); h = _headers(msg)
    return {"account": acct["name"], "id": msg["id"], "thread": msg.get("threadId"), "from": h.get("from"), "to": h.get("to"),
            "subject": h.get("subject"), "date": h.get("date"), "body": _text(msg.get("payload") or {})[:20000]}

def draft_reply(account, id, body_file="", body=""):
    """A reply DRAFT in the same thread, from the account's own address. Nothing is sent."""
    acct = _account(account)
    text = pathlib.Path(body_file).read_text(encoding="utf-8") if body_file else body
    if not text.strip(): raise ConnectorError("empty reply: give --body_file or --body")
    orig = _call(acct, "GET", f"/messages/{id}", {"format": "metadata", "metadataHeaders": ["From", "Reply-To", "Subject", "Message-ID", "References"]})
    h = _headers(orig)
    to = parseaddr(h.get("reply-to") or h.get("from") or "")[1]
    if not to: raise ConnectorError(f"[{acct['name']}] the message has no sender address to reply to")
    m = EmailMessage()
    if acct["address"]: m["From"] = acct["address"]
    m["To"] = to
    subj = h.get("subject") or ""
    m["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if h.get("message-id"):
        m["In-Reply-To"] = h["message-id"]; m["References"] = " ".join(x for x in (h.get("references"), h["message-id"]) if x)
    m.set_content(text)
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
    d = _call(acct, "POST", "/drafts", body={"message": {"raw": raw, "threadId": orig.get("threadId")}})
    return {"account": acct["name"], "draft_id": d.get("id"), "thread": orig.get("threadId"), "from": acct["address"] or "(mailbox default)"}

def send(account, draft_id, confirm="no"):
    """Sends a draft. Dry run unless confirm=yes; run it only for an item that got GO from `core/approvals.py consume`."""
    acct = _account(account)
    d = _call(acct, "GET", f"/drafts/{draft_id}", {"format": "metadata"})
    h = _headers(d.get("message") or {})
    if confirm != "yes":
        return {"dry_run": True, "account": acct["name"], "draft_id": draft_id, "subject": h.get("subject"), "next": "--confirm yes sends it (needs a GO)"}
    s = _call(acct, "POST", "/drafts/send", body={"id": draft_id})
    return {"sent": True, "account": acct["name"], "message_id": s.get("id"), "thread": s.get("threadId")}

OPS = {"accounts": accounts, "check": check, "search": search, "read": read, "draft_reply": draft_reply, "send": send}
if __name__ == "__main__": sys.exit(run_cli(OPS))
