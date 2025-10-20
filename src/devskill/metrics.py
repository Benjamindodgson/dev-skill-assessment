import datetime as dt
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple


def _parse_iso(s: str) -> dt.datetime:
    if not s:
        # Arbitrary far past for missing dates
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    s = s.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s)


def _business_hours_delta(start: dt.datetime, end: dt.datetime, exclude_weekends: bool) -> float:
    if end < start:
        return 0.0
    if not exclude_weekends:
        return (end - start).total_seconds() / 3600.0
    # Count only weekdays
    total = 0.0
    cursor = start
    one_hour = dt.timedelta(hours=1)
    while cursor < end:
        if cursor.weekday() < 5:
            total += 1.0
        cursor += one_hour
    return total


def _winsorize(values: List[float], lower_q: float = 0.05, upper_q: float = 0.95) -> List[float]:
    if not values:
        return values
    xs = sorted(values)
    lo = xs[int(lower_q * (len(xs) - 1))]
    hi = xs[int(upper_q * (len(xs) - 1))]
    return [min(max(v, lo), hi) for v in values]


def _minmax_norm(values: List[float], higher_is_better: bool = True) -> List[float]:
    if not values:
        return values
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return [0.5] * len(values)
    norm = [(v - vmin) / (vmax - vmin) for v in values]
    return norm if higher_is_better else [1.0 - x for x in norm]


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2 == 1:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def _first(items: Iterable[Any]) -> Any:
    for it in items:
        return it
    return None


