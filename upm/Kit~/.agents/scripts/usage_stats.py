#!/usr/bin/env python3
"""Persistent usage statistics aggregator. Python port of usage-stats.ps1.

Reads .agents/usage/history.jsonl, optionally scans Codex rollout files, and
writes stats-summary.json plus usage-stats.md for the Unity package UI.
"""

import argparse
import datetime as _dt
import os
import sys
import uuid

sys.dont_write_bytecode = True  # keep .agents/scripts free of __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage_common as uc  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_usage_config(usage_dir):
    retention_days = 90
    codex_scan_enabled = True
    config = uc.read_json_file(os.path.join(usage_dir, "usage-config.json"))
    if isinstance(config, dict):
        if config.get("retentionDays"):
            retention_days = uc.to_long(config["retentionDays"])
        if "codexScanEnabled" in config:
            codex_scan_enabled = uc.ps_bool(config["codexScanEnabled"])
    retention_days = min(365, max(30, retention_days))
    return {"retentionDays": retention_days, "codexScanEnabled": codex_scan_enabled}


def read_history_records(path, retention_days):
    result = {"records": [], "invalid": 0, "unknown": 0, "rawLines": [], "lineCount": 0,
              "hasExpired": False, "firstRecord": None, "lastByPlatform": {}}
    if not os.path.exists(path):
        return result

    raw_lines = uc.read_all_lines(path)
    result["rawLines"] = raw_lines
    result["lineCount"] = len(raw_lines)
    cutoff = uc.utcnow() - _dt.timedelta(days=retention_days)
    parsed = []

    for index, line in enumerate(raw_lines):
        if not line.strip():
            continue
        record = uc.parse_json(line)
        if record is None:
            result["invalid"] += 1
            continue
        if not isinstance(record, dict) or uc.to_long(record.get("v"), -1) != 1:
            result["unknown"] += 1
            continue
        if record.get("platform") == "codex" and uc.to_long(record.get("accountingVersion"), 0) < 2:
            # Legacy Codex rows omitted reasoning effort, descendants, and
            # replay ownership. Treat them as untrusted derived data.
            result["unknown"] += 1
            continue
        ts = uc.to_utc_date(record.get("ts"))
        if not ts:
            result["invalid"] += 1
            continue
        if ts < cutoff:
            result["hasExpired"] = True
        parsed.append({"record": record, "ts": ts, "index": index})

    # A quiet-lineage rollout scan is an authoritative Codex checkpoint. Keep
    # only the latest checkpoint plus hook deltas written after it. Before the
    # first checkpoint, incremental hook records remain the best available data.
    latest_scan_by_session = {}
    for item in parsed:
        record = item["record"]
        if str(record.get("platform", "")) != "codex":
            continue
        if record.get("source") not in ("rollout-scan", "session-snapshot"):
            continue
        session_id = str(record.get("sessionId", "") or "")
        if session_id.strip():
            latest_scan_by_session[session_id] = item["index"]

    for item in parsed:
        record = item["record"]
        platform = str(record.get("platform", "unknown"))
        if platform == "codex":
            session_id = str(record.get("sessionId", "") or "")
            if session_id in latest_scan_by_session and item["index"] < latest_scan_by_session[session_id]:
                continue
        if not result["firstRecord"] or item["ts"] < result["firstRecord"]:
            result["firstRecord"] = item["ts"]
        last = result["lastByPlatform"].get(platform)
        if not last or item["ts"] > last:
            result["lastByPlatform"][platform] = item["ts"]
        result["records"].append(item)
    return result


def prune_history(path, history, retention_days):
    if not history["hasExpired"] or not os.path.exists(path):
        return
    cutoff = uc.utcnow() - _dt.timedelta(days=retention_days)
    kept = []
    for line in history["rawLines"]:
        if not line.strip():
            continue
        record = uc.parse_json(line)
        if isinstance(record, dict) and uc.to_long(record.get("v"), -1) == 1:
            ts = uc.to_utc_date(record.get("ts"))
            if ts and ts < cutoff:
                continue
        kept.append(line)

    current = uc.read_all_lines(path)
    kept.extend(current[history["lineCount"]:])

    temp = path + "." + uuid.uuid4().hex[:8] + ".tmp"
    uc.write_text(temp, ("\n".join(kept) + "\n") if kept else "")
    os.replace(temp, path)


def new_accumulator():
    return {"requests": 0, "userMessages": 0, "assistantMessages": 0, "tokensIn": 0, "tokensOut": 0,
            "cacheRead": 0, "cacheWrite": 0, "estCost": 0.0, "costComplete": True, "wallSeconds": 0.0}


