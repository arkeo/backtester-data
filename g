#!/bin/sh
# Bootstrap run once from the provider's console.
#
# Everything it does is written to a file inside the folder the web server
# already hands out, so whoever asked for it can see how far it got from the
# outside — no shell, no login. That is the whole point: the person running
# this cannot read a console, and the person who can read the result has no
# way in.
#
# Idempotent. Running it twice is harmless.
D=/srv/backtest/files
S=$D/_status.txt
say() { echo "$1" >> "$S" 2>/dev/null; echo "$1"; }

mkdir -p "$D" 2>/dev/null
: > "$S" 2>/dev/null
say "started $(date -u '+%Y-%m-%d %H:%M') UTC"

K='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDjSMupLPVwl+T2D3Z8iIN1Kce6lj3K6Mvruz0dfEMox irvps'
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if grep -qF 'irvps' /root/.ssh/authorized_keys 2>/dev/null; then
  say "1 key already present"
else
  echo "$K" >> /root/.ssh/authorized_keys && say "1 key added" || say "1 KEY FAILED"
fi
chmod 600 /root/.ssh/authorized_keys 2>/dev/null
systemctl restart ssh 2>/dev/null && say "2 ssh restarted" || say "2 SSH RESTART FAILED"

U=https://github.com/arkeo/backtester-data/releases/download/tools/sync_mirror.py
if curl -fsSL -o /tmp/sm.py "$U"; then
  say "3 downloaded $(md5sum /tmp/sm.py | cut -c1-8)"
  if install -m 0755 /tmp/sm.py /opt/backtest/sync_mirror.py; then
    say "4 installed"
    systemctl start --no-block backtest-sync 2>/dev/null \
      && say "5 sync started" || say "5 SYNC START FAILED"
  else
    say "4 INSTALL FAILED"
  fi
else
  say "3 DOWNLOAD FAILED - the server cannot reach github.com"
fi
say "finished"
