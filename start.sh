#!/bin/sh
# Starts the bot, plus a background watchdog that keeps Cloudflare WARP
# (wireproxy SOCKS5 on 127.0.0.1:40000) running. Set NO_WARP=1 to disable.
W=/tmp/warp
mkdir -p $W

port_open() {
  python -c "import socket;socket.create_connection(('127.0.0.1',40000),1)" 2>/dev/null
}

make_conf() {
  # 1) saved config (Render Secret File warp.conf) -> no registration needed
  if [ -s /etc/secrets/warp.conf ]; then
    cp /etc/secrets/warp.conf $W/wp.conf; echo "WARP: using saved warp.conf"; return 0
  fi
  # 2) register a new free WARP account
  cd $W && rm -f wgcf-account.toml wgcf-profile.conf
  if wgcf register --accept-tos >reg.log 2>&1 && wgcf generate >gen.log 2>&1; then
    { cat wgcf-profile.conf; printf '\n[Socks5]\nBindAddress = 127.0.0.1:40000\n'; } > $W/wp.conf
    echo "WARP: registered new account"; return 0
  fi
  echo "WARP: registration failed ($(grep -o '[0-9][0-9][0-9] [A-Za-z ]*' reg.log | head -1))"
  return 1
}

warp_watchdog() {
  delay=10
  while true; do
    if ! port_open; then
      if [ -s $W/wp.conf ] || make_conf; then
        wireproxy -c $W/wp.conf >>$W/wireproxy.log 2>&1 &
        sleep 5
        port_open && { echo "WARP: started"; delay=10; }
      else
        delay=$((delay * 2)); [ $delay -gt 600 ] && delay=600
        echo "WARP: retry in ${delay}s"
        sleep $delay
        continue
      fi
    fi
    sleep 30
  done
}

if [ "$NO_WARP" != "1" ] && command -v wgcf >/dev/null && command -v wireproxy >/dev/null; then
  warp_watchdog &
  for i in $(seq 1 20); do port_open && break; sleep 1; done
else
  echo "WARP: not available"
fi
cd /app && exec python bot.py
