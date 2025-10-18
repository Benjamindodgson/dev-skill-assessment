import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional


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


def _pick_strength_area(subscores: Dict[str, float]) -> str:
    return max(subscores.items(), key=lambda kv: kv[1])[0]


def _pick_improvement_area(subscores: Dict[str, float]) -> str:
    return min(subscores.items(), key=lambda kv: kv[1])[0]


def _strength_example(area: str, dev_login: str, data: Dict[str, Any], small_pr_threshold: float) -> str:
    prs = data.get("pull_requests", [])
    # Delivery: fastest merged PR
    if area == "delivery":
        best = None
        best_hours = None
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
                continue
            created = _parse_iso(pr.get("createdAt", ""))
            merged = _parse_iso(pr.get("mergedAt", ""))
            hours = (merged - created).total_seconds() / 3600.0
            if best_hours is None or hours < best_hours:
                best_hours = hours
                best = pr
        if best is not None and best_hours is not None:
            size = (best.get("additions", 0) or 0) + (best.get("deletions", 0) or 0)
            return f"PR #{best.get('number')} merged in {_fmt_duration_hm(best_hours)} (±{int(size)} lines)."
        return "Merged PR with quick turnaround."

    # Collaboration: earliest review on others' PRs
    if area == "collaboration":
        best = None
        best_delta = None
        for pr in prs:
            if (pr.get("author") or {}).get("login") == dev_login:
                continue
            created = _parse_iso(pr.get("createdAt", ""))
            for rv in (pr.get("reviews") or {}).get("nodes", []):
                if (rv.get("author") or {}).get("login") != dev_login:
                    continue
                submitted = _parse_iso(rv.get("submittedAt", ""))
                delta = (submitted - created).total_seconds() / 3600.0
                if delta < 0:
                    continue
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = pr
        if best is not None and best_delta is not None:
            author = (best.get("author") or {}).get("login", "")
            return f"Reviewed PR #{best.get('number')} from @{author} within {_fmt_duration_hm(best_delta)} of creation."
        return "Provided timely peer review."

    # Hygiene: small PR example
    if area == "hygiene":
        best = None
        best_size = None
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            size = (pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0)
            if best_size is None or size < best_size:
                best_size = size
                best = pr
        if best is not None and best_size is not None:
            return f"PR #{best.get('number')} was a focused change (±{int(best_size)} lines)."
        return "Submitted focused, reviewable PRs."

    # Stability: PR merged without change requests (proxy for initial quality)
    if area == "stability":
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            if pr.get("state") != "MERGED":
                continue
            reviews = (pr.get("reviews") or {}).get("nodes", [])
            if not any(rv.get("state") == "CHANGES_REQUESTED" for rv in reviews):
                return f"PR #{pr.get('number')} merged with 0 change requests."
        return "PRs merged with minimal rework."

    return "Consistent performance."


def _fmt_duration_hm(hours: float) -> str:
    if hours < 0:
        hours = 0
    total_minutes = int(round(hours * 60))
    h = total_minutes // 60
    m = total_minutes % 60
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    if h > 0:
        return f"{h}h"
    return f"{m}m"


def _fmt_pretty_date(iso: str) -> str:
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    day = d.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{d.strftime('%B')} {day}{suffix}, {d.year}"


def _improvement_tip(area: str, dev_row: Dict[str, Any], team_medians: Dict[str, float], small_pr_threshold: float) -> str:
    if area == "delivery":
        lead_h = float(dev_row.get("delivery.lead_time_median_h", 0.0))
        team_h = float(team_medians.get("delivery.lead_time_median_h", 0.0))
        return f"Reduce PR lead time (median {_fmt_duration_hm(lead_h)} vs team {_fmt_duration_hm(team_h)}). Break work into smaller increments and request reviews earlier."
    if area == "collaboration":
        reviews = float(dev_row.get("collab.reviews_written", 0.0))
        team = team_medians.get("collab.reviews_written", 0.0)
        return f"Increase peer reviews (you wrote {reviews:.0f} vs team {team:.0f}). Aim to review 1–2 PRs/day."
    if area == "hygiene":
        size = float(dev_row.get("hygiene.pr_size_median", 0.0))
        team = team_medians.get("hygiene.pr_size_median", 0.0)
        return f"Target smaller PRs (median ±{int(size)} lines vs team ±{int(team)}). Try ≤{int(small_pr_threshold)} lines per PR."
    if area == "stability":
        cr = float(dev_row.get("stability.change_requests", 0.0))
        team = team_medians.get("stability.change_requests", 0.0)
        return f"Reduce change requests (you had {cr:.0f} vs team {team:.0f}). Add pre-review checklists and run tests locally."
    return "Focus on consistent incremental improvements."


