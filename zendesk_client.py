"""Thin wrapper around the Zendesk REST API for ticket printing."""

import time

import requests


class ZendeskError(Exception):
    """Zendesk API failure with a user-presentable message."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ZendeskClient:
    FIELDS_CACHE_TTL = 600  # ticket field definitions rarely change

    def __init__(self, subdomain, email, api_token):
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        self.session = requests.Session()
        self.session.auth = (f"{email}/token", api_token)
        self._fields_cache = None
        self._fields_cache_at = 0.0

    def _request(self, method, path_or_url, params=None, json=None, _retried=False):
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        try:
            resp = self.session.request(method, url, params=params, json=json, timeout=30)
        except requests.RequestException as exc:
            raise ZendeskError(f"Could not reach Zendesk: {exc}") from exc
        if resp.status_code == 429 and not _retried:
            # Rate limited: honor Retry-After once, then give up with a clear error.
            try:
                delay = min(float(resp.headers.get("Retry-After", 2)), 30.0)
            except ValueError:
                delay = 2.0
            time.sleep(delay)
            return self._request(method, path_or_url, params=params, json=json, _retried=True)
        if resp.status_code == 401:
            raise ZendeskError(
                "Zendesk rejected the credentials (401). Check ZENDESK_EMAIL and "
                "ZENDESK_API_TOKEN, and that API token access is enabled in Admin Center.",
                status=401,
            )
        if resp.status_code == 404:
            raise ZendeskError("Zendesk returned 404 (not found).", status=404)
        if not resp.ok:
            raise ZendeskError(
                f"Zendesk API error {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
        return resp.json()

    def _get(self, path_or_url, params=None):
        return self._request("GET", path_or_url, params=params)

    def add_comment(self, ticket_id, body, public=True):
        """Post a comment to a ticket. public=True makes it visible to the requester."""
        payload = {"ticket": {"comment": {"body": body, "public": bool(public)}}}
        return self._request("PUT", f"/tickets/{ticket_id}.json", json=payload)

    def post_update(self, ticket_id, body, public=True, tags=None, custom_fields=None):
        """Comment, add tags and set custom fields in ONE ticket update.

        One request means the customer email and the tag/field change land
        together: there is no half-applied state if the second call would
        have failed. ``additional_tags`` appends without replacing existing tags.
        """
        ticket = {"comment": {"body": body, "public": bool(public)}}
        if tags:
            ticket["additional_tags"] = list(tags)
        if custom_fields:
            ticket["custom_fields"] = [{"id": fid, "value": val} for fid, val in custom_fields.items()]
        return self._request("PUT", f"/tickets/{ticket_id}.json", json={"ticket": ticket})

    def add_tags(self, ticket_id, tags):
        """Append tags to a ticket without touching the ones already there."""
        return self._request("PUT", f"/tickets/{ticket_id}/tags.json", json={"tags": list(tags)})

    def search_export(self, query, max_pages=20):
        """Yield tickets matching a Zendesk search query via the export endpoint.

        Uses cursor pagination (no 1,000-result cap). max_pages bounds the pull
        so a runaway query cannot hang a request thread.
        """
        url = "/search/export"
        params = {"filter[type]": "ticket", "page[size]": 100, "query": query}
        for _ in range(max_pages):
            data = self._get(url, params=params)
            params = None  # the next link carries the full query string
            for item in data.get("results", []):
                yield item
            if not (data.get("meta") or {}).get("has_more"):
                return
            url = (data.get("links") or {}).get("next")
            if not url:
                return

    def get_ticket(self, ticket_id):
        """Return (ticket, requester, organization) using sideloading.

        Note: a closed ticket can still be read, but Zendesk rejects comments on it.
        """
        try:
            data = self._get(f"/tickets/{ticket_id}.json", params={"include": "users,organizations"})
        except ZendeskError as exc:
            if exc.status == 404:
                raise ZendeskError(f"Ticket {ticket_id} was not found.", status=404) from exc
            raise
        ticket = data["ticket"]
        users = {u["id"]: u for u in data.get("users", [])}
        orgs = {o["id"]: o for o in data.get("organizations", [])}
        requester = users.get(ticket.get("requester_id"))
        organization = orgs.get(ticket.get("organization_id"))
        return ticket, requester, organization

    def get_comments(self, ticket_id):
        """Return (comments, users_by_id) for the full conversation, oldest first."""
        comments = []
        users = {}
        url = f"/tickets/{ticket_id}/comments.json"
        params = {"include": "users"}
        while url:
            data = self._get(url, params=params)
            params = None  # next_page URLs already carry the query string
            comments.extend(data.get("comments", []))
            for user in data.get("users", []):
                users[user["id"]] = user
            url = data.get("next_page")
        return comments, users

    def get_ticket_fields(self):
        """Return {field_id: field_definition}, cached for a few minutes."""
        now = time.time()
        if self._fields_cache is not None and now - self._fields_cache_at < self.FIELDS_CACHE_TTL:
            return self._fields_cache
        fields = []
        url = "/ticket_fields.json"
        while url:
            data = self._get(url)
            fields.extend(data.get("ticket_fields", []))
            url = data.get("next_page")
        self._fields_cache = {f["id"]: f for f in fields}
        self._fields_cache_at = now
        return self._fields_cache

    def get_ticket_form(self, form_id):
        """Return the ticket form, or None if unavailable (forms are plan-dependent)."""
        if not form_id:
            return None
        try:
            data = self._get(f"/ticket_forms/{form_id}.json")
        except ZendeskError:
            return None
        return data.get("ticket_form")


def _format_field_value(field, value):
    """Translate a raw custom field value into a human-readable string."""
    options = {opt["value"]: opt["name"] for opt in (field or {}).get("custom_field_options", [])}
    if isinstance(value, list):
        return ", ".join(str(options.get(v, v)) for v in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(options.get(value, value))


def resolve_custom_fields(ticket, fields_map, form=None):
    """Return [{'title', 'value'}] for populated custom fields.

    Skips null/empty/unchecked values; orders per the ticket form when available.
    """
    values = {cf["id"]: cf.get("value") for cf in ticket.get("custom_fields", [])}

    if form and form.get("ticket_field_ids"):
        form_ids = [fid for fid in form["ticket_field_ids"] if fid in values]
        ordered_ids = form_ids + [fid for fid in values if fid not in set(form_ids)]
    else:
        ordered_ids = list(values)

    resolved = []
    for field_id in ordered_ids:
        value = values.get(field_id)
        if value is None or value == "" or value == [] or value is False:
            continue
        field = fields_map.get(field_id)
        title = field["title"] if field else f"Field {field_id}"
        resolved.append({"title": title, "value": _format_field_value(field, value)})
    return resolved
