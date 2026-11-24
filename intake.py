"""Customer repair-intake form: the emailed link, the saved answers, and the
write-back to Zendesk.

When a ticket is added to the repair queue the portal posts one public comment
with a private link. The customer's answers and photos are saved in
``repair.intake`` first, then written to the same ticket; anything Zendesk
refuses is retried by ``sweep`` (run from the delivery checker) or by staff.

Only the SHA-256 of the link token is stored. The raw token lives in the
outbound comment and nowhere else: never log it.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from markupsafe import escape
from psycopg.types.json import Jsonb

import files
import repair_db
import repair_settings
from db import _conn
from zendesk_client import ZendeskError

logger = logging.getLogger("intake")

SENT_TAG = "repair_intake_sent"
RECEIVED_TAG = "repair_intake_received"
LINK_DAYS = 30
MAX_PHOTOS = 6
MAX_PHOTO_BYTES = 20 * 1024 * 1024
MAX_POST_BYTES = 110 * 1024 * 1024  # under the app-wide 120 MB cap
MAX_PIXELS = 60_000_000  # HEIC photos are decoded here; refuse absurd sizes
MAX_ATTEMPTS = 8
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)

SHORT, LONG = 120, 4000
# (name, label, required, maxlen), in the order the form and the Zendesk comment show them.
FIELDS = (
    ("model", "Model", True, SHORT),
    ("serial", "Serial number", True, SHORT),
    ("caliber", "Caliber", True, SHORT),
    ("barrel_length", "Barrel length", False, SHORT),
    ("purchase_date", "Purchase date", False, SHORT),
    ("round_count", "Approximate round count", False, SHORT),
    ("ammo", "Ammunition used", False, SHORT),
    ("optic", "Optic and mount", False, SHORT),
    ("issue", "What's going on", True, LONG),
    ("check_specific", "Anything specific to look at, or concerns", False, LONG),
)
# The form asks for no contact details (name, address, phone): those stay with
# customer service in Zendesk.
CONCERN_KIND = "concern"  # repair.choice list behind the "what best describes it" tick boxes
CONCERNS_LABEL = "Concerns"

_OPEN = ("pending_link", "link_sent")
_ANSWERED = ("submitted", "synced", "sync_failed")
# Zendesk failures worth another automatic try: 5 min, 10, 20 ... capped at 6 h.
_BACKOFF = "now() + least(interval '5 minutes' * power(2, attempts), interval '6 hours')"

_PHOTO_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "pdf": "application/pdf",
}
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heif")
# Shown as "who" on events the sweep logs (no tech, and not the customer either).
_PORTAL = {"id": None, "name": "Repair portal"}


# ---------------------------------------------------------------- config (read at call time)


def enabled() -> bool:
    return os.environ.get("REPAIR_INTAKE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    """Public origin of the form. Never the request host or PORTAL_BASE_URL:
    those are the shop's internal address, which a customer cannot open."""
    return os.environ.get("REPAIR_INTAKE_BASE_URL", "").strip().rstrip("/")


def go_live_at() -> tuple[Optional[datetime], bool]:
    """(cutoff, ok). Unset = (None, True): no cutoff. Unparseable = (None, False): send nothing."""
    raw = os.environ.get("REPAIR_INTAKE_GO_LIVE_AT", "").strip()
    if not raw:
        return None, True
    when = _parse_when(raw)
    return when, when is not None


def field_ids() -> dict[str, Optional[int]]:
    out = {}
    for role in ("serial", "model", "caliber"):
        raw = os.environ.get(f"ZENDESK_INTAKE_{role.upper()}_FIELD_ID", "").strip()
        out[role] = int(raw) if raw.isdigit() else None
    return out


def reminder_hours() -> float:
    try:
        return max(float(os.environ.get("REPAIR_INTAKE_REMINDER_HOURS", "") or 0), 0.0)
    except ValueError:
        return 0.0


def _portal_tag() -> str:
    return os.environ.get("PORTAL_TAG", "repair-portal")


