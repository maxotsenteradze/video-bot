# Telegram Video Downloader Bot

Send the bot a link and it sends the video back. It works with YouTube,
Instagram, TikTok, X/Twitter, Facebook, Reddit, Vimeo and about 1000 other sites
(powered by yt-dlp).

- Send a link and the bot shows buttons: **🎵 MP3 · 🎬 360p · 480p · 720p · 1080p · ⭐ Best that fits**,
  each with an estimated file size. Tap one and you get the file.
- Send a **YouTube playlist** link to download all of it: pick MP3, 360p, 480p or 720p and the
  videos come **one by one**, each as its own file. If one is over 50 MB, the bot tries a lower
  quality automatically. Send `/stop` to stop.
  Tap **☑️ Choose videos** to tick only the ones you want (8 per page), or type numbers like
  `1-5, 8, 12`, then pick the format. (A link to a video inside a playlist also offers
  "Only this video".)
- Send `/id` and you get your Telegram ID.

Telegram lets bots send files up to **50 MB**. Options marked ⚠️ are probably
bigger than that. Pick a lower quality or MP3 instead.

---

## Setup (about 15 minutes, all free)

### 1. Create the bot in Telegram
1. Open **@BotFather** in Telegram and send `/newbot`.
2. Choose a name and a username ending in `bot`.
3. BotFather gives you a **token** like `7412345678:AAH...`. Copy it and keep it secret.

### 2. Get your Telegram ID
Open **@userinfobot** in Telegram and press Start. It shows your ID (a number).
This makes the bot work only for you, so strangers can't use it.

### 3. Put the code on GitHub
1. On github.com, click **New repository**, name it `video-bot`, and set it to **Private**.
2. Click **uploading an existing file** and upload all files from this folder:
   `bot.py`, `Dockerfile`, `requirements.txt`, `README.md`, `.gitignore`.
3. Click **Commit changes**.

### 4. Deploy on Render (free, no card needed)
1. Go to **render.com** and sign up with your GitHub account.
2. Click **New → Web Service** and pick your `video-bot` repo.
3. Render detects the Dockerfile automatically. Set:
   - **Instance type:** Free
4. Under **Environment Variables**, add:
   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | your token from BotFather |
   | `ALLOWED_USERS` | your ID from step 2 (several IDs separated by commas) |
5. Click **Deploy**. Wait until the log says `Starting webhook`.

### 5. Test it
Open your bot in Telegram, press **Start**, and send a link.

---

## Good to know

- **The first message after a break is slow.** Render's free server sleeps after
  15 minutes with no use. Your next message wakes it up, which takes about a minute,
  and then it works normally. Nothing is lost.
- **YouTube blocks cloud servers.** The bot routes downloads through Cloudflare WARP
  (free, built into the Docker image) and uses your YouTube cookies. Cookies expire every
  few weeks; when they do, the bot messages you "🍪 YouTube cookies have expired".
  To update them:
  1. Open a **private/incognito** window and log in to YouTube (use a spare account).
  2. Export cookies with **"Get cookies.txt LOCALLY"** (PC) or **Cookie Editor** (iPhone Safari).
  3. **Close the private window** right away (using it again rotates the cookies).
  4. **Send the file to the bot** in Telegram. It accepts Netscape .txt or JSON, pins the
     message, and reloads it after restarts. No Render visit needed.
  For the auto-reload after restarts, set `ALLOWED_USERS` (or `OWNER_ID`) to your Telegram ID (`/id`).
  `/status` shows cookies/WARP state; `/debug <link>` tests every setup and picks one that works.
- **Keep yt-dlp up to date.** Sites change often. If downloads start failing, open
  Render and click **Manual Deploy → Clear build cache & deploy**. This installs the
  newest yt-dlp.
- **Run it on your own PC instead:** install Python, run `pip install -r requirements.txt`,
  set `BOT_TOKEN`, and run `python bot.py`. With no webhook URL set, it uses polling mode.
  You also need ffmpeg installed for merging and MP3.
- Only download videos you have the right to save (your own, free-to-share ones, and so on).
