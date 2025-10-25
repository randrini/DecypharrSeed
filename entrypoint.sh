#!/bin/sh
set -e

PUID=${PUID:-10001}
PGID=${PGID:-10001}

# Create group if it doesn't exist
if ! getent group app >/dev/null 2>&1; then
    groupadd -g "${PGID}" app 2>/dev/null || addgroup -g "${PGID}" app
fi

# Create user if it doesn't exist
if ! getent passwd app >/dev/null 2>&1; then
    useradd -u "${PUID}" -g "${PGID}" -m -s /bin/sh app 2>/dev/null || \
    adduser -D -u "${PUID}" -G app -s /bin/sh app
fi

# Update user/group IDs if they differ
CURRENT_UID=$(id -u app 2>/dev/null || echo 0)
CURRENT_GID=$(id -g app 2>/dev/null || echo 0)

if [ "$CURRENT_UID" != "$PUID" ] || [ "$CURRENT_GID" != "$PGID" ]; then
    groupmod -g "${PGID}" app 2>/dev/null || true
    usermod -u "${PUID}" -g "${PGID}" app 2>/dev/null || true
fi

# Fix ownership
chown -R "${PUID}:${PGID}" /data /app 2>/dev/null || true

# Execute as the app user
exec su -s /bin/sh app -c "exec $*"
