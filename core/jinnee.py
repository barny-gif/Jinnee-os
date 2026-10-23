"""
Entry point: the Telegram bridge. The owner's messages go to Claude Code with the lead persona; the owner's decisions
(/ok, /change, /drop, /undo) are recorded here in plain code, never by the agent; a check once a minute sends alerts and
hands decided items to the lead; another once a minute starts what core/scheduler.py says is due (the morning brief, the
agents' own rhythm), one job at a time and never while the owner's own run is going. /schedule shows it. A text file the owner sends
(.txt, .md, .csv, .html) is saved under handoffs/inbox/ and the lead is told where it is.
run_agent() is the hook for the full orchestration logic (sessions, intake, delegation).
"""
import os, re, sys, time, subprocess, pathlib, asyncio, logging, threading, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import env; env.load()  # before anything below reads the environment
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from pack_loader import agent_files, resolve
from heartbeat import beat, trouble
from registry_client import check as registry_check
import alerts, approvals, business, logs, scheduler, store

log = logging.getLogger("jinnee")
ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
NAME = os.getenv("JINNEE_NAME", "Jinnee")
LANG = business.language()  # JINNEE_LANG in .env wins; otherwise business.language in config/business.yaml
PACKS = [p.strip() for p in os.getenv("PACKS", "general").split(",")]

def owner_id() -> int:
    """The one Telegram user the bridge obeys. 0 = not set or not a number; main() refuses to start then."""
    raw = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else 0

OWNER = owner_id()

def from_owner(update) -> bool:
    """Every handler starts with this. Anyone else is dropped without a reply, so the bot does not reveal that it is alive."""
    user = update.effective_user
    return bool(OWNER) and user is not None and user.id == OWNER

BOOK = approvals.Book(BRAIN)
WATCH = alerts.Watch(BRAIN)
# The agent may run these three programs and nothing else in the shell, and may not edit the files they guard.
TOOLS = [f"Bash({py} core/{prog}.py *)" for prog in ("approvals", "autonomy", "heartbeat") for py in ("python3", "python")]
# Lovable: reading is free. send_message/get_message let Forge build, which spends the owner's credits, so the
# agent files require the owner's yes on the exact prompt first. respond_to_approval stays with the owner.
TOOLS += [f"mcp__claude_ai_Lovable__{t}" for t in ("list_workspaces", "list_projects", "get_project", "list_files", "read_file", "list_edits", "get_diff", "send_message", "get_message")]
# Shopify through the owner's claude.ai connector: the shop's catalogue, stock and sales numbers only. Orders and customers
# (names, addresses, e-mails) are deliberately NOT here: they stay behind connectors/shopify, which shows order numbers only.
TOOLS += [f"mcp__claude_ai_Shopify__{t}" for t in ("get-shop-info", "search_products", "get-product", "get-inventory-levels", "run-analytics-query", "search_collections", "get-collection")]
# Forge's only way to git: this helper (own branch forge/*, tests must pass, pull request, never main).
TOOLS += [f"Bash({py} core/forge_git.py *)" for py in ("python3", "python")]
# Forge needs to read API docs and run the connectors and tests it writes; still no curl, rm or free shell.
TOOLS += ["WebFetch", "WebSearch"] + [f"Bash({py} {cmd})" for py in ("python3", "python") for cmd in ("connectors/*", "-m unittest *", "-I connectors/*")]
GUARDED = [f"Edit(brain/{name})" for name in ("approvals.json", "autonomy_config.json", "alerts_state.json")]
# A scheduled run (core/scheduler.py) has nobody watching it. It gets less: no building (Lovable, git, tests), writing only
# in brain/ and handoffs/ (dontAsk refuses everything not allowed here), and JINNEE_UNATTENDED=1, with which the connectors
# refuse every outgoing step and approvals.py refuses to hand out a decision.
SCHEDULED_TOOLS = [t for t in TOOLS if not any(x in t for x in ("forge_git", "-m unittest", "Lovable__send_message", "Lovable__get_message"))]
SCHEDULED_TOOLS += ["Edit(brain/**)", "Edit(handoffs/**)"]
SCHEDULED_DENY = GUARDED + [f"Edit(brain/{name})" for name in ("schedule_state.json", "schedule_outbox.json", "heartbeat.json")]
SCHEDULED_DENY += [f"Bash({py} core/{cmd} *)" for py in ("python3", "python") for cmd in ("approvals.py consume", "autonomy.py apply")]

