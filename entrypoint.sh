#!/bin/sh
# entrypoint.sh
# Handles PUID/PGID privilege dropping for Unraid and other hosts.
# Defaults to nobody:users (99:100) — standard Unraid values.

PUID=${PUID:-99}
PGID=${PGID:-100}

echo ""
echo "-------------------------------------"
echo "      mediaMender"
echo "-------------------------------------"
echo "  PUID: ${PUID}"
echo "  PGID: ${PGID}"
echo "  Role: ${MEDIAMENDER_ROLE:-controller}"
echo "-------------------------------------"
echo ""

# Create group if GID doesn't exist
if ! getent group "${PGID}" > /dev/null 2>&1; then
    addgroup --gid "${PGID}" appgroup 2>/dev/null || true
fi

# Create user if UID doesn't exist
# Use --no-log-init and redirect warnings to suppress UID range warning
if ! getent passwd "${PUID}" > /dev/null 2>&1; then
    useradd --no-log-init -u "${PUID}" \
        -g "$(getent group ${PGID} | cut -d: -f1)" \
        --no-create-home --shell /bin/sh appuser 2>/dev/null || true
fi

# Only chown dirs we control — NOT mounted volumes (they may be read-only)
# /app/src, /app/templates etc are in the image and owned by root at build time
# /app/data may be a read-only mount so we skip it
chown -R "${PUID}:${PGID}" /app/src /app/templates /app/app.py /app/worker.py 2>/dev/null || true

if [ "${MEDIAMENDER_ROLE:-controller}" = "repair-worker" ]; then
    mkdir -p /app/data/worker
    chown -R "${PUID}:${PGID}" /app/data/worker
    set -- gunicorn --bind 0.0.0.0:8223 --workers 1 --threads 4 --timeout 3700 worker:app
fi

# Drop privileges and run the app
exec gosu "${PUID}:${PGID}" "$@"
