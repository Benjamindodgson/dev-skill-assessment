import datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


def _parse_dt(value: Any) -> dt.datetime:
    if not value:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2 == 1:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def _clamp01(v: float) -> float:
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _business_hours_between(
    start: dt.datetime, end: dt.datetime, skip_weekends: bool = True
) -> float:
    if not start or not end or end <= start:
        return 0.0
    if not skip_weekends:
        return max(0.0, (end - start).total_seconds() / 3600.0)

    total_hours = 0.0
    current = start
    while current < end:
        next_day = dt.datetime(current.year, current.month, current.day, tzinfo=current.tzinfo) + dt.timedelta(days=1)
        day_end = min(next_day, end)
        if current.weekday() < 5:  # Monday=0, Sunday=6
            total_hours += max(0.0, (day_end - current).total_seconds() / 3600.0)
        current = next_day
    return total_hours


def _inverse_time_score(hours: float, good: float, bad: float) -> float:
    if hours <= 0:
        return 100.0
    if hours <= good:
        return 100.0
    if hours >= bad:
        return 0.0
    return 100.0 * (bad - hours) / (bad - good)


def _extract_assigned_email(fields: Dict[str, Any]) -> str:
    assigned = fields.get("System.AssignedTo")
    if isinstance(assigned, dict):
        email = (
            assigned.get("uniqueName")
            or assigned.get("mail")
            or assigned.get("email")
            or assigned.get("UserPrincipalName")
        )
        if email:
            return str(email).lower()
    elif isinstance(assigned, str):
        return assigned.lower()
    return ""


def _state_time_from_updates(
    updates: Iterable[Dict[str, Any]],
    target_states: Set[str],
    created_at: dt.datetime,
) -> Optional[dt.datetime]:
    if not target_states:
        return None
    normalized_targets = {s.lower() for s in target_states}
    sorted_updates = sorted(
        list(updates),
        key=lambda u: _parse_dt(u.get("revisedDate") or u.get("timestamp") or u.get("revised_date")),
    )
    for upd in sorted_updates:
        fields = upd.get("fields") or {}
        state_info = fields.get("System.State") or {}
        if isinstance(state_info, dict):
            new_state = state_info.get("newValue") or state_info.get("newvalue") or state_info.get("new_value")
        else:
            new_state = None
        if not new_state:
            continue
        if str(new_state).lower() in normalized_targets:
            ts = _parse_dt(upd.get("revisedDate") or upd.get("timestamp") or upd.get("revised_date"))
            return ts if ts.year > 1970 else created_at
    return None


def _has_state_hit(
    updates: Iterable[Dict[str, Any]],
    target_states: Set[str],
) -> bool:
    normalized_targets = {s.lower() for s in target_states}
    for upd in updates:
        fields = upd.get("fields") or {}
        state_info = fields.get("System.State") or {}
        new_state = None
        if isinstance(state_info, dict):
            new_state = state_info.get("newValue") or state_info.get("newvalue") or state_info.get("new_value")
        elif isinstance(state_info, str):
            new_state = state_info
        if new_state and str(new_state).lower() in normalized_targets:
            return True
    return False


