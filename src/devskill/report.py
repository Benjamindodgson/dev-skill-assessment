import csv
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


def generate_reports(
    scores: Dict[str, Any],
    outdir: str,
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    named: bool = True,
    tag: Optional[str] = None,
) -> None:
    out = Path(outdir)
    _ensure_dir(out)

    # compute default tag if not provided
    if not tag:
        now = dt.datetime.utcnow()
        tag = f"{owner}-{repo}-{now:%Y-%m-%d}"

    # JSON (already aggregated)
    json_path = out / f"dev-skill-scores-{tag}.json"
    json_path.write_text(json.dumps(scores, indent=2), encoding="utf-8")

    # CSV (flat per-developer)
    csv_path = out / f"dev-skill-assessment-{tag}.csv"
    devs: List[Dict[str, Any]] = scores.get("developers", [])
    fieldnames = [
        "developer",
        "score",
        "delivery",
        "collaboration",
        "hygiene",
        "stability",
        "delivery.merged_prs",
        "delivery.lead_time_median_h",
        "delivery.merge_rate",
        "collab.reviews_written",
        "collab.review_comments",
        "collab.unique_people_reviewed",
        "collab.tffr_median_h",
        "hygiene.pr_size_median",
        "hygiene.small_pr_pct",
        "hygiene.draft_rate",
        "stability.change_requests",
        "stability.reopened_prs",
        "stability.fix48",
        "activity.commits",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for d in devs:
            w.writerow({
                **{k: d.get(k) for k in fieldnames if "." in k or k in {"developer", "score"}},
                "delivery": d.get("subscores", {}).get("delivery"),
                "collaboration": d.get("subscores", {}).get("collaboration"),
                "hygiene": d.get("subscores", {}).get("hygiene"),
                "stability": d.get("subscores", {}).get("stability"),
            })

    # Markdown summary
    md_path = out / f"dev-skill-assessment-{tag}.md"
    team = scores.get("team", {})
    lines = []
    lines.append(f"# 90-Day GitHub Dev Assessment\n")
    lines.append(f"Repository: {owner}/{repo}  ")
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


