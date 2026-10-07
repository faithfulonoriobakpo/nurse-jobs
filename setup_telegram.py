"""One-off Telegram setup for new-job alerts (nurse-jobs and dev-jobs). Safe to re-run.

1. In Telegram, message @BotFather, send /newbot, and follow the prompts. It replies with a token.
2. Put it in this folder's .env.local (git-ignored):   TELEGRAM_BOT_TOKEN=123456:ABC...
3. Open your new bot in Telegram and press Start (or send it any message).
   To alert a group instead, add the bot to the group and send a message there.
4. Run:  python setup_telegram.py

It finds the chat you messaged the bot from, sends a test message, and stores TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID as GitHub Actions secrets on both repos. The token is never printed.
"""

import json
import urllib.error
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
REPOS = {"faithfulonoriobakpo/nurse-jobs": ROOT, "faithfulonoriobakpo/dev-jobs": ROOT.parent / "dev-jobs"}


def read_env(path):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                env[k.strip()] = v.strip().strip('"')
    return env


def write_env(path, updates):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [l for l in lines if l.partition("=")[0].strip() not in updates]
    path.write_text("\n".join(lines + [f"{k}={v}" for k, v in updates.items()]) + "\n", encoding="utf-8")


def bot(token, method, body=None):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(body).encode() if body else None, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["result"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Telegram {method} failed ({e.code}). Check TELEGRAM_BOT_TOKEN in .env.local.")


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN") or read_env(ROOT / ".env.local").get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Add TELEGRAM_BOT_TOKEN=... to .env.local first (see the top of this file).")
    me = bot(token, "getMe")
    print(f"Bot: @{me['username']}")

    chats = {}
    for u in bot(token, "getUpdates"):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat
    if not chats:
        sys.exit(f"No messages yet. Open https://t.me/{me['username']} in Telegram, press Start, then run this again.")
    chat = list(chats.values())[-1]   # the most recent conversation
    name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username")
    print(f"Chat: {name} ({chat['type']})")

    bot(token, "sendMessage", {"chat_id": chat["id"], "text": "✅ Job alerts are set up. You'll get a message here when "
                               "the nurse or developer search finds new jobs (it runs every 4 hours)."})
    print("Sent a test message")

    values = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat["id"])}
    gh = shutil.which("gh") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "gh-cli" / "bin" / "gh.exe")
    for repo, folder in REPOS.items():
        for k, v in values.items():
            subprocess.run([gh, "secret", "set", k, "--repo", repo], input=v.encode(), check=True, stdout=subprocess.DEVNULL)
        if folder.exists():
            write_env(folder / ".env.local", values)
        print(f"Stored TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on {repo}")


if __name__ == "__main__":
    main()
