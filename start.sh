#!/bin/sh
# Start Cloudflare WARP (wireproxy SOCKS5 on 127.0.0.1:40000), then the bot.
# Set NO_WARP=1 in Render to turn it off.
if [ "$NO_WARP" != "1" ] && command -v wgcf >/dev/null && command -v wireproxy >/dev/null; then
  mkdir -p /tmp/warp && cd /tmp/warp
  if wgcf register --accept-tos >reg.log 2>&1 && wgcf generate >gen.log 2>&1; then
    { cat wgcf-profile.conf; printf '\n[Socks5]\nBindAddress = 127.0.0.1:40000\n'; } > wp.conf
    wireproxy -c wp.conf >wireproxy.log 2>&1 &
    sleep 3
    echo "WARP: started"
  else
    echo "WARP: registration failed"; cat reg.log gen.log
  fi
else
  echo "WARP: not available"
fi
cd /app && exec python bot.py
