# Telegram Video Downloader Bot

Send the bot a link and it sends the video back. It works with YouTube,
Instagram, TikTok, X/Twitter, Facebook, Reddit, Vimeo and about 1000 other sites
(powered by yt-dlp).

- Send a link and you get the video (MP4).
- Send `/mp3 <link>` and you get only the audio.
- Send `/id` and you get your Telegram ID.

Telegram lets bots send files up to **50 MB**, so the bot automatically picks a
quality that fits (usually 720p or lower).

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
- **YouTube sometimes blocks cloud servers** with "Sign in to confirm you're not a bot".
  Instagram often needs a login too. To fix that, add cookies:
  1. On your PC, install the browser extension **"Get cookies.txt LOCALLY"**.
  2. Log in to YouTube or Instagram, then export cookies.txt with the extension.
  3. In Render, add an environment variable named `COOKIES` and paste the
     **whole contents** of the file as its value.
  Use a spare account for this, not your main one.
- **Keep yt-dlp up to date.** Sites change often. If downloads start failing, open
  Render and click **Manual Deploy → Clear build cache & deploy**. This installs the
  newest yt-dlp.
- **Run it on your own PC instead:** install Python, run `pip install -r requirements.txt`,
  set `BOT_TOKEN`, and run `python bot.py`. With no webhook URL set, it uses polling mode.
  You also need ffmpeg installed for merging and MP3.
- Only download videos you have the right to save (your own, free-to-share ones, and so on).
