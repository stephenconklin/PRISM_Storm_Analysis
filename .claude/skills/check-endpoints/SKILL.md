---
name: check-endpoints
description: Check that the external services PRISM Storm Analysis depends on (PRISM, USGS WBD/Water Data/NLDI, NID, RCC-ACIS, NRCS AWDB) still answer as the code expects, and diagnose a failing one. Use when a script fails with a network error, before relying on an endpoint, or when the user asks whether the services are up.
---

# Check the external services

1. `conda run -n prism-storm-analysis python scripts/check_endpoints.py` (or `--only <name> ...`).
   One light request per service; nothing counts toward a download limit. It makes one USGS request of
   the 1,000/hour allowed, so don't loop it.
2. All `ok`: the problem is elsewhere; read the failing step's log in `<out>/run/logs/`.
3. A `FAIL`:
   - Connection errors or timeouts on one host: likely an outage; check again later before changing code.
   - An HTTP error, or `unexpected reply`: the service may have changed. Fetch the endpoint once by hand
     (curl or WebFetch) and compare with what `scripts/observations.py`, `scripts/prism_watershed.py` or
     `scripts/storm_context.py` expects. PRISM's syntax changed on 1 Oct 2025 (CLAUDE.md lists the
     current URLs); USGS replaced NWIS with the Water Data API.
   - `CERTIFICATE_VERIFY_FAILED` from services.nacse.org: truststore isn't active (never use `verify=False`).
4. When you confirm an endpoint's current behavior, update the "checked <date>" notes in CLAUDE.md and
   the docstrings of the module that uses it. If the code needs changing, keep network code in
   `observations.py` (or the script's fetch function) so the offline tests still cover the processing.
