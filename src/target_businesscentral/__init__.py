"""
target-businesscentral
=============
Reads JournalEntries.csv, transforms each row into a Business Central
general journal line payload using mapping_config.json, and POSTs them
to the BC Chargebee $batch API.

Two-step flow:
  1. Authenticate → Microsoft OAuth2 client_credentials → Bearer token
  2. POST journal lines in chunks of up to 100 → $batch → bc_gj_lines

Rows sharing the same Posting ID share the same ext_document_no in BC,
grouping them as one logical journal entry. A Posting ID is never split
across $batch calls.

Execution flow
--------------
1. Parse --config config.json        (singer.utils.parse_args)
2. BusinessCentralClient.__init__()  → _authenticate() → Bearer token
3. load_csv()                        → validate columns from mapping
4. transform_to_bc_lines()           → list of bc_gj_lines payloads
5. chunk_journal_lines()             → groups that fit in BATCH_SIZE
6. For each chunk: bc_client.post_batch()
7. Log summary: N succeeded, M failed
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Sequence

import pandas as pd
import singer

from .client import get_client
from .const import (
    BATCH_SIZE,
    BC_EXT_DOCUMENT_NO,
    DEFAULT_TIMEOUT_SECONDS,
    INPUT_FILENAME,
    REQUIRED_CONFIG_KEYS,
)
from .exceptions import BusinessCentralError, EntryPostError, TransformError
from .mapping import (
    grouping_column,
    load_field_maps,
    required_csv_columns,
    transform_to_bc_lines,
)

logger = singer.get_logger()


# ── CSV loading ─────────────────────────────────────────────────────────────────

def load_csv(
    input_path: str,
    required_columns: Sequence[str],
    grouping_col: str = "Posting ID",
) -> pd.DataFrame:
    """Read and validate JournalEntries.csv. Raises TransformError on errors."""
    filepath = os.path.join(input_path, INPUT_FILENAME)
    logger.info("Reading input file: %s", filepath)

    try:
        df = pd.read_csv(filepath, dtype=str)
    except FileNotFoundError:
        message = f"Input file not found: {filepath}"
        logger.error(message)
        raise TransformError(message)

    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        message = (
            "CSV is missing required columns: %s  Found: %s"
            % (json.dumps(missing), json.dumps(list(df.columns)))
        )
        logger.error(message)
        raise TransformError(message)

    unique_groups = df[grouping_col].nunique() if grouping_col in df.columns else 0
    logger.info(
        "Loaded %d rows, %d unique Posting Ids.",
        len(df),
        unique_groups,
    )
    return df


def chunk_journal_lines(
    lines: List[Dict[str, Any]],
    batch_size: int = BATCH_SIZE,
    group_key: str = BC_EXT_DOCUMENT_NO,
) -> List[List[Dict[str, Any]]]:
    """
    Pack journal lines into $batch chunks without splitting a Posting ID.

    Groups are packed in first-seen order. A single journal larger than
    ``batch_size`` cannot be posted atomically and raises TransformError.
    """
    if not lines:
        return []

    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for line in lines:
        key = line.get(group_key) or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(line)

    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for key in order:
        group = groups[key]
        if len(group) > batch_size:
            raise TransformError(
                f"Journal '{key}' has {len(group)} lines, exceeding batch "
                f"size {batch_size}."
            )
        if current and len(current) + len(group) > batch_size:
            chunks.append(current)
            current = []
        current.extend(group)
    if current:
        chunks.append(current)
    return chunks


# ── Orchestration ───────────────────────────────────────────────────────────────

def upload(config: Dict, bc_client) -> None:
    """
    Main pipeline:
    1. Load mapping config (bundled JSON or mapping_config_path)
    2. Load + validate CSV
    3. Transform rows → bc_gj_lines payloads
    4. Pack into $batch chunks without splitting a Posting ID
    5. Log per-batch and overall summary
    """
    field_maps = load_field_maps(config)
    required_columns = required_csv_columns(field_maps)
    group_col = grouping_column(field_maps) or "Posting ID"

    df = load_csv(config["input_path"], required_columns, grouping_col=group_col)
    logger.info("Transforming CSV rows to BC journal line payloads...")
    lines = transform_to_bc_lines(df, field_maps)

    chunks = chunk_journal_lines(lines)
    total = len(lines)
    num_chunks = len(chunks)

    logger.info(
        "Posting %d lines in %d batch(es) of up to %d records each...",
        total, num_chunks, BATCH_SIZE,
    )

    all_succeeded = 0
    all_failed: List[Dict] = []

    for batch_num, chunk in enumerate(chunks, start=1):
        logger.info(
            "--- Batch %d/%d: sending %d records ---",
            batch_num, num_chunks, len(chunk),
        )
        try:
            succeeded, failed = bc_client.post_batch(chunk)
            all_succeeded += len(succeeded)
            all_failed.extend(failed)
            logger.info(
                "Batch %d/%d complete: %d succeeded, %d failed.",
                batch_num, num_chunks, len(succeeded), len(failed),
            )
        except BusinessCentralError as exc:
            logger.error(
                "Batch %d/%d HTTP-level failure: %s",
                batch_num, num_chunks, exc.message,
            )
            all_failed.extend(_http_failures(chunk, exc.message))

    logger.info(
        "Done. %d/%d lines posted successfully to Business Central.",
        all_succeeded, total,
    )
    if all_failed:
        failed_journals = [
            item.get("ext_document_no") or item.get("id") for item in all_failed
        ]
        message = (
            "%d lines FAILED across all batches. Failed journals: %s"
            % (len(all_failed), failed_journals)
        )
        logger.error(message)
        raise EntryPostError(message)


def _http_failures(chunk: List[Dict[str, Any]], error: str) -> List[Dict[str, Any]]:
    return [
        {
            "id": line.get(BC_EXT_DOCUMENT_NO) or str(index + 1),
            "ext_document_no": line.get(BC_EXT_DOCUMENT_NO),
            "account_no": line.get("account_no"),
            "amount": line.get("amount"),
            "status": "http_error",
            "error": error,
        }
        for index, line in enumerate(chunk)
    ]


# ── Entry point ─────────────────────────────────────────────────────────────────

@singer.utils.handle_top_exception(logger)
def main() -> None:
    args = singer.utils.parse_args(REQUIRED_CONFIG_KEYS)
    config = args.config

    logger.info("Starting target-businesscentral.")

    bc_client = get_client(
        tenant_domain=config["tenant_domain"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        environment=config["environment"],
        company_id=config["company_id"],
        timeout=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )

    try:
        upload(config, bc_client)
    finally:
        bc_client.close()

    logger.info("target-businesscentral finished.")


if __name__ == "__main__":
    main()
