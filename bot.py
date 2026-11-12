"""
Telegram video downloader bot.

Send the bot a link and it shows buttons to pick a format:
MP3 (audio), 360p, 480p, 720p, 1080p or Best. Works with YouTube, Instagram,
TikTok, X/Twitter, Facebook, Reddit, Vimeo and ~1000 other sites (yt-dlp).

Environment variables:
  BOT_TOKEN       - token from @BotFather (required)
  ALLOWED_USERS   - comma-separated Telegram user IDs allowed to use the bot
                    (optional but strongly recommended; empty = anyone)
  COOKIES         - optional: contents of a cookies.txt file (Netscape format)
                    for sites that need login (Instagram, some YouTube videos)
  WEBHOOK_URL     - public URL of the service. On Render this is detected
                    automatically from RENDER_EXTERNAL_URL.
  PORT            - set automatically by the host.

Without WEBHOOK_URL / RENDER_EXTERNAL_URL the bot runs in polling mode,
so you can also just run `python bot.py` on your own PC.
"""

import asyncio
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("videobot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x
}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "8080"))

# Telegram bots can upload at most 50 MB per file.
LIMIT_MB = 50
MAX_BYTES = 49 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")
HEIGHTS = [360, 480, 720, 1080]

COOKIE_FILE = None
if os.environ.get("COOKIES"):
    COOKIE_FILE = Path(tempfile.gettempdir()) / "cookies.txt"
    COOKIE_FILE.write_text(os.environ["COOKIES"])

# One download at a time keeps the small free server from running out of RAM.
download_lock = asyncio.Lock()


def allowed(update: Update) -> bool:
    return not ALLOWED_USERS or (
        update.effective_user and update.effective_user.id in ALLOWED_USERS
    )


def base_opts() -> dict:
    opts = {"noplaylist": True, "quiet": True, "no_warnings": True}
    if COOKIE_FILE:
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


# ---------- yt-dlp helpers (run in a thread) ----------

def _size(f: dict) -> int:
    return f.get("filesize") or f.get("filesize_approx") or 0


def probe(url: str) -> dict:
    """Read video info without downloading: title, length and size per quality."""
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)

    formats = info.get("formats") or []
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    best_audio = max((_size(f) for f in audios), default=0)

    options = []  # (quality p, est. bytes, yt-dlp format string)
    vids = []
    for f in formats:
        if f.get("vcodec") == "none" or not f.get("height"):
            continue
        short = min(f["height"], f.get("width") or f["height"])  # vertical videos: use short side
        vids.append((short, f))
    for h in HEIGHTS:
        cands = [(q, f) for q, f in vids if q <= h * 1.1]
        if not cands:
            continue
        q, best = max(cands, key=lambda t: (t[0], t[1].get("ext") == "mp4", t[1].get("tbr") or 0))
        if any(o[0] == q for o in options):
            continue
        fid = best["format_id"]
        size = _size(best)
        if best.get("acodec") == "none":  # video-only stream: add audio
            fmt = f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio/{fid}"
            if size:
                size += best_audio
        else:
            fmt = fid
        options.append((q, size, fmt))

    return {
        "title": info.get("title") or "video",
        "duration": info.get("duration"),
        "options": options,
    }


BEST_FIT = (
    "bv*[ext=mp4][filesize<40M]+ba[ext=m4a]/"
    "b[ext=mp4][filesize<48M]/"
    "bv*[height<=720][filesize_approx<40M]+ba/"
    "b[height<=720]/bv*[height<=480]+ba/b"
)


def download(url: str, folder: str, choice: str, fmt: str | None = None) -> Path:
    opts = base_opts()
    opts["outtmpl"] = os.path.join(folder, "%(title).80s.%(ext)s")
    if choice == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "160"}
        ]
    else:
        opts["format"] = fmt or BEST_FIT
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(folder).iterdir() if p.is_file() and not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("Nothing was downloaded.")
    return max(files, key=lambda p: p.stat().st_size)


# ---------- Telegram handlers ----------

def mb(n: int) -> str:
    return f"~{n / 1024 / 1024:.0f} MB" if n else "size ?"


