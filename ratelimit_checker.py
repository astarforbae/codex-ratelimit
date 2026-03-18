#!/usr/bin/env python3
"""
Token Usage Parser for Claude Code Sessions

This script finds the latest session file and extracts token usage and rate limit information
from the most recent event_msg record with token_count payload.
"""

import argparse
import json
import os
import glob
import curses
import hashlib
import pickle
import time
import signal
import sys
import unicodedata
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Iterable



LABEL_AREA_WIDTH = 12
BAR_WIDTH = 46
LITELLM_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CODEX_PROVIDER_PREFIXES = ("openai/", "azure/", "openrouter/openai/")
CODEX_MODEL_ALIASES = {
    "gpt-5-codex": "gpt-5",
}
LEGACY_FALLBACK_MODEL = "gpt-5"
DEFAULT_PRICING_CACHE_TTL_SECONDS = 3600
DEFAULT_PRICING_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "litellm_pricing_map.json"
EVENT_INDEX_CACHE_VERSION = 1
DEFAULT_EVENT_INDEX_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "usage_event_index_v1.pickle"


def pad_label_to_width(label: str, target_width: int = LABEL_AREA_WIDTH) -> str:
    """Trim and pad the label so its rendered width matches target_width."""
    current_width = 0
    truncated_chars = []

    for char in label:
        char_width = get_display_width(char)

        # Always include zero-width characters (e.g., combining marks)
        if char_width == 0:
            truncated_chars.append(char)
            continue

        if current_width + char_width > target_width:
            break

        truncated_chars.append(char)
        current_width += char_width

    padded_label = "".join(truncated_chars)

    if current_width < target_width:
        padded_label += " " * (target_width - current_width)

    return padded_label


def get_display_width(text: str) -> int:
    """Calculate the actual display width of text including Unicode characters."""
    width = 0
    for char in text:
        # Handle common block characters
        if char in '█░':
            # These block characters typically display as 1 column
            width += 1
        elif unicodedata.category(char).startswith('M'):
            # Combining marks (don't add width)
            width += 0
        else:
            # Regular characters
            width += 1
    return width

def get_session_base_path(custom_path: Optional[str] = None) -> Path:
    """Get the base path for session storage."""
    if custom_path:
        return Path(custom_path).expanduser()
    return Path.home() / ".codex" / "sessions"


def parse_iso_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse an ISO timestamp string to a timezone-aware datetime."""
    if not timestamp_str:
        return None

    try:
        return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def build_token_event_signature(record: Dict[str, Any]) -> Optional[str]:
    """Build a stable fingerprint for token_count replay dedupe across forked files."""
    payload = record.get('payload')
    if not isinstance(payload, dict):
        return None

    try:
        canonical = json.dumps(
            {
                'payload': payload,
            },
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        )
    except (TypeError, ValueError):
        return None

    return hashlib.sha1(canonical.encode('utf-8')).hexdigest()


def normalize_usage(raw_usage: Any) -> Optional[Dict[str, int]]:
    """Normalize raw usage payload to integer counters."""
    if not isinstance(raw_usage, dict):
        return None

    fields = (
        'input_tokens',
        'cached_input_tokens',
        'output_tokens',
        'reasoning_output_tokens',
        'total_tokens',
    )
    normalized = {}
    has_any_value = False

    for field in fields:
        value = raw_usage.get(field, 0)
        try:
            parsed_value = int(value)
        except (TypeError, ValueError):
            parsed_value = 0

        if parsed_value < 0:
            parsed_value = 0

        if parsed_value > 0:
            has_any_value = True

        normalized[field] = parsed_value

    if not has_any_value:
        return None

    return normalized


def subtract_usage(current: Dict[str, int], previous: Optional[Dict[str, int]]) -> Dict[str, int]:
    """Subtract two cumulative usage snapshots and clamp at zero."""
    if previous is None:
        return dict(current)

    result = {}
    for field in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens'):
        result[field] = max(0, current.get(field, 0) - previous.get(field, 0))
    return result


def usage_has_tokens(usage: Dict[str, int]) -> bool:
    """Check whether usage contains non-zero token counters."""
    return any(
        usage.get(field, 0) > 0
        for field in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
    )


def extract_model_from_object(value: Any) -> Optional[str]:
    """
    Extract model name from nested payloads.

    Extraction order mirrors ccusage behavior:
    - info.model / info.model_name / info.metadata.model
    - model
    - metadata.model
    """
    if not isinstance(value, dict):
        return None

    def as_non_empty_string(candidate: Any) -> Optional[str]:
        if not isinstance(candidate, str):
            return None
        stripped = candidate.strip()
        return stripped if stripped else None

    info = value.get('info')
    if isinstance(info, dict):
        for candidate in (info.get('model'), info.get('model_name')):
            model = as_non_empty_string(candidate)
            if model:
                return model

        info_metadata = info.get('metadata')
        if isinstance(info_metadata, dict):
            model = as_non_empty_string(info_metadata.get('model'))
            if model:
                return model

    model = as_non_empty_string(value.get('model'))
    if model:
        return model

    metadata = value.get('metadata')
    if isinstance(metadata, dict):
        model = as_non_empty_string(metadata.get('model'))
        if model:
            return model

    return None


def read_default_model_from_config() -> Optional[str]:
    """Read the global default model from ~/.codex/config.toml."""
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return None

    model_pattern = re.compile(r'^\s*model\s*=\s*"([^"]+)"\s*$')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith('['):
                    # We only care about top-level keys before sections.
                    break
                match = model_pattern.match(line)
                if match:
                    model = match.group(1).strip()
                    if model:
                        return model
    except OSError:
        return None

    return None


def iterate_all_rollout_files(base_path: Path) -> Iterable[Path]:
    """Iterate all rollout files in deterministic order."""
    if not base_path.exists():
        return []
    return sorted(base_path.rglob("rollout-*.jsonl"))


def get_recent_window_start(recent_days: int, now_local: Optional[datetime] = None) -> datetime:
    """Get local-time window start for recent-days filter."""
    if now_local is None:
        now_local = datetime.now().astimezone()
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start - timedelta(days=recent_days - 1)


def _read_int_env(name: str, default: int) -> int:
    """Read integer env var with fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def get_pricing_cache_ttl_seconds() -> int:
    """Get pricing cache TTL in seconds."""
    return _read_int_env("CODEX_RATELIMIT_PRICING_CACHE_TTL_SECONDS", DEFAULT_PRICING_CACHE_TTL_SECONDS)


def get_pricing_cache_path() -> Path:
    """Get cache file path for LiteLLM pricing data."""
    env_path = os.getenv("CODEX_RATELIMIT_PRICING_CACHE_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_PRICING_CACHE_PATH


def _read_pricing_cache(cache_path: Path) -> Optional[Dict[str, Any]]:
    """Read pricing cache payload if available."""
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cached = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(cached, dict):
        return None

    data = cached.get("data")
    fetched_at = cached.get("fetched_at")
    url = cached.get("url")
    if not isinstance(data, dict):
        return None
    if not isinstance(fetched_at, (int, float)):
        return None
    if not isinstance(url, str):
        return None

    return {
        "url": url,
        "fetched_at": float(fetched_at),
        "data": data,
    }


def _write_pricing_cache(cache_path: Path, url: str, data: Dict[str, Any]) -> None:
    """Write pricing cache payload atomically."""
    cache_dir = cache_path.parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url,
        "fetched_at": time.time(),
        "data": data,
    }

    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    tmp_path.replace(cache_path)


