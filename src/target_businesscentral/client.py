"""
Business Central API client for target-businesscentral.

Two-step flow:
  Step 1 — _authenticate()
            POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
            grant_type=client_credentials
            → stores Bearer access_token + expiry

  Step 2 — post_batch(payloads)
            POST https://api.businesscentral.dynamics.com/.../$batch
            Authorization: Bearer <access_token>
            Body: { "requests": [ up to 100 bc_gj_lines entries ] }
            → creates up to 100 general journal lines in one API call
            → caller chunks by journal so a Posting ID is never split
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import singer

from .const import (
    AUTH_URL_TEMPLATE,
    BATCH_SIZE,
    BATCH_SUCCESS_STATUSES,
    BC_BATCH_ITEM_URL_TEMPLATE,
    BC_BATCH_URL_TEMPLATE,
    BC_SCOPE,
    DEFAULT_TIMEOUT_SECONDS,
    GRANT_TYPE,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
)
from .exceptions import (
    AuthenticationError,
    EntryPostError,
    RateLimitError,
    ServerError,
    TokenExpiredError,
)

logger = singer.get_logger()


class BusinessCentralClient:
    """
    Authenticates via Microsoft OAuth2 (client_credentials) and bulk-posts
    general journal lines to the Business Central Chargebee $batch API.

    Token refresh: 401 mid-run triggers one automatic re-auth + retry.

    Retry policy for $batch (GL posting must not double-post):
      - 401: re-authenticate once
      - 429: retry with backoff (request typically not processed)
      - ConnectionError: retry with backoff (request never reached the server)
      - Timeout / 5xx: do not retry (the batch may already have been committed)
    """

    def __init__(
        self,
        tenant_domain: str,
        client_id: str,
        client_secret: str,
        environment: str,
        company_id: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._tenant_domain = tenant_domain
        self._client_id = client_id
        self._client_secret = client_secret
        self._environment = environment
        self._company_id = company_id
        self._timeout = timeout

        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

        self._auth_url = AUTH_URL_TEMPLATE.format(tenant_domain=tenant_domain)
        self._batch_url = BC_BATCH_URL_TEMPLATE.format(
            tenant_domain=tenant_domain,
            environment=environment,
        )
        self._batch_item_url = BC_BATCH_ITEM_URL_TEMPLATE.format(
            company_id=company_id,
        )

        self._session = requests.Session()

        # Authenticate immediately — fail fast on bad credentials
        self._authenticate()

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session.close()

    # ── Step 1: Authentication ──────────────────────────────────────────────────

    def _authenticate(self) -> None:
        """
        POST to Microsoft OAuth2 token endpoint using client_credentials.
        Stores Bearer token and expiry.
        """
        logger.info("Authenticating with Microsoft OAuth2 (client_credentials)...")

        payload = {
            "grant_type": GRANT_TYPE,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": BC_SCOPE,
        }

        try:
            response = self._session.post(
                self._auth_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise AuthenticationError(
                f"Cannot connect to auth endpoint: {self._auth_url}. Error: {exc}"
            ) from exc

        if response.status_code != 200:
            raise AuthenticationError(
                f"OAuth2 token request failed [{response.status_code}]: "
                f"{_error_detail(response)}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise AuthenticationError(
                "OAuth2 response was not JSON."
            ) from exc

        if "access_token" not in body:
            raise AuthenticationError(
                "OAuth2 response missing access_token."
            )

        self._access_token = body["access_token"]
        expires_in = int(body.get("expires_in", 3599))
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        logger.info(
            "Authentication successful. Token valid for ~%d minutes.", expires_in // 60
        )

        self._session.headers.update({
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _ensure_valid_token(self) -> None:
        """Re-authenticate if the token has expired or is close to expiry."""
        if self._token_expiry and datetime.now(timezone.utc) >= self._token_expiry:
            logger.warning("Bearer token expired. Re-authenticating...")
            self._authenticate()

    # ── Step 2: Bulk batch POST ─────────────────────────────────────────────────

    def post_batch(
        self, payloads: List[Dict[str, Any]]
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        POST up to BATCH_SIZE (100) bc_gj_lines in a single $batch call.

        Returns
        -------
        succeeded : list of dict
            Response bodies for records that returned 200/201/204.
        failed : list of dict
            Dicts with id, ext_document_no, account_no, amount, status, error.

        Raises
        ------
        EntryPostError    : on HTTP-level 4xx (not per-record), timeout, or
                            a 200 response missing a matching ``responses`` array
        TokenExpiredError : on 401 after re-auth
        RateLimitError    : on 429 after MAX_RETRIES
        ServerError       : on 5xx (not retried — the batch may have committed)
        """
        if len(payloads) > BATCH_SIZE:
            raise EntryPostError(
                f"Batch size {len(payloads)} exceeds maximum {BATCH_SIZE}."
            )
        if not payloads:
            return [], []

        self._ensure_valid_token()

        batch_requests = [
            {
                "method": "POST",
                "id": str(i + 1),
                "url": self._batch_item_url,
                "headers": {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                "body": payload,
            }
            for i, payload in enumerate(payloads)
        ]
        body = {"requests": batch_requests}
        delay = RETRY_BACKOFF_BASE

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._session.post(
                    self._batch_url,
                    json=body,
                    timeout=self._timeout,
                )
            except requests.exceptions.Timeout as exc:
                raise EntryPostError(
                    "Batch request timed out. Not retrying to avoid duplicate "
                    "journal lines."
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Cannot connect to BC $batch API. Retrying in %ds "
                        "(attempt %d/%d)...",
                        delay, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise EntryPostError(
                    f"Cannot connect to BC $batch API: {self._batch_url}. Error: {exc}"
                ) from exc

            if response.status_code == 200:
                try:
                    response_body = response.json()
                except ValueError as exc:
                    raise EntryPostError(
                        "Batch HTTP 200 but the body was not JSON."
                    ) from exc
                return self._parse_batch_response(response_body, payloads)

            if response.status_code == 401:
                if attempt == 0:
                    logger.warning("401 Unauthorized. Re-authenticating and retrying...")
                    self._authenticate()
                    continue
                raise TokenExpiredError(
                    "401 Unauthorized after re-authentication attempt.",
                    response=_error_detail(response),
                )

            if response.status_code == 429:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Rate limited (429). Retrying in %ds (attempt %d/%d)...",
                        delay, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RateLimitError(
                    f"Batch rate limited after {MAX_RETRIES} retries.",
                    response=_error_detail(response),
                )

            if response.status_code >= 500:
                raise ServerError(
                    f"Batch server error {response.status_code}. Not retrying "
                    "to avoid duplicate journal lines.",
                    response=_error_detail(response),
                )

            raise EntryPostError(
                f"$batch API error [{response.status_code}]: {_error_detail(response)}",
                response=_error_detail(response),
            )

        raise EntryPostError("Batch POST failed after all retries.")

    # ── Response parsing ────────────────────────────────────────────────────────

    def _parse_batch_response(
        self,
        response_body: Dict,
        payloads: List[Dict[str, Any]],
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Parse the $batch response and separate successes from failures.

        A 200 envelope with a missing, empty, or length-mismatched ``responses``
        array is a hard failure — never treat it as "0 posted successfully".
        """
        responses = response_body.get("responses") if isinstance(response_body, dict) else None
        if not isinstance(responses, list) or len(responses) != len(payloads):
            actual = len(responses) if isinstance(responses, list) else 0
            raise EntryPostError(
                "Batch HTTP 200 but 'responses' is missing or does not match "
                f"the request count (expected {len(payloads)}, got {actual})."
            )

        payload_by_id = {str(index + 1): payload for index, payload in enumerate(payloads)}
        succeeded = []
        failed = []
        seen_ids = set()

        for item in responses:
            item_id = str(item.get("id", "?"))
            if item_id in seen_ids:
                raise EntryPostError(
                    f"Batch response contains duplicate id {item_id!r}."
                )
            seen_ids.add(item_id)
            payload = payload_by_id.get(item_id)
            if payload is None:
                raise EntryPostError(
                    f"Batch response id {item_id!r} does not match any request."
                )
            status = item.get("status")
            body = item.get("body", {})
            identity = _payload_identity(payload, item_id)

            if status in BATCH_SUCCESS_STATUSES:
                logger.info(
                    "  [id=%s ext_doc=%s] Created: bc_id=%s account=%s amount=%s",
                    item_id,
                    identity["ext_document_no"],
                    body.get("id") if isinstance(body, dict) else None,
                    identity["account_no"],
                    identity["amount"],
                )
                succeeded.append(body if isinstance(body, dict) else {"body": body})
            else:
                error_msg = (
                    body.get("error", {}).get("message", str(body))
                    if isinstance(body, dict)
                    else str(body)
                )
                logger.error(
                    "  [id=%s ext_doc=%s] FAILED status=%s: %s",
                    item_id,
                    identity["ext_document_no"],
                    status,
                    error_msg,
                )
                failed.append({
                    **identity,
                    "id": item_id,
                    "status": status,
                    "error": error_msg,
                })

        return succeeded, failed


def _payload_identity(payload: Dict[str, Any], fallback_id: str) -> Dict[str, Any]:
    return {
        "id": fallback_id,
        "ext_document_no": payload.get("ext_document_no"),
        "account_no": payload.get("account_no"),
        "amount": payload.get("amount"),
    }


def _error_detail(response: requests.Response) -> str:
    """Short error text from an HTTP response; avoids dumping secrets."""
    try:
        data = response.json()
    except ValueError:
        return response.reason or f"HTTP {response.status_code}"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)
        description = data.get("error_description") or error
        if description:
            return str(description)
    return response.reason or f"HTTP {response.status_code}"


# ── Factory ──────────────────────────────────────────────────────────────────────

def get_client(
    *,
    tenant_domain: str,
    client_id: str,
    client_secret: str,
    environment: str,
    company_id: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> BusinessCentralClient:
    """Instantiate and return an authenticated BusinessCentralClient."""
    return BusinessCentralClient(
        tenant_domain=tenant_domain,
        client_id=client_id,
        client_secret=client_secret,
        environment=environment,
        company_id=company_id,
        timeout=timeout,
    )