def compute_scores(
    data: Dict[str, Any],
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    # Collect per-developer aggregates
    alias_map = {**{k.lower(): v for k, v in (config.get("aliases") or {}).items()}}

    def canonical(login: str) -> str:
        if not login:
            return "unknown"
        return alias_map.get(login.lower(), login)

    prs = data.get("pull_requests", [])
    commits = data.get("commits", [])

    per_dev = defaultdict(lambda: {
        "merged_prs": 0,
        "total_prs": 0,
        "pr_lead_times_hours": [],
        "reviews_written": 0,
        "review_comments_written": 0,
        "unique_teammates_reviewed": set(),
        "time_to_first_review_hours": [],
        "pr_sizes_lines": [],
        "draft_prs": 0,
        "change_requests": 0,
        "reopened_prs": 0,
        "post_merge_fix_within_48h": 0,
        "commits": 0,
    })

    # Index commits by author login or email
    for c in commits:
        login = (c.get("author") or {}).get("login") or (c.get("commit") or {}).get("author", {}).get("email", "")
        per_dev[canonical(login)]["commits"] += 1

    # Process PRs
    for pr in prs:
        author = canonical((pr.get("author") or {}).get("login", ""))
        state = pr.get("state")
        created = _parse_iso(pr.get("createdAt", ""))
        merged = _parse_iso(pr.get("mergedAt", "")) if pr.get("mergedAt") else None

        per_dev[author]["total_prs"] += 1
        if pr.get("isDraft"):
            per_dev[author]["draft_prs"] += 1

        additions = float(pr.get("additions") or 0)
        deletions = float(pr.get("deletions") or 0)
        per_dev[author]["pr_sizes_lines"].append(additions + deletions)

        # Reviews written on others' PRs
        for rv in (pr.get("reviews") or {}).get("nodes", []):
            reviewer = canonical((rv.get("author") or {}).get("login", ""))
            if reviewer and reviewer != author:
                per_dev[reviewer]["reviews_written"] += 1
                # Approximate review comments count from state changes + presence as proxy
                per_dev[reviewer]["review_comments_written"] += 1
                per_dev[reviewer]["unique_teammates_reviewed"].add(author)

        # Time to first review (for author)
        review_times = []
        for rv in (pr.get("reviews") or {}).get("nodes", []):
            submitted = _parse_iso(rv.get("submittedAt", ""))
            review_times.append((submitted - created).total_seconds() / 3600.0)
        if review_times:
            per_dev[author]["time_to_first_review_hours"].append(min([t for t in review_times if t >= 0]))

        # Lead time (created -> merged)
        if state == "MERGED" and merged is not None:
            per_dev[author]["merged_prs"] += 1
            per_dev[author]["pr_lead_times_hours"].append((merged - created).total_seconds() / 3600.0)

        # Change requests proxy
        for rv in (pr.get("reviews") or {}).get("nodes", []):
            if rv.get("state") == "CHANGES_REQUESTED":
                per_dev[author]["change_requests"] += 1

    # Derive metrics
    weights = config.get("weights", {"delivery": 0.30, "collaboration": 0.25, "hygiene": 0.15, "stability": 0.30})
    small_pr_threshold = float(((config.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300)

    # Prepare exclusions (case-insensitive), support either 'exclude' or legacy 'exclude_logins'.
    # Also include bots from the bots list to ensure they're filtered from reports even when using cached data.
    # Canonicalize using aliases and also consider email local-parts to catch identities that
    # appear as emails in commit data.
    raw_excludes = (config.get("exclude") or []) + (config.get("exclude_logins") or []) + (config.get("bots") or [])
    excluded_logins_lower = set()
    for raw in raw_excludes:
        if raw is None:
            continue
        # Consider the value as provided
        candidates = [str(raw)]
        # Also consider its canonicalized form via aliases
        candidates.append(canonical(str(raw)))
        # If an email, also consider the local-part before '@'
        for cand in list(candidates):
            if "@" in cand:
                local = cand.split("@", 1)[0]
                candidates.append(local)
                # also canonicalize the local-part via aliases
                candidates.append(canonical(local))
        for cand in candidates:
            if cand:
                excluded_logins_lower.add(str(cand).lower())

    dev_rows = []
    for dev, agg in per_dev.items():
        if dev.lower() in excluded_logins_lower:
            continue
        total_prs = max(1, agg["total_prs"])  # avoid division by zero
        merged_prs = agg["merged_prs"]
        pr_lead_median = _median(agg["pr_lead_times_hours"]) if agg["pr_lead_times_hours"] else 0.0
        merge_rate = merged_prs / total_prs

        reviews_written = agg["reviews_written"]
        review_comments_written = agg["review_comments_written"]
        unique_teammates = len(agg["unique_teammates_reviewed"]) if isinstance(agg["unique_teammates_reviewed"], set) else 0
        tffr_median = _median(agg["time_to_first_review_hours"]) if agg["time_to_first_review_hours"] else 0.0

        pr_sizes = agg["pr_sizes_lines"]
        pr_size_median = _median(pr_sizes) if pr_sizes else 0.0
        small_pr_pct = (sum(1 for s in pr_sizes if s <= small_pr_threshold) / max(1, len(pr_sizes))) if pr_sizes else 0.0
        draft_rate = (agg["draft_prs"] / total_prs) if total_prs else 0.0

        change_requests = agg["change_requests"]
        reopened = agg["reopened_prs"]
        fix48 = agg["post_merge_fix_within_48h"]

        dev_rows.append({
            "developer": dev,
            "delivery.merged_prs": float(merged_prs),
            "delivery.lead_time_median_h": float(pr_lead_median),
            "delivery.merge_rate": float(merge_rate),
            "collab.reviews_written": float(reviews_written),
            "collab.review_comments": float(review_comments_written),
            "collab.unique_people_reviewed": float(unique_teammates),
            "collab.tffr_median_h": float(tffr_median),
            "hygiene.pr_size_median": float(pr_size_median),
            "hygiene.small_pr_pct": float(small_pr_pct),
            "hygiene.draft_rate": float(draft_rate),
            "stability.change_requests": float(change_requests),
            "stability.reopened_prs": float(reopened),
            "stability.fix48": float(fix48),
            "activity.commits": float(agg["commits"]),
        })

    # Normalize within each metric
    def norm_field(rows: List[Dict[str, float]], key: str, higher_is_better: bool) -> List[float]:
        vals = [float(r.get(key, 0.0)) for r in rows]
        vals = _winsorize(vals)
        return _minmax_norm(vals, higher_is_better)

    if not dev_rows:
        return {"developers": [], "team": {"score": 0.0}}

    # Delivery
    d1 = norm_field(dev_rows, "delivery.merged_prs", True)
    d2 = norm_field(dev_rows, "delivery.lead_time_median_h", False)
    d3 = norm_field(dev_rows, "delivery.merge_rate", True)
    delivery = [0.4 * a + 0.3 * b + 0.3 * c for a, b, c in zip(d1, d2, d3)]

    # Collaboration
    c1 = norm_field(dev_rows, "collab.reviews_written", True)
    c2 = norm_field(dev_rows, "collab.review_comments", True)
    c3 = norm_field(dev_rows, "collab.unique_people_reviewed", True)
    c4 = norm_field(dev_rows, "collab.tffr_median_h", False)
    collaboration = [0.3 * a + 0.2 * b + 0.2 * c + 0.3 * d for a, b, c, d in zip(c1, c2, c3, c4)]

    # Hygiene
    h1 = norm_field(dev_rows, "hygiene.pr_size_median", False)
    h2 = norm_field(dev_rows, "hygiene.small_pr_pct", True)
    h3 = norm_field(dev_rows, "hygiene.draft_rate", False)
    hygiene = [0.4 * a + 0.4 * b + 0.2 * c for a, b, c in zip(h1, h2, h3)]

    # Stability/quality
    s1 = norm_field(dev_rows, "stability.change_requests", False)
    s2 = norm_field(dev_rows, "stability.reopened_prs", False)
    s3 = norm_field(dev_rows, "stability.fix48", True)
    stability = [0.4 * a + 0.3 * b + 0.3 * c for a, b, c in zip(s1, s2, s3)]

    w_del = float(weights.get("delivery", 0.30))
    w_col = float(weights.get("collaboration", 0.25))
    w_hyg = float(weights.get("hygiene", 0.15))
    w_sta = float(weights.get("stability", 0.30))

    scores = []
    for row, dv, co, hy, st in zip(dev_rows, delivery, collaboration, hygiene, stability):
        score01 = w_del * dv + w_col * co + w_hyg * hy + w_sta * st
        scores.append({
            **row,
            "score": round(100.0 * score01, 2),
            "subscores": {
                "delivery": round(100.0 * dv, 2),
                "collaboration": round(100.0 * co, 2),
                "hygiene": round(100.0 * hy, 2),
                "stability": round(100.0 * st, 2),
            },
        })

    # Team aggregate: average of individual scores
    team_score = round(sum(s["score"] for s in scores) / max(1, len(scores)), 2)

    # Sort by score desc
    scores.sort(key=lambda r: r["score"], reverse=True)

    return {
        "developers": scores,
        "team": {
            "owner": owner,
            "repo": repo,
            "since": since_iso,
            "until": until_iso,
            "score": team_score,
            "count": len(scores),
        },
    }