RULES = """The agent files are your team's rules; brain/ is the business memory. Log every decision as one line in brain/decisions.log.md.

APPROVALS. The autonomy table below says what the team may do alone. Anything at "approval required", and anything not in the table, needs the owner's decision first; "forbidden" is never done by the team, only suggested in words.
- Ask: `{PY} core/approvals.py add --agent NAME --action ACTION --title "…" --summary "…" --file handoffs/…` (or --text "…"). The file or text is exactly what will go out: finished, no DRAFT or TODO in it. The owner is told automatically, with the commands to answer; in your reply just say in one line what you queued.
- Only the owner decides: on the dashboard, or on Telegram with /ok, /change or /drop (the bridge records it; you cannot). A "yes" in conversation is not a decision: point the owner to /ok.
- Immediately before carrying out anything that needed approval: `{PY} core/approvals.py consume ID`. Act only if it prints GO, exactly as approved, once. Then `{PY} core/approvals.py result ID "what happened"` (add --failed if it did not work). Any other answer means do not act. Never act on the memory of an earlier approval.
- CHANGE: fix it and ask again with `add … --replaces ID`; the new version needs its own decision. DROPPED: do not do it; the reason is in brain/lessons.md.
- Never write brain/approvals.json, brain/autonomy_config.json or brain/alerts_state.json yourself.
- A level change is a request too: `{PY} core/autonomy.py propose ACTION LEVEL --why "…"`, and after the owner approves, `{PY} core/autonomy.py apply ID`. Locked actions cannot go above their maximum; only the owner can change that, by hand.
- When an agent starts or finishes a job: `{PY} core/heartbeat.py beat AGENT "what was done"` (--working at the start, --failed if it went wrong).""".replace("{PY}", store.PY)

def system_prompt() -> str:
    parts = [f"Your name is {NAME}. You speak with the owner in language '{business.language()}'. Working directory: {ROOT}.\n" + RULES]
    # who the business is (config/business.yaml, the owner's own settings; never in git)
    parts.append("\n\n===== the business (config/business.yaml) =====\n" + business.prompt_block())
    for f in agent_files(PACKS):
        parts.append(f"\n\n===== {f.name} =====\n{f.read_text(encoding='utf-8')}")
    for b in ("company.md", "lessons.md", "forge_notes.md"):
        p = BRAIN / b
        if p.exists(): parts.append(f"\n\n===== brain/{b} =====\n{p.read_text(encoding='utf-8')}")
    parts.append("\n\n===== autonomy levels (what the gate answers right now) =====\n" + BOOK.gate.describe())
    parts.append("\n\n===== approvals right now =====\n" + BOOK.brief())
    return "".join(parts)