def _parse_when(raw) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _fill(template: str, values: dict[str, Any]) -> str:
    """Fill {placeholders}; unknown ones stay as typed (same as portal_views.render_message)."""

    def sub(match):
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    text = re.sub(r"\{(\w+)\}", sub, template)
    return re.sub(r"(?<=\S) {2,}(?=\S)", " ", text)


def _first_name(requester) -> str:
    name = ((requester or {}).get("name") or "").strip()
    if not name or "@" in name:  # Zendesk uses the email as the name when it has nothing else
        return "there"
    return name.split()[0]


# ---------------------------------------------------------------- token


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """(raw token for the link, sha256 hex to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


# ---------------------------------------------------------------- link


def eligible(ticket: dict[str, Any]) -> Optional[str]:
    """Why this ticket gets no automatic link, or None when it should get one."""
    if not enabled():
        return "off"
    if not base_url():
        return "no_base_url"
    cutoff, ok = go_live_at()
    if not ok:
        return "bad_go_live"
    if cutoff is not None:
        created = _parse_when(ticket.get("created_at") or "")
        # A ticket whose age can't be read is treated as old: the cutoff exists
        # to keep tickets from before go-live from being emailed.
        if created is None or created < cutoff:
            return "before_go_live"
    if ticket.get("status") in ("solved", "closed"):
        return "ticket_done"
    if SENT_TAG in (ticket.get("tags") or []):
        return "already_tagged"
    return None


def link_url(raw: str) -> str:
    return f"{base_url()}/repair/intake/{raw}"


def _post_link(zendesk, ticket_id: int, requester, raw: str, message_key: str) -> None:
    settings = repair_settings.load()
    body = _fill(
        settings.get(message_key) or repair_settings.DEFAULTS[message_key],
        {"first_name": _first_name(requester), "ticket_id": str(ticket_id), "link": link_url(raw),
         "days": str(LINK_DAYS)},
    ).strip()
    # Comment and tag in one PUT, so the tag is proof the comment landed.
    zendesk.post_update(ticket_id, body, public=True, tags=[_portal_tag(), SENT_TAG])


def _link_ok(conn, intake_id: int, ticket_id: int, tech, label: str) -> None:
    conn.execute(
        "UPDATE repair.intake SET status = 'link_sent', link_sent_at = now(), "
        "link_send_count = link_send_count + 1, attempts = 0, next_attempt_at = NULL, last_error = NULL, "
        "updated_at = now() WHERE id = %s",
        (intake_id,),
    )
    _event(conn, ticket_id, label, tech or _PORTAL)


def _link_failed(conn, intake_id: int, exc: ZendeskError, auto_retry: bool) -> None:
    # pending_link (not link_sent): the stored hash may be one no email carries.
    conn.execute(
        "UPDATE repair.intake SET status = 'pending_link', last_error = %s, attempts = attempts + 1, "
        f"next_attempt_at = CASE WHEN %s AND attempts + 1 < %s THEN {_BACKOFF} END, updated_at = now() "
        "WHERE id = %s",
        (str(exc)[:500], auto_retry and _retryable(exc), MAX_ATTEMPTS, intake_id),
    )


def send_link(zendesk, ticket: dict[str, Any], requester, tech) -> str:
    """Email the intake link for a ticket just added to the queue.

    Returns 'sent', 'off', 'no_base_url', 'bad_go_live', 'before_go_live',
    'ticket_done', 'already_tagged', 'exists' or 'failed'. Never raises
    ZendeskError: a failed post leaves the row at pending_link for the sweep.
    """
    reason = eligible(ticket)
    if reason:
        return reason
    ticket_id = ticket["id"]
    raw, hashed = new_token()
    # Committed before the post. The insert picks the one poster when two techs
    # add at once, and next_attempt_at lets the sweep finish the job if this
    # request dies mid-post (it checks the ticket's tag before posting again).
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO repair.intake (ticket_id, token_hash, expires_at, created_by, next_attempt_at) "
            "VALUES (%s, %s, now() + make_interval(days => %s), %s, now() + interval '10 minutes') "
            "ON CONFLICT (ticket_id) DO NOTHING RETURNING id",
            (ticket_id, hashed, LINK_DAYS, (tech or {}).get("name")),
        ).fetchone()
    if row is None:
        return "exists"
    try:
        _post_link(zendesk, ticket_id, requester, raw, "intake_link_message")
    except ZendeskError as exc:
        logger.warning("RI-%s: intake link for ticket %s was not posted: %s", row["id"], ticket_id, exc)
        with _conn() as conn:
            _link_failed(conn, row["id"], exc, auto_retry=True)
        return "failed"
    with _conn() as conn:
        with conn.transaction():
            _link_ok(conn, row["id"], ticket_id, tech, "Intake form link sent")
    return "sent"


def resend_link(zendesk, ticket: dict[str, Any], requester, tech=None, manual: bool = False) -> str:
    """Retry a link that never went out (the sweep) or send a new one (staff button).

    Returns 'sent'; 'recovered' (the first post had landed after all, nothing
    new sent); 'failed'; or why nothing was done: 'submitted', 'ticket_done',
    'no_base_url', 'no_rma', 'no_intake', 'exists', or a reason from eligible().
    A new link replaces the old one: only one token hash is kept.
    """
    ticket_id = ticket["id"]
    with repair_db.ticket_lock(ticket_id) as conn:
        row = conn.execute(
            "SELECT id, status, link_send_count FROM repair.intake WHERE ticket_id = %s FOR UPDATE",
            (ticket_id,),
        ).fetchone()
        if row and row["status"] in _ANSWERED:
            return "submitted"
        if ticket.get("status") in ("solved", "closed"):
            if row and not manual:
                _stop_link_retry(conn, row["id"], "The ticket is solved or closed.")
            return "ticket_done"
        if not base_url():
            return "no_base_url"
        if not manual:
            if row is None:
                return "no_intake"
            if row["status"] != "pending_link":
                return "exists"
            if SENT_TAG in (ticket.get("tags") or []):
                # The earlier PUT landed and only its response was lost. The
                # stored hash is the one in that email, so keep it.
                _link_ok(conn, row["id"], ticket_id, tech, "Intake form link sent")
                return "recovered"
            reason = eligible(ticket)
            if reason:
                _stop_link_retry(conn, row["id"], f"No link sent ({reason}).")
                return reason
        raw, hashed = new_token()
        had_link = bool(row) and row["status"] == "link_sent"
        rotate = row is not None
        if row is None:
            # Ticket was added while the form was switched off.
            if not conn.execute("SELECT 1 FROM repair.rma WHERE ticket_id = %s", (ticket_id,)).fetchone():
                return "no_rma"
            row = conn.execute(
                "INSERT INTO repair.intake (ticket_id, token_hash, expires_at, created_by) "
                "VALUES (%s, %s, now() + make_interval(days => %s), %s) RETURNING id, status, link_send_count",
                (ticket_id, hashed, LINK_DAYS, (tech or {}).get("name")),
            ).fetchone()

        def store_new_token():
            if rotate:
                conn.execute(
                    "UPDATE repair.intake SET token_hash = %s, expires_at = now() + make_interval(days => %s), "
                    "updated_at = now() WHERE id = %s",
                    (hashed, LINK_DAYS, row["id"]),
                )

        # "The earlier one no longer works" only makes sense if there was one.
        again = row["link_send_count"] > 0
        try:
            _post_link(zendesk, ticket_id, requester, raw,
                       "intake_resend_message" if again else "intake_link_message")
        except ZendeskError as exc:
            logger.warning("RI-%s: intake link for ticket %s was not posted: %s", row["id"], ticket_id, exc)
            if had_link and exc.status is not None and 400 <= exc.status < 500:
                # Zendesk answered and refused, so no new email exists: the
                # link the customer already has must keep working.
                conn.execute(
                    "UPDATE repair.intake SET last_error = %s, updated_at = now() WHERE id = %s",
                    (str(exc)[:500], row["id"]),
                )
                return "failed"
            store_new_token()
            # After a staff send the tag can no longer tell a landed post from a
            # failed one, so only the sweep's own sends are retried automatically.
            _link_failed(conn, row["id"], exc, auto_retry=not manual)
            return "failed"
        store_new_token()
        _link_ok(conn, row["id"], ticket_id, tech,
                 "New intake form link sent" if again else "Intake form link sent")
    return "sent"


def _stop_link_retry(conn, intake_id: int, why: str) -> None:
    conn.execute(
        "UPDATE repair.intake SET next_attempt_at = NULL, last_error = %s, updated_at = now() WHERE id = %s",
        (why, intake_id),
    )


# ---------------------------------------------------------------- reads

_FILE_COUNT = "(SELECT count(*) FROM repair.intake_file f WHERE f.intake_id = i.id) AS file_count"


def find_by_token(raw: str) -> Optional[dict[str, Any]]:
    """The intake a link points at, with the rifle details to prefill, or None."""
    if not isinstance(raw, str) or not TOKEN_RE.match(raw):
        return None
    with _conn() as conn:
        return conn.execute(
            f"SELECT i.*, {_FILE_COUNT}, r.zd_model, r.zd_serial, r.zd_caliber "
            "FROM repair.intake i JOIN repair.rma r ON r.ticket_id = i.ticket_id WHERE i.token_hash = %s",
            (hash_token(raw),),
        ).fetchone()


def for_ticket(ticket_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            f"SELECT i.*, {_FILE_COUNT} FROM repair.intake i WHERE i.ticket_id = %s", (ticket_id,)
        ).fetchone()


def needs_attention() -> list[dict[str, Any]]:
    """Intakes staff should look at: failed write-backs, stuck submissions, unsent links."""
    with _conn() as conn:
        return conn.execute(
            "SELECT i.id, i.ticket_id, i.status, i.last_error, i.attempts, i.next_attempt_at, "
            "i.submitted_at, r.requester_name "
            "FROM repair.intake i JOIN repair.rma r ON r.ticket_id = i.ticket_id "
            "WHERE i.status = 'sync_failed' "
            "   OR (i.status = 'submitted' AND (i.claimed_until IS NULL OR i.claimed_until < now()) "
            "       AND i.submitted_at < now() - interval '10 minutes') "
            "   OR (i.status = 'pending_link' AND i.last_error IS NOT NULL) "
            "ORDER BY i.ticket_id"
        ).fetchall()


def is_open(row: dict[str, Any]) -> bool:
    """True while the customer can still fill in the form."""
    return row["status"] in _OPEN and row["expires_at"] > datetime.now(timezone.utc)


# ---------------------------------------------------------------- form


def validate(form) -> tuple[dict[str, str], dict[str, str]]:
    """Clean the posted answers. Returns (answers for every field, {field: error message})."""
    answers, errors = {}, {}
    for name, label, required, maxlen in FIELDS:
        value = str(form.get(name) or "").replace("\x00", "")  # Postgres jsonb rejects NUL
        if maxlen > SHORT:
            value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        else:
            value = " ".join(value.split())
        answers[name] = value[:maxlen]
        if required and not answers[name]:
            errors[name] = f"Please fill in: {label}"
    return answers, errors


def concern_choices() -> list[str]:
    """Labels of the tick boxes on the form, in Settings order."""
    return [c["label"] for c in repair_db.list_choices(CONCERN_KIND)]


def pick_concerns(posted, choices: Optional[list[str]] = None) -> list[str]:
    """The ticked concerns, kept only if they are on the list, in list order."""
    wanted = {str(v) for v in posted or []}
    return [label for label in (concern_choices() if choices is None else choices) if label in wanted]


def _concerns(answers: dict[str, Any]) -> str:
    picked = (answers or {}).get("concerns")
    return ", ".join(str(c) for c in picked) if isinstance(picked, list) else ""


def sniff(data: bytes) -> Optional[str]:
    """File type from the bytes themselves: jpg, png, webp, heic, pdf, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "heic"
    if b"%PDF-" in data[:1024]:
        return "pdf"
    return None


