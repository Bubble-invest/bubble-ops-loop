#!/usr/bin/env python3
"""Build offline department KPIs from dispatch ledgers and transcript evidence."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

try:
    from .nofollow_io import open_text
except ImportError:
    from nofollow_io import open_text

PARIS = ZoneInfo("Europe/Paris")
TOKEN_FIELDS = {
    "input_tokens": "input_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
    "output_tokens": "output_tokens",
}
MISSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def day(value: str) -> dt.date:
    parsed = dt.date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("day must be canonical YYYY-MM-DD")
    return parsed


def timestamp(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def atomic_write(path: Path, body: str) -> None:
    """Publish in the target directory, preserving an existing file's mode."""
    if path.is_symlink():
        raise ValueError("refusing a symlink output")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_source(path: Path, missing: set[str], name: str, expected: type,
                *, yaml_source: bool = False, not_configured: set[str] | None = None):
    try:
        with open_text(path) as stream:
            text = stream.read()
        value = yaml.safe_load(text) if yaml_source else json.loads(text)
        if not isinstance(value, expected):
            raise ValueError("unexpected source shape")
        return value
    except FileNotFoundError:
        (missing if not_configured is None else not_configured).add(name)
        return None
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        missing.add(name)
        return None


def read_calls(root: Path, missing: set[str]) -> list[dict] | None:
    """Read all JSONLs, including subagents; retain fullest usage per message id."""
    errors = []
    paths = []
    if not root.is_dir():
        missing.add("transcripts")
        return None
    for directory, dirs, files in os.walk(root, onerror=errors.append):
        dirs.sort()
        paths.extend(Path(directory) / name for name in sorted(files) if name.endswith(".jsonl"))
    if not paths or errors:
        missing.add("transcripts")
        return None
    calls = {}
    try:
        for path in sorted(paths):
            with open_text(path) as stream:
                for index, line in enumerate(stream):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("invalid transcript record")
                    if record.get("type") != "assistant":
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict) or not message.get("usage"):
                        continue
                    usage = message["usage"]
                    stamp = timestamp(record.get("timestamp"))
                    if not isinstance(usage, dict) or stamp is None:
                        raise ValueError("usage has no readable timestamp")
                    tokens = {key: usage.get(field, 0) or 0 for key, field in TOKEN_FIELDS.items()}
                    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0
                           for n in tokens.values()):
                        raise ValueError("invalid usage counter")
                    tokens["total_tokens"] = sum(tokens.values())
                    call = dict(timestamp=stamp, model=message.get("model", "unknown"), **tokens)
                    key = message.get("id") or record.get("uuid") or (str(path), index)
                    # Streaming/tool-content duplicates may carry partial usage first.
                    if key not in calls or call["total_tokens"] > calls[key]["total_tokens"]:
                        calls[key] = call
    except (OSError, UnicodeError, ValueError, TypeError):
        missing.add("transcripts")
        return None  # A partial transcript scan must not masquerade as a total.
    return list(calls.values())


