#!/usr/bin/env python3
"""Usage and cost reporter for local agent sessions. Python port of usage-report.ps1.

Default mode runs as a lifecycle hook for Claude Code, Codex, and Gemini CLI:
it reads the hook JSON from stdin, parses local transcripts or telemetry,
reconciles replay-aware Codex session snapshots, aggregates token usage per
model and role, prices it as an API-equivalent estimate, writes
global/platform/session usage reports plus history, and returns a compact
report through systemMessage when the client shows it. usage_footer.py is the
visible final-response path for clients that hide hook output.
Everything comes from files already on disk: zero extra LLM tokens, zero API calls.

--refresh-prices mode runs detached in the background: it downloads current
prices from the LiteLLM community feed into .agents/usage/prices.cache.json.

Transcript facts this parser relies on are documented in usage-report.ps1.
These formats are product-owned; every read is defensive and the hook never
fails the turn - on any error it records .agents/usage/last-error.txt and exits 0.
"""

import argparse
import json
import os
import re
import sys
import traceback

sys.dont_write_bytecode = True  # keep .agents/scripts free of __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage_common as uc  # noqa: E402

SEEN_REQUEST_ID_CAP = 1500  # dedup window for usage repeated across content-block entries
SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
TIMESTAMP_PATTERN = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def _tool_entry(tool):
    return {"name": str(tool.get("name")), "kind": str(tool.get("kind")),
            "calls": uc.to_long(tool.get("calls")), "failures": uc.to_long(tool.get("failures"))}


def read_report_state(path):
    state = {
        "files": {},
        "seen": [],
        "seenTools": [],
        "session": {"models": {}, "tools": {}, "turns": 0, "samples": 0},
        "codexRows": {},
    }
    loaded = uc.read_json_file(path)
    if not isinstance(loaded, dict):
        return state
    for name, value in (loaded.get("files") or {}).items():
        state["files"][name] = uc.to_long(value)
    state["seen"] = [str(i) for i in loaded.get("seen") or []]
    state["seenTools"] = [str(i) for i in loaded.get("seenTools") or []]
    session = loaded.get("session")
    if isinstance(session, dict):
        if session.get("turns"):
            state["session"]["turns"] = uc.to_long(session["turns"])
        if session.get("samples"):
            state["session"]["samples"] = uc.to_long(session["samples"])
        for key, value in (session.get("models") or {}).items():
            bucket = uc.copy_token_bucket(value)
            if "|" not in key:
                key += "|unspecified"
            target = state["session"]["models"].setdefault(key, uc.new_token_bucket())
            uc.add_token_bucket(target, bucket)
        for key, tool in (session.get("tools") or {}).items():
            state["session"]["tools"][key] = _tool_entry(tool)
    for key, value in (loaded.get("codexRows") or {}).items():
        state["codexRows"][key] = uc.copy_token_bucket(value)
    for key, tool in (loaded.get("codexTools") or {}).items():
        state["session"]["tools"].setdefault(key, _tool_entry(tool))
    return state


def save_report_state(path, state, seen_order, seen_tool_order):
    del seen_order[:max(0, len(seen_order) - SEEN_REQUEST_ID_CAP)]
    del seen_tool_order[:max(0, len(seen_tool_order) - SEEN_REQUEST_ID_CAP)]
    uc.write_json_atomic(path, {
        "updatedUtc": uc.iso_o(uc.utcnow()),
        "files": state["files"],
        "seen": seen_order,
        "seenTools": seen_tool_order,
        "session": state["session"],
        "codexRows": state["codexRows"],
    })


def get_first_prop_value(obj, names):
    if not isinstance(obj, dict):
        return None
    for name in names:
        if obj.get(name) is not None:
            return obj[name]
    for value in obj.values():
        if value is None or isinstance(value, (str, int, float, bool)):
            continue
        for item in (value if isinstance(value, list) else [value]):
            found = get_first_prop_value(item, names)
            if found is not None:
                return found
    return None


def get_first_long(obj, names):
    return uc.to_long(get_first_prop_value(obj, names))


def get_first_string(obj, names, default="unknown"):
    value = get_first_prop_value(obj, names)
    if value is None or str(value).strip() == "":
        return default
    return str(value)


def add_tool_metric(tool_counts, name, kind="tool", status="ok"):
    if tool_counts is None or not name:
        return
    tool = tool_counts.setdefault(kind + "|" + name, {"name": name, "kind": kind, "calls": 0, "failures": 0})
    tool["calls"] += 1
    if status != "ok":
        tool["failures"] += 1