def read_photos(file_storages) -> tuple[list[files.Upload], Optional[str]]:
    """Validate the customer's files. Returns (uploads, error message).

    The type comes from the bytes, never the name or the browser's
    Content-Type, and the stored filename is ours.
    """
    picked = [f for f in file_storages if f and f.filename]
    if len(picked) > MAX_PHOTOS:
        return [], f"Please attach at most {MAX_PHOTOS} files."
    uploads: list[files.Upload] = []
    for f in picked:
        shown = files._safe_name(f.filename)
        data = f.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            return [], f"“{shown}” is over {MAX_PHOTO_BYTES // (1024 * 1024)} MB."
        if not data:
            continue
        ext = sniff(data)
        if ext is None:
            return [], f"“{shown}” isn't a file we can take. Please send photos (JPG, PNG, WEBP, HEIC) or a PDF."
        upload = files.Upload(f"photo-{len(uploads) + 1}.{ext}", data, _PHOTO_TYPES[ext])
        if ext == "heic":
            upload = _convert_heic(upload)
        uploads.append(upload)
    return uploads, None


def _convert_heic(upload: files.Upload) -> files.Upload:
    """HEIC to JPEG so staff can see it. Oversized or unreadable files go to Zendesk as they are."""
    try:
        from PIL import Image
        from pillow_heif import register_heif_opener
    except ImportError:
        return upload
    try:
        register_heif_opener()
        with Image.open(io.BytesIO(upload.data)) as img:  # reads the header only
            width, height = img.size
    except Exception:
        return upload
    if width * height > MAX_PIXELS:
        return upload
    return files._maybe_convert(upload, "heic")


