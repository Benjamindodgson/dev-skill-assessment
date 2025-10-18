# Dev Skill Assessment CLI

Run a 90-day GitHub developer assessment against any repository and export Markdown, CSV, and JSON reports.

## Install

```bash
pip install git+https://github.com/Benjamindodgson/dev-skill-assessment.git
```

## Usage

First, authenticate with GitHub CLI:

```bash
gh auth login
```

Then run the assessment:

```bash
devskill \
  --repo-url https://github.com/owner/repo \
  --days 90 \
  --outdir reports \
  --config config.yml
```

**Authentication:** The tool uses GitHub CLI authentication by default. For CI/automation, you can set `GITHUB_TOKEN` environment variable instead.

- `--repo-url`: accepts `owner/repo`, HTTPS, or SSH GitHub URLs
- `--days`: time window (default 90)
- `--outdir`: output directory (default `reports`)
- `--config`: optional YAML for aliases/bots/weights (see `example-config.yml`)
- `--no-cache`: disable local API response caching

Outputs include repository and date in filenames, e.g. `dev-skill-assessment-OWNER-REPO-YYYY-MM-DD.md` and `dev-skill-assessment-insights-OWNER-REPO-YYYY-MM-DD.md`.

## Config YAML (optional)

See `example-config.yml` for a complete configuration example. Basic structure:

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
```

## Example Reports

See the `examples/` directory for sample output reports generated from a real repository.

## License

MIT


