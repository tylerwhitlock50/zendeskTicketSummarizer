#!/bin/sh
set -e

if [ -n "$RTM_DATABASE_URL" ]; then
    echo "Running RTM database migrations..."
    python migrations/migrate.py
else
    echo "WARNING: RTM_DATABASE_URL not set; skipping migrations (RTM tracker disabled)." >&2
fi

exec gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --timeout 120 app:app
