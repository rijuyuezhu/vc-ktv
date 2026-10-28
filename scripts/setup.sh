#!/usr/bin/env bash
set -eu
umask 077
CONFIG="${VC_KTV_CONFIG:-$HOME/.config/vc-ktv}"
mkdir -p "$CONFIG"
chmod 700 "$CONFIG"
if [ ! -e "$CONFIG/admin-password" ]; then
  python3 -c 'import secrets,sys;open(sys.argv[1],"w").write(secrets.token_urlsafe(18)+"\n")' "$CONFIG/admin-password"
  chmod 600 "$CONFIG/admin-password"
fi
if [ ! -e "$CONFIG/token-secret" ]; then
  python3 -c 'import secrets,sys;open(sys.argv[1],"w").write(secrets.token_hex(32)+"\n")' "$CONFIG/token-secret"
  chmod 600 "$CONFIG/token-secret"
fi
chmod 600 "$CONFIG/admin-password" "$CONFIG/token-secret"
echo "Configuration initialized at $CONFIG"
echo "Read administrator password with: cat $CONFIG/admin-password"
