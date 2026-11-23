"""Who is posting: tech sign-in (name + PIN), the Settings password, and CSRF.

Techs sign in on a shop device with their name and PIN; the sign-in lasts a
shift (TECH_SESSION_HOURS, default 12) in the signed session cookie, and is
re-checked against the database on every request, so deactivating someone or
changing their PIN signs them out everywhere. Settings pages need
ADMIN_PASSWORD when it is set. Every POST must carry the session's CSRF token
(templates render it with ``csrf_field()``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for
from markupsafe import Markup

import db
import repair_db

# ---------------------------------------------------------------- CSRF


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_field() -> Markup:
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


def check_csrf() -> None:
    """before_request hook: reject state-changing requests without the token."""
    if request.method != "POST" or not current_app.config.get("CSRF_CHECK", True):
        return
    sent = request.form.get("_csrf", "")
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, "This form expired. Go back, refresh the page and try again.")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- techs


def _hours(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def sign_in_tech(tech: dict) -> None:
    session["tech"] = {
        "id": tech["id"],
        "name": tech["name"],
        "at": time.time(),
        "fp": _fingerprint(tech["pin_hash"]),
    }


def sign_out_tech() -> None:
    session.pop("tech", None)


def current_tech() -> dict | None:
    """The signed-in tech for this device, or None.

    None when there is no sign-in, it is older than TECH_SESSION_HOURS, or the
    person was deactivated or given a new PIN since signing in.
    """
    if "current_tech" in g:
        return g.current_tech
    g.current_tech = _load_tech()
    return g.current_tech


def _load_tech() -> dict | None:
    tech = session.get("tech")
    if not tech or not db.available():
        return None
    if time.time() - float(tech.get("at", 0)) > _hours("TECH_SESSION_HOURS", 12) * 3600:
        session.pop("tech", None)
        return None
    row = repair_db.get_tech_auth(int(tech["id"]))
    if not row or not row["active"] or _fingerprint(row["pin_hash"]) != tech.get("fp"):
        session.pop("tech", None)
        return None
    return {"id": row["id"], "name": row["name"]}


# ---------------------------------------------------------------- admin

ADMIN_MAX_FAILS = 5
ADMIN_LOCK_SECONDS = 600
_admin_fails: dict[str, list[float]] = {}
_admin_lock = threading.Lock()


def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "")


def is_admin() -> bool:
    """Open when ADMIN_PASSWORD is unset; else a recent login with the CURRENT password."""
    password = admin_password()
    if not password:
        return True
    grant = session.get("admin")
    if not isinstance(grant, dict):
        return False
    fresh = time.time() - float(grant.get("at", 0)) < _hours("ADMIN_SESSION_HOURS", 8) * 3600
    return fresh and grant.get("fp") == _fingerprint(password)


def admin_login_locked(client: str) -> bool:
    now = time.time()
    with _admin_lock:
        fails = [t for t in _admin_fails.get(client, []) if now - t < ADMIN_LOCK_SECONDS]
        _admin_fails[client] = fails
        return len(fails) >= ADMIN_MAX_FAILS


def try_admin_login(password: str, client: str) -> bool:
    """Check the Settings password, with a per-client lockout after repeated misses."""
    expected = admin_password()
    if admin_login_locked(client):
        return False
    if expected and hmac.compare_digest(password.encode(), expected.encode()):
        with _admin_lock:
            _admin_fails.pop(client, None)
        session["admin"] = {"at": time.time(), "fp": _fingerprint(expected)}
        return True
    with _admin_lock:
        _admin_fails.setdefault(client, []).append(time.time())
    return False


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped
