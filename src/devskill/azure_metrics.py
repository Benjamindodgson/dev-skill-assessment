import datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _parse_iso(s: str) -> dt.datetime:
    if not s:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    normalized = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized)
    except Exception:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


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


def _state_events(updates: List[Dict[str, Any]], field: str) -> List[Tuple[dt.datetime, str, str]]:
    events: List[Tuple[dt.datetime, str, str]] = []
    for u in updates or []:
        when = _parse_iso(u.get("revisedDate") or "")
        fields = u.get("fields", {}) or {}
        change = fields.get(field) or {}
        if not isinstance(change, dict):
            continue
        old_v = str(change.get("oldValue") or "").lower()
        new_v = str(change.get("newValue") or "").lower()
        if old_v or new_v:
            events.append((when, old_v, new_v))
    events.sort(key=lambda x: x[0])
    return events


def _iteration_at(creation: dt.datetime, initial: str, events: List[Tuple[dt.datetime, str, str]], when: dt.datetime) -> str:
    current = str(initial or "")
    for evt_time, _old, new in events:
        if evt_time <= when:
            current = new or current
        else:
            break
    return current


def _state_metrics(
    created: dt.datetime,
    initial_state: str,
    events: List[Tuple[dt.datetime, str, str]],
    ready_states: Iterable[str],
    done_states: Iterable[str],
    qa_failed_states: Iterable[str],
) -> Tuple[Optional[dt.datetime], Optional[dt.datetime], int]:
    ready_at: Optional[dt.datetime] = created if initial_state in ready_states else None
    done_at: Optional[dt.datetime] = created if initial_state in done_states else None
    qa_failed = 1 if initial_state in qa_failed_states else 0

    for when, old_state, new_state in events:
        if new_state in qa_failed_states and old_state not in qa_failed_states:
            qa_failed += 1
        if new_state in ready_states and ready_at is None:
            ready_at = when
        if new_state in done_states and ready_at is not None and done_at is None:
            done_at = when
    if done_at is not None and ready_at is None:
        ready_at = created
    return ready_at, done_at, qa_failed


