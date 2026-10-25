#!/usr/bin/env python3
"""Renders a visible usage footer for agent final responses. Python port of usage-footer.ps1.

The lifecycle hook collects usage into .agents/usage/. For an exact Codex
session this script also reads a live, replay-aware rollout snapshot, because
some clients hide hook output or invoke the footer before the Stop hook.
"""

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True  # keep .agents/scripts free of __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage_common as uc  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VERBOSE = False


def write_unavailable(reason):
    print("Usage: unavailable")
    if VERBOSE:
        print("VERBOSE: " + reason, file=sys.stderr)


def get_report_section(lines, heading):
    result = []
    in_section = False
    for line in lines:
        if re.match(r"^##\s+", line):
            if in_section:
                break
            if line.strip() == "## " + heading:
                in_section = True
            continue
        if in_section and line.strip():
            result.append(line.strip())
    return result


def to_brief_summary(line):
    text = (line or "").strip()
    if text.startswith("Usage "):
        return text[6:]
    if text.startswith("Usage:"):
        return text[6:].strip()
    return text


def resolve_platform(requested):
    if requested != "auto":
        return requested
    env = os.environ
    if env.get("CODEX_SHELL") or env.get("CODEX_THREAD_ID") or env.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
        return "codex"
    for name in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_SESSION_ID", "ANTHROPIC_SESSION_ID"):
        if (env.get(name) or "").strip():
            return "claude"
    if env.get("GEMINI_CLI") or env.get("GEMINI_SESSION_ID"):
        return "gemini"
    return "auto"


def resolve_session_id(requested, platform):
    if requested and requested.strip():
        return requested
    env = os.environ
    if platform == "codex" and (env.get("CODEX_THREAD_ID") or "").strip():
        return env["CODEX_THREAD_ID"]
    if platform == "claude":
        for name in ("CLAUDE_SESSION_ID", "ANTHROPIC_SESSION_ID"):
            if (env.get(name) or "").strip():
                return env[name]
    if platform == "gemini" and (env.get("GEMINI_SESSION_ID") or "").strip():
        return env["GEMINI_SESSION_ID"]
    return ""


def resolve_report_path(usage_dir, platform, session_id):
    if not os.path.isdir(usage_dir) or platform == "auto" or not session_id:
        return None
    path = os.path.join(usage_dir, "last-report-" + uc.to_safe_name(platform) + "-" + uc.to_safe_name(session_id) + ".md")
    return path if os.path.exists(path) else None


def get_codex_footer_totals(rows, price_models):
    totals = {}
    for row in rows:
        key = str(row["model"]) + "|" + str(row["effort"])
        item = totals.setdefault(key, {"model": str(row["model"]), "effort": str(row["effort"]), "bucket": uc.new_token_bucket()})
        uc.add_token_bucket(item["bucket"], row["bucket"])
    result = []
    for key in sorted(totals, key=uc.sort_key_text):
        item = totals[key]
        price = uc.get_model_price(price_models, item["model"])
        item["cost"] = uc.get_bucket_cost(price, item["bucket"]) if price else None
        result.append(item)
    return result


def get_identity_summary(rows):
    by_model = {}
    for row in rows:
        model = str(row.get("model") or "") or "unknown"
        effort = str(row.get("effort") or "") or "unspecified"
        by_model.setdefault(model, set()).add(effort)
    identities = [model + " [" + "/".join(sorted(by_model[model], key=uc.sort_key_text)) + "]"
                  for model in sorted(by_model, key=uc.sort_key_text)]
    if not identities:
        return "unknown"
    if len(identities) <= 2:
        return ", ".join(identities)
    return ", ".join(identities[:2]) + " +" + str(len(identities) - 2)


def write_compact_usage(identity, cost, input_tokens, output_tokens, calls):
    cost_text = "n/a" if cost == "n/a" else "~" + cost
    print("Usage: " + identity + " | session " + cost_text + " | " + uc.format_tokens(input_tokens) + " in / "
          + uc.format_tokens(output_tokens) + " out | " + "{:,}".format(int(calls)) + " calls")


