"""ONE-TIME LOGIN for a Gmail account. Run by the OWNER in his own terminal, never by an agent: it prints a refresh token,
which goes into .env by hand and nowhere else.

  docker compose exec jinnee python3 connectors/gmail/auth.py info

1. It prints a Google link. Open it in your browser and log in with the account (for info: the one that holds info@montivilag.hu).
2. Google sends the browser to http://127.0.0.1:8765/?code=... and the page "cannot be reached". That is expected:
   copy the WHOLE address from the browser's address bar and paste it here.
3. It prints GMAIL_<NAME>_REFRESH_TOKEN=... Put that line into /opt/jinnee-os/.env, then `docker compose up -d`.
Needs GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env (a "Desktop app" OAuth client in the owner's Google Cloud project, with the
Gmail API enabled). In "Testing" status Google lets the token expire after 7 days: publish the consent screen ("In production")."""
import sys, base64, hashlib, secrets, pathlib, urllib.parse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT = "http://127.0.0.1:8765/"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"]

def link(client_id, verifier, hint=""):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "code_challenge": challenge, "code_challenge_method": "S256"}
    if hint: q["login_hint"] = hint
    return AUTH_URL + "?" + urllib.parse.urlencode(q)

def code_from(pasted):
    """The code from the pasted address (or the bare code)."""
    pasted = pasted.strip()
    if "://" not in pasted and "=" not in pasted: return pasted  # the bare code
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if qs.get("error"): raise ConnectorError(f"Google said: {qs['error'][0]}")
    if not qs.get("code"): raise ConnectorError("no code in the pasted address: copy the whole address from the browser")
    return qs["code"][0]

def exchange(client_id, secret, code, verifier):
    r = requests.post(TOKEN_URL, data={"client_id": client_id, "client_secret": secret, "code": code, "code_verifier": verifier,
                                       "grant_type": "authorization_code", "redirect_uri": REDIRECT}, timeout=20)
    if not r.ok: raise ConnectorError(f"Google refused the code ({r.status_code}): start again, a code works only once and only for a few minutes")
    tok = r.json().get("refresh_token")
    if not tok: raise ConnectorError("Google gave no refresh token: remove the app's access at myaccount.google.com/permissions and start again")
    return tok

def main(argv):
    if len(argv) != 1: print(__doc__); return 2
    name = argv[0].strip().lower()
    cid, secret = need("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET")
    import os
    verifier = secrets.token_urlsafe(64)
    print("\n1) Open this link and log in with the account for '%s':\n\n%s\n" % (name, link(cid, verifier, os.getenv(f"GMAIL_{name.upper()}_ADDRESS", ""))))
    pasted = input("2) Paste the whole address from the browser (http://127.0.0.1:8765/?code=...): ")
    tok = exchange(cid, secret, code_from(pasted), verifier)
    print(f"\n3) Put this line into .env (and nowhere else), then: docker compose up -d\n\nGMAIL_{name.upper()}_REFRESH_TOKEN={tok}\n")
    return 0

if __name__ == "__main__":
    try: sys.exit(main(sys.argv[1:]))
    except ConnectorError as e: print(f"error: {e}"); sys.exit(1)
