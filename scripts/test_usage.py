#!/usr/bin/env python3
"""Regression tests for usage identity, pricing, and Codex lineage accounting.

Python port of test-usage.ps1 exercising the Python usage scripts. Standard
library only; prints "usage regression tests: PASS" and exits 0 on success.
"""

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True  # keep scripts/ free of __pycache__
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import usage_common as uc  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json_line(path, value):
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")


def run_script(name, args, input_text=None):
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, name)] + list(args),
                          input=input_text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, env=env, check=False)
    return proc.stdout.strip()


def session_meta(rollout_id, root_id, cwd, parent_id="", depth=0, agent_path=""):
    source = "test"
    thread_source = "user"
    if parent_id:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": depth,
                                                "agent_path": agent_path, "agent_role": None}}}
        thread_source = "subagent"
    return {"timestamp": "2026-07-09T00:00:00Z", "type": "session_meta",
            "payload": {"id": rollout_id, "session_id": root_id, "cwd": cwd, "source": source, "thread_source": thread_source}}


def task_started(turn_id):
    return {"timestamp": "2026-07-09T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}


def turn_context(turn_id, effort):
    return {"timestamp": "2026-07-09T00:00:02Z", "type": "turn_context", "payload": {"turn_id": turn_id, "model": "gpt-5.6-sol", "effort": effort}}


def token_count(total_in, total_cache, total_out, last_in, last_cache, last_out):
    return {"timestamp": "2026-07-09T00:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": total_cache, "output_tokens": total_out},
        "last_token_usage": {"input_tokens": last_in, "cached_input_tokens": last_cache, "output_tokens": last_out}}}}


def response_tool(turn_id, name, tool_id):
    return {"timestamp": "2026-07-09T00:00:02Z", "type": "response_item", "payload": {
        "type": "custom_tool_call", "id": tool_id, "status": "completed", "name": name,
        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}}


def legacy_tool_end(event_type, turn_id):
    return {"timestamp": "2026-07-09T00:00:02Z", "type": "event_msg", "payload": {"type": event_type, "turn_id": turn_id}}


def set_mtime(path, when):
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def bucket(**values):
    result = uc.new_token_bucket()
    result.update(values)
    return result


def view_path(usage_dir):
    return os.path.join(usage_dir, "v2", "views", "current-session.json")


