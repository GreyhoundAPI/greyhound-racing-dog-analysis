"""Fetching into the cache: one results page a day and one /field call a race, newest day first."""

import time
from datetime import datetime, timezone

from . import TOOL, __version__, store
from .api import ApiError

PERMANENT = (400, 403, 404, 409, 410, 422)
STOPPING = ("unauthorized", "key_suspended", "quota_exceeded", "basic_auth_gate")
FINAL_AFTER_DAYS = 3
PROGRESS_EVERY = 25


class Stop(Exception):
    """A refusal no retry will fix, so the run ends cleanly with the day saved."""

    def __init__(self, error):
        Exception.__init__(self, error.summary())
        self.error = error


def num(value):
    return "{:,}".format(value)


def took_text(seconds):
    seconds = int(round(seconds))
    if seconds < 100:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %02ds" % (seconds // 60, seconds % 60)
    return "%dh %02dm" % (seconds // 3600, seconds // 60 % 60)


def fetch_results(client, region, day):
    races, meta, page = [], {}, 1
    while page <= 20:
        payload = client.get(
            "/results",
            {"date_from": day.isoformat(), "date_to": day.isoformat(), "region": region, "limit": 200, "page": page},
            endpoint="results",
        )
        batch = payload.get("data") or []
        races.extend(batch)
        info = payload.get("meta") or {}
        if page == 1:
            meta = {"total": info.get("total"), "unresolved": info.get("unresolved")}
        pages = info.get("total_pages")
        if not batch or (pages and page >= pages) or (not pages and len(batch) < 200):
            break
        page += 1
    return races, meta


def settle(payload, day, today):
    """A day is final once its results are, and complete once it is final with no field left to fetch."""
    races = payload["races"]
    age = (today - day).days
    if races:
        settled = all(((race.get("result") or {}).get("result_status") or "final") == "final" for race in races)
        waiting = (payload["results_meta"].get("unresolved") or 0) > 0 and age <= FINAL_AFTER_DAYS
        payload["final"] = (settled and not waiting) or age > FINAL_AFTER_DAYS
    else:
        payload["final"] = age > 1
    payload["complete"] = bool(payload["final"] and not payload["open"])


def fetch_day(client, region, day, today, previous=None):
    races, meta = fetch_results(client, region, day)
    fields = dict((previous or {}).get("fields") or {})
    no_field = dict((previous or {}).get("no_field") or {})
    payload = {
        "tool": TOOL,
        "version": __version__,
        "region": region,
        "date": day.isoformat(),
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results_meta": meta,
        "races": races,
        "fields": fields,
        "no_field": no_field,
        "open": [],
        "final": False,
        "complete": False,
    }
    wanted = [
        str(race.get("race_id"))
        for race in races
        if not race.get("is_handicap") and str(race.get("race_id")) not in fields and str(race.get("race_id")) not in no_field
    ]

    def remaining():
        return [rid for rid in wanted if rid not in fields and rid not in no_field]

    failed = []
    try:
        for rid in wanted:
            try:
                answer = client.get("/races/%s/field" % rid, endpoint="races/{race_id}/field")
                fields[rid] = {"meta": answer.get("meta") or {}, "data": answer.get("data") or {}}
            except ApiError as exc:
                if exc.code in STOPPING or exc.status == 401:
                    payload["open"] = remaining()
                    settle(payload, day, today)
                    store.save_day(region, day, payload)
                    raise Stop(exc)
                if exc.status in PERMANENT:
                    no_field[rid] = exc.as_dict()
                else:
                    failed.append(rid)
    except KeyboardInterrupt:
        payload["open"] = remaining()
        settle(payload, day, today)
        store.save_day(region, day, payload)
        raise
    payload["open"] = failed
    settle(payload, day, today)
    store.save_day(region, day, payload)
    return payload


def day_stats(payload):
    runners = backs = lays = 0
    for race in payload.get("races") or []:
        for position in (race.get("result") or {}).get("positions") or []:
            runners += 1
            exchange = position.get("betfair") or {}
            backs += 1 if exchange.get("back_price") else 0
            lays += 1 if exchange.get("lay_price") else 0
    fields = payload.get("fields") or {}
    thin = [len((item.get("data") or {}).get("thin") or []) for item in fields.values()]
    return {
        "races": len(payload.get("races") or []),
        "fields": len(fields),
        "handicaps": sum(1 for race in payload.get("races") or [] if race.get("is_handicap")),
        "no_field": len(payload.get("no_field") or {}),
        "open": len(payload.get("open") or []),
        "unresolved": (payload.get("results_meta") or {}).get("unresolved") or 0,
        "runners": runners,
        "backs": backs,
        "lays": lays,
        "thin": sum(thin),
        "thin_races": len(thin),
        "final": bool(payload.get("final")),
    }


def header(ui):
    head = (
        "date".ljust(12) + "races".rjust(7) + "fields".rjust(8) + "results".rjust(10) + "back".rjust(7) + "lay".rjust(7)
        + "thin a race".rjust(13) + "calls".rjust(8) + "took".rjust(8)
    )
    ui.cont(ui.paint(head, "muted"))


def share(part, whole):
    return "%d%%" % int(round(100.0 * part / whole)) if whole else "-"


def day_row(ui, day, stats, calls, seconds):
    if not stats["races"] and not stats["unresolved"]:
        ui.row(ui.paint(day.isoformat(), "white", True) + "  " + ui.paint("none", "muted") + "  " + ui.paint("no racing on this day", "muted"))
        return
    open_day = stats["open"] > 0
    mark = ui.paint(ui.g["cross"], "red", True) if open_day else ui.paint(ui.g["tick"], "green", True)
    fields = str(stats["fields"]).rjust(8)
    ui.row(
        ui.paint(day.isoformat(), "white", True) + "  " + mark
        + str(stats["races"]).rjust(6)
        + (ui.paint(fields, "red", True) if open_day else fields)
        + ("final" if stats["final"] else "prov.").rjust(10)
        + share(stats["backs"], stats["runners"]).rjust(7)
        + share(stats["lays"], stats["runners"]).rjust(7)
        + (("%.1f" % (stats["thin"] / float(stats["thin_races"]))) if stats["thin_races"] else "-").rjust(13)
        + str(calls).rjust(8)
        + took_text(seconds).rjust(8)
    )
    if open_day:
        ui.cont(
            " " * 13
            + ui.paint(
                "%d /field %s failed after retries, so this day stays open for the next run"
                % (stats["open"], "call" if stats["open"] == 1 else "calls"),
                "red",
            )
        )
    elif not stats["final"]:
        ui.cont(" " * 13 + ui.paint("results are not final yet, so this day is refetched on the next run", "muted"))


def run(ui, client, region, days, today, cached_days=0):
    """Fetch these days newest first and return what happened, for the closing panel and the run record."""
    started = time.monotonic()
    first_call = client.calls
    totals = {
        "days": 0, "empty": 0, "open_days": [], "races": 0, "fields": 0, "handicaps": 0, "no_field": 0,
        "unresolved": 0, "runners": 0, "backs": 0, "lays": 0, "open": 0, "calls": 0, "seconds": 0.0,
    }
    ui.log(
        "Fetching %d days, newest first. One line a day as it lands. Ctrl+C stops safely and a rerun resumes." % len(days)
    )
    header(ui)
    for count, day in enumerate(days, 1):
        before, clock = client.calls, time.monotonic()
        previous = store.load_day(region, day)
        with ui.busy("GET /v1/results and /field  %s %s" % (region, day.isoformat())):
            payload = fetch_day(client, region, day, today, previous)
        stats = day_stats(payload)
        calls, seconds = client.calls - before, time.monotonic() - clock
        day_row(ui, day, stats, calls, seconds)
        totals["days"] += 1
        if not stats["races"] and not stats["unresolved"]:
            totals["empty"] += 1
        if stats["open"]:
            totals["open_days"].append((day, stats["open"]))
        for name in ("races", "fields", "handicaps", "no_field", "unresolved", "runners", "backs", "lays", "open"):
            totals[name] += stats[name]
        if count % PROGRESS_EVERY == 0 and count < len(days):
            spent = client.calls - first_call
            left = (len(days) - count) * (spent / float(count))
            ui.row(
                ui.bar(count / float(len(days)), 24, "teal")
                + "  %d of %d days, %s races, %s calls, about %s to go"
                % (count, len(days), num(totals["races"]), num(spent), took_text(left / client.per_minute() * 60))
            )
            header(ui)
    totals["calls"] = client.calls - first_call
    totals["seconds"] = time.monotonic() - started
    panel(ui, region, totals, cached_days)
    return totals


def panel(ui, region, totals, cached_days):
    def stat(label, value):
        return ui.paint(label.ljust(18), "muted") + value

    days = "%d fetched, %d already cached" % (totals["days"], cached_days)
    if totals["empty"]:
        days += ", %d without racing" % totals["empty"]
    if totals["open_days"]:
        listed = ", ".join(day.isoformat() for day, _ in totals["open_days"][:3])
        more = len(totals["open_days"]) - 3
        if more > 0:
            listed += " and %d more" % more
        days += ", " + ui.paint("%d left open" % len(totals["open_days"]), "red", True) + ": " + listed
    races = "%s with a field, %s handicaps not fetched" % (num(totals["fields"]), num(totals["handicaps"]))
    if totals["no_field"]:
        races += ", %s refused a field" % num(totals["no_field"])
    if totals["open"]:
        races += ", " + ui.paint("%s still to fetch" % num(totals["open"]), "red", True)
    lines = [
        stat("Days", days),
        stat("Races", races),
        stat(
            "Results",
            "%s complete races; %s void, abandoned or unsettled, counted and kept out of both stages"
            % (num(totals["races"]), num(totals["unresolved"])),
        ),
        stat(
            "Prices",
            "back price on %s of runners, lay price on %s"
            % (share(totals["backs"], totals["runners"]), share(totals["lays"], totals["runners"])),
        ),
        stat("Calls", "%s this run" % num(totals["calls"])),
        stat("Took", took_text(totals["seconds"])),
    ]
    ui.box("FETCHED", lines, right="%s  %s" % (region, datetime.now().strftime("%H:%M")), tone="red" if totals["open_days"] else "teal")