def save_submission(intake_id: int, answers: dict[str, str], uploads: list[files.Upload]) -> bool:
    """Store the answers and photo bytes. False if the form was already sent or the link ran out."""
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "UPDATE repair.intake SET status = 'submitted', answers = %s, submitted_at = now(), "
                "attempts = 0, next_attempt_at = now(), last_error = NULL, updated_at = now() "
                "WHERE id = %s AND status IN ('pending_link', 'link_sent') AND expires_at > now() "
                "RETURNING id",
                (Jsonb({**answers, "photo_count": len(uploads)}), intake_id),
            ).fetchone()
            if row is None:
                return False
            for position, up in enumerate(uploads):
                conn.execute(
                    "INSERT INTO repair.intake_file (intake_id, position, filename, content_type, size_bytes, data) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (intake_id, position, up.filename, up.content_type, len(up.data), up.data),
                )
    return True


# ---------------------------------------------------------------- sync (write-back to Zendesk)


def comment_html(row: dict[str, Any]) -> str:
    """The customer's answers as the Zendesk comment. Every value is escaped."""
    answers = row.get("answers") or {}
    parts = ["<p><strong>Repair intake form</strong></p>"]
    if _concerns(answers):
        parts.append(f"<p><strong>{CONCERNS_LABEL}:</strong> {escape(_concerns(answers))}</p>")
    for name, label, _required, _maxlen in FIELDS:
        value = str(answers.get(name) or "").strip()
        if not value:
            continue
        shown = str(escape(value)).replace("\n", "<br>")
        parts.append(f"<p><strong>{escape(label)}:</strong> {shown}</p>")
    # The visible reference line and the marker are how a retry sees this
    # comment is already on the ticket (Zendesk may strip the HTML comment).
    parts.append(f"<p>Intake form reference RI-{int(row['id'])}</p>")
    parts.append(f"<!-- intake:{int(row['id'])} -->")
    return "\n".join(parts)