def run_tests(temp_root):
    generic_models = {"gpt-5": {"in": 1.25, "out": 10.0, "cache_read": 0.125}}
    check(uc.get_model_price(generic_models, "gpt-5.6-sol") is None, "unknown model inherited a generic prefix price")
    dated_models = {"claude-test": {"in": 1.0, "out": 2.0}}
    check(uc.get_model_price(dated_models, "claude-test-20260709") is not None, "safe dated alias lookup regressed")

    uc.write_json_atomic(os.path.join(temp_root, "prices.cache.json"), {
        "fetched_at": "2026-07-09T00:00:00Z", "source": "test",
        "models": {"gpt-5.5": {"in": 5.0, "out": 30.0, "cache_read": 0.5}}})
    merged = uc.get_price_table(SCRIPT_DIR, temp_root)
    check(merged["models"]["gpt-5.5"].get("long_in") == 10.0, "cache refresh erased official long-context pricing metadata")

    id_a = "019f1234-0000-0000-0000-000000000001"
    id_b = "019f1234-0000-0000-0000-000000000002"
    state_a = uc.get_usage_state_path(temp_root, id_a)
    state_b = uc.get_usage_state_path(temp_root, id_b)
    check(state_a != state_b, "full session ids still collide in state paths")
    check(id_a in state_a and id_b in state_b, "state path truncated a session id")

    root_id, child_id, grandchild_id = "root-session", "child-session", "grandchild-session"
    paths = {name: os.path.join(temp_root, name + ".jsonl") for name in ("root", "child", "grandchild", "unrelated", "orphan")}

    root_entries = [task_started("root-turn"), turn_context("root-turn", "ultra"), response_tool("root-turn", "exec", "root-tool"),
                    legacy_tool_end("exec_command_end", "root-turn"), legacy_tool_end("patch_apply_end", "root-turn"),
                    token_count(100, 40, 10, 100, 40, 10)]
    child_entries = [task_started("child-turn"), turn_context("child-turn", "high"), response_tool("child-turn", "wait", "child-tool"),
                     token_count(160, 60, 16, 60, 20, 6)]
    grandchild_entries = [task_started("grandchild-turn"), turn_context("grandchild-turn", "max"), token_count(200, 70, 20, 40, 10, 4)]
    for entry in [session_meta(root_id, root_id, temp_root)] + root_entries:
        write_json_line(paths["root"], entry)
    for entry in [session_meta(child_id, root_id, temp_root, root_id, 1, "/root/child")] + root_entries + child_entries:
        write_json_line(paths["child"], entry)
    for entry in [session_meta(grandchild_id, root_id, temp_root, child_id, 2, "/root/child/grandchild")] + root_entries + child_entries + grandchild_entries:
        write_json_line(paths["grandchild"], entry)
    for entry in [session_meta("unrelated", "unrelated", temp_root), task_started("other-turn"), turn_context("other-turn", "low"), token_count(999, 0, 99, 999, 0, 99)]:
        write_json_line(paths["unrelated"], entry)
    for entry in [session_meta("orphan", root_id, temp_root, "missing-parent", 2, "/root/orphan"), task_started("orphan-turn"), turn_context("orphan-turn", "ultra"), token_count(999, 0, 99, 999, 0, 99)]:
        write_json_line(paths["orphan"], entry)

    price_models = {"gpt-5.6-sol": {"in": 5.0, "out": 30.0, "cache_read": 0.5, "cache_write_5m": 6.25, "cache_write_1h": 6.25,
                                    "long_context_threshold": 272000, "long_in": 10.0, "long_out": 45.0, "long_cache_read": 1.0, "long_cache_write": 12.5}}
    snapshot = uc.get_codex_session_usage(root_id, paths["root"], [paths["root"], paths["child"], paths["grandchild"], paths["unrelated"]], price_models)
    check(snapshot["status"] == "ok", "lineage snapshot was not complete: " + "; ".join(snapshot["warnings"]))
    check(snapshot["rollouts"] == 3 and snapshot["agentRuns"] == 2 and snapshot["turns"] == 1, "lineage/session counts are wrong")
    check(len(snapshot["rows"]) == 3, "model+effort rows were merged or unrelated data leaked in")
    total = uc.new_token_bucket()
    for row in snapshot["rows"]:
        uc.add_token_bucket(total, row["bucket"])
    check(total["calls"] == 3 and total["in"] == 130 and total["cacheRead"] == 70 and total["out"] == 20, "ancestor replay was double-counted")
    check(",".join(sorted(row["effort"] for row in snapshot["rows"])) == "high,max,ultra", "reasoning effort was lost")
    tool_counts = snapshot["toolCounts"]
    check(sum(t["calls"] for t in tool_counts.values()) == 3, "hybrid Codex response/legacy tools were missed, duplicated, or replayed")
    check("codex|exec" in tool_counts and "codex|wait" in tool_counts and "codex|apply_patch" in tool_counts and "codex|shell" not in tool_counts,
          "hybrid Codex tool schema did not prefer modern names while retaining legacy-only tools")
    partial = uc.get_codex_session_usage(root_id, paths["root"], [paths["root"], paths["child"], paths["grandchild"], paths["orphan"]], price_models)
    check(partial["status"] == "partial" and partial["rollouts"] == 3 and len(partial["rows"]) == 3, "missing-parent descendant was not excluded fail-closed")

    long_bucket = bucket(calls=2, **{"in": 300}, out=30, cacheRead=100, longCalls=1, longIn=200, longOut=20, longCacheRead=80)
    tiered = uc.get_bucket_cost(price_models["gpt-5.6-sol"], long_bucket)
    expected = ((100 * 5.0) + (10 * 30.0) + (20 * 0.5) + (200 * 10.0) + (20 * 45.0) + (80 * 1.0)) / 1000000.0
    check(abs(tiered - expected) < 0.0000001, "long-context pricing was applied after aggregation")

    v2_usage = os.path.join(temp_root, "v2-usage")
    ultra = bucket(calls=1, **{"in": 10}, out=2)
    high = bucket(calls=1, **{"in": 20}, out=3)
    v2_state = {"session": {"models": {"gpt-5.6-sol|ultra": uc.copy_token_bucket(ultra), "gpt-5.6-sol|high": uc.copy_token_bucket(high)},
                            "tools": {"codex|shell": {"name": "shell", "kind": "codex", "calls": 1, "failures": 0}},
                            "turns": 1, "samples": 1}}
    v2_prices = {"models": price_models, "sourceLabel": "test prices"}
    sol = price_models["gpt-5.6-sol"]
    shell_tool = {"codex|shell": {"name": "shell", "kind": "codex", "calls": 1, "failures": 0}}
    uc.write_v2_session_snapshot(v2_usage, temp_root, "codex", "v2-session", v2_state, v2_prices, [
        {"model": "gpt-5.6-sol", "effort": "ultra", "scope": "main", "bucket": ultra, "cost": uc.get_bucket_cost(sol, ultra)},
        {"model": "gpt-5.6-sol", "effort": "high", "scope": "main", "bucket": high, "cost": uc.get_bucket_cost(sol, high)},
    ], 0.0, True, 0.0, True, 0.0, 0, {"userMessages": 1, "assistantMessages": 1}, shell_tool)
    ultra_delta = bucket(calls=1, **{"in": 5}, out=1)
    uc.add_token_bucket(v2_state["session"]["models"]["gpt-5.6-sol|ultra"], ultra_delta)
    v2_state["session"]["tools"]["codex|shell"]["calls"] = 2
    v2_state["session"]["samples"] = 2
    uc.write_v2_session_snapshot(v2_usage, temp_root, "codex", "v2-session", v2_state, v2_prices, [
        {"model": "gpt-5.6-sol", "effort": "ultra", "scope": "main", "bucket": ultra_delta, "cost": uc.get_bucket_cost(sol, ultra_delta)},
    ], 0.0, True, 0.0, True, 0.0, 0, {"userMessages": 0, "assistantMessages": 1}, shell_tool)
    v2_events = uc.get_v2_events(v2_usage)
    check(len([e for e in v2_events if e["type"] == "span.usage"]) == 3 and len([e for e in v2_events if e["type"] == "trace.ended"]) == 2,
          "v2 sample or effort idempotency keys collided")
    v2_view = uc.read_json_file(view_path(v2_usage))
    check(len(v2_view["totals"]["models"]) == 2, "v2 totals merged distinct reasoning efforts")
    check(",".join(sorted(m["effort"] for m in v2_view["totals"]["models"])) == "high,ultra", "v2 view lost reasoning effort")
    check(v2_view["totals"]["tools"][0]["calls"] == 2, "v2 tool metrics did not aggregate across samples")

    unpriced_root = os.path.join(temp_root, "unpriced-root")
    unpriced_usage = os.path.join(unpriced_root, ".agents", "usage")
    unpriced_bucket = bucket(calls=1, **{"in": 10}, out=2)
    unpriced_state = {"session": {"models": {"future-model|ultra": unpriced_bucket}, "tools": {}, "turns": 1, "samples": 1}}
    uc.write_v2_session_snapshot(unpriced_usage, unpriced_root, "claude", "unpriced-session", unpriced_state, {"models": {}, "sourceLabel": "test prices"},
                                 [{"model": "future-model", "effort": "ultra", "scope": "main", "bucket": unpriced_bucket, "cost": None}],
                                 0.0, False, 0.0, False, 1.0, 0, {"userMessages": 1, "assistantMessages": 1}, {})
    unpriced_view = uc.read_json_file(view_path(unpriced_usage))
    check(not unpriced_view["lastTurn"]["costComplete"] and unpriced_view["lastTurn"]["estimatedCostUsd"] is None,
          "unpriced v2 turn was rendered as a zero-cost estimate")

    stats_root = os.path.join(temp_root, "stats-root")
    stats_usage = os.path.join(stats_root, ".agents", "usage")
    os.makedirs(stats_usage)
    history = os.path.join(stats_usage, "history.jsonl")
    now_text = uc.iso_o(uc.utcnow())

    def codex_record(source, calls, tokens_in, out, cost, revision=None):
        record = {"v": 1, "accountingVersion": 2, "ts": now_text, "platform": "codex", "source": source, "sessionId": "checkpoint-session",
                  "rows": [{"model": "gpt-5.6-sol", "effort": "ultra", "scope": "main", "calls": calls, "in": tokens_in, "out": out,
                            "cacheRead": 0, "cache5m": 0, "cache1h": 0, "estCost": cost}]}
        if revision:
            record["rolloutRevision"] = revision
        return record

    for record in (codex_record("session", 1, 10, 1, 0.1), codex_record("rollout-scan", 2, 100, 10, 1.0, "one"),
                   codex_record("session", 1, 5, 1, 0.05), codex_record("rollout-scan", 3, 120, 12, 1.2, "two")):
        write_json_line(history, record)
    stats_args = ["--project-root", stats_root, "--no-codex-scan", "--quiet"]

    def stats_tokens_in():
        run_script("usage_stats.py", stats_args)
        return uc.read_json_file(os.path.join(stats_usage, "stats-summary.json"))["windows"]["24h"]["totals"]["tokensIn"]

    check(stats_tokens_in() == 120, "latest rollout checkpoint did not replace older Codex deltas")
    write_json_line(history, codex_record("session", 1, 3, 1, 0.03))
    check(stats_tokens_in() == 123, "post-checkpoint Codex hook delta was discarded")
    write_json_line(history, codex_record("session-snapshot", 4, 150, 15, 1.5, "live-three"))
    check(stats_tokens_in() == 150, "full live session snapshot was added to an older checkpoint")

    claude_root = os.path.join(temp_root, "claude-root")
    claude_usage = os.path.join(claude_root, ".agents", "usage")
    os.makedirs(claude_usage)
    claude_session = "claude-state-migration"
    claude_state_path = uc.get_usage_state_path(claude_usage, claude_session)
    uc.write_json_atomic(claude_state_path, {"files": {}, "seen": [], "session": {"turns": 2, "models": {
        "claude-test": {"calls": 1, "in": 10, "out": 1, "cacheRead": 0, "cache5m": 0, "cache1h": 0},
        "claude-test|unspecified": {"calls": 2, "in": 20, "out": 2, "cacheRead": 0, "cache5m": 0, "cache1h": 0}}}})
    claude_transcript = os.path.join(claude_root, "claude.jsonl")
    for content in ({"type": "text", "text": "done"}, {"type": "tool_use", "id": "tool-one", "name": "read"}):
        write_json_line(claude_transcript, {"timestamp": now_text, "type": "assistant", "requestId": "request-one", "message": {
            "id": "message-one", "model": "claude-test", "usage": {"input_tokens": 5, "output_tokens": 1}, "content": [content]}})
    run_script("usage_report.py", ["--platform", "claude"],
               json.dumps({"session_id": claude_session, "transcript_path": claude_transcript, "cwd": claude_root}))
    migrated = uc.read_json_file(claude_state_path)
    state_keys = list(migrated["session"]["models"])
    check(state_keys == ["claude-test|unspecified"], "legacy Claude model key was not normalized")
    merged_bucket = migrated["session"]["models"]["claude-test|unspecified"]
    check(merged_bucket["calls"] == 4 and merged_bucket["in"] == 35, "legacy and normalized Claude state buckets were not merged exactly once")
    claude_view = uc.read_json_file(view_path(claude_usage))
    check(claude_view["sessionId"] == claude_session and claude_view["totals"]["models"][0]["effort"] == "unspecified",
          "Claude v2 session view was not written with effort")
    check(claude_view["totals"]["models"][0]["calls"] == 4 and claude_view["totals"]["models"][0]["inputTokens"] == 35,
          "v2 rebuild discarded migrated Claude totals")
    check(claude_view["totals"]["tools"][0]["name"] == "read", "Claude tool metric was not restored in v2")

    footer_args = ["--project-root", claude_root, "--platform", "claude", "--session-id", claude_session]
    claude_footer = run_script("usage_footer.py", footer_args + ["--mode", "Full"])
    footer_lines = [line for line in claude_footer.splitlines() if line.strip()]
    check(len(footer_lines) == 1 and claude_footer.startswith("Usage: claude-test [unspecified] | session ")
          and claude_footer.endswith("35 in / 4 out | 4 calls"), "Claude compact footer regressed: " + claude_footer)
    check(not any(noise in claude_footer for noise in ("sessionId", "recorded", "prices:", "claude/read", "cacheR", "source:")),
          "Claude footer leaked operator noise: " + claude_footer)
    check(run_script("usage_footer.py", footer_args + ["--mode", "Brief"]) == claude_footer,
          "Brief and Full footer modes no longer share the compact operator format")
    unpriced_footer = run_script("usage_footer.py", ["--project-root", unpriced_root, "--platform", "claude", "--session-id", "unpriced-session", "--mode", "Full"])
    check("session n/a" in unpriced_footer and "$0.00" not in unpriced_footer, "unpriced v2 footer did not fail closed: " + unpriced_footer)

    gemini_root = os.path.join(temp_root, "gemini-root")
    gemini_usage = os.path.join(gemini_root, ".agents", "usage")
    os.makedirs(gemini_usage)
    write_json_line(os.path.join(gemini_usage, "gemini-telemetry.log"),
                    {"timestamp": now_text, "model": "gemini-test", "input_token_count": 7, "output_token_count": 2, "tool_token_count": 3})
    gemini_session = "gemini-v2-session"
    run_script("usage_report.py", ["--platform", "gemini"], json.dumps({"session_id": gemini_session, "cwd": gemini_root, "model": "gemini-test"}))
    gemini_view = uc.read_json_file(view_path(gemini_usage))
    check(gemini_view["sessionId"] == gemini_session and gemini_view["totals"]["models"][0]["model"] == "gemini-test"
          and gemini_view["totals"]["models"][0]["effort"] == "unspecified", "Gemini v2 session/model view regressed")
    check(gemini_view["totals"]["tools"][0]["name"] == "tool_tokens", "Gemini tool metric was not restored in v2")

    scanner_usage = os.path.join(temp_root, "scanner-usage")
    os.makedirs(scanner_usage)
    write_json_line(os.path.join(scanner_usage, "history.jsonl"), {
        "v": 1, "accountingVersion": 2, "ts": now_text, "platform": "codex", "source": "session", "sessionId": root_id,
        "rows": [{"model": "gpt-5.6-sol", "effort": "ultra", "scope": "main", "calls": 1, "in": 60, "out": 10, "cacheRead": 40,
                  "cache5m": 0, "cache1h": 0, "estCost": 0.001}]})
    codex_home = os.path.join(temp_root, "codex-home")
    scanner_sessions = os.path.join(codex_home, "sessions", "2026", "07", "09")
    os.makedirs(scanner_sessions)
    scanner = {name: os.path.join(scanner_sessions, "rollout-%s-session.jsonl" % name) for name in ("root", "child", "grandchild", "orphan")}
    for name in ("root", "child", "grandchild"):
        shutil.copyfile(paths[name], scanner[name])
    old_write = uc.utcnow() - _dt.timedelta(hours=3)
    set_mtime(scanner["root"], old_write)
    set_mtime(scanner["child"], old_write)
    set_mtime(scanner["grandchild"], uc.utcnow())
    os.environ["CODEX_HOME"] = codex_home

    def scan():
        return uc.get_codex_rollout_records(temp_root, scanner_usage, SCRIPT_DIR, 90)

    check(scan()["records"] == 0, "active descendant did not keep the Codex lineage open")
    set_mtime(scanner["grandchild"], old_write)
    check(scan()["records"] == 1, "intermediate hook record incorrectly suppressed the quiet rollout scan")
    scan_history = [json.loads(line) for line in uc.read_all_lines(os.path.join(scanner_usage, "history.jsonl"))]
    scan_history = [record for record in scan_history if record.get("source") == "rollout-scan"]
    check(len(scan_history) == 1 and str(scan_history[0].get("rolloutRevision") or "").strip(), "rollout scan did not write an authoritative revision checkpoint")
    check(scan()["records"] == 0, "unchanged rollout revision was scanned twice")
    write_json_line(scanner["grandchild"], {"timestamp": now_text, "type": "event_msg", "payload": {"type": "agent_message"}})
    set_mtime(scanner["grandchild"], old_write)
    check(scan()["records"] == 1, "changed rollout revision was permanently suppressed")
    shutil.copyfile(paths["orphan"], scanner["orphan"])
    set_mtime(scanner["orphan"], old_write)
    check(scan()["records"] == 0, "partial Codex lineage was written as an authoritative checkpoint")
    set_mtime(scanner["orphan"], uc.utcnow())

    partial_usage = os.path.join(temp_root, ".agents", "usage")
    os.makedirs(partial_usage, exist_ok=True)
    partial_state = uc.get_usage_state_path(partial_usage, root_id)
    uc.write_json_atomic(partial_state, {"sentinel": "preserve", "files": {}, "seen": [], "session": {"turns": 9, "models": {}}})
    with open(partial_state, encoding="utf-8") as handle:
        before = handle.read()
    run_script("usage_report.py", ["--platform", "codex"], json.dumps({"session_id": root_id, "transcript_path": scanner["root"], "cwd": temp_root}))
    with open(partial_state, encoding="utf-8") as handle:
        check(handle.read() == before, "partial Codex hook snapshot replaced trusted reporter state")

    uc.write_text(os.path.join(partial_usage, "last-report-codex.md"),
                  "# Usage report\n\nPlatform: codex\nSession: stale\n\n## Last turn\n\n    Usage codex: stale gpt-5.5\n")
    for name in ("CODEX_THREAD_ID", "CODEX_SHELL", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
        os.environ.pop(name, None)
    footer_output = run_script("usage_footer.py", ["--project-root", temp_root, "--platform", "codex", "--mode", "Full"])
    check(footer_output == "Usage: unavailable", "footer did not fail closed compactly without a session id: " + footer_output)
    check("gpt-5.5" not in footer_output, "footer leaked a stale platform-global report")
    auto_output = run_script("usage_footer.py", ["--project-root", temp_root, "--platform", "auto", "--mode", "Full"])
    check(auto_output == "Usage: unavailable" and "gpt-5.5" not in auto_output, "auto mode leaked a stale global report or verbose diagnostics")


def main():
    temp_root = os.path.realpath(tempfile.mkdtemp(prefix="agent-kit-usage-test-"))
    saved = dict(os.environ)
    os.environ["AGENT_KIT_USAGE_NO_PRICE_REFRESH"] = "1"
    # Hooks prefer the project that contains the hook cwd; keep the developer's
    # own Claude project out of the fixtures.
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
    try:
        run_tests(temp_root)
    except AssertionError as exc:
        print("usage regression tests: FAIL - %s" % exc)
        return 1
    finally:
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(temp_root, ignore_errors=True)
    print("usage regression tests: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