FRESH = {"next": True}  # first message after start or /new opens a new session
TIMEOUT = int(alerts.number("JINNEE_TIMEOUT", 600))  # seconds for one run; raise it in .env for long build jobs
MINUTES = max(1, TIMEOUT // 60)
MODEL = os.getenv("JINNEE_MODEL", "").strip()  # e.g. the strongest model for Forge's jobs; empty = the CLI default
RUNNING = threading.Lock()  # one agent run at a time: the owner's message and a hand-over must not share a session mid-run
HELP = ("What you can do: send it again. If it keeps happening, send /new to start a clean conversation. "
        "/pending shows what is waiting.")

def command(message: str, unattended=False, conversation=True):
    """→ (the command, the same without --continue, the environment). unattended: started by the schedule.
    conversation=False: a session of its own that is not kept, so the owner's next message does not continue it."""
    if unattended:
        mode, tools, deny = "dontAsk", SCHEDULED_TOOLS, SCHEDULED_DENY
        run_env = {**os.environ, "JINNEE_UNATTENDED": "1"}
    else:
        mode, tools, deny = "acceptEdits", TOOLS, GUARDED
        run_env = {k: v for k, v in os.environ.items() if k != "JINNEE_UNATTENDED"}
    base = ["claude", "-p", message, "--permission-mode", mode, "--allowedTools", *tools, "--disallowedTools", *deny,
            "--append-system-prompt", system_prompt(), "--output-format", "text"] + (["--model", MODEL] if MODEL else [])
    if not conversation: return base + ["--no-session-persistence"], None, run_env
    return (base if FRESH["next"] else base[:2] + ["--continue"] + base[2:]), base, run_env

def call_claude(message: str, unattended=False, conversation=True):
    """→ (worked, text for the owner, short note for the heartbeat)."""
    cmd, base, run_env = command(message, unattended, conversation)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=TIMEOUT, env=run_env)
        if r.returncode != 0 and "--continue" in cmd:
            r = subprocess.run(base, capture_output=True, text=True, cwd=ROOT, timeout=TIMEOUT, env=run_env)
    except FileNotFoundError:
        return False, ("I could not start: the `claude` program is not installed on this machine, or not found. Nothing was done. "
                       "What you can do: install Claude Code and run `claude login` on the machine, then send your message again."), "claude not found"
    except subprocess.TimeoutExpired:
        return False, (f"I worked on this for {MINUTES} minutes without finishing, so I stopped. Some steps may be done and others not; "
                       "an approval that was already taken will not be used a second time. "
                       "What you can do: ask for a smaller step, or send /pending to see what is open."), f"timed out after {MINUTES} minutes"
    if r.returncode != 0:
        detail = " ".join((r.stderr.strip() or r.stdout.strip()).split())[-300:]
        return False, ("I could not finish this: the Claude program stopped with an error, so nothing more was done. " + HELP +
                       " If the error below mentions signing in, run `claude login` on the machine." +
                       (f"\n\nError: {detail}" if detail else "")), f"stopped with an error (exit {r.returncode})"
    if conversation: FRESH["next"] = False
    return True, r.stdout.strip() or r.stderr.strip() or "…", "replied"

def run_agent(message: str) -> str:
    """Claude Code CLI, non-interactive. Runs on the owner's Pro/Max subscription.
    --continue keeps one running conversation; acceptEdits lets agents write brain/ and handoffs/.
    A run that fails or times out comes back as a message the owner can act on, and the heartbeat records it."""
    with RUNNING:
        beat("jinnee", "working", "working")
        ok, out, note = call_claude(message)
        beat("jinnee", note, "ok" if ok else "failed")
    if not ok: log.warning("agent run failed: %s", note)
    return out

async def say(send, text, markup=None):
    for i in range(0, len(text), 4000):
        last = i + 4000 >= len(text)
        await send(text[i:i+4000], **({"reply_markup": markup} if markup is not None and last else {}))

# --- the owner's decisions, recorded without the agent in between
COMMAND = re.compile(r"/(ok|change|drop|undo|pending|schedule)(?:@\w+)?(?:[_ ]+(\S+))?(?:\s+(.*))?", re.S)
DECISION = {"ok": "ok", "change": "edit", "drop": "drop"}
PLAIN_OK = {"ok", "oké", "oke", "go", "mehet"}  # said alone, they approve the one waiting item; the /undo window still applies
AWAITING = {}  # {"id", "decision", "until"} while the bridge waits for the owner's words after a bare /change or /drop
AWAIT_SECONDS = 600  # after that the next message is an ordinary message again

def decide(item_id, decision, note):
    it = BOOK.decide(item_id, decision, note, by="telegram")
    what = alerts.line(BOOK, it)
    if decision == "ok":
        if not it["sendable"]:
            return f"Approved, but it cannot go out as it is: {it['blocked']}.\n{what}\n{NAME} will fix it and ask you again."
        minutes = max(1, round(alerts.number("UNDO_SECONDS", 120) / 60) + 1)
        return f"Approved: {what}\n{NAME} picks it up within about {minutes} minutes. Until then: /undo_{item_id}"
    if decision == "edit": return f"Sent back with your note: {what}\nThe corrected version will come to you again."
    return f"Dropped: {what}\nYour reason is saved for the team. /undo_{item_id} takes it back."

