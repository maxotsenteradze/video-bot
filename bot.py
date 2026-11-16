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

import shutil
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

def load_cookies() -> Path | None:
    """Cookies from a Render Secret File (cookies.txt) or the COOKIES variable.
    Copied to /tmp because yt-dlp writes updated cookies back to the file."""
    text = None
    for p in (Path("/etc/secrets/cookies.txt"), Path("cookies.txt")):
        if p.is_file():
            text = p.read_text(errors="ignore")
            break
    if text is None and os.environ.get("COOKIES"):
        text = os.environ["COOKIES"]
        if "\n" not in text and "\\n" in text:  # pasted with literal \n
            text = text.replace("\\n", "\n")
    if not text or not text.strip():
        return None
    if not text.lstrip().startswith("# "):
        text = "# Netscape HTTP Cookie File\n" + text
    dest = Path(tempfile.gettempdir()) / "cookies.txt"
    dest.write_text(text)
    return dest


COOKIE_FILE = load_cookies()
COOKIE_LINES = 0
if COOKIE_FILE:
    COOKIE_LINES = sum(1 for l in COOKIE_FILE.read_text().splitlines() if l.strip() and not l.startswith("#"))

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
            entries.append({"url": link, "title": e.get("title") or "video", "no": len(entries) + 1})
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
        if await maybe_select_numbers(update, context):
            return
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
            text, markup = playlist_menu(key, context.bot_data["links"][key])
            await status.edit_text(text, reply_markup=markup)
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
                            last_err = str(err).replace("please report this issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. Confirm you are on the latest version using  yt-dlp -U", "").strip()[:200]
                            log.warning("Playlist item failed %s: %s", e["url"], err)
                            break  # a download error won't be fixed by lower quality
                        if path.stat().st_size > MAX_BYTES:
                            last_err = f"too big ({path.stat().st_size / 1024 / 1024:.0f} MB)"
                            continue  # try the next lower quality
                        num = f"#{e['no']} " if e.get("no", i) != i else ""
                        caption = f"{i}/{n} · {num}{e['title'][:900]}" + ("" if step == "mp3" else f" · {step}p")
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
                if "sign in" in last_err.lower() and len(skipped) >= 2 and sent == 0:
                    skipped.append("Stopped early: YouTube is blocking the server. Add/refresh cookies (/status).")
                    break
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
    pl = item["playlist"]
    if item.get("use_sel"):
        chosen = [e for e in pl["entries"] if e["no"] in item["sel"]]
        if not chosen:
            await q.edit_message_text("Nothing selected. Send the link again.")
            return
        pl = {"title": pl["title"], "entries": chosen}
    context.user_data.pop("selecting", None)
    jobs[chat_id] = {"stop": False}
    await q.edit_message_text(f"📃 Starting playlist ({len(pl['entries'])} videos)…")
    # Run in the background so /stop and other messages still work.
    context.application.create_task(run_playlist(context, chat_id, q.message, pl, choice))


# ---------- choosing videos from a playlist ----------

PAGE = 8


def playlist_menu(key: str, item: dict):
    pl = item["playlist"]
    n = len(pl["entries"])
    rows = []
    if "v=" in item["url"] or "youtu.be/" in item["url"]:
        rows.append([InlineKeyboardButton("▶️ Only this video", callback_data=f"one|{key}")])
    rows.append([InlineKeyboardButton("☑️ Choose videos", callback_data=f"sel|{key}|0")])
    rows.append([InlineKeyboardButton(f"🎵 All {n} as MP3", callback_data=f"pl|{key}|mp3")])
    rows.append([
        InlineKeyboardButton("🎬 All 360p", callback_data=f"pl|{key}|360"),
        InlineKeyboardButton("🎬 All 480p", callback_data=f"pl|{key}|480"),
    ])
    rows.append([
        InlineKeyboardButton("🎬 All 720p", callback_data=f"pl|{key}|720"),
        InlineKeyboardButton("✖️ Cancel", callback_data=f"dl|{key}|cancel"),
    ])
    text = (
        f"📃 Playlist: {pl['title'][:150]}\n{n} videos\n\n"
        "Download all, or tap ☑️ Choose videos to pick some.\n"
        "Videos come one by one. If one is over 50 MB, I'll try a lower quality.\n"
        "Send /stop to stop."
    )
    return text, InlineKeyboardMarkup(rows)


def selection_view(key: str, item: dict, page: int):
    entries = item["playlist"]["entries"]
    sel = item.setdefault("sel", set())
    pages = max(1, (len(entries) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for e in entries[page * PAGE:(page + 1) * PAGE]:
        mark = "✅" if e["no"] in sel else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {e['no']}. {e['title'][:45]}",
                                          callback_data=f"tg|{key}|{e['no']}|{page}")])
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"sel|{key}|{page - 1}"),
            InlineKeyboardButton(f"Page {page + 1}/{pages}", callback_data=f"sel|{key}|{page}"),
            InlineKeyboardButton("▶️", callback_data=f"sel|{key}|{page + 1}"),
        ])
    rows.append([
        InlineKeyboardButton("All", callback_data=f"sa|{key}|{page}"),
        InlineKeyboardButton("None", callback_data=f"sn|{key}|{page}"),
        InlineKeyboardButton("⬅️ Back", callback_data=f"bk|{key}"),
    ])
    rows.append([InlineKeyboardButton(f"➡️ Next: choose format ({len(sel)} selected)",
                                      callback_data=f"sd|{key}")])
    text = (
        f"☑️ {item['playlist']['title'][:100]}\n"
        f"Selected: {len(sel)} of {len(entries)}\n\n"
        "Tap videos to tick them, or type numbers here, like:  1-5, 8, 12"
    )
    return text, InlineKeyboardMarkup(rows)


