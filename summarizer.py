"""Summarize a Zendesk ticket conversation with OpenAI."""

import json
import os

from openai import OpenAI

DEFAULT_MODEL = "gpt-4o-mini"
MAX_TRANSCRIPT_CHARS = 12000

SYSTEM_PROMPT = """\
You summarize customer support tickets for a printable one-page reference sheet.
Only state facts that appear in the ticket content provided; never invent details.
Respond with a JSON object containing exactly these keys:
- "issue_summary": 2-4 sentences describing the customer's issue and its cause if known.
- "key_points": array of 3-6 short bullet strings covering what happened and what was tried.
- "current_status": 1-2 sentences on where things stand now (resolution, or next step owed and by whom).
- "product_context": one sentence tying the product information to the issue ("" if no product info).
Keep the whole summary concise enough to fit on one printed page."""


def build_transcript(comments, users_by_id, max_chars=MAX_TRANSCRIPT_CHARS):
    """Render the conversation as plain text, truncating the middle of huge threads."""
    lines = []
    for comment in comments:
        author = users_by_id.get(comment.get("author_id")) or {}
        name = author.get("name") or f"User {comment.get('author_id')}"
        role = author.get("role") or "unknown"
        visibility = "public reply" if comment.get("public") else "internal note"
        body = (comment.get("plain_body") or comment.get("body") or "").strip()
        lines.append(f"[{comment.get('created_at', '')}] {name} ({role}, {visibility}):\n{body}")
    transcript = "\n\n".join(lines)
    if len(transcript) > max_chars:
        half = max_chars // 2
        transcript = (
            transcript[:half]
            + "\n\n[... middle of conversation truncated for length ...]\n\n"
            + transcript[-half:]
        )
    return transcript


def summarize_ticket(ticket, custom_fields, transcript):
    """Return {'issue_summary', 'key_points', 'current_status', 'product_context'}."""
    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL

    fields_text = "\n".join(f"- {f['title']}: {f['value']}" for f in custom_fields) or "(none)"
    user_prompt = f"""\
Ticket #{ticket.get('id')}
Subject: {ticket.get('subject') or '(no subject)'}
Status: {ticket.get('status')} | Priority: {ticket.get('priority') or 'none'} | Type: {ticket.get('type') or 'n/a'}
Created: {ticket.get('created_at')} | Updated: {ticket.get('updated_at')}

Product / custom field information:
{fields_text}

Conversation ({len(transcript)} chars):
{transcript}"""

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    key_points = data.get("key_points") or []
    if not isinstance(key_points, list):
        key_points = [str(key_points)]
    return {
        "issue_summary": str(data.get("issue_summary") or ""),
        "key_points": [str(p) for p in key_points],
        "current_status": str(data.get("current_status") or ""),
        "product_context": str(data.get("product_context") or ""),
    }