def owner_command(text: str) -> str:
    """/ok k7qd · /ok_k7qd · /change k7qd shorter · /drop k7qd why · /undo k7qd · /pending · /schedule.
    The id may be left out when only one item fits."""
    m = COMMAND.fullmatch(text.strip())
    if not m: return "Commands: /pending · /ok · /change · /drop · /undo · /schedule · /new"
    word, first, rest = m.group(1), m.group(2) or "", (m.group(3) or "").strip()
    if word == "schedule": return SCHED.summary()
    pending, decided, _ = BOOK.open_items()
    if word == "pending":
        rows = [f"• {alerts.line(BOOK, i)}: {BOOK.status(i)}" for i in pending + decided]
        return "\n".join(rows) + "\n\n/ok <id> · /change <id> what to change · /drop <id> why · /undo <id>" if rows else "Nothing is waiting."
    fits = decided if word == "undo" else pending
    known = {i["id"] for i in BOOK.read()[0]}
    if first in known: target, note = first, rest
    elif first and word in ("ok", "undo"): return f"There is no item '{first}'. /pending shows what is waiting."
    elif len(fits) == 1: target, note = fits[0]["id"], " ".join(x for x in (first, rest) if x)
    elif not fits: return "Nothing to undo." if word == "undo" else "Nothing is waiting for a decision."
    else: return "Which one?\n" + "\n".join(f"• {alerts.line(BOOK, i)}" for i in fits) + f"\n\nSend /{word} <id>" + ("" if word in ("ok", "undo") else " <your words>")
    try:
        if word == "undo":
            return f"Undone. It is waiting for your decision again: {alerts.line(BOOK, BOOK.undo(target, by='telegram'))}"
        if word != "ok" and not note:
            it = BOOK.get(target)
            if it["state"] != "pending": raise approvals.Conflict(f"'{target}' is already {BOOK.status(it)}")
            AWAITING.update(id=target, decision=DECISION[word], until=time.time() + AWAIT_SECONDS)
            ask = "What should change?" if word == "change" else "Why are you dropping it? One sentence; the team learns from it."
            return f"{alerts.line(BOOK, it)}\n{ask} Send it as your next message."
        return decide(target, DECISION[word], note)
    except approvals.Refused as e:
        return str(e)[:1].upper() + str(e)[1:]

async def on_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not from_owner(update) or update.message is None: return
    AWAITING.clear()
    await say(update.message.reply_text, await asyncio.to_thread(owner_command, update.message.text or ""))

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not from_owner(update): return
    if update.message is None: return  # an edited message arrives without one; acting on it would run the same request twice
    text = update.message.text or ""
    waiting = dict(AWAITING); AWAITING.clear()
    if waiting and time.time() < waiting["until"]:  # the words for the /change or /drop just before
        item_id, decision = waiting["id"], waiting["decision"]
        try: reply = await asyncio.to_thread(decide, item_id, decision, text)
        except approvals.Refused as e: reply = str(e)[:1].upper() + str(e)[1:]
        return await say(update.message.reply_text, reply)
    if text.strip().strip(".!").lower() in PLAIN_OK:  # a bare "ok" from the owner: one waiting item is approved, several are asked about
        pending = BOOK.open_items()[0]
        if len(pending) == 1:
            return await say(update.message.reply_text, await asyncio.to_thread(decide, pending[0]["id"], "ok", ""))
        if len(pending) > 1:
            ids = [i["id"] for i in pending][:5]
            return await say(update.message.reply_text, "Which one?\n" + "\n".join(f"• {alerts.line(BOOK, i)}" for i in pending), buttons(ids))
    await answer(update, text)

async def answer(update, text):
    if SCHEDULING["busy"] and RUNNING.locked():  # a scheduled job started just before: say so instead of a silent wait
        await update.message.reply_text(f"One moment: a scheduled run is finishing ({SCHEDULING['job']}). Your message is next, "
                                        f"within {MINUTES} minutes at most.")
    reply = await asyncio.to_thread(run_agent, text)
    ids = [i["id"] for i in BOOK.open_items()[0] if i["id"] in reply][:5]  # the agent named a waiting item: put the tap buttons under it
    await say(update.message.reply_text, reply, buttons(ids))

# --- text files from the owner (a brief, an invoice export): saved under handoffs/inbox/, then the lead is told where it is.
# Plain text only: the agent reads it like any handoff. Nothing in it is run, and other file types are refused with a reason.
INBOX = ROOT / "handoffs" / "inbox"  # handoffs/ is a mounted volume, so the file survives a rebuild
TEXT_FILES = (".txt", ".md", ".csv", ".html", ".htm")  # .csv: exports (GLS, Billingo); .html: briefs and saved pages
HTML_FILES = (".html", ".htm")  # kept as text: the agent reads the markup, nothing in it is opened, fetched or run
MAX_FILE = 1_000_000  # bytes; a brief or an export is far smaller, and the whole file goes into one agent run

