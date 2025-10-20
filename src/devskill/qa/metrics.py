import datetime as dt
from collections import defaultdict
from typing import Any, Dict, Iterable, List


def _parse_iso(s: str) -> dt.datetime:
    if not s:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    s = s.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s)


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


def compute_scores(
    data: Dict[str, Any],
    org: str,
    project: str,
    since_iso: str,
    until_iso: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    # aliases/exclude
    alias_map = {**{k.lower(): v for k, v in (config.get("aliases") or {}).items()}}
    excluded = {str(x).lower() for x in (config.get("exclude") or [])}

    def canonical(name: str) -> str:
        if not name:
            return "unknown"
        lower = name.lower()
        return alias_map.get(lower, name)

    results = data.get("test_results", [])
    bugs = data.get("bugs", [])

    per_user = defaultdict(lambda: {
        "test_executed": 0,
        "test_pass": 0,
        "test_fail": 0,
        "bug_created": 0,
        "bug_closed": 0,
        "bug_severity_weights": [],
        "bug_close_hours": [],
        "bug_has_repro": 0,
        "bug_has_attachments": 0,
        "bug_total": 0,
    })

    # Aggregate test results by tester
    for r in results:
        owner = r.get("tester") or {}
        who = canonical(owner.get("uniqueName") or owner.get("displayName") or owner.get("id") or "")
        if who.lower() in excluded:
            continue
        per_user[who]["test_executed"] += 1
        outcome = (r.get("outcome") or "").upper()
        if outcome == "PASSED":
            per_user[who]["test_pass"] += 1
        elif outcome in ("FAILED", "ERROR"):
            per_user[who]["test_fail"] += 1

    # Aggregate bugs by creator and lifecycle
    for b in bugs:
        fields = b.get("fields", {})
        created_by = fields.get("System.CreatedBy") or {}
        creator = canonical(created_by.get("uniqueName") or created_by.get("displayName") or created_by.get("id") or "")
        if creator.lower() not in excluded:
            per_user[creator]["bug_created"] += 1
            per_user[creator]["bug_total"] += 1
            # hygiene proxies on the author
            repro = fields.get("Microsoft.VSTS.TCM.ReproSteps") or fields.get("System.Description")
            if repro:
                per_user[creator]["bug_has_repro"] += 1
            # attachments presence via Relations
            relations = b.get("relations") or []
            if any((rel.get("rel") or "").lower() == "attachedfile" for rel in relations):
                per_user[creator]["bug_has_attachments"] += 1

        # Closing stats attributed to the resolver/closer for responsiveness
        state = (fields.get("System.State") or "").lower()
        closed_dates: List[str] = []
        if state in ("closed", "resolved", "done"):
            closed_dates.append(fields.get("Microsoft.VSTS.Common.ResolvedDate") or fields.get("System.ChangedDate") or "")
        if closed_dates:
            created_date = fields.get("System.CreatedDate") or ""
            start = _parse_iso(created_date)
            for cd in closed_dates:
                end = _parse_iso(cd)
                hours = (end - start).total_seconds() / 3600.0
                # Attribute to creator for simplicity; could also attribute to AssignedTo
                per_user[creator]["bug_close_hours"].append(hours)
                per_user[creator]["bug_closed"] += 1

        sev_raw = (fields.get("Microsoft.VSTS.Common.Severity") or fields.get("Severity") or "").lower()
        weight = 1.0
        if "1" in sev_raw or "critical" in sev_raw:
            weight = 3.0
        elif "2" in sev_raw or "high" in sev_raw:
            weight = 2.0
        elif "3" in sev_raw or "medium" in sev_raw:
            weight = 1.5
        else:
            weight = 1.0
        per_user[creator]["bug_severity_weights"].append(weight)

    # Build rows
    rows: List[Dict[str, Any]] = []
    for user, agg in per_user.items():
        if user.lower() in excluded:
            continue
        executed = max(1, int(agg["test_executed"]))
        pass_rate = (agg["test_pass"] / executed) if executed else 0.0
        valid_bug_ratio = (agg["bug_closed"] / max(1, agg["bug_created"])) if agg["bug_created"] else 0.0
        sev_avg = sum(agg["bug_severity_weights"]) / max(1, len(agg["bug_severity_weights"]))
        close_median_h = _median(agg["bug_close_hours"]) if agg["bug_close_hours"] else 0.0
        hygiene_repro = (agg["bug_has_repro"] / max(1, agg["bug_total"])) if agg["bug_total"] else 0.0
        hygiene_attach = (agg["bug_has_attachments"] / max(1, agg["bug_total"])) if agg["bug_total"] else 0.0
        hygiene_score_proxy = 0.6 * hygiene_repro + 0.4 * hygiene_attach

        rows.append({
            "person": user,
            "testing.executed": float(agg["test_executed"]),
            "testing.pass_rate": float(pass_rate),
            "defects.created": float(agg["bug_created"]),
            "defects.valid_ratio": float(valid_bug_ratio),
            "defects.severity_avg": float(sev_avg),
            "hygiene.score": float(hygiene_score_proxy),
            "responsiveness.close_median_h": float(close_median_h),
        })

    if not rows:
        return {"people": [], "team": {"score": 0.0}}

    # Normalize per area
    def norm_field(key: str, higher_is_better: bool) -> List[float]:
        vals = [float(r.get(key, 0.0)) for r in rows]
        vals = _winsorize(vals)
        return _minmax_norm(vals, higher_is_better)

    # Testing: executed (higher better), pass_rate (higher better)
    t1 = norm_field("testing.executed", True)
    t2 = norm_field("testing.pass_rate", True)
    testing = [0.5 * a + 0.5 * b for a, b in zip(t1, t2)]

    # Defects: created (higher is ambiguous; we treat moderate as neutral by inverting severity only). We'll reward closure ratio and higher severity average (as proxy for finding impactful bugs)
    d1 = norm_field("defects.valid_ratio", True)
    d2 = norm_field("defects.severity_avg", True)
    defects = [0.7 * a + 0.3 * b for a, b in zip(d1, d2)]

    # Hygiene: hygiene.score (higher better)
    h1 = norm_field("hygiene.score", True)
    hygiene = h1

    # Responsiveness: close_median_h (lower is better)
    r1 = norm_field("responsiveness.close_median_h", False)
    responsiveness = r1

    weights = config.get("weights", {"testing": 0.30, "defects": 0.30, "hygiene": 0.20, "responsiveness": 0.20})
    w_t = float(weights.get("testing", 0.30))
    w_d = float(weights.get("defects", 0.30))
    w_h = float(weights.get("hygiene", 0.20))
    w_r = float(weights.get("responsiveness", 0.20))

    scores = []
    for row, tv, dv, hv, rv in zip(rows, testing, defects, hygiene, responsiveness):
        score01 = w_t * tv + w_d * dv + w_h * hv + w_r * rv
        scores.append({
            **row,
            "score": round(100.0 * score01, 2),
            "subscores": {
                "testing": round(100.0 * tv, 2),
                "defects": round(100.0 * dv, 2),
                "hygiene": round(100.0 * hv, 2),
                "responsiveness": round(100.0 * rv, 2),
            },
        })

    team_score = round(sum(s["score"] for s in scores) / max(1, len(scores)), 2)
    scores.sort(key=lambda r: r["score"], reverse=True)

    return {
        "people": scores,
        "team": {
            "org": org,
            "project": project,
            "since": since_iso,
            "until": until_iso,
            "score": team_score,
            "count": len(scores),
        },
    }