def _counts_as_user_message(entry):
    if entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = None
    if "content" in entry:
        content = entry["content"]
    elif isinstance(entry.get("message"), dict) and "content" in entry["message"]:
        content = entry["message"]["content"]
    if content is None:
        return False
    if isinstance(content, str):
        return not content.startswith("<command-")
    has_text = has_tool_result = False
    for block in content if isinstance(content, list) else [content]:
        if not isinstance(block, dict) or "type" not in block:
            continue
        has_text = has_text or block["type"] == "text"
        has_tool_result = has_tool_result or block["type"] == "tool_result"
    return has_text and not has_tool_result


def read_transcript_usage(path, state, seen, seen_order, seen_tools, seen_tool_order, scope,
                          turn_buckets, message_counts, tool_counts, intervals):
    """Incrementally reads one transcript .jsonl: only lines past the stored
    offset are considered, usage is deduplicated by requestId, and the
    interval of new activity is captured for wall-time and parallelism."""
    message_counts.setdefault("userMessages", 0)
    message_counts.setdefault("assistantMessages", 0)
    lines = uc.read_all_lines(path)
    offset = uc.to_long(state["files"].get(path, 0))
    if offset > len(lines):
        offset = 0
    min_ts = max_ts = None
    for index in range(offset, len(lines)):
        line = lines[index]
        # Cheap timestamp extraction first; full JSON parsing only for lines
        # that can actually carry usage.
        match = TIMESTAMP_PATTERN.search(line)
        if match:
            ts = uc.to_utc_date(match.group(1))
            if ts:
                min_ts = ts if not min_ts or ts < min_ts else min_ts
                max_ts = ts if not max_ts or ts > max_ts else max_ts
        if scope == "main" and '"user"' in line:
            user_entry = uc.parse_json(line)
            if isinstance(user_entry, dict) and user_entry.get("type") == "user" and _counts_as_user_message(user_entry):
                message_counts["userMessages"] += 1
        if '"assistant"' not in line or '"usage"' not in line:
            continue
        entry = uc.parse_json(line)
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
            continue
        message = entry["message"]
        if message.get("content") and isinstance(message["content"], list):
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") not in ("tool_use", "server_tool_use"):
                    continue
                tool_name = str(block["name"]) if block.get("name") else block["type"]
                tool_id = str(block["id"]) if block.get("id") else "%s|%d|%s" % (path, index, tool_name)
                if tool_id not in seen_tools:
                    seen_tools.add(tool_id)
                    seen_tool_order.append(tool_id)
                    add_tool_metric(tool_counts, tool_name, "claude")
        usage = message.get("usage")
        if not usage:
            continue
        dedup_id = str(entry["requestId"]) if entry.get("requestId") else (str(message["id"]) if message.get("id") else None)
        if dedup_id:
            if dedup_id in seen:
                continue
            seen.add(dedup_id)
            seen_order.append(dedup_id)
        model = str(message["model"]) if message.get("model") else "unknown"
        # Claude Code writes placeholder entries (model "<synthetic>", zero
        # usage) for internal events; they carry no billable tokens.
        if model == "<synthetic>":
            continue
        key = scope + "|" + model + "|unspecified"
        bucket = turn_buckets.setdefault(key, uc.new_token_bucket())
        message_counts["assistantMessages"] += 1
        uc.add_usage_to_bucket(bucket, usage)
    state["files"][path] = len(lines)
    if min_ts and max_ts:
        intervals.append({"scope": scope, "from": min_ts, "to": max_ts})


