"""
Entry point: the Telegram bridge. The owner's messages go to Claude Code with the lead persona; the owner's decisions
(/ok, /change, /drop, /undo) are recorded here in plain code, never by the agent; a check once a minute sends alerts and
hands decided items to the lead; the morning brief runs daily.
run_agent() is the hook for the full orchestration logic (sessions, intake, delegation).
"""
import os, re, sys, time, subprocess, pathlib, asyncio, logging, threading, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import env; env.load()  # before anything below reads the environment
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from pack_loader import agent_files, resolve
from heartbeat import beat, trouble
from registry_client import check as registry_check
import alerts, approvals, logs

log = logging.getLogger("jinnee")
ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
NAME = os.getenv("JINNEE_NAME", "Jinnee")
LANG = os.getenv("JINNEE_LANG", "en")
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
GUARDED = [f"Edit(brain/{name})" for name in ("approvals.json", "autonomy_config.json", "alerts_state.json")]

RULES = """The agent files are your team's rules; brain/ is the business memory. Log every decision as one line in brain/decisions.log.md.

APPROVALS. The autonomy table below says what the team may do alone. Anything at "approval required", and anything not in the table, needs the owner's decision first; "forbidden" is never done by the team, only suggested in words.
- Ask: `python3 core/approvals.py add --agent NAME --action ACTION --title "…" --summary "…" --file handoffs/…` (or --text "…"). The file or text is exactly what will go out: finished, no DRAFT or TODO in it. Then tell the owner in one line what is asked.
- Only the owner decides: on the dashboard, or on Telegram with /ok, /change or /drop (the bridge records it; you cannot). A "yes" in conversation is not a decision: point the owner to /ok.
- Immediately before carrying out anything that needed approval: `python3 core/approvals.py consume ID`. Act only if it prints GO, exactly as approved, once. Then `python3 core/approvals.py result ID "what happened"` (add --failed if it did not work). Any other answer means do not act. Never act on the memory of an earlier approval.
- CHANGE: fix it and ask again with `add … --replaces ID`; the new version needs its own decision. DROPPED: do not do it; the reason is in brain/lessons.md.
- Never write brain/approvals.json, brain/autonomy_config.json or brain/alerts_state.json yourself.
- A level change is a request too: `python3 core/autonomy.py propose ACTION LEVEL --why "…"`, and after the owner approves, `python3 core/autonomy.py apply ID`. Locked actions cannot go above their maximum; only the owner can change that, by hand.
- When an agent starts or finishes a job: `python3 core/heartbeat.py beat AGENT "what was done"` (--working at the start, --failed if it went wrong)."""

def system_prompt() -> str:
    parts = [f"Your name is {NAME}. You speak with the owner in language '{LANG}'. Working directory: {ROOT}.\n" + RULES]
    for f in agent_files(PACKS):
        parts.append(f"\n\n===== {f.name} =====\n{f.read_text(encoding='utf-8')}")
    for b in ("company.md", "lessons.md"):
        p = BRAIN / b
        if p.exists(): parts.append(f"\n\n===== brain/{b} =====\n{p.read_text(encoding='utf-8')}")
    parts.append("\n\n===== autonomy levels (what the gate answers right now) =====\n" + BOOK.gate.describe())
    parts.append("\n\n===== approvals right now =====\n" + BOOK.brief())
    return "".join(parts)

FRESH = {"next": True}  # first message after start or /new opens a new session
TIMEOUT = 600  # seconds for one run
RUNNING = threading.Lock()  # one agent run at a time: the owner's message and a hand-over must not share a session mid-run
HELP = ("What you can do: send it again. If it keeps happening, send /new to start a clean conversation. "
        "/pending shows what is waiting.")

