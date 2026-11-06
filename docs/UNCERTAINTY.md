# Uncertainty of a storm's precipitation volume

`storm_total.py` reports a storm's volume as a value with a range and the components behind it:

```
Uncertainty: 4.685 km³, range 4.625-4.916 km³ (-1.3% to +4.9%; components added, see docs/UNCERTAINTY.md)
  gauges     -0.5% to +4.2%   all gauges (n=88) PRISM -4.0% -> volume +4.2%; independent gauges (n=7) PRISM +0.5% -> volume -0.5%. ...
  boundary   +0.0% to +0.0%   area-weighted +0.01% (any cell touched +1.68%, not used: it counts land outside the basin)
  revisions  -0.7% to +0.7%   window volume changed -0.72% from first recorded to latest releases; 8 of 8 days not final (release 2), ...
  products   not assessed     comparison with other precipitation products not built yet
  rain       4.521-4.573 km³ across ramps 0,3 (used) and -1,3, 1,1 °C; shifts water between rain and snow, not the total
```
(San Juan, HUC 1408, PRISM days Oct 20–27, 2026, release 2.) The same numbers are saved to
`<out>/ppt_<res>_uncertainty_<start>_<end>.json` and the run manifest, and the QGIS project shows the
range on the study basin label. Code: `scripts/uncertainty.py`.

## What the range means

It is a **bounding range**, not a confidence interval. Each component's range always includes 0 (no
change), so a component with one estimate, say +4.2%, contributes 0 to +4.2%. Each component says how far below and above the
reported volume one source of error could plausibly put it. The overall range adds the components'
lows and their highs. That is deliberately wide in one sense: the components are not independent
(PRISM is built from many of the same gauges used to check it) and PRISM's errors are spatially
correlated, so adding them in quadrature or treating them as random would be unjustified.

It is narrow in another sense: it covers only what has been measured. Gauge undercatch, products not
yet compared, and future PRISM revisions are not in it (see Limits). Read it as "the volume is at
least this uncertain", not "the true volume is inside this range".

## Components

### Gauges (station comparison, `validate_stations.py`)
Event totals at gauges in the basin and a 10 km buffer, for the same window (the component is skipped if
`validation/stations_summary.json` is for another window). If PRISM is *b*% below the gauges on average,
the gauges imply a volume of V / (1 + *b*/100). The component spans the volumes implied by:
- all gauges (most are PRISM inputs, so the comparison is *in-sample*: PRISM was fitted to them), and
- independent gauges (not PRISM inputs), when there are at least 5.

Scaling a point bias to the basin assumes the gauges represent the basin. They cluster in valleys
and towns; high terrain is represented mostly by SNOTEL. Gauges also catch less than falls, especially in
wind and snow (often 5–10%, more for snow), which would push the true volume higher than either estimate.

### Boundary (`storm_total.boundary_volumes`)
The cube counts a cell when its center is in the basin. Weighting each cell by the share of its area
inside the basin is the better estimate of the basin's water; the component is the difference. The
volume over every cell the basin touches is printed for reference but not used, because it adds land
outside the basin. Large basins: negligible (San Juan +0.01%). Small basins (a few hundred km² or less):
can be several percent.

### Revisions (`storm_total.revision_change`)
PRISM revises each daily grid up to 8 times over about 6 months. The component is the change in window
volume from each day's first recorded release to its latest, applied in both directions, since later
releases can move either way. While any day is below release 8 the component says so: the change so
far says little about changes still to come. With no revisions recorded yet and days not final, the
component is reported as not assessed. Refresh with `prism_watershed.py --refresh` and rerun.

Do not add an allowance for future revisions until several storms have been followed to release 8; the
first San Juan refresh moved single days by up to 55% (a near-dry day) and 19% (Oct 26), but the window by
only 0.7%.

### Products (not built)
Comparison with other gridded products (Stage IV radar/gauge, AORC, Daymet, gridMET; roadmap 1.3) is the
main missing component. Product spread is often the largest term in mountain basins.

### Rain volume (with a `tmean` cube)
The rain/snow split moves water between rain and snow; it does not change the total, so it is not in
the volume range. It is reported as a range of the rain volume across alternative ramps
(`--ramp-alternatives`, default −1 to 3 °C and a single 1 °C threshold). Use it for runoff ratios.
`validate_snow.py` checks the split against SNOTEL snow-water: for the San Juan, PRISM's snow was 1.16× the
observed snow-water gain with the default ramp (observed is a lower bound), 0.9× with −1 to 3 °C.

## Limits
- Not in the range: gauge undercatch, other products, future revisions, PRISM's own interpolation error
  away from gauges, and the 4-hour or more offsets between station and PRISM days.
- One storm. Thresholds and judgments here should be revisited as more storms are processed.