def read_gemini_telemetry_usage(usage_dir, state, turn_buckets, tool_counts, intervals, default_model):
    path = os.path.join(usage_dir, "gemini-telemetry.log")
    if not os.path.exists(path):
        return
    lines = uc.read_all_lines(path)
    offset = uc.to_long(state["files"].get(path, 0))
    if offset > len(lines):
        offset = 0
    min_ts = max_ts = None
    duration_seconds = 0.0

    for index in range(offset, len(lines)):
        entry = uc.parse_json(lines[index])
        if not isinstance(entry, dict):
            continue
        input_tokens = get_first_long(entry, ["input_token_count", "gen_ai.usage.input_tokens"])
        output_tokens = get_first_long(entry, ["output_token_count", "gen_ai.usage.output_tokens"])
        thought = get_first_long(entry, ["thoughts_token_count", "thought_token_count"])
        cache = get_first_long(entry, ["cached_content_token_count", "cache_token_count"])
        tool = get_first_long(entry, ["tool_token_count"])
        if input_tokens + output_tokens + thought + cache + tool <= 0:
            continue

        model = get_first_string(entry, ["model", "model_name", "gen_ai.response.model", "gen_ai.request.model"], default_model) or "unknown"
        bucket = turn_buckets.setdefault("main|" + model + "|unspecified", uc.new_token_bucket())
        bucket["calls"] += 1
        bucket["in"] += input_tokens + tool
        bucket["out"] += output_tokens + thought
        bucket["cacheRead"] += cache
        if tool > 0:
            add_tool_metric(tool_counts, "tool_tokens", "gemini")

        duration_ms = get_first_long(entry, ["duration_ms", "duration"])
        if duration_ms > 0:
            duration_seconds += duration_ms / 1000.0

        ts = uc.to_utc_date(get_first_prop_value(entry, ["timestamp", "time"]))
        if ts:
            min_ts = ts if not min_ts or ts < min_ts else min_ts
            max_ts = ts if not max_ts or ts > max_ts else max_ts

    state["files"][path] = len(lines)
    if min_ts and max_ts:
        intervals.append({"scope": "main", "from": min_ts, "to": max_ts})
    elif duration_seconds > 0:
        to = uc.utcnow()
        intervals.append({"scope": "main", "from": to - uc._dt.timedelta(seconds=duration_seconds), "to": to})


def resolve_platform(hook, transcript, requested):
    if requested != "auto":
        return requested
    if hook.get("hook_event_name") == "AfterAgent":
        return "gemini"
    if transcript and os.path.exists(transcript):
        entry = uc.parse_json(uc.read_first_line(transcript) or "")
        if isinstance(entry, dict):
            if entry.get("type") == "session_meta" and "payload" in entry:
                return "codex"
            if "message" in entry or entry.get("type") in ("assistant", "user"):
                return "claude"
    if "model" in hook:
        return "codex"
    return "claude"


def write_last_report(usage_dir, turn_lines, state, prices, session_id, platform_name):
    lines = [
        "# Usage report", "",
        "Platform: " + platform_name,
        "Session: " + session_id,
        "Generated: " + uc.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC",
        "", "## Last turn", "",
    ]
    lines += ["    " + line for line in turn_lines]
    lines += ["", "## Session totals", ""]
    lines.append("    " + "model [effort]".ljust(32) + "calls".ljust(7) + "input".ljust(9) + "output".ljust(9)
                 + "cacheW".ljust(9) + "cacheR".ljust(9) + "long".ljust(7) + "est$")
    for session_key in sorted(state["session"]["models"], key=uc.sort_key_text):
        parts = session_key.split("|", 1)
        model = parts[0]
        effort = parts[1] if len(parts) > 1 else "unspecified"
        bucket = state["session"]["models"][session_key]
        price = uc.get_model_price(prices["models"], model)
        cost_text = "n/a"
        if price:
            cost = uc.get_bucket_cost(price, bucket)
            if cost is not None:
                cost_text = uc.format_money(cost)
        lines.append("    " + (model + " [" + effort + "]").ljust(32) + str(bucket["calls"]).ljust(7)
                     + uc.format_tokens(bucket["in"]).ljust(9) + uc.format_tokens(bucket["out"]).ljust(9)
                     + uc.format_tokens(bucket["cache5m"] + bucket["cache1h"]).ljust(9) + uc.format_tokens(bucket["cacheRead"]).ljust(9)
                     + str(bucket["longCalls"]).ljust(7) + cost_text)
    text = "\n".join(lines) + "\n"
    uc.write_text(os.path.join(usage_dir, "last-report.md"), text)
    if platform_name:
        platform_safe = uc.to_safe_name(platform_name)
        uc.write_text(os.path.join(usage_dir, "last-report-" + platform_safe + ".md"), text)
        if session_id:
            uc.write_text(os.path.join(usage_dir, "last-report-" + platform_safe + "-" + uc.to_safe_name(session_id) + ".md"), text)


