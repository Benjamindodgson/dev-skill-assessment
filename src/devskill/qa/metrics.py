import datetime as dt
from collections import defaultdict
from typing import Any, Dict, List, Optional


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
        # Extended metrics
        "qa_comment_response_hours": [],
        "qa_time_hours": [],
        "qa_items_touched": 0,
        "reopen_count": 0,
        "items_with_parent": 0,
        "items_total_authored": 0,
        "hygiene_structured": 0,
        "hygiene_structured_total": 0,
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

    # --- Extended signals from comments/updates/stories ---
    qa_states = {str(s).lower() for s in (config.get("qa_states") or [])}
    comments_by_wi: Dict[str, List[Dict[str, Any]]] = data.get("work_item_comments", {}) or {}
    updates_by_wi: Dict[str, List[Dict[str, Any]]] = data.get("work_item_updates", {}) or {}
    stories: List[Dict[str, Any]] = data.get("stories", []) or []
    qa_users_list: List[str] = data.get("qa_users", []) or []
    qa_set_lower = {str(x).lower() for x in qa_users_list}

    def to_canonical_from_identity(u: Dict[str, Any]) -> str:
        return canonical(u.get("uniqueName") or u.get("displayName") or u.get("id") or "")

    def is_qa_identity(u: Dict[str, Any]) -> bool:
        who = to_canonical_from_identity(u)
        return who.lower() in qa_set_lower

    def parse_state_spans(updates: List[Dict[str, Any]]) -> List[tuple]:
        # returns list of (enter_dt, exit_dt, state_lower)
        events = []
        for u in updates:
            rd = u.get("revisedDate") or ""
            try:
                when = _parse_iso(rd)
            except Exception:
                continue
            fields = u.get("fields", {}) or {}
            st = fields.get("System.State") or {}
            if "newValue" in st or "oldValue" in st:
                old_v = str(st.get("oldValue") or "").lower()
                new_v = str(st.get("newValue") or "").lower()
                events.append((when, old_v, new_v))
        events.sort(key=lambda x: x[0])
        spans: List[tuple] = []
        current_in: Optional[dt.datetime] = None
        for when, old_v, new_v in events:
            # enter QA
            if new_v in qa_states and (old_v not in qa_states):
                if current_in is None:
                    current_in = when
            # exit QA
            if old_v in qa_states and (new_v not in qa_states):
                if current_in is not None:
                    spans.append((current_in, when, old_v))
                    current_in = None
        # do not keep open span to now; our updates are window-bound, so ignore trailing
        return spans

    def first_enter_time(updates: List[Dict[str, Any]]) -> Optional[dt.datetime]:
        for u in sorted(updates, key=lambda x: _parse_iso(x.get("revisedDate") or "1970-01-01T00:00:00Z")):
            st = (u.get("fields", {}) or {}).get("System.State") or {}
            new_v = str(st.get("newValue") or "").lower()
            old_v = str(st.get("oldValue") or "").lower()
            if new_v in qa_states and (old_v not in qa_states):
                try:
                    return _parse_iso(u.get("revisedDate") or "")
                except Exception:
                    return None
        return None

    def has_structured_text(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        # simplistic heuristics: mentions steps and expected/actual
        has_steps = ("steps" in t) or ("step" in t) or ("1." in t and "2." in t)
        has_expect = ("expected" in t)
        has_actual = ("actual" in t)
        return has_steps and (has_expect or has_actual)

    # Evaluate items (bugs + stories)
    def process_item(item: Dict[str, Any]) -> None:
        fields = item.get("fields", {})
        wi_id = str(item.get("id") or "")
        created_by = fields.get("System.CreatedBy") or {}
        creator = to_canonical_from_identity(created_by)
        if creator.lower() not in excluded:
            # Parent association for authored items
            per_user[creator]["items_total_authored"] += 1
            relations = item.get("relations") or []
            if any("hierarchy" in str((rel.get("rel") or "")).lower() and "reverse" in str((rel.get("rel") or "")).lower() for rel in relations) or any("parent" in str((rel.get("rel") or "")).lower() for rel in relations):
                per_user[creator]["items_with_parent"] += 1
            # Structured hygiene in description/repro or author's comments
            desc = str(fields.get("Microsoft.VSTS.TCM.ReproSteps") or fields.get("System.Description") or "")
            structured = has_structured_text(desc)
            # Author comments
            for c in (comments_by_wi.get(wi_id) or []):
                author = c.get("author") or {}
                if to_canonical_from_identity(author) == creator and has_structured_text(str(c.get("text") or "")):
                    structured = True
                    break
            per_user[creator]["hygiene_structured_total"] += 1
            if structured:
                per_user[creator]["hygiene_structured"] += 1

        # QA commenters involvement
        qa_commenters = set()
        for c in (comments_by_wi.get(wi_id) or []):
            author = c.get("author") or {}
            if is_qa_identity(author):
                qa_commenters.add(to_canonical_from_identity(author))

        # Time spans in QA states
        spans = parse_state_spans(updates_by_wi.get(wi_id) or [])
        total_qa_h = 0.0
        for ent, ext, _ in spans:
            total_qa_h += max(0.0, (ext - ent).total_seconds() / 3600.0)

        # First enter time
        ent_time = first_enter_time(updates_by_wi.get(wi_id) or [])

        # Response times per QA commenter
        for qc in qa_commenters:
            per_user[qc]["qa_items_touched"] += 1
            if total_qa_h > 0:
                per_user[qc]["qa_time_hours"].append(total_qa_h)
            # comment delta to first enter
            if ent_time is not None:
                # find earliest comment time by this QA commenter
                times = []
                for c in (comments_by_wi.get(wi_id) or []):
                    a = c.get("author") or {}
                    if to_canonical_from_identity(a) == qc:
                        try:
                            times.append(_parse_iso(c.get("createdDate") or c.get("revisedDate") or ""))
                        except Exception:
                            continue
                if times:
                    first_c = min(times)
                    delta_h = max(0.0, (first_c - ent_time).total_seconds() / 3600.0)
                    per_user[qc]["qa_comment_response_hours"].append(delta_h)

        # Reopen/bounce count: number of times re-entered QA after leaving it
        reentries = 0
        # count number of entries
        entries = 0
        for u in sorted(updates_by_wi.get(wi_id) or [], key=lambda x: _parse_iso(x.get("revisedDate") or "1970-01-01T00:00:00Z")):
            st = (u.get("fields", {}) or {}).get("System.State") or {}
            old_v = str(st.get("oldValue") or "").lower()
            new_v = str(st.get("newValue") or "").lower()
            if new_v in qa_states and (old_v not in qa_states):
                entries += 1
        if entries > 1:
            reentries = entries - 1
            for qc in qa_commenters:
                per_user[qc]["reopen_count"] += reentries

    # Process bugs and stories
    for b in bugs:
        process_item(b)
    for s in stories:
        process_item(s)

    # Now build rows
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
        hygiene_base = 0.6 * hygiene_repro + 0.4 * hygiene_attach
        structured_ratio = (agg["hygiene_structured"] / max(1, agg["hygiene_structured_total"])) if agg["hygiene_structured_total"] else 0.0
        parent_assoc_ratio = (agg["items_with_parent"] / max(1, agg["items_total_authored"])) if agg["items_total_authored"] else 0.0
        hygiene_score_proxy = 0.5 * hygiene_base + 0.3 * structured_ratio + 0.2 * parent_assoc_ratio

        qa_comment_median_h = _median(agg["qa_comment_response_hours"]) if agg["qa_comment_response_hours"] else 0.0
        qa_time_median_h = _median(agg["qa_time_hours"]) if agg["qa_time_hours"] else 0.0
        reopen_rate = (float(agg["reopen_count"]) / max(1, float(agg["qa_items_touched"]))) if agg["qa_items_touched"] else 0.0

        rows.append({
            "person": user,
            "testing.executed": float(agg["test_executed"]),
            "testing.pass_rate": float(pass_rate),
            "defects.created": float(agg["bug_created"]),
            "defects.valid_ratio": float(valid_bug_ratio),
            "defects.severity_avg": float(sev_avg),
            "defects.reopen_rate": float(reopen_rate),
            "hygiene.score": float(hygiene_score_proxy),
            "hygiene.structured_ratio": float(structured_ratio),
            "hygiene.parent_assoc_ratio": float(parent_assoc_ratio),
            "responsiveness.close_median_h": float(close_median_h),
            "responsiveness.qa_comment_median_h": float(qa_comment_median_h),
            "responsiveness.qa_time_median_h": float(qa_time_median_h),
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

    # Defects: valid ratio (higher better), severity (higher better), reopen rate (lower better)
    d1 = norm_field("defects.valid_ratio", True)
    d2 = norm_field("defects.severity_avg", True)
    d3 = norm_field("defects.reopen_rate", False)
    defects = [0.6 * a + 0.3 * b + 0.1 * c for a, b, c in zip(d1, d2, d3)]

    # Hygiene: hygiene.score (higher better)
    h1 = norm_field("hygiene.score", True)
    hygiene = h1

    # Responsiveness: close time (lower better), time to first QA comment (lower better), time-in-QA (lower better)
    r1 = norm_field("responsiveness.close_median_h", False)
    r2 = norm_field("responsiveness.qa_comment_median_h", False)
    r3 = norm_field("responsiveness.qa_time_median_h", False)
    responsiveness = [0.5 * a + 0.25 * b + 0.25 * c for a, b, c in zip(r1, r2, r3)]

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