def _strength_examples(area: str, dev_login: str, data: Dict[str, Any], small_pr_threshold: float) -> List[str]:
    prs = data.get("pull_requests", [])
    examples: List[str] = []

    if area == "delivery":
        merged: List[Tuple[float, Dict[str, Any]]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
                continue
            created = _parse_iso(pr.get("createdAt", ""))
            merged_at = _parse_iso(pr.get("mergedAt", ""))
            hours = (merged_at - created).total_seconds() / 3600.0
            merged.append((hours, pr))
        for hours, pr in sorted(merged, key=lambda t: t[0])[:3]:
            size = (pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0)
            examples.append(f"PR #{pr.get('number')} merged in {_fmt_duration_hm(hours)} (±{int(size)} lines).")
        return examples

    if area == "collaboration":
        reviews: List[Tuple[float, Dict[str, Any]]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") == dev_login:
                continue
            created = _parse_iso(pr.get("createdAt", ""))
            for rv in (pr.get("reviews") or {}).get("nodes", []):
                if (rv.get("author") or {}).get("login") != dev_login:
                    continue
                submitted = _parse_iso(rv.get("submittedAt", ""))
                delta = (submitted - created).total_seconds() / 3600.0
                if delta >= 0:
                    reviews.append((delta, pr))
        for delta, pr in sorted(reviews, key=lambda t: t[0])[:3]:
            author = (pr.get("author") or {}).get("login", "")
            examples.append(f"Reviewed PR #{pr.get('number')} from @{author} within {_fmt_duration_hm(delta)}.")
        if not examples:
            examples.append("No recorded reviews in window; collaboration visible via other channels may not be captured.")
        return examples

    if area == "hygiene":
        sizes: List[Tuple[float, Dict[str, Any]]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            size = float((pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0))
            sizes.append((size, pr))
        for size, pr in sorted(sizes, key=lambda t: t[0])[:3]:
            examples.append(f"PR #{pr.get('number')} was focused (±{int(size)} lines).")
        return examples

    if area == "stability":
        zero_cr: List[Dict[str, Any]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            if pr.get("state") != "MERGED":
                continue
            reviews = (pr.get("reviews") or {}).get("nodes", [])
            if not any(rv.get("state") == "CHANGES_REQUESTED" for rv in reviews):
                zero_cr.append(pr)
        for pr in zero_cr[:3]:
            examples.append(f"PR #{pr.get('number')} merged with 0 change requests.")
        if not examples:
            examples.append("Merged PRs generally required minimal rework.")
        return examples

    return ["Consistent performance across areas."]


def _improvement_examples(area: str, dev_login: str, data: Dict[str, Any], small_pr_threshold: float) -> List[str]:
    prs = data.get("pull_requests", [])
    examples: List[str] = []

    if area == "delivery":
        merged: List[Tuple[float, Dict[str, Any]]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
                continue
            created = _parse_iso(pr.get("createdAt", ""))
            merged_at = _parse_iso(pr.get("mergedAt", ""))
            hours = (merged_at - created).total_seconds() / 3600.0
            merged.append((hours, pr))
        for hours, pr in sorted(merged, key=lambda t: t[0], reverse=True)[:3]:
            size = (pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0)
            examples.append(f"PR #{pr.get('number')} took {_fmt_duration_hm(hours)} to merge (±{int(size)} lines). Consider breaking into smaller parts.")
        if not examples:
            examples.append("No merged PRs to analyze; consider smaller, more frequent PRs to improve delivery cadence.")
        return examples

    if area == "collaboration":
        reviews: List[Tuple[float, Dict[str, Any]]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") == dev_login:
                continue
            created = _parse_iso(pr.get("createdAt", ""))
            for rv in (pr.get("reviews") or {}).get("nodes", []):
                if (rv.get("author") or {}).get("login") != dev_login:
                    continue
                submitted = _parse_iso(rv.get("submittedAt", ""))
                delta = (submitted - created).total_seconds() / 3600.0
                if delta >= 0:
                    reviews.append((delta, pr))
        for delta, pr in sorted(reviews, key=lambda t: t[0], reverse=True)[:3]:
            author = (pr.get("author") or {}).get("login", "")
            examples.append(f"Reviewed PR #{pr.get('number')} from @{author} after {_fmt_duration_hm(delta)}; aim for <24h.")
        if not examples:
            examples.append("No recorded peer reviews; start by reviewing 1–2 teammate PRs daily.")
            examples.append("Enable notifications for review requests to improve responsiveness.")
            examples.append("Pick small PRs to review first to build cadence.")
        return examples

    if area == "hygiene":
        sizes: List[Tuple[float, Dict[str, Any]]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            size = float((pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0))
            sizes.append((size, pr))
        for size, pr in sorted(sizes, key=lambda t: t[0], reverse=True)[:3]:
            examples.append(f"PR #{pr.get('number')} was large (±{int(size)} lines). Split along feature boundaries.")
        if not examples:
            examples.append("No authored PRs; create smaller, focused PRs to improve reviewability.")
        return examples

    if area == "stability":
        with_cr: List[Dict[str, Any]] = []
        for pr in prs:
            if (pr.get("author") or {}).get("login") != dev_login:
                continue
            reviews = (pr.get("reviews") or {}).get("nodes", [])
            if any(rv.get("state") == "CHANGES_REQUESTED" for rv in reviews):
                with_cr.append(pr)
        for pr in with_cr[:3]:
            examples.append(f"PR #{pr.get('number')} received change requests; add pre-review checks and tests.")
        if not examples:
            examples.append("No change-requested PRs found; ensure tests and checklists to maintain quality.")
        return examples

    return ["Focus on consistent incremental improvements."]


def _team_medians(devs: List[Dict[str, Any]]) -> Dict[str, float]:
    keys = [
        "delivery.lead_time_median_h",
        "collab.reviews_written",
        "hygiene.pr_size_median",
        "stability.change_requests",
    ]
    out: Dict[str, float] = {}
    for k in keys:
        out[k] = _median([float(d.get(k, 0.0)) for d in devs]) if devs else 0.0
    return out


def generate_insights(
    scores: Dict[str, Any],
    data: Dict[str, Any],
    outdir: str,
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    small_pr_threshold: float = 300.0,
    tag: Optional[str] = None,
) -> None:
    out = Path(outdir)
    _ensure_dir(out)

    # compute default tag if not provided
    if not tag:
        now = dt.datetime.utcnow()
        tag = f"{owner}-{repo}-{now:%Y-%m-%d}"

    devs: List[Dict[str, Any]] = scores.get("developers", [])
    team_meds = _team_medians(devs)

    md_path = out / f"dev-skill-assessment-insights-{tag}.md"

    lines: List[str] = []
    lines.append("# 90-Day Assessment: Strengths & Improvements")
    lines.append(f"Repository: {owner}/{repo}  ")
    lines.append(f"Window: { _fmt_pretty_date(since_iso) } → { _fmt_pretty_date(until_iso) }")
    lines.append("")

    for d in devs:
        name = d.get("developer", "unknown")
        subs = d.get("subscores", {})
        strength = _pick_strength_area(subs)
        improvement = _pick_improvement_area(subs)
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- Strength: {strength.capitalize()}  ")
        lines.append("  Examples:")
        for ex in _strength_examples(strength, name, data, small_pr_threshold)[:3]:
            lines.append(f"  - {ex}")
        lines.append(f"- Improvement: {improvement.capitalize()}  ")
        lines.append("  Examples:")
        for ex in _improvement_examples(improvement, name, data, small_pr_threshold)[:3]:
            lines.append(f"  - {ex}")
        lines.append(f"  Tip: {_improvement_tip(improvement, d, team_meds, small_pr_threshold)}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")