def compute_scores(azure_data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Compute Azure assessment scores (Resolution, Predictability, Velocity, Quality)."""
    if not azure_data:
        return {
            "team": {
                "score": 0.0,
                "count": 0,
            }
        }

    azure_cfg = config.get("azure") or {}
    ready_states = {str(s).lower() for s in (azure_data.get("ready_states") or azure_cfg.get("ready_states") or ["Ready for Dev"])}
    done_states = {str(s).lower() for s in (azure_data.get("resolved_states") or azure_cfg.get("resolved_states") or ["Resolved", "Closed", "Done", "Removed", "Cannot Reproduce"])}
    qa_failed_states = {str(s).lower() for s in (azure_data.get("qa_failed_states") or azure_cfg.get("qa_failed_states") or ["QA Failed"])}
    effort_field = azure_cfg.get("effort_field") or "Microsoft.VSTS.Scheduling.StoryPoints"
    weights = azure_cfg.get("weights") or {"resolution": 0.25, "predictability": 0.25, "velocity": 0.25, "quality": 0.25}

    since_iso = azure_data.get("since") or ""
    until_iso = azure_data.get("until") or ""
    since_dt = _parse_iso(since_iso)
    until_dt = _parse_iso(until_iso)

    work_items = azure_data.get("work_items", []) or []
    iterations = azure_data.get("iterations", []) or []

    # Precompute iteration events for each item
    items_summary: List[Dict[str, Any]] = []
    resolution_hours: List[float] = []
    qa_failed_counts: List[float] = []
    velocity_efforts: List[float] = []
    velocity_completed = 0

    for item in work_items:
        fields = item.get("fields", {}) or {}
        created = _parse_iso(fields.get("System.CreatedDate") or "")
        state_initial = str(fields.get("System.State") or "").lower()
        updates = item.get("updates", []) or []
        state_events = _state_events(updates, "System.State")
        iter_events = _state_events(updates, "System.IterationPath")
        iteration_initial = str(fields.get("System.IterationPath") or "")

        ready_at, done_at, qa_failed = _state_metrics(
            created=created,
            initial_state=state_initial,
            events=state_events,
            ready_states=ready_states,
            done_states=done_states,
            qa_failed_states=qa_failed_states,
        )

        resolution_h: Optional[float] = None
        if ready_at and done_at and done_at >= ready_at:
            resolution_h = (done_at - ready_at).total_seconds() / 3600.0
            resolution_hours.append(resolution_h)

        if qa_failed > 0:
            qa_failed_counts.append(float(qa_failed))

        done_within_window = done_at is not None and since_dt <= done_at <= until_dt
        effort_raw = fields.get(effort_field)
        try:
            effort = float(effort_raw)
        except Exception:
            effort = 0.0
        if done_within_window:
            velocity_completed += 1
            velocity_efforts.append(effort)

        items_summary.append(
            {
                "id": item.get("id"),
                "state": state_initial,
                "created": fields.get("System.CreatedDate"),
                "iteration_path": iteration_initial,
                "ready_at": ready_at.isoformat() if ready_at else None,
                "done_at": done_at.isoformat() if done_at else None,
                "qa_failed_entries": qa_failed,
                "resolution_hours": resolution_h,
                "effort": effort,
                "iteration_events": [
                    {"when": t.isoformat(), "old": old, "new": new} for t, old, new in iter_events
                ],
            }
        )

    # Predictability: per-iteration commitment completion
    iteration_rows: List[Dict[str, Any]] = []
    predictability_scores: List[float] = []
    for it in iterations:
        attrs = it.get("attributes", {}) or {}
        start = _parse_iso(attrs.get("startDate") or "")
        finish = _parse_iso(attrs.get("finishDate") or "")
        if finish < since_dt or start > until_dt:
            continue
        path = it.get("path") or it.get("name") or ""

        committed = 0
        completed = 0
        for item_summary, raw in zip(items_summary, work_items):
            iter_events = _state_events(raw.get("updates", []) or [], "System.IterationPath")
            iter_initial = str((raw.get("fields") or {}).get("System.IterationPath") or "")
            creation = _parse_iso((raw.get("fields") or {}).get("System.CreatedDate") or "")
            iter_value = _iteration_at(creation, iter_initial, iter_events, start)
            if iter_value != path:
                continue
            committed += 1
            done_iso = item_summary.get("done_at")
            if done_iso:
                done_at = _parse_iso(done_iso)
                if done_at <= finish:
                    completed += 1

        ratio = 1.0 if committed == 0 else min(1.0, completed / committed)
        predictability_scores.append(ratio)
        iteration_rows.append(
            {
                "id": it.get("id"),
                "path": path,
                "name": it.get("name"),
                "start": attrs.get("startDate"),
                "finish": attrs.get("finishDate"),
                "committed": committed,
                "completed": completed,
                "commitment_ratio": ratio,
            }
        )

    # Normalize dimensions
    def _score_from_values(values: List[float], higher_is_better: bool, empty_default: float = 0.5) -> float:
        if not values:
            return empty_default
        norms = _minmax_norm(_winsorize(values), higher_is_better)
        if not norms:
            return empty_default
        return sum(norms) / len(norms)

    resolution_score = _score_from_values(resolution_hours, higher_is_better=False)
    predictability_score = sum(predictability_scores) / len(predictability_scores) if predictability_scores else 0.5
    if velocity_efforts and all(v <= 0 for v in velocity_efforts):
        velocity_score = 0.0
    else:
        velocity_score = _score_from_values(velocity_efforts, higher_is_better=True, empty_default=0.0)
    if qa_failed_counts:
        quality_score = _score_from_values(qa_failed_counts, higher_is_better=False)
    else:
        # If no QA failed entries were observed, treat as best quality.
        quality_score = 1.0 if work_items else 0.5

    w_res = float(weights.get("resolution", 0.25))
    w_pred = float(weights.get("predictability", 0.25))
    w_vel = float(weights.get("velocity", 0.25))
    w_qual = float(weights.get("quality", 0.25))

    team_score01 = w_res * resolution_score + w_pred * predictability_score + w_vel * velocity_score + w_qual * quality_score

    return {
        "team": {
            "since": since_iso,
            "until": until_iso,
            "score": round(100.0 * team_score01, 2),
            "count": len(work_items),
            "subscores": {
                "resolution": round(100.0 * resolution_score, 2),
                "predictability": round(100.0 * predictability_score, 2),
                "velocity": round(100.0 * velocity_score, 2),
                "quality": round(100.0 * quality_score, 2),
            },
        },
        "resolution": {
            "median_hours": _median(resolution_hours),
            "items_with_resolution": len(resolution_hours),
            "samples": resolution_hours,
        },
        "predictability": {
            "iterations": iteration_rows,
            "average_ratio": sum(predictability_scores) / len(predictability_scores) if predictability_scores else 0.0,
        },
        "velocity": {
            "completed_effort": sum(velocity_efforts),
            "completed_items": velocity_completed,
            "efforts": velocity_efforts,
        },
        "quality": {
            "qa_failed_counts": qa_failed_counts,
            "median_qa_failed": _median(qa_failed_counts),
            "items_with_qa_failed": len(qa_failed_counts),
        },
        "items": items_summary,
    }
