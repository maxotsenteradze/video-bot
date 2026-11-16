FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && rm -rf /var/lib/apt/lists/*
# Cloudflare WARP as a local SOCKS5 proxy (YouTube blocks Render's own IP).
# If a download fails the image still builds; the bot just runs without WARP.
RUN (curl -fsSL -o /usr/local/bin/wgcf https://github.com/ViRb3/wgcf/releases/download/v2.2.30/wgcf_2.2.30_linux_amd64 \
      && chmod +x /usr/local/bin/wgcf) || (rm -f /usr/local/bin/wgcf; echo "wgcf download failed") ; \
    (curl -fsSL https://github.com/whyvl/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz \
      | tar -xz -C /usr/local/bin wireproxy && chmod +x /usr/local/bin/wireproxy) || echo "wireproxy download failed"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -U "yt-dlp[default]"
COPY bot.py start.sh ./
RUN chmod +x start.sh
CMD ["./start.sh"]
