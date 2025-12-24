# Dev Skill Assessment CLI

Run a 90-day GitHub developer assessment against any repository and export Markdown and JSON reports.

## What the Dev Skill assessment measures

- Purpose: quickly understand delivery, collaboration, hygiene, and stability signals for developers and teams over a recent window (defaults to 90 days).
- Data inputs: GitHub pull requests, reviews, and commits pulled via GitHub CLI or `GITHUB_TOKEN`, with optional aliases/exclusions/bot filters from `config.yml`.
- Scoring: weighted pillars (delivery, collaboration, hygiene, stability) produce per-developer subscores and a team average; weights and thresholds are configurable.
- Outputs: raw JSON, scores/insights JSON, and Markdown reports saved under `--outdir` (e.g., `dev-skill-assessment-*.md` and `dev-skill-assessment-insights-*.md`); you can rerun the last query with `dev-skill-rerun` or regenerate offline from raw data with `dev-skill-rerun-local`.

## Install

```bash
pip install git+https://github.com/Benjamindodgson/dev-skill-assessment.git
```

## Run locally

From a cloned checkout, install in editable mode and start the interactive CLI:

```bash
pip install -e .
devskill
```

## Usage

### Interactive Mode (Recommended)

Simply run devskill without any arguments for an interactive experience:

```bash
devskill
```

The tool will guide you through:
1. **Authentication** - Automatically checks if you're already authenticated with GitHub CLI or `GITHUB_TOKEN`
2. **Repository Selection** - Lists all repositories you have access to (owned, collaborated, or organization repos)
3. **Analysis Period** - Prompts for number of days to analyze (default: 90)
4. **Output Directory** - Where to save reports (default: `reports`)
5. **Config File** - Optional configuration file path

### CLI Mode (For Automation)

For scripting and CI/CD, use command-line arguments:

```bash
devskill \
  --repo-url https://github.com/owner/repo \
  --days 90 \
  --outdir reports \
  --config config.yml
```

**Authentication:** The tool uses GitHub CLI authentication by default. For CI/automation, you can set `GITHUB_TOKEN` environment variable instead. Run `gh auth login` to authenticate if needed.

**CLI Arguments:**
- `--repo-url`: accepts `owner/repo`, HTTPS, or SSH GitHub URLs
- `--days`: time window (default 90)
- `--outdir`: output directory (default `reports`)
- `--config`: optional YAML for aliases/bots/weights (see `config.yml`)
- `--no-cache`: disable local API response caching
- Azure DevOps assessment (optional):
  - `--ado-org`: Azure DevOps org (e.g., `ecolabcommercialsolutions` or `https://dev.azure.com/ecolabcommercialsolutions`)
  - `--ado-project`: Azure DevOps project name (e.g., `Pest Commercial Solutions`)
  - `--ado-ready-states`: comma-separated Ready-for-Dev state names (default from config)
  - `--ado-resolved-states`: comma-separated resolved/closed state names (default from config)
  - `--ado-qa-failed-states`: comma-separated QA Failed state names (default from config)
  - `--ado-disable`: skip Azure DevOps enrichment even if org/project provided
  - `--azure-exclude-emails`: comma-separated assignee emails to exclude from Azure assessment

**Azure DevOps assessment:** When `ado.org` and `ado.project` are configured (or passed as flags), `devskill` fetches all project work items (and their updates) in the window, plus iteration dates. It computes a separate Azure assessment (Resolution Time, Predictability, Velocity, Quality) with configurable weights (see `config.yml` under `azure`). These scores do **not** change DevSkill (GitHub) scores; they are shown separately in the terminal and embedded into `dev-skill-raw-*.json` alongside the raw Azure payload.
Resolution now uses SLA-based scoring (configurable at `azure.resolution_sla`):
- Business-day timing (Mon–Fri, configurable) for duration calculations.
- Bug/defect types use priority SLAs expressed in hours (defaults: Blocker 24, Critical 24, Major 72, Minor 120, Trivial 240) as pass/fail.
- All other items (e.g., PBIs) use hourly buckets (defaults: ≤48h=100, ≤72h=75, ≤96h=50, ≤120h=25, >120h=0).
Unassigned work items are skipped, and you can exclude specific assignee emails via `azure.exclude_emails` in `config.yml` or `--azure-exclude-emails`.

