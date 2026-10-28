"""The message box drawn inside the popup when there is no dashboard to run."""
import re
import textwrap

TITLES = {"personal": "Personal Dashboard", "work": "Work Dashboard"}
# Everything a TUI may have switched on, so a killed child never leaves the popup in its mode.
RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[?1l\x1b>"
         "\x1b[<9u\x1b[0m\x1b[r\x1b[?1049l\x1b[?25h")
CLEAR = "\x1b[2J\x1b[H"
ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>78]")


def strip_ansi(text):
    return ANSI.sub("", text).replace("\r", "")


def excerpt(raw, head=2, tail=4):
    """Printable output with escape sequences removed: the first lines (usage errors lead) and the last (crashes trail)."""
    text = strip_ansi(raw.decode("utf-8", "replace"))
    lines = [l.rstrip() for l in text.split("\n") if l.strip() and l.isprintable()]
    if len(lines) <= head + tail:
        return lines
    return lines[:head] + ["..."] + lines[-tail:]


def render(title, lines, hints, rows, cols):
    """A centred bordered box; hints are (key, description) pairs."""
    inner = max(min(cols - 6, 76), 10)
    body = []
    for line in lines:
        body += textwrap.wrap(line, inner, replace_whitespace=False, drop_whitespace=False) or [""]
    if hints:
        width = max(len(k) for k, _ in hints)
        body += [""] + [f"{k.ljust(width)}  {d}" for k, d in hints]
    inner = min(max([len(title) + 2] + [len(l) for l in body]), max(cols - 4, 10))
    box = ["┌─ \x1b[1m" + title + "\x1b[0m " + "─" * max(inner - len(title) - 1, 0) + "┐"]
    box += ["│ " + l[:inner].ljust(inner) + " │" for l in body]
    box.append("└" + "─" * (inner + 2) + "┘")
    top = max((rows - len(box)) // 2, 0)
    left = max((cols - (inner + 4)) // 2, 0)
    out = [CLEAR]
    out += [f"\x1b[{top + i + 1};{left + 1}H{row}" for i, row in enumerate(box)]
    out.append("\x1b[?25l")
    return "".join(out)
