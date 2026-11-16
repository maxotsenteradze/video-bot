#!/bin/sh
# Start Cloudflare WARP (wireproxy SOCKS5 on 127.0.0.1:40000), then the bot.
# Set NO_WARP=1 in Render to turn it off.
if [ "$NO_WARP" != "1" ] && command -v wgcf >/dev/null && command -v wireproxy >/dev/null; then
  mkdir -p /tmp/warp && cd /tmp/warp
  ok=0
  for i in 1 2 3 4 5; do
    if wgcf register --accept-tos >reg.log 2>&1 && wgcf generate >gen.log 2>&1; then ok=1; break; fi
    echo "WARP: register attempt $i failed"; cat reg.log; rm -f wgcf-account.toml; sleep $((i * 5))
  done
  if [ $ok = 1 ]; then
    { cat wgcf-profile.conf; printf '\n[Socks5]\nBindAddress = 127.0.0.1:40000\n'; } > wp.conf
    wireproxy -c wp.conf >wireproxy.log 2>&1 &
    for i in $(seq 1 20); do
      python -c "import socket;socket.create_connection(('127.0.0.1',40000),1)" 2>/dev/null && break
      sleep 1
    done
    echo "WARP: started"
  else
    echo "WARP: registration failed"
  fi
else
  echo "WARP: not available"
fi
cd /app && exec python bot.py
