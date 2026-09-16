"""Phase 1, the lookahead probe.

A backtest replays races that have already run, so every figure it reads must be the figure as it
stood before the off. /v1/races/{race_id}/form documents that cut. /v1/races/{race_id}/field stamps
form_state_computed_utc and says its form state is rebuilt nightly. This probe asks /field about
completed races and recounts each figure from the dog's own history two ways: cut at the off, and up
to the state's build. Each figure is judged on its own, because one endpoint can serve some figures
cut at the off and others from current state.
"""

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import TOOL, __version__
from .api import ApiError
from .config import DATA, load_json, private_dir, relative, write_json

DEFAULT_RACES = 8
NEAREST_DAYS = 2
FURTHEST_DAYS = 540
STEP_BACK_DAYS = 5
AGREEMENT = 0.9
MIN_DECIDED = 5
REGION_NAMES = {"GB": "Great Britain", "AU": "Victoria, Australia"}
READ_TONES = {"clean": "green", "leaks": "red", "same": "blue", "unclear": "amber", "mixed": "amber", "n/a": "muted"}
VERDICT_TONES = {"clean": "green", "leaks": "red", "split": "red", "mixed": "amber", "unclear": "amber", "refused": "red"}

# key, column title, short name, the /field value it reads
FIGURES = (
    ("here_runs", "runs here", "here", "here.runs"),
    ("grade_runs", "grade runs", "grade", "at_this_grade.runs"),
    ("pace_runs", "pace runs", "pace", "pace.runs"),
    ("layoff_days", "layoff days", "layoff", "layoff.days_since_last_run"),
)

QUESTION = (
    "Does /v1/races/{race_id}/field show a race that has already run as it stood before the off, "
    "or as the form state stood when it was built? Each figure is judged on its own."
)

CRITERIA = {
    "sample": (
        "completed, non-handicap races, the largest field on each sampled day, spread geometrically from "
        "%d to %d days back; a rerun probes the same races unless --resample" % (NEAREST_DAYS, FURTHEST_DAYS)
    ),
    "control": (
        "the next non-handicap race today that has not run, preferring 400m or more because sprint trips often "
        "carry no early sectionals; both readings should agree there on every figure, and a figure that is zero "
        "across the whole card is reported as untested"
    ),
    "figures": (
        "here.runs (here.wins alongside), at_this_grade.runs, pace.runs and layoff.days_since_last_run "
        "from /v1/races/{race_id}/field, each judged on its own"
    ),
    "recount_source": (
        "/v1/dogs/{dog_id}/form, the dog's complete history newest first; a run counts toward a runs "
        "figure when it has a finishing position"
    ),
    "here_runs": "runs at the same track name and distance",
    "grade_runs": "runs at the same grade, at any track",
    "pace_runs": (
        "runs at the same track name and distance carrying an early sectional, over the dog's whole history "
        "with no time window; the recount cannot see handicaps, so it does not exclude them"
    ),
    "layoff_days": "days back to the latest dated run of any kind",
    "before_off": "counted up to the race's local date: what a figure cut at the off would show",
    "at_build": (
        "counted up to the local date of form_state_computed_utc, with runs dated on that day allowed either "
        "side: what a figure served from current form state would show"
    ),
    "layoff_before_off": (
        "race date minus the previous run, from /v1/races/{race_id}/form (cut at the off) and from the dog's form"
    ),
    "layoff_at_build": "the build date, the race date or today, minus the latest run before the build",
    "runner_read": (
        "per figure: clean when it matches the before-off recount, leaks when it matches the at-build "
        "recount, same when both readings give that value, unclear when neither matches or the two tie; "
        "exact is false when the read is only the nearer of the two, and such near reads never vote"
    ),
    "figure_verdict": (
        "decided on exact reads only: clean or leaks when at least %d%% of them agree, mixed below that, "
        "unclear when fewer than %d exact reads separate the readings" % (int(AGREEMENT * 100), MIN_DECIDED)
    ),
    "overall_verdict": (
        "clean when every decided figure is clean, leaks when every decided figure leaks, split when some "
        "figures are clean and others leak, mixed when a figure disagrees with itself, refused when /field "
        "answers for no race that has run, unclear when no figure is decided"
    ),
}


# small helpers


def spread_days(count, near=NEAREST_DAYS, far=FURTHEST_DAYS):
    if count <= 1:
        return [near]
    days = []
    for step in range(count):
        value = int(round(near * (float(far) / near) ** (float(step) / (count - 1))))
        if days and value <= days[-1]:
            value = days[-1] + 1
        days.append(value)
    return days


