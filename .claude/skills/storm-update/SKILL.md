---
name: storm-update
description: Bring an existing PRISM storm analysis folder up to date with PRISM revisions, later or approved USGS flow, and rerun the analysis, then report what changed. Use when the user asks to refresh, update, or check an existing storm folder.
---

# Update a storm folder

1. **Status first (no downloads):** a folder made before 7 Nov 2026 (outputs at its top level) must be
   migrated first; every command says so. `pipeline.py migrate --out <DIR> --dry-run` lists the moves
   (offline, renames only); show the user, then run it without `--dry-run`.

   
   `conda run -n prism-storm-analysis python scripts/pipeline.py status --out <DIR>`
   It lists days PRISM has revised (from its release service, which doesn't count toward the download
   limit), outputs made from older releases, gauges whose flow window was cut short or is provisional,
   and the next command. If it says there is nothing to do, stop and tell the user.
2. **Confirm with the user** before updating when it will re-download PRISM files: each re-download counts
   toward PRISM's limit of 2 per file per 24 h per IP. Say how many files.
3. **Update:** `conda run -n prism-storm-analysis python scripts/pipeline.py update --out <DIR> --yes`
   (run in the background for large basins). It refreshes ppt and tmean, reruns every event, keeps the
   previous report in `events/<event>/report/history/`, and prints per-event changes.
4. **Report the changes** it printed (volume and range, releases, gauge bias, runoff ratios, snow check),
   with the releases now in use and the date final releases are expected (`status` prints it).
5. Exit 3 means some steps are incomplete (often the USGS rate limit): rerun the same `update` later.
   For network errors use `/check-endpoints`.

For a recurring check, the README's `pipeline.py` section has a launchd example that runs
`update --check --notify` weekly. Don't install it without asking.
