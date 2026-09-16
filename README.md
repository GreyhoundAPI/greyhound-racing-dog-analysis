# greyhound-racing-dog-analysis

Does the race field pick winners, and did the price already know?

A command-line lab built on [GreyhoundAPI](https://greyhoundapi.com). One call to [`/v1/races/{race_id}/field`](https://greyhoundapi.com/documentation/race-field) returns every dog in a race with its measurements beside it: early pace at this track and trip, its record here, the strike rate of the trap it drew, its record at the grade, and how it has run off its current break. The endpoint measures and never forecasts. This lab asks whether those measurements forecast anyway, and whether the Betfair market got there first.

## The premise

**Stage one: the starting line audit.** Every race is ranked on each measurement (pace, record here, record at the grade, layoff), and the top-ranked dog's wins are counted against its fair share of the field. The trap figure is the same for any dog drawn in that box, so ranking on it ranks the draw and nothing else. It is the control: the other measurements have to beat it, in each region and period after period, not only in the total.

**Stage two: is it in the price?** Within the same price band, does the dog topping a measurement win more often than its price implies? Bets settle at the Betfair back price captured before the off, with commission as a setting and SP alongside. Betfair lay prices exist from 2 September 2026.

**The thin line.** The endpoint names the runners short of history. Both stages split their figures by how many runners in the race it names, which shows when the field is too thin to trust at all.

If stage two finds nothing, that is the finding: the market already reads the starting line, and the code and data are here for anyone to check.

## Status

Phases 1 to 4 are built: the lookahead probe, the design of the analysis screens, the data layer behind `analyse`, and stage one. Stage two is next.

## The lookahead probe

Before anything is counted, one question has to be settled. A backtest replays races that have already run, so every figure it uses must be the figure as it stood before the off. `/v1/races/{race_id}/form` is cut at the off and says so in `meta.form_cutoff`. `/field` stamps `form_state_computed_utc` and rebuilds its form state nightly. If a race from June answers with last night's state, its record here and its layoff contain the June race itself and everything since, and a backtest on it would measure its own lookahead.

The probe settles it from the public endpoints alone. For completed races spread from 2 to 540 days back it reads four figures per dog from `/field`: `here.runs`, `at_this_grade.runs`, `pace.runs` and `layoff.days_since_last_run`. It recounts each one from the dog's own history, `/v1/dogs/{dog_id}/form`, in two ways:

1. **Before the off:** runs dated before the race (at this track and distance, at this grade, or with an early sectional here), and the gap back to the previous run. The gap is cross-checked against `/form`, which is cut at the off.
2. **At the build:** the same counts up to the day `form_state_computed_utc` was built, and the gap from the latest run before the build.

A figure cut at the off matches the first. A figure served from current state matches the second. Each figure is judged on its own, because one endpoint can serve both kinds. A race today that has not run yet is probed as a control: there both readings agree, so it shows, figure by figure, whether the recount counts what the endpoint counts. It prefers a race of 400m or more, because sprint trips often carry no early sectionals, and a figure that is zero across the whole card is reported as untested.

## Requirements

- Python 3.8 or newer. Standard library only, nothing to install.
- A GreyhoundAPI **Live Plus** key. `/field` and `/form` are Live Plus endpoints. Any other key is told so after the free `/v1/usage` check, before a counted call is spent. See [pricing](https://greyhoundapi.com/pricing).

## Run it

From this folder:

```
python3 -m doganalysis probe
```

The first run asks for your key once (the input is hidden), checks it on `/v1/usage` and saves it to `config.json`, readable by your user only. Pick a region with the arrow keys and the probe runs end to end: about 80 calls for the default eight races and the control, paced at 85% of your key's own per-minute limit.

- `--region GB|AU|BOTH` skips the region menu.
- `--races N` samples N races per region, from 1 to 24 (default 8).
- `--resample` picks new races. Otherwise a rerun with the same `--races` probes the same races, so a before and after comparison means something.
- `--no-control` skips today's control race.
- `--json` prints only the JSON report and never prompts. Every setting comes from the command line, and a missing key is named on stderr.
- `--key KEY` uses a key for one run without saving it. `GAPI_KEY` in the environment does the same.

`python3 -m doganalysis key` replaces the saved key, and `python3 -m doganalysis` on its own opens a menu.

Exit codes: `0` when the probe finishes, whatever the verdict; `2` for a usage problem or a missing key; `3` when the key is refused or its plan cannot reach `/field`; `130` when stopped with Ctrl+C.

## Reading the output

Every line starts with the clock and the time since the run began. Each race prints one row per dog as its history arrives, with three columns under each of the four figures:

- **field** is what `/field` says, coloured by how it reads: green when it matches the recount cut at the off, red when it matches the build, blue when both readings give the same value, amber when it only comes near one of them.
- **off** is the recount cut at the off.
- **built** is the recount up to the state's build date. It shows as a range when runs on the build day itself could fall either side.

Under each race, one line gives every figure's read and the runners behind it, for example `here LEAKS 5/5`. Only exact matches vote. A runner the recount can only come near is shown as `near` and left out, so a scope the recount cannot see never decides a figure. Across the run, a figure is called clean or leaking when at least 90% of the exact reads agree, and at least five are needed.

The verdict at the end is one of:

- **CLEAN**: every figure checked is cut at the off, so `/field` can be backtested directly.
- **LEAKS**: every figure checked is served from current form state, so the figures include the race itself and later runs.
- **SPLIT**: some figures are cut at the off and others leak. The verdict names which, and only the figures cut at the off can be backtested as served.
- **MIXED**: a figure reads clean on some runners and leaks on others.
- **UNCLEAR**: too few runners separated the two readings. Read the control line first.
- **REFUSED**: `/field` would not answer for races that have run.

The state's build time on its own decides nothing: an endpoint can be rebuilt after a race and still cut a figure at the off. Only the recounts decide.

## What it saves

Everything stays inside this folder:

- `config.json`: your key, mode 600.
- `data/probe/sample-GB.json` (and `-AU`): the races the probe uses, reused on the next run.
- `data/probe/runs/<timestamp>/`: every raw response the verdict rests on (`field-`, `form-` and `dog-form-` files per region) plus `report.json`, the same document `--json` prints, stating the criteria it was run with.

Folders under `data/` are created at mode 700. If this folder sits inside a web root, run the tool as the folder's owner, never as the web server's user.

`GAPI_BASE_URL` points the tool at a different API root, for testing against a stand-in server.

## The analysis run

```
python3 -m doganalysis analyse
```

It opens with the same key panel as the probe, then spends three calls checking that `/field` is cut at the off on races from across the archive, by reading `meta.form_cutoff`. If any of them is not, it stops before fetching anything, because a backtest on those figures would be reading its own lookahead.

Then it asks for the region, the range and the commission, counts the races in the range, and shows what the fetch will cost before a single race is fetched: days already cached, calls, the share of your month, the time at your pace and the disk it needs. Nothing is fetched until you say so.

The fetch reads one results page a day and one `/field` call a race, newest day first, one line a day. Handicaps are skipped, since both stages leave them out. A day is complete once its results are final and every field has landed, and a complete day is never fetched again. A day with a failed call is saved as open, and the next run fetches only what it is missing. Ctrl+C is safe at any point.

- `--region GB|AU|BOTH` skips the region menu. Both regions run one after the other and are never pooled.
- `--last N`, or `--from YYYY-MM-DD` with an optional `--to` (yesterday when left out), skips the range menu.
- `--commission PCT` sets the commission on winnings, 5 when left out.
- `--yes` fetches once the cost panel is shown. Outside an interactive terminal, a run without it shows the panel and stops.
- `--key KEY` works as it does for the probe.

Once the range is cached, stage one runs on it without a single call. Every complete, non-handicap race with one winner, at least four finishers and a field cut at the off is ranked on each measurement: pace by the quickest average early split, the others by the highest win percentage, counting only runners with six runs behind that figure. The top dog's wins, with tied top dogs sharing the credit, are set against its fair share (one in the number of finishers) with a chi-squared test, and against the trap pick on exactly the same races, month by month. A measurement beats the trap pick when its top dog wins more often overall, clears p < 0.001 and is ahead in at least 75% of the months with 50 or more ranked races. Nothing is called from fewer than 1,000 races across three such months. The thin line splits the same races by how many runners `/field` names as thin. Choosing Analyse what is cached at the cost panel reruns stage one on the cache alone.

The cache is `data/cache/<region>/<year>/<date>.json.gz`: one compressed file a day holding that day's results, every field and the state of the day. `data/cache/<region>/index.json` sums it up, so the cost panel never has to open the archive. Every run writes its settings, its fetch totals and every stage one figure, with the criteria behind them, to `data/runs/<timestamp>-<region>/run.json`.

## Rules that keep the lab honest

1. Nothing is fitted in either stage. Every figure is a count, so there is no training window to leak into. The only lookahead risk is in the data itself, which is why the probe runs first.
2. A dog is ranked on a measurement only when it clears a stated minimum of runs, the endpoint's own six-run line. Below that it sits in the thin segment.
3. Handicaps are out. Void and abandoned races are counted and reported as excluded, never silently dropped.
4. A race counts once, however the periods it falls in overlap.
5. Significance is a chi-squared test at p < 0.001, never a spread measured against one standard error.
6. GB and AU are selectable and never pooled.

## Roadmap

1. The lookahead probe (built)
2. Design of the analysis screens (done)
3. Data layer: races day by day, cached under `data/` so nothing is fetched twice, resumable (built)
4. Stage one, the starting line audit (built)
5. Stage two, the price
6. JSON output for both stages
7. A Labs page on greyhoundapi.com

## Licence

MIT, see `LICENSE`. GreyhoundAPI publishes factual racing data only. Nothing in this repository is a prediction, a tip or betting advice.
