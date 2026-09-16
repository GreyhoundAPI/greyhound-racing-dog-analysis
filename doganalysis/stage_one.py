"""Stage one, the starting line: does the dog topping each /field measurement win more than its fair share?

Every figure comes from the cache. A race is ranked on a measurement when at least one runner clears the
endpoint's own six-run line on it; the best value tops the race, and top dogs that tie share the win. Each
measurement is set against the trap pick on the same races, month by month, because with tens of thousands
of races almost any gap over fair share clears a significance test on its own.
"""

import math
from collections import Counter

from . import store

MIN_RUNS = 6
MIN_FIELD = 4
MIN_RACES = 1000
MIN_MONTHS = 3
MONTH_FLOOR = 50
SIGNIFICANCE = 0.001
AHEAD_SHARE = 0.75

# key, label, /field block, value inside the block, which end ranks first
MEASURES = (
    ("pace", "pace, early split", "pace", "avg_first_split_s", "low"),
    ("here", "here, track and trip", "here", "win_pct", "high"),
    ("grade", "at this grade", "at_this_grade", "win_pct", "high"),
    ("layoff", "layoff band", "layoff", "win_pct", "high"),
)
TRAP = ("trap", "trap (control)", "from_this_trap", "win_pct", "high")
THIN_LABELS = ("none", "one", "two", "three or more")

CRITERIA = {
    "ranking": (
        "a runner ranks on a measurement once the runs behind that figure reach %d, the endpoint's own line; "
        "pace ranks the quickest average early split first, every other measurement the highest win percentage"
        % MIN_RUNS
    ),
    "ties": "top dogs that tie share the win, so a two-way tie with the winner credits half a win",
    "fair_share": "one in the number of runners that finished the race",
    "races": (
        "complete, non-handicap races with a single winner, at least %d finishers and a field carrying "
        "meta.form_cutoff; void, abandoned and unsettled races are counted and kept out" % MIN_FIELD
    ),
    "significance": "chi-squared goodness of fit against fair share, one degree of freedom, called at p < %g" % SIGNIFICANCE,
    "trap_control": (
        "the runner drawn in the trap with the best strike rate at this track and trip, compared with each "
        "measurement on exactly the same races"
    ),
    "months": (
        "a month counts once a measurement ranks %d races in it; a measurement beats the trap pick when its "
        "top dog wins more often overall, clears the significance line, and is ahead in at least %d%% of the "
        "months that count" % (MONTH_FLOOR, int(AHEAD_SHARE * 100))
    ),
    "floor": "a measurement is only called from %d ranked races across %d months that count" % (MIN_RACES, MIN_MONTHS),
    "thin": "races split by how many runners /field names in its own thin list",
}


def block_value(block, name):
    if not isinstance(block, dict):
        return None
    if name in block:
        return block[name]
    for key, value in block.items():
        if key.endswith(name):
            return value
    return None


