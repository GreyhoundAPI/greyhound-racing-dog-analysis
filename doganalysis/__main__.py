"""python3 -m doganalysis: the command line."""

import argparse
import json
import sys

from . import REPO_URL, TOOL, __version__, analyse, probe
from .api import ApiError, Client
from .config import CONFIG, base_url, relative, saved_key, store_key, web_user_note
from .tui import UI

PLAN_NAMES = {
    "sandbox": "Free key",
    "free": "Free key",
    "test": "Free key",
    "live": "Live",
    "live_plus": "Live Plus",
}
PRICING_URL = "https://greyhoundapi.com/pricing"
FIELD_DOCS_URL = "https://greyhoundapi.com/documentation/race-field"


def plan_key(plan):
    return "".join(ch if ch.isalnum() else "_" for ch in str(plan or "").strip().lower())


def plan_name(plan):
    return PLAN_NAMES.get(plan_key(plan), str(plan or "unknown"))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m doganalysis",
        description="Does the race field pick winners, and did the price already know? %s" % REPO_URL,
    )
    parser.add_argument("--version", action="version", version="%s %s" % (TOOL, __version__))
    commands = parser.add_subparsers(dest="command")

    run = commands.add_parser("analyse", aliases=["analyze"], help="fetch a range into the cache, ready for both stages")
    run.add_argument("--region", type=str.upper, choices=["GB", "AU", "BOTH"], help="region to analyse (asked when left out)")
    run.add_argument("--from", dest="date_from", help="first day, YYYY-MM-DD")
    run.add_argument("--to", dest="date_to", help="last day, YYYY-MM-DD (yesterday when left out)")
    run.add_argument("--last", type=int, help="the last N days, up to yesterday")
    run.add_argument("--commission", type=float, help="commission on winnings in percent, %g when left out" % analyse.DEFAULT_COMMISSION)
    run.add_argument("--yes", action="store_true", help="fetch once the cost panel is shown, without asking")
    run.add_argument("--key", help="use this key for one run without saving it")

    check = commands.add_parser("probe", help="check whether /field on races that have run is cut at the off")
    check.add_argument("--region", type=str.upper, choices=["GB", "AU", "BOTH"], help="region to probe (asked when left out)")
    check.add_argument(
        "--races",
        type=int,
        default=probe.DEFAULT_RACES,
        help="races to sample per region, spread back through the archive (default %d, most 24)" % probe.DEFAULT_RACES,
    )
    check.add_argument("--resample", action="store_true", help="pick new races instead of reusing the saved sample")
    check.add_argument("--no-control", action="store_true", help="skip today's control race that has not run yet")
    check.add_argument("--json", action="store_true", help="print only the JSON report and never prompt")
    check.add_argument("--key", help="use this key for one run without saving it")

    commands.add_parser("key", help="replace the saved API key")
    return parser


def banner(ui, subtitle, kind):
    if kind == "analyse":
        lines = [
            ui.paint("Does the race field pick winners, and did the price already know?", "white", True),
            "",
            "Stage one ranks every race on each /field measurement and counts how often the top dog wins against",
            "its fair share, with the trap figure as the control. Stage two asks whether the back price already knew.",
        ]
    else:
        lines = [
            ui.paint("Does the race field pick winners, and did the price already know?", "white", True),
            "",
            "The probe checks the ground first: does /field show a race that has already run as it stood",
            "before the off, or as the form state stands now?",
            "",
            ui.paint("field   ", "teal", True) + "what /field says for here, at_this_grade, pace and layoff",
            ui.paint("off     ", "teal", True) + "the dog's own history, recounted up to the off",
            ui.paint("built   ", "teal", True) + "the same recount, up to the day the form state was built",
            ui.paint("reads   ", "teal", True) + "each figure on its own: clean when field matches off, LEAKS when it matches built",
        ]
    ui.box("GREYHOUND RACING DOG ANALYSIS", lines, right="%s  v%s" % (subtitle, __version__), tone="orange")