def comment_text(row: dict[str, Any]) -> str:
    """Plain-text version of the answers."""
    answers = row.get("answers") or {}
    lines = ["Repair intake form", ""]
    if _concerns(answers):
        lines.append(f"{CONCERNS_LABEL}: {_concerns(answers)}")
    for name, label, _required, _maxlen in FIELDS:
        value = str(answers.get(name) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    lines += ["", f"Intake form reference RI-{int(row['id'])}"]
    return "\n".join(lines)


def field_values(answers: dict[str, Any]) -> dict[int, str]:
    """Zendesk custom fields to set from the answers (only the ids set in the environment)."""
    out = {}
    for role, fid in field_ids().items():
        value = str((answers or {}).get(role) or "").strip()
        if fid is not None and value:
            out[fid] = value
    return out


def _retryable(exc) -> bool:
    """Network trouble, rate limits and Zendesk outages pass; 401/403/404/422 need a person."""
    if not isinstance(exc, ZendeskError):
        return False
    return exc.status is None or exc.status == 429 or exc.status >= 500


def _claim(intake_id: int, force: bool = False) -> Optional[dict[str, Any]]:
    """Take the 10-minute sync lease. None when another sync holds it or there is nothing to sync.

    The lease, not a held connection or lock, is what keeps the inline attempt,
    the sweep and the staff button apart while photos upload. ``force`` skips
    the schedule in sweep(); it never steals a live lease.
    """
    with _conn() as conn:
        return conn.execute(
            "UPDATE repair.intake SET claimed_until = now() + interval '10 minutes', attempts = attempts + 1, "
            "updated_at = now() "
            "WHERE id = %s AND status IN ('submitted', 'sync_failed') "
            "AND (claimed_until IS NULL OR claimed_until < now()) RETURNING *",
            (intake_id,),
        ).fetchone()


def _renew(intake_id: int, lease) -> Optional[datetime]:
    """Push this attempt's lease out another 10 minutes. None if another sync has taken the row."""
    with _conn() as conn:
        row = conn.execute(
            "UPDATE repair.intake SET claimed_until = now() + interval '10 minutes', updated_at = now() "
            "WHERE id = %s AND status IN ('submitted', 'sync_failed') AND claimed_until = %s "
            "RETURNING claimed_until",
            (intake_id, lease),
        ).fetchone()
    return row["claimed_until"] if row else None


def _already_posted(zendesk, ticket: dict[str, Any], intake_id: int) -> bool:
    """True if the answers are on the ticket already (the PUT landed but our update didn't)."""
    if RECEIVED_TAG in (ticket.get("tags") or []):
        return True
    marks = re.compile(rf"(intake:{intake_id}|Intake form reference RI-{intake_id})(?!\d)")
    comments, _users = zendesk.get_comments(ticket["id"])
    return any(marks.search(f"{c.get('html_body') or ''}\n{c.get('body') or ''}") for c in comments)


def _mark_synced(intake_id: int, note: Optional[str] = None) -> None:
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "UPDATE repair.intake SET status = 'synced', synced_at = now(), claimed_until = NULL, "
                "next_attempt_at = NULL, last_error = %s, updated_at = now() "
                "WHERE id = %s AND status IN ('submitted', 'sync_failed') RETURNING ticket_id",
                (note, intake_id),
            ).fetchone()
            if row is None:
                return
            # The photos are in Zendesk now; they were only kept for retries.
            conn.execute("DELETE FROM repair.intake_file WHERE intake_id = %s", (intake_id,))
            _event(conn, row["ticket_id"], "Customer intake form received")