def as_float(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def top_credit(runners, winner, block_name, value_name, order):
    """The winner's share of the top spot on one measurement, or None when nobody ranks."""
    eligible = []
    for runner in runners:
        block = runner.get(block_name)
        value = as_float(block_value(block, value_name))
        runs = as_float(block_value(block, "runs"))
        if value is None and value_name == "win_pct" and runs:
            wins = as_float(block_value(block, "wins"))
            value = 100.0 * wins / runs if wins is not None else None
        if value is None or runs is None or runs < MIN_RUNS:
            continue
        eligible.append((value, runner.get("dog_id")))
    if not eligible:
        return None
    best = min(v for v, _ in eligible) if order == "low" else max(v for v, _ in eligible)
    top = [dog for value, dog in eligible if value == best]
    return (1.0 / len(top)) if winner in top else 0.0


def new_stat():
    return {"n": 0, "w": 0.0, "f": 0.0, "trap_n": 0, "trap_w": 0.0, "months": {}, "thin": [[0, 0.0] for _ in THIN_LABELS]}


def add(stat, credit, fair, month, thin, trap_credit):
    stat["n"] += 1
    stat["w"] += credit
    stat["f"] += fair
    cell = stat["months"].setdefault(month, {"n": 0, "w": 0.0, "trap_n": 0, "trap_w": 0.0})
    cell["n"] += 1
    cell["w"] += credit
    if trap_credit is not None:
        stat["trap_n"] += 1
        stat["trap_w"] += trap_credit
        cell["trap_n"] += 1
        cell["trap_w"] += trap_credit
    stat["thin"][thin][0] += 1
    stat["thin"][thin][1] += credit


def chi_square(n, observed, expected):
    if expected <= 0 or n - expected <= 0:
        return 0.0
    gap = observed - expected
    return gap * gap / expected + gap * gap / (n - expected)


def month_list(first, last):
    months, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append("%04d-%02d" % (year, month))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


def compute(region, first, last):
    counts = Counter()
    stats = dict((key, new_stat()) for key in [m[0] for m in MEASURES] + [TRAP[0]])
    for day in store.days_between(first, last):
        payload = store.load_day(region, day)
        if not payload:
            counts["uncached_days"] += 1
            continue
        month = day.strftime("%Y-%m")
        counts["unresolved"] += int((payload.get("results_meta") or {}).get("unresolved") or 0)
        fields = payload.get("fields") or {}
        for race in payload.get("races") or []:
            counts["complete"] += 1
            if race.get("is_handicap"):
                counts["handicaps"] += 1
                continue
            positions = [p for p in (race.get("result") or {}).get("positions") or [] if isinstance(p.get("position"), int)]
            winners = [p for p in positions if p["position"] == 1]
            if len(positions) < MIN_FIELD:
                counts["small_fields"] += 1
                continue
            if len(winners) != 1:
                counts["dead_heats"] += 1
                continue
            item = fields.get(str(race.get("race_id")))
            if not item:
                counts["no_field"] += 1
                continue
            if not (item.get("meta") or {}).get("form_cutoff"):
                counts["not_cut"] += 1
                continue
            finished = set(p.get("dog_id") for p in positions)
            data = item.get("data") or {}
            runners = [r for r in data.get("runners") or [] if r.get("dog_id") in finished]
            if len(runners) < 2:
                counts["no_field"] += 1
                continue
            counts["ranked"] += 1
            winner = winners[0].get("dog_id")
            fair = 1.0 / len(positions)
            thin = min(3, len(data.get("thin") or []))
            trap_credit = top_credit(runners, winner, TRAP[2], TRAP[3], TRAP[4])
            if trap_credit is not None:
                add(stats[TRAP[0]], trap_credit, fair, month, thin, trap_credit)
            for key, _, block, value, order in MEASURES:
                credit = top_credit(runners, winner, block, value, order)
                if credit is not None:
                    add(stats[key], credit, fair, month, thin, trap_credit)

    months = month_list(first, last)
    summary = {}
    for key, label, _, _, _ in MEASURES + (TRAP,):
        stat = stats[key]
        entry = {"label": label, "races": stat["n"]}
        if stat["n"]:
            rate, fair = stat["w"] / stat["n"], stat["f"] / stat["n"]
            chi = chi_square(stat["n"], stat["w"], stat["f"])
            trap_rate = stat["trap_w"] / stat["trap_n"] if stat["trap_n"] else None
            strip = []
            for month in months:
                cell = stat["months"].get(month)
                counted = bool(cell) and cell["n"] >= MONTH_FLOOR and cell["trap_n"] > 0
                strip.append(
                    {
                        "month": month,
                        "races": cell["n"] if cell else 0,
                        "rate": round(cell["w"] / cell["n"], 4) if cell and cell["n"] else None,
                        "trap_rate": round(cell["trap_w"] / cell["trap_n"], 4) if cell and cell["trap_n"] else None,
                        "counts": counted,
                        "ahead": counted and cell["w"] / cell["n"] > cell["trap_w"] / cell["trap_n"],
                    }
                )
            counted = [m for m in strip if m["counts"]]
            ahead = sum(1 for m in counted if m["ahead"])
            entry.update(
                {
                    "top_dog_won": round(rate, 4),
                    "fair_share": round(fair, 4),
                    "lift": round(rate / fair - 1, 4) if fair else None,
                    "chi_square": round(chi, 1),
                    "p": math.erfc(math.sqrt(chi / 2.0)),
                    "trap_same_races": round(trap_rate, 4) if trap_rate is not None else None,
                    "vs_trap_points": round((rate - trap_rate) * 100, 2) if trap_rate is not None and key != TRAP[0] else None,
                    "months": strip,
                    "months_counted": len(counted),
                    "months_ahead": ahead,
                    "thin": [
                        {"thin": THIN_LABELS[i], "races": n, "top_dog_won": round(w / n, 4) if n else None}
                        for i, (n, w) in enumerate(stat["thin"])
                    ],
                }
            )
            called = stat["n"] >= MIN_RACES and len(counted) >= MIN_MONTHS
            entry["called"] = called
            if key != TRAP[0]:
                entry["beats_trap"] = bool(
                    called
                    and entry["vs_trap_points"] is not None
                    and entry["vs_trap_points"] > 0
                    and entry["p"] < SIGNIFICANCE
                    and ahead >= AHEAD_SHARE * len(counted)
                )
        summary[key] = entry
    return {"region": region, "from": first.isoformat(), "to": last.isoformat(), "counts": dict(counts), "measures": summary, "criteria": CRITERIA}


# the panel


def _nice(text):
    year, month, day = [int(x) for x in text.split("-")]
    return "%d %s %d" % (day, "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[month - 1], year)


def _num(value):
    return "{:,}".format(int(value))


def _join(words):
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def panel(ui, result):
    counts, measures = result["counts"], result["measures"]
    ranked = counts.get("ranked", 0)
    on_cards = counts.get("complete", 0) + counts.get("unresolved", 0)
    inner = ui.width - 4
    lines = [
        ui.paint("Does the dog topping each measurement win more than its fair share of the field?", "white", True),
        ui.paint(
            "A dog ranks on a measurement once it has %d runs behind it, the endpoint's own line. Tied top dogs share the win." % MIN_RUNS,
            "muted",
        ),
        ui.paint("Ranked %s of %s races." % (_num(ranked), _num(on_cards)), "muted"),
    ]
    out = [
        ("handicaps", "handicaps"),
        ("unresolved", "void, abandoned or unsettled"),
        ("small_fields", "fields under four"),
        ("dead_heats", "dead heats"),
        ("no_field", "with no field"),
        ("not_cut", "with a field not cut at the off"),
    ]
    parts = ["%s %s" % (_num(counts[k]), label) for k, label in out if counts.get(k)]
    if parts:
        lines.extend(ui.paint(part, "muted") for part in ui.wrap("Left out: " + _join(parts) + ".", inner))
    if counts.get("uncached_days"):
        lines.append(ui.paint("%d days in the range are not in the cache yet." % counts["uncached_days"], "amber"))
    lines.append("")

    if not ranked:
        lines.append(ui.paint("Nothing in the cache for this range can be ranked yet.", "amber", True))
        ui.box("STAGE ONE  THE STARTING LINE", lines, right="%s  %s to %s" % (result["region"], _nice(result["from"]), _nice(result["to"])), tone="amber")
        return

    head = (
        "measurement".ljust(24) + "races".rjust(8) + "   " + "top dog won".ljust(24) + "fair".rjust(6) + "lift".rjust(7)
        + "χ²".rjust(9) + "p".rjust(9) + "vs trap".rjust(11)
    )
    lines.append(ui.paint(head, "muted"))
    scale = max([m.get("top_dog_won") or 0 for m in measures.values()] + [0.3])
    for key, label, _, _, _ in MEASURES + (TRAP,):
        m = measures[key]
        if not m["races"]:
            lines.append(ui.paint(label.ljust(24), "white", True) + ui.paint("   0   nobody clears the six-run line here", "muted"))
            continue
        is_trap = key == TRAP[0]
        cell = ("%.1f%%" % (m["top_dog_won"] * 100)).rjust(6) + " " + ui.bar(m["top_dog_won"] / scale, 16, "blue" if is_trap else "green")
        significant = m["p"] < SIGNIFICANCE
        row = (
            ui.paint(label.ljust(24), "white", True) + _num(m["races"]).rjust(8) + "   " + cell + " "
            + ("%.1f%%" % (m["fair_share"] * 100)).rjust(6)
            + ui.paint(("%+d%%" % round(m["lift"] * 100)).rjust(7), "green" if m["lift"] > 0 else "red")
            + _num(m["chi_square"]).rjust(9)
            + ui.paint(("<0.001" if significant else "%.3f" % m["p"]).rjust(9), "green" if significant else "amber")
        )
        if is_trap:
            row += ui.paint("control".rjust(11), "muted")
        elif m["vs_trap_points"] is None:
            row += ui.paint("-".rjust(11), "muted")
        else:
            row += ui.paint(("%+.1f pts" % m["vs_trap_points"]).rjust(11), "green" if m["vs_trap_points"] > 0 else "red", True)
        lines.append(row)

    months = measures[MEASURES[0][0]].get("months") or next((m.get("months") for m in measures.values() if m.get("months")), [])
    wide = len(months) <= 36
    lines.append("")
    lines.append(ui.paint("Month by month: did its top dog win more often than the trap pick?", "white", True))
    for key, label, _, _, _ in MEASURES:
        m = measures[key]
        if not m.get("months"):
            continue
        strip = ""
        for cell in m["months"]:
            tone = "track" if not cell["counts"] else ("green" if cell["ahead"] else "red")
            strip += ui.paint(ui.g["full"], tone) + (" " if wide else "")
        counted, ahead = m["months_counted"], m["months_ahead"]
        good = counted and ahead >= AHEAD_SHARE * counted
        lines.append(
            ui.paint(label.ljust(24), "white", True) + strip + " "
            + ui.paint(("%2d of %d" % (ahead, counted)) if counted else "no month counts yet", "green" if good else ("red" if counted else "muted"), True)
        )
    lines.append(ui.paint(TRAP[1].ljust(24), "white", True) + ui.paint("the baseline every row above is measured against", "muted"))
    if months:
        if wide:
            letters = " ".join("JFMAMJJASOND"[int(cell["month"][5:]) - 1] for cell in months)
            years = ""
            for index, cell in enumerate(months):
                if index == 0 or cell["month"].endswith("-01"):
                    years = years.ljust(index * 2) + cell["month"][:4]
            lines.append(" " * 24 + ui.paint(letters, "muted"))
            lines.append(" " * 24 + ui.paint(years, "muted"))
        else:
            lines.append(" " * 24 + ui.paint("%s to %s, one cell a month" % (months[0]["month"], months[-1]["month"]), "muted"))
        lines.append(" " * 24 + ui.paint("grey: fewer than %d ranked races that month" % MONTH_FLOOR, "muted"))

    lines.append("")
    lines.append(ui.paint("The thin line: the top dog's strike rate by how many runners /field names as thin", "white", True))
    lines.append(ui.paint("thin runners".ljust(24) + "races".rjust(8) + "   " + "pace".ljust(18) + "here".ljust(18) + "at this grade", "muted"))
    thin_rows = zip(*[measures[k].get("thin") or [{"races": 0, "top_dog_won": None}] * 4 for k in ("pace", "here", "grade")])
    for index, row in enumerate(thin_rows):
        races = max(item["races"] for item in row)
        cells = ""
        for item in row:
            if item["top_dog_won"] is None:
                cells += "     -" + " " * 12
            else:
                cells += ("%.1f%%" % (item["top_dog_won"] * 100)).rjust(6) + " " + ui.bar(item["top_dog_won"] / scale, 10, "green") + " "
        lines.append(ui.paint(THIN_LABELS[index].ljust(24), "white", True) + _num(races).rjust(8) + "   " + cells)

    lines.append("")
    lines.extend(verdict_lines(ui, measures, inner))
    ui.box("STAGE ONE  THE STARTING LINE", lines, right="%s  %s to %s" % (result["region"], _nice(result["from"]), _nice(result["to"])), tone="teal")


def verdict_lines(ui, measures, inner):
    out = []
    beaters = [MEASURES[i] for i in range(len(MEASURES)) if measures[MEASURES[i][0]].get("beats_trap")]
    if beaters:
        least = min(measures[m[0]]["months_ahead"] for m in beaters)
        of = max(measures[m[0]]["months_counted"] for m in beaters)
        subject = _join(m[1].split(",")[0] for m in beaters)
        verb = "each beat" if len(beaters) > 1 else "beats"
        out.extend(
            ui.paint(part, "green", True)
            for part in ui.wrap("● %s %s the trap pick in at least %d months of %d." % (subject, verb, least, of), inner)
        )
    for key, label, _, _, _ in MEASURES:
        m = measures[key]
        name = label.split(",")[0]
        if not m.get("races"):
            continue
        if not m.get("called"):
            text = "▲ %s: too few races to call, %s ranked. Stage one calls a measurement from %s races across %d months." % (
                name, _num(m["races"]), _num(MIN_RACES), MIN_MONTHS)
            out.extend(ui.paint(part, "amber") for part in ui.wrap(text, inner))
            continue
        if m.get("beats_trap"):
            continue
        if m["vs_trap_points"] is None or m["vs_trap_points"] <= 0:
            reason = "its top dog wins less often than the trap pick on the same races"
        elif m["p"] >= SIGNIFICANCE:
            reason = "its gap over fair share does not clear p < %g" % SIGNIFICANCE
        else:
            reason = "its top dog beat the trap pick in %d months of %d" % (m["months_ahead"], m["months_counted"])
        out.extend(ui.paint(part, "red", True) for part in ui.wrap("%s %s does not: %s." % (ui.g["cross"], name, reason), inner))

    pace = measures.get("pace") or {}
    thin = pace.get("thin") or []
    rates = [item["top_dog_won"] for item in thin]
    if len(rates) == 4 and all(r is not None for r in rates) and thin[0]["races"] >= 100 and thin[3]["races"] >= 100:
        if rates[0] > rates[1] > rates[2] > rates[3]:
            text = "▲ The top dog fades as thin runners fill the race: pace falls from %.1f%% with none to %.1f%% with three or more." % (
                rates[0] * 100, rates[3] * 100)
            out.extend(ui.paint(part, "amber") for part in ui.wrap(text, inner))
    return out
