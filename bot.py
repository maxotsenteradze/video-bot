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


def probe_playlist(url: str) -> dict | None:
    """Fast list of playlist entries (no per-video requests). None if not a playlist."""
    opts = base_opts()
    opts.update({"noplaylist": False, "extract_flat": "in_playlist"})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") != "playlist":
        return None
    entries = []
    for e in info.get("entries") or []:
        if not e:
            continue
        link = e.get("url") or e.get("webpage_url")
        if link and not link.startswith("http") and e.get("id"):
            link = f"https://www.youtube.com/watch?v={e['id']}"
        if link and (e.get("title") or "") not in ("[Private video]", "[Deleted video]"):
            entries.append({"url": link, "title": e.get("title") or "video"})
    if not entries:
        return None
    return {"title": info.get("title") or "Playlist", "entries": entries}


def download_quality(url: str, folder: str, height: int) -> Path:
    """Best video up to `height` (short side, so vertical videos work too)."""
    opts = base_opts()
    opts.update({
        "outtmpl": os.path.join(folder, "%(title).80s.%(ext)s"),
        "format": "bv*+ba/b",
        "format_sort": [f"res:{height}", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(folder).iterdir() if p.is_file() and not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("Nothing was downloaded.")
    return max(files, key=lambda p: p.stat().st_size)


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
        "YouTube playlist links work too: videos come one by one. /stop stops it.\n\n"
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

    # Playlist? (YouTube links with list=, or playlist pages)
    if "list=" in url or "/playlist" in url:
        try:
            pl = await asyncio.to_thread(probe_playlist, url)
        except Exception as e:
            log.info("Playlist probe failed, treating as single video: %s", e)
            pl = None
        if pl and len(pl["entries"]) > 1:
            key = uuid.uuid4().hex[:10]
            context.bot_data.setdefault("links", {})[key] = {
                "url": url, "title": pl["title"], "playlist": pl
            }
            n = len(pl["entries"])
            rows = []
            if "v=" in url or "youtu.be/" in url:
                rows.append([InlineKeyboardButton("▶️ Only this video", callback_data=f"one|{key}")])
            rows.append([InlineKeyboardButton(f"🎵 All {n} as MP3", callback_data=f"pl|{key}|mp3")])
            rows.append([
                InlineKeyboardButton(f"🎬 All 360p", callback_data=f"pl|{key}|360"),
                InlineKeyboardButton(f"🎬 All 480p", callback_data=f"pl|{key}|480"),
            ])
            rows.append([
                InlineKeyboardButton(f"🎬 All 720p", callback_data=f"pl|{key}|720"),
                InlineKeyboardButton("✖️ Cancel", callback_data=f"dl|{key}|cancel"),
            ])
            await status.edit_text(
                f"📃 Playlist: {pl['title'][:150]}\n{n} videos\n\n"
                "They will come one by one, each as its own file. "
                "If a video is too big for 50 MB, I'll try a lower quality automatically.\n"
                "Send /stop to stop.",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

    await show_single(status, context, url)


async def show_single(status, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
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


async def on_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'Only this video' pressed on a playlist link."""
    q = update.callback_query
    if not allowed(update):
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    key = q.data.split("|", 1)[1]
    item = context.bot_data.get("links", {}).pop(key, None)
    if not item:
        await q.edit_message_text("⌛ This link expired. Please send it again.")
        return
    await q.edit_message_text("🔎 Checking the video…")
    await show_single(q.message, context, item["url"])


# ---------- playlists ----------

QUALITY_STEPS = [720, 480, 360, 240, 144]


async def keep_awake(stop: asyncio.Event) -> None:
    """Render's free plan sleeps after 15 min with no incoming requests.
    While a playlist is running, ping our own URL so the server stays up."""
    if not WEBHOOK_URL:
        return
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        while not stop.is_set():
            try:
                await client.get(WEBHOOK_URL)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=240)
            except asyncio.TimeoutError:
                pass


async def run_playlist(context: ContextTypes.DEFAULT_TYPE, chat_id: int, status, pl: dict, choice: str) -> None:
    jobs = context.bot_data.setdefault("jobs", {})
    job = jobs[chat_id]
    entries = pl["entries"]
    n = len(entries)
    sent, skipped = 0, []
    awake = asyncio.Event()
    pinger = asyncio.create_task(keep_awake(awake))
    try:
        for i, e in enumerate(entries, 1):
            if job["stop"]:
                break
            title = e["title"][:120]
            try:
                await status.edit_text(f"📃 {pl['title'][:100]}\n⏳ {i}/{n}: {title}\n\nSend /stop to stop.")
            except Exception:
                pass

            steps = ["mp3"] if choice == "mp3" else [h for h in QUALITY_STEPS if h <= int(choice)]
            done = False
            last_err = ""
            for step in steps:
                if job["stop"]:
                    break
                async with download_lock:
                    with tempfile.TemporaryDirectory() as folder:
                        try:
                            if step == "mp3":
                                path = await asyncio.to_thread(download, e["url"], folder, "mp3")
                            else:
                                path = await asyncio.to_thread(download_quality, e["url"], folder, step)
                        except Exception as err:
                            last_err = str(err)[:120]
                            log.warning("Playlist item failed %s: %s", e["url"], err)
                            break  # a download error won't be fixed by lower quality
                        if path.stat().st_size > MAX_BYTES:
                            last_err = f"too big ({path.stat().st_size / 1024 / 1024:.0f} MB)"
                            continue  # try the next lower quality
                        caption = f"{i}/{n} · {e['title'][:900]}" + ("" if step == "mp3" else f" · {step}p")
                        try:
                            with path.open("rb") as f:
                                if step == "mp3":
                                    await context.bot.send_audio(chat_id, f, title=e["title"][:64], caption=caption,
                                                                 read_timeout=300, write_timeout=300)
                                else:
                                    await context.bot.send_video(chat_id, f, caption=caption, supports_streaming=True,
                                                                 read_timeout=300, write_timeout=300)
                        except Exception as err:
                            log.warning("Media upload failed, sending as file: %s", err)
                            with path.open("rb") as f:
                                await context.bot.send_document(chat_id, f, caption=caption,
                                                                read_timeout=300, write_timeout=300)
                        done = True
                        break
            if done:
                sent += 1
            elif not job["stop"]:
                skipped.append(f"{i}. {title} ({last_err or 'failed'})")
    finally:
        awake.set()
        pinger.cancel()
        jobs.pop(chat_id, None)

    summary = f"✅ Playlist finished: {sent}/{n} sent." if not job["stop"] else f"⏹ Stopped. {sent}/{n} sent."
    if skipped:
        summary += "\n\nSkipped:\n" + "\n".join(skipped)[:3500]
    try:
        await status.edit_text(summary)
    except Exception:
        await context.bot.send_message(chat_id, summary)


async def on_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(update):
        await q.answer("Not allowed", show_alert=True)
        return
    _, key, choice = q.data.split("|", 2)
    chat_id = q.message.chat_id
    jobs = context.bot_data.setdefault("jobs", {})
    if chat_id in jobs:
        await q.answer("A playlist is already running. Send /stop first.", show_alert=True)
        return
    await q.answer()
    item = context.bot_data.get("links", {}).pop(key, None)
    if not item:
        await q.edit_message_text("⌛ This link expired. Please send it again.")
        return
    jobs[chat_id] = {"stop": False}
    await q.edit_message_text(f"📃 Starting playlist ({len(item['playlist']['entries'])} videos)…")
    # Run in the background so /stop and other messages still work.
    context.application.create_task(run_playlist(context, chat_id, q.message, item["playlist"], choice))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    job = context.bot_data.get("jobs", {}).get(update.effective_chat.id)
    if job:
        job["stop"] = True
        await update.message.reply_text("⏹ Stopping after the current video…")
    else:
        await update.message.reply_text("Nothing is running.")


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
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(on_one, pattern=r"^one\|"))
    app.add_handler(CallbackQueryHandler(on_playlist, pattern=r"^pl\|"))
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