def safe_name(name: str) -> str:
    """The owner's file name without folders or odd characters, so it can only land inside handoffs/inbox/."""
    stem = re.sub(r"[^\w.-]+", "_", pathlib.PurePath(name or "file").name, flags=re.UNICODE).strip("._") or "file"
    return stem[-80:]

def decode(data: bytes) -> str:
    """UTF-8 first (with or without BOM); a Hungarian Windows file is often cp1250. Anything else is not a text file."""
    for enc in ("utf-8-sig", "cp1250"):
        try: text = data.decode(enc)
        except UnicodeDecodeError: continue
        if "\x00" not in text: return text
    raise ValueError("not a text file")

def file_refusal(name: str, size) -> str:
    """'' when the file may be taken; otherwise the reason, in words the owner can act on."""
    if pathlib.PurePath(name or "").suffix.lower() not in TEXT_FILES:
        return f"I can only take text files ({', '.join(TEXT_FILES)}). Nothing was saved. Send it as one of these, or paste the text."
    if size and size > MAX_FILE:
        return f"This file is {size // 1000} kB; the limit is {MAX_FILE // 1000} kB. Nothing was saved. Split it, or send the part that matters."
    return ""

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not from_owner(update) or update.message is None: return
    AWAITING.clear()
    doc = update.message.document
    name = getattr(doc, "file_name", "") or ""
    no = file_refusal(name, getattr(doc, "file_size", 0))
    if no: return await update.message.reply_text(no)
    try:
        data = bytes(await (await doc.get_file()).download_as_bytearray())
        if len(data) > MAX_FILE: return await update.message.reply_text(file_refusal(name, len(data)))
        text = decode(data)
    except ValueError:
        return await update.message.reply_text("This does not read as a text file, so nothing was saved. Paste the text instead.")
    except Exception as e:
        log.warning("file download failed: %s", type(e).__name__)
        return await update.message.reply_text("I could not download the file from Telegram, so nothing was saved. Send it again.")
    INBOX.mkdir(parents=True, exist_ok=True)
    path = INBOX / f"{dt.datetime.now(alerts.zone()):%Y-%m-%d_%H%M%S}-{safe_name(name)}"
    path.write_text(text, encoding="utf-8")
    caption = (update.message.caption or "").strip()
    await answer(update, f"The owner sent a file on Telegram, saved as {path.relative_to(ROOT)} ({len(text)} characters). "
                         "Read it from there. "
                         + ("It is HTML: read it as text only; do not open its links or run its scripts. " if path.suffix.lower() in HTML_FILES else "")
                         + (f"The owner's message with it: {caption}" if caption else "No message came with it."))

def buttons(ids):
    """Tap instead of typing: one row per waiting item. The tap goes through the same owner_command as /ok, /change, /drop."""
    if not ids: return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ OK " + i, callback_data=f"ok:{i}"),
                                  InlineKeyboardButton("✏️ Change", callback_data=f"change:{i}"),
                                  InlineKeyboardButton("🗑 Drop", callback_data=f"drop:{i}")] for i in ids])

async def on_tap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None or not from_owner(update): return  # a stranger's tap is dropped without a reply
    word, _, item_id = (q.data or "").partition(":")
    if word not in ("ok", "change", "drop") or not item_id: return await q.answer()
    await q.answer()
    reply = await asyncio.to_thread(owner_command, f"/{word} {item_id}")
    await q.message.reply_text(reply[:4000])