def format_menu_for_selection(key: str, item: dict):
    k = len(item["sel"])
    rows = [
        [InlineKeyboardButton(f"🎵 {k} as MP3", callback_data=f"pl|{key}|mp3")],
        [InlineKeyboardButton("🎬 360p", callback_data=f"pl|{key}|360"),
         InlineKeyboardButton("🎬 480p", callback_data=f"pl|{key}|480"),
         InlineKeyboardButton("🎬 720p", callback_data=f"pl|{key}|720")],
        [InlineKeyboardButton("⬅️ Change selection", callback_data=f"sel|{key}|0"),
         InlineKeyboardButton("✖️ Cancel", callback_data=f"dl|{key}|cancel")],
    ]
    nums = ", ".join(str(x) for x in sorted(item["sel"]))
    return f"✅ {k} videos selected: {nums[:500]}\n\nChoose format:", InlineKeyboardMarkup(rows)


async def on_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(update):
        await q.answer("Not allowed", show_alert=True)
        return
    parts = q.data.split("|")
    action, key = parts[0], parts[1]
    item = context.bot_data.get("links", {}).get(key)
    if not item or "playlist" not in item:
        await q.answer()
        await q.edit_message_text("⌛ This link expired. Please send it again.")
        return
    sel = item.setdefault("sel", set())
    page = 0

    if action == "bk":
        item["use_sel"] = False
        context.user_data.pop("selecting", None)
        text, markup = playlist_menu(key, item)
        await q.answer()
        await q.edit_message_text(text, reply_markup=markup)
        return
    if action == "sd":
        if not sel:
            await q.answer("Tick at least one video first.", show_alert=True)
            return
        item["use_sel"] = True
        text, markup = format_menu_for_selection(key, item)
        await q.answer()
        await q.edit_message_text(text, reply_markup=markup)
        return
    if action == "tg":
        no, page = int(parts[2]), int(parts[3])
        sel.symmetric_difference_update({no})
    elif action == "sa":
        page = int(parts[2])
        sel.update(e["no"] for e in item["playlist"]["entries"])
    elif action == "sn":
        page = int(parts[2])
        sel.clear()
    else:  # "sel": open / change page
        page = int(parts[2])

    item["use_sel"] = False
    item["page"] = page
    context.user_data["selecting"] = {"key": key, "chat_id": q.message.chat_id,
                                      "message_id": q.message.message_id}
    text, markup = selection_view(key, item, page)
    await q.answer()
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except Exception:
        pass  # "message is not modified" (e.g. pressed the page label)


def parse_numbers(text: str, maximum: int) -> set[int] | None:
    """'1-5, 8 12' -> {1,2,3,4,5,8,12}. None if the text isn't a list of numbers."""
    if not re.fullmatch(r"[\d\s,;\-–]+", text.strip()):
        return None
    result = set()
    for a, b in re.findall(r"(\d+)\s*(?:[-–]\s*(\d+))?", text):
        lo = int(a)
        hi = int(b) if b else lo
        if lo > hi:
            lo, hi = hi, lo
        result.update(x for x in range(lo, hi + 1) if 1 <= x <= maximum)
    return result


async def maybe_select_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("selecting")
    if not state:
        return False
    item = context.bot_data.get("links", {}).get(state["key"])
    if not item or "playlist" not in item:
        return False
    nums = parse_numbers(update.message.text or "", len(item["playlist"]["entries"]))
    if nums is None:
        return False
    if not nums:
        await update.message.reply_text("Those numbers aren't in the playlist.")
        return True
    item["sel"] = nums
    item["use_sel"] = False
    text, markup = selection_view(state["key"], item, item.get("page", 0))
    try:
        await context.bot.edit_message_text(text, chat_id=state["chat_id"],
                                            message_id=state["message_id"], reply_markup=markup)
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Selected {len(nums)}: {', '.join(map(str, sorted(nums)))[:300]}\n"
        "Tap ➡️ Next in the list above to choose the format."
    )
    return True


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if COOKIE_FILE:
        domains = sorted({l.split("\t")[0].lstrip(".") for l in COOKIE_FILE.read_text().splitlines()
                          if l.strip() and not l.startswith("#") and "\t" in l})
        ck = f"✅ Cookies loaded: {COOKIE_LINES} lines\nSites: {', '.join(domains)[:500] or '?'}"
    else:
        ck = "❌ No cookies loaded. YouTube may block downloads."
    deno = shutil.which("deno")
    js = f"✅ JS runtime: deno ({deno})" if deno else "❌ No JS runtime (deno) — YouTube will fail"
    await update.message.reply_text(f"{ck}\n{js}\n\nyt-dlp version: {yt_dlp.version.__version__}")


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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(on_one, pattern=r"^one\|"))
    app.add_handler(CallbackQueryHandler(on_playlist, pattern=r"^pl\|"))
    app.add_handler(CallbackQueryHandler(on_select, pattern=r"^(sel|tg|sa|sn|sd|bk)\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))

    if WEBHOOK_URL:
        secret_path = BOT_TOKEN.split(":")[-1][:20]
        log.info("Starting webhook on port %s (cookies: %s lines)", PORT, COOKIE_LINES)
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
