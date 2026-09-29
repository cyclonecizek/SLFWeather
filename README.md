# 7-Day Constraint Probabilities

Hourly-updated chance of each 7-Day product constraint (precip within 30 nm,
lightning within 30/10 nm, crosswinds, head/tailwinds, visibility, RH > 98 %, plus
high/low) for every local window, built from HRRR, the HREF member models,
NAM 3 km, NBM, NDFD and the ECMWF, AIFS, GEFS, ICON and GEM ensembles.
The page exports a CSV that the OTV 7-Day workbook imports.

## Set up

1. Push this folder to a new GitHub repository.
2. Settings > Pages: deploy from branch `main`, folder `/docs`.
3. Settings > Actions > General > Workflow permissions: Read and write.
4. Actions > Update constraint board > Run workflow. Read the probe job's
   log to confirm each source and field resolved. The first run takes the
   longest (roughly 15-25 minutes); later runs only fetch new model cycles.

`docs/data/board.json` and `constraints.csv` ship with demo data until then.

## Getting the numbers into Excel

- Page button **Download CSV for Excel** saves `Constraint_Plume.csv` using the
  sources currently switched on.
- The hourly job also publishes `docs/data/constraints.csv` with every source.
- In the workbook: Data > Refresh All, pick the NBM file when asked, then pick
  `Constraint_Plume.csv`. To load only the plume CSV: Data > Queries &
  Connections > PLUME_CSV > Refresh.
- 7-Day Forecast!B25 switches guidance between Plume and NBM. Overrides
  (yellow rows) always win.

## Your window

The "Your window" card takes any start and end time (local) and gives the
chance of breaking each constraint during it, plus the chance of breaking any
checked constraint. The window is saved in the page link, so it can be shared.
A member counts toward "any" if it can judge every checked non-lightning
constraint;
sources that can't are listed under the result. Lightning rows only use
models with explicit lightning output (hi-res LTNG, NBM thunder probability)
and visibility uses NBM's probability below its lowest threshold plus ECMWF HRES
judged against that same threshold, so "any"
is never below those rows when they are checked.

## Wind CSVs (stand-in for the GSL NBM file)

Each hourly run also publishes two files in the same layout as the GSL 1D-viewer
NBM point CSV, so the workbook's normal NBM import reads them with no changes:

- `docs/data/KTTS_NBM_wind.csv`: NBM alone (from Open-Meteo, not GSL).
- `docs/data/KTTS_ENS_wind.csv`: every source blended with the board's weights.
  Wind and gust are the weighted median, direction is the weighted mean wind
  vector, and the gust / wind / RH percentile columns come from all members.

Both have hourly rows from the current hour out to the end of the board, with
wind and gust in m/s, RH in % and temperature in K. They carry wind, RH and
temperature only, so the workbook's NBM status reads "some 7-Day guidance
fields missing"; keep 7-Day Forecast!B25 on Plume for those rows. The page's
**Wind CSV** buttons download the published files. Name and NBM source id are
set under `wind_csv` in `config.yaml`.

## Change limits or sources

`config.yaml`: site, runway, limits, radii, thresholds, window times, source
weights. Any source with `enabled: false` is skipped.

## Notes

- Window chance counts a member once if any hour it has in the window breaks
  the limit, so it runs higher than the highest single-hour chance.
- The window math lives in both `pipeline/windows.py` and `docs/index.html`
  (`windowTable`). Change both together.
- Open-Meteo sources refresh every 3 hours (`openmeteo_refresh_hours`) to stay
  inside the free call limit. Each pull covers 13 points (the site plus rings
  at 10 and 25 nm).
- NOMADS requests are capped at 90 per minute, with one multi-range request
  per GRIB file.
- The HREF member models and NAM retire when RRFS/REFS go operational; those
  sources will then show "missing".
