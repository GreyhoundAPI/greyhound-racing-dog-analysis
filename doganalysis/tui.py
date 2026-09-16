"""Terminal output: panels, colour, inline bars, arrow-key menus, a spinner and timestamped lines."""

import atexit
import getpass
import math
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from datetime import datetime

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP_WIDTH = 17

# 256-colour codes. Box borders are the only faint tone; everything read is kept bright.
TONES = {
    "border": 239,
    "track": 238,
    "muted": 250,
    "stamp": 110,
    "teal": 80,
    "green": 78,
    "red": 203,
    "amber": 221,
    "blue": 111,
    "orange": 214,
    "white": 255,
}

UNICODE = {
    "tl": "\u256d", "tr": "\u256e", "bl": "\u2570", "br": "\u256f", "h": "\u2500", "v": "\u2502",
    "full": "\u2588", "empty": "\u2591", "tick": "\u2713", "cross": "\u2717", "arrow": "\u25b8",
    "pointer": "\u276f", "dot": "\u00b7", "ellipsis": "\u2026",
    "spin": "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f",
}
ASCII = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
    "full": "#", "empty": ".", "tick": "+", "cross": "x", "arrow": ">",
    "pointer": ">", "dot": "|", "ellipsis": "~",
    "spin": "|/-\\",
}


def vlen(text):
    return len(ANSI.sub("", text))


def fit(text, width, ellipsis="~"):
    """Cut text to a visible width without breaking colour codes."""
    if width <= 0:
        return ""
    if vlen(text) <= width:
        return text
    out, seen, i = [], 0, 0
    keep = max(0, width - len(ellipsis))
    while i < len(text) and seen < keep:
        match = ANSI.match(text, i)
        if match:
            out.append(match.group(0))
            i = match.end()
            continue
        out.append(text[i])
        seen += 1
        i += 1
    tail = "\x1b[0m" if ANSI.search(text) else ""
    return "".join(out) + tail + ellipsis


