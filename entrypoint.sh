#!/bin/sh
# entrypoint: adapt container user/group to PUID/PGID and chown data dirs,
# then drop privileges and exec the container command as that user.
set -eu

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"

# Try to create the group if it doesn't exist
if ! getent group "${PGID}" >/dev/null 2>&1; then
  if command -v groupadd >/dev/null 2>&1; then
    groupadd -g "${PGID}" app || true
  else
    addgroup -g "${PGID}" app || true
  fi
fi

# Create or modify the user "app" to match PUID/PGID
if ! id -u app >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd -u "${PUID}" -g "${PGID}" -m -s /usr/sbin/nologin app || true
  else
    adduser -u "${PUID}" -G app -D -H app || true
  fi
else
  if command -v usermod >/dev/null 2>&1; then
    usermod -u "${PUID}" -g "${PGID}" app || true
  fi
fi

# Ensure ownership of important dirs so the app user can write to them
chown -R "${PUID}:${PGID}" /data /app || true

# If running as root, drop to PUID/PGID and exec the given command.
# Use Python to perform setgid/setuid then exec the command.
if [ "$(id -u)" = '0' ]; then
  # If no CMD provided, default to python /app/app.py (keeps previous behavior)
  if [ "$#" -eq 0 ]; then
    set -- python /app/app.py
  fi
  exec python - <<'PY' "$@"
import os, sys
try:
    uid = int(os.environ.get("PUID", "10001"))
    gid = int(os.environ.get("PGID", "10001"))
    os.setgid(gid)
    os.setuid(uid)
except Exception as e:
    print("entrypoint: failed to drop privileges:", e, file=sys.stderr)
# Execute the requested command
os.execvp(sys.argv[1], sys.argv[1:])
PY
else
  # Already running as non-root (unlikely in this setup) — just exec
  exec "$@"
fi