def fail(ui, json_mode, message, code):
    if json_mode or ui.quiet:
        sys.stderr.write("%s: %s\n" % (TOOL, message))
    else:
        ui.log("%s %s" % (ui.g["cross"], message), tone="red")
    return code


def check_key(ui, client):
    with ui.busy("GET /v1/usage  checking the key"):
        payload = client.get("/usage", counted=False, endpoint="usage")
    data = payload.get("data") or {}
    minute = data.get("minute") or {}
    try:
        if minute.get("limit"):
            client.minute_limit = int(minute["limit"])
    except (TypeError, ValueError):
        pass
    return data


def key_problem(exc, source):
    if exc.code == "unauthorized" or (exc.status == 401 and exc.code != "basic_auth_gate"):
        return "The key from %s was not accepted (%d %s: %s). Run `python3 -m doganalysis key` to replace it." % (
            source,
            exc.status,
            exc.code,
            exc.message.rstrip(". ") or "no reason given",
        )
    if exc.code == "key_suspended":
        return "The key from %s is suspended: %s" % (source, exc.message)
    return exc.summary()


def prompt_for_key(ui):
    ui.log(
        "No saved key yet. It is asked for once, checked on /v1/usage, and saved to config.json "
        "where only this user can read it."
    )
    for _ in range(3):
        key = ui.ask_secret("GreyhoundAPI key (input hidden)").strip()
        if not key:
            ui.log("Nothing was pasted.", tone="amber")
            continue
        client = Client(key, base_url(), on_wait=ui.sleep)
        try:
            usage = check_key(ui, client)
        except ApiError as exc:
            ui.log("%s %s" % (ui.g["cross"], key_problem(exc, "the prompt")), tone="red")
            continue
        store_key(key)
        ui.log("%s Key saved to %s at mode 600." % (ui.g["tick"], relative(CONFIG)), tone="green")
        return key, client, usage
    ui.log("Three attempts without a working key, stopping.", tone="red")
    return None, None, None


def key_panel(ui, client, usage, source):
    month = usage.get("month") or {}
    used, limit = month.get("used"), month.get("limit")
    prefix = str(usage.get("key_prefix") or "")
    lines = [
        ui.paint("Key".ljust(13), "muted")
        + ui.paint((prefix + ui.g["ellipsis"]) if prefix else "unknown", "white", True)
        + "    "
        + ui.paint("Plan  ", "muted")
        + ui.paint(plan_name(usage.get("plan")), "white", True),
    ]
    if isinstance(used, int) and isinstance(limit, int) and limit > 0:
        share = used / float(limit)
        tone = "green" if share < 0.6 else ("amber" if share < 0.85 else "red")
        lines.append(
            ui.paint("This month".ljust(13), "muted")
            + ("{:,} of {:,}".format(used, limit)).ljust(22)
            + ui.bar(share, 20, tone)
            + "  %.1f%%" % (share * 100)
        )
    if client.minute_limit:
        lines.append(
            ui.paint("Pacing".ljust(13), "muted")
            + "%d calls a minute, 85%% of the key's %d" % (client.per_minute(), client.minute_limit)
        )
    lines.append(ui.paint("Key from".ljust(13), "muted") + source)
    ui.box("KEY", lines, tone="teal")


def resolve_key(args):
    import os

    if getattr(args, "key", None):
        return args.key.strip(), "--key (not saved)"
    if os.environ.get("GAPI_KEY"):
        return os.environ["GAPI_KEY"].strip(), "the GAPI_KEY environment variable"
    stored = saved_key()
    if stored:
        return stored, "%s (this user only)" % relative(CONFIG)
    return "", None


