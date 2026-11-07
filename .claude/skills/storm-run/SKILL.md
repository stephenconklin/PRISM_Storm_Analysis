---
name: storm-run
description: Set up and run a first PRISM storm analysis for a watershed and date range (find the HUC, pad the dates, write storm.toml, run the pipeline, summarize the results). Use when the user wants to analyze a new storm or basin.
---

# First run of a storm analysis

Everything goes through `scripts/pipeline.py`; don't chain the individual scripts by hand.
Run Python with `conda run -n prism-storm-analysis python ...` (see CLAUDE.md for why).

1. **Basin.** If the user gave a name, not a code, search first and confirm the choice with them when
   several units match:
   `conda run -n prism-storm-analysis python scripts/find_huc.py "<name>" [--state XX] [--digits 4 6 8 10]`.
   A polygon file works too (`--boundary`).
2. **Dates.** Ask for the storm's PRISM days if they aren't clear. A PRISM day ends 12:00 UTC on its date;
   `init` pads the download by `--pad` days (default 1) on each side. Each event gets a short name.
3. **Plan before downloading.** Write the settings without running, then check the plan:
   ```
   conda run -n prism-storm-analysis python scripts/pipeline.py init --huc <code> --start <d> --end <d> --event <name> [--out DIR]
   conda run -n prism-storm-analysis python scripts/pipeline.py run --out <DIR> --dry-run
   ```
   The dry run lists each step's command, checks the services, and estimates disk space. Tell the user
   how many PRISM files will be downloaded. PRISM allows each file only twice per 24 h per IP, so never
   delete the PRISM store's zips or rerun downloads to "test". National data goes to one store shared by
   every basin (`prism_store/` next to the folder by default; `prism_store` in storm.toml or `PRISM_STORE`
   to put it elsewhere), so a second basin over the same days downloads nothing.
4. **Run:** `conda run -n prism-storm-analysis python scripts/pipeline.py run --out <DIR>`. It can take
   minutes to hours (800 m national grids; USGS allows 1,000 requests/hour, more with `USGS_API_KEY`).
   Use `run_in_background` for long runs and wait for the notification rather than polling.
5. **If it stops:** exit 3 means some steps failed or hit a rate limit; rerun the same command later
   (finished steps are skipped). Exit 4 or a `network_error` step: run `/check-endpoints`. Read the
   step's log in `<DIR>/run/logs/<time>/` before guessing.
6. **Summarize** from the outputs, not from memory. Each event's are in `<DIR>/events/<event>/`:
   `totals/ppt_<res>_uncertainty_<start>_<end>.json` (volume and range),
   `validation/stations_summary_<start>_<end>.json` (bias), `validation/runoff_summary_<start>_<end>.csv`
   (runoff ratios), and `report/storm_report_<start>_<end>.html`.
   Say which PRISM releases the numbers come from and that they change for ~6 months.