async def alert_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Once a minute: tell the owner what core/alerts.py says is due, then give decided items to the lead."""
    try:
        plan = await asyncio.to_thread(WATCH.due)
        for message in plan["messages"]:
            await ctx.bot.send_message(chat_id=OWNER, text=message[:4000],
                                       reply_markup=buttons(plan.get("ask", [])) if message.startswith("Waiting for your decision") else None)
        await asyncio.to_thread(WATCH.done, plan)  # only now: a message that could not be sent is tried again
    except Exception:
        log.exception("alert check failed; it runs again in a minute")
        return
    todo = []
    for item in plan["hand_over"]:
        if item["decision"] == "drop": await asyncio.to_thread(BOOK.consume, item["id"])  # nothing to carry out: closed here, no agent run
        else: todo.append(item)
    if todo:  # as its own task: a run can take minutes, and the next check should not wait for it
        ctx.application.create_task(hand_over(ctx.bot, todo))

async def hand_over(bot, items):
    reply = await asyncio.to_thread(run_agent, alerts.hand_over_message(BOOK, items))
    await say(lambda t: bot.send_message(chat_id=OWNER, text=t), reply)

# --- the schedule (core/scheduler.py decides; this runs it)
SCHED = scheduler.Scheduler(BRAIN, PACKS)
SCHEDULING = {"busy": False, "job": ""}  # one scheduled job at a time from this program

def brief_extra() -> str:
    """What the morning brief is told on top of its task: agents in trouble (with the schedule's rhythm) and updates."""
    extra = ""
    stuck = trouble(BRAIN, expected=SCHED.expected())
    if stuck: extra += f"\nAgents in trouble: {'; '.join(stuck)}."
    ups = registry_check()
    if ups: extra += "\nUpdates available: " + "; ".join(f"{u['key']} → {u['to']}" for u in ups) + " Ask the owner whether to update."
    return extra

SCHED.extra["jinnee.brief"] = brief_extra

class Runner:
    """The scheduler's way into Claude: the same prompt, the narrower tools, and no start while the owner's run is going."""
    def busy(self):
        return RUNNING.locked()  # the owner's conversation (or a hand-over) is running: the job waits for the next minute
    def run(self, job, prompt):
        with RUNNING:
            return call_claude(prompt, unattended=True, conversation=job.lead)  # the brief stays in the owner's conversation

RUNNER = Runner()

async def schedule_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Once a minute: deliver what finished during the quiet hours, then start the earliest due job, one at a time."""
    try:
        for text in await asyncio.to_thread(SCHED.outbox):
            await say(lambda t: ctx.bot.send_message(chat_id=OWNER, text=t), text)
        if SCHEDULING["busy"]: return
        due = await asyncio.to_thread(SCHED.due)
    except Exception:
        log.exception("schedule check failed; it runs again in a minute")
        return
    if not due: return
    SCHEDULING.update(busy=True, job=due[0][0].name)
    ctx.application.create_task(run_job(ctx.bot, *due[0]))

async def run_job(bot, job, t, key):
    try:
        out = await asyncio.to_thread(SCHED.run, job, t, key, RUNNER)
        if out.result in ("failed", "ok", "silent"): log.info("scheduled %s: %s (%s)", job.id, out.result, out.note)
        if out.message:
            if alerts.quiet(time.time()): await asyncio.to_thread(SCHED.later, out.message)  # finished after the quiet hours began
            else: await say(lambda t: bot.send_message(chat_id=OWNER, text=t), out.message)
    except Exception:
        log.exception("scheduled job %s could not be finished", job.id)
    finally:
        SCHEDULING["busy"] = False

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not from_owner(update) or update.message is None: return
    FRESH["next"] = True; AWAITING.clear()
    await update.message.reply_text("New day, clean slate. What are we doing?")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather on Telegram, "
                         "put its token in .env and start again.")
    if not OWNER:
        raise SystemExit("TELEGRAM_OWNER_ID is missing or not a number, so the Telegram bridge will not start: "
                         "without it anyone who finds the bot could run the agent on this machine. "
                         "Message @userinfobot on Telegram, put the number it replies with in .env "
                         "(TELEGRAM_OWNER_ID=123456789) and start again.")
    logs.setup("jinnee")
    BOOK.gate.migrate()  # a file from before locks existed keeps its levels
    BOOK.tidy()          # items from before the lifecycle: decided ones are closed, never carried out twice
    for jid in SCHED.recover(): log.warning("scheduled %s was cut off by the restart; recorded as failed", jid)
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CallbackQueryHandler(on_tap))
    app.add_handler(MessageHandler(filters.COMMAND, on_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    if app.job_queue:  # both go to OWNER, which is always set here
        app.job_queue.run_repeating(alert_tick, interval=60, first=20)
        app.job_queue.run_repeating(schedule_tick, interval=60, first=40)
    else:
        print("The Telegram job queue is missing (pip install 'python-telegram-bot[job-queue]'): no alerts and no schedule.")
    on = [j.id for j in SCHED.jobs() if not j.off]
    print(f"{NAME} is running. Packs: {', '.join(PACKS)}. Scheduled: {', '.join(on) or 'nothing'} (python3 core/scheduler.py plan)")
    app.run_polling()

if __name__ == "__main__":
    main()