def _fail(intake_id: int, exc_or_msg, permanent: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE repair.intake SET status = 'sync_failed', claimed_until = NULL, last_error = %s, "
            f"next_attempt_at = CASE WHEN %s OR attempts >= %s THEN NULL ELSE {_BACKOFF} END, "
            "updated_at = now() WHERE id = %s AND status IN ('submitted', 'sync_failed')",
            (str(exc_or_msg)[:500], bool(permanent), MAX_ATTEMPTS, intake_id),
        )


def sync(zendesk, intake_id: int, force: bool = False) -> str:
    """Write a submitted intake to its Zendesk ticket.

    Returns 'synced', 'already' (it was on the ticket; nothing posted),
    'failed' (recorded on the row) or 'busy' (another sync has it, or there
    is nothing to sync). Raises only if the database itself fails.
    """
    row = _claim(intake_id, force)
    if row is None:
        with _conn() as conn:
            now = conn.execute("SELECT status FROM repair.intake WHERE id = %s", (intake_id,)).fetchone()
        return "already" if now and now["status"] == "synced" else "busy"
    ticket_id = row["ticket_id"]
    try:
        ticket, requester, _org = zendesk.get_ticket(ticket_id)
        if ticket.get("status") == "closed":
            _fail(intake_id, "The Zendesk ticket is closed, so the answers can't be added to it.", permanent=True)
            return "failed"
        if _already_posted(zendesk, ticket, intake_id):
            _mark_synced(intake_id)
            return "already"
        with _conn() as conn:
            stored = conn.execute(
                "SELECT filename, content_type, data FROM repair.intake_file WHERE intake_id = %s "
                "ORDER BY position, id",
                (intake_id,),
            ).fetchall()
        # Uploaded fresh on every attempt: Zendesk's upload tokens last an hour.
        lease, tokens = row["claimed_until"], []
        for f in stored:
            tokens.append(zendesk.upload_file(f["filename"], bytes(f["data"]), f["content_type"]))
            lease = _renew(intake_id, lease)  # a big photo on a slow line can outlast the lease
            if lease is None:
                logger.warning("RI-%s: another sync took over ticket %s; not posting", intake_id, ticket_id)
                return "busy"
        update = dict(
            public=True,
            html_body=comment_html(row),
            # Posted as the customer: these are their words, and Zendesk does
            # not email a requester a copy of their own comment.
            author_id=(requester or {}).get("id") or ticket.get("requester_id"),
            tags=[_portal_tag(), RECEIVED_TAG],
            uploads=tokens,
        )
        fields = field_values(row.get("answers") or {})
        note = None
        try:
            zendesk.post_update(ticket_id, None, custom_fields=fields, **update)
        except ZendeskError as exc:
            if exc.status != 422 or not fields:
                raise
            # Most likely a field that won't take free text; the answers matter more.
            zendesk.post_update(ticket_id, None, custom_fields={}, **update)
            note = f"Posted without the Zendesk ticket fields, which Zendesk refused: {exc}"[:500]
        _mark_synced(intake_id, note)
        logger.info("RI-%s: intake form written to ticket %s", intake_id, ticket_id)
        return "synced"
    except ZendeskError as exc:
        logger.warning("RI-%s: write-back to ticket %s failed: %s", intake_id, ticket_id, exc)
        _fail(intake_id, exc, permanent=not _retryable(exc))
        return "failed"
    except psycopg.Error:
        raise
    except Exception as exc:  # a bug here must not strand the row behind its lease
        logger.exception("RI-%s: write-back to ticket %s failed", intake_id, ticket_id)
        _fail(intake_id, f"Unexpected error: {exc}", permanent=False)
        return "failed"


