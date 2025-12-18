import json
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _fmt_pretty_date(iso: str) -> str:
    # Format: April 7th, 2025
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    day = d.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    month = d.strftime("%B")
    return f"{month} {day}{suffix}, {d.year}"


def _inclusive_day_count(start_iso: str, end_iso: str) -> int:
    """Return the inclusive day span between two ISO timestamps."""
    start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc).date()
    end = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc).date()
    delta = (end - start).days + 1
    return max(delta, 1)


def _fmt_folder_name(repo: str, date_iso: str) -> str:
    """Format folder name as: RepoName, Month Day, Year"""
    d = dt.datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    month = d.strftime("%B")  # Full month name
    day = d.day
    year = d.year
    return f"{repo}, {month} {day}, {year}"


def _github_repo_link(owner: str, repo: str) -> str:
    """Returns markdown link for GitHub repository."""
    return f"[{owner}/{repo}](https://github.com/{owner}/{repo})"


def generate_reports(
    scores: Dict[str, Any],
    outdir: str,
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    named: bool = True,
    tag: Optional[str] = None,
    repos: Optional[List[Dict[str, Any]]] = None,
) -> None:
    out = Path(outdir)
    _ensure_dir(out)

    # compute default tag if not provided
    if not tag:
        now = dt.datetime.utcnow()
        tag = f"{owner}-{repo}-{now:%Y-%m-%d}"

    # Create dated subfolder
    folder_name = _fmt_folder_name(repo, until_iso)
    report_dir = out / folder_name
    _ensure_dir(report_dir)

    # JSON (already aggregated)
    json_path = report_dir / f"dev-skill-scores-{tag}.json"
    json_path.write_text(json.dumps(scores, indent=2), encoding="utf-8")

    # Markdown summary
    devs: List[Dict[str, Any]] = scores.get("developers", [])
    md_path = report_dir / f"dev-skill-assessment-{tag}.md"
    team = scores.get("team", {})
    lines = []
    day_count = _inclusive_day_count(since_iso, until_iso)
    lines.append(f"# {day_count}-Day GitHub Dev Assessment\n")
    if repos:
        repo_list = ", ".join(f"{r.get('owner')}/{r.get('repo')}" for r in repos if r.get("owner") and r.get("repo"))
        repo_display = repo_list or f"{owner}/{repo}"
        lines.append(f"Repositories: {repo_display}  ")
    else:
        lines.append(f"Repository: {_github_repo_link(owner, repo)}  ")
    lines.append(f"Window: { _fmt_pretty_date(since_iso) } → { _fmt_pretty_date(until_iso) }  ")
    lines.append(f"Team Score: {team.get('score', 0.0)}  ")
    lines.append("")
    # Scoring overview and relevance
    lines.append("## Scoring overview")
    lines.append("- Delivery (30%): merged PRs, median lead time, merge rate.")
    lines.append("- Collaboration (25%): reviews, review comments, unique teammates, time-to-first-review.")
    lines.append("- Hygiene (15%): PR size, % small PRs, draft usage.")
    lines.append("- Stability (30%): change requests, reopened PRs, quick fix-forward.")
    lines.append("")
    lines.append("Scores are normalized per-metric (winsorized min–max) and combined into a 0–100 composite.")
    lines.append("These proxies reflect throughput, collaboration, reviewability, and rework risk within the window.")
    lines.append("")
    lines.append("## Per-Developer Scores (named)" if named else "## Per-Developer Scores (anonymized)")
    lines.append("")
    lines.append("| Developer | Score | Delivery | Collaboration | Hygiene | Stability |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for d in devs:
        dev_name = d.get("developer") if named else "dev-***"
        subs = d.get("subscores", {})
        lines.append(
            f"| {dev_name} | {d.get('score')} | {subs.get('delivery')} | {subs.get('collaboration')} | {subs.get('hygiene')} | {subs.get('stability')} |"
        )
    lines.append("")
    lines.append("### Notes")
    lines.append("- Metrics are proxies; interpret alongside context.")
    lines.append("- Weekends excluded from review responsiveness metrics.")
    lines.append("- Weights: delivery 30%, collaboration 25%, hygiene 15%, stability 30%.")

    md_path.write_text("\n".join(lines), encoding="utf-8")