def add_row(acc, row):
    acc["requests"] += uc.to_long(uc.prop(row, "calls"))
    acc["tokensIn"] += uc.to_long(uc.prop(row, "in"))
    acc["tokensOut"] += uc.to_long(uc.prop(row, "out"))
    acc["cacheRead"] += uc.to_long(uc.prop(row, "cacheRead"))
    acc["cacheWrite"] += uc.to_long(uc.prop(row, "cache5m")) + uc.to_long(uc.prop(row, "cache1h"))
    cost = uc.prop(row, "estCost")
    if cost is None:
        acc["costComplete"] = False
    else:
        acc["estCost"] += float(cost)


def to_totals(acc):
    denom = float(acc["tokensIn"] + acc["cacheRead"])
    ratio = float(acc["cacheRead"]) / denom if denom > 0 else 0.0
    totals = dict(acc)
    totals["cacheHitRatio"] = ratio
    return totals


def new_window_summary(records, window_days, now, first_record):
    cutoff = now - _dt.timedelta(days=window_days)
    window_records = [item for item in records if item["ts"] >= cutoff]
    covered_days = 0.0
    if first_record:
        covered_days = min(window_days, max(0.0, (now - first_record).total_seconds() / 86400.0))

    totals = new_accumulator()
    per_platform = {name: new_accumulator() for name in ("claude", "codex", "gemini")}
    models = {}
    roles = {}

    for item in window_records:
        record = item["record"]
        platform = str(record.get("platform", "unknown"))
        platform_acc = per_platform.setdefault(platform, new_accumulator())
        user_messages = uc.to_long(record.get("userMessages"))
        assistant_messages = uc.to_long(record.get("assistantMessages"))
        wall_seconds = uc.to_float(record.get("wallSeconds"))
        for acc in (totals, platform_acc):
            acc["userMessages"] += user_messages
            acc["assistantMessages"] += assistant_messages
            acc["wallSeconds"] += wall_seconds

        seen_role_keys = set()
        for row in uc.as_list(record.get("rows")):
            model = str(uc.prop(row, "model", "unknown"))
            effort = str(uc.prop(row, "effort", "unspecified"))
            scope = str(uc.prop(row, "scope", "main"))
            model_key = platform + "|" + model + "|" + effort
            role_key = platform + "|" + scope
            model_info = models.setdefault(model_key, {"platform": platform, "model": model, "effort": effort,
                                                       "acc": new_accumulator(), "costPriced": True})
            role = roles.setdefault(role_key, {"platform": platform, "scope": scope, "runs": 0, "tokensIn": 0,
                                               "tokensOut": 0, "estCost": 0.0, "lastUsedUtc": item["ts"]})
            add_row(totals, row)
            add_row(platform_acc, row)
            add_row(model_info["acc"], row)
            if not model_info["acc"]["costComplete"]:
                model_info["costPriced"] = False

            role["tokensIn"] += uc.to_long(uc.prop(row, "in"))
            role["tokensOut"] += uc.to_long(uc.prop(row, "out"))
            role_cost = uc.prop(row, "estCost")
            if role_cost is not None:
                role["estCost"] += float(role_cost)
            if item["ts"] > role["lastUsedUtc"]:
                role["lastUsedUtc"] = item["ts"]
            seen_role_keys.add(role_key)
        for role_key in seen_role_keys:
            roles[role_key]["runs"] += 1

    tokens_per_day = cost_per_day = 0.0
    if covered_days > 0:
        tokens_per_day = float(totals["tokensIn"] + totals["tokensOut"]) / covered_days
        cost_per_day = totals["estCost"] / covered_days
    cost_per_active_hour = totals["estCost"] / (totals["wallSeconds"] / 3600.0) if totals["wallSeconds"] >= 60 else 0.0

    total_tokens = float(totals["tokensIn"] + totals["tokensOut"])
    total_requests = float(totals["requests"])
    total_cost = totals["estCost"]

    model_objects = []
    for key in sorted(models, key=uc.sort_key_text):
        info = models[key]
        acc = info["acc"]
        token_share = 100.0 * (acc["tokensIn"] + acc["tokensOut"]) / total_tokens if total_tokens > 0 else 0.0
        request_share = 100.0 * acc["requests"] / total_requests if total_requests > 0 else 0.0
        cost_share = 100.0 * acc["estCost"] / total_cost if info["costPriced"] and total_cost > 0 else 0.0
        model_objects.append({
            "model": info["model"], "effort": info["effort"], "platform": info["platform"],
            "requests": acc["requests"], "tokensIn": acc["tokensIn"], "tokensOut": acc["tokensOut"],
            "estCost": acc["estCost"], "costPriced": info["costPriced"],
            "costSharePct": round(cost_share, 1), "tokenSharePct": round(token_share, 1),
            "requestSharePct": round(request_share, 1),
        })

    role_objects = []
    for key in sorted(roles, key=uc.sort_key_text):
        role = roles[key]
        role_objects.append({
            "scope": role["scope"], "platform": role["platform"], "runs": role["runs"],
            "tokensIn": role["tokensIn"], "tokensOut": role["tokensOut"], "estCost": role["estCost"],
            "lastUsedUtc": uc.iso_o(role["lastUsedUtc"]),
        })

    platform_objects = []
    for name in sorted(per_platform, key=uc.sort_key_text):
        acc = per_platform[name]
        platform_objects.append({
            "platform": name, "requests": acc["requests"], "userMessages": acc["userMessages"],
            "assistantMessages": acc["assistantMessages"], "tokensIn": acc["tokensIn"], "tokensOut": acc["tokensOut"],
            "cacheRead": acc["cacheRead"], "cacheWrite": acc["cacheWrite"], "estCost": acc["estCost"],
            "costComplete": acc["costComplete"],
        })

    return {
        "coveredDays": float(covered_days),
        "totals": to_totals(totals),
        "burn": {"tokensPerDay": tokens_per_day, "costPerDay": cost_per_day, "costPerActiveHour": cost_per_active_hour},
        "perPlatform": platform_objects,
        "models": model_objects,
        "roles": role_objects,
    }