def get_event_index_cache_path() -> Path:
    """Get cache file path for parsed rollout token events."""
    env_path = os.getenv("CODEX_RATELIMIT_EVENT_CACHE_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_EVENT_INDEX_CACHE_PATH


def _read_event_index_cache(cache_path: Path) -> Dict[str, Any]:
    """Read parsed token event cache, returning an empty cache on failure."""
    empty_cache: Dict[str, Any] = {"version": EVENT_INDEX_CACHE_VERSION, "files": {}}

    payload: Any = None
    try:
        with open(cache_path, 'rb') as f:
            payload = pickle.load(f)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ValueError):
        payload = None

    # Backward compatibility with old JSON cache format.
    if payload is None:
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return empty_cache

    if not isinstance(payload, dict):
        return empty_cache
    try:
        version = int(payload.get("version", -1))
    except (TypeError, ValueError):
        return empty_cache
    if version != EVENT_INDEX_CACHE_VERSION:
        return empty_cache

    files_payload = payload.get("files")
    if not isinstance(files_payload, dict):
        return empty_cache

    normalized_files: Dict[str, Dict[str, Any]] = {}
    for file_key, entry in files_payload.items():
        if not isinstance(file_key, str) or not isinstance(entry, dict):
            continue
        size = entry.get("size")
        mtime_ns = entry.get("mtime_ns")
        parse_errors = entry.get("parse_errors", 0)
        events = entry.get("events")
        if not isinstance(size, int) or size < 0:
            continue
        if not isinstance(mtime_ns, int) or mtime_ns < 0:
            continue
        if not isinstance(parse_errors, int) or parse_errors < 0:
            parse_errors = 0
        if not isinstance(events, list):
            continue

        normalized_files[file_key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "parse_errors": parse_errors,
            "events": events,
        }

    return {"version": EVENT_INDEX_CACHE_VERSION, "files": normalized_files}


def _write_event_index_cache(cache_path: Path, files_payload: Dict[str, Dict[str, Any]]) -> None:
    """Write parsed token event cache atomically."""
    cache_dir = cache_path.parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": EVENT_INDEX_CACHE_VERSION,
        "files": files_payload,
        "updated_at": time.time(),
    }
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with open(tmp_path, 'wb') as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp_path.replace(cache_path)


