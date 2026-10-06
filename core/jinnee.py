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
OWNER = int(os.getenv("TELEGRAM_OWNER_ID", "0") or 0)

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
    if OWNER and update.effective_user.id != OWNER: return
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
    if OWNER and update.effective_user.id != OWNER: return
    FRESH["next"] = True
    await update.message.reply_text("New day, clean slate. What are we doing?")

def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    if OWNER and app.job_queue:
        app.job_queue.run_daily(morning_brief, time=dt.time(hour=8, minute=0))
    print(f"{NAME} is running. Packs: {', '.join(PACKS)}")
    app.run_polling()

if __name__ == "__main__":
    main()
