import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def _normalize_org(org: str) -> str:
    """Return an org URL acceptable to Azure DevOps CLI."""
    if not org:
        return org
    org = org.strip()
    if org.startswith("http://"):
        org = org.replace("http://", "https://", 1)
    if org.startswith("https://"):
        return org.rstrip("/")
    # Accept bare org name
    return f"https://dev.azure.com/{org}"


def _run_az(args: List[str], env: Optional[Dict[str, str]] = None) -> Dict:
    """Run az CLI and return parsed JSON, raising on failure."""
    cmd = ["az"] + args + ["-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"Azure DevOps CLI failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse az CLI JSON response: {e}") from e


def _cache_path(cache_dir: Path, org: str, project: str) -> Path:
    safe_org = org.replace("/", "_").replace(":", "_")
    safe_project = project.replace("/", "_").replace(":", "_")
    return cache_dir / f"ado_workitems_{safe_org}_{safe_project}.json"


def _load_cache(cache_dir: Optional[str], org: str, project: str) -> Dict[str, Dict]:
    if not cache_dir:
        return {}
    p = _cache_path(Path(cache_dir), org, project)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache_dir: Optional[str], org: str, project: str, data: Dict[str, Dict]) -> None:
    if not cache_dir:
        return
    p = _cache_path(Path(cache_dir), org, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fetch_work_items(
    ids: Iterable[int],
    org: str,
    project: str,
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
) -> List[Dict]:
    """Fetch work items and updates via Azure DevOps CLI."""
    org_url = _normalize_org(org)
    project_name = project
    cache = _load_cache(cache_dir, org_url, project_name) if use_cache else {}

    results: List[Dict] = []
    dirty = False
    for raw_id in ids:
        wid = str(raw_id)
        if use_cache and wid in cache:
            results.append(cache[wid])
            continue

        item = _run_az(
            [
                "boards",
                "work-item",
                "show",
                "--id",
                wid,
                "--organization",
                org_url,
                "--project",
                project_name,
            ]
        )
        updates = _run_az(
            [
                "boards",
                "work-item",
                "updates",
                "list",
                "--id",
                wid,
                "--organization",
                org_url,
                "--project",
                project_name,
            ]
        ).get("value", [])

        payload = {"id": item.get("id"), "fields": item.get("fields", {}), "relations": item.get("relations", []), "updates": updates}
        cache[wid] = payload
        results.append(payload)
        dirty = True

    if dirty:
        _save_cache(cache_dir, org_url, project_name, cache)
    return results