def call_claude(message: str):
    """→ (worked, text for the owner, short note for the heartbeat)."""
    base = ["claude", "-p", message, "--permission-mode", "acceptEdits", "--allowedTools", *TOOLS, "--disallowedTools", *GUARDED,
            "--append-system-prompt", system_prompt(), "--output-format", "text"]
    cmd = base if FRESH["next"] else base[:2] + ["--continue"] + base[2:]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=TIMEOUT)
        if r.returncode != 0 and "--continue" in cmd:
            r = subprocess.run(base, capture_output=True, text=True, cwd=ROOT, timeout=TIMEOUT)
    except FileNotFoundError:
        return False, ("I could not start: the `claude` program is not installed on this machine, or not found. Nothing was done. "
                       "What you can do: install Claude Code and run `claude login` on the machine, then send your message again."), "claude not found"
    except subprocess.TimeoutExpired:
        return False, ("I worked on this for 10 minutes without finishing, so I stopped. Some steps may be done and others not; "
                       "an approval that was already taken will not be used a second time. "
                       "What you can do: ask for a smaller step, or send /pending to see what is open."), "timed out after 10 minutes"
    if r.returncode != 0:
        detail = " ".join((r.stderr.strip() or r.stdout.strip()).split())[-300:]
        return False, ("I could not finish this: the Claude program stopped with an error, so nothing more was done. " + HELP +
                       " If the error below mentions signing in, run `claude login` on the machine." +
                       (f"\n\nError: {detail}" if detail else "")), f"stopped with an error (exit {r.returncode})"
    FRESH["next"] = False
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

async def say(send, text):
    for i in range(0, len(text), 4000):
        await send(text[i:i+4000])

# --- the owner's decisions, recorded without the agent in between
COMMAND = re.compile(r"/(ok|change|drop|undo|pending)(?:@\w+)?(?:[_ ]+(\S+))?(?:\s+(.*))?", re.S)
DECISION = {"ok": "ok", "change": "edit", "drop": "drop"}
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
    """/ok k7qd · /ok_k7qd · /change k7qd shorter · /drop k7qd why · /undo k7qd · /pending. The id may be left out when only one item fits."""
    m = COMMAND.fullmatch(text.strip())
    if not m: return "Commands: /pending · /ok · /change · /drop · /undo · /new"
    word, first, rest = m.group(1), m.group(2) or "", (m.group(3) or "").strip()
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
    await say(update.message.reply_text, await asyncio.to_thread(run_agent, text))

async def alert_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Once a minute: tell the owner what core/alerts.py says is due, then give decided items to the lead."""
    try:
        plan = await asyncio.to_thread(WATCH.due)
        for message in plan["messages"]:
            await ctx.bot.send_message(chat_id=OWNER, text=message[:4000])
        await asyncio.to_thread(WATCH.done, plan)  # only now: a message that could not be sent is tried again
    except Exception:
        log.exception("alert check failed; it runs again in a minute")
        return
    if plan["hand_over"]:  # as its own task: a run can take minutes, and the next check should not wait for it
        ctx.application.create_task(hand_over(ctx.bot, plan["hand_over"]))

async def hand_over(bot, items):
    reply = await asyncio.to_thread(run_agent, alerts.hand_over_message(BOOK, items))
    await say(lambda t: bot.send_message(chat_id=OWNER, text=t), reply)

async def morning_brief(ctx: ContextTypes.DEFAULT_TYPE):
    extra = ""
    stuck = trouble(BRAIN)  # before this run records its own start
    if stuck: extra += f"\nAgents in trouble: {'; '.join(stuck)}."
    ups = registry_check()
    if ups: extra += "\nUpdates available: " + "; ".join(f"{u['key']} → {u['to']}" for u in ups) + " Ask the owner whether to update."
    text = await asyncio.to_thread(run_agent,
        "Write the 5-line morning brief from brain/ and handoffs/, and also save it to brain/brief_today.md. "
        "Include what is waiting for the owner's decision (see 'approvals right now')." + extra)
    await ctx.bot.send_message(chat_id=OWNER, text=text[:4000])

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
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.COMMAND, on_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    if app.job_queue:  # both go to OWNER, which is always set here
        app.job_queue.run_daily(morning_brief, time=dt.time(hour=8, minute=0, tzinfo=alerts.zone()))
        app.job_queue.run_repeating(alert_tick, interval=60, first=20)
    print(f"{NAME} is running. Packs: {', '.join(PACKS)}")
    app.run_polling()

if __name__ == "__main__":
    main()
