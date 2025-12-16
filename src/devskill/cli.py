import argparse
import datetime as dt
import json
import os
import re
import time
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from rich.panel import Panel
from rich.box import ROUNDED

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


def _load_config(path: Any, verbose: bool = True) -> Dict[str, Any]:
    # Auto-discover config.yml if not specified
    auto_discovered = False
    if not path:
        default_config = Path.cwd() / "config.yml"
        if default_config.exists():
            path = default_config
            auto_discovered = True
        else:
            return {}
    p = Path(path)
    if not p.exists():
        return {}
    if yaml is None:  # pragma: no cover
        print("PyYAML not installed; ignoring --config", file=sys.stderr)
        return {}
    with p.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
        if verbose and auto_discovered:
            print(f"[config] Auto-discovered and loaded: {p}")
        return config


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


def _get_github_username(token: str) -> str:
    """Get the authenticated user's GitHub username."""
    query = """
    query {
        viewer {
            login
        }
    }
    """
    data = collect._request_json(
        "POST",
        collect.GITHUB_GRAPHQL,
        token,
        timeout=10,
        json={"query": query},
    )
    return data["data"]["viewer"]["login"]


def _list_user_repos(token: str) -> List[Dict[str, Any]]:
    """Fetch all repositories the authenticated user has access to via GitHub GraphQL API."""
    repos = []
    cursor = None
    
    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = f"""
        query {{
            viewer {{
                repositories(first: 100{after_clause}, affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {{
                    nodes {{
                        owner {{
                            login
                        }}
                        name
                        description
                    }}
                    pageInfo {{
                        hasNextPage
                        endCursor
                    }}
                }}
            }}
        }}
        """
        data = collect._request_json(
            "POST",
            collect.GITHUB_GRAPHQL,
            token,
            timeout=30,
            json={"query": query},
        )
        
        nodes = data["data"]["viewer"]["repositories"]["nodes"]
        repos.extend(nodes)
        
        page_info = data["data"]["viewer"]["repositories"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        # Gentle pacing to avoid secondary rate limits
        time.sleep(0.2)
    
    return repos


def _prompt_authentication() -> str:
    """Prompt for GitHub authentication, checking if already authenticated."""
    try:
        token = _get_github_token()
        username = _get_github_username(token)
        print(f"✓ Already authenticated as {username}")
        return token
    except RuntimeError:
        print("\n⚠️  GitHub authentication required")
        print("Please authenticate using one of these methods:")
        print("  1. GitHub CLI: gh auth login")
        print("  2. Environment variable: export GITHUB_TOKEN=your_token")
        print("\nPress Enter after authenticating...")
        input()
        # Retry after user authenticates
        return _get_github_token()


def _prompt_repo_selection(token: str) -> Tuple[str, str]:
    """Display available repos and prompt user to select one."""
    print("\nFetching your repositories...")
    repos = _list_user_repos(token)
    
    if not repos:
        print("No repositories found.")
        sys.exit(1)
    
    print(f"\nFound {len(repos)} repositories:")
    print("-" * 80)
    
    for idx, repo in enumerate(repos, 1):
        owner = repo["owner"]["login"]
        name = repo["name"]
        description = repo["description"] or "No description"
        # Truncate description if too long
        if len(description) > 60:
            description = description[:57] + "..."
        print(f"{idx:4d}. {owner}/{name:30s} - {description}")
    
    print("-" * 80)
    
    while True:
        try:
            selection = input(f"\nSelect repository number (1-{len(repos)}): ").strip()
            idx = int(selection) - 1
            if 0 <= idx < len(repos):
                selected = repos[idx]
                owner = selected["owner"]["login"]
                name = selected["name"]
                print(f"✓ Selected: {owner}/{name}")
                return owner, name
            else:
                print(f"Please enter a number between 1 and {len(repos)}")
        except (ValueError, KeyboardInterrupt):
            print("\nInvalid input. Please enter a number.")
        except EOFError:
            print("\nAborted.")
            sys.exit(1)


def _prompt_days() -> int:
    """Prompt for number of days to analyze."""
    while True:
        try:
            response = input("\nNumber of days to analyze [90]: ").strip()
            if not response:
                return 90
            days = int(response)
            if days > 0:
                return days
            print("Please enter a positive number.")
        except ValueError:
            print("Please enter a valid number.")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)


def _prompt_output_dir(default: str) -> str:
    """Prompt for output directory."""
    try:
        response = input(f"\nOutput directory [{default}]: ").strip()
        return response if response else default
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


