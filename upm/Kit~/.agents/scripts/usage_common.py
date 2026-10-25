"""Shared usage reporting helpers. Python port of usage-common.ps1.

Standard library only. File formats (state, history, v2 events/views, price
cache) match the PowerShell scripts, so both implementations can share one
.agents/usage/ directory.
"""

import datetime as _dt
import decimal
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
REFRESH_AFTER_DAYS = 7      # cache older than this triggers a background refresh
STALE_WARN_DAYS = 14        # cache older than this adds a staleness warning
SNAPSHOT_WARN_DAYS = 30     # snapshot-only data older than this adds a warning
ATTEMPT_COOLDOWN_HOURS = 6  # minimum spacing between refresh attempts

TOKEN_BUCKET_FIELDS = (
    "calls", "in", "out", "cacheRead", "cache5m", "cache1h",
    "longCalls", "longIn", "longOut", "longCacheRead",
)

UTC = _dt.timezone.utc
DOTNET_EPOCH_TICKS = 621355968000000000


# --- Generic helpers ---------------------------------------------------------

def utcnow():
    return _dt.datetime.now(UTC)


def iso_o(value):
    """Formats a datetime like .NET ToString("o") for UTC: 7 fractional digits and Z."""
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + ("%06d0" % value.microsecond) + "Z"


def to_utc_date(text):
    if text is None:
        return None
    if isinstance(text, _dt.datetime):
        return text.astimezone(UTC)
    value = str(text).strip()
    if not value:
        return None
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    # Older Pythons accept only 3 or 6 fractional digits; .NET writes 7.
    value = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value, count=1)
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    # Naive values are local time, like .NET RoundtripKind + ToUniversalTime.
    return parsed.astimezone(UTC)


def sort_key_text(value):
    # PowerShell Sort-Object compares strings case-insensitively.
    text = "" if value is None else str(value)
    return (text.lower(), text)


def sort_key_date(value):
    return value if value is not None else _dt.datetime.min.replace(tzinfo=UTC)


def to_long(value, default=0):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def to_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ps_bool(value):
    """[bool] cast semantics for JSON-ish values."""
    if isinstance(value, str):
        return value != ""
    return bool(value)


def fmt_fixed(value, digits):
    """.NET custom numeric format ("0.0"/"0.00"): rounds half away from zero."""
    quant = decimal.Decimal(1).scaleb(-digits)
    return str(decimal.Decimal(repr(float(value))).quantize(quant, rounding=decimal.ROUND_HALF_UP))


def ps_num(value):
    """String conversion of a number as PowerShell [string] prints it."""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return "{:.15g}".format(value)
    return str(value)


def read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return handle.read()


