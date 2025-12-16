import json
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _fmt_pretty_date(iso: str) -> str:
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    day = d.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    month = d.strftime("%B")
    return f"{month} {day}{suffix}, {d.year}"


def _fmt_folder_name(project: str, date_iso: str) -> str:
    d = dt.datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    month = d.strftime("%B")
    day = d.day
    year = d.year
    return f"{project}, {month} {day}, {year}"


def generate_reports(
    scores: Dict[str, Any],
    outdir: str,
    org: str,
    project: str,
    since_iso: str,
    until_iso: str,
    named: bool = True,
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

    json_path = report_dir / f"qa-skill-scores-{tag}.json"
    json_path.write_text(json.dumps(scores, indent=2), encoding="utf-8")

    people: List[Dict[str, Any]] = scores.get("people", [])
    md_path = report_dir / f"qa-skill-assessment-{tag}.md"
    team = scores.get("team", {})
    lines = []
    lines.append(f"# 90-Day Azure DevOps QA Assessment\n")
    lines.append(f"Organization: {org}  ")
    lines.append(f"Project: {project}  ")
    lines.append(f"Window: { _fmt_pretty_date(since_iso) } → { _fmt_pretty_date(until_iso) }  ")
    lines.append(f"Team Score: {team.get('score', 0.0)}  ")
    lines.append("")
    lines.append("## Scoring overview")
    lines.append("- Testing (30%): executed test cases, pass rate.")
    lines.append("- Defects (30%): valid bug ratio, severity weighting, reopen rate.")
    lines.append("- Hygiene (20%): repro/attachments plus structured steps (in description/comments) and linkage.")
    lines.append("- Responsiveness (20%): bug closure time, time to first QA comment, time-in-QA states.")
    lines.append("")
    lines.append("Scores normalized per-metric and combined into a 0–100 composite.")
    lines.append("")
    lines.append("## Per-QA Scores (named)" if named else "## Per-QA Scores (anonymized)")
    lines.append("")
    lines.append("| QA | Score | Testing | Defects | Hygiene | Responsiveness |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for p in people:
        qa_name = p.get("person") if named else "qa-***"
        subs = p.get("subscores", {})
        lines.append(
            f"| {qa_name} | {p.get('score')} | {subs.get('testing')} | {subs.get('defects')} | {subs.get('hygiene')} | {subs.get('responsiveness')} |"
        )
    lines.append("")
    lines.append("### Notes")
    lines.append("- Metrics are proxies; interpret alongside context.")
    lines.append("- Some fields depend on project conventions (severity, repro steps fields).")
    lines.append("- Weights: testing 30%, defects 30%, hygiene 20%, responsiveness 20%.")

    md_path.write_text("\n".join(lines), encoding="utf-8")


