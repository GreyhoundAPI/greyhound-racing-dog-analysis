"""python3 -m doganalysis analyse: the ground check, the range, what it will cost, then the fetch into the cache."""

from datetime import date, datetime, timedelta, timezone

from . import TOOL, __version__, fetch, probe, stage_one, store
from .api import ApiError
from .config import DATA, load_json, private_dir, relative, write_json

EXCHANGE_START = date(2025, 3, 1)
PRETEST_DAYS = 7
DAILY_RACES = {"GB": 133, "AU": 100}
COUNT_WINDOW_DAYS = 366
DEFAULT_COMMISSION = 5.0


def today_utc():
    return datetime.now(timezone.utc).date()


def nice(day):
    return "%d %s %d" % (day.day, day.strftime("%b"), day.year)


def num(value):
    return "{:,}".format(int(value))


def duration(minutes):
    if minutes < 1:
        return "under a minute"
    minutes = int(round(minutes))
    if minutes < 90:
        return "about %d minute%s" % (minutes, "" if minutes == 1 else "s")
    return "about %dh %02dm" % (minutes // 60, minutes % 60)


def size_text(size):
    if size >= 1024 * 1024:
        return "%d MB" % max(1, int(round(size / 1048576.0)))
    return "%d KB" % max(1, int(round(size / 1024.0)))


def parse_day(text):
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# the ground check


def ground_check(ui, client, region):
    """Three races from across the archive: /field must say its figures stop at the race's own off."""
    picks, calls = [], 3
    for name in (region, "GB", "AU"):
        saved = load_json(DATA / "probe" / ("sample-%s.json" % name))
        races = (saved or {}).get("races") or []
        if len(races) >= 3:
            picks = [races[0], races[len(races) // 2], races[-1]]
            break
    if not picks:
        calls = 6
    ui.log("Ground check: is /field cut at the off? Three races from across the archive, %d calls" % calls)
    if not picks:
        today = today_utc()
        for back in (2, 90, 360):
            day = today - timedelta(days=back)
            try:
                with ui.busy("GET /v1/results  %s %s" % (region, day.isoformat())):
                    payload = client.get(
                        "/results",
                        {"date_from": day.isoformat(), "date_to": day.isoformat(), "region": region, "limit": 1},
                        endpoint="results",
                    )
            except ApiError as exc:
                ui.log("%s /v1/results answered %s" % (ui.g["cross"], exc.summary()), tone="red")
                continue
            rows = payload.get("data") or []
            if rows:
                picks.append(probe.race_ref(rows[0], today))

    passed = 0
    for race in picks:
        label = ui.paint(probe_pad("%s %sm %s" % (race.get("track"), race.get("distance_m"), race.get("grade")), 20), "white", True)
        when = probe_pad(probe.nice_date(probe.as_date(race.get("date_local"))), 17)
        try:
            with ui.busy("GET /v1/races/%s/field" % race.get("race_id")):
                answer = client.get("/races/%s/field" % race.get("race_id"), endpoint="races/{race_id}/field")
        except ApiError as exc:
            ui.row("%s %s %s %s" % (ui.paint(ui.g["cross"], "red", True), label, when, ui.paint(exc.summary(), "red")))
            continue
        meta = answer.get("meta") or {}
        data = answer.get("data") or {}
        cutoff = probe.parse_utc(meta.get("form_cutoff"))
        start = probe.parse_utc((data.get("scheduled_start") or {}).get("utc")) or probe.parse_utc(race.get("start_utc"))
        if cutoff is None:
            verdict = ui.paint("no form_cutoff in meta, so not cut at the off", "red")
            good = False
        elif start is not None and cutoff > start + timedelta(minutes=30):
            verdict = ui.paint("form_cutoff %s, %s after the off" % (probe.nice_moment(cutoff), probe.gap_text((cutoff - start).total_seconds() / 3600.0)), "red")
            good = False
        else:
            verdict = ui.paint("form_cutoff", "muted") + " " + probe.nice_moment(cutoff) + "  " + ui.paint("the race's own off", "muted")
            good = True
        passed += 1 if good else 0
        mark = ui.paint(ui.g["tick"], "green", True) if good else ui.paint(ui.g["cross"], "red", True)
        ui.row("%s %s %s %s" % (mark, label, when, verdict))

    if picks and passed == len(picks):
        ui.log("CUT AT THE OFF  %d of %d, so history is read straight from /field" % (passed, len(picks)), tone="green", bold=True)
        return True
    ui.box(
        "NOT CUT AT THE OFF",
        ui.wrap(
            "/field showed a cut at the off on %d of %d races checked. A backtest on figures that are not cut at the off "
            "would be reading its own lookahead, so nothing has been fetched. python3 -m doganalysis probe shows which "
            "figures leak." % (passed, len(picks)),
            ui.width - 4,
        ),
        tone="red",
    )
    return False


def probe_pad(text, width):
    text = str(text)
    return text + " " * max(1, width - len(text))


# choices


def choose_region(ui, args):
    if args.region:
        return args.region
    if not ui.interactive():
        return None
    choice = ui.menu("Region", [("GB", "Great Britain"), ("AU", "Victoria, Australia"), ("Both", "GB, then AU, never pooled")], 0)
    return ("GB", "AU", "BOTH")[choice]


def range_from_flags(args, yesterday):
    if args.last:
        if args.last < 1:
            return None, "--last takes a number of days, 1 or more."
        return (yesterday - timedelta(days=args.last - 1), yesterday), None
    if args.date_from:
        first = parse_day(args.date_from)
        last = parse_day(args.date_to) if args.date_to else yesterday
        if first is None or last is None:
            return None, "--from and --to take dates written YYYY-MM-DD."
        if last > yesterday:
            return None, "--to can be yesterday at the latest, %s; today's racing has not finished." % yesterday.isoformat()
        if first > last:
            return None, "--from has to come before --to."
        return (first, last), None
    return None, None


def choose_range(ui, args, client, region, force_menu=False):
    yesterday = today_utc() - timedelta(days=1)
    if not force_menu:
        picked, problem = range_from_flags(args, yesterday)
        if picked or problem:
            return picked, problem
    if not ui.interactive():
        return None, "No range given. Pass --last N, or --from YYYY-MM-DD with an optional --to."
    per_day = DAILY_RACES.get(region, 120) + 1
    per_minute = float(client.per_minute())

    def hint(days):
        calls = days * per_day
        return "about %s calls, %s" % (num(calls), duration(calls / per_minute).replace("about ", ""))

    span = (yesterday - EXCHANGE_START).days + 1
    options = [
        ("Everything since %s" % nice(EXCHANGE_START), "exchange prices start here, %d days" % span),
        ("Last 90 days", hint(90)),
        ("Last 7 days", "the pre-test, " + hint(7)),
        ("Custom dates", "asks for a start and an end"),
    ]
    choice = ui.menu("Range", options, 0)
    if choice == 0:
        return (EXCHANGE_START, yesterday), None
    if choice == 1:
        return (yesterday - timedelta(days=89), yesterday), None
    if choice == 2:
        return (yesterday - timedelta(days=PRETEST_DAYS - 1), yesterday), None
    for _ in range(3):
        first = parse_day(ui.ask_text("First day, YYYY-MM-DD"))
        last_text = ui.ask_text("Last day, YYYY-MM-DD (enter for yesterday)")
        last = parse_day(last_text) if last_text else yesterday
        if first and last and first <= last <= yesterday:
            return (first, last), None
        ui.log("Those dates do not make a range that ends by yesterday, %s. Try again." % yesterday.isoformat(), tone="amber")
    return None, "Three ranges that did not work, stopping."


def choose_commission(ui, args):
    if args.commission is not None:
        return args.commission
    if not ui.interactive():
        return DEFAULT_COMMISSION
    choice = ui.menu(
        "Commission on winnings",
        [("5%", "a conservative default"), ("2%", "a discounted rate"), ("None", "gross returns")],
        0,
    )
    return (5.0, 2.0, 0.0)[choice]


# what it will cost


def count_races(client, region, first, last):
    total, start = 0, first
    while start <= last:
        end = min(last, start + timedelta(days=COUNT_WINDOW_DAYS - 1))
        payload = client.get(
            "/races",
            {"date_from": start.isoformat(), "date_to": end.isoformat(), "region": region, "limit": 1},
            endpoint="races",
        )
        total += int((payload.get("meta") or {}).get("total") or 0)
        start = end + timedelta(days=1)
    return total


def cost_panel(ui, client, usage, region, first, last):
    days = store.days_between(first, last)
    held = store.survey(region, days)
    complete = set(held["complete"])
    todo = [day for day in days if day not in complete]
    windows = ((last - first).days // COUNT_WINDOW_DAYS) + 1
    ui.log("Counting %s races in the range with %s" % (region, "one /v1/races call" if windows == 1 else "%d /v1/races calls" % windows))
    with ui.busy("GET /v1/races  %s %s to %s" % (region, first.isoformat(), last.isoformat())):
        total = count_races(client, region, first, last)
    fields = max(0, total - held["races"] - held["partial_fields"]) if todo else 0
    calls = (len(todo) + fields) if todo else 0
    per_minute = client.per_minute()
    month = usage.get("month") or {}
    used, cap = int(month.get("used") or 0), int(month.get("limit") or 0)

    def stat(label, value):
        return ui.paint(label.ljust(18), "muted") + value

    days_text = "%d in the range, %d already cached" % (len(days), len(complete))
    if held["empty"]:
        days_text += ", %d of them without racing" % len(held["empty"])
    lines = [stat("Days", days_text), stat("Races", "%s on the cards, %s of them already cached" % (num(total), num(held["races"])))]
    if todo:
        lines.append(stat("To fetch", "%d days: %d result pages and up to %s fields, one /field call a race" % (len(todo), len(todo), num(fields))))
    else:
        lines.append(stat("To fetch", "nothing, every day in the range is cached"))
    if cap:
        cells = 40
        used_cells = min(cells, int(round(used / float(cap) * cells)))
        run_cells = max(0, min(cells, int(round((used + calls) / float(cap) * cells))) - used_cells)
        bar = (
            ui.paint(ui.g["full"] * used_cells, "green")
            + ui.paint(ui.g["full"] * run_cells, "amber")
            + ui.paint(ui.g["empty"] * (cells - used_cells - run_cells), "track")
        )
        lines.append(stat("Calls", "%s  %s  %.1f%% of the month, %s used after it" % (num(calls), bar, 100.0 * calls / cap, num(used + calls))))
        lines.append(stat("", ui.paint("green is already used this month, amber is this run", "muted")))
    else:
        lines.append(stat("Calls", num(calls)))
    lines.append(stat("Time", "%s at %s calls a minute" % (duration(calls / float(per_minute)), num(per_minute))))
    lines.append(stat("Disk", "about %s under data/cache/%s, one compressed file a day" % (size_text(len(todo) * store.average_day_bytes(region)), region)))
    lines.append(stat("Stopping", "safe at any moment: a finished day is never fetched again, an unfinished one is redone"))
    over = bool(cap) and used + calls > cap
    if over:
        lines.append("")
        lines.extend(
            ui.paint(part, "red", True)
            for part in ui.wrap(
                "This needs about %s calls and the key has %s left this month. The fetch stops cleanly at the limit, "
                "and a rerun once the month resets carries on from there." % (num(calls), num(max(0, cap - used))),
                ui.width - 4,
            )
        )
    ui.box("BEFORE ANY CALL", lines, right="%s  %s to %s" % (region, nice(first), nice(last)), tone="red" if over else "teal")
    return {"days": days, "todo": todo, "calls": calls, "total": total, "cached": len(complete), "over": over}


def go_ahead(ui, args, plan, per_minute):
    if not plan["todo"]:
        ui.log("Every day in the range is cached already, so there is nothing to fetch.", tone="green")
        return "done"
    if args.yes:
        return "fetch"
    if not ui.interactive():
        ui.log("Nothing fetched. Add --yes to fetch once this panel has been read.", tone="amber")
        return "stop"
    options = [("Fetch %d days now" % len(plan["todo"]), "%s, %s calls" % (duration(plan["calls"] / per_minute), num(plan["calls"])))]
    actions = ["fetch"]
    if len(plan["todo"]) > PRETEST_DAYS:
        options.append(("Fetch the last %d days first" % PRETEST_DAYS, "a pre-test before committing to the rest"))
        actions.append("pretest")
    if plan["cached"]:
        options.append(("Analyse what is cached", "%d cached days, no calls at all" % plan["cached"]))
        actions.append("cached")
    options.append(("Change the range", ""))
    actions.append("range")
    options.append(("Stop here", "nothing is fetched"))
    actions.append("stop")
    default = 1 if plan["over"] and "pretest" in actions else 0
    return actions[ui.menu("Go ahead?", options, default)]


# the run


def run(ui, client, usage, args):
    hint = args.region if args.region in ("GB", "AU") else "GB"
    if not ground_check(ui, client, hint):
        return 3

    region = choose_region(ui, args)
    if region is None:
        return named(ui, "No region given. Pass --region GB, AU or BOTH.")
    regions = ["GB", "AU"] if region == "BOTH" else [region]
    picked, problem = choose_range(ui, args, client, regions[0])
    if problem:
        return named(ui, problem)
    commission = choose_commission(ui, args)
    today = today_utc()

    record = {
        "tool": TOOL,
        "version": __version__,
        "command": "analyse",
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings": {"regions": regions, "from": picked[0].isoformat(), "to": picked[1].isoformat(), "commission_pct": commission},
        "regions": {},
    }
    for name in regions:
        first, last = picked
        while True:
            plan = cost_panel(ui, client, usage, name, first, last)
            action = go_ahead(ui, args, plan, float(client.per_minute()))
            if action != "range":
                break
            picked, problem = choose_range(ui, args, client, name, force_menu=True)
            if problem:
                return named(ui, problem)
            first, last = picked
        if action == "stop":
            return 0
        todo = plan["todo"]
        analysed_from = first
        if action == "pretest":
            todo = [day for day in todo if day > last - timedelta(days=PRETEST_DAYS)]
            analysed_from = max(first, last - timedelta(days=PRETEST_DAYS - 1))
        if action == "cached":
            todo = []
        totals = None
        if todo:
            try:
                totals = fetch.run(ui, client, name, todo, today, cached_days=plan["cached"])
            except fetch.Stop as stop:
                ui.log("%s The API refused a call no retry will fix: %s. The day in progress is saved." % (ui.g["cross"], stop), tone="red", bold=True)
                return 3
        with ui.busy("Ranking %s races from the cache" % name):
            result = stage_one.compute(name, analysed_from, last)
        ui.log(
            "Stage one: ranked %s %s races on five measurements, every figure read from the cache"
            % ("{:,}".format(result["counts"].get("ranked", 0)), name)
        )
        stage_one.panel(ui, result)
        record["regions"][name] = {
            "from": analysed_from.isoformat(),
            "to": last.isoformat(),
            "cached_before": plan["cached"],
            "fetched_days": len(todo),
            "open_days": [day.isoformat() for day, _ in (totals or {}).get("open_days", [])],
            "calls": (totals or {}).get("calls", 0),
            "stage_one": result,
        }

    record["finished_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = write_json(private_dir(DATA / "runs" / ("%s-%s" % (stamp, "-".join(regions)))) / "run.json", record)
    ui.log("Run saved to %s. Stage two, whether the price already knew, is the next build." % relative(path), tone="teal")
    return 0


def named(ui, message):
    ui.log("%s %s" % (ui.g["cross"], message), tone="red")
    return 2