def read_all_lines(path):
    """Mirrors File.ReadAllLines: splits on CR, LF, or CRLF only."""
    text = read_text(path)
    if text == "":
        return []
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_first_line(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        line = handle.readline()
    return re.split(r"\r\n|\r|\n", line)[0] if line else None


def read_json_file(path):
    if not path or not os.path.exists(path):
        return None
    try:
        return json.loads(read_text(path))
    except (OSError, ValueError):
        return None


def parse_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_json_atomic(path, value):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    temp = path + "." + uuid.uuid4().hex[:8] + ".tmp"
    write_text(temp, json.dumps(value, indent=2, ensure_ascii=False))
    os.replace(temp, path)


def append_line(path, line):
    for attempt in range(1, 4):
        try:
            with open(path, "a", encoding="utf-8", newline="") as handle:
                handle.write(line + "\n")
            return
        except OSError:
            if attempt >= 3:
                return
            time.sleep(0.1)


def compact_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def prop(obj, name, default=None):
    if isinstance(obj, dict) and name in obj:
        return obj[name]
    return default


def file_ticks(path):
    """LastWriteTimeUtc.Ticks of a file, for revision hashes shared with the PowerShell scripts."""
    return os.stat(path).st_mtime_ns // 100 + DOTNET_EPOCH_TICKS


def file_mtime_utc(path):
    return _dt.datetime.fromtimestamp(os.stat(path).st_mtime, UTC)


def iter_files(root, predicate):
    """Recursive file walk in a stable order (Get-ChildItem -Recurse -File -Filter)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if predicate(name):
                yield os.path.join(dirpath, name)


def find_project_root(start=None):
    """Resolves the kit project root: the nearest ancestor of start (default: cwd)
    holding .agents/kit-manifest.json, else $CLAUDE_PROJECT_DIR when set, else start.
    A hook fired from a subdirectory still writes to the project's .agents/usage."""
    start = os.path.abspath(start or os.getcwd())
    current = start
    while True:
        if os.path.isfile(os.path.join(current, ".agents", "kit-manifest.json")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root and os.path.isdir(os.path.join(env_root, ".agents")):
        return env_root
    return start


def get_usage_dir(root):
    directory = os.path.join(root, ".agents", "usage")
    os.makedirs(directory, exist_ok=True)
    gitignore = os.path.join(directory, ".gitignore")
    if not os.path.exists(gitignore):
        # Self-ignoring runtime directory: nothing under .agents/usage/ is committed.
        write_text(gitignore, "*\n")
    return directory


def to_safe_name(value):
    if value is None or str(value).strip() == "":
        return "unknown"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(value))
    return safe[:120]


def get_usage_state_path(usage_dir, session_id):
    return os.path.join(usage_dir, "state-" + to_safe_name(session_id) + ".json")


def format_tokens(value):
    value = to_long(value)
    if value >= 1000000:
        return fmt_fixed(value / 1000000.0, 1) + "M"
    if value >= 1000:
        return fmt_fixed(value / 1000.0, 1) + "k"
    return str(value)


def format_duration(seconds):
    if seconds < 0:
        seconds = 0
    total_hours = seconds / 3600.0
    minutes = int(seconds // 60) % 60
    secs = int(seconds) % 60
    if total_hours >= 1:
        return "%dh%02dm" % (int(total_hours), minutes)
    if seconds >= 60:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % int(-(-seconds // 1))


def format_money(value):
    if 0 < value < 0.01:
        return "<0.01"
    return fmt_fixed(value, 2)


# --- Token buckets -------------------------------------------------------------

def new_token_bucket():
    return {field: 0 for field in TOKEN_BUCKET_FIELDS}


def copy_token_bucket(source):
    bucket = new_token_bucket()
    if not source:
        return bucket
    for field in TOKEN_BUCKET_FIELDS:
        if field in source:
            bucket[field] = to_long(source[field])
    return bucket


def add_token_bucket(target, source):
    if not source:
        return
    for field in TOKEN_BUCKET_FIELDS:
        value = to_long(source.get(field, 0))
        target[field] = to_long(target.get(field, 0)) + value


def subtract_token_bucket(current, previous):
    result = new_token_bucket()
    current_bucket = copy_token_bucket(current)
    previous_bucket = copy_token_bucket(previous)
    for field in TOKEN_BUCKET_FIELDS:
        delta = current_bucket[field] - previous_bucket[field]
        if delta < 0:
            delta = current_bucket[field]
        result[field] = delta
    return result


def to_history_row(row):
    bucket = row["bucket"]
    return {
        "model": row.get("model"),
        "effort": row.get("effort"),
        "scope": row.get("scope"),
        "calls": to_long(bucket.get("calls")),
        "in": to_long(bucket.get("in")),
        "out": to_long(bucket.get("out")),
        "cacheRead": to_long(bucket.get("cacheRead")),
        "cache5m": to_long(bucket.get("cache5m")),
        "cache1h": to_long(bucket.get("cache1h")),
        "longCalls": to_long(bucket.get("longCalls")),
        "longIn": to_long(bucket.get("longIn")),
        "longOut": to_long(bucket.get("longOut")),
        "longCacheRead": to_long(bucket.get("longCacheRead")),
        "estCost": row.get("cost"),
    }


def add_usage_to_bucket(bucket, usage):
    bucket["calls"] += 1
    if usage.get("input_tokens"):
        bucket["in"] += to_long(usage["input_tokens"])
    if usage.get("output_tokens"):
        bucket["out"] += to_long(usage["output_tokens"])
    if usage.get("cache_read_input_tokens"):
        bucket["cacheRead"] += to_long(usage["cache_read_input_tokens"])
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, dict) and ("ephemeral_5m_input_tokens" in breakdown or "ephemeral_1h_input_tokens" in breakdown):
        if breakdown.get("ephemeral_5m_input_tokens"):
            bucket["cache5m"] += to_long(breakdown["ephemeral_5m_input_tokens"])
        if breakdown.get("ephemeral_1h_input_tokens"):
            bucket["cache1h"] += to_long(breakdown["ephemeral_1h_input_tokens"])
    elif usage.get("cache_creation_input_tokens"):
        # No TTL breakdown available: count as 5-minute writes (the cheaper
        # rate), so the estimate errs low rather than inventing a premium.
        bucket["cache5m"] += to_long(usage["cache_creation_input_tokens"])


# --- Prices ------------------------------------------------------------------

def merge_price_entry(base, override):
    merged = {}
    if isinstance(base, dict):
        merged.update(base)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def get_price_table(script_dir, usage_dir):
    """Merges price sources into one lookup: bundled snapshot as the base, the
    refreshed LiteLLM cache on top, and time-limited overrides (promo pricing)
    above both while they last."""
    now = utcnow()
    warnings = []
    models = {}

    snapshot = read_json_file(os.path.join(script_dir, "usage-prices.json"))
    snapshot_date = None
    if isinstance(snapshot, dict) and snapshot.get("models"):
        snapshot_date = to_utc_date(snapshot.get("snapshot_date"))
        models.update(snapshot["models"])

    cache = read_json_file(os.path.join(usage_dir, "prices.cache.json"))
    cache_date = None
    if isinstance(cache, dict) and cache.get("models"):
        cache_date = to_utc_date(cache.get("fetched_at"))
        for name, value in cache["models"].items():
            models[name] = merge_price_entry(models.get(name), value)

    if isinstance(snapshot, dict) and snapshot.get("overrides"):
        for override in snapshot["overrides"]:
            until = to_utc_date(override.get("until"))
            if until and now.date() <= until.date() and override.get("prices"):
                name = str(override.get("model"))
                models[name] = merge_price_entry(models.get(name), override["prices"])

    meta = read_json_file(os.path.join(usage_dir, "prices.meta.json"))
    last_attempt = last_success = last_error = None
    if isinstance(meta, dict):
        last_attempt = to_utc_date(meta.get("lastAttemptUtc"))
        last_success = to_utc_date(meta.get("lastSuccessUtc"))
        if meta.get("lastError"):
            last_error = str(meta["lastError"])

    if cache_date:
        source_label = "litellm " + cache_date.strftime("%Y-%m-%d")
        if snapshot_date:
            source_label = "merged official snapshot " + snapshot_date.strftime("%Y-%m-%d") + " + litellm " + cache_date.strftime("%Y-%m-%d")
        age_days = (now - cache_date).total_seconds() / 86400.0
        if last_error and last_attempt and (not last_success or last_attempt > last_success):
            warnings.append("! price refresh FAILED at " + last_attempt.strftime("%Y-%m-%d %H:%M") + " UTC - using prices from " + cache_date.strftime("%Y-%m-%d") + "; estimates may be outdated")
        elif age_days > STALE_WARN_DAYS:
            warnings.append("! price data is " + str(int(age_days)) + " days old (refresh pending); estimates may be outdated")
    else:
        label = "bundled snapshot"
        if snapshot_date:
            label += " " + snapshot_date.strftime("%Y-%m-%d")
        source_label = label
        if last_error:
            warnings.append("! price refresh FAILED - using the " + label + "; estimates may be outdated")
        elif snapshot_date and (now - snapshot_date).total_seconds() / 86400.0 > SNAPSHOT_WARN_DAYS:
            warnings.append("! using the " + label + " (no successful price refresh yet); estimates may be outdated")

    return {
        "models": models,
        "sourceLabel": source_label,
        "warnings": warnings,
        "cacheDate": cache_date,
        "lastAttempt": last_attempt,
    }


def get_model_price(models, model):
    if model is None or str(model).strip() == "":
        return None
    if model in models:
        return models[model]
    # Dated snapshot ids (claude-haiku-4-5-20251001) fall back to the alias.
    stripped = re.sub(r"[-@]\d{8}$", "", model)
    if stripped != model and stripped in models:
        return models[stripped]
    # Never infer a price from an arbitrary prefix. A newer model family may
    # have materially different rates (for example gpt-5.6-sol vs gpt-5).
    return None


def get_bucket_cost(price, bucket):
    """Prices are USD per million tokens. cache_write_1h falls back to
    1.6 x cache_write_5m (the 2.0/1.25 multiplier ratio) when a source lacks
    the 1h rate."""
    short = copy_token_bucket(bucket)
    for total_field, long_field in (("calls", "longCalls"), ("in", "longIn"), ("out", "longOut"), ("cacheRead", "longCacheRead")):
        short[total_field] = max(0, short[total_field] - short[long_field])

    short_w5 = to_float(price.get("cache_write_5m"))
    short_w1 = short_w5 * 1.6
    if price.get("cache_write_1h") is not None:
        short_w1 = to_float(price["cache_write_1h"])
    short_cr = to_float(price.get("cache_read"))
    total = (short["in"] * to_float(price.get("in")) + short["out"] * to_float(price.get("out"))
             + short["cacheRead"] * short_cr + short["cache5m"] * short_w5 + short["cache1h"] * short_w1)

    if short["longCalls"] > 0:
        if "long_in" not in price or "long_out" not in price:
            return None
        long_cr = to_float(price.get("long_cache_read"))
        total += (short["longIn"] * to_float(price["long_in"]) + short["longOut"] * to_float(price["long_out"])
                  + short["longCacheRead"] * long_cr)
    return total / 1000000.0


def update_price_cache(usage_dir):
    """Background mode: fetch the LiteLLM community price feed and distill the
    first-party Claude, OpenAI, and Gemini entries into a small local cache.
    Every outcome is recorded in prices.meta.json."""
    meta_path = os.path.join(usage_dir, "prices.meta.json")
    meta = read_json_file(meta_path)
    last_success = str(meta["lastSuccessUtc"]) if isinstance(meta, dict) and meta.get("lastSuccessUtc") else None
    attempt = iso_o(utcnow())
    try:
        with urllib.request.urlopen(LITELLM_URL, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = {}
        for key, entry in data.items():
            if not re.match(r"^(claude-|gpt-|o[0-9]|chatgpt-|gemini-)", key, re.IGNORECASE):
                continue
            if not isinstance(entry, dict) or "input_cost_per_token" not in entry or "output_cost_per_token" not in entry:
                continue
            in_cost = entry["input_cost_per_token"]
            out_cost = entry["output_cost_per_token"]
            if not in_cost or not out_cost:
                continue
            in_per_m = round(float(in_cost) * 1000000, 4)
            out_per_m = round(float(out_cost) * 1000000, 4)
            if in_per_m <= 0 or out_per_m <= 0 or out_per_m < in_per_m:
                continue
            price = {"in": in_per_m, "out": out_per_m}
            if entry.get("cache_read_input_token_cost"):
                price["cache_read"] = round(float(entry["cache_read_input_token_cost"]) * 1000000, 4)
            if entry.get("cache_creation_input_token_cost"):
                price["cache_write_5m"] = round(float(entry["cache_creation_input_token_cost"]) * 1000000, 4)
            if entry.get("cache_creation_input_token_cost_above_1hr"):
                price["cache_write_1h"] = round(float(entry["cache_creation_input_token_cost_above_1hr"]) * 1000000, 4)
            models[key] = price
        if len(models) < 5:
            raise ValueError("feed sanity check failed: only %d priced models extracted" % len(models))
        write_json_atomic(os.path.join(usage_dir, "prices.cache.json"), {
            "fetched_at": iso_o(utcnow()),
            "source": "litellm",
            "models": models,
        })
        write_json_atomic(meta_path, {"lastAttemptUtc": attempt, "lastSuccessUtc": iso_o(utcnow()), "lastError": None})
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never raised
        write_json_atomic(meta_path, {"lastAttemptUtc": attempt, "lastSuccessUtc": last_success, "lastError": str(exc)})


def start_price_refresh_if_due(prices, usage_dir, root, reporter_script):
    if os.environ.get("AGENT_KIT_USAGE_NO_PRICE_REFRESH") == "1":
        return
    now = utcnow()
    if prices["cacheDate"] and (now - prices["cacheDate"]).total_seconds() / 86400.0 <= REFRESH_AFTER_DAYS:
        return
    if prices["lastAttempt"] and (now - prices["lastAttempt"]).total_seconds() / 3600.0 < ATTEMPT_COOLDOWN_HOURS:
        return
    # Stamp the attempt before launching so overlapping Stop events do not
    # spawn duplicate refreshers; the child rewrites the meta with the result.
    meta_path = os.path.join(usage_dir, "prices.meta.json")
    meta = read_json_file(meta_path)
    last_success = last_error = None
    if isinstance(meta, dict):
        last_success = str(meta["lastSuccessUtc"]) if meta.get("lastSuccessUtc") else None
        last_error = str(meta["lastError"]) if meta.get("lastError") else None
    write_json_atomic(meta_path, {"lastAttemptUtc": iso_o(now), "lastSuccessUtc": last_success, "lastError": last_error})
    if not reporter_script:
        return
    subprocess.Popen(
        [sys.executable, reporter_script, "--refresh-prices", "--project-root", root],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def add_history_record(usage_dir, record):
    if not usage_dir or not record:
        return
    os.makedirs(usage_dir, exist_ok=True)
    append_line(os.path.join(usage_dir, "history.jsonl"), compact_json(record))


# --- V2 events and views -----------------------------------------------------

def get_stable_hash(value, length=16):
    text = hashlib.sha256(("" if value is None else str(value)).encode("utf-8")).hexdigest()
    if length > 0 and len(text) > length:
        return text[:length]
    return text


def to_codex_comparable_path(path):
    if path is None or str(path).strip() == "":
        return ""
    text = str(path).strip()
    match = re.match(r"^/mnt/([A-Za-z])/(.*)$", text)
    if match:
        text = match.group(1).lower() + ":\\" + match.group(2).replace("/", "\\")
    text = text.replace("/", "\\").rstrip("\\")
    return text.lower()


def get_project_id(root):
    return "prj_" + get_stable_hash(to_codex_comparable_path(root), 16)


def get_v2_dir(usage_dir):
    if not usage_dir:
        return None
    v2 = os.path.join(usage_dir, "v2")
    for child in ("events", "state", "views", "reports"):
        os.makedirs(os.path.join(v2, child), exist_ok=True)
    return v2


def new_v2_event(project_root, event_type, platform, session_id, trace_id=None, span_id=None,
                 idempotency_key=None, source=None, payload=None, confidence="high", observed_utc=None):
    ts = to_utc_date(observed_utc) if observed_utc else None
    if not ts:
        ts = utcnow()
    if not session_id:
        session_id = "unknown"
    if not trace_id:
        trace_id = "trc_" + get_stable_hash(session_id + "|" + event_type + "|" + iso_o(ts), 24)
    if not span_id:
        span_id = "spn_" + get_stable_hash(trace_id + "|" + event_type, 24)
    if not idempotency_key:
        idempotency_key = "%s|%s|%s|%s|%s" % (platform, session_id, trace_id, span_id, event_type)
    source = source or {}
    return {
        "schemaVersion": 2,
        "eventId": "evt_" + get_stable_hash(idempotency_key, 24),
        "idempotencyKey": idempotency_key,
        "observedUtc": iso_o(ts),
        "source": {
            "platform": platform,
            "adapter": str(source["adapter"]) if "adapter" in source else "hook",
            "path": str(source["path"]) if source.get("path") is not None else None,
            "offset": source.get("offset"),
            "confidence": confidence,
        },
        "project": {
            "projectId": get_project_id(project_root),
            "rootHash": get_stable_hash(to_codex_comparable_path(project_root), 24),
        },
        "sessionId": session_id,
        "traceId": trace_id,
        "spanId": span_id,
        "type": event_type,
        "payload": payload if payload else {},
    }


def add_v2_event(usage_dir, event):
    if not usage_dir or not event:
        return
    v2 = get_v2_dir(usage_dir)
    ts = to_utc_date(event.get("observedUtc")) or utcnow()
    path = os.path.join(v2, "events", ts.strftime("%Y-%m-%d") + ".jsonl")
    append_line(path, compact_json(event))


def to_v2_row(row):
    bucket = row["bucket"]
    cost = row.get("cost")
    return {
        "model": str(row.get("model")),
        "effort": str(row["effort"]) if "effort" in row else "unspecified",
        "scope": str(row.get("scope")),
        "calls": to_long(bucket.get("calls")),
        "inputTokens": to_long(bucket.get("in")),
        "outputTokens": to_long(bucket.get("out")),
        "cacheReadTokens": to_long(bucket.get("cacheRead")),
        "cacheWriteTokens": to_long(bucket.get("cache5m")) + to_long(bucket.get("cache1h")),
        "cache5mTokens": to_long(bucket.get("cache5m")),
        "cache1hTokens": to_long(bucket.get("cache1h")),
        "longContextCalls": to_long(bucket.get("longCalls")),
        "longInputTokens": to_long(bucket.get("longIn")),
        "longOutputTokens": to_long(bucket.get("longOut")),
        "longCacheReadTokens": to_long(bucket.get("longCacheRead")),
        "estimatedCostUsd": None if cost is None else float(cost),
    }


def event_number(obj, name, default=0.0):
    value = prop(obj, name, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def get_v2_events(usage_dir):
    v2 = get_v2_dir(usage_dir)
    if not v2:
        return []
    events_dir = os.path.join(v2, "events")
    if not os.path.isdir(events_dir):
        return []
    events = []
    seen = set()
    for name in sorted(os.listdir(events_dir)):
        path = os.path.join(events_dir, name)
        if not name.endswith(".jsonl") or not os.path.isfile(path):
            continue
        for line in read_all_lines(path):
            if not line.strip():
                continue
            event = parse_json(line)
            if not isinstance(event, dict):
                continue
            if to_long(prop(event, "schemaVersion", 0)) != 2:
                continue
            key = str(prop(event, "idempotencyKey", "") or "")
            if not key.strip():
                key = str(prop(event, "eventId", uuid.uuid4().hex))
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
    return events


def _new_model_total(model, effort):
    return {
        "model": model, "effort": effort, "calls": 0,
        "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0,
        "cache5mTokens": 0, "cache1hTokens": 0, "longContextCalls": 0,
        "longInputTokens": 0, "longOutputTokens": 0, "longCacheReadTokens": 0,
        "estimatedCostUsd": 0.0, "costComplete": True,
    }


_MODEL_TOTAL_FIELDS = (
    "calls", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
    "cache5mTokens", "cache1hTokens", "longContextCalls", "longInputTokens",
    "longOutputTokens", "longCacheReadTokens",
)


def _observed_key(event):
    return sort_key_date(to_utc_date(prop(event, "observedUtc")))


def update_v2_current_session_view(usage_dir, project_root):
    events = get_v2_events(usage_dir)
    if not events:
        return {"status": "no-data", "sessions": 0}

    session_events = sorted((e for e in events if str(prop(e, "type", "")) == "session.observed"), key=_observed_key)
    if not session_events:
        return {"status": "no-session", "sessions": 0}
    latest_session_event = session_events[-1]
    session_id = str(prop(latest_session_event, "sessionId", "unknown"))
    platform = str(prop(prop(latest_session_event, "source"), "platform", "") or "")
    if not platform.strip():
        platform = "unknown"
    session_payload = prop(latest_session_event, "payload")

    def of_type(event_type):
        return [e for e in events if str(prop(e, "sessionId", "")) == session_id and str(prop(e, "type", "")) == event_type]

    trace_events = sorted(of_type("trace.ended"), key=_observed_key)
    latest_trace = trace_events[-1] if trace_events else None
    latest_trace_id = str(prop(latest_trace, "traceId", "") or "")
    usage_events = of_type("span.usage")
    agent_events = of_type("agent.ended")
    tool_events = of_type("tool.completed")

    last_rows = []
    total_by_model = {}
    warnings = []
    for warning in as_list(prop(session_payload, "warnings", [])):
        text = str(warning)
        if text.strip() and text not in warnings:
            warnings.append(text)
    unpriced_models = set()
    session_cost = 0.0
    session_cost_complete = True
    for event in usage_events:
        payload = prop(event, "payload")
        if not payload:
            continue
        model = str(prop(payload, "model", "unknown"))
        effort = str(prop(payload, "effort", "unspecified"))
        model_key = model + "|" + effort
        target = total_by_model.setdefault(model_key, _new_model_total(model, effort))
        for field in _MODEL_TOTAL_FIELDS:
            target[field] += int(event_number(payload, field, 0.0))
        cost = prop(payload, "estimatedCostUsd")
        if cost is None:
            target["costComplete"] = False
            session_cost_complete = False
            unpriced_models.add(model)
        else:
            target["estimatedCostUsd"] += float(cost)
            session_cost += float(cost)
        if latest_trace_id and str(prop(event, "traceId", "")) == latest_trace_id:
            last_rows.append(payload)

    total_rows = []
    for model_key in sorted(total_by_model, key=sort_key_text):
        row = total_by_model[model_key]
        out = {key: value for key, value in row.items() if key != "costComplete"}
        out["estimatedCostUsd"] = float(row["estimatedCostUsd"]) if row["costComplete"] else None
        total_rows.append(out)
    snapshot_models = as_list(prop(session_payload, "models", []))
    if snapshot_models:
        total_rows = list(snapshot_models)
        snapshot_cost = prop(session_payload, "estimatedCostUsd")
        if snapshot_cost is not None:
            session_cost = float(snapshot_cost)
        session_cost_complete = ps_bool(prop(session_payload, "costComplete", session_cost_complete))

    agents_by_role = {}
    for event in agent_events:
        payload = prop(event, "payload")
        role = str(prop(payload, "role", "") or "")
        if not role.strip():
            continue
        agent = agents_by_role.setdefault(role, {
            "role": role, "runs": 0, "tokensIn": 0, "tokensOut": 0,
            "estCost": 0.0, "costComplete": True, "lastUsedUtc": None,
        })
        agent["runs"] += int(event_number(payload, "runs", 1.0))
        agent["tokensIn"] += int(event_number(payload, "tokensIn", 0.0))
        agent["tokensOut"] += int(event_number(payload, "tokensOut", 0.0))
        cost = prop(payload, "estCost")
        if cost is None:
            agent["costComplete"] = False
        else:
            agent["estCost"] += float(cost)
        observed = str(prop(event, "observedUtc", "") or "")
        if observed:
            agent["lastUsedUtc"] = observed
    agent_rows = [agents_by_role[role] for role in sorted(agents_by_role, key=sort_key_text)]

    tools_by_key = {}
    for event in tool_events:
        payload = prop(event, "payload")
        name = str(prop(payload, "name", "") or "")
        if not name.strip():
            continue
        kind = str(prop(payload, "kind", "tool"))
        tool = tools_by_key.setdefault(kind + "|" + name, {"name": name, "kind": kind, "calls": 0, "failures": 0, "lastUsedUtc": None})
        tool["calls"] += int(event_number(payload, "calls", 1.0))
        tool["failures"] += int(event_number(payload, "failures", 0.0))
        observed = str(prop(event, "observedUtc", "") or "")
        if observed:
            tool["lastUsedUtc"] = observed
    tool_rows = [tools_by_key[key] for key in sorted(tools_by_key, key=sort_key_text)]
    if isinstance(session_payload, dict) and "tools" in session_payload:
        tool_rows = as_list(prop(session_payload, "tools", []))

    trace_payload = prop(latest_trace, "payload")
    turn_cost = 0.0
    turn_cost_complete = True
    for row in last_rows:
        cost = prop(row, "estimatedCostUsd")
        if cost is not None:
            turn_cost += float(cost)
        else:
            turn_cost_complete = False
    if latest_trace:
        turn_cost_complete = ps_bool(prop(trace_payload, "costComplete", turn_cost_complete))
    turns = int(event_number(session_payload, "turns", float(len(trace_events))))
    aliases = [str(alias) for alias in as_list(prop(session_payload, "aliases", [])) if str(alias).strip()]
    if unpriced_models:
        unpriced_warning = "! no price data for: " + ", ".join(sorted(unpriced_models, key=sort_key_text)) + " - excluded from the estimate"
        if unpriced_warning not in warnings:
            warnings.append(unpriced_warning)

    generated = iso_o(utcnow())
    view = {
        "v": 2,
        "generatedUtc": generated,
        "projectId": get_project_id(project_root),
        "platform": platform,
        "sessionId": session_id,
        "aliases": aliases,
        "sourceConfidence": str(prop(session_payload, "sourceConfidence", "high")),
        "status": "ok",
        "priceSource": str(prop(session_payload, "priceSource", "unknown")),
        "warnings": warnings,
        "lastTurn": {
            "traceId": latest_trace_id,
            "turn": int(event_number(trace_payload, "turn", float(turns))),
            "durationSeconds": event_number(trace_payload, "durationSeconds", 0.0),
            "estimatedCostUsd": turn_cost if turn_cost_complete else None,
            "costComplete": turn_cost_complete,
            "agentRuns": int(event_number(trace_payload, "agentRuns", 0.0)),
            "userMessages": int(event_number(trace_payload, "userMessages", 0.0)),
            "assistantMessages": int(event_number(trace_payload, "assistantMessages", 0.0)),
            "rows": last_rows,
        },
        "totals": {
            "turns": turns,
            "estimatedCostUsd": session_cost,
            "costComplete": session_cost_complete,
            "models": total_rows,
            "agents": agent_rows,
            "tools": tool_rows,
        },
    }
    _write_views(usage_dir, view, session_id, agent_rows, tool_rows, generated)
    return {"status": "ok", "sessions": len(session_events), "events": len(events)}


def _write_views(usage_dir, view, session_id, agent_rows, tool_rows, generated):
    views = os.path.join(get_v2_dir(usage_dir), "views")
    write_json_atomic(os.path.join(views, "current-session.json"), view)
    write_json_atomic(os.path.join(views, "agent-summary.json"), {"v": 2, "generatedUtc": generated, "sessionId": session_id, "agents": agent_rows})
    write_json_atomic(os.path.join(views, "tool-summary.json"), {"v": 2, "generatedUtc": generated, "sessionId": session_id, "tools": tool_rows})


def import_v1_history_to_v2(usage_dir, project_root):
    history_path = os.path.join(usage_dir, "history.jsonl")
    if not os.path.exists(history_path):
        return {"status": "no-history", "imported": 0}

    v2 = get_v2_dir(usage_dir)
    state_path = os.path.join(v2, "state", "migrations.json")
    imported = {}
    state = read_json_file(state_path)
    if isinstance(state, dict) and isinstance(state.get("importedHistory"), dict):
        imported = {name: str(value) for name, value in state["importedHistory"].items()}

    count = 0
    for line_number, line in enumerate(read_all_lines(history_path), start=1):
        if not line.strip():
            continue
        digest = get_stable_hash(line, 32)
        if digest in imported:
            continue
        record = parse_json(line)
        if not isinstance(record, dict) or to_long(record.get("v"), -1) != 1:
            continue
        ts = to_utc_date(record.get("ts")) or utcnow()
        platform = str(record.get("platform", "unknown"))
        source_session_id = str(record.get("sessionId", "unknown"))
        session_id = "hist_" + get_stable_hash(platform + "|" + source_session_id, 24)
        trace_id = "trc_" + get_stable_hash("v1-history|" + digest, 24)
        event = new_v2_event(
            project_root, "migration.imported", platform, session_id, trace_id,
            "spn_" + get_stable_hash(trace_id + "|migration", 24), "v1-history|" + digest,
            {"adapter": "v1-history", "path": history_path, "offset": line_number},
            {"sourceSessionId": source_session_id, "sourceTurn": record.get("turn"), "platform": platform, "record": record},
            observed_utc=ts,
        )
        add_v2_event(usage_dir, event)
        imported[digest] = iso_o(utcnow())
        count += 1

    write_json_atomic(state_path, {"updatedUtc": iso_o(utcnow()), "importedHistory": imported})
    return {"status": "ok", "imported": count}


def write_v2_session_snapshot(usage_dir, project_root, platform_name, session_id, state, prices, rows,
                              turn_cost, turn_cost_complete, session_cost, session_cost_complete,
                              wall_seconds, agent_runs, message_counts, tool_counts, warnings=(), alias_session_ids=()):
    if not usage_dir or not session_id or not state:
        return
    get_v2_dir(usage_dir)

    observed = utcnow()
    observed_text = iso_o(observed)
    project_id = get_project_id(project_root)
    turn_ordinal = int(state["session"]["turns"])
    sample_ordinal = int(state["session"].get("samples", turn_ordinal))
    trace_id = "trc_" + get_stable_hash("%s|%s|%s|sample|%d" % (project_id, platform_name, session_id, sample_ordinal), 24)
    source = {"adapter": "hook", "path": None, "offset": None}
    safe_aliases = [str(alias) for alias in alias_session_ids if alias and alias != session_id]
    key_prefix = "usage-report|%s|%s|sample|%d|" % (platform_name, session_id, sample_ordinal)

    turn_rows = [to_v2_row(row) for row in rows]

    agent_rows_by_role = {}
    for row in turn_rows:
        scope = row["scope"]
        if scope == "main" or not scope.strip():
            continue
        agent = agent_rows_by_role.setdefault(scope, {
            "role": scope, "runs": 0, "tokensIn": 0, "tokensOut": 0,
            "estCost": 0.0, "costComplete": True, "lastUsedUtc": observed_text,
        })
        agent["runs"] += 1
        agent["tokensIn"] += row["inputTokens"]
        agent["tokensOut"] += row["outputTokens"]
        if row["estimatedCostUsd"] is None:
            agent["costComplete"] = False
        else:
            agent["estCost"] += row["estimatedCostUsd"]
    agent_rows = [agent_rows_by_role[role] for role in sorted(agent_rows_by_role, key=sort_key_text)]

    def tool_row(tool):
        return {"name": str(tool["name"]), "kind": str(tool["kind"]), "calls": int(tool["calls"]),
                "failures": int(tool["failures"]), "lastUsedUtc": observed_text}

    tool_rows = [tool_row(tool_counts[key]) for key in sorted(tool_counts or {}, key=sort_key_text)]
    total_tool_rows = list(tool_rows)
    if "tools" in state["session"]:
        session_tools = state["session"]["tools"]
        total_tool_rows = [tool_row(session_tools[key]) for key in sorted(session_tools, key=sort_key_text)]

    total_rows = []
    for session_key in sorted(state["session"]["models"], key=sort_key_text):
        parts = session_key.split("|", 1)
        model = parts[0]
        effort = parts[1] if len(parts) > 1 else "unspecified"
        bucket = state["session"]["models"][session_key]
        price = get_model_price(prices["models"], model)
        cost = get_bucket_cost(price, bucket) if price else None
        total_rows.append(to_v2_row({"model": model, "effort": effort, "scope": "", "bucket": bucket, "cost": cost}))
        del total_rows[-1]["scope"]

    view = {
        "v": 2,
        "generatedUtc": observed_text,
        "projectId": project_id,
        "platform": platform_name,
        "sessionId": session_id,
        "aliases": safe_aliases,
        "sourceConfidence": "high",
        "status": "ok",
        "priceSource": prices["sourceLabel"],
        "warnings": list(warnings),
        "lastTurn": {
            "traceId": trace_id,
            "turn": turn_ordinal,
            "durationSeconds": float(wall_seconds),
            "estimatedCostUsd": float(turn_cost) if turn_cost_complete else None,
            "costComplete": bool(turn_cost_complete),
            "agentRuns": int(agent_runs),
            "userMessages": int(message_counts.get("userMessages", 0)),
            "assistantMessages": int(message_counts.get("assistantMessages", 0)),
            "rows": turn_rows,
        },
        "totals": {
            "turns": int(state["session"]["turns"]),
            "estimatedCostUsd": float(session_cost),
            "costComplete": bool(session_cost_complete),
            "models": total_rows,
            "agents": agent_rows,
            "tools": total_tool_rows,
        },
    }
    _write_views(usage_dir, view, session_id, agent_rows, total_tool_rows, observed_text)

    def emit(event_type, span_suffix, key_suffix, payload):
        event = new_v2_event(project_root, event_type, platform_name, session_id, trace_id,
                             "spn_" + get_stable_hash(trace_id + "|" + span_suffix, 24),
                             key_prefix + key_suffix, source, payload, observed_utc=observed)
        add_v2_event(usage_dir, event)

    emit("session.observed", "session", "session", {
        "aliases": safe_aliases,
        "turns": int(state["session"]["turns"]),
        "samples": sample_ordinal,
        "sourceConfidence": "high",
        "priceSource": prices["sourceLabel"],
        "warnings": list(warnings),
        "models": total_rows,
        "tools": total_tool_rows,
        "estimatedCostUsd": float(session_cost),
        "costComplete": bool(session_cost_complete),
    })
    emit("trace.ended", "trace", "trace", {
        "turn": turn_ordinal,
        "sample": sample_ordinal,
        "costComplete": bool(turn_cost_complete),
        "durationSeconds": float(wall_seconds),
        "agentRuns": int(agent_runs),
        "userMessages": int(message_counts.get("userMessages", 0)),
        "assistantMessages": int(message_counts.get("assistantMessages", 0)),
    })
    for row in turn_rows:
        identity = "%s|%s|%s" % (row["scope"], row["model"], row["effort"])
        emit("span.usage", identity, "usage|" + identity, row)
    for agent in agent_rows:
        emit("agent.ended", "agent|" + agent["role"], "agent|" + agent["role"], agent)
    for tool in tool_rows:
        identity = tool["kind"] + "|" + tool["name"]
        emit("tool.completed", "tool|" + identity, "tool|" + identity, tool)
    try:
        update_v2_current_session_view(usage_dir, project_root)
    except Exception:  # noqa: BLE001
        pass


# --- Codex rollouts ------------------------------------------------------------

def codex_token_delta(total_usage, previous):
    if not total_usage:
        return None
    for field in ("input_tokens", "output_tokens"):
        if field not in total_usage:
            return None
    delta_in = to_long(total_usage["input_tokens"]) - to_long(previous.get("in", 0))
    delta_out = to_long(total_usage["output_tokens"]) - to_long(previous.get("out", 0))
    delta_cache = to_long(total_usage.get("cached_input_tokens") or 0) - to_long(previous.get("cacheRead", 0))
    if delta_in < 0 or delta_out < 0 or delta_cache < 0:
        return None
    if delta_in == 0 and delta_out == 0 and delta_cache == 0:
        return {"changed": False}
    return {"changed": True, "rawInput": delta_in, "out": delta_out, "cacheRead": delta_cache}


def _wsl_distros():
    distros = []
    try:
        output = subprocess.run(["wsl.exe", "-l", "-q"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
        for line in output.decode("utf-16-le", errors="ignore").splitlines():
            name = line.replace("\0", "").strip()
            if name and name not in distros:
                distros.append(name)
    except OSError:
        pass
    for fallback in ("Ubuntu", "Ubuntu-24.04", "Ubuntu-22.04", "Debian"):
        if fallback not in distros:
            distros.append(fallback)
    return distros


def get_codex_session_roots():
    roots = []
    candidates = [os.environ.get("CODEX_HOME"), os.path.join(os.path.expanduser("~"), ".codex")]
    if os.environ.get("USERPROFILE"):
        candidates.append(os.path.join(os.environ["USERPROFILE"], ".codex"))
    for candidate in candidates:
        if not candidate:
            continue
        sessions = os.path.join(candidate, "sessions")
        if os.path.isdir(sessions) and sessions not in roots:
            roots.append(sessions)
    if os.environ.get("CODEX_HOME") and roots:
        return roots
    if os.name == "nt":
        for distro in _wsl_distros():
            root_sessions = "\\\\wsl.localhost\\" + distro + "\\root\\.codex\\sessions"
            if os.path.isdir(root_sessions) and root_sessions not in roots:
                roots.append(root_sessions)
            home_root = "\\\\wsl.localhost\\" + distro + "\\home"
            if not os.path.isdir(home_root):
                continue
            for home_dir in sorted(os.listdir(home_root)):
                sessions = os.path.join(home_root, home_dir, ".codex", "sessions")
                if os.path.isdir(sessions) and sessions not in roots:
                    roots.append(sessions)
    return roots


def get_codex_rollout_metadata(path):
    if not path or not os.path.exists(path):
        return None
    try:
        first_line = read_first_line(path)
        if not first_line or not first_line.strip():
            return None
        first = parse_json(first_line)
        if not isinstance(first, dict) or first.get("type") != "session_meta":
            return None
        payload = first.get("payload")
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        rollout_id = str(payload["id"])
        root_session_id = str(payload["session_id"]) if payload.get("session_id") else rollout_id

        parent_id = ""
        depth = 0
        scope = "main"
        source = payload.get("source")
        if isinstance(source, dict) and source.get("subagent"):
            subagent = source["subagent"]
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            if spawn:
                if spawn.get("parent_thread_id"):
                    parent_id = str(spawn["parent_thread_id"])
                if spawn.get("depth") is not None:
                    depth = to_long(spawn["depth"])
                if spawn.get("agent_path"):
                    scope = str(spawn["agent_path"])
                    scope = scope[6:] if scope.startswith("/root/") else scope.lstrip("/")
                elif spawn.get("agent_role"):
                    scope = str(spawn["agent_role"])
                else:
                    scope = "subagent"

        return {
            "path": path,
            "id": rollout_id,
            "sessionId": root_session_id,
            "parentId": parent_id,
            "depth": depth,
            "scope": scope,
            "startedUtc": to_utc_date(first.get("timestamp")),
            "cwd": str(payload.get("cwd", "") or ""),
        }
    except Exception:  # noqa: BLE001
        return None


def _is_rollout(name):
    return name.startswith("rollout-") and name.endswith(".jsonl")


def find_codex_session_rollouts(session_id, main_transcript=None, rollout_paths=None):
    if not session_id:
        return []
    candidates = []
    seen_paths = set()

    def add(path):
        key = os.path.normcase(path)
        if key in seen_paths:
            return False
        seen_paths.add(key)
        candidates.append(path)
        return True

    explicit_count = 0
    for path in rollout_paths or []:
        if not path:
            continue
        explicit_count += 1
        if os.path.exists(path):
            add(path)

    main_path = None
    if main_transcript and os.path.exists(main_transcript):
        meta = get_codex_rollout_metadata(main_transcript)
        if meta and meta["sessionId"] == session_id:
            main_path = main_transcript
            add(main_transcript)

    session_roots = get_codex_session_roots()
    if not main_path:
        for root in session_roots:
            for path in iter_files(root, _is_rollout):
                if not os.path.splitext(os.path.basename(path))[0].lower().endswith(session_id.lower()):
                    continue
                meta = get_codex_rollout_metadata(path)
                if meta and meta["id"] == session_id and meta["sessionId"] == session_id:
                    main_path = path
                    add(path)
                    break
            if main_path:
                break

    if main_path and explicit_count == 0:
        main_meta = get_codex_rollout_metadata(main_path)
        cutoff = None
        if main_meta and main_meta["startedUtc"]:
            cutoff = main_meta["startedUtc"] - _dt.timedelta(minutes=5)
        derived_root = os.path.dirname(main_path)
        for _ in range(3):
            if not derived_root:
                break
            derived_root = os.path.dirname(derived_root)
        if derived_root and os.path.isdir(derived_root) and derived_root not in session_roots:
            session_roots.append(derived_root)
        for root in session_roots:
            for path in iter_files(root, _is_rollout):
                if cutoff and file_mtime_utc(path) < cutoff:
                    continue
                add(path)

    result = []
    for path in candidates:
        meta = get_codex_rollout_metadata(path)
        if meta and meta["sessionId"] == session_id:
            result.append(meta)
    return sorted(result, key=lambda m: (m["depth"], sort_key_date(m["startedUtc"]), sort_key_text(m["id"])))


_CODEX_MARKERS = ('"turn_context"', '"task_started"', '"token_count"', '"custom_tool_call"',
                  '"function_call"', '"exec_command_end"', '"patch_apply_end"', '"tool_call_end"')


def read_codex_rollout_usage(metadata):
    turn_ids = []
    seen_turns = set()
    events = []
    response_tools = []
    legacy_tools = []
    warnings = []
    active_turn_id = ""
    current_model = "unknown"
    current_effort = "unspecified"

    try:
        lines = read_all_lines(metadata["path"])
        for line_number, line in enumerate(lines, start=1):
            if not any(marker in line for marker in _CODEX_MARKERS):
                continue
            entry = parse_json(line)
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")

            if entry.get("type") == "response_item" and isinstance(payload, dict):
                response_type = str(payload.get("type", ""))
                if response_type in ("custom_tool_call", "function_call"):
                    tool_turn_id = active_turn_id
                    passthrough = payload.get("internal_chat_message_metadata_passthrough")
                    if isinstance(passthrough, dict) and passthrough.get("turn_id"):
                        tool_turn_id = str(passthrough["turn_id"])
                    tool_name = str(payload["name"]) if payload.get("name") else response_type
                    response_tools.append({"turnId": tool_turn_id, "name": tool_name, "kind": "codex", "failures": 0})
                continue

            if entry.get("type") == "turn_context" and isinstance(payload, dict):
                if payload.get("turn_id"):
                    active_turn_id = str(payload["turn_id"])
                if payload.get("model"):
                    current_model = str(payload["model"])
                if payload.get("effort"):
                    current_effort = str(payload["effort"])
                if active_turn_id and active_turn_id not in seen_turns:
                    seen_turns.add(active_turn_id)
                    turn_ids.append(active_turn_id)
                continue

            if entry.get("type") != "event_msg" or not isinstance(payload, dict) or "type" not in payload:
                continue
            payload_type = str(payload["type"])
            if payload_type in ("exec_command_end", "patch_apply_end", "tool_call_end"):
                tool_name = {"exec_command_end": "shell", "patch_apply_end": "apply_patch"}.get(payload_type, "tool")
                if payload_type == "tool_call_end" and payload.get("name"):
                    tool_name = str(payload["name"])
                legacy_tools.append({"turnId": active_turn_id, "name": tool_name, "kind": "codex", "failures": 0})
                continue
            if payload_type == "task_started":
                if payload.get("turn_id"):
                    active_turn_id = str(payload["turn_id"])
                    if active_turn_id not in seen_turns:
                        seen_turns.add(active_turn_id)
                        turn_ids.append(active_turn_id)
                continue
            info = payload.get("info")
            if payload_type != "token_count" or not isinstance(info, dict) or not info.get("total_token_usage"):
                continue
            total = info["total_token_usage"]
            if "input_tokens" not in total or "output_tokens" not in total:
                warnings.append("token_count without input/output totals at line %d" % line_number)
                continue
            last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
            events.append({
                "turnId": active_turn_id,
                "model": current_model,
                "effort": current_effort,
                "timestampUtc": to_utc_date(entry.get("timestamp")),
                "totalIn": to_long(total["input_tokens"]),
                "totalOut": to_long(total["output_tokens"]),
                "totalCache": to_long(total.get("cached_input_tokens") or 0),
                "lastIn": to_long(last["input_tokens"]) if last and "input_tokens" in last else -1,
                "lastOut": to_long(last["output_tokens"]) if last and "output_tokens" in last else -1,
                "lastCache": to_long(last.get("cached_input_tokens") or 0) if last else 0,
            })
    except Exception as exc:  # noqa: BLE001
        warnings.append("could not read rollout: " + str(exc))

    tools = []
    if response_tools:
        modern_names = set()
        for tool in response_tools:
            tools.append(tool)
            modern_names.add(tool["name"].lower())
        if "exec" in modern_names:
            modern_names.add("shell")
        for tool in legacy_tools:
            if tool["name"] == "tool" or tool["name"].lower() in modern_names:
                continue
            tools.append(tool)
    else:
        tools.extend(legacy_tools)
    return {"turnIds": turn_ids, "events": events, "tools": tools, "warnings": warnings}


def get_codex_session_usage(session_id, main_transcript=None, rollout_paths=None, price_models=None):
    result = {
        "status": "no-data", "rows": [], "warnings": [], "rollouts": 0, "agentRuns": 0, "turns": 0,
        "wallFrom": None, "wallTo": None, "toolCounts": {}, "rolloutRevision": "",
    }
    metadata = find_codex_session_rollouts(session_id, main_transcript, rollout_paths)
    if not metadata:
        return result

    valid_metadata = []
    valid_ids = set()
    for meta in metadata:
        if meta["parentId"] and meta["parentId"] not in valid_ids:
            result["warnings"].append("missing parent rollout %s for %s; descendant excluded" % (meta["parentId"], meta["id"]))
            continue
        valid_metadata.append(meta)
        valid_ids.add(meta["id"])
    if not valid_metadata:
        result["status"] = "partial"
        return result
    revision_parts = []
    for meta in sorted(valid_metadata, key=lambda m: sort_key_text(m["id"])):
        try:
            revision_parts.append("%s:%d:%d" % (meta["id"], os.path.getsize(meta["path"]), file_ticks(meta["path"])))
        except OSError:
            continue
    if revision_parts:
        result["rolloutRevision"] = get_stable_hash("|".join(revision_parts), 32)

    data_by_id = {}
    owner_by_turn = {}
    for meta in valid_metadata:
        data = read_codex_rollout_usage(meta)
        data_by_id[meta["id"]] = data
        for warning in data["warnings"]:
            result["warnings"].append(meta["id"] + ": " + warning)
        for turn_id in data["turnIds"]:
            owner_by_turn.setdefault(turn_id, meta["id"])

    row_buckets = {}
    files_with_owned_usage = set()
    root_turns_with_usage = set()
    for meta in valid_metadata:
        data = data_by_id[meta["id"]]
        for tool in data["tools"]:
            if not tool["turnId"] or owner_by_turn.get(tool["turnId"]) != meta["id"]:
                continue
            tool_key = tool["kind"] + "|" + tool["name"]
            counts = result["toolCounts"].setdefault(tool_key, {"name": tool["name"], "kind": tool["kind"], "calls": 0, "failures": 0})
            counts["calls"] += 1
            counts["failures"] += int(tool["failures"])
        previous = {"in": 0, "out": 0, "cacheRead": 0}
        unattributed_warned = False
        for event in data["events"]:
            total_usage = {"input_tokens": event["totalIn"], "output_tokens": event["totalOut"], "cached_input_tokens": event["totalCache"]}
            delta = codex_token_delta(total_usage, previous)
            previous = {"in": event["totalIn"], "out": event["totalOut"], "cacheRead": event["totalCache"]}
            if not delta:
                result["warnings"].append(meta["id"] + ": cumulative token counter decreased; event excluded")
                continue
            if not delta["changed"]:
                continue
            if not event["turnId"] or event["turnId"] not in owner_by_turn:
                # Partial-fork child rollouts can replay cumulative ancestor
                # counters before their first task_started/turn_context entry.
                # They advance the baseline but never belong to the child.
                if meta["id"] == session_id and not unattributed_warned:
                    result["warnings"].append(meta["id"] + ": token event without a turn id; event excluded")
                    unattributed_warned = True
                continue
            if owner_by_turn[event["turnId"]] != meta["id"]:
                continue

            raw_input = delta["rawInput"]
            output = delta["out"]
            cache_read = delta["cacheRead"]
            if event["lastIn"] >= 0 and (event["lastIn"] != raw_input or event["lastOut"] != output or event["lastCache"] != cache_read):
                result["warnings"].append(meta["id"] + ": last_token_usage disagrees with cumulative delta; event excluded")
                continue
            uncached_input = max(0, raw_input - cache_read)
            model = event["model"] or "unknown"
            effort = event["effort"] or "unspecified"
            key = meta["scope"] + "|" + model + "|" + effort
            row = row_buckets.setdefault(key, {"scope": meta["scope"], "model": model, "effort": effort, "bucket": new_token_bucket()})
            bucket = row["bucket"]
            bucket["calls"] += 1
            bucket["in"] += uncached_input
            bucket["out"] += output
            bucket["cacheRead"] += cache_read

            price = get_model_price(price_models, model) if price_models else None
            is_long = False
            if price and price.get("long_context_threshold") is not None:
                is_long = raw_input > to_long(price["long_context_threshold"])
            if is_long:
                bucket["longCalls"] += 1
                bucket["longIn"] += uncached_input
                bucket["longOut"] += output
                bucket["longCacheRead"] += cache_read

            files_with_owned_usage.add(meta["id"])
            if meta["id"] == session_id:
                root_turns_with_usage.add(event["turnId"])
            ts = event["timestampUtc"]
            if ts:
                if not result["wallFrom"] or ts < result["wallFrom"]:
                    result["wallFrom"] = ts
                if not result["wallTo"] or ts > result["wallTo"]:
                    result["wallTo"] = ts

    rows = []
    for key in sorted(row_buckets, key=sort_key_text):
        row = row_buckets[key]
        price = get_model_price(price_models, row["model"]) if price_models else None
        cost = get_bucket_cost(price, row["bucket"]) if price else None
        rows.append({"scope": row["scope"], "model": row["model"], "effort": row["effort"], "bucket": row["bucket"], "cost": cost})
    for meta in valid_metadata:
        if meta["id"] != session_id and meta["id"] in files_with_owned_usage:
            result["agentRuns"] += 1
    result["rows"] = rows
    result["rollouts"] = len(valid_metadata)
    result["turns"] = len(root_turns_with_usage)
    if rows:
        result["status"] = "partial" if result["warnings"] else "ok"
    elif result["warnings"]:
        result["status"] = "partial"
    return result


def get_codex_rollout_records(project_root, usage_dir, script_dir, retention_days=90):
    result = {"status": "no-data", "records": 0, "warnings": []}
    if not project_root or not usage_dir:
        return result
    session_roots = get_codex_session_roots()
    if not session_roots:
        return result

    recorded_revisions = set()
    history_path = os.path.join(usage_dir, "history.jsonl")
    if os.path.exists(history_path):
        for line in read_all_lines(history_path):
            if not line.strip():
                continue
            record = parse_json(line)
            if not isinstance(record, dict) or record.get("platform") != "codex" or not record.get("sessionId"):
                continue
            if record.get("source") not in ("rollout-scan", "session-snapshot"):
                continue
            if str(record.get("rolloutRevision") or "").strip():
                recorded_revisions.add(str(record["sessionId"]) + "|" + str(record["rolloutRevision"]))

    root_key = to_codex_comparable_path(project_root)
    now = utcnow()
    cutoff = now - _dt.timedelta(days=max(1, retention_days))
    complete_cutoff = now - _dt.timedelta(hours=2)
    groups = {}
    for sessions_root in session_roots:
        for path in iter_files(sessions_root, _is_rollout):
            last_write = file_mtime_utc(path)
            if last_write < cutoff:
                continue
            meta = get_codex_rollout_metadata(path)
            if not meta or to_codex_comparable_path(meta["cwd"]) != root_key:
                continue
            groups.setdefault(meta["sessionId"], []).append({
                "meta": meta, "lastWriteUtc": last_write, "length": os.path.getsize(path), "ticks": file_ticks(path),
            })

    prices = get_price_table(script_dir, usage_dir)
    for session_id in sorted(groups, key=sort_key_text):
        items = groups[session_id]
        root_items = [i for i in items if i["meta"]["id"] == session_id and not i["meta"]["parentId"]]
        if not root_items:
            result["warnings"].append("codex session " + session_id + " has descendants but no root rollout; excluded")
            continue
        root_item = root_items[0]
        latest_write = max(i["lastWriteUtc"] for i in items)
        if latest_write >= complete_cutoff:
            continue
        revision_parts = ["%s:%d:%d" % (i["meta"]["id"], i["length"], i["ticks"])
                          for i in sorted(items, key=lambda i: sort_key_text(i["meta"]["id"]))]
        rollout_revision = get_stable_hash("|".join(revision_parts), 32)
        if session_id + "|" + rollout_revision in recorded_revisions:
            continue
        paths = [i["meta"]["path"] for i in items]
        snapshot = get_codex_session_usage(session_id, root_item["meta"]["path"], paths, prices["models"])
        for warning in snapshot["warnings"]:
            result["warnings"].append(session_id + ": " + warning)
        if snapshot["status"] != "ok" or not snapshot["rows"]:
            if snapshot["status"] != "ok":
                result["warnings"].append(session_id + ": lineage snapshot is " + snapshot["status"] + "; checkpoint not written")
            continue

        record_ts = snapshot["wallTo"] or root_item["lastWriteUtc"]
        add_history_record(usage_dir, {
            "v": 1,
            "accountingVersion": 2,
            "ts": iso_o(record_ts),
            "platform": "codex",
            "source": "rollout-scan",
            "sessionId": session_id,
            "rolloutRevision": rollout_revision,
            "turn": int(snapshot["turns"]),
            "wallSeconds": (snapshot["wallTo"] - snapshot["wallFrom"]).total_seconds() if snapshot["wallFrom"] and snapshot["wallTo"] else 0.0,
            "agentRuns": int(snapshot["agentRuns"]),
            "userMessages": 0,
            "assistantMessages": 0,
            "rows": [to_history_row(row) for row in snapshot["rows"]],
        })
        recorded_revisions.add(session_id + "|" + rollout_revision)
        result["records"] += 1

    if groups:
        result["status"] = "ok"
    return result