def open_client(ui, args, json_mode):
    """Key, check, panel and plan gate, shared by every command. Returns (client, usage, exit_code)."""
    note = web_user_note()
    if note:
        ui.log(note, tone="amber")
    key, source = resolve_key(args)
    client, usage = None, None
    if not key:
        if json_mode or not ui.interactive():
            return None, None, fail(
                ui,
                json_mode,
                "No API key. Pass --key, set GAPI_KEY, or run `python3 -m doganalysis key` once to save one.",
                2,
            )
        key, client, usage = prompt_for_key(ui)
        if not key:
            return None, None, 2
        source = "%s (this user only)" % relative(CONFIG)
    if client is None:
        client = Client(key, base_url(), on_wait=ui.sleep)
        try:
            usage = check_key(ui, client)
        except ApiError as exc:
            return None, None, fail(ui, json_mode, key_problem(exc, source), 3)

    key_panel(ui, client, usage, source)
    plan = plan_key(usage.get("plan"))
    if plan and "plus" not in plan:
        message = (
            "/field and /form are Live Plus endpoints and this key is on %s, so this stops here "
            "before spending a call. Live Plus: %s" % (plan_name(usage.get("plan")), PRICING_URL)
        )
        if json_mode:
            return None, None, fail(ui, json_mode, message, 3)
        ui.box("LIVE PLUS NEEDED", ui.wrap(message, ui.width - 4) + ["", "What /field returns: %s" % FIELD_DOCS_URL], tone="amber")
        return None, None, 3
    return client, usage, 0


def run_probe(ui, args, json_mode, show_banner=True):
    if args.races < 1 or args.races > 24:
        return fail(ui, json_mode, "--races takes a number from 1 to 24.", 2)
    if show_banner:
        banner(ui, "probe", "probe")
    client, usage, code = open_client(ui, args, json_mode)
    if code:
        return code

    region = args.region
    if not region:
        if ui.interactive():
            choice = ui.menu(
                "Region to probe",
                [("GB", "Great Britain"), ("AU", "Victoria, Australia"), ("Both", "GB first, then AU")],
                0,
            )
            region = ("GB", "AU", "BOTH")[choice]
        else:
            region = "GB"
    regions = ["GB", "AU"] if region == "BOTH" else [region]

    report = probe.run(ui, client, usage, regions, args.races, args.resample, not args.no_control)
    if json_mode:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def run_analyse(ui, args, show_banner=True):
    if args.commission is not None and not 0 <= args.commission <= 20:
        return fail(ui, False, "--commission takes a percentage from 0 to 20.", 2)
    if show_banner:
        banner(ui, "analyse", "analyse")
    client, usage, code = open_client(ui, args, False)
    if code:
        return code
    return analyse.run(ui, client, usage, args)


def replace_key(ui):
    banner(ui, "key", "probe")
    if not ui.interactive():
        return fail(ui, False, "Replacing the key needs an interactive terminal.", 2)
    key, client, usage = prompt_for_key(ui)
    if not key:
        return 2
    key_panel(ui, client, usage, "%s (this user only)" % relative(CONFIG))
    return 0


def home(ui, parser):
    banner(ui, "home", "analyse")
    choice = ui.menu(
        "What now",
        [
            ("Run the analysis", "choose a range, see what it costs, fetch it into the cache"),
            ("Run the lookahead probe", "checks /field on races that have already run"),
            ("Replace the saved API key", "kept in config.json, this user only"),
            ("Quit", ""),
        ],
        0,
    )
    if choice == 0:
        return run_analyse(ui, parser.parse_args(["analyse"]), show_banner=False)
    if choice == 1:
        return run_probe(ui, parser.parse_args(["probe"]), False, show_banner=False)
    if choice == 2:
        key, client, usage = prompt_for_key(ui)
        if key:
            key_panel(ui, client, usage, "%s (this user only)" % relative(CONFIG))
        return 0 if key else 2
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    long_clock = args.command in (None, "analyse", "analyze")
    ui = UI(quiet=json_mode, long_clock=long_clock)
    try:
        if args.command in ("analyse", "analyze"):
            return run_analyse(ui, args)
        if args.command == "probe":
            return run_probe(ui, args, json_mode)
        if args.command == "key":
            return replace_key(ui)
        if ui.interactive():
            return home(ui, parser)
        parser.print_help()
        return 0
    except KeyboardInterrupt:
        ui.stopped()
        return 130


if __name__ == "__main__":
    sys.exit(main())