def write_stats_markdown(path, summary):
    lines = ["# Usage statistics", "", "Generated: " + summary["generatedUtc"], "Retention: %d day(s)" % summary["retentionDays"]]
    if summary["firstRecordUtc"]:
        lines.append("First record: " + summary["firstRecordUtc"])
    lines.append("")

    if summary["warnings"]:
        lines.append("## Warnings")
        lines += ["- " + warning for warning in summary["warnings"]]
        lines.append("")

    lines.append("## Platforms")
    for platform in summary["platforms"]:
        lines.append("    " + platform["platform"].ljust(12) + platform["status"].ljust(16) + (platform["lastActivityUtc"] or "n/a"))
    lines.append("")

    for name in ("24h", "7d", "30d"):
        window = summary["windows"][name]
        totals = window["totals"]
        burn = window["burn"]
        cost_suffix = "" if totals["costComplete"] else "+"
        lines.append("## " + name)
        lines.append("    requests %d | messages %d | in %s | out %s | cacheR %s | cacheW %s | est $%s%s" % (
            totals["requests"], totals["userMessages"] + totals["assistantMessages"],
            uc.format_tokens(totals["tokensIn"]), uc.format_tokens(totals["tokensOut"]),
            uc.format_tokens(totals["cacheRead"]), uc.format_tokens(totals["cacheWrite"]),
            uc.format_money(totals["estCost"]), cost_suffix))
        lines.append("    burn %s/day | $%s/day over %sd | $%s/active hour" % (
            uc.format_tokens(int(round(burn["tokensPerDay"]))), uc.format_money(burn["costPerDay"]),
            uc.fmt_fixed(window["coveredDays"], 1), uc.format_money(burn["costPerActiveHour"])))
        lines.append("    per platform:")
        for platform in window["perPlatform"]:
            suffix = "" if platform["costComplete"] else "+"
            lines.append("      " + platform["platform"].ljust(10) + "req " + str(platform["requests"]).ljust(6)
                         + "msg " + str(platform["userMessages"] + platform["assistantMessages"]).ljust(6)
                         + "in " + uc.format_tokens(platform["tokensIn"]).ljust(9) + "out " + uc.format_tokens(platform["tokensOut"]).ljust(9)
                         + "cacheR " + uc.format_tokens(platform["cacheRead"]).ljust(9) + "cacheW " + uc.format_tokens(platform["cacheWrite"]).ljust(9)
                         + "$" + uc.format_money(platform["estCost"]) + suffix)
        if window["models"]:
            lines.append("    models:")
            for model in window["models"]:
                cost_text = "$" + uc.format_money(model["estCost"]) if model["costPriced"] else "n/a cost"
                identity = model["platform"] + "/" + model["model"] + " [" + model["effort"] + "]"
                lines.append("      " + identity.ljust(42) + uc.ps_num(model["costSharePct"]).rjust(5) + "% cost | "
                             + uc.ps_num(model["tokenSharePct"]).rjust(5) + "% tokens | "
                             + uc.ps_num(model["requestSharePct"]).rjust(5) + "% calls | " + cost_text)
        if window["roles"]:
            lines.append("    roles:")
            for role in sorted(window["roles"], key=lambda r: r["estCost"], reverse=True)[:10]:
                lines.append("      " + (role["platform"] + "/" + role["scope"]).ljust(28) + "runs " + str(role["runs"]).ljust(5)
                             + "tokens " + uc.format_tokens(role["tokensIn"] + role["tokensOut"]).ljust(9)
                             + "est $" + uc.format_money(role["estCost"]).ljust(8) + "last " + role["lastUsedUtc"])
        lines.append("")

    uc.write_text(path, "\n".join(lines) + "\n")