# ---------------------------------------------------------------- background


def sweep(zendesk, force: bool = False) -> dict[str, int]:
    """One pass over everything that is due. Returns counts of what happened.

    Keys (present only when non-zero): synced, failed, links, expired, reminded.
    ``force`` (the staff "Retry all" button) also retries write-backs that are
    not due yet or have stopped retrying.
    """
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    try:
        with _conn() as conn:
            expired = conn.execute(
                "UPDATE repair.intake SET status = 'expired', next_attempt_at = NULL, updated_at = now() "
                "WHERE status IN ('pending_link', 'link_sent') AND expires_at < now() RETURNING id"
            ).fetchall()
            due = conn.execute(
                "SELECT id FROM repair.intake WHERE (claimed_until IS NULL OR claimed_until < now()) "
                "AND (status = 'submitted' OR (status = 'sync_failed' AND (%s OR next_attempt_at <= now()))) "
                "ORDER BY id",
                (bool(force),),
            ).fetchall()
    except psycopg.errors.UndefinedTable:
        # The delivery checker does not run migrations; it may start first.
        logger.info("repair.intake does not exist yet; run the migrations.")
        return counts
    if expired:
        counts["expired"] = len(expired)

    for row in due:
        try:
            result = sync(zendesk, row["id"], force=force)
        except Exception:
            logger.exception("RI-%s: sync failed", row["id"])
            result = "failed"
        if result in ("synced", "already"):
            bump("synced")
        elif result == "failed":
            bump("failed")

    if enabled():
        _sweep_links(zendesk, bump)
        hours = reminder_hours()
        if hours > 0:
            _sweep_reminders(zendesk, hours, bump)
    return counts