def _prompt_config_file() -> Any:
    """Prompt for config file path."""
    try:
        response = input("\nConfig file path (press Enter to skip): ").strip()
        return response if response else None
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


def _discover_latest_raw(search_root: Optional[str]) -> Optional[Path]:
    """Find the most recent dev-skill-raw-*.json under the given root or defaults.

    Search order when search_root is None:
      1) ./reports (if exists)
    Select by newest modification time.
    """
    candidates: List[Path] = []
    roots: List[Path] = []
    if search_root:
        roots.append(Path(search_root))
    else:
        default_reports = Path.cwd() / "reports"
        if default_reports.exists():
            roots.append(default_reports)

    for root in roots:
        try:
            for p in root.rglob("dev-skill-raw-*.json"):
                if p.is_file():
                    candidates.append(p)
        except Exception:
            # Ignore permission or traversal errors and continue
            continue

    if not candidates:
        return None
    # Pick latest by mtime
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]

def _resolve_raw_path(raw_arg: Optional[str], outdir_arg: Optional[str]) -> Optional[Path]:
    """Return explicit raw path or discover the latest raw JSON."""
    if raw_arg:
        return Path(raw_arg)
    return _discover_latest_raw(outdir_arg)


def _derive_tag_from_raw_filename(p: Path) -> Optional[str]:
    name = p.name
    prefix = "dev-skill-raw-"
    suffix = ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix) : -len(suffix)]
    return None


