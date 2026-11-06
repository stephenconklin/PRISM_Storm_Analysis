---
name: report-check
description: Check the HTML storm report's layout by rendering it in headless Chrome in light and dark mode and looking at the screenshots. Use after changing scripts/report.py charts, CSS or layout.
---

# Check the storm report visually

1. Make a report to look at. With no real folder at hand, build a synthetic one offline from the tests'
   helpers (as `tests/test_report.py` does) in the scratchpad, then
   `conda run -n prism-storm-analysis python scripts/report.py --out <DIR>`.
2. Screenshot it in both themes (write the PNGs to the scratchpad, not the repo):
   ```
   C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
   "$C" --headless=new --disable-gpu --hide-scrollbars --window-size=900,2400 --screenshot=<scratch>/light.png "file://<report.html>"
   "$C" --headless=new --disable-gpu --hide-scrollbars --force-dark-mode --window-size=900,2400 --screenshot=<scratch>/dark.png "file://<report.html>"
   ```
   Also try a phone width (`--window-size=390,2400`).
3. Read both images. Check: labels don't overlap or clip, legends match the series, axis ticks are
   readable, colors are the dataviz skill's validated slots 1–3 (CLAUDE.md), text has contrast in dark
   mode, and sections without inputs show their "Not shown: run ..." note.
4. Fix and repeat. The report must stay self-contained (no external resources).