def pricing_table() -> dict | None:
    """Import only the constant; never invoke pricing overrides or cache/report I/O."""
    path = Path(__file__).resolve().parents[2] / "console/services/cost_tracker.py"
    try:
        spec = importlib.util.spec_from_file_location("_fleet_cost_pricing", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._DEFAULT_PRICING
    except (OSError, ImportError, AttributeError):
        return None


def token_totals(calls: list[dict], pricing: dict | None) -> dict:
    totals = {key: sum(call[key] for call in calls) for key in (*TOKEN_FIELDS, "total_tokens")}
    totals["model_calls"] = len(calls)
    costs = []
    for call in calls:
        rates = next((rates for name, rates in (pricing or {}).items()
                      if name in str(call["model"]).lower()), None)
        if rates is None:
            return totals  # Unknown model is unpriced, never a free model call.
        costs.append(sum(call[key] * rates[rate] for key, rate in (
            ("input_tokens", "input"), ("output_tokens", "output"),
            ("cache_read_tokens", "cache_read"), ("cache_write_tokens", "cache_write"))) / 1e6)
    if pricing is not None:
        totals["cost_usd_estimate"] = round(math.fsum(costs), 2)
    return totals


def optional_metrics(mapping: dict, board: list | None, runs: list | None,
                     start: dt.date, end: dt.date) -> dict:
    """Reference semantics: first title match, UTC creation day, last equal-day run."""
    result = {}
    for mission, config in mapping.items():
        if not MISSION_ID.fullmatch(mission) or not isinstance(config, dict):
            raise ValueError("invalid mission KPI map")
        if set(config) != {"titles", "jobs"} or any(
            not isinstance(config[key], list) or any(not isinstance(v, str) or not v
                                                     for v in config[key])
            for key in ("titles", "jobs")
        ):
            raise ValueError("mission KPI map requires titles and jobs lists")
        for pattern in config["titles"]:
            re.compile(pattern, re.I)
        result[mission] = {}
    if board is not None:
        patterns = {m: [re.compile(p, re.I) for p in c["titles"]] for m, c in mapping.items()}
        for data in result.values():
            data.update(cards_created=0, cards_done=0, cards_dropped=0, cards_open=0, drop_rate=0.0)
        for issue in board:
            created = timestamp(issue["createdAt"])
            if created is None:
                raise ValueError("invalid board creation timestamp")
            if not start <= created.astimezone(dt.timezone.utc).date() <= end:
                continue
            mission = next((m for m, pats in patterns.items()
                            if any(p.search(issue["title"]) for p in pats)), None)
            if mission is None:
                continue
            data = result[mission]
            data["cards_created"] += 1
            if issue["state"] == "OPEN":
                data["cards_open"] += 1
            elif issue["state"] == "CLOSED":
                if issue.get("stateReason") == "COMPLETED":
                    data["cards_done"] += 1
                elif issue.get("stateReason") == "NOT_PLANNED":
                    data["cards_dropped"] += 1
        for data in result.values():
            if data["cards_created"]:
                data["drop_rate"] = round(data["cards_dropped"] / data["cards_created"], 3)
    if runs is not None:
        dated = [(day(run["date"]), run) for run in runs]
        for mission, config in mapping.items():
            matched = [(d, i, run) for i, (d, run) in enumerate(dated)
                       if start <= d <= end and run["job"] in config["jobs"]]
            costs = [run["cost_usd"] for _, _, run in matched]
            if any(isinstance(c, bool) or not isinstance(c, (int, float)) or
                   not math.isfinite(c) or c < 0 for c in costs):
                raise ValueError("invalid external run cost")
            last = max(matched, key=lambda item: (item[0], item[1]))[2] if matched else None
            result[mission].update(
                runs=len(matched),
                failures=sum(r["is_error"] is True or r["turns"] <= 1 for _, _, r in matched),
                cost_usd_total=round(math.fsum(costs), 2),
                cost_usd_last=round(last["cost_usd"], 2) if last else 0.0,
                cost_usd_max=round(max(costs), 2) if costs else 0.0,
            )
    return result


def build(dept_dir: Path, report_day: str, *, transcripts_dir: Path | None = None,
          runs_json: Path | None = None, board_json: Path | None = None,
          token_threshold: int = 30_000_000) -> dict:
    dept_dir = Path(dept_dir)
    end = day(report_day)
    start = end - dt.timedelta(days=27)
    if token_threshold < 1:
        raise ValueError("token threshold must be positive")
    missing = set()
    not_configured = set()
    manifest = read_source(dept_dir / "dept.yaml", missing, "dept.yaml", dict, yaml_source=True)
    entries = (manifest or {}).get("recurring_missions", [])
    if not isinstance(entries, list) or any(not isinstance(m, dict) or
            not isinstance(m.get("id"), str) or not MISSION_ID.fullmatch(m["id"]) for m in entries):
        missing.add("dept.yaml")
        entries = []
    missions = {m["id"]: {} for m in entries}
    records = []
    dispatch_complete = True
    dispatch_days_absent = []
    for offset in range(28):
        date = (start + dt.timedelta(days=offset)).isoformat()
        ledger = read_source(dept_dir / "outputs" / date / "dispatch.json",
                             missing, "dispatch", dict, not_configured=set())
        if ledger is None:
            dispatch_complete = False
            dispatch_days_absent.append(date)
            continue
        for mission, record in sorted(ledger.items()):
            if not MISSION_ID.fullmatch(mission) or not isinstance(record, dict):
                missing.add("dispatch")
                dispatch_complete = False
                continue
            missions.setdefault(mission, {})
            dispatched = timestamp(record.get("dispatched_at"))
            completed = timestamp(record.get("completed_at"))
            if (record.get("dispatched_at") and dispatched is None) or (
                record.get("completed_at") and (completed is None or dispatched and completed < dispatched)
            ):
                missing.add("dispatch")
                dispatch_complete = False
            if completed is not None and (
                completed.astimezone(PARIS).date() > end or dispatched and completed < dispatched
            ):
                completed = None
            if dispatched and start <= dispatched.astimezone(PARIS).date() <= end:
                records.append((mission, date, dispatched, completed, record))
    if len(dispatch_days_absent) == 28:
        missing.add("dispatch")
    calls = read_calls(transcripts_dir or Path(
        f"/home/agent-{dept_dir.name}/.claude/projects/-srv-agents-{dept_dir.name}"), missing)
    pricing = pricing_table()
    for mission, data in missions.items():
        matched = [r for r in records if r[0] == mission]
        if not matched and not dispatch_complete:
            continue  # No observation plus a missing ledger is not zero runs.
        data.update(days_dispatched=len({r[1] for r in matched}), runs_dispatched=len(matched),
                    runs_completed=sum(r[3] is not None for r in matched),
                    runs_incomplete=sum(r[3] is None for r in matched),
                    runs_unattributed=sum(r[3] is None or r[3] < r[2] for r in matched))
        artifacts = [r[4]["artifacts"] for r in matched if isinstance(r[4].get("artifacts"), list)]
        if artifacts:
            data["artifacts_count"] = sum(len(a) for a in artifacts)
        if calls is not None:
            windows = [(r[2], r[3]) for r in matched if r[3] is not None and r[3] >= r[2]]
            attributed = [c for c in calls if any(a <= c["timestamp"] <= b for a, b in windows)]
            for key, value in token_totals(attributed, pricing).items():
                if key.endswith("tokens"):
                    data[f"{key}_approx"] = value
                elif key == "cost_usd_estimate":
                    data["cost_usd_estimate_approx"] = value
    mapping = read_source(dept_dir / "config/mission_kpi_map.yaml", missing,
                          "mission_map", dict, yaml_source=True, not_configured=not_configured)
    runs = read_source(runs_json or dept_dir / "monitoring/runs.json", missing, "runs", list,
                       not_configured=not_configured)
    board = read_source(board_json or dept_dir / "monitoring/board-issues.json", missing, "board", list,
                        not_configured=not_configured)
    if mapping is not None:
        try:
            optional_metrics(mapping, None, None, start, end)
        except (ValueError, TypeError, KeyError, re.error):
            missing.add("mission_map")
            mapping = None
    if mapping is not None:
        # Validate each source separately so one bad optional input cannot hide the other.
        for name, source in (("board", board), ("runs", runs)):
            if source is None:
                continue
            try:
                extra = optional_metrics(mapping, source if name == "board" else None,
                                         source if name == "runs" else None, start, end)
                for mission, data in extra.items():
                    missions.setdefault(mission, {}).update(data)
            except (ValueError, TypeError, KeyError, re.error):
                missing.add(name)
    today_calls = [c for c in calls or [] if c["timestamp"].astimezone(PARIS).date() == end]
    today = token_totals(today_calls, pricing) if calls is not None else {}
    flat = dict(dispatch_days_present=28 - len(dispatch_days_absent),
                dispatch_days_absent=len(dispatch_days_absent))
    attention = []

    def flag(identifier, priority, summary):
        attention.append(dict(id=identifier, kind="mission_cost_quality", priority=priority, summary=summary))

    for mission, data in sorted(missions.items()):
        # Mission ids stay unchanged in keys; attention ids use the schema's kebab form.
        identifier = "kpi-" + re.sub(r"[^a-z0-9-]+", "-", mission.lower())
        flat.update({f"m_{mission}_{key}": value for key, value in data.items()})
        if data.get("runs_incomplete", 0) >= 2:
            flag(f"{identifier}-incomplete-runs", "high",
                 f"{mission}: {data['runs_incomplete']} recorded dispatches lack completion evidence in 28 days")
        if data.get("cards_created", 0) >= 10 and data["drop_rate"] >= 0.4:
            flag(f"{identifier}-low-yield", "medium",
                 f"{mission}: {data['cards_dropped']} of {data['cards_created']} cards closed without action in 28 days")
    if today:
        flat.update(tokens_today_total_mtok=round(today["total_tokens"] / 1e6, 1),
                    tokens_today_output_mtok=round(today["output_tokens"] / 1e6, 1),
                    model_calls_today=today["model_calls"])
        if today["total_tokens"] >= token_threshold:
            flag("kpi-dept-token-use", "medium",
                 f"Department used {today['total_tokens'] / 1e6:.1f} million tokens "
                 f"across {today['model_calls']} model calls on {report_day}")
    flat["mission_kpi_sources_missing"] = len(missing)
    if missing:
        flag("kpi-sources-missing", "low", "KPI sources unreadable or absent: " + ", ".join(sorted(missing)))
    return dict(missions=dict(sorted(missions.items())), dept_tokens_today=today,
                top_kpis_flat=flat, attention=attention, sources_missing=sorted(missing),
                sources_not_configured=sorted(not_configured),
                dispatch_days_absent_list=dispatch_days_absent,
                notes=["Days and transcript totals use Europe/Paris; optional board creation dates use UTC.",
                       "Dispatch counts cover surviving ledger records; overwritten runs and uncommitted crashes are unobservable.",
                       "Mission token and cost windows are approximate and may overlap across missions.",
                       "Dollar estimates use the console pricing constant; unpriced or unreadable usage has no dollar figure."])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dept-dir", required=True, type=Path)
    parser.add_argument("--day", required=True)
    parser.add_argument("--transcripts-dir", type=Path)
    parser.add_argument("--runs-json", type=Path)
    parser.add_argument("--board-json", type=Path)
    parser.add_argument("--token-threshold", type=int, default=30_000_000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        doc = build(args.dept_dir, args.day, transcripts_dir=args.transcripts_dir,
                    runs_json=args.runs_json, board_json=args.board_json, token_threshold=args.token_threshold)
        body = json.dumps(doc, indent=2, allow_nan=False) + "\n"
        if args.out:
            atomic_write(args.out, body)
        else:
            print(body, end="")
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