def write_codex_live_footer(snapshot, prices):
    totals = get_codex_footer_totals(snapshot["rows"], prices["models"])
    session_cost = 0.0
    cost_complete = True
    session_bucket = uc.new_token_bucket()
    for row in totals:
        uc.add_token_bucket(session_bucket, row["bucket"])
        if row["cost"] is None:
            cost_complete = False
        else:
            session_cost += row["cost"]
    cost_text = "$" + uc.format_money(session_cost) if cost_complete else "n/a"
    write_compact_usage(get_identity_summary(totals), cost_text, session_bucket["in"], session_bucket["out"], session_bucket["calls"])


def resolve_v2_current_session(usage_dir, platform, session_id):
    view = uc.read_json_file(os.path.join(usage_dir, "v2", "views", "current-session.json"))
    if not isinstance(view, dict) or uc.to_long(view.get("v")) != 2:
        return None
    if str(view.get("platform", "")).lower() != platform.lower():
        return None
    if str(view.get("sessionId", "")) == session_id:
        return view
    for alias in uc.as_list(view.get("aliases")):
        if str(alias) == session_id:
            return view
    return None


def write_v2_footer(view):
    totals = view.get("totals") if isinstance(view.get("totals"), dict) else {}
    session_cost = totals.get("estimatedCostUsd")
    complete = uc.ps_bool(totals.get("costComplete", True))
    models = uc.as_list(totals.get("models"))
    rows = [{"model": str(uc.prop(m, "model", "unknown")), "effort": str(uc.prop(m, "effort", "unspecified"))} for m in models]
    calls = sum(int(uc.event_number(m, "calls")) for m in models)
    input_tokens = sum(int(uc.event_number(m, "inputTokens")) for m in models)
    output_tokens = sum(int(uc.event_number(m, "outputTokens")) for m in models)
    cost = "n/a" if session_cost is None or not complete else "$" + uc.format_money(float(session_cost))
    write_compact_usage(get_identity_summary(rows), cost, input_tokens, output_tokens, calls)


def main(argv=None):
    global VERBOSE
    parser = argparse.ArgumentParser(description="Visible usage footer for agent final responses.")
    # Retained for caller compatibility. Both modes render one operator line.
    parser.add_argument("--mode", "-Mode", dest="mode", default="Brief", choices=["Brief", "Full"])
    parser.add_argument("--project-root", "-ProjectRoot", dest="project_root", default=uc.find_project_root())
    parser.add_argument("--platform", "-Platform", dest="platform", default="auto", choices=["auto", "codex", "claude", "gemini"])
    parser.add_argument("--session-id", "-SessionId", dest="session_id", default="")
    parser.add_argument("--verbose", "-Verbose", dest="verbose", action="store_true")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    try:
        root = args.project_root
        if not root or not os.path.isdir(root):
            root = os.getcwd()
        usage_dir = os.path.join(root, ".agents", "usage")
        platform = resolve_platform(args.platform)
        session_id = resolve_session_id(args.session_id, platform)
        if platform == "auto":
            write_unavailable("platform not detected; pass --platform codex, --platform claude, or --platform gemini")
            return 0
        if not session_id:
            write_unavailable("exact " + platform + " session id is required; pass --session-id explicitly")
            return 0

        if platform == "codex":
            prices = uc.get_price_table(SCRIPT_DIR, usage_dir)
            snapshot = uc.get_codex_session_usage(session_id, price_models=prices["models"])
            if snapshot["status"] == "ok" and snapshot["rows"]:
                write_codex_live_footer(snapshot, prices)
                return 0
            if snapshot["status"] == "partial":
                write_unavailable("Codex rollout lineage is incomplete for session " + session_id + "; refusing a partial estimate")
                return 0
        view = resolve_v2_current_session(usage_dir, platform, session_id)
        if view:
            write_v2_footer(view)
            return 0
        last_report = resolve_report_path(usage_dir, platform, session_id)
        if not last_report:
            write_unavailable("no " + platform + " usage report for session " + session_id + " yet; finish one hooked turn in this session")
            return 0

        last_turn = get_report_section(uc.read_all_lines(last_report), "Last turn")
        if not last_turn:
            write_unavailable("last-report.md has no Last turn section")
            return 0
        print("Usage: " + to_brief_summary(last_turn[0]))
        return 0
    except Exception as exc:  # noqa: BLE001
        write_unavailable(str(exc))
        return 0


if __name__ == "__main__":
    sys.exit(main())
