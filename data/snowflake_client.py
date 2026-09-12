"""
Thin REST client for Snowflake's SQL API and Cortex inference -- no
heavy `snowflake-connector-python` dependency, just `requests` (same
approach as agent/cloud.py's OpenRouter client). See
docs/snowflake-integracion.md for the account setup and the reasoning
behind this integration.

Nothing here is imported by the DATA_SOURCE=local path: this module
(and graph/sql_detectors.py, data/snowflake_loader.py) only load when
DATA_SOURCE=snowflake, and that path always falls back to local on any
failure -- see api/main.py's _build_graph_for_state().
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

_EXTRA_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "forensic-auditor/1.0",
}

#: How long to poll an async (202) statement before giving up, and how
#: often. Our volume is tiny (a few hundred rows) so 200 is generous.
_POLL_ATTEMPTS = 30
_POLL_INTERVAL_SECONDS = 1.0

_NUMERIC_COLUMN_TYPES = {"fixed", "real"}


class SnowflakeError(RuntimeError):
    """A Snowflake REST call failed. The message is already
    human-readable -- never let a caller see a raw requests exception
    or an unparsed HTTP body."""


def snowflake_available() -> bool:
    """True only if both required credentials are in the environment."""
    return bool(os.environ.get("SNOWFLAKE_ACCOUNT")) and bool(os.environ.get("SNOWFLAKE_PAT"))


def sql_string_literal(value: str | None) -> str:
    """Single-quotes and escapes a Python string for inline SQL. Used by
    data/snowflake_loader.py's batched INSERTs and graph/sql_detectors.py's
    IN (...) clauses -- there is no parameterized-query support in the
    plain SQL API this client uses, so every literal goes through here."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _base_url() -> str:
    account = os.environ.get("SNOWFLAKE_ACCOUNT", "")
    return f"https://{account}.snowflakecomputing.com"


def _headers() -> dict[str, str]:
    pat = os.environ.get("SNOWFLAKE_PAT", "")
    return {
        "Authorization": f"Bearer {pat}",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        **_EXTRA_HEADERS,
    }


def _rows_to_dicts(payload: dict) -> list[dict]:
    """Maps the SQL API's {resultSetMetaData, data} shape to a list of
    dicts keyed by column name (Snowflake returns unquoted identifiers
    upper-cased, so callers read e.g. row["SUPPLIER_ID"]). Every value
    in `data` arrives as a string; columns the metadata marks fixed/real
    are cast to int/float, everything else (including dates -- ISO
    strings already) is left as-is."""
    meta = payload.get("resultSetMetaData") or {}
    row_type = meta.get("rowType") or []
    names = [col.get("name", str(i)) for i, col in enumerate(row_type)]
    numeric_idx = {i for i, col in enumerate(row_type) if col.get("type") in _NUMERIC_COLUMN_TYPES}

    results = []
    for raw_row in payload.get("data") or []:
        record: dict[str, Any] = {}
        for i, value in enumerate(raw_row):
            name = names[i] if i < len(names) else str(i)
            if value is None:
                record[name] = None
            elif i in numeric_idx:
                try:
                    record[name] = float(value) if ("." in value or "e" in value.lower()) else int(value)
                except (ValueError, AttributeError):
                    record[name] = value
            else:
                record[name] = value
        results.append(record)
    return results


def execute_sql(statement: str, timeout: int = 60) -> list[dict]:
    """Runs one SQL statement against the configured warehouse/database/
    schema and returns its rows as a list of dicts. Polls
    GET /api/v2/statements/{handle} when Snowflake answers 202 (async);
    almost everything at our volume comes back 200 synchronously, but
    202 is handled regardless. Raises SnowflakeError -- never returns
    silently-wrong data -- on any transport, HTTP, or shape failure."""
    if not snowflake_available():
        raise SnowflakeError("SNOWFLAKE_ACCOUNT/SNOWFLAKE_PAT not set")

    url = f"{_base_url()}/api/v2/statements"
    body = {
        "statement": statement,
        "timeout": timeout,
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "FORENSIC_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "FORENSIC_AUDITOR"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "ESTATE"),
    }
    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=timeout + 10)
    except requests.RequestException as exc:
        raise SnowflakeError(f"Could not reach Snowflake at {url}: {exc}") from exc

    if resp.status_code == 200:
        payload = resp.json()
    elif resp.status_code == 202:
        payload = _poll_statement(_safe_json(resp), timeout)
    else:
        raise SnowflakeError(
            f"Snowflake SQL API returned {resp.status_code} for statement "
            f"{statement[:120]!r}: {resp.text[:500]}")

    return _rows_to_dicts(payload)


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError as exc:
        raise SnowflakeError(f"Snowflake returned non-JSON: {resp.text[:500]}") from exc


def _poll_statement(initial_payload: dict, timeout: int) -> dict:
    handle = initial_payload.get("statementHandle")
    if not handle:
        raise SnowflakeError(f"202 response had no statementHandle: {initial_payload}")

    url = f"{_base_url()}/api/v2/statements/{handle}"
    for _ in range(_POLL_ATTEMPTS):
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            resp = requests.get(url, headers=_headers(), timeout=timeout + 10)
        except requests.RequestException as exc:
            raise SnowflakeError(f"Could not poll statement {handle}: {exc}") from exc
        if resp.status_code == 200:
            return _safe_json(resp)
        if resp.status_code != 202:
            raise SnowflakeError(f"Polling {handle} returned {resp.status_code}: {resp.text[:500]}")
    raise SnowflakeError(f"Statement {handle} did not complete after {_POLL_ATTEMPTS} polls")


#: Default model for cortex_complete(). Not every model is available on
#: every account/region (docs/snowflake-integracion.md's "Trampas
#: conocidas") -- Claude and Mistral returned "unknown model" on the
#: team's trial account (verified live), while the Llama family worked
#: via both AI_COMPLETE and SNOWFLAKE.CORTEX.COMPLETE. Override per call
#: if your account supports something else.
DEFAULT_CORTEX_MODEL = "llama3.1-70b"


def cortex_complete(prompt: str, model: str = DEFAULT_CORTEX_MODEL) -> str:
    """One-shot Cortex chat completion via the REST inference endpoint
    (the row-by-row fallback path -- graph/sql_detectors.py prefers a
    single in-warehouse SQL query and only calls this per-concept if the
    account has neither Cortex SQL function). Handles both a plain JSON
    body and an SSE stream, since which one an account returns isn't
    guaranteed."""
    if not snowflake_available():
        raise SnowflakeError("SNOWFLAKE_ACCOUNT/SNOWFLAKE_PAT not set")

    url = f"{_base_url()}/api/v2/cortex/inference:complete"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=60)
    except requests.RequestException as exc:
        raise SnowflakeError(f"Could not reach Cortex at {url}: {exc}") from exc

    if resp.status_code != 200:
        raise SnowflakeError(f"Cortex inference returned {resp.status_code}: {resp.text[:500]}")

    if "text/event-stream" in resp.headers.get("content-type", ""):
        return _extract_from_sse(resp.text)
    return _extract_from_json(_safe_json(resp))


def _extract_from_json(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SnowflakeError(f"Unexpected Cortex response shape: {payload}") from exc


def _extract_from_sse(raw: str) -> str:
    pieces: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        choice = (chunk.get("choices") or [{}])[0]
        text = (choice.get("delta") or {}).get("content") or (choice.get("message") or {}).get("content")
        if text:
            pieces.append(text)
    if not pieces:
        raise SnowflakeError(f"Could not extract any text from Cortex SSE response: {raw[:500]}")
    return "".join(pieces)