def invoke_usage_stats(args):
    project_root = args.project_root
    if not os.path.isdir(os.path.join(project_root, ".agents")):
        print("usage-stats: .agents directory not found under " + project_root, file=sys.stderr)
        return 1

    usage_dir = uc.get_usage_dir(project_root)
    config = get_usage_config(usage_dir)
    if args.rebuild or args.migrate_v1:
        migration = uc.import_v1_history_to_v2(usage_dir, project_root)
        if not args.quiet and args.migrate_v1:
            print("usage-stats: v1 migration %s imported %d" % (migration["status"], migration["imported"]))
    if args.rebuild:
        v2 = uc.update_v2_current_session_view(usage_dir, project_root)
        if not args.quiet:
            print("usage-stats: v2 rebuild " + v2["status"])
    warnings = []
    codex_status = "disabled"
    if config["codexScanEnabled"] and not args.no_codex_scan:
        scan = uc.get_codex_rollout_records(project_root, usage_dir, SCRIPT_DIR, config["retentionDays"])
        codex_status = str(scan["status"])
        warnings += scan["warnings"]
    elif config["codexScanEnabled"]:
        codex_status = "no-data"

    history_path = os.path.join(usage_dir, "history.jsonl")
    history = read_history_records(history_path, config["retentionDays"])
    if history["hasExpired"]:
        prune_history(history_path, history, config["retentionDays"])
        history = read_history_records(history_path, config["retentionDays"])

    if history["invalid"] > 0:
        warnings.append("skipped %d invalid history line(s)" % history["invalid"])
    if history["unknown"] > 0:
        warnings.append("skipped %d history record(s) with unknown schema" % history["unknown"])

    unpriced = set()
    for item in history["records"]:
        for row in uc.as_list(item["record"].get("rows")):
            if uc.prop(row, "estCost") is None:
                unpriced.add(str(item["record"].get("platform", "unknown")) + "/" + str(uc.prop(row, "model", "unknown")))
    if unpriced:
        warnings.append("unpriced models excluded from cost: " + ", ".join(sorted(unpriced, key=uc.sort_key_text)))

    now = uc.utcnow()
    last = {name: uc.iso_o(ts) for name, ts in history["lastByPlatform"].items()}
    claude_status = "ok" if last.get("claude") else "no-data"
    gemini_status = "ok" if last.get("gemini") else "no-data"
    if last.get("codex") and codex_status not in ("disabled", "format-unknown"):
        codex_status = "ok"

    summary = {
        "v": 1,
        "generatedUtc": uc.iso_o(now),
        "retentionDays": config["retentionDays"],
        "firstRecordUtc": uc.iso_o(history["firstRecord"]) if history["firstRecord"] else None,
        "platforms": [
            {"platform": "claude", "status": claude_status, "lastActivityUtc": last.get("claude")},
            {"platform": "codex", "status": codex_status, "lastActivityUtc": last.get("codex")},
            {"platform": "gemini", "status": gemini_status, "lastActivityUtc": last.get("gemini")},
            {"platform": "antigravity", "status": "unsupported", "lastActivityUtc": None},
        ],
        "windows": {
            "24h": new_window_summary(history["records"], 1, now, history["firstRecord"]),
            "7d": new_window_summary(history["records"], 7, now, history["firstRecord"]),
            "30d": new_window_summary(history["records"], 30, now, history["firstRecord"]),
        },
        "warnings": warnings,
    }

    uc.write_json_atomic(os.path.join(usage_dir, "stats-summary.json"), summary)
    write_stats_markdown(os.path.join(usage_dir, "usage-stats.md"), summary)
    if not args.quiet:
        print("usage-stats: wrote " + os.path.join(usage_dir, "stats-summary.json"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent usage statistics aggregator.")
    parser.add_argument("--project-root", "-ProjectRoot", dest="project_root", default=uc.find_project_root())
    parser.add_argument("--no-codex-scan", "-NoCodexScan", dest="no_codex_scan", action="store_true")
    parser.add_argument("--rebuild", "-Rebuild", dest="rebuild", action="store_true")
    parser.add_argument("--migrate-v1", "-MigrateV1", dest="migrate_v1", action="store_true")
    parser.add_argument("--quiet", "-Quiet", dest="quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        return invoke_usage_stats(args)
    except Exception as exc:  # noqa: BLE001
        if not args.quiet:
            print("usage-stats: " + str(exc), file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
