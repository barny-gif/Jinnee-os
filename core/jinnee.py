"""
Entry point. v0.1: Telegram bridge + Claude Code call with the lead persona + morning brief.
run_agent() is the hook for the full orchestration logic (sessions, intake, delegation).
This is the skeleton; Forge fills it in.
"""
import os, sys, subprocess, pathlib, asyncio, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import env; env.load()  # before anything below reads the environment
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from pack_loader import agent_files, resolve
from heartbeat import beat, silent
from registry_client import check as registry_check

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

def system_prompt() -> str:
    parts = [f"Your name is {NAME}. You speak with the owner in language '{LANG}'. Working directory: {ROOT}.\n"
             "The agent files are your team's rules; brain/ is the business memory. Log every decision as one line in brain/decisions.log.md. "
             "Put any draft that needs approval into brain/approvals.json (id, agent, title, summary)."]
    for f in agent_files(PACKS):
        parts.append(f"\n\n===== {f.name} =====\n{f.read_text(encoding='utf-8')}")
    for b in ("company.md", "lessons.md", "autonomy_config.json"):
        p = BRAIN / b
        if p.exists(): parts.append(f"\n\n===== brain/{b} =====\n{p.read_text(encoding='utf-8')}")
    return "".join(parts)

FRESH = {"next": True}  # first message after start or /new opens a new session

def run_agent(message: str) -> str:
    """Claude Code CLI, non-interactive. Runs on the owner's Pro/Max subscription.
    --continue keeps one running conversation; acceptEdits lets agents write brain/ and handoffs/."""
    base = ["claude", "-p", message, "--permission-mode", "acceptEdits",
            "--append-system-prompt", system_prompt(), "--output-format", "text"]
    cmd = base if FRESH["next"] else base[:2] + ["--continue"] + base[2:]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=600)
        if r.returncode != 0 and "--continue" in cmd:
            r = subprocess.run(base, capture_output=True, text=True, cwd=ROOT, timeout=600)
        out = r.stdout.strip() or r.stderr.strip() or "…"
        FRESH["next"] = False
    except FileNotFoundError:
        out = "The `claude` command was not found. Did `claude login` run?"
    except subprocess.TimeoutExpired:
        out = "This took longer than 10 minutes, so I stopped. Try a smaller step."
    beat("jinnee", "replied")
    return out

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not from_owner(update): return
    reply = await asyncio.to_thread(run_agent, update.message.text)
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i:i+4000])

async def morning_brief(ctx: ContextTypes.DEFAULT_TYPE):
    extra = ""
    sil = silent(resolve(PACKS)["agents"])
    if sil: extra += f"\nAgents that went quiet: {', '.join(sil)}."
    ups = registry_check()
    if ups: extra += "\nUpdates available: " + "; ".join(f"{u['key']} → {u['to']}" for u in ups) + " Ask the owner whether to update."
    text = await asyncio.to_thread(run_agent,
        "Write the 5-line morning brief from brain/ and handoffs/, and also save it to brain/brief_today.md." + extra)
    await ctx.bot.send_message(chat_id=OWNER, text=text[:4000])

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not from_owner(update): return
    FRESH["next"] = True
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
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    if app.job_queue:  # the brief goes to OWNER, which is always set here
        app.job_queue.run_daily(morning_brief, time=dt.time(hour=8, minute=0))
    print(f"{NAME} is running. Packs: {', '.join(PACKS)}")
    app.run_polling()

if __name__ == "__main__":
    main()