def _sweep_links(zendesk, bump) -> None:
    """Retry link comments that failed (or whose request died) in the last day."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ticket_id FROM repair.intake WHERE status = 'pending_link' "
            "AND next_attempt_at <= now() AND created_at > now() - interval '24 hours' ORDER BY id"
        ).fetchall()
    for row in rows:
        try:
            ticket, requester, _org = zendesk.get_ticket(row["ticket_id"])
            if resend_link(zendesk, ticket, requester, manual=False) in ("sent", "recovered"):
                bump("links")
        except Exception:
            logger.exception("RI-%s: link retry failed", row["id"])


def _sweep_reminders(zendesk, hours: float, bump) -> None:
    """One nudge, without the link, for forms not filled in after N hours."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ticket_id FROM repair.intake WHERE status = 'link_sent' AND reminder_sent_at IS NULL "
            "AND expires_at > now() AND link_sent_at < now() - make_interval(secs => %s) "
            # Only links that came due in the last day, so switching reminders
            # on later does not nudge every old link at once.
            "AND link_sent_at > now() - make_interval(secs => %s) - interval '24 hours' ORDER BY id",
            (hours * 3600, hours * 3600),
        ).fetchall()
    for row in rows:
        try:
            # Stamped before the post: a reminder that fails is skipped, never sent twice.
            with _conn() as conn:
                mine = conn.execute(
                    "UPDATE repair.intake SET reminder_sent_at = now(), updated_at = now() "
                    "WHERE id = %s AND status = 'link_sent' AND reminder_sent_at IS NULL RETURNING id",
                    (row["id"],),
                ).fetchone()
            if mine is None:
                continue
            ticket, requester, _org = zendesk.get_ticket(row["ticket_id"])
            if ticket.get("status") in ("solved", "closed"):
                continue
            settings = repair_settings.load()
            body = _fill(
                settings.get("intake_reminder_message") or repair_settings.DEFAULTS["intake_reminder_message"],
                {"first_name": _first_name(requester), "ticket_id": str(row["ticket_id"]), "link": "",
                 "days": str(LINK_DAYS)},
            ).strip()
            zendesk.post_update(row["ticket_id"], body, public=True, tags=[_portal_tag()])
            with _conn() as conn:
                _event(conn, row["ticket_id"], "Intake form reminder sent", _PORTAL)
            bump("reminded")
        except Exception:
            logger.exception("RI-%s: reminder failed", row["id"])


def _event(conn, ticket_id: int, label: str, tech=None, detail=None) -> None:
    """Log an intake step on the repair record. No tech means the customer did it."""
    conn.execute(
        "INSERT INTO repair.event (ticket_id, kind, label, tech_id, tech_name, detail) "
        "VALUES (%s, 'intake', %s, %s, %s, %s)",
        (ticket_id, label, tech["id"] if tech else None, tech["name"] if tech else "Customer",
         Jsonb(detail or {})),
    )