def fmt_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def build_keyboard(key: str, info: dict) -> InlineKeyboardMarkup:
    rows = []
    # Audio: mp3 at 160 kbps is ~1.2 MB per minute
    mp3_est = int((info["duration"] or 0) * 160_000 / 8)
    rows.append([InlineKeyboardButton(f"🎵 MP3  {mb(mp3_est)}", callback_data=f"dl|{key}|mp3")])

    row = []
    for i, (q, size, _) in enumerate(info["options"]):
        warn = " ⚠️" if size > MAX_BYTES else ""
        row.append(InlineKeyboardButton(f"🎬 {q}p  {mb(size)}{warn}", callback_data=f"dl|{key}|{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⭐ Best that fits 50 MB", callback_data=f"dl|{key}|best")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"dl|{key}|cancel")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text(f"Not allowed. Your ID: {update.effective_user.id}")
        return
    await update.message.reply_text(
        "Send me a video link and choose the format:\n"
        "🎵 MP3 · 🎬 360p · 480p · 720p · 1080p\n\n"
        f"Limit: {LIMIT_MB} MB per file (Telegram rule for bots). "
        "Options marked ⚠️ are probably too big."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not allowed(update):
        await msg.reply_text(f"Not allowed. Your ID: {update.effective_user.id}")
        return
    m = URL_RE.search(msg.text or "")
    if not m:
        await msg.reply_text("Send me a link that starts with http:// or https://")
        return
    url = m.group(0)

    status = await msg.reply_text("🔎 Checking the link…")
    try:
        info = await asyncio.to_thread(probe, url)
    except Exception as e:
        log.warning("Probe failed for %s: %s", url, e)
        await status.edit_text(error_text(e))
        return

    key = uuid.uuid4().hex[:10]
    context.bot_data.setdefault("links", {})[key] = {
        "url": url, "title": info["title"], "options": info["options"]
    }

    dur = fmt_duration(info["duration"])
    text = f"🎞 {info['title'][:200]}" + (f"\n⏱ {dur}" if dur else "") + "\n\nChoose format:"
    await status.edit_text(text, reply_markup=build_keyboard(key, info))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(update):
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    _, key, choice = q.data.split("|", 2)

    if choice == "cancel":
        context.bot_data.get("links", {}).pop(key, None)
        await q.edit_message_text("Cancelled.")
        return

    item = context.bot_data.get("links", {}).get(key)
    if not item:
        await q.edit_message_text("⌛ This link expired (the server restarted). Please send it again.")
        return

    fmt = None
    if choice == "mp3":
        label = "MP3"
    elif choice == "best":
        label = "best quality"
    else:
        q, _, fmt = item["options"][int(choice)]
        label = f"{q}p"
    await q.edit_message_text(f"⏳ Downloading {label}…\n{item['title'][:200]}")

    chat_id = q.message.chat_id
    async with download_lock:
        with tempfile.TemporaryDirectory() as folder:
            try:
                await context.bot.send_chat_action(
                    chat_id, ChatAction.UPLOAD_VOICE if choice == "mp3" else ChatAction.UPLOAD_VIDEO
                )
                path = await asyncio.to_thread(download, item["url"], folder, choice, fmt)
            except Exception as e:
                log.warning("Download failed for %s: %s", item["url"], e)
                await q.edit_message_text(error_text(e))
                return

            size = path.stat().st_size
            if size > MAX_BYTES:
                await q.edit_message_text(
                    f"❌ {label} is {size / 1024 / 1024:.0f} MB, but Telegram lets bots send "
                    f"only {LIMIT_MB} MB. Send the link again and pick a lower quality or MP3."
                )
                return

            await q.edit_message_text(f"📤 Uploading {label} ({size / 1024 / 1024:.1f} MB)…")
            try:
                with path.open("rb") as f:
                    if choice == "mp3":
                        await context.bot.send_audio(
                            chat_id, f, title=item["title"][:64], read_timeout=300, write_timeout=300
                        )
                    else:
                        await context.bot.send_video(
                            chat_id, f, supports_streaming=True, read_timeout=300, write_timeout=300
                        )
            except Exception as e:
                log.warning("Upload as media failed, sending as file: %s", e)
                with path.open("rb") as f:
                    await context.bot.send_document(chat_id, f, read_timeout=300, write_timeout=300)

    context.bot_data.get("links", {}).pop(key, None)
    try:
        await q.message.delete()
    except Exception:
        pass


def error_text(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if "sign in" in low or "login" in low or "cookies" in low:
        hint = "This site wants a login. Add cookies (see README)."
    elif "unsupported url" in low:
        hint = "This site isn't supported."
    elif "403" in text:
        hint = "The site blocked the server."
    else:
        hint = "Check the link is public and try again."
    return f"❌ Couldn't download.\n{hint}\n\n{text[:300]}"


async def mp3_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Old shortcut still works: /mp3 <link> just shows the menu.
    await on_link(update, context)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("id", myid))
    app.add_handler(CommandHandler("mp3", mp3_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))

    if WEBHOOK_URL:
        secret_path = BOT_TOKEN.split(":")[-1][:20]
        log.info("Starting webhook on port %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=secret_path,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{secret_path}",
        )
    else:
        log.info("No WEBHOOK_URL found, starting in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