Prereqs: Azure CLI with Azure DevOps extension (`az extension add --name azure-devops`) and an authenticated session (`az devops login` with PAT or `az login`). No enrichment occurs if org/project are omitted or `--ado-disable` is set.

Outputs include repository and date in filenames, e.g. `dev-skill-assessment-OWNER-REPO-YYYY-MM-DD.md` and `dev-skill-assessment-insights-OWNER-REPO-YYYY-MM-DD.md`.
`dev-skill-raw-*.json` now also embeds `devskill_scores` and (when configured) `azure_assessment` plus the raw Azure work item payload under `azure`.

### QA Assessment (Azure DevOps) — qaskill

Run a 90-day QA assessment using Azure DevOps test runs/results and bugs. Requires Azure CLI and Azure AD login (no PAT needed):

```bash
qaskill
```

The tool will guide you through:
1. Organization prompt (e.g., `YourOrg` or `https://dev.azure.com/YourOrg`)
2. Azure AD sign-in if needed (`az login`)
3. Project selection from your accessible projects
4. Days to analyze (default: 90), QA users confirm, and output directory

CLI mode:

```bash
qaskill \
  --az-org YourOrg \
  --az-project MyProject \
  --days 90 \
  --qa-users "qa1@company.com,qa2@company.com" \
  --outdir reports \
  --config config.yml
```

Authentication:
- Uses Azure AD access token from `az account get-access-token` (resource `Azure DevOps`).
- For automation, you can set `AZDO_AUTH_TOKEN` to an existing Azure AD bearer token.

Outputs include project and date in filenames, e.g. `qa-skill-raw-ORG-PROJECT-YYYY-MM-DD.json`, `qa-skill-scores-*.json`, `qa-skill-assessment-*.md`, and `qa-skill-assessment-insights-*.md`.

Rerun from last raw data:

```bash
qaskill rerun
qaskill rerun --outdir reports
qaskill rerun --raw reports/qa-skill-raw-ORG-PROJECT-YYYY-MM-DD.json --anonymous
qaskill rerun --tag custom-tag
```

### Rerun commands

`dev-skill-rerun` replays the most recent query window with fresh data using metadata from the latest `dev-skill-raw-*.json` (or one you pass with `--raw`):

```bash
# Fetch fresh GitHub/ADO data for the same owner/repo window
dev-skill-rerun

# Point at a specific raw file, override output tag, anonymize names
dev-skill-rerun --raw reports/dev-skill-raw-OWNER-REPO-YYYY-MM-DD.json --tag custom-tag --anonymous
```

`dev-skill-rerun-local` preserves the previous behavior: regenerate reports from existing raw JSON without calling GitHub/ADO.

```bash
# Regenerate from the latest local raw data
dev-skill-rerun-local

# Regenerate from a specific raw file and output directory
dev-skill-rerun-local --raw reports/dev-skill-raw-OWNER-REPO-YYYY-MM-DD.json --outdir reports --config config.yml --anonymous
```

### Fork this repo (recommended)

To keep your results organized in this project and save outputs under `./reports`:

```bash
gh repo fork Benjamindodgson/dev-skill-assessment --clone
cd dev-skill-assessment
pip install -e .
devskill
```

The default output directory is `./reports`. You can override with `--outdir`.

## Config YAML (optional)

See `config.yml` for a complete configuration example. Basic structure:

```yaml
# config.yml
repo: owner/repo            # optional override; CLI --repo-url has precedence
aliases:                    # map alternate logins to canonical names
  jdoe-alt: jdoe
exclude:                    # developer logins to exclude from scoring
  - bot-account
bots:                       # logins treated as bots and filtered out
  - dependabot
weights:                    # area weights (0..1)
  delivery: 0.30
  collaboration: 0.25
  hygiene: 0.15
  stability: 0.30
hygiene:
  small_pr_lines_threshold: 300

# QA assessment configuration (Azure DevOps)
qa:
  users:
    - qa1@company.com
    - qa2@company.com
  aliases:
    qa1@company.com: qa1
    "Smith, Alex": alex.smith@company.com
  exclude:
    - service.account@company.com
  weights:
    testing: 0.30
    defects: 0.30
    hygiene: 0.20
    responsiveness: 0.20
```

## Example Reports

See the `examples/` directory for sample output reports generated from a real repository.

## License

MIT