def _print_terminal_summary(
    console: Console,
    scores: Dict[str, Any],
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    named: bool = True,
) -> None:
    team = scores.get("team", {})
    devs = scores.get("developers", [])

    title = f"{owner}/{repo} — {since_iso[:10]} → {until_iso[:10]}"
    team_score = float(team.get("score", 0.0))
    console.print(
        Panel.fit(
            f"Team Score: {team_score:.2f}",
            title="[bold]DevSkill Assessment[/bold]",
            subtitle=title,
            border_style="cyan",
            box=ROUNDED,
        )
    )

    table = Table(box=ROUNDED, header_style="bold cyan", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Developer", justify="left")
    table.add_column("Score", justify="right")
    table.add_column("Delivery", justify="right")
    table.add_column("Collaboration", justify="right")
    table.add_column("Hygiene", justify="right")
    table.add_column("Stability", justify="right")

    for idx, row in enumerate(devs, 1):
        name = row.get("developer", "unknown") if named else "dev-***"
        subs = row.get("subscores", {})
        table.add_row(
            str(idx),
            str(name),
            f"{float(row.get('score', 0.0)):.2f}",
            f"{float(subs.get('delivery', 0.0)):.2f}",
            f"{float(subs.get('collaboration', 0.0)):.2f}",
            f"{float(subs.get('hygiene', 0.0)):.2f}",
            f"{float(subs.get('stability', 0.0)):.2f}",
        )

    console.print(table)


def main(argv=None) -> int:
    p = argparse.ArgumentParser("devskill")
    subparsers = p.add_subparsers(dest="command")
    # Rerun subcommand (re-generate from existing raw JSON)
    rerun_p = subparsers.add_parser("rerun", help="Regenerate reports from last saved raw data")
    rerun_p.add_argument("--raw", default=None, help="Path to dev-skill-raw-*.json (optional)")
    rerun_p.add_argument("--tag", default=None, help="Tag to use for regenerated reports (optional)")
    rerun_p.add_argument("--anonymous", action="store_true", help="Generate anonymized developer names")
    # Rerun-local subcommand preserves previous behavior (no fresh fetch)
    rerun_local_p = subparsers.add_parser(
        "rerun-local", help="Regenerate reports from raw data without refetching GitHub"
    )
    rerun_local_p.add_argument("--raw", default=None, help="Path to dev-skill-raw-*.json (optional)")
    rerun_local_p.add_argument("--tag", default=None, help="Tag to use for regenerated reports (optional)")
    rerun_local_p.add_argument("--anonymous", action="store_true", help="Generate anonymized developer names")
    p.add_argument("--repo-url", required=False, help="owner/repo or GitHub URL")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--outdir", default=None, help="Output directory (default: ./reports)")
    p.add_argument("--config", default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--ado-org", default=None, help="Azure DevOps org (e.g., ecolabcommercialsolutions or https://dev.azure.com/org)")
    p.add_argument("--ado-project", default=None, help="Azure DevOps project name")
    p.add_argument("--ado-ready-states", default=None, help="Comma-separated states considered Ready for Dev")
    p.add_argument("--ado-resolved-states", default=None, help="Comma-separated states considered Resolved/Closed")
    p.add_argument("--ado-qa-failed-states", default=None, help="Comma-separated states representing QA Failed")
    p.add_argument("--ado-disable", action="store_true", help="Disable Azure DevOps bug enrichment even if org/project provided")
    args = p.parse_args(argv)

    # Handle rerun subcommand: rerun the previous query by re-collecting data for the same window
    if getattr(args, "command", None) == "rerun":
        console = Console()
        console.print("[bold cyan]Re-running last query with fresh data[/bold cyan]")

        raw_path = _resolve_raw_path(getattr(args, "raw", None), args.outdir)
        if not raw_path:
            console.print("[red]No dev-skill-raw-*.json found.[/red]")
            console.print("Hint: pass --raw PATH or --outdir DIR to search in.")
            return 2
        if not raw_path.exists():
            console.print(f"[red]Raw file not found:[/red] {raw_path}")
            return 2

        try:
            with raw_path.open("r", encoding="utf-8") as f:
                previous_raw = json.load(f)
        except Exception as e:
            console.print(f"[red]Failed to read raw JSON:[/red] {e}")
            return 2

        owner = previous_raw.get("owner")
        repo = previous_raw.get("repo")
        since_iso = previous_raw.get("since")
        until_iso = previous_raw.get("until")
        if not all([owner, repo, since_iso, until_iso]):
            console.print("[red]Raw JSON missing required fields (owner, repo, since, until).[/red]")
            return 2

        ado_meta = previous_raw.get("ado") or {}
        ado_org = ado_meta.get("org")
        ado_project = ado_meta.get("project")
        ado_ready_states = list(ado_meta.get("ready_states") or [])
        ado_resolved_states = list(ado_meta.get("resolved_states") or [])
        ado_qa_failed_states = list(ado_meta.get("qa_failed_states") or [])
        ado_disable = bool(args.ado_disable or not (ado_org and ado_project))

        cfg = _load_config(args.config)
        bots = list(cfg.get("bots", []))

        # Determine output directory and tag
        outdir_path = Path(args.outdir) if args.outdir else raw_path.parent
        outdir_path.mkdir(parents=True, exist_ok=True)

        tag = args.tag or _derive_tag_from_raw_filename(raw_path) or f"{owner}-{repo}-{dt.datetime.utcnow():%Y-%m-%d}"

        cache_dir = Path.cwd() / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            token = _get_github_token()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            return 2

        console.print(f"[dim]Owner/repo:[/dim] {owner}/{repo}")
        console.print(f"[dim]Date range:[/dim] {since_iso[:10]} → {until_iso[:10]}")
        console.print(f"[dim]Output dir:[/dim] {outdir_path}")
        console.print("\n[bold yellow]Collecting fresh data...[/bold yellow]")

        data = collect.collect_repository_data(
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            token=token,
            bots=bots,
            cache_dir=str(cache_dir),
            use_cache=not args.no_cache,
            on_prs_progress=None,
            on_commits_progress=None,
            on_ado_progress=None,
            ado_org=ado_org,
            ado_project=ado_project,
            ado_ready_states=ado_ready_states,
            ado_resolved_states=ado_resolved_states,
            ado_qa_failed_states=ado_qa_failed_states,
            ado_disable=ado_disable,
        )

        # Create dated subfolder for reports
        folder_name = report._fmt_folder_name(repo, until_iso)
        report_subdir = outdir_path / folder_name
        report_subdir.mkdir(parents=True, exist_ok=True)

        raw_out = report_subdir / f"dev-skill-raw-{tag}.json"
        with raw_out.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        console.print(f"[dim]Wrote refreshed raw data → {raw_out}[/dim]")

        console.print("\n[bold yellow]Computing metrics and scores...[/bold yellow]")
        scores = metrics.compute_scores(
            data=data,
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            config=cfg,
        )

        console.print("[bold magenta]Generating reports...[/bold magenta]")
        report.generate_reports(
            scores=scores,
            outdir=str(outdir_path),
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            named=not getattr(args, "anonymous", False),
            tag=tag,
        )
        insights.generate_insights(
            scores=scores,
            data=data,
            outdir=str(outdir_path),
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            small_pr_threshold=float(((cfg.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300),
            tag=tag,
        )
        _print_terminal_summary(
            console=console,
            scores=scores,
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            named=not getattr(args, "anonymous", False),
        )
        console.print(f"\n[bold green]✓ Reports regenerated in {report_subdir}[/bold green]")
        return 0

    # Handle rerun-local subcommand: reuse last raw JSON without refetching
    if getattr(args, "command", None) == "rerun-local":
        console = Console()
        console.print("[bold magenta]Regenerating from existing raw data (no refetch)[/bold magenta]")

        raw_path = _resolve_raw_path(getattr(args, "raw", None), args.outdir)
        if not raw_path:
            console.print("[red]No dev-skill-raw-*.json found.[/red]")
            console.print("Hint: pass --raw PATH or --outdir DIR to search in.")
            return 2
        if not raw_path.exists():
            console.print(f"[red]Raw file not found:[/red] {raw_path}")
            return 2

        try:
            with raw_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            console.print(f"[red]Failed to read raw JSON:[/red] {e}")
            return 2

        owner = data.get("owner")
        repo = data.get("repo")
        since_iso = data.get("since")
        until_iso = data.get("until")
        if not all([owner, repo, since_iso, until_iso]):
            console.print("[red]Raw JSON missing required fields (owner, repo, since, until).[/red]")
            return 2

        cfg = _load_config(args.config)

        # Determine output directory and tag
        outdir_path = Path(args.outdir) if args.outdir else raw_path.parent
        outdir_path.mkdir(parents=True, exist_ok=True)

        tag = args.tag or _derive_tag_from_raw_filename(raw_path) or f"{owner}-{repo}-{dt.datetime.utcnow():%Y-%m-%d}"

        console.print(f"[dim]Using raw:[/dim] {raw_path}")
        console.print(f"[dim]Output dir:[/dim] {outdir_path}")
        console.print("\n[bold yellow]Computing metrics and scores...[/bold yellow]")

        scores = metrics.compute_scores(
            data=data,
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            config=cfg,
        )

        # Create dated subfolder for reports
        folder_name = report._fmt_folder_name(repo, until_iso)
        report_subdir = outdir_path / folder_name
        report_subdir.mkdir(parents=True, exist_ok=True)

        raw_out = report_subdir / f"dev-skill-raw-{tag}.json"
        with raw_out.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        console.print(f"[dim]Wrote raw data → {raw_out}[/dim]")

        console.print("[bold magenta]Generating reports...[/bold magenta]")
        report.generate_reports(
            scores=scores,
            outdir=str(outdir_path),
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            named=not getattr(args, "anonymous", False),
            tag=tag,
        )
        insights.generate_insights(
            scores=scores,
            data=data,
            outdir=str(outdir_path),
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            small_pr_threshold=float(((cfg.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300),
            tag=tag,
        )
        _print_terminal_summary(
            console=console,
            scores=scores,
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            named=not getattr(args, "anonymous", False),
        )
        console.print(f"\n[bold green]✓ Reports regenerated in {report_subdir}[/bold green]")
        return 0

    # Interactive mode: if no repo-url is provided
    if args.repo_url is None:
        print("=" * 80)
        print("DevSkill Assessment - Interactive Mode")
        print("=" * 80)
        
        # Prompt for authentication
        try:
            token = _prompt_authentication()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        
        # Prompt for repository selection
        owner, repo = _prompt_repo_selection(token)
        
        # Prompt for other parameters
        days = _prompt_days()
        
        # Default output directory inside the repository workspace
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        default_outdir = str(Path.cwd() / "reports")
        outdir = _prompt_output_dir(default_outdir)
        config_path = _prompt_config_file()
        
        print("\n" + "=" * 80)
        print("Starting assessment...")
        print("=" * 80 + "\n")
    else:
        # CLI mode: use provided arguments
        owner, repo = parse_repo_input(args.repo_url.strip())
        try:
            token = _get_github_token()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        
        days = args.days if args.days is not None else 90
        
        # Default output directory inside the repository workspace
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        default_outdir = str(Path.cwd() / "reports")
        outdir = args.outdir if args.outdir is not None else default_outdir
        config_path = args.config

    # Calculate date range (reuse 'now' from interactive mode if already set)
    if 'now' not in locals():
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    since_iso, until_iso = since.isoformat(), now.isoformat()

    cfg = _load_config(config_path)
    bots = list(cfg.get("bots", []))
    ado_cfg = cfg.get("ado") or {}
    ado_org = args.ado_org or ado_cfg.get("org")
    ado_project = args.ado_project or ado_cfg.get("project")

    def _states(value: Optional[str], fallback: Any) -> List[str]:
        if value:
            return [s.strip() for s in value.split(",") if s.strip()]
        return list(fallback or [])

    ado_ready_states = _states(args.ado_ready_states, ado_cfg.get("ready_states"))
    ado_resolved_states = _states(args.ado_resolved_states, ado_cfg.get("resolved_states"))
    ado_qa_failed_states = _states(args.ado_qa_failed_states, ado_cfg.get("qa_failed_states"))
    ado_disable = bool(args.ado_disable or not (ado_org and ado_project))

    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)

    cache_dir = Path.cwd() / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    console = Console()
    console.print(f"[bold cyan]Collecting GitHub data for {owner}/{repo}[/bold cyan]")
    console.print(f"[dim]Date range: {since_iso[:10]} to {until_iso[:10]}[/dim]\n")
    
    # Set up Rich progress display
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        # Create tasks for PRs and commits
        pr_task = progress.add_task("[cyan]Fetching pull requests...", total=None)
        commit_task = progress.add_task("[green]Fetching commits...", total=None)
        ado_task = progress.add_task("[yellow]Fetching ADO tickets...", total=None)
        
        # Define progress callbacks
        def on_prs_progress(status: str, count: int):
            if status == "fetching":
                progress.update(pr_task, description=f"[cyan]Fetching pull requests... ({count} fetched)")
            elif status == "complete":
                progress.update(pr_task, completed=100, total=100, description=f"[cyan]✓ Pull requests fetched ({count} total)")
            elif status == "cached":
                progress.update(pr_task, completed=100, total=100, description=f"[cyan]✓ Pull requests (cached: {count} total)")
        
        def on_commits_progress(status: str, count: int):
            if status == "fetching":
                progress.update(commit_task, description=f"[green]Fetching commits... ({count} fetched)")
            elif status == "complete":
                progress.update(commit_task, completed=100, total=100, description=f"[green]✓ Commits fetched ({count} total)")
            elif status == "cached":
                progress.update(commit_task, completed=100, total=100, description=f"[green]✓ Commits (cached: {count} total)")

        def on_ado_progress(status: str, count: int):
            if status == "fetching":
                progress.update(ado_task, description=f"[yellow]Fetching ADO tickets... ({count} fetched)")
            elif status == "complete":
                progress.update(ado_task, completed=100, total=100, description=f"[yellow]✓ ADO tickets fetched ({count} total)")
            elif status == "cached":
                progress.update(ado_task, completed=100, total=100, description=f"[yellow]✓ ADO tickets (cached: {count} total)")
            elif status == "disabled":
                progress.update(ado_task, completed=100, total=100, description="[yellow]ADO tickets disabled")
            elif status == "skipped":
                progress.update(ado_task, completed=100, total=100, description="[yellow]✓ ADO tickets skipped (no references)")
        
        # Collect data with progress callbacks
        data = collect.collect_repository_data(
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            token=token,
            bots=bots,
            cache_dir=str(cache_dir),
            use_cache=not args.no_cache,
            on_prs_progress=on_prs_progress,
            on_commits_progress=on_commits_progress,
            on_ado_progress=on_ado_progress,
            ado_org=ado_org,
            ado_project=ado_project,
            ado_ready_states=ado_ready_states,
            ado_resolved_states=ado_resolved_states,
            ado_qa_failed_states=ado_qa_failed_states,
            ado_disable=ado_disable,
        )
    
    console.print()

    tag = f"{owner}-{repo}-{now:%Y-%m-%d}"
    
    # Create dated subfolder for reports
    folder_name = report._fmt_folder_name(repo, until_iso)
    report_subdir = outdir_path / folder_name
    report_subdir.mkdir(parents=True, exist_ok=True)
    
    raw_path = report_subdir / f"dev-skill-raw-{tag}.json"
    with raw_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    console.print(f"[dim]Wrote raw data → {raw_path}[/dim]")

    console.print("\n[bold yellow]Computing metrics and scores...[/bold yellow]")
    scores = metrics.compute_scores(
        data=data,
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        config=cfg,
    )

    console.print("[bold magenta]Generating reports...[/bold magenta]")
    report.generate_reports(
        scores=scores,
        outdir=str(outdir_path),
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
        outdir=str(outdir_path),
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        small_pr_threshold=float(((cfg.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300),
        tag=tag,
    )
    _print_terminal_summary(
        console=console,
        scores=scores,
        owner=owner,
        repo=repo,
        since_iso=since_iso,
        until_iso=until_iso,
        named=True,
    )
    console.print(f"\n[bold green]✓ Reports written to {report_subdir}[/bold green]")
    return 0


def rerun_entrypoint() -> None:
    """Console script entrypoint: rerun last query with fresh data."""
    sys.exit(main(["rerun"] + sys.argv[1:]))


def rerun_local_entrypoint() -> None:
    """Console script entrypoint: regenerate from local raw data only."""
    sys.exit(main(["rerun-local"] + sys.argv[1:]))


