import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

from . import collect, metrics, report, insights


REPO_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$")


def parse_repo_input(s: str) -> Tuple[str, str]:
    m = REPO_RE.search(s)
    if m:
        return m.group("owner"), m.group("repo")
    if "/" in s:
        owner, repo = s.split("/", 1)
        return owner, repo
    raise ValueError("Expected 'owner/repo' or a GitHub URL")


def _load_config(path: Any) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    if yaml is None:  # pragma: no cover
        print("PyYAML not installed; ignoring --config", file=sys.stderr)
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_github_token() -> str:
    """Get GitHub token from gh CLI or GITHUB_TOKEN env var."""
    # Try gh CLI first
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Fall back to environment variable
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    
    raise RuntimeError(
        "No GitHub authentication found. Either:\n"
        "  1. Install and authenticate with GitHub CLI: gh auth login\n"
        "  2. Set GITHUB_TOKEN environment variable"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser("devskill")
    p.add_argument("--repo-url", required=True, help="owner/repo or GitHub URL")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--outdir", default="reports")
    p.add_argument("--config")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args(argv)

    owner, repo = parse_repo_input(args.repo_url.strip())
    try:
        token = _get_github_token()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    since = now - dt.timedelta(days=args.days)
    since_iso, until_iso = since.isoformat(), now.isoformat()

    cfg = _load_config(args.config)
    bots = list(cfg.get("bots", []))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path.cwd() / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"Collecting GitHub data for {owner}/{repo} since {since_iso}...")
    data = collect.collect_repository_data(
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        token=token,
        bots=bots,
        cache_dir=str(cache_dir),
        use_cache=not args.no_cache,
    )

    tag = f"{owner}-{repo}-{now:%Y-%m-%d}"
    raw_path = outdir / f"dev-skill-raw-{tag}.json"
    with raw_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    print(f"Wrote raw data → {raw_path}")

    print("Computing metrics and scores...")
    scores = metrics.compute_scores(
        data=data,
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        config=cfg,
    )

    print("Generating reports...")
    report.generate_reports(
        scores=scores,
        outdir=str(outdir),
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        named=True,
        tag=tag,
    )
    insights.generate_insights(
        scores=scores,
        data=data,
        outdir=str(outdir),
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        small_pr_threshold=float(((cfg.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300),
        tag=tag,
    )
    print(f"Reports written to {outdir}")
    return 0


