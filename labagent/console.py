"""What a person sees in a terminal: colour, step progress with a live timer, and a result card.

Colour and layout are for people only. They switch on when the stream is a terminal, and never when `NO_COLOR` is set
(https://no-color.org), when the stream is a pipe or a file, or when TERM=dumb. `FORCE_COLOR` or `--color always` forces them on,
`--color never` off. With them off, every command prints exactly what it always did, so logs, CI and scripts do not change.

Nothing here decides what is printed, only how: packs still write plain text through `ctx.log`.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CODES = {"bold": "1", "dim": "2", "underline": "4", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
          "gray": "90"}
_UTF = {"ok": "✔", "fail": "✖", "warn": "▲", "skip": "↷", "run": "▶", "plan": "○", "bar": "┃",
        "top": "┏", "bottom": "┗", "rule": "━", "dot": "·", "times": "×", "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"}
_ASCII = {"ok": "ok", "fail": "x", "warn": "!", "skip": "-", "run": ">", "plan": "o", "bar": "|", "top": "+", "bottom": "+", "rule": "-",
          "dot": "-", "times": "x", "spin": "|/-\\"}
_URL = re.compile(r"https?://[^\s)]+")
_PRICE = re.compile(r"\$\d+(?:\.\d+)?/h")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def color_enabled(stream: Any, mode: str = "auto", env: Optional[Dict[str, str]] = None) -> bool:
    """mode: auto | always | never. In auto, a terminal gets colour unless NO_COLOR is set; FORCE_COLOR turns it on anywhere."""
    env = os.environ if env is None else env
    if mode == "never":
        return False
    if mode == "always":
        return True
    if env.get("NO_COLOR"):
        return False
    if env.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    if env.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


class Style:
    """style("text", "bold", "green"). With colour off it returns the text unchanged."""

    def __init__(self, on: bool):
        self.on = on

    def __call__(self, text: str, *names: str) -> str:
        if not self.on or not names:
            return text
        return "\x1b[" + ";".join(_CODES[n] for n in names) + "m" + text + "\x1b[0m"


def elapsed_text(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


class Console:
    """Pretty output on one stream (stderr for progress, stdout for tables). Safe to call from several threads."""

    def __init__(self, stream: Any = None, color: str = "auto", env: Optional[Dict[str, str]] = None, tick: float = 0.12,
                 clock=time.monotonic, width: Optional[int] = None):
        self.out = stream if stream is not None else sys.stderr
        self.color = color_enabled(self.out, color, env)
        self.s = Style(self.color)
        enc = (getattr(self.out, "encoding", None) or "").lower()
        self.sym = _UTF if "utf" in enc else _ASCII
        self.live = self.color and bool(getattr(self.out, "isatty", lambda: False)())     # a timer that redraws needs a real terminal
        self.tick, self.clock = tick, clock
        self.width = width or min(shutil.get_terminal_size((100, 24)).columns, 100)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0
        self._drawn = False
        self._frame = 0
        self._later: List[Any] = []

    # ---- low level ----
    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def _clear(self) -> None:
        if self._drawn:
            self._write("\r\x1b[2K")
            self._drawn = False

    def line(self, text: str = "") -> None:
        with self._lock:
            self._clear()
            self._write(text + "\n")

    def _draw(self) -> None:
        frame = self.sym["spin"][self._frame % len(self.sym["spin"])]
        self._frame += 1
        self._write(f"\r\x1b[2K      {self.s(frame, 'cyan')} {self.s(elapsed_text(self.clock() - self._t0), 'gray')}")
        self._drawn = True

    def _spin(self) -> None:
        while not self._stop.wait(self.tick):
            with self._lock:
                self._draw()

    def _start_spin(self) -> None:
        if not self.live:
            return
        self._t0, self._frame = self.clock(), 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _stop_spin(self) -> None:
        thread, self._thread = self._thread, None
        if thread:
            self._stop.set()
            thread.join(timeout=2)
        with self._lock:
            self._clear()

    def later(self, fn) -> None:
        """Print something once the current step has shown its tick, so a result card never sits above its own checkmark."""
        self._later.append(fn)

    def _flush_later(self) -> None:
        pending, self._later = self._later, []
        for fn in pending:
            fn()

    def close(self) -> None:
        """Always call this when done, so a timer never survives an error or Ctrl-C."""
        self._stop_spin()
        self._flush_later()

    def _fit(self, text: str, room: int) -> str:
        """Cut a title to the room there is, so a long one never wraps under the step line."""
        if len(text) <= room:
            return text
        return text[:max(room - 1, 1)] + ("\u2026" if self.sym is _UTF else "")

    def highlight(self, text: str) -> str:
        if not self.color:
            return text
        text = _URL.sub(lambda m: self.s(m.group(0), "cyan", "underline"), text)
        return _PRICE.sub(lambda m: self.s(m.group(0), "green", "bold"), text)

    # ---- what packs print ----
    def log(self, msg: str) -> None:
        """A line a pack wrote with ctx.log: warnings in yellow, details indented, links and prices picked out."""
        s = self.s
        if not msg.strip():
            self.line("")
        elif msg.startswith("  warning: "):
            self.line("      " + s(self.sym["warn"], "yellow", "bold") + " " + s(msg[len("  warning: "):], "yellow"))
        elif msg.startswith("  "):
            self.line("      " + self.highlight(msg.strip()))
        else:
            self.line(self.highlight(msg))

    def banner(self, pack_id: str, action: str, name: str, dry_run: bool = False) -> None:
        s = self.s
        tag = "  " + s("DRY RUN", "yellow", "bold") if dry_run else ""
        self.line()
        self.line(f" {s('labagent', 'bold', 'cyan')} {s(self.sym['dot'], 'gray')} {s(pack_id + ' ' + action, 'bold')} {s(self.sym['dot'], 'gray')} {name}{tag}")
        self.line(" " + s(self.sym["rule"] * (self.width - 2), "gray"))

    def _head(self, mark: str, n: int, total: int, step_id: str, title: str, tone: str) -> str:
        s = self.s
        left = f" {s(mark, tone, 'bold')} {s(f'[{n}/{total}]', 'gray')} {s(step_id, 'bold')}  "
        return left + s(self._fit(title, max(self.width - len(strip_ansi(left)), 20)), "dim")

    def step_planned(self, n: int, total: int, step_id: str, title: str) -> None:
        self.line(self._head(self.sym["plan"], n, total, step_id, title, "gray"))

    def step_skipped(self, n: int, total: int, step_id: str, title: str) -> None:
        self.line(self._head(self.sym["skip"], n, total, step_id, title, "gray") + self.s("  (already done)", "gray"))

    def step_start(self, n: int, total: int, step_id: str, title: str) -> None:
        self._stop_spin()
        self.line(self._head(self.sym["run"], n, total, step_id, title, "cyan"))
        self._t0 = self.clock()
        self._start_spin()

    def step_done(self, seconds: float) -> None:
        self._stop_spin()
        self.line("      " + self.s(self.sym["ok"], "green", "bold") + " " + self.s(elapsed_text(seconds), "gray"))
        self._flush_later()

    def step_failed(self, seconds: float) -> None:
        self._stop_spin()
        self.line("      " + self.s(self.sym["fail"], "red", "bold") + " " + self.s("failed after " + elapsed_text(seconds), "red"))
        self._flush_later()

    # ---- a result ----
    def kv(self, label: str, value: str, width: int = 11) -> str:
        return self.s(label.ljust(width), "gray") + value

    def card(self, title: str, lines: Sequence[str], tone: str = "green") -> None:
        """A titled block with a coloured bar down the left side. No right border: a long link may wrap and must not break it."""
        s, sym = self.s, self.sym
        bar = s(sym["bar"], tone)
        self.line()
        self.line(s(sym["top"] + sym["rule"], tone) + " " + s(title, tone, "bold") + " " + s(sym["rule"] * max(self.width - len(title) - 5, 3), tone))
        for text in lines:
            self.line(f"{bar}  {text}" if text else bar)
        self.line(s(sym["bottom"] + sym["rule"] * (self.width - 1), tone))
