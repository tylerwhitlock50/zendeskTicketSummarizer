#!/bin/sh
set -e

if [ -n "$DATABASE_URL" ] || [ -n "$RTM_DATABASE_URL" ]; then
    echo "Running database migrations..."
    python migrations/migrate.py
else
    echo "WARNING: DATABASE_URL not set; skipping migrations (scan portal quick actions disabled)." >&2
fi

exec gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --timeout 120 app:app
