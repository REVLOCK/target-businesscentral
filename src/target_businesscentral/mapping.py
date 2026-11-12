"""
CSV → Business Central field mapping engine.

Mapping *data* lives in mapping_config.json (overridable via
``config["mapping_config_path"]``). This module is the engine: load, validate,
and apply handlers. Keep handlers small — do not copy SAP-specific sources.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd
import singer

from .const import POSTING_TYPE_CREDIT, POSTING_TYPE_DEBIT
from .exceptions import MappingConfigError, TransformError

logger = singer.get_logger()

DEFAULT_MAPPING_CONFIG_PATH = Path(__file__).with_name("mapping_config.json")

_BLANK_TOKENS = {"", "nan", "none", "<na>", "nat"}
_TOKEN_NORMALIZERS = {"account", "token"}
_KNOWN_SIGN_TYPES = (POSTING_TYPE_DEBIT, POSTING_TYPE_CREDIT)


@dataclass(frozen=True)
class FieldMap:
    """Validated mapping for one Business Central payload field."""

    target: str
    source: str
    column: Optional[str] = None
    required: bool = False
    default: Any = ""
    max_length: Optional[int] = None
    warn_on_truncate: bool = False
    value_map: Optional[Dict[str, str]] = None
    negate_when: Optional[str] = None
    round_digits: Optional[int] = None
    normalize: Optional[str] = None
    value: Any = None
    sign_column: Optional[str] = None


Handler = Callable[
    [pd.Series, FieldMap, Set[str], str],
    Tuple[Any, List[str]],
]


def load_mapping_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the ``field_mappings`` object from a mapping JSON file."""
    path = Path(config_path) if config_path else DEFAULT_MAPPING_CONFIG_PATH
    if not path.exists():
        raise MappingConfigError(f"Mapping config not found: {path}")

    with open(path) as handle:
        mapping = json.load(handle)

    if not isinstance(mapping, dict) or "field_mappings" not in mapping:
        raise MappingConfigError("Mapping config must contain 'field_mappings' key")

    field_mappings = mapping["field_mappings"]
    if not isinstance(field_mappings, dict) or not field_mappings:
        raise MappingConfigError("'field_mappings' must be a non-empty object")

    return field_mappings


def parse_field_maps(field_mappings: Dict[str, Any]) -> List[FieldMap]:
    """Validate JSON mapping specs into FieldMap instances."""
    field_maps: List[FieldMap] = []
    for target, spec in field_mappings.items():
        if not isinstance(spec, dict):
            raise MappingConfigError(
                f"Mapping for '{target}' must be an object, got {type(spec).__name__}"
            )
        source = spec.get("source")
        if source not in SOURCE_HANDLERS:
            raise MappingConfigError(
                f"Unknown mapping source '{source}' for field '{target}'"
            )
        field_map = FieldMap(
            target=target,
            source=source,
            column=spec.get("column"),
            required=bool(spec.get("required", False)),
            default=spec.get("default", ""),
            max_length=spec.get("max_length"),
            warn_on_truncate=bool(spec.get("warn_on_truncate", False)),
            value_map=spec.get("value_map"),
            negate_when=spec.get("negate_when"),
            round_digits=spec.get("round"),
            normalize=spec.get("normalize"),
            value=spec.get("value"),
            sign_column=spec.get("sign_column"),
        )
        _validate_field_map(field_map)
        field_maps.append(field_map)
    return field_maps


def load_field_maps(config: Optional[Dict[str, Any]] = None) -> List[FieldMap]:
    """Load FieldMaps from ``mapping_config_path`` or the bundled JSON."""
    path = (config or {}).get("mapping_config_path") or str(DEFAULT_MAPPING_CONFIG_PATH)
    return parse_field_maps(load_mapping_config(path))


def required_csv_columns(field_maps: Sequence[FieldMap]) -> List[str]:
    """CSV columns required by mappings with ``required: true``."""
    columns: List[str] = []
    seen: Set[str] = set()
    for field_map in field_maps:
        if not field_map.required:
            continue
        for column in (field_map.column, field_map.sign_column):
            if column and column not in seen:
                seen.add(column)
                columns.append(column)
    return columns


def grouping_column(field_maps: Sequence[FieldMap], target: str = "ext_document_no") -> Optional[str]:
    """CSV column that maps to the journal grouping key."""
    for field_map in field_maps:
        if field_map.target == target:
            return field_map.column
    return None


def transform_to_bc_lines(
    df: pd.DataFrame,
    field_maps: Optional[Sequence[FieldMap]] = None,
) -> List[Dict[str, Any]]:
    """
    Transform each CSV row into a Business Central bc_gj_lines payload.

    One output dict = one GJ line. Rows sharing the same Posting ID share
    the same ext_document_no, grouping them as one logical journal entry.

    Raises TransformError after collecting every row-level mapping error.
    """
    maps = list(field_maps) if field_maps is not None else load_field_maps()
    columns = set(df.columns)
    lines: List[Dict[str, Any]] = []
    errors: List[str] = []

    for position, (_, row) in enumerate(df.iterrows(), start=1):
        row_label = f"Row {position}"
        payload: Dict[str, Any] = {}
        row_errors: List[str] = []
        for field_map in maps:
            handler = SOURCE_HANDLERS[field_map.source]
            value, field_errors = handler(row, field_map, columns, row_label)
            row_errors.extend(field_errors)
            payload[field_map.target] = value
        errors.extend(row_errors)
        if not row_errors:
            lines.append(payload)

    if errors:
        for error in errors:
            logger.error(error)
        raise TransformError(
            f"Transformation failed with {len(errors)} error(s). See logs above."
        )

    logger.info(
        "Transformed %d CSV rows → %d bc_gj_lines payloads.",
        len(df),
        len(lines),
    )
    return lines