def compute_scores(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute Azure assessment (Resolution, Predictability, Velocity, Quality) from ADO work items.
    Filters:
      - skip unassigned items
      - skip items whose assignee email appears in config["exclude_emails"] (case-insensitive)
    """
    work_items = data.get("work_items") or []
    ready_states = set((data.get("ready_states") or config.get("ready_states") or []))
    resolved_states = set((data.get("resolved_states") or config.get("resolved_states") or []))
    qa_failed_states = set((data.get("qa_failed_states") or config.get("qa_failed_states") or []))
    raw_excludes = config.get("exclude_emails") or []
    if isinstance(raw_excludes, str):
        raw_excludes = [part.strip() for part in raw_excludes.split(",") if part.strip()]
    exclude_emails = {str(e).lower() for e in raw_excludes}
    effort_field = config.get("effort_field") or "Microsoft.VSTS.Scheduling.StoryPoints"
    priority_field = config.get("priority_field") or "Microsoft.VSTS.Common.Priority"
    resolution_sla_cfg = config.get("resolution_sla") or {}
    use_business_days = bool(resolution_sla_cfg.get("use_business_days", True))
    bug_types = {str(t).lower() for t in (resolution_sla_cfg.get("bug_types") or ["Bug", "Defect"])}
    raw_priority_hours = (
        resolution_sla_cfg.get("bug_priority_hours")
        or resolution_sla_cfg.get("bug_priority_business_days")
        or {
            "blocker": 24,
            "critical": 24,
            "major": 72,
            "minor": 120,
            "trivial": 240,
        }
    )
    bug_priority_hours = {str(k).lower(): float(v) for k, v in raw_priority_hours.items() if v is not None}
    default_pbi_buckets = [
        {"max_hours": 48, "score": 100},
        {"max_hours": 72, "score": 75},
        {"max_hours": 96, "score": 50},
        {"max_hours": 120, "score": 25},
        {"max_hours": None, "score": 0},
    ]
    raw_pbi_buckets = resolution_sla_cfg.get("pbi_buckets") or default_pbi_buckets

    def _score_pbi(hours: float) -> float:
        cleaned = []
        for b in raw_pbi_buckets:
            if not isinstance(b, dict):
                continue
            max_h = b.get("max_hours")
            score = float(b.get("score", 0))
            cleaned.append((max_h, score))
        # preserve order; assume first matching bucket applies
        for max_h, score in cleaned:
            if max_h is None:
                return score
            try:
                if hours <= float(max_h):
                    return score
            except Exception:
                continue
        return 0.0

    def _norm_priority(value: Any) -> str:
        if isinstance(value, (int, float)):
            return str(int(value))
        return str(value or "").strip().lower()

    weights = config.get(
        "weights",
        {"resolution": 0.25, "predictability": 0.25, "velocity": 0.25, "quality": 0.25},
    )

    since_dt = _parse_dt(data.get("since"))
    until_dt = _parse_dt(data.get("until"))
    window_days = max(1.0, (until_dt - since_dt).total_seconds() / 86400.0)
    window_weeks = window_days / 7.0

    counts = {
        "total": len(work_items),
        "excluded": 0,
        "unassigned": 0,
        "excluded_emails": 0,
        "considered": 0,
        "resolved": 0,
        "qa_failed": 0,
        "resolution_items": 0,
        "resolution_bug_items": 0,
        "resolution_bug_sla_met": 0,
        "resolution_pbi_items": 0,
    }

    resolution_hours: List[float] = []
    resolution_scores: List[float] = []
    story_points_total = 0.0

    for item in work_items:
        fields = item.get("fields") or {}
        updates = item.get("updates") or []

        assignee_email = _extract_assigned_email(fields)
        if not assignee_email:
            counts["excluded"] += 1
            counts["unassigned"] += 1
            continue
        if assignee_email in exclude_emails:
            counts["excluded"] += 1
            counts["excluded_emails"] += 1
            continue

        created_at = _parse_dt(fields.get("System.CreatedDate"))
        resolved_at = _state_time_from_updates(updates, resolved_states, created_at)
        if not resolved_at:
            resolved_at = _parse_dt(
                fields.get("Microsoft.VSTS.Common.ResolvedDate")
                or fields.get("Microsoft.VSTS.Common.ClosedDate")
            )
            if resolved_at.year < 1971:
                resolved_at = None

        counts["considered"] += 1
        qa_failed = _has_state_hit(updates, qa_failed_states)
        if qa_failed:
            counts["qa_failed"] += 1

        if resolved_at:
            counts["resolved"] += 1
            delta_h = _business_hours_between(created_at, resolved_at, skip_weekends=use_business_days)
            if delta_h >= 0:
                resolution_hours.append(delta_h)
            story_points_total += float(fields.get(effort_field, 0.0) or 0.0)
            counts["resolution_items"] += 1

            work_item_type = str(fields.get("System.WorkItemType") or "").strip().lower()
            if work_item_type in bug_types:
                counts["resolution_bug_items"] += 1
                target_hours = bug_priority_hours.get(_norm_priority(fields.get(priority_field)))
                if target_hours is not None:
                    target_hours = max(0.0, float(target_hours))
                    score = 100.0 if delta_h <= target_hours else 0.0
                    if score == 100.0:
                        counts["resolution_bug_sla_met"] += 1
                else:
                    score = 0.0
                resolution_scores.append(score)
            else:
                counts["resolution_pbi_items"] += 1
                resolution_scores.append(_score_pbi(delta_h))

    resolution_hours_median = _median(resolution_hours)
    resolution_score_avg = sum(resolution_scores) / len(resolution_scores) if resolution_scores else 0.0
    predictability_rate = counts["resolved"] / counts["considered"] if counts["considered"] else 0.0
    velocity_points_per_week = story_points_total / window_weeks if window_weeks > 0 else 0.0
    qa_pass_rate = (
        (counts["resolved"] - counts["qa_failed"]) / counts["resolved"] if counts["resolved"] else 0.0
    )

    # Scores (0-100)
    resolution_score = resolution_score_avg
    predictability_score = 100.0 * _clamp01(predictability_rate)
    velocity_target = float(config.get("velocity_target", 20.0))
    if velocity_target <= 0:
        velocity_target = 20.0
    velocity_score = 100.0 * _clamp01(velocity_points_per_week / velocity_target)
    quality_score = 100.0 * _clamp01(qa_pass_rate)

    w_res = float(weights.get("resolution", 0.25))
    w_pre = float(weights.get("predictability", 0.25))
    w_vel = float(weights.get("velocity", 0.25))
    w_qual = float(weights.get("quality", 0.25))
    total_weight = w_res + w_pre + w_vel + w_qual
    if total_weight <= 0:
        total_weight = 1.0

    team_score = round(
        (
            w_res * resolution_score
            + w_pre * predictability_score
            + w_vel * velocity_score
            + w_qual * quality_score
        )
        / total_weight,
        2,
    )

    return {
        "team": {
            "org": data.get("org"),
            "project": data.get("project"),
            "since": data.get("since"),
            "until": data.get("until"),
            "score": team_score,
            "pillars": {
                "resolution": round(resolution_score, 2),
                "predictability": round(predictability_score, 2),
                "velocity": round(velocity_score, 2),
                "quality": round(quality_score, 2),
            },
            "metrics": {
                "resolution_hours_median": round(resolution_hours_median, 2),
                "resolution_score_avg": round(resolution_score_avg, 2),
                "resolution_items_scored": counts["resolution_items"],
                "resolution_bug_items": counts["resolution_bug_items"],
                "resolution_bug_sla_met": counts["resolution_bug_sla_met"],
                "resolution_pbi_items": counts["resolution_pbi_items"],
                "predictability_rate": round(predictability_rate, 4),
                "velocity_points_per_week": round(velocity_points_per_week, 4),
                "quality_pass_rate": round(qa_pass_rate, 4),
            },
        },
        "counts": counts,
    }