def parse_utc(value):
    """'2026-09-15 14:19:00', '2026-07-08T20:46:00Z' or '2026-07-08T21:46:00+01:00' as an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", text
    )
    if not match:
        return None
    parts = [int(match.group(n) or 0) for n in range(1, 7)]
    moment = datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=timezone.utc)
    zone = match.group(7)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        moment -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return moment


def local_offset_minutes(value):
    match = re.search(r"([+-])(\d{2}):?(\d{2})$", str(value or "").strip())
    if not match:
        return 0
    sign = 1 if match.group(1) == "+" else -1
    return sign * (int(match.group(2)) * 60 + int(match.group(3)))


def local_date(moment, timezone_name, fallback_minutes):
    if moment is None:
        return None
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            return moment.astimezone(ZoneInfo(timezone_name)).date()
        except Exception:
            pass
    return (moment + timedelta(minutes=fallback_minutes or 0)).date()


def as_date(value):
    try:
        year, month, day = str(value)[:10].split("-")
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def as_int(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def latest(days):
    return max(days) if days else None


def join_words(words):
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def nice_date(day):
    if not day:
        return "unknown date"
    return "%s %d %s %d" % (day.strftime("%a"), day.day, day.strftime("%b"), day.year)


def nice_moment(moment):
    if not moment:
        return "unknown"
    return "%d %s %s UTC" % (moment.day, moment.strftime("%b"), moment.strftime("%H:%M"))


def gap_text(hours):
    size = abs(hours)
    if size < 1:
        return "%dm" % int(round(size * 60))
    if size < 48:
        return "%dh" % int(round(size))
    days = int(size // 24)
    rest = int(round(size - days * 24))
    return "%dd %dh" % (days, rest) if rest else "%dd" % days


def number(value):
    return "-" if value is None else str(value)


def tone_for(item):
    """A near read is amber, so the eye never takes it for a verdict."""
    if item["read"] in ("clean", "leaks") and not item.get("exact", True):
        return "amber"
    return READ_TONES.get(item["read"])


def vote(item):
    """Only exact reads vote; a read that is merely nearer to one side is counted as near."""
    if item["read"] in ("clean", "leaks") and not item.get("exact", True):
        return "near"
    return item["read"]


def show_value(key, value):
    if key == "layoff_days":
        if isinstance(value, list):
            return ("%dd" % value[0]) if value else "none"
        return "-" if value is None else "%dd" % value
    return number(value)


# choosing races


def race_ref(row, today):
    track = row.get("track") or {}
    start = row.get("scheduled_start") or {}
    day = as_date(start.get("local")) or as_date(start.get("utc"))
    return {
        "race_id": row.get("race_id"),
        "track": track.get("name"),
        "track_id": track.get("track_id"),
        "timezone": track.get("timezone"),
        "distance_m": row.get("distance_m"),
        "grade": row.get("grade"),
        "field_size": row.get("field_size"),
        "start_utc": start.get("utc"),
        "start_local": start.get("local"),
        "date_local": day.isoformat() if day else None,
        "days_back": (today - day).days if day else None,
    }


def choose_race(rows):
    candidates = [r for r in rows if isinstance(r, dict) and r.get("race_id") and not r.get("is_handicap")]
    if not candidates:
        return None
    candidates.sort(key=lambda r: str((r.get("scheduled_start") or {}).get("utc") or ""))
    largest = max(as_int(r.get("field_size")) or 0 for r in candidates)
    widest = [r for r in candidates if (as_int(r.get("field_size")) or 0) == largest]
    return widest[len(widest) // 2]


def select_sample(ui, client, region, count):
    today = datetime.now(timezone.utc).date()
    picks = []
    ui.log(
        "Choosing %d completed %s races, spread from %d to %d days back"
        % (count, region, NEAREST_DAYS, FURTHEST_DAYS)
    )
    for target in spread_days(count):
        found = None
        for extra in range(STEP_BACK_DAYS + 1):
            day = today - timedelta(days=target + extra)
            params = {
                "date_from": day.isoformat(),
                "date_to": day.isoformat(),
                "region": region,
                "status": "complete",
                "is_handicap": "false",
                "limit": 200,
            }
            with ui.busy("GET /v1/races  %s %s" % (region, day.isoformat())):
                payload = client.get("/races", params, endpoint="races")
            row = choose_race(payload.get("data") or [])
            if row and not any(p["race_id"] == row.get("race_id") for p in picks):
                found = race_ref(row, today)
                break
            ui.log("no completed %s race on %s, stepping back a day" % (region, day.isoformat()), tone="amber")
        if not found:
            ui.log("nothing found near %d days back, that slot is skipped" % target, tone="amber")
            continue
        picks.append(found)
        ui.row(
            "%s %s  %s %sm %s, %d runners  %s"
            % (
                ui.paint(ui.g["tick"], "green", True),
                ui.paint(nice_date(as_date(found["date_local"])), "white", True),
                found["track"],
                found["distance_m"],
                found["grade"],
                as_int(found["field_size"]) or 0,
                ui.paint("race %s" % found["race_id"], "muted"),
            )
        )
    return picks


def select_control(ui, client, region):
    now = datetime.now(timezone.utc)
    today = now.date()
    rows = []
    sources = (
        ("/racecards/today", {"region": region, "limit": 200}, "racecards/today"),
        ("/racecards/upcoming", {"region": region, "hours": 24, "limit": 200}, "racecards/upcoming"),
    )
    for path, params, name in sources:
        try:
            with ui.busy("GET /v1%s  %s" % (path, region)):
                payload = client.get(path, params, endpoint=name)
        except ApiError as exc:
            ui.log("%s answered %s" % (path, exc.summary()), tone="amber")
            continue
        rows = []
        for row in payload.get("data") or []:
            start = parse_utc((row.get("scheduled_start") or {}).get("utc"))
            if row.get("is_handicap") or start is None:
                continue
            if row.get("status") not in (None, "scheduled"):
                continue
            if start <= now + timedelta(minutes=5):
                continue
            rows.append((start, row))
        if rows:
            break
    if not rows:
        return None
    rows.sort(key=lambda pair: pair[0])
    # Sprint trips often carry no early sectionals, which would leave pace untested on the control.
    preferred = [pair for pair in rows if (as_int(pair[1].get("distance_m")) or 0) >= 400] or rows
    largest = max(as_int(r.get("field_size")) or 0 for _, r in preferred)
    for _, row in preferred:
        if (as_int(row.get("field_size")) or 0) >= min(largest, 5):
            return race_ref(row, today)
    return race_ref(preferred[0][1], today)


# reading one runner


def fetch_history(client, dog_id, pages=5):
    lines, cursor = [], None
    for _ in range(pages):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = client.get("/dogs/%s/form" % dog_id, params, endpoint="dogs/{dog_id}/form")
        batch = payload.get("data") or []
        lines.extend(batch)
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor or len(batch) < 200:
            return lines, False
    return lines, bool(cursor)


def read_value(value, before_values, build_values):
    """Which reading a field figure belongs to. Returns (read, exact)."""
    if value is None:
        return "n/a", True
    fits_before = value in before_values
    fits_build = value in build_values
    if fits_before and fits_build:
        return "same", True
    if fits_before:
        return "clean", True
    if fits_build:
        return "leaks", True
    if not before_values or not build_values:
        return "unclear", False
    near_before = min(abs(value - v) for v in before_values)
    near_build = min(abs(value - v) for v in build_values)
    if near_before < near_build:
        return "clean", False
    if near_build < near_before:
        return "leaks", False
    return "unclear", False


def assess_runner(runner, lines, truncated, race, race_day, build_day, today_local, form_previous):
    track = str(race.get("track") or "").strip().lower()
    distance = as_int(race.get("distance_m"))
    grade = str(race.get("grade") or "").strip().lower()
    race_id = race.get("race_id")

    runs = []
    dated = []
    race_in_history = False
    for line in lines:
        if line.get("race_id") == race_id:
            race_in_history = True
        day = as_date(line.get("date_local"))
        if day is None:
            continue
        dated.append(day)
        position = as_int(line.get("position"))
        if position is None or position < 1:
            continue
        runs.append(
            {
                "day": day,
                "here": str(line.get("track") or "").strip().lower() == track
                and as_int(line.get("distance_m")) == distance,
                "grade": bool(grade) and str(line.get("grade") or "").strip().lower() == grade,
                "timed": line.get("sectional_s") is not None,
                "won": position == 1,
            }
        )

    def count(keep, anchor, inclusive):
        total = 0
        for run in runs:
            if keep(run) and (run["day"] < anchor or (inclusive and run["day"] == anchor)):
                total += 1
        return total

    def figure(field_value, keep):
        before = count(keep, race_day, False)
        low = count(keep, build_day, False)
        high = count(keep, build_day, True)
        read, exact = read_value(field_value, {before}, set(range(low, high + 1)))
        return {"field": field_value, "before_off": before, "at_build": [low, high], "read": read, "exact": exact}

    here_block = runner.get("here") or {}
    grade_block = runner.get("at_this_grade") or {}
    pace_block = runner.get("pace") or {}
    layoff_block = runner.get("layoff") or {}

    figures = {
        "here_runs": figure(as_int(here_block.get("runs")), lambda r: r["here"]),
        "grade_runs": figure(as_int(grade_block.get("runs")), lambda r: r["grade"]),
        "pace_runs": figure(as_int(pace_block.get("runs")), lambda r: r["here"] and r["timed"]),
    }
    figures["here_runs"]["wins"] = figure(as_int(here_block.get("wins")), lambda r: r["here"] and r["won"])

    history_previous = latest([d for d in dated if d < race_day])
    form_day = as_date(form_previous) if form_previous else None
    before_days = set()
    for previous in (history_previous, form_day):
        if previous is not None:
            before_days.add((race_day - previous).days)
    build_days = set()
    primary = None
    for last in (latest([d for d in dated if d < build_day]), latest([d for d in dated if d <= build_day])):
        if last is None:
            continue
        for anchor in (build_day, race_day, today_local):
            gap = (anchor - last).days
            build_days.add(gap)
            if primary is None:
                primary = gap
    field_days = as_int(layoff_block.get("days_since_last_run"))
    if field_days is not None and field_days < 0:
        read, exact = "leaks", True
    else:
        read, exact = read_value(field_days, before_days, build_days)
    figures["layoff_days"] = {
        "field": field_days,
        "before_off": sorted(before_days),
        "at_build": sorted(build_days),
        "at_build_primary": primary,
        "read": read,
        "exact": exact,
    }

    agrees = None
    if form_day is not None or history_previous is not None:
        agrees = form_day == history_previous

    return {
        "trap": runner.get("trap"),
        "dog_id": runner.get("dog_id"),
        "dog_name": runner.get("dog_name"),
        "figures": figures,
        "race_in_dog_history": race_in_history,
        "previous_run_from_race_form": form_previous or None,
        "previous_run_from_dog_form": history_previous.isoformat() if history_previous else None,
        "race_form_agrees_with_dog_form": agrees,
        "history_lines": len(lines),
        "history_truncated": truncated,
    }


# layout


def _centre(text, width):
    gap = max(0, width - len(text))
    return " " * (gap // 2) + text + " " * (gap - gap // 2)


class Table(object):
    """trap 3, dog name, then field / off / built for each of the four figures."""

    TRAP = 3
    GROUP = 16

    def __init__(self, ui):
        self.ui = ui
        fixed = self.TRAP + self.GROUP * len(FIGURES) + 2 * (len(FIGURES) - 1)
        self.name_width = max(9, min(24, ui.width - ui.stamp_width - fixed))

    def headers(self):
        ui = self.ui
        lead = " " * (self.TRAP + self.name_width)
        first = lead + "  ".join(ui.paint(_centre(title, self.GROUP), "teal", True) for _, title, _, _ in FIGURES)
        labels = ui.paint("field".rjust(5) + "off".rjust(5) + "built".rjust(6), "muted")
        second = ui.paint("T".ljust(self.TRAP) + "dog".ljust(self.name_width), "muted") + "  ".join(
            [labels] * len(FIGURES)
        )
        return first, second

    def _runs(self, item):
        low, high = item["at_build"]
        built = str(low) if low == high else "%d-%d" % (low, high)
        if len(built) > 6:
            built = "%d+" % low
        return (
            self.ui.paint(number(item["field"]).rjust(5), tone_for(item), True)
            + number(item["before_off"]).rjust(5)
            + built.rjust(6)
        )

    def _days(self, item):
        field = item["field"]
        before, built = item["before_off"], item["at_build"]
        if field is not None and field in before:
            shown_before = "%dd" % field
        else:
            shown_before = ("%dd" % before[0]) if before else "-"
        if field is not None and field in built:
            shown_built = "%dd" % field
        elif item.get("at_build_primary") is not None:
            shown_built = "%dd" % item["at_build_primary"]
        else:
            shown_built = "-"
        return (
            self.ui.paint(("-" if field is None else "%dd" % field).rjust(5), tone_for(item), True)
            + shown_before.rjust(5)
            + shown_built.rjust(6)
        )

    def line(self, runner):
        ui = self.ui
        trap = ("T%s" % runner["trap"]) if runner.get("trap") is not None else "T?"
        name = str(runner.get("dog_name") or runner.get("dog_id"))
        if len(name) > self.name_width - 1:
            name = name[: self.name_width - 2] + ui.g["ellipsis"]
        lead = trap.ljust(self.TRAP) + name.ljust(self.name_width)
        if runner.get("error"):
            return lead + ui.paint("history unavailable: %s" % runner["error"].get("code"), "red")
        cells = []
        for key, _, _, _ in FIGURES:
            item = runner["figures"][key]
            cells.append(self._days(item) if key == "layoff_days" else self._runs(item))
        return lead + "  ".join(cells)


# judging a race


def race_figures(runners):
    figures = {}
    for key, _, _, _ in FIGURES:
        tally = Counter()
        for runner in runners:
            item = (runner.get("figures") or {}).get(key)
            if item:
                tally[vote(item)] += 1
        clean, leaks = tally.get("clean", 0), tally.get("leaks", 0)
        if clean + leaks == 0:
            verdict = "unclear" if tally.get("near") else ("same" if tally.get("same") else "n/a")
        elif leaks > clean:
            verdict = "leaks"
        elif clean > leaks:
            verdict = "clean"
        else:
            verdict = "unclear"
        figures[key] = {
            "verdict": verdict,
            "clean": clean,
            "leaks": leaks,
            "same": tally.get("same", 0),
            "unclear": tally.get("unclear", 0),
            "not_available": tally.get("n/a", 0),
            "near": tally.get("near", 0),
        }
    return figures


def race_verdict(figures):
    called = [item["verdict"] for item in figures.values() if item["verdict"] in ("clean", "leaks")]
    if not called:
        return "unclear"
    if all(v == "clean" for v in called):
        return "clean"
    if all(v == "leaks" for v in called):
        return "leaks"
    return "split"


def figure_line(ui, verdict, figures):
    parts = []
    for key, _, short, _ in FIGURES:
        item = figures[key]
        shown = item["verdict"]
        decided = item["clean"] + item["leaks"]
        if shown in ("clean", "leaks"):
            text = "%s %d/%d" % (shown.upper(), item[shown], decided)
        elif shown == "unclear":
            text = "UNCLEAR %d/%d" % (item["leaks"], decided)
        else:
            text = shown
        if item.get("near"):
            text += " +%d near" % item["near"]
        parts.append(ui.paint(short, "white", True) + " " + ui.paint(text, READ_TONES.get(shown, "amber"), True))
    return ui.paint(verdict.upper().ljust(9), VERDICT_TONES.get(verdict, "amber"), True) + "   ".join(parts)


def scope_check(runners):
    check = {}
    for key, _, _, _ in FIGURES:
        counted, matched, misses, tested = 0, 0, [], False
        for runner in runners:
            item = (runner.get("figures") or {}).get(key)
            if not item or item["field"] is None:
                continue
            counted += 1
            if key == "layoff_days" or item["field"] or item["before_off"]:
                tested = True
            before = item["before_off"]
            fits = item["field"] in before if isinstance(before, list) else item["field"] == before
            if fits:
                matched += 1
            elif len(misses) < 3:
                misses.append(
                    {"trap": runner.get("trap"), "dog_name": runner.get("dog_name"), "field": item["field"], "recount": before}
                )
        check[key] = {"runners": counted, "match": matched, "misses": misses, "tested": tested}
    return check


# one race


def probe_race(ui, client, race, label, evidence, control=False):
    out = dict(race)
    out.update({"control": control, "runners": [], "error": None, "verdict": None})
    race_id = race["race_id"]
    offset = local_offset_minutes(race.get("start_local"))
    race_day = as_date(race.get("date_local")) or local_date(parse_utc(race.get("start_utc")), race.get("timezone"), offset)
    now = datetime.now(timezone.utc)
    today_local = local_date(now, race.get("timezone"), offset)
    if race_day is None:
        out["verdict"] = "unclear"
        out["error"] = {"endpoint": "sample", "status": 0, "code": "no_date", "message": "the race carries no start date"}
        ui.log("race %s carries no start date, skipped" % race_id, tone="amber")
        return out

    if control:
        tail = "today, not run yet"
    else:
        back = (today_local - race_day).days
        tail = "1 day back" if back == 1 else "%d days back" % back
    ui.blank()
    ui.row(
        "%s  %s  %s  %s"
        % (
            ui.paint(ui.g["arrow"] + " " + label, "orange", True),
            ui.paint("%s %sm %s" % (race.get("track"), race.get("distance_m"), race.get("grade")), "white", True),
            "%s, %s" % (nice_date(race_day), tail),
            ui.paint("race %s" % race_id, "muted"),
        )
    )

    try:
        with ui.busy("GET /v1/races/%s/field" % race_id):
            field = client.get("/races/%s/field" % race_id, endpoint="races/{race_id}/field")
    except ApiError as exc:
        out["error"] = dict(exc.as_dict(), endpoint="field")
        out["verdict"] = "refused"
        ui.log("%s /field answered %s" % (ui.g["cross"], exc.summary()), tone="red")
        return out
    write_json(evidence / ("field-%s.json" % race_id), field)
    data = field.get("data") or {}

    start = parse_utc((data.get("scheduled_start") or {}).get("utc")) or parse_utc(race.get("start_utc"))
    built = parse_utc(data.get("form_state_computed_utc"))
    out["form_state_computed_utc"] = data.get("form_state_computed_utc")
    out["scheduled_start_utc"] = iso(start)
    if built and start:
        hours = (built - start).total_seconds() / 3600.0
        out["state_vs_off_hours"] = round(hours, 2)
        if control:
            ui.log(
                "state built %s, %s %s the off, and the race has not run yet"
                % (nice_moment(built), gap_text(hours), "after" if hours > 0 else "before"),
                tone="blue",
            )
        elif hours > 0:
            ui.log("state built %s, %s after the off" % (nice_moment(built), gap_text(hours)), tone="red")
        else:
            ui.log("state built %s, %s before the off" % (nice_moment(built), gap_text(hours)), tone="green")
    else:
        out["state_vs_off_hours"] = None
        ui.log("form_state_computed_utc is missing, so only the recounts can decide", tone="amber")
    build_day = local_date(built or now, race.get("timezone"), offset)
    out["build_date_local"] = build_day.isoformat() if build_day else None

    previous = {}
    try:
        with ui.busy("GET /v1/races/%s/form" % race_id):
            form = client.get("/races/%s/form" % race_id, {"runs": 1}, endpoint="races/{race_id}/form")
        write_json(evidence / ("form-%s.json" % race_id), form)
        out["form_cutoff"] = (form.get("meta") or {}).get("form_cutoff")
        for runner in (form.get("data") or {}).get("runners") or []:
            lines = runner.get("form") or []
            previous[runner.get("dog_id")] = lines[0].get("date_local") if lines else ""
    except ApiError as exc:
        out["form_error"] = exc.as_dict()
        ui.log("/form answered %s, so the layoff check leans on each dog's own form" % exc.summary(), tone="amber")

    runners = data.get("runners") or []
    if not runners:
        out["verdict"] = "unclear"
        ui.log("/field returned no runners for this race", tone="amber")
        return out
    traps = [runner.get("trap") for runner in runners]
    out["runner_count"] = len(runners)
    out["distinct_traps"] = len(set(traps))
    if out["distinct_traps"] < out["runner_count"]:
        ui.log(
            "/field lists %d runners across %d traps: reserves or non-runners are in the field, with nothing "
            "marking which" % (out["runner_count"], out["distinct_traps"]),
            tone="amber",
        )

    table = Table(ui)
    for header in table.headers():
        ui.row(header)
    for runner in runners:
        dog_id = runner.get("dog_id")
        try:
            with ui.busy("GET /v1/dogs/%s/form  %s" % (dog_id, runner.get("dog_name") or "")):
                lines, truncated = fetch_history(client, dog_id)
        except ApiError as exc:
            row = {"trap": runner.get("trap"), "dog_id": dog_id, "dog_name": runner.get("dog_name"), "error": exc.as_dict()}
            out["runners"].append(row)
            ui.row(table.line(row))
            continue
        write_json(evidence / ("dog-form-%s.json" % dog_id), {"dog_id": dog_id, "lines": lines})
        row = assess_runner(runner, lines, truncated, race, race_day, build_day, today_local, previous.get(dog_id))
        out["runners"].append(row)
        ui.row(table.line(row))

    figures = race_figures(out["runners"])
    out["figures"] = figures
    if control:
        check = scope_check(out["runners"])
        out["scope_check"] = check
        out["verdict"] = "control"
        parts = [
            "%s %d of %d%s"
            % (short, check[key]["match"], check[key]["runners"], "" if check[key]["tested"] else " (all zero, untested)")
            for key, _, short, _ in FIGURES
        ]
        agrees = all(item["match"] == item["runners"] for item in check.values())
        ui.log(
            "CONTROL  a plain recount matches /field on " + ", ".join(parts),
            tone="green" if agrees else "amber",
            bold=True,
        )
        for key, _, _, source in FIGURES:
            for miss in check[key]["misses"]:
                ui.log(
                    "%s differs for %s: /field says %s, the recount %s"
                    % (source, miss["dog_name"], show_value(key, miss["field"]), show_value(key, miss["recount"])),
                    tone="amber",
                )
        return out

    out["verdict"] = race_verdict(figures)
    ui.row(figure_line(ui, out["verdict"], figures))
    return out


# verdicts


def summarise(races, controls):
    verdicts = Counter(r.get("verdict") for r in races)
    figures = {}
    for key, _, _, source in FIGURES:
        tally = Counter()
        for race in races:
            for runner in race.get("runners") or []:
                item = (runner.get("figures") or {}).get(key)
                if item:
                    tally[vote(item)] += 1
        clean, leaks = tally.get("clean", 0), tally.get("leaks", 0)
        decided = clean + leaks
        if decided < MIN_DECIDED:
            verdict = "unclear"
        elif leaks >= AGREEMENT * decided:
            verdict = "leaks"
        elif clean >= AGREEMENT * decided:
            verdict = "clean"
        else:
            verdict = "mixed"
        figures[key] = {
            "field": source,
            "verdict": verdict,
            "clean": clean,
            "leaks": leaks,
            "same": tally.get("same", 0),
            "unclear": tally.get("unclear", 0),
            "not_available": tally.get("n/a", 0),
            "near": tally.get("near", 0),
            "decided": decided,
        }

    called = [item["verdict"] for item in figures.values() if item["verdict"] != "unclear"]
    if races and verdicts.get("refused", 0) == len(races):
        verdict = "refused"
    elif not called:
        verdict = "unclear"
    elif "mixed" in called:
        verdict = "mixed"
    elif all(v == "clean" for v in called):
        verdict = "clean"
    elif all(v == "leaks" for v in called):
        verdict = "leaks"
    else:
        verdict = "split"

    def blocks(wanted):
        return [source.split(".")[0] for key, _, _, source in FIGURES if figures[key]["verdict"] == wanted]

    headline = {
        "clean": "/field is cut at the off for every figure checked",
        "leaks": "/field serves every figure checked from current form state",
        "split": "/field serves %s from current form state, and cuts %s at the off"
        % (join_words(blocks("leaks")), join_words(blocks("clean"))),
        "mixed": "%s reads clean on some runners and leaks on others" % join_words(blocks("mixed")),
        "unclear": "the recounts could not separate the two readings",
        "refused": "/field would not answer for races that have run",
    }[verdict]
    meaning = {
        "clean": "Every figure checked matched the field as it stood before the off, so /field can be backtested directly.",
        "leaks": (
            "Every figure checked includes the race itself and the dogs' later runs. A backtest on them would "
            "be measuring its own lookahead."
        ),
        "split": (
            "The figures served from current state include the race itself and the dogs' later runs, so a "
            "backtest cannot use them as served. The figures cut at the off can be used."
        ),
        "mixed": "The per-race lines above show which races each reading came from.",
        "unclear": "Too few runners separated the two readings to call any figure. Check the control line first.",
        "refused": "History cannot be replayed through /field as it stands. Each refusal and its error code is listed above.",
    }[verdict]

    scope = {}
    probed = [c for c in controls if c.get("scope_check")]
    for key, _, _, _ in FIGURES:
        scope[key] = {
            "runners": sum(c["scope_check"][key]["runners"] for c in probed),
            "match": sum(c["scope_check"][key]["match"] for c in probed),
            "misses": [miss for c in probed for miss in c["scope_check"][key]["misses"]][:3],
            "tested": any(c["scope_check"][key]["tested"] for c in probed),
        }

    return {
        "verdict": verdict,
        "headline": headline,
        "meaning": meaning,
        "figures": figures,
        "leaking": [source for key, _, _, source in FIGURES if figures[key]["verdict"] == "leaks"],
        "cut_at_off": [source for key, _, _, source in FIGURES if figures[key]["verdict"] == "clean"],
        "races": len(races),
        "races_by_verdict": dict(verdicts),
        "state_built_after_off": sum(1 for r in races if (r.get("state_vs_off_hours") or 0) > 0),
        "state_times_known": sum(1 for r in races if r.get("state_vs_off_hours") is not None),
        "controls_probed": len(probed),
        "control_scope": scope,
        "races_with_more_runners_than_traps": sum(
            1 for r in list(races) + list(controls) if r.get("runner_count", 0) > (r.get("distinct_traps") or 0) > 0
        ),
    }


def verdict_panel(ui, title, summary, calls, evidence, control_note=None):
    verdict = summary["verdict"]
    tone = VERDICT_TONES[verdict]
    inner = ui.width - 4
    head = ui.wrap(summary["headline"], inner - 10)
    lines = [ui.paint(verdict.upper().ljust(10), tone, True) + ui.paint(head[0], "white", True)]
    lines.extend(" " * 10 + ui.paint(part, "white", True) for part in head[1:])
    lines.append("")
    lines.extend(ui.wrap(summary["meaning"], inner))
    lines.append("")
    lines.append(
        ui.paint("figure".ljust(30), "muted")
        + ui.paint("reads as".ljust(10), "muted")
        + ui.paint("runners that separate the two readings", "muted")
    )
    for key, _, _, source in FIGURES:
        item = summary["figures"][key]
        shown = item["verdict"]
        item_tone = READ_TONES.get(shown, "amber")
        if shown in ("clean", "leaks"):
            share = item[shown] / float(item["decided"])
            detail = ("%d of %d" % (item[shown], item["decided"])).ljust(11) + ui.bar(share, 20, item_tone)
            detail += "  %3d%%" % int(round(share * 100))
        elif shown == "mixed":
            detail = "%d clean, %d leak" % (item["clean"], item["leaks"])
        else:
            detail = "%d, too few to call" % item["decided"]
        if item.get("near"):
            detail += "   %d near, not counted" % item["near"]
        lines.append(source.ljust(30) + ui.paint(shown.upper().ljust(10), item_tone, True) + detail)
    lines.append("")

    def stat(label, value):
        return ui.paint(label.ljust(26), "muted") + value

    by_verdict = summary["races_by_verdict"]
    lines.append(
        stat(
            "Races",
            ", ".join("%d %s" % (by_verdict[name], name) for name in ("split", "leaks", "clean", "unclear", "refused") if by_verdict.get(name))
            or "none",
        )
    )
    lines.append(stat("State built after the off", "%d of %d races" % (summary["state_built_after_off"], summary["state_times_known"])))
    scope = summary["control_scope"]
    if summary["controls_probed"]:
        lines.append(
            stat(
                "Control",
                ", ".join(
                    "%s %d of %d%s" % (short, scope[key]["match"], scope[key]["runners"], "" if scope[key]["tested"] else " untested")
                    for key, _, short, _ in FIGURES
                ),
            )
        )
    else:
        lines.append(stat("Control", control_note or "no race left to run today, skipped"))
    if summary["races_with_more_runners_than_traps"]:
        extra = summary["races_with_more_runners_than_traps"]
        lines.append(
            stat("More runners than traps", "%d %s, so reserves or non-runners are in the field" % (extra, "race" if extra == 1 else "races"))
        )
    lines.append(stat("Calls this run", str(calls)))
    lines.append(stat("Evidence", evidence))

    notes = []
    for key, _, _, source in FIGURES:
        item = scope[key]
        if item["runners"] and item["match"] < item["runners"]:
            miss = item["misses"][0] if item["misses"] else None
            example = ""
            if miss:
                example = " (%s: /field %s, recount %s)" % (
                    miss["dog_name"],
                    show_value(key, miss["field"]),
                    show_value(key, miss["recount"]),
                )
            notes.append(
                "On the control, %s matched a plain recount on %d of %d runners%s, so its reads carry that allowance."
                % (source, item["match"], item["runners"], example)
            )
    for key, _, _, source in FIGURES:
        if scope[key]["runners"] and not scope[key]["tested"]:
            notes.append("%s could not be tested on the control: every value on that card was zero." % source)
    if notes:
        lines.append("")
        for note in notes:
            lines.extend(ui.paint(part, "amber") for part in ui.wrap(note, inner))
    ui.blank()
    ui.box(title, lines, right=datetime.now().strftime("%H:%M:%S"), tone=tone)


# a region, then the run


def probe_region(ui, client, region, count, resample, with_control, evidence):
    today = datetime.now(timezone.utc).date()
    sample_path = DATA / "probe" / ("sample-%s.json" % region)
    saved = load_json(sample_path)
    reused = False
    if not resample and isinstance(saved, dict) and len(saved.get("races") or []) == count:
        races = saved["races"]
        reused = True
        for race in races:
            day = as_date(race.get("date_local"))
            race["days_back"] = (today - day).days if day else None
        ui.log(
            "Reusing the %d %s races saved on %s, so this run compares like with like (--resample picks new ones)"
            % (count, region, str(saved.get("created_utc") or "")[:10])
        )
    else:
        races = select_sample(ui, client, region, count)
        write_json(
            sample_path,
            {"tool": TOOL, "version": __version__, "region": region, "created_utc": iso(datetime.now(timezone.utc)), "races": races},
        )

    control_ref = select_control(ui, client, region) if with_control else None
    if with_control and control_ref is None:
        ui.log("No %s race is left to run today, so the control is skipped" % region, tone="amber")

    ui.blank()
    ui.log(
        "Probing %d %s races%s, about %d calls"
        % (
            len(races),
            region,
            " and a control" if control_ref else "",
            sum(2 + (as_int(r.get("field_size")) or 6) for r in races)
            + ((2 + (as_int(control_ref.get("field_size")) or 6)) if control_ref else 0),
        ),
        bold=True,
    )

    region_dir = private_dir(evidence / region)
    control = probe_race(ui, client, control_ref, "CONTROL", region_dir, control=True) if control_ref else None
    results = [probe_race(ui, client, race, "%d/%d" % (index, len(races)), region_dir) for index, race in enumerate(races, 1)]

    summary = summarise(results, [control] if control else [])
    verdict_panel(
        ui,
        "VERDICT  %s  %s" % (region, REGION_NAMES.get(region, region)),
        summary,
        client.calls,
        relative(region_dir),
        None if with_control else "skipped with --no-control",
    )
    return {
        "region": region,
        "region_name": REGION_NAMES.get(region, region),
        "sample_file": relative(sample_path),
        "sample_reused": reused,
        "control": control,
        "races": results,
        "summary": summary,
    }


def run(ui, client, usage, regions, count, resample, with_control):
    started = datetime.now(timezone.utc)
    evidence = private_dir(DATA / "probe" / "runs" / started.strftime("%Y%m%d-%H%M%S"))
    reports = [probe_region(ui, client, region, count, resample, with_control, evidence) for region in regions]

    every_race = [race for report in reports for race in report["races"]]
    controls = [report["control"] for report in reports if report["control"]]
    overall = summarise(every_race, controls)

    report = {
        "tool": TOOL,
        "version": __version__,
        "command": "probe",
        "question": QUESTION,
        "run_utc": iso(started),
        "finished_utc": iso(datetime.now(timezone.utc)),
        "api": {
            "base_url": client.base,
            "key_prefix": usage.get("key_prefix"),
            "plan": usage.get("plan"),
            "paced_per_minute": client.per_minute(),
        },
        "criteria": dict(CRITERIA, regions=regions, races_per_region=count, control=with_control, resample=resample),
        "calls": {"total": client.calls, "by_endpoint": client.by_endpoint},
        "verdict": overall,
        "regions": reports,
        "evidence_dir": relative(evidence),
    }
    path = write_json(evidence / "report.json", report)
    if len(regions) > 1:
        verdict_panel(
            ui,
            "VERDICT  ALL REGIONS",
            overall,
            client.calls,
            relative(evidence),
            None if with_control else "skipped with --no-control",
        )
    ui.log("Report saved to %s" % relative(path), tone="teal")
    return report