def invoke_usage_report(requested_platform):
    raw = sys.stdin.read()
    if not raw.strip():
        return
    hook = json.loads(raw)
    if not isinstance(hook, dict):
        return
    transcript = str(hook["transcript_path"]) if hook.get("transcript_path") else None
    root = str(hook["cwd"]) if hook.get("cwd") else None
    if not root or not os.path.isdir(root):
        root = os.getcwd()
    root = uc.find_project_root(root)
    platform_name = resolve_platform(hook, transcript, requested_platform)
    if platform_name in ("claude", "codex") and (not transcript or not os.path.exists(transcript)):
        return

    session_id = "unknown"
    if transcript:
        session_id = os.path.splitext(os.path.basename(transcript))[0]
    if hook.get("session_id"):
        session_id = str(hook["session_id"])

    usage_dir = uc.get_usage_dir(root)
    state_path = uc.get_usage_state_path(usage_dir, session_id)
    state = read_report_state(state_path)

    seen_order = state["seen"]
    seen = set(seen_order)
    seen_tool_order = state["seenTools"]
    seen_tools = set(seen_tool_order)

    turn_buckets = {}
    message_counts = {"userMessages": 0, "assistantMessages": 0}
    tool_counts = {}
    intervals = []
    prices = uc.get_price_table(SCRIPT_DIR, usage_dir)
    codex_warnings = []
    snapshot = None
    agent_runs = 0

    if platform_name == "claude":
        read_transcript_usage(transcript, state, seen, seen_order, seen_tools, seen_tool_order, "main",
                              turn_buckets, message_counts, tool_counts, intervals)
        # Subagent transcripts: <transcript-dir>/<session-id>/subagents/**/agent-*.jsonl
        # with agent-*.meta.json carrying agentType. Workflow-spawned agents live
        # one level deeper under subagents/workflows/.
        subagents_dir = os.path.join(os.path.dirname(transcript), session_id, "subagents")
        if os.path.isdir(subagents_dir):
            for path in uc.iter_files(subagents_dir, lambda n: n.startswith("agent-") and n.endswith(".jsonl")):
                agent_type = "subagent"
                meta = uc.read_json_file(re.sub(r"\.jsonl$", ".meta.json", path))
                if isinstance(meta, dict) and meta.get("agentType"):
                    agent_type = str(meta["agentType"])
                before = len(intervals)
                read_transcript_usage(path, state, seen, seen_order, seen_tools, seen_tool_order, agent_type,
                                      turn_buckets, message_counts, tool_counts, intervals)
                if len(intervals) > before:
                    agent_runs += 1
    elif platform_name == "codex":
        snapshot = uc.get_codex_session_usage(session_id, transcript, None, prices["models"])
        agent_runs = int(snapshot["agentRuns"])
        codex_warnings = list(snapshot["warnings"])
        if snapshot["status"] != "ok":
            return
        new_codex_rows = {}
        state["session"]["models"] = {}
        for row in snapshot["rows"]:
            key = row["scope"] + "|" + row["model"] + "|" + row["effort"]
            current = uc.copy_token_bucket(row["bucket"])
            delta = uc.subtract_token_bucket(current, state["codexRows"].get(key))
            if delta["calls"] > 0 or delta["in"] > 0 or delta["out"] > 0 or delta["cacheRead"] > 0:
                turn_buckets[key] = delta
            new_codex_rows[key] = current
            session_bucket = state["session"]["models"].setdefault(row["model"] + "|" + row["effort"], uc.new_token_bucket())
            uc.add_token_bucket(session_bucket, current)
        state["codexRows"] = new_codex_rows
        new_codex_tools = {}
        for key, current in snapshot["toolCounts"].items():
            previous = state["session"]["tools"].get(key, {})
            delta_calls = max(0, int(current["calls"]) - uc.to_long(previous.get("calls")))
            delta_failures = max(0, int(current["failures"]) - uc.to_long(previous.get("failures")))
            if delta_calls > 0 or delta_failures > 0:
                tool_counts[key] = {"name": current["name"], "kind": current["kind"], "calls": delta_calls, "failures": delta_failures}
            new_codex_tools[key] = _tool_entry(current)
        state["session"]["tools"] = new_codex_tools
        state["session"]["turns"] = int(snapshot["turns"])
        if snapshot["wallFrom"] and snapshot["wallTo"]:
            intervals.append({"scope": "main", "from": snapshot["wallFrom"], "to": snapshot["wallTo"]})
    elif platform_name == "gemini":
        default_model = str(hook["model"]) if hook.get("model") else "unknown"
        read_gemini_telemetry_usage(usage_dir, state, turn_buckets, tool_counts, intervals, default_model)
    else:
        return

    if not turn_buckets and not tool_counts:
        # Nothing new to report; still persist advanced offsets.
        save_report_state(state_path, state, seen_order, seen_tool_order)
        return

    if platform_name != "codex":
        # Fold the turn into session totals (per model and effort when known).
        for key, bucket in turn_buckets.items():
            parts = key.split("|", 2)
            effort = parts[2] if len(parts) > 2 else "unspecified"
            target = state["session"]["models"].setdefault(parts[1] + "|" + effort, uc.new_token_bucket())
            uc.add_token_bucket(target, bucket)
        state["session"]["turns"] = int(state["session"]["turns"]) + 1
        for key, tool in tool_counts.items():
            target = state["session"]["tools"].setdefault(key, {"name": tool["name"], "kind": tool["kind"], "calls": 0, "failures": 0})
            target["calls"] += int(tool["calls"])
            target["failures"] += int(tool["failures"])
    state["session"]["samples"] = int(state["session"]["samples"]) + 1

    uc.start_price_refresh_if_due(prices, usage_dir, root, SCRIPT_PATH)

    unknown_models = set()
    turn_cost = 0.0
    turn_cost_complete = True
    rows = []
    for key in sorted(turn_buckets, key=uc.sort_key_text):
        parts = key.split("|", 2)
        scope, model = parts[0], parts[1]
        effort = parts[2] if len(parts) > 2 else "unspecified"
        bucket = turn_buckets[key]
        price = uc.get_model_price(prices["models"], model)
        cost = None
        if price:
            cost = uc.get_bucket_cost(price, bucket)
            if cost is None:
                turn_cost_complete = False
            else:
                turn_cost += cost
        else:
            turn_cost_complete = False
            unknown_models.add(model)
        rows.append({"model": model, "effort": effort, "scope": scope, "bucket": bucket, "cost": cost})
    session_cost = 0.0
    session_cost_complete = True
    for session_key, bucket in state["session"]["models"].items():
        model = session_key.split("|", 1)[0]
        price = uc.get_model_price(prices["models"], model)
        if price:
            model_cost = uc.get_bucket_cost(price, bucket)
            if model_cost is None:
                session_cost_complete = False
            else:
                session_cost += model_cost
        else:
            session_cost_complete = False
            unknown_models.add(model)

    # Wall time and parallelism from new-entry intervals.
    wall_from = wall_to = None
    agent_busy = 0.0
    for interval in intervals:
        if not wall_from or interval["from"] < wall_from:
            wall_from = interval["from"]
        if not wall_to or interval["to"] > wall_to:
            wall_to = interval["to"]
        if interval["scope"] != "main":
            agent_busy += (interval["to"] - interval["from"]).total_seconds()
    wall_seconds = (wall_to - wall_from).total_seconds() if wall_from and wall_to else 0.0
    if platform_name == "codex":
        wall_seconds = 0.0

    report_warnings = [str(w) for w in prices["warnings"]] + ["! " + str(w) for w in codex_warnings]

    turn_cost_text = "$" + uc.format_money(turn_cost) if turn_cost_complete else "n/a"
    session_cost_text = "$" + uc.format_money(session_cost) if session_cost_complete else "n/a"
    if platform_name == "codex":
        header = "Usage codex: new since previous sample | est " + turn_cost_text
    else:
        header = "Usage " + platform_name + ": turn " + uc.format_duration(wall_seconds) + " | est " + turn_cost_text
    header += " | session " + session_cost_text + " (" + str(state["session"]["turns"]) + " turn(s))"
    lines = [header]
    if unknown_models:
        report_warnings.append("! no price data for: " + ", ".join(sorted(unknown_models, key=uc.sort_key_text)) + " - excluded from the estimate")
    lines += report_warnings

    if not rows:
        lines.append("  no new token usage")
    elif len(rows) > 1:
        lines.append("  " + "model [effort]".ljust(32) + " " + "agent".ljust(40) + " " + "calls".ljust(7) + "input".ljust(9)
                     + "output".ljust(9) + "cacheW".ljust(9) + "cacheR".ljust(9) + "est$")
        for row in rows:
            bucket = row["bucket"]
            cost_text = uc.format_money(row["cost"]) if row["cost"] is not None else "n/a"
            lines.append("  " + (row["model"] + " [" + row["effort"] + "]").ljust(32) + " " + row["scope"].ljust(40) + " "
                         + str(bucket["calls"]).ljust(7) + uc.format_tokens(bucket["in"]).ljust(9) + uc.format_tokens(bucket["out"]).ljust(9)
                         + uc.format_tokens(bucket["cache5m"] + bucket["cache1h"]).ljust(9) + uc.format_tokens(bucket["cacheRead"]).ljust(9) + cost_text)
    else:
        row = rows[0]
        bucket = row["bucket"]
        lines.append("  " + row["model"] + " [" + row["effort"] + "] (" + row["scope"] + "): in " + uc.format_tokens(bucket["in"])
                     + " | out " + uc.format_tokens(bucket["out"]) + " | cacheW " + uc.format_tokens(bucket["cache5m"] + bucket["cache1h"])
                     + " | cacheR " + uc.format_tokens(bucket["cacheRead"]) + " | calls " + str(bucket["calls"]))
    if platform_name == "codex" and agent_runs > 0:
        lines.append("  agents: %d descendant run(s), ancestor replay deduplicated" % agent_runs)
    elif agent_runs > 0 and wall_seconds > 0:
        parallel = (agent_busy + wall_seconds) / wall_seconds
        lines.append("  agents: %d run(s) | busy %s | parallel x%s" % (agent_runs, uc.format_duration(agent_busy), uc.fmt_fixed(parallel, 1)))
    lines.append("  prices: " + prices["sourceLabel"] + " | Standard API-equivalent token estimate, not billing")
    if platform_name == "codex":
        lines.append("  caveat: tool/container fees, cache writes, and non-Standard service tiers are absent from rollout telemetry")

    save_report_state(state_path, state, seen_order, seen_tool_order)
    write_last_report(usage_dir, lines, state, prices, session_id, platform_name)
    try:
        uc.write_v2_session_snapshot(usage_dir, root, platform_name, session_id, state, prices, rows,
                                     turn_cost, turn_cost_complete, session_cost, session_cost_complete,
                                     wall_seconds, agent_runs, message_counts, tool_counts, report_warnings)
    except Exception:  # noqa: BLE001
        pass

    print(json.dumps({"systemMessage": "\n".join(lines)}, separators=(",", ":")))
    sys.stdout.flush()
    try:
        history_source = "session"
        history_source_rows = rows
        history_revision = None
        history_wall_seconds = float(wall_seconds)
        if platform_name == "codex":
            history_source = "session-snapshot"
            history_source_rows = snapshot["rows"]
            history_revision = str(snapshot["rolloutRevision"])
            if snapshot["wallFrom"] and snapshot["wallTo"]:
                history_wall_seconds = (snapshot["wallTo"] - snapshot["wallFrom"]).total_seconds()
        uc.add_history_record(usage_dir, {
            "v": 1,
            "accountingVersion": 2 if platform_name == "codex" else 1,
            "ts": uc.iso_o(wall_to or uc.utcnow()),
            "platform": platform_name,
            "source": history_source,
            "sessionId": session_id,
            "rolloutRevision": history_revision,
            "turn": int(state["session"]["turns"]),
            "wallSeconds": history_wall_seconds,
            "agentRuns": int(agent_runs),
            "userMessages": int(message_counts["userMessages"]),
            "assistantMessages": int(message_counts["assistantMessages"]),
            "rows": [uc.to_history_row(row) for row in history_source_rows],
        })
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Usage and cost reporter hook.")
    parser.add_argument("--refresh-prices", "-RefreshPrices", dest="refresh_prices", action="store_true")
    parser.add_argument("--project-root", "-ProjectRoot", dest="project_root")
    parser.add_argument("--platform", "-Platform", dest="platform", default="auto", choices=["auto", "claude", "codex", "gemini"])
    # The hook must never fail the turn: any unexpected error is recorded in
    # .agents/usage/last-error.txt and the script exits 0 silently.
    try:
        args = parser.parse_args(argv)
        if args.refresh_prices:
            if args.project_root:
                uc.update_price_cache(uc.get_usage_dir(args.project_root))
            return 0
        invoke_usage_report(args.platform)
    except SystemExit:
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            error_dir = os.path.join(uc.find_project_root(), ".agents", "usage")
            if os.path.isdir(error_dir):
                uc.write_text(os.path.join(error_dir, "last-error.txt"),
                              uc.iso_o(uc.utcnow()) + " " + str(exc) + "\n" + traceback.format_exc() + "\n")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
