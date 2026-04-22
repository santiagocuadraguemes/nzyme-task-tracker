"""Thin, rate-limited HTTP wrapper around the Affinity REST API.

Uses Affinity V1 exclusively (https://api.affinity.co/). V1 is required for
field-value writes (PUT/POST /field-values), which V2 does not expose, and it
covers everything else the fundraising branch needs with a single Basic-auth
scheme.

Auth: HTTP Basic, empty username, API key as password.

Rate limits: Affinity's published V1 limit is 900 req/minute per org. We cap
at 5 req/s to leave headroom and match the pattern used by
``src.notion_client_wrapper``.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

V1_BASE = "https://api.affinity.co"


class AffinityError(RuntimeError):
    """Raised when the Affinity API returns a non-retryable error."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"Affinity {status} on {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


class AffinityClient:
    """Rate-limited, retry-aware facade over the Affinity V1 REST API."""

    def __init__(
        self,
        api_key: str,
        *,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 3,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise ValueError("AffinityClient requires an API key")
        token = base64.b64encode(f":{api_key}".encode()).decode()
        self._http = httpx.Client(
            base_url=V1_BASE,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        self._rate_limiter = rate_limiter or RateLimiter(max_requests_per_second=5.0)
        self._max_retries = max_retries

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AffinityClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal request plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else path
        for attempt in range(1, self._max_retries + 1):
            self._rate_limiter.acquire()
            try:
                resp = self._http.request(method, url, params=params, json=json_body)
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Affinity transport error %s (attempt %d/%d), retrying in %ds",
                        exc, attempt, self._max_retries, wait,
                    )
                    time.sleep(wait)
                    continue
                raise
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                wait = 2 ** attempt
                logger.warning(
                    "Affinity %s on %s (attempt %d/%d), retrying in %ds",
                    resp.status_code, url, attempt, self._max_retries, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise AffinityError(resp.status_code, resp.text, url)
            if not resp.content:
                return None
            return resp.json()
        raise AffinityError(599, "exhausted retries", url)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        """Verify auth; returns tenant + permissions."""
        return self._request("GET", "/auth/whoami")

    def get_fields(self, list_id: int) -> list[dict[str, Any]]:
        """GET /v1/fields?list_id=N — returns fields with dropdown_options.

        Use this to fetch allowed-values enums for ranked-dropdowns.
        """
        return self._request("GET", "/fields", params={"list_id": list_id}) or []

    def list_list_entries(self, list_id: int, page_size: int = 500) -> list[dict[str, Any]]:
        """GET /v1/lists/{list_id}/list-entries — paginated.

        Returns all entries in a list, following page_token pagination.
        """
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            resp = self._request("GET", f"/lists/{list_id}/list-entries", params=params)
            if isinstance(resp, list):  # old API returned a plain list
                out.extend(resp)
                return out
            entries = resp.get("list_entries") or []
            out.extend(entries)
            page_token = resp.get("next_page_token")
            if not page_token:
                return out

    def get_field_values_for_entry(self, list_entry_id: int) -> list[dict[str, Any]]:
        """GET /v1/field-values?list_entry_id=N.

        Returns current field values including their ``id`` (needed for PUT).
        """
        return self._request(
            "GET", "/field-values", params={"list_entry_id": list_entry_id},
        ) or []

    def search_persons(self, term: str) -> list[dict[str, Any]]:
        """GET /v1/persons?term=X — search persons by email or name.

        Always requests ``with_opportunities=true`` so the response includes
        each person's ``opportunity_ids`` — the key the LP matcher intersects
        against the LP Funnel index.
        """
        resp = self._request(
            "GET",
            "/persons",
            params={"term": term, "with_opportunities": "true"},
        )
        if isinstance(resp, dict):
            return resp.get("persons") or []
        return resp or []

    def search_organizations(self, term: str) -> list[dict[str, Any]]:
        """GET /v1/organizations?term=X — search orgs by domain or name."""
        resp = self._request("GET", "/organizations", params={"term": term})
        if isinstance(resp, dict):
            return resp.get("organizations") or []
        return resp or []

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def update_field_value(self, field_value_id: int, value: Any) -> dict[str, Any]:
        """PUT /v1/field-values/{id} — update an existing field value."""
        return self._request(
            "PUT", f"/field-values/{field_value_id}", json_body={"value": value},
        )

    def create_field_value(
        self,
        *,
        field_id: int | str,
        entity_id: int,
        list_entry_id: int | None,
        value: Any,
    ) -> dict[str, Any]:
        """POST /v1/field-values — create a new field value."""
        body: dict[str, Any] = {
            "field_id": field_id,
            "entity_id": entity_id,
            "value": value,
        }
        if list_entry_id is not None:
            body["list_entry_id"] = list_entry_id
        return self._request("POST", "/field-values", json_body=body)

    def create_note(
        self,
        *,
        content: str,
        content_type: str = "html",
        opportunity_ids: list[int] | None = None,
        organization_ids: list[int] | None = None,
        person_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/notes — create a note attached to one or more entities.

        ``content_type`` must be ``"plain"``, ``"html"``, or ``"markdown"``.
        """
        body: dict[str, Any] = {"content": content}
        # Affinity V1 uses ``type`` (integer) or ``content_type``; modern API accepts ``content_type``.
        if content_type:
            body["content_type"] = content_type
        if opportunity_ids:
            body["opportunity_ids"] = opportunity_ids
        if organization_ids:
            body["organization_ids"] = organization_ids
        if person_ids:
            body["person_ids"] = person_ids
        return self._request("POST", "/notes", json_body=body)


__all__ = ["AffinityClient", "AffinityError"]