def parse_rollout_file_token_events(file_path: Path) -> Dict[str, Any]:
    """Parse a rollout file into normalized token_count events for caching."""
    parse_errors = 0
    events: List[Dict[str, Any]] = []
    current_model: Optional[str] = None
    previous_totals: Optional[Dict[str, int]] = None

    with open(file_path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if 'token_count' not in line and 'turn_context' not in line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            if not isinstance(record, dict):
                continue

            entry_type = record.get('type')
            payload = record.get('payload')
            if not isinstance(payload, dict):
                continue

            if entry_type == 'turn_context':
                context_model = extract_model_from_object(payload)
                if context_model:
                    current_model = context_model
                continue

            if entry_type != 'event_msg':
                continue
            if payload.get('type') != 'token_count':
                continue

            timestamp = parse_iso_timestamp(record.get('timestamp', ''))
            if timestamp is None:
                continue

            info = payload.get('info')
            if not isinstance(info, dict):
                continue

            last_usage = normalize_usage(info.get('last_token_usage'))
            total_usage = normalize_usage(info.get('total_token_usage'))
            raw_usage = last_usage
            if raw_usage is None and total_usage is not None:
                raw_usage = subtract_usage(total_usage, previous_totals)

            if total_usage is not None:
                previous_totals = total_usage

            if raw_usage is None or not usage_has_tokens(raw_usage):
                continue

            signature = build_token_event_signature(record)
            if not signature:
                continue

            extracted_model = extract_model_from_object({
                'info': info,
                'model': payload.get('model'),
                'metadata': payload.get('metadata'),
            })
            if extracted_model:
                current_model = extracted_model

            model_candidate = extracted_model or current_model
            timestamp_utc = timestamp.astimezone(timezone.utc)
            events.append({
                "timestamp": timestamp_utc.timestamp(),
                "signature": signature,
                "model": model_candidate,
                "input_tokens": int(raw_usage.get('input_tokens', 0) or 0),
                "cached_input_tokens": int(raw_usage.get('cached_input_tokens', 0) or 0),
                "output_tokens": int(raw_usage.get('output_tokens', 0) or 0),
                "reasoning_output_tokens": int(raw_usage.get('reasoning_output_tokens', 0) or 0),
                "total_tokens": int(raw_usage.get('total_tokens', 0) or 0),
            })

    return {
        "parse_errors": parse_errors,
        "events": events,
    }


def load_recent_usage_events(
    base_path: Path,
    recent_days: int,
    fallback_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Load token usage events in the recent-days window with model attribution."""
    now_local = datetime.now().astimezone()
    window_start_local = get_recent_window_start(recent_days, now_local=now_local)
    now_utc = now_local.astimezone(timezone.utc)
    window_start_utc = window_start_local.astimezone(timezone.utc)
    now_utc_epoch = now_utc.timestamp()
    window_start_utc_epoch = window_start_utc.timestamp()

    events: List[Dict[str, Any]] = []
    seen_event_signatures: set[str] = set()
    deduplicated_events = 0
    scanned_files = 0
    parse_errors = 0
    fallback_model_name = fallback_model or LEGACY_FALLBACK_MODEL

    rollout_files = list(iterate_all_rollout_files(base_path))
    scanned_files = len(rollout_files)

    cache_path = get_event_index_cache_path()
    cache_payload = _read_event_index_cache(cache_path)
    cached_files: Dict[str, Dict[str, Any]] = cache_payload.get("files", {})
    refreshed_files: Dict[str, Dict[str, Any]] = {}
    cache_changed = False

    for file_path in rollout_files:
        file_key = str(file_path)
        try:
            stat_result = file_path.stat()
        except OSError:
            cache_changed = cache_changed or (file_key in cached_files)
            continue

        size = int(stat_result.st_size)
        mtime_ns = int(stat_result.st_mtime_ns)
        cached_entry = cached_files.get(file_key)
        cached_size = None
        cached_mtime_ns = None
        if isinstance(cached_entry, dict):
            try:
                cached_size = int(cached_entry.get("size", -1))
            except (TypeError, ValueError):
                cached_size = None
            try:
                cached_mtime_ns = int(cached_entry.get("mtime_ns", -1))
            except (TypeError, ValueError):
                cached_mtime_ns = None

        if (
            isinstance(cached_entry, dict)
            and cached_size == size
            and cached_mtime_ns == mtime_ns
            and isinstance(cached_entry.get("events"), list)
        ):
            entry = cached_entry
        else:
            try:
                parsed = parse_rollout_file_token_events(file_path)
            except OSError:
                cache_changed = cache_changed or (file_key in cached_files)
                continue
            entry = {
                "size": size,
                "mtime_ns": mtime_ns,
                "parse_errors": int(parsed.get("parse_errors", 0) or 0),
                "events": parsed.get("events", []),
            }
            cache_changed = True

        refreshed_files[file_key] = entry
        parse_errors += int(entry.get("parse_errors", 0) or 0)

    if len(refreshed_files) != len(cached_files):
        cache_changed = True

    if cache_changed:
        try:
            _write_event_index_cache(cache_path, refreshed_files)
        except OSError:
            pass

    for file_path in rollout_files:
        file_key = str(file_path)
        entry = refreshed_files.get(file_key)
        if not isinstance(entry, dict):
            continue
        file_events = entry.get("events")
        if not isinstance(file_events, list):
            continue

        for cached_event in file_events:
            if not isinstance(cached_event, dict):
                continue

            try:
                timestamp_epoch = float(cached_event.get("timestamp"))
            except (TypeError, ValueError):
                continue
            if timestamp_epoch > now_utc_epoch:
                continue

            signature = cached_event.get("signature")
            if not isinstance(signature, str) or not signature:
                continue
            if signature in seen_event_signatures:
                deduplicated_events += 1
                continue
            seen_event_signatures.add(signature)

            if timestamp_epoch < window_start_utc_epoch:
                continue

            timestamp = datetime.fromtimestamp(timestamp_epoch, tz=timezone.utc)
            timestamp_local = timestamp.astimezone()
            model_candidate = cached_event.get("model")
            if isinstance(model_candidate, str):
                stripped_model = model_candidate.strip()
            else:
                stripped_model = ""
            has_model = bool(stripped_model)
            model_name = stripped_model if has_model else fallback_model_name

            events.append({
                'timestamp': timestamp,
                'timestamp_local': timestamp_local,
                'model': model_name,
                'input_tokens': int(cached_event.get('input_tokens', 0) or 0),
                'cached_input_tokens': int(cached_event.get('cached_input_tokens', 0) or 0),
                'output_tokens': int(cached_event.get('output_tokens', 0) or 0),
                'reasoning_output_tokens': int(cached_event.get('reasoning_output_tokens', 0) or 0),
                'total_tokens': int(cached_event.get('total_tokens', 0) or 0),
                'used_fallback_model': not has_model,
            })

    events.sort(key=lambda e: e['timestamp'])
    return {
        'events': events,
        'window_start_local': window_start_local,
        'window_end_local': now_local,
        'scanned_files': scanned_files,
        'parse_errors': parse_errors,
        'deduplicated_events': deduplicated_events,
        'fallback_model': fallback_model_name,
    }


def load_litellm_pricing_map(
    url: str = LITELLM_PRICING_URL,
    cache_path: Optional[Path] = None,
    cache_ttl_seconds: Optional[int] = None,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """
    Load LiteLLM pricing map with local cache.

    Returns:
        (pricing_map, source), where source is one of:
        - "cache_fresh"
        - "network"
        - "cache_stale_fallback"
    """
    actual_cache_path = cache_path or get_pricing_cache_path()
    ttl_seconds = cache_ttl_seconds if cache_ttl_seconds is not None else get_pricing_cache_ttl_seconds()

    cached_payload = _read_pricing_cache(actual_cache_path)
    if cached_payload and cached_payload.get("url") == url:
        age_seconds = max(0.0, time.time() - float(cached_payload["fetched_at"]))
        if age_seconds <= ttl_seconds:
            return cached_payload["data"], "cache_fresh"

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            body = response.read().decode('utf-8')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Pricing map is not a JSON object")
        _write_pricing_cache(actual_cache_path, url, data)
        return data, "network"
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
        if cached_payload and cached_payload.get("url") == url:
            return cached_payload["data"], "cache_stale_fallback"
        raise


def resolve_model_pricing(
    model_name: str,
    pricing_map: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve pricing entry for a model via exact/prefix/alias/fuzzy matching."""
    aliases = [model_name]
    alias = CODEX_MODEL_ALIASES.get(model_name)
    if alias:
        aliases.append(alias)

    for candidate in aliases:
        if candidate in pricing_map and isinstance(pricing_map[candidate], dict):
            return candidate, pricing_map[candidate]

    prefixed_candidates = []
    for base in aliases:
        for prefix in CODEX_PROVIDER_PREFIXES:
            prefixed_candidates.append(f"{prefix}{base}")

    for candidate in prefixed_candidates:
        if candidate in pricing_map and isinstance(pricing_map[candidate], dict):
            return candidate, pricing_map[candidate]

    lower_aliases = [a.lower() for a in aliases]
    for key, value in pricing_map.items():
        if not isinstance(value, dict):
            continue
        key_lower = key.lower()
        for lower_model in lower_aliases:
            if lower_model in key_lower or key_lower in lower_model:
                return key, value

    return None, None


def usage_totals_template() -> Dict[str, int]:
    """Create an empty usage totals dictionary."""
    return {
        'input_tokens': 0,
        'cached_input_tokens': 0,
        'output_tokens': 0,
        'reasoning_output_tokens': 0,
        'total_tokens': 0,
    }


def add_usage(acc: Dict[str, int], delta: Dict[str, Any]) -> None:
    """Accumulate usage counters."""
    for field in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens'):
        acc[field] += int(delta.get(field, 0) or 0)


def calculate_usage_cost_usd(usage: Dict[str, int], pricing: Dict[str, Any]) -> float:
    """Calculate USD cost with ccusage-compatible token accounting."""
    input_price = float(pricing.get('input_cost_per_token') or 0.0)
    cached_price = float(pricing.get('cache_read_input_token_cost') or input_price or 0.0)
    output_price = float(pricing.get('output_cost_per_token') or 0.0)
    input_tokens = int(usage.get('input_tokens', 0) or 0)
    cached_tokens_raw = int(usage.get('cached_input_tokens', 0) or 0)
    output_tokens = int(usage.get('output_tokens', 0) or 0)

    # Match ccusage: input is non-cached input, cached is capped by input.
    cached_tokens = min(cached_tokens_raw, input_tokens)
    non_cached_input_tokens = max(0, input_tokens - cached_tokens)

    input_cost = non_cached_input_tokens * input_price
    output_cost = usage.get('output_tokens', 0) * output_price
    cached_cost = cached_tokens * cached_price

    return input_cost + cached_cost + output_cost


def usage_to_table_metrics(usage: Dict[str, int]) -> Dict[str, int]:
    """Convert raw usage counters to ccusage-style table metrics."""
    input_tokens = int(usage.get('input_tokens', 0) or 0)
    cached_tokens_raw = int(usage.get('cached_input_tokens', 0) or 0)
    output_tokens = int(usage.get('output_tokens', 0) or 0)
    reasoning_tokens = int(usage.get('reasoning_output_tokens', 0) or 0)

    cached_read_tokens = min(cached_tokens_raw, input_tokens)
    non_cached_input_tokens = max(0, input_tokens - cached_read_tokens)
    total_tokens = non_cached_input_tokens + cached_read_tokens + output_tokens

    return {
        'input_tokens': non_cached_input_tokens,
        'cached_input_tokens': cached_read_tokens,
        'output_tokens': output_tokens,
        'reasoning_output_tokens': reasoning_tokens,
        'total_tokens': total_tokens,
    }


def _truncate_with_ellipsis(text: str, width: int) -> str:
    """Truncate text to width with trailing ellipsis."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[:width - 1] + "…"


def _format_table_cell(text: str, width: int, align: str = "left") -> str:
    """Format a single table cell with truncation and alignment."""
    clipped = _truncate_with_ellipsis(text, width)
    if align == "right":
        return clipped.rjust(width)
    return clipped.ljust(width)


def _format_count_for_table(value: int, width: int) -> str:
    """Format numeric count for fixed-width table cells."""
    return _format_table_cell(f"{int(value):,}", width, align="right")


def _format_cost_for_table(cost_usd: Optional[float], width: int) -> str:
    """Format USD value for fixed-width table cells."""
    if cost_usd is None:
        return _format_table_cell("-", width, align="right")
    return _format_table_cell(f"${cost_usd:,.2f}", width, align="right")


def render_recent_usage_table(summary: Dict[str, Any]) -> str:
    """Render recent usage summary as a box-drawing table."""
    columns = [
        ("Date", 11, "left"),
        ("Models", 39, "left"),
        ("Input", 9, "right"),
        ("Output", 8, "right"),
        ("Reasoni…", 8, "right"),
        ("Cache", 11, "right"),
        ("Total", 12, "right"),
        ("Cost", 9, "right"),
    ]
    sub_header = ["", "", "", "", "", "Read", "Tokens", "(USD)"]

    widths = [c[1] for c in columns]
    aligns = [c[2] for c in columns]

    def build_separator(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * width for width in widths) + right

    def build_row(cells: List[str]) -> str:
        parts = []
        for idx, cell in enumerate(cells):
            parts.append(_format_table_cell(cell, widths[idx], align=aligns[idx]))
        return "│" + "│".join(parts) + "│"

    lines = []
    lines.append(build_separator("┌", "┬", "┐"))
    lines.append(build_row([c[0] for c in columns]))
    lines.append(build_row(sub_header))
    lines.append(build_separator("├", "┼", "┤"))

    daily_rows: List[Dict[str, Any]] = summary.get('daily', [])
    for row_idx, day_row in enumerate(daily_rows):
        date_key = day_row.get('date', '')
        try:
            dt = datetime.strptime(date_key, "%Y-%m-%d")
            date_lines = [f"{dt.strftime('%b')} {dt.day},", f"{dt.year}"]
        except ValueError:
            date_lines = [date_key]

        model_lines = [f"- {name}" for name in day_row.get('models', [])]
        if not model_lines:
            model_lines = [""]

        usage_metrics = usage_to_table_metrics(day_row.get('usage', {}))
        numeric_first_line = [
            _format_count_for_table(usage_metrics['input_tokens'], widths[2]),
            _format_count_for_table(usage_metrics['output_tokens'], widths[3]),
            _format_count_for_table(usage_metrics['reasoning_output_tokens'], widths[4]),
            _format_count_for_table(usage_metrics['cached_input_tokens'], widths[5]),
            _format_count_for_table(usage_metrics['total_tokens'], widths[6]),
            _format_cost_for_table(day_row.get('usd'), widths[7]),
        ]
        numeric_blank_line = [" " * widths[2], " " * widths[3], " " * widths[4], " " * widths[5], " " * widths[6], " " * widths[7]]

        line_count = max(len(date_lines), len(model_lines))
        for i in range(line_count):
            date_cell = date_lines[i] if i < len(date_lines) else ""
            model_cell = model_lines[i] if i < len(model_lines) else ""
            nums = numeric_first_line if i == 0 else numeric_blank_line
            lines.append(
                "│" + "│".join(
                    [
                        _format_table_cell(date_cell, widths[0], aligns[0]),
                        _format_table_cell(model_cell, widths[1], aligns[1]),
                        nums[0],
                        nums[1],
                        nums[2],
                        nums[3],
                        nums[4],
                        nums[5],
                    ]
                ) + "│"
            )

        if row_idx != len(daily_rows) - 1:
            lines.append(build_separator("├", "┼", "┤"))

    if daily_rows:
        lines.append(build_separator("├", "┼", "┤"))

    total_metrics = usage_to_table_metrics(summary.get('totals', {}))
    total_row = [
        "Total",
        "",
        _format_count_for_table(total_metrics['input_tokens'], widths[2]),
        _format_count_for_table(total_metrics['output_tokens'], widths[3]),
        _format_count_for_table(total_metrics['reasoning_output_tokens'], widths[4]),
        _format_count_for_table(total_metrics['cached_input_tokens'], widths[5]),
        _format_count_for_table(total_metrics['total_tokens'], widths[6]),
        _format_cost_for_table(summary.get('usd_total') if summary.get('cost_enabled') else None, widths[7]),
    ]
    lines.append(
        "│" + "│".join(
            [
                _format_table_cell(total_row[0], widths[0], aligns[0]),
                _format_table_cell(total_row[1], widths[1], aligns[1]),
                total_row[2],
                total_row[3],
                total_row[4],
                total_row[5],
                total_row[6],
                total_row[7],
            ]
        ) + "│"
    )
    lines.append(build_separator("└", "┴", "┘"))

    return "\n".join(lines)


def summarize_recent_usage_with_cost(
    base_path: Path,
    recent_days: int,
    enable_cost: bool = False,
    pricing_url: str = LITELLM_PRICING_URL,
) -> Dict[str, Any]:
    """Summarize recent token usage and estimate USD cost by model."""
    default_model = read_default_model_from_config() or LEGACY_FALLBACK_MODEL
    events_data = load_recent_usage_events(base_path, recent_days, fallback_model=default_model)
    events = events_data['events']

    totals = usage_totals_template()
    model_usage: Dict[str, Dict[str, int]] = {}
    daily_usage: Dict[str, Dict[str, Any]] = {}
    fallback_events = 0

    for event in events:
        add_usage(totals, event)
        model_name = event.get('model') or default_model
        if model_name not in model_usage:
            model_usage[model_name] = usage_totals_template()
        add_usage(model_usage[model_name], event)

        timestamp_local = event.get('timestamp_local')
        if isinstance(timestamp_local, datetime):
            day_key = timestamp_local.strftime('%Y-%m-%d')
        else:
            day_key = event.get('timestamp').astimezone().strftime('%Y-%m-%d')

        if day_key not in daily_usage:
            daily_usage[day_key] = {
                'usage': usage_totals_template(),
                'models': {},
            }
        add_usage(daily_usage[day_key]['usage'], event)

        day_models = daily_usage[day_key]['models']
        if model_name not in day_models:
            day_models[model_name] = usage_totals_template()
        add_usage(day_models[model_name], event)

        if event.get('used_fallback_model'):
            fallback_events += 1

    pricing_enabled = bool(enable_cost)
    pricing_warning = None
    pricing_source = None
    pricing_map = None
    model_breakdown = {}
    total_cost_usd = 0.0
    priced_models = 0
    unpriced_models = 0

    if pricing_enabled:
        try:
            pricing_map, pricing_source = load_litellm_pricing_map(url=pricing_url)
            if pricing_source == "cache_stale_fallback":
                pricing_warning = "Using stale cached pricing data because refresh failed"
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as e:
            pricing_warning = f"Failed to load pricing map: {e}"

    for model_name, usage in sorted(model_usage.items()):
        matched_model, pricing = (None, None)
        model_cost = None
        if pricing_map is not None:
            matched_model, pricing = resolve_model_pricing(model_name, pricing_map)
            if pricing is not None:
                model_cost = calculate_usage_cost_usd(usage, pricing)
                total_cost_usd += model_cost
                priced_models += 1
            else:
                unpriced_models += 1
        else:
            unpriced_models += 1

        model_breakdown[model_name] = {
            'usage': usage,
            'usd': model_cost,
            'pricing_model': matched_model,
        }

    daily_rows: List[Dict[str, Any]] = []
    for day_key in sorted(daily_usage.keys()):
        day_payload = daily_usage[day_key]
        day_models_usage: Dict[str, Dict[str, int]] = day_payload['models']
        day_cost: Optional[float] = None

        if pricing_map is not None:
            running_cost = 0.0
            has_priced_model = False
            for day_model_name, day_model_usage in day_models_usage.items():
                _, day_pricing = resolve_model_pricing(day_model_name, pricing_map)
                if day_pricing is None:
                    continue
                running_cost += calculate_usage_cost_usd(day_model_usage, day_pricing)
                has_priced_model = True
            if has_priced_model:
                day_cost = running_cost

        daily_rows.append({
            'date': day_key,
            'usage': day_payload['usage'],
            'models': sorted(day_models_usage.keys()),
            'usd': day_cost if pricing_enabled else None,
        })

    return {
        'recent_days': recent_days,
        'window_start_local': events_data['window_start_local'],
        'window_end_local': events_data['window_end_local'],
        'event_count': len(events),
        'scanned_files': events_data['scanned_files'],
        'parse_errors': events_data['parse_errors'],
        'deduplicated_event_count': events_data.get('deduplicated_events', 0),
        'fallback_model': default_model,
        'fallback_event_count': fallback_events,
        'totals': totals,
        'cost_enabled': pricing_enabled,
        'usd_total': total_cost_usd if pricing_enabled and pricing_map is not None else None,
        'pricing_url': pricing_url,
        'pricing_source': pricing_source,
        'pricing_warning': pricing_warning,
        'priced_models': priced_models,
        'unpriced_models': unpriced_models,
        'models': model_breakdown,
        'daily': daily_rows,
    }


def get_session_files_with_mtime(base_path: Path, days_back: int = 7) -> list:
    """
    Collect all session files from the last N days with their modification times.

    Args:
        base_path: Base path for session files
        days_back: Number of days to search backwards

    Returns:
        List of tuples (file_path, mtime) sorted by modification time descending
    """
    current_date = datetime.now()
    files_with_mtime = []

    for days in range(days_back):
        search_date = current_date - timedelta(days=days)
        date_path = base_path / str(search_date.year) / f"{search_date.month:02d}" / f"{search_date.day:02d}"

        if date_path.exists():
            pattern = str(date_path / "rollout-*.jsonl")
            files = glob.glob(pattern)

            for file_path in files:
                try:
                    file_obj = Path(file_path)
                    mtime = file_obj.stat().st_mtime
                    files_with_mtime.append((file_obj, mtime))
                except (OSError, IOError):
                    continue  # Skip files we can't stat

    # Sort by modification time, most recent first
    files_with_mtime.sort(key=lambda x: x[1], reverse=True)
    return files_with_mtime


def find_latest_token_count_record(base_path: Optional[Path] = None, silent: bool = False) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """
    Find the most recent token_count record using a two-phase search strategy.

    Phase 1 (Fast Path): Check today's directory for recently modified files (within 1 hour)
    Phase 2 (Comprehensive): Search all files from last 7 days, sorted by modification time

    Args:
        base_path: Custom base path for session files
        silent: If True, suppress error messages to stdout

    Returns:
        Tuple of (file_path, record) for the latest token_count event, or None if not found

    Note:
        Uses file modification time to prioritize actively-used sessions,
        even if they're located in past date directories.
    """
    if base_path is None:
        base_path = get_session_base_path()

    current_time = datetime.now()
    one_hour_ago = current_time.timestamp() - 3600

    # Phase 1: Fast path - check today's directory for recently modified files
    today_date_path = base_path / str(current_time.year) / f"{current_time.month:02d}" / f"{current_time.day:02d}"

    if today_date_path.exists():
        pattern = str(today_date_path / "rollout-*.jsonl")
        today_files = glob.glob(pattern)

        # Collect files modified in the last hour with their mtimes
        recent_files = []
        for file_path in today_files:
            try:
                file_obj = Path(file_path)
                mtime = file_obj.stat().st_mtime

                # If file was modified within the last hour, add to candidates
                if mtime > one_hour_ago:
                    recent_files.append((file_obj, mtime))
            except (OSError, IOError):
                continue  # Skip files we can't stat

        # Sort by modification time, most recent first
        recent_files.sort(key=lambda x: x[1], reverse=True)

        # Check files in order of most recent modification
        for file_obj, mtime in recent_files:
            record = parse_session_file(file_obj, silent=silent)
            if record:
                return file_obj, record

    # Phase 2: Comprehensive search - check all files sorted by modification time
    files_with_mtime = get_session_files_with_mtime(base_path, days_back=7)

    for file_path, mtime in files_with_mtime:
        record = parse_session_file(file_path, silent=silent)
        if record:
            return file_path, record

    return None


def validate_token_count_record(record: Dict[str, Any]) -> bool:
    """
    Validate that a token_count record has all required properties.

    Args:
        record: The record to validate

    Returns:
        True if the record has all required properties, False otherwise
    """
    try:
        # Check basic structure
        payload = record.get('payload')
        if not payload or payload.get('type') != 'token_count':
            return False

        # Check info section exists
        info = payload.get('info')
        if not info:
            return False

        # Check required token usage fields exist
        total_usage = info.get('total_token_usage')
        last_usage = info.get('last_token_usage')
        if not total_usage or not last_usage:
            return False

        # Check timestamp exists
        if not record.get('timestamp'):
            return False

        return True
    except Exception:
        return False


def parse_session_file(file_path: Path, silent: bool = False) -> Optional[Dict[str, Any]]:
    """
    Parse the session file and find the latest token_count event.

    Args:
        file_path: Path to the session file
        silent: If True, suppress error messages to stdout

    Returns:
        The latest token_count event data, or None if not found
    """
    latest_record = None
    latest_timestamp = None

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)

                    # Check if this is a valid token_count event with required properties
                    if (record.get('type') == 'event_msg' and
                        validate_token_count_record(record)):

                        timestamp_str = record.get('timestamp')
                        if timestamp_str:
                            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))

                            if latest_timestamp is None or timestamp > latest_timestamp:
                                latest_timestamp = timestamp
                                latest_record = record

                except json.JSONDecodeError:
                    continue  # Skip malformed lines

    except Exception as e:
        if not silent:
            print(f"Error reading session file: {e}", file=sys.stderr)
        return None

    return latest_record


def format_token_usage(usage_data: Dict[str, int]) -> str:
    """Format token usage data into a readable string."""
    input_tokens = usage_data.get('input_tokens', 0)
    cached_tokens = usage_data.get('cached_input_tokens', 0)
    output_tokens = usage_data.get('output_tokens', 0)
    reasoning_tokens = usage_data.get('reasoning_output_tokens', 0)
    total_tokens = usage_data.get('total_tokens', 0)

    return (
        f"input {input_tokens:,}, cached {cached_tokens:,}, "
        f"output {output_tokens:,}, reasoning {reasoning_tokens:,}, subtotal {total_tokens:,}"
    )


def calculate_reset_time(rate_limit: Dict[str, Any], record_timestamp: datetime) -> Tuple[datetime, float, bool]:
    """
    Normalize reset time information for a rate limit entry.

    Args:
        rate_limit: Rate limit payload from the session record
        record_timestamp: Timestamp of the record containing the rate limit

    Returns:
        Tuple of (reset_time, seconds_until_reset, is_outdated)
    """
    resets_at = rate_limit.get('resets_at')
    resets_in_seconds = rate_limit.get('resets_in_seconds')
    tzinfo = record_timestamp.tzinfo

    reset_time: Optional[datetime] = None

    if resets_at is not None:
        try:
            reset_time = datetime.fromtimestamp(float(resets_at), tz=tzinfo)
        except (OSError, OverflowError, ValueError, TypeError):
            reset_time = None

    if reset_time is None and resets_in_seconds is not None:
        try:
            reset_time = record_timestamp + timedelta(seconds=float(resets_in_seconds))
        except (OverflowError, TypeError, ValueError):
            reset_time = None

    current_time = datetime.now(tzinfo)

    if reset_time is None:
        # Fall back to record timestamp and mark the value as outdated.
        reset_time = record_timestamp
        seconds_until_reset = 0.0
        is_outdated = True
    else:
        delta_seconds = (reset_time - current_time).total_seconds()
        seconds_until_reset = max(0.0, delta_seconds)
        is_outdated = reset_time <= current_time

    return reset_time, seconds_until_reset, is_outdated


def get_rate_limit_data(base_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Get rate limit data and return structured information for display."""
    result = find_latest_token_count_record(base_path)
    if not result:
        return None

    latest_file, record = result

    # Defensive validation - this should already be validated, but double-check
    if not validate_token_count_record(record):
        return None

    try:
        payload = record['payload']
        info = payload['info']
        rate_limits = payload.get('rate_limits', {})
        record_timestamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
        current_time_local = datetime.now().astimezone()

        data = {
            'file_path': latest_file,
            'record_timestamp': record_timestamp,
            'current_time': current_time_local,
            'total_usage': info['total_token_usage'],
            'last_usage': info['last_token_usage']
        }
    except Exception as e:
        print(f"Error processing rate limit data: {e}")
        return None

    # Process primary (5h) rate limits
    try:
        primary = rate_limits.get('primary', {})
        if primary:
            primary_reset_time, primary_seconds_until_reset, primary_outdated = calculate_reset_time(primary, record_timestamp)

            window_minutes_raw = primary.get('window_minutes', 299)
            try:
                window_minutes = float(window_minutes_raw)
            except (TypeError, ValueError):
                window_minutes = 0.0
            window_seconds = max(0.0, window_minutes * 60)

            if window_seconds > 0:
                elapsed_seconds = window_seconds - primary_seconds_until_reset
                elapsed_seconds = max(0.0, min(window_seconds, elapsed_seconds))
                time_percent = (elapsed_seconds / window_seconds) * 100
            else:
                time_percent = 0.0

            data['primary'] = {
                'used_percent': primary.get('used_percent', 0),
                'time_percent': max(0.0, min(100.0, time_percent)),
                'reset_time': primary_reset_time,
                'seconds_until_reset': primary_seconds_until_reset,
                'outdated': primary_outdated,
                'window_minutes': window_minutes_raw,
                'resets_at': primary.get('resets_at'),
                'resets_in_seconds': primary.get('resets_in_seconds'),
            }
    except Exception as e:
        print(f"Error processing primary rate limit data: {e}")

    # Process secondary (weekly) rate limits
    try:
        secondary = rate_limits.get('secondary', {})
        if secondary:
            secondary_reset_time, secondary_seconds_until_reset, secondary_outdated = calculate_reset_time(secondary, record_timestamp)

            window_minutes_raw = secondary.get('window_minutes', 10079)
            try:
                window_minutes = float(window_minutes_raw)
            except (TypeError, ValueError):
                window_minutes = 0.0
            window_seconds = max(0.0, window_minutes * 60)

            if window_seconds > 0:
                elapsed_seconds = window_seconds - secondary_seconds_until_reset
                elapsed_seconds = max(0.0, min(window_seconds, elapsed_seconds))
                time_percent = (elapsed_seconds / window_seconds) * 100
            else:
                time_percent = 0.0

            data['secondary'] = {
                'used_percent': secondary.get('used_percent', 0),
                'time_percent': max(0.0, min(100.0, time_percent)),
                'reset_time': secondary_reset_time,
                'seconds_until_reset': secondary_seconds_until_reset,
                'outdated': secondary_outdated,
                'window_minutes': window_minutes_raw,
                'resets_at': secondary.get('resets_at'),
                'resets_in_seconds': secondary.get('resets_in_seconds'),
            }
    except Exception as e:
        print(f"Error processing secondary rate limit data: {e}")

    return data


def draw_progress_bar(
    stdscr,
    y: int,
    x: int,
    bar_width: int,
    percent: float,
    label: str,
    details: str = "",
    total_width: int = 70,
    outdated: bool = False,
    is_usage: bool = False,
    warning_threshold: int = 70,
    colors_enabled: bool = False,
) -> None:
    """Draw a progress bar at the specified position."""
    color_attr = 0

    if outdated:
        bar = "-" * bar_width
        percent_text = "  N/A"
        color_pair = 0  # Default color
    else:
        filled_width = int((percent / 100.0) * bar_width)
        bar = "█" * filled_width + "░" * (bar_width - filled_width)
        percent_text = f"{percent:5.1f}%"

        # Determine color based on usage threshold
        if is_usage and percent >= warning_threshold:
            color_pair = 2  # Dark red
        elif is_usage:
            color_pair = 1  # Green
        else:
            color_pair = 0  # Default color for time bars

        if is_usage and colors_enabled and color_pair:
            try:
                color_attr = curses.color_pair(color_pair)
            except curses.error:
                color_attr = 0

    try:
        left_edge = x
        right_edge = x + total_width - 1
        content_start = left_edge + 1
        content_width = total_width - 2

        # Prepare formatted pieces
        label_text = pad_label_to_width(label)
        label_x = content_start + 1  # leave one-space margin after border
        bar_x = label_x + LABEL_AREA_WIDTH + 1  # space between label area and bar
        percent_x = right_edge - len(percent_text) - 3  # leave three spaces before border

        # Draw borders
        stdscr.addch(y, left_edge, "│")
        stdscr.addch(y, right_edge, "│")

        # Clear the interior content area
        stdscr.addstr(y, content_start, " " * content_width)

        # Draw label
        stdscr.addstr(y, label_x, label_text)

        # Draw bar at fixed column
        stdscr.addstr(y, bar_x, "[")
        if is_usage and not outdated and color_attr:
            stdscr.addstr(y, bar_x + 1, bar, color_attr)
        else:
            stdscr.addstr(y, bar_x + 1, bar)
        stdscr.addstr(y, bar_x + 1 + bar_width, "]")

        # Ensure at least one space between bar and percentage
        percent_x = max(percent_x, bar_x + 1 + bar_width + 2)

        # Draw percentage value
        stdscr.addstr(y, percent_x, percent_text)
    except curses.error:
        pass  # Skip if can't draw

    # Draw details line if provided
    if details:
        detail_template = f"    {details}"
        detail_length = len(detail_template)

        if detail_length <= content_width:
            detail_padding = content_width - detail_length
            detail_line = f"│{detail_template}{' ' * detail_padding}│"
        else:
            truncated_detail = detail_template[:content_width]
            detail_line = f"│{truncated_detail}│"

        try:
            stdscr.addstr(y + 1, x, detail_line)
        except curses.error:
            pass  # Skip if can't draw



def run_tui(base_path: Optional[Path], refresh_interval: int, warning_threshold: int = 70) -> None:
    """Run the TUI interface."""
    def tui_main(stdscr):
        # Configure curses
        curses.curs_set(0)  # Hide cursor
        stdscr.nodelay(True)  # Non-blocking input
        stdscr.timeout(100)  # 100ms timeout for getch()
        curses.use_default_colors()  # Use terminal's default colors

        colors_enabled = False
        if curses.has_colors():
            try:
                curses.start_color()
                curses.init_pair(1, curses.COLOR_GREEN, -1)  # Green text on default background
                curses.init_pair(2, curses.COLOR_RED, -1)    # Dark red text on default background
                colors_enabled = True
            except curses.error:
                colors_enabled = False

        # Get terminal dimensions
        max_y, max_x = stdscr.getmaxyx()

        last_refresh = 0

        while True:
            current_time = time.time()

            # Check for 'q' key to quit
            key = stdscr.getch()
            if key == ord('q') or key == ord('Q'):
                break

            # Refresh data based on interval
            if current_time - last_refresh >= refresh_interval:
                stdscr.clear()

                # Get current data
                data = get_rate_limit_data(base_path)

                if not data:
                    stdscr.addstr(2, 2, "No token_count events found in session files.")
                    stdscr.addstr(3, 2, "Press 'q' to quit.")
                else:
                    # Header
                    header = "CODEX RATELIMIT - LIVE USAGE MONITOR"
                    total_width = 74  # Extended by 2 characters
                    content_width = total_width - 2
                    header_padding = (content_width - len(header)) // 2

                    # Check if we have enough space to draw
                    if max_y < 20 or max_x < 76:
                        stdscr.addstr(1, 2, "Terminal too small! Need at least 76x20")
                        stdscr.refresh()
                        continue

                    try:
                        stdscr.addstr(1, 2, "┌" + "─" * content_width + "┐")
                        stdscr.addstr(2, 2, f"│{' ' * header_padding}{header}{' ' * (content_width - header_padding - len(header))}│")
                        stdscr.addstr(3, 2, "├" + "─" * content_width + "┤")
                    except curses.error:
                        stdscr.addstr(1, 2, "Display error - terminal too small")
                        stdscr.refresh()
                        continue

                    y_pos = 4

                    # 5-hour session bars
                    if 'primary' in data:
                        primary = data['primary']

                        # Session time bar
                        reset_time_str = primary['reset_time'].astimezone().strftime('%m-%d %H:%M:%S')
                        outdated_str = " [OUTDATED]" if primary['outdated'] else ""
                        time_details = f"Reset: {reset_time_str}{outdated_str}"
                        draw_progress_bar(
                            stdscr,
                            y_pos,
                            2,
                            BAR_WIDTH,
                            primary['time_percent'],
                            "5H SESSION",
                            time_details,
                            total_width,
                            outdated=primary['outdated'],
                            is_usage=False,
                            warning_threshold=warning_threshold,
                            colors_enabled=colors_enabled,
                        )
                        y_pos += 2

                        # Border line after 5H SESSION
                        try:
                            stdscr.addstr(y_pos, 2, "├" + "─" * content_width + "┤")
                        except curses.error:
                            pass
                        y_pos += 1

                        # Session usage bar
                        draw_progress_bar(
                            stdscr,
                            y_pos,
                            2,
                            BAR_WIDTH,
                            primary['used_percent'],
                            "5H USAGE",
                            "",
                            total_width,
                            outdated=primary['outdated'],
                            is_usage=True,
                            warning_threshold=warning_threshold,
                            colors_enabled=colors_enabled,
                        )
                        y_pos += 1

                    # Weekly bars
                    if 'secondary' in data:
                        # Border line before WEEK TIME
                        try:
                            stdscr.addstr(y_pos, 2, "├" + "─" * content_width + "┤")
                        except curses.error:
                            pass
                        y_pos += 1

                        secondary = data['secondary']

                        # Weekly time bar
                        reset_time_str = secondary['reset_time'].astimezone().strftime('%m-%d %H:%M:%S')
                        outdated_str = " [OUTDATED]" if secondary['outdated'] else ""
                        time_details = f"Reset: {reset_time_str}{outdated_str}"
                        draw_progress_bar(
                            stdscr,
                            y_pos,
                            2,
                            BAR_WIDTH,
                            secondary['time_percent'],
                            "WEEKLY TIME",
                            time_details,
                            total_width,
                            outdated=secondary['outdated'],
                            is_usage=False,
                            warning_threshold=warning_threshold,
                            colors_enabled=colors_enabled,
                        )
                        y_pos += 2

                        # Border line after WEEK TIME
                        try:
                            stdscr.addstr(y_pos, 2, "├" + "─" * content_width + "┤")
                        except curses.error:
                            pass
                        y_pos += 1

                        # Weekly usage bar
                        draw_progress_bar(
                            stdscr,
                            y_pos,
                            2,
                            BAR_WIDTH,
                            secondary['used_percent'],
                            "WEEKLY USAGE",
                            "",
                            total_width,
                            outdated=secondary['outdated'],
                            is_usage=True,
                            warning_threshold=warning_threshold,
                            colors_enabled=colors_enabled,
                        )
                        y_pos += 1

                    # Footer info
                    try:
                        stdscr.addstr(y_pos, 2, "├" + "─" * content_width + "┤")

                        # Last update line
                        # content_width already defined above
                        last_update_content = f" Last update: {data['current_time'].strftime('%Y-%m-%d %H:%M:%S')}"
                        if len(last_update_content) > content_width:
                            last_update_content = last_update_content[:content_width]
                        last_update_padding = content_width - len(last_update_content)
                        last_update_line = f"│{last_update_content}{' ' * last_update_padding}│"
                        stdscr.addstr(y_pos + 1, 2, last_update_line)

                        # Refresh interval line
                        refresh_content = f" Refresh interval: {refresh_interval}s | Press 'q' to quit"
                        if len(refresh_content) > content_width:
                            refresh_content = refresh_content[:content_width]
                        refresh_padding = content_width - len(refresh_content)
                        refresh_line = f"│{refresh_content}{' ' * refresh_padding}│"
                        stdscr.addstr(y_pos + 2, 2, refresh_line)

                        stdscr.addstr(y_pos + 3, 2, "└" + "─" * content_width + "┘")
                    except curses.error:
                        pass

                stdscr.refresh()
                last_refresh = current_time

            time.sleep(0.1)

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    try:
        curses.wrapper(tui_main)
    except KeyboardInterrupt:
        pass


def main():
    """Main function to parse and display token usage information."""
    parser = argparse.ArgumentParser(description='Parse Claude Code session token usage and rate limits')
    parser.add_argument('--input-folder', '-i', type=str,
                       help='Custom input folder path (default: ~/.codex/sessions)')
    parser.add_argument('--recent-days', type=int,
                       help='Aggregate usage for recent N days (N=1 means today)')
    parser.add_argument('--cost', action='store_true',
                       help='Enable USD cost estimation from LiteLLM pricing map (off by default)')
    parser.add_argument('--live', action='store_true',
                       help='Launch TUI live monitoring interface')
    parser.add_argument('--interval', type=int, default=10,
                       help='Refresh interval in seconds for live mode (default: 10)')
    parser.add_argument('--warning-threshold', type=int, default=70,
                       help='Usage percentage threshold for warning color (default: 70)')
    parser.add_argument('--json', action='store_true',
                       help='Output data in JSON format')

    args = parser.parse_args()

    # Set up base path
    if args.input_folder:
        base_path = Path(args.input_folder).expanduser()
        if not args.live and not args.json:
            print(f"Using custom input folder: {base_path}")
    else:
        base_path = get_session_base_path()
        if not args.live and not args.json:
            print(f"Using default input folder: {base_path}")

    # Launch TUI if --live flag is used
    if args.live:
        run_tui(base_path, args.interval, args.warning_threshold)
        return

    if args.recent_days is not None:
        if args.recent_days <= 0:
            if args.json:
                print(json.dumps({"error": "--recent-days must be a positive integer"}))
            else:
                print("Error: --recent-days must be a positive integer.")
            return

        summary = summarize_recent_usage_with_cost(
            base_path,
            args.recent_days,
            enable_cost=args.cost,
        )
        totals = summary['totals']

        if args.json:
            payload = {
                "recent_days": summary['recent_days'],
                "window_start_local": summary['window_start_local'].strftime('%Y-%m-%d %H:%M:%S%z'),
                "window_end_local": summary['window_end_local'].strftime('%Y-%m-%d %H:%M:%S%z'),
                "events": summary['event_count'],
                "scanned_files": summary['scanned_files'],
                "parse_errors": summary['parse_errors'],
                "deduplicated_events": summary.get('deduplicated_event_count', 0),
                "fallback_model": summary['fallback_model'],
                "fallback_event_count": summary['fallback_event_count'],
                "cost_enabled": summary['cost_enabled'],
                "totals": {
                    "input": totals.get('input_tokens', 0),
                    "cached": totals.get('cached_input_tokens', 0),
                    "output": totals.get('output_tokens', 0),
                    "reasoning": totals.get('reasoning_output_tokens', 0),
                    "subtotal": totals.get('total_tokens', 0),
                },
                "models": {},
            }
            if summary['cost_enabled']:
                payload.update({
                    "pricing_url": summary['pricing_url'],
                    "pricing_source": summary['pricing_source'],
                    "pricing_warning": summary['pricing_warning'],
                    "usd_total": summary['usd_total'],
                    "priced_models": summary['priced_models'],
                    "unpriced_models": summary['unpriced_models'],
                })
            for model_name, model_data in summary['models'].items():
                usage = model_data['usage']
                model_payload = {
                    "usage": {
                        "input": usage.get('input_tokens', 0),
                        "cached": usage.get('cached_input_tokens', 0),
                        "output": usage.get('output_tokens', 0),
                        "reasoning": usage.get('reasoning_output_tokens', 0),
                        "subtotal": usage.get('total_tokens', 0),
                    }
                }
                if summary['cost_enabled']:
                    model_payload["usd"] = model_data.get('usd')
                    model_payload["pricing_model"] = model_data.get('pricing_model')
                payload['models'][model_name] = model_payload
            print(json.dumps(payload, indent=2))
        else:
            window_start = summary['window_start_local'].strftime('%Y-%m-%d %H:%M:%S')
            window_end = summary['window_end_local'].strftime('%Y-%m-%d %H:%M:%S')
            print(f"Recent {summary['recent_days']} day(s) window: {window_start} -> {window_end}")
            deduped = summary.get('deduplicated_event_count', 0)
            print(
                f"Events: {summary['event_count']} "
                f"(scanned files: {summary['scanned_files']}, parse errors: {summary['parse_errors']}, deduplicated: {deduped})"
            )
            print(f"Fallback model: {summary['fallback_model']} (applied on {summary['fallback_event_count']} events)")
            if summary['cost_enabled']:
                if summary.get('pricing_source'):
                    print(f"Pricing source: {summary['pricing_source']}")
                if summary['pricing_warning']:
                    print(f"Pricing warning: {summary['pricing_warning']}")

            print(render_recent_usage_table(summary))
        return

    if not args.json:
        print("Searching for latest token_count event...")

    # Find the latest token_count record
    result = find_latest_token_count_record(base_path, silent=args.json)
    if not result:
        if args.json:
            print(json.dumps({"error": "No token_count events found in session files"}))
        else:
            print("No token_count events found in session files.")
        return

    latest_file, record = result
    if not args.json:
        print(f"Found latest token_count event in: {latest_file}")

    # Validate record structure before processing
    if not validate_token_count_record(record):
        if args.json:
            print(json.dumps({"error": "Found token_count event has invalid or missing required properties"}))
        else:
            print("Error: Found token_count event has invalid or missing required properties.")
        return

    try:
        # Extract data from the record
        payload = record['payload']
        info = payload['info']
        rate_limits = payload.get('rate_limits', {})

        record_timestamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"Error processing token_count record: {e}"}))
        else:
            print(f"Error processing token_count record: {e}")
        return

    # Prepare data for output
    try:
        total_usage = info['total_token_usage']
        last_usage = info['last_token_usage']

        output_data = {
            "total": {
                "input": total_usage.get('input_tokens', 0),
                "cached": total_usage.get('cached_input_tokens', 0),
                "output": total_usage.get('output_tokens', 0),
                "reasoning": total_usage.get('reasoning_output_tokens', 0),
                "subtotal": total_usage.get('total_tokens', 0)
            },
            "last": {
                "input": last_usage.get('input_tokens', 0),
                "cached": last_usage.get('cached_input_tokens', 0),
                "output": last_usage.get('output_tokens', 0),
                "reasoning": last_usage.get('reasoning_output_tokens', 0),
                "subtotal": last_usage.get('total_tokens', 0)
            }
        }

        # Add source file path for JSON output
        if args.json:
            output_data["source_file"] = str(latest_file)

        # Add rate limit information
        primary = rate_limits.get('primary', {})
        if primary:
            primary_percent = primary.get('used_percent', 0)
            primary_reset_time, primary_seconds_until_reset, primary_outdated = calculate_reset_time(primary, record_timestamp)

            output_data["limit_5h"] = {
                "used_percent": primary_percent,
                "reset_time": primary_reset_time.astimezone().strftime('%Y-%m-%d %H:%M:%S'),
                "seconds_until_reset": int(round(primary_seconds_until_reset)),
                "outdated": primary_outdated,
                "resets_at": primary.get('resets_at'),
                "resets_in_seconds": primary.get('resets_in_seconds'),
            }

        secondary = rate_limits.get('secondary', {})
        if secondary:
            secondary_percent = secondary.get('used_percent', 0)
            secondary_reset_time, secondary_seconds_until_reset, secondary_outdated = calculate_reset_time(secondary, record_timestamp)

            output_data["limit_weekly"] = {
                "used_percent": secondary_percent,
                "reset_time": secondary_reset_time.astimezone().strftime('%Y-%m-%d %H:%M:%S'),
                "seconds_until_reset": int(round(secondary_seconds_until_reset)),
                "outdated": secondary_outdated,
                "resets_at": secondary.get('resets_at'),
                "resets_in_seconds": secondary.get('resets_in_seconds'),
            }

        # Output in JSON or text format
        if args.json:
            print(json.dumps(output_data, indent=2))
        else:
            # Display token usage
            print(f"total: {format_token_usage(total_usage)}")
            print(f"last:  {format_token_usage(last_usage)}")

            # Display rate limits
            if 'limit_5h' in output_data:
                limit_5h = output_data['limit_5h']
                outdated_str = " [OUTDATED]" if limit_5h['outdated'] else ""
                print(f"5h limit: used {limit_5h['used_percent']}%, reset: {limit_5h['reset_time']}{outdated_str}")

            if 'limit_weekly' in output_data:
                limit_weekly = output_data['limit_weekly']
                outdated_str = " [OUTDATED]" if limit_weekly['outdated'] else ""
                print(f"weekly limit: used {limit_weekly['used_percent']}%, reset: {limit_weekly['reset_time']}{outdated_str}")

    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"Error processing data: {e}"}))
        else:
            print(f"Error processing data: {e}")


if __name__ == "__main__":
    main()
