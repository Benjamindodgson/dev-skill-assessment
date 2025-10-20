import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    n = len(ys)
    m = n // 2
    if n % 2 == 1:
        return ys[m]
    return 0.5 * (ys[m - 1] + ys[m])


def _parse_iso(s: str) -> dt.datetime:
    if not s:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    s = s.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s)


def _fmt_pretty_date(iso: str) -> str:
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    day = d.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{d.strftime('%B')} {day}{suffix}, {d.year}"


def _fmt_folder_name(project: str, date_iso: str) -> str:
    d = dt.datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    month = d.strftime("%B")
    day = d.day
    year = d.year
    return f"{project}, {month} {day}, {year}"


def _pick_strength_area(subscores: Dict[str, float]) -> str:
    return max(subscores.items(), key=lambda kv: kv[1])[0]


def _pick_improvement_area(subscores: Dict[str, float]) -> str:
    return min(subscores.items(), key=lambda kv: kv[1])[0]


def _improvement_tip(area: str, row: Dict[str, Any], team_medians: Dict[str, float]) -> str:
    if area == "testing":
        you = float(row.get("testing.pass_rate", 0.0))
        team = float(team_medians.get("testing.pass_rate", 0.0))
        return f"Increase pass rate (you {you:.0%}, team {team:.0%}). Pair with devs to clarify acceptance.")
    if area == "defects":
        you = float(row.get("defects.valid_ratio", 0.0))
        team = float(team_medians.get("defects.valid_ratio", 0.0))
        return f"Raise valid bug ratio (you {you:.0%}, team {team:.0%}). Tighten repro steps and triage severity."
    if area == "hygiene":
        you = float(row.get("hygiene.score", 0.0))
        team = float(team_medians.get("hygiene.score", 0.0))
        return f"Improve hygiene (you {you:.0%}, team {team:.0%}). Ensure repro steps and attachments."
    if area == "responsiveness":
        you = float(row.get("responsiveness.close_median_h", 0.0))
        team = float(team_medians.get("responsiveness.close_median_h", 0.0))
        return f"Reduce closure time (you {you:.1f}h, team {team:.1f}h). Nudge assignees and pre-validate."
    return "Focus on consistent incremental improvements."


def _team_medians(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    keys = [
        "testing.pass_rate",
        "defects.valid_ratio",
        "hygiene.score",
        "responsiveness.close_median_h",
    ]
    out: Dict[str, float] = {}
    for k in keys:
        out[k] = _median([float(r.get(k, 0.0)) for r in rows]) if rows else 0.0
    return out


def generate_insights(
    scores: Dict[str, Any],
    data: Dict[str, Any],
    outdir: str,
    org: str,
    project: str,
    since_iso: str,
    until_iso: str,
    tag: Optional[str] = None,
) -> None:
    out = Path(outdir)
    _ensure_dir(out)

    if not tag:
        now = dt.datetime.utcnow()
        tag = f"{org}-{project}-{now:%Y-%m-%d}"

    folder_name = _fmt_folder_name(project, until_iso)
    report_dir = out / folder_name
    _ensure_dir(report_dir)

    rows: List[Dict[str, Any]] = scores.get("people", [])
    team_meds = _team_medians(rows)

    md_path = report_dir / f"qa-skill-assessment-insights-{tag}.md"

    lines: List[str] = []
    lines.append("# 90-Day QA Assessment: Strengths & Improvements")
    lines.append(f"Organization: {org}  ")
    lines.append(f"Project: {project}  ")
    lines.append(f"Window: { _fmt_pretty_date(since_iso) } → { _fmt_pretty_date(until_iso) }")
    lines.append("")

    for r in rows:
        name = r.get("person", "unknown")
        subs = r.get("subscores", {})
        # Map subscores into the same keys used by metrics rows
        row_view = {
            "testing.pass_rate": float(r.get("testing.pass_rate", 0.0)),
            "defects.valid_ratio": float(r.get("defects.valid_ratio", 0.0)),
            "hygiene.score": float(r.get("hygiene.score", 0.0)),
            "responsiveness.close_median_h": float(r.get("responsiveness.close_median_h", 0.0)),
        }
        strength = max(subs, key=lambda k: subs[k]) if subs else "testing"
        improvement = min(subs, key=lambda k: subs[k]) if subs else "responsiveness"
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- Strength: {strength.capitalize()}  ")
        lines.append("  Examples:")
        if strength == "testing":
            lines.append("  - High pass rate with consistent execution volume.")
        elif strength == "defects":
            lines.append("  - Valid, high-severity issues identified with clear triage.")
        elif strength == "hygiene":
            lines.append("  - Bugs include reproducible steps and helpful attachments.")
        elif strength == "responsiveness":
            lines.append("  - Fast bug lifecycle closure.")
        lines.append(f"- Improvement: {improvement.capitalize()}  ")
        lines.append("  Suggestions:")
        lines.append(f"  - {_improvement_tip(improvement, row_view, team_meds)}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")


