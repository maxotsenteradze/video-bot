"""
Telegram video downloader bot.

Send the bot a link (YouTube, Instagram, TikTok, X/Twitter, Facebook, Reddit,
Vimeo and ~1000 other sites supported by yt-dlp) and it replies with the video.

Environment variables:
  BOT_TOKEN       - token from @BotFather (required)
  ALLOWED_USERS   - comma-separated Telegram user IDs allowed to use the bot
                    (optional but strongly recommended; empty = anyone)
  COOKIES         - optional: contents of a cookies.txt file (Netscape format)
                    for sites that need login (Instagram, some YouTube videos)
  WEBHOOK_URL     - public URL of the service. On Render this is detected
                    automatically from RENDER_EXTERNAL_URL.
  PORT            - set automatically by the host.

If no WEBHOOK_URL / RENDER_EXTERNAL_URL is found, the bot runs in polling
mode, so you can also just run `python bot.py` on your own PC.
"""

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
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
MAX_BYTES = 49 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")

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


def download(url: str, folder: str, audio_only: bool) -> Path:
    """Download with yt-dlp, preferring files small enough for Telegram."""
    if audio_only:
        fmt = "bestaudio[filesize<45M]/bestaudio"
        post = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "160"}]
    else:
        # Best quality that fits under ~48 MB, MP4 for best phone compatibility.
        fmt = (
            "bv*[ext=mp4][filesize<40M]+ba[ext=m4a]/"
            "b[ext=mp4][filesize<48M]/"
            "bv*[height<=720][filesize_approx<40M]+ba/"
            "b[height<=720]/"
            "bv*[height<=480]+ba/b"
        )
        post = []
    opts = {
        "format": fmt,
        "outtmpl": os.path.join(folder, "%(title).80s.%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": post,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = str(COOKIE_FILE)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(folder).iterdir() if p.is_file() and not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("Nothing was downloaded.")
    return max(files, key=lambda p: p.stat().st_size)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text(f"Not allowed. Your ID: {update.effective_user.id}")
        return
    await update.message.reply_text(
        "Send me a video link and I'll send the video back.\n\n"
        "Want only the sound? Send:  /mp3 <link>\n\n"
        "Limit: 50 MB per file (Telegram rule for bots)."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_only=False) -> None:
    msg = update.message
    if not allowed(update):
        await msg.reply_text(f"Not allowed. Your ID: {update.effective_user.id}")
        return
    m = URL_RE.search(msg.text or "")
    if not m:
        await msg.reply_text("Send me a link that starts with http:// or https://")
        return
    url = m.group(0)

    status = await msg.reply_text("⏳ Downloading…")
    async with download_lock:
        with tempfile.TemporaryDirectory() as folder:
            try:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_VIDEO)
                path = await asyncio.to_thread(download, url, folder, audio_only)
            except Exception as e:  # yt-dlp raises many different errors
                log.warning("Download failed for %s: %s", url, e)
                text = str(e)
                if "Sign in" in text or "login" in text.lower() or "cookies" in text.lower():
                    hint = "This site wants a login. Add cookies (see README)."
                elif "Unsupported URL" in text:
                    hint = "This site isn't supported."
                else:
                    hint = "Check the link is public and try again."
                await status.edit_text(f"❌ Couldn't download.\n{hint}\n\n{text[:300]}")
                return

            size = path.stat().st_size
            if size > MAX_BYTES:
                await status.edit_text(
                    f"❌ The file is {size / 1024 / 1024:.0f} MB, but Telegram lets bots "
                    "send only 50 MB. Try a shorter video or /mp3 for sound only."
                )
                return

            await status.edit_text("📤 Uploading…")
            try:
                with path.open("rb") as f:
                    if audio_only:
                        await msg.reply_audio(f, title=path.stem, read_timeout=300, write_timeout=300)
                    else:
                        await msg.reply_video(
                            f, supports_streaming=True, read_timeout=300, write_timeout=300
                        )
            except Exception as e:
                log.warning("Video upload failed, sending as file: %s", e)
                with path.open("rb") as f:
                    await msg.reply_document(f, read_timeout=300, write_timeout=300)
            await status.delete()


async def mp3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context, audio_only=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context)


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("id", myid))
    app.add_handler(CommandHandler("mp3", mp3))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if WEBHOOK_URL:
        # Webhook mode: Telegram calls our URL, which also wakes a sleeping free server.
        secret_path = BOT_TOKEN.split(":")[-1][:20]
        log.info("Starting webhook on port %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=secret_path,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{secret_path}",
            drop_pending_updates=False,
        )
    else:
        log.info("No WEBHOOK_URL found, starting in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