def _validate_field_map(field_map: FieldMap) -> None:
    if field_map.source == "column":
        if not field_map.column:
            raise MappingConfigError(
                f"Field '{field_map.target}' source 'column' requires 'column'"
            )
    elif field_map.source == "static":
        if field_map.value is None:
            raise MappingConfigError(
                f"Field '{field_map.target}' source 'static' requires 'value'"
            )
    elif field_map.source == "signed_amount":
        missing = [
            key
            for key, present in (
                ("column", field_map.column),
                ("sign_column", field_map.sign_column),
                ("negate_when", field_map.negate_when),
            )
            if not present
        ]
        if missing:
            raise MappingConfigError(
                f"Field '{field_map.target}' source 'signed_amount' requires {missing}"
            )
    else:
        raise MappingConfigError(
            f"Unknown mapping source '{field_map.source}' for field '{field_map.target}'"
        )

    if field_map.normalize and field_map.normalize not in _TOKEN_NORMALIZERS:
        raise MappingConfigError(
            f"Unknown normalize '{field_map.normalize}' for field '{field_map.target}'"
        )
    if field_map.max_length is not None and (
        not isinstance(field_map.max_length, int) or field_map.max_length <= 0
    ):
        raise MappingConfigError(
            f"Field '{field_map.target}' max_length must be a positive integer"
        )
    if field_map.value_map is not None and not isinstance(field_map.value_map, dict):
        raise MappingConfigError(
            f"Field '{field_map.target}' value_map must be an object"
        )


def _cell_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        if bool(pd.isna(val)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, float) and math.isnan(val):
        return None
    if hasattr(val, "strftime") and not isinstance(val, str):
        try:
            return val.strftime("%Y-%m-%d")
        except (ValueError, TypeError, OverflowError):
            pass
    text = str(val).strip()
    if text.lower() in _BLANK_TOKENS:
        return None
    return text


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        if bool(pd.isna(val)):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(val)
        return None if math.isnan(number) else number
    except (TypeError, ValueError):
        return None


def _normalize_token(value: Optional[str]) -> Optional[str]:
    """Strip a trailing ``.0`` from whole numbers inferred as floats."""
    if value is None:
        return None
    text = value.strip()
    if text.endswith(".0"):
        head = text[:-2]
        if head.lstrip("-").isdigit():
            return head
    return text


def _handle_column(
    row: pd.Series,
    field_map: FieldMap,
    columns: Set[str],
    row_label: str,
) -> Tuple[Any, List[str]]:
    errors: List[str] = []
    column = field_map.column
    if column not in columns:
        if field_map.required:
            errors.append(f"{row_label}: required column '{column}' is missing.")
            return None, errors
        return field_map.default, errors

    value = _cell_str(row.get(column))
    if field_map.normalize in _TOKEN_NORMALIZERS:
        value = _normalize_token(value)

    if field_map.value_map is not None:
        lookup = {str(key).strip().upper(): mapped for key, mapped in field_map.value_map.items()}
        raw_key = (value or "").strip().upper()
        if raw_key not in lookup:
            allowed = ", ".join(sorted(lookup))
            errors.append(
                f"{row_label}: '{column}' must be one of {allowed}, got '{value or ''}'."
            )
            return None, errors
        value = lookup[raw_key]

    if field_map.max_length and value and len(value) > field_map.max_length:
        truncated = value[: field_map.max_length]
        if field_map.warn_on_truncate:
            logger.warning(
                "%s: '%s' truncated from %d to %d characters (%r → %r).",
                row_label,
                column,
                len(value),
                field_map.max_length,
                value,
                truncated,
            )
        value = truncated

    if not value:
        if field_map.required:
            errors.append(f"{row_label}: '{column}' is empty.")
            return None, errors
        return field_map.default, errors

    return value, errors


def _handle_static(
    row: pd.Series,
    field_map: FieldMap,
    columns: Set[str],
    row_label: str,
) -> Tuple[Any, List[str]]:
    return field_map.value, []


def _handle_signed_amount(
    row: pd.Series,
    field_map: FieldMap,
    columns: Set[str],
    row_label: str,
) -> Tuple[Any, List[str]]:
    errors: List[str] = []
    column = field_map.column
    sign_column = field_map.sign_column

    if column not in columns:
        errors.append(f"{row_label}: required column '{column}' is missing.")
        return None, errors

    amount = _safe_float(row.get(column))
    if amount is None:
        errors.append(f"{row_label}: '{column}' is missing or non-numeric.")

    sign_raw = _cell_str(row.get(sign_column)) if sign_column in columns else None
    sign = (sign_raw or "").upper()
    if sign not in _KNOWN_SIGN_TYPES:
        errors.append(
            f"{row_label}: '{sign_column}' must be Debit or Credit, got '{sign_raw or ''}'."
        )

    if errors:
        return None, errors

    digits = field_map.round_digits if field_map.round_digits is not None else 6
    magnitude = round(abs(amount), digits)
    negate_when = (field_map.negate_when or POSTING_TYPE_DEBIT).upper()
    if sign == negate_when:
        signed = -magnitude if magnitude != 0 else 0.0
    else:
        signed = magnitude
    return signed, errors


SOURCE_HANDLERS: Dict[str, Handler] = {
    "column": _handle_column,
    "static": _handle_static,
    "signed_amount": _handle_signed_amount,
}