def pad(text, width, align="left"):
    gap = max(0, width - vlen(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _unicode_ok(stream):
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "\u256d\u2500\u2588\u2591\u2713\u276f\u280b\u2026".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _enable_windows_terminal():
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class _Keys(object):
    """Read single key presses: up, down, enter, quit."""

    def __enter__(self):
        if os.name == "nt":
            import msvcrt

            self.msvcrt = msvcrt
        else:
            import termios
            import tty

            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if os.name != "nt":
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return False

    def read(self):
        if os.name == "nt":
            char = self.msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                return {"H": "up", "P": "down"}.get(self.msvcrt.getwch(), "")
            if char in ("\r", "\n"):
                return "enter"
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\x1b", "q"):
                return "quit"
            return {"k": "up", "w": "up", "j": "down", "s": "down"}.get(char, "")
        import select

        char = os.read(self.fd, 1)
        if char == b"\x1b":
            ready = select.select([self.fd], [], [], 0.05)[0]
            if not ready:
                return "quit"
            sequence = os.read(self.fd, 2)
            return {b"[A": "up", b"OA": "up", b"[B": "down", b"OB": "down"}.get(sequence, "")
        if char in (b"\r", b"\n"):
            return "enter"
        if char == b"q":
            return "quit"
        return {b"k": "up", b"w": "up", b"j": "down", b"s": "down"}.get(char, "")


class _Spinner(object):
    def __init__(self, ui, text):
        self.ui = ui
        self.text = text
        self.thread = None
        self.stop = threading.Event()
        self.outer = None

    def __enter__(self):
        ui = self.ui
        if ui.quiet or not ui.tty:
            return self
        if ui.spinner is not None:
            self.outer = ui.spinner
            self.saved_text = self.outer.text
            self.outer.text = self.text
            return self
        ui.spinner = self
        ui.cursor(False)
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()
        return self

    def _run(self):
        frames = self.ui.g["spin"]
        index = 0
        while not self.stop.is_set():
            with self.ui.lock:
                frame = self.ui.paint(frames[index % len(frames)], "teal")
                line = "%s  %s %s" % (self.ui.stamp(), frame, self.text)
                self.ui.out.write("\r\x1b[2K" + fit(line, self.ui.width - 1, self.ui.g["ellipsis"]))
                self.ui.out.flush()
            index += 1
            self.stop.wait(0.09)

    def __exit__(self, *exc):
        if self.outer is not None:
            self.outer.text = self.saved_text
            return False
        if self.thread is not None:
            self.stop.set()
            self.thread.join()
            with self.ui.lock:
                self.ui.out.write("\r\x1b[2K")
                self.ui.out.flush()
            self.ui.spinner = None
            self.ui.cursor(True)
        return False


class UI(object):
    def __init__(self, quiet=False, long_clock=False):
        self.quiet = quiet
        self.long_clock = long_clock
        self.stamp_width = STAMP_WIDTH + 2 if long_clock else STAMP_WIDTH
        self.out = sys.stdout
        self.tty = (not quiet) and self.out.isatty()
        if self.tty and os.name == "nt":
            _enable_windows_terminal()
            self.out = sys.stdout
        self.colour = self.tty and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        self.g = UNICODE if _unicode_ok(self.out) else ASCII
        self.width = self._measure()
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.spinner = None
        self.cursor_hidden = False
        atexit.register(self.cursor, True)

    # basics

    def _measure(self):
        if not self.tty:
            return 120
        columns = shutil.get_terminal_size((100, 30)).columns
        return max(76, min(120, columns))

    def interactive(self):
        return self.tty and sys.stdin.isatty()

    def paint(self, text, tone=None, bold=False):
        if not self.colour or (tone is None and not bold):
            return text
        codes = []
        if bold:
            codes.append("1")
        if tone:
            codes.append("38;5;%d" % TONES[tone])
        return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text)

    def cursor(self, visible):
        if not self.tty:
            return
        if visible and self.cursor_hidden:
            self.out.write("\x1b[?25h")
            self.out.flush()
            self.cursor_hidden = False
        elif not visible and not self.cursor_hidden:
            self.out.write("\x1b[?25l")
            self.out.flush()
            self.cursor_hidden = True

    def stamp(self):
        clock = datetime.now().strftime("%H:%M:%S")
        elapsed = int(time.monotonic() - self.started)
        if self.long_clock:
            text = "%s +%d:%02d:%02d" % (clock, elapsed // 3600, elapsed // 60 % 60, elapsed % 60)
        else:
            text = "%s +%02d:%02d" % (clock, elapsed // 60, elapsed % 60)
        return self.paint(text, "stamp")

    def _write(self, text):
        with self.lock:
            if self.spinner is not None and self.tty:
                self.out.write("\r\x1b[2K")
            self.out.write(text + "\n")
            self.out.flush()

    def blank(self):
        if not self.quiet:
            self._write("")

    # lines

    def log(self, text, tone=None, bold=False):
        """A timestamped line of plain text, wrapped under its own indent."""
        if self.quiet:
            return
        width = self.width - self.stamp_width
        lines = textwrap.wrap(str(text), width) or [""]
        self._write("%s  %s" % (self.stamp(), self.paint(lines[0], tone, bold)))
        for extra in lines[1:]:
            self._write(" " * self.stamp_width + self.paint(extra, tone, bold))

    def row(self, painted):
        """A timestamped line that is already coloured and laid out."""
        if self.quiet:
            return
        self._write("%s  %s" % (self.stamp(), fit(painted, self.width - self.stamp_width, self.g["ellipsis"])))

    def cont(self, painted):
        """A line under a timestamped one, indented to its text."""
        if self.quiet:
            return
        self._write(" " * self.stamp_width + fit(painted, self.width - self.stamp_width, self.g["ellipsis"]))

    def wrap(self, text, width):
        return textwrap.wrap(str(text), width) or [""]

    def bar(self, fraction, width, tone="green"):
        fraction = max(0.0, min(1.0, fraction or 0.0))
        full = int(round(fraction * width))
        return self.paint(self.g["full"] * full, tone) + self.paint(self.g["empty"] * (width - full), "track")

    def box(self, title, lines, right=None, tone="teal"):
        if self.quiet:
            return
        g = self.g
        width = self.width
        inner = width - 4
        head = " %s " % title
        tail = " %s " % right if right else ""
        fill = width - 4 - len(head) - len(tail)
        if fill < 1:
            tail = ""
            fill = max(1, width - 4 - len(head))
        top = (
            self.paint(g["tl"] + g["h"], "border")
            + self.paint(head, tone, True)
            + self.paint(g["h"] * fill, "border")
            + (self.paint(tail, "muted") if tail else "")
            + self.paint(g["h"] + g["tr"], "border")
        )
        self._write(top)
        side = self.paint(g["v"], "border")
        for line in lines:
            self._write("%s %s %s" % (side, pad(fit(line, inner, g["ellipsis"]), inner), side))
        self._write(self.paint(g["bl"] + g["h"] * (width - 2) + g["br"], "border"))

    # waiting

    def busy(self, text):
        return _Spinner(self, text)

    def sleep(self, seconds, reason):
        """Pacing and retry waits: say so on a timestamped line, then count down on the spinner."""
        if seconds <= 0:
            return
        if not self.quiet:
            self.log("waiting %ds: %s" % (int(math.ceil(seconds)), reason), tone="amber")
        end = time.monotonic() + seconds
        spinner = self.spinner
        if spinner is None or not self.tty:
            time.sleep(seconds)
            return
        saved = spinner.text
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            spinner.text = "%s, %ds left" % (reason, int(math.ceil(left)))
            time.sleep(min(0.25, left))
        spinner.text = saved

    # input

    def menu(self, title, options, default=0):
        """Arrow-key selection. options: list of (label, hint). Non-interactive runs take the default."""
        if not self.interactive():
            return default
        index = default
        label_width = max(vlen(label) for label, _ in options) + 3
        self._write("%s  %s" % (self.stamp(), self.paint(title, "white", True)))
        hint_line = self.paint("   up and down to move, enter to choose", "muted")
        self._write(" " * self.stamp_width + hint_line)

        def render(first):
            if not first:
                self.out.write("\x1b[%dA" % len(options))
            for position, (label, hint) in enumerate(options):
                chosen = position == index
                pointer = self.paint(self.g["pointer"], "orange", True) if chosen else " "
                name = self.paint(pad(label, label_width), "white", True) if chosen else pad(label, label_width)
                line = " " * self.stamp_width + " %s %s%s" % (pointer, name, self.paint(hint, "muted"))
                self.out.write("\r\x1b[2K" + line + "\n")
            self.out.flush()

        self.cursor(False)
        try:
            render(True)
            with _Keys() as keys:
                while True:
                    key = keys.read()
                    if key == "up":
                        index = (index - 1) % len(options)
                        render(False)
                    elif key == "down":
                        index = (index + 1) % len(options)
                        render(False)
                    elif key == "enter":
                        break
                    elif key == "quit":
                        raise KeyboardInterrupt
        finally:
            self.cursor(True)
        # collapse the list and the hint into one confirmed line
        self.out.write("\x1b[%dA" % (len(options) + 1))
        for _ in range(len(options) + 1):
            self.out.write("\r\x1b[2K\n")
        self.out.write("\x1b[%dA" % (len(options) + 1))
        label, hint = options[index]
        self._write(
            " " * self.stamp_width
            + "%s %s  %s" % (self.paint(self.g["tick"], "green", True), self.paint(label, "white", True), self.paint(hint, "muted"))
        )
        return index

    def ask_secret(self, prompt):
        text = "%s  %s: " % (self.stamp(), self.paint(prompt, "white", True))
        try:
            return getpass.getpass(text)
        except (EOFError, getpass.GetPassWarning):
            return ""

    def ask_text(self, prompt):
        text = "%s  %s: " % (self.stamp(), self.paint(prompt, "white", True))
        try:
            return input(text).strip()
        except EOFError:
            return ""

    def stopped(self):
        self.cursor(True)
        if not self.quiet:
            if self.tty:
                self.out.write("\r\x1b[2K")
            self.log("Stopped. Everything already saved stays in data/, and a rerun carries on from there.", tone="amber")
