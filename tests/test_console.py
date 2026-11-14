"""Colour and live progress: on for a person at a terminal, off everywhere else, and never a change in what is said."""
import io
import json
import sys
import threading
import time

import pytest

from labagent import cli
from labagent.console import Console, Style, color_enabled, elapsed_text, strip_ansi
from labagent.packs import registry
from labagent.packs.base import load_state, run_pack
from labagent.packs.gpu_notebooks import offers as offers_cli
from labagent.packs.gpu_notebooks import providers
from labagent.packs.gpu_notebooks.pack import GpuNotebooks
from labagent.packs.gpu_notebooks.providers import ShadeformProvider
from tests import test_gpu_notebooks as gpu
from tests import test_gpu_offers as offers


class Stream(io.StringIO):
    def __init__(self, tty=False, encoding="utf-8"):
        super().__init__()
        self._tty, self._enc = tty, encoding

    @property
    def encoding(self):
        return self._enc

    def isatty(self):
        return self._tty


# ---- when colour is on ----

@pytest.mark.parametrize("tty,env,mode,expected", [
    (True, {}, "auto", True),
    (False, {}, "auto", False),                              # a pipe or a file
    (True, {"NO_COLOR": "1"}, "auto", False),
    (True, {"NO_COLOR": ""}, "auto", True),                  # an empty NO_COLOR does not count (no-color.org)
    (True, {"TERM": "dumb"}, "auto", False),
    (False, {"FORCE_COLOR": "1"}, "auto", True),
    (False, {"FORCE_COLOR": "0"}, "auto", False),
    (True, {"NO_COLOR": "1"}, "always", True),               # the flag beats the environment
    (False, {"FORCE_COLOR": "1"}, "never", False),
])
def test_when_colour_is_on(tty, env, mode, expected):
    assert color_enabled(Stream(tty), mode, env) is expected


def test_style_is_a_no_op_when_off_and_closes_its_codes_when_on():
    assert Style(False)("x", "bold", "red") == "x"
    assert Style(True)("x", "bold", "red") == "\x1b[1;31mx\x1b[0m"
    assert Style(True)("x") == "x"


@pytest.mark.parametrize("seconds,text", [(0.04, "0.0s"), (0.8, "0.8s"), (41.9, "41s"), (134, "2m 14s"), (3725, "1h 02m")])
def test_elapsed_text(seconds, text):
    assert elapsed_text(seconds) == text


# ---- the live timer ----

def test_the_timer_redraws_one_line_and_logs_stay_above_it():
    out = Stream(tty=True)
    ui = Console(out, color="always", tick=0.01)
    assert ui.live
    ui.step_start(1, 2, "host", "Create the VM")
    time.sleep(0.15)
    ui.log("  created inst-1")
    time.sleep(0.05)
    ui.step_done(0.2)
    ui.close()
    raw = out.getvalue()
    assert "\r\x1b[2K" in raw                                       # the timer line was drawn and wiped
    text = strip_ansi(raw).replace("\r", "\n")
    assert "created inst-1" in text and text.rstrip().splitlines()[-1].strip().startswith("✔")    # ends on the tick, not a frame
    assert ui._thread is None and not any(t.name.startswith("Thread") and t.is_alive() and t._target == ui._spin for t in threading.enumerate())


def test_close_stops_a_timer_that_was_never_finished():
    """A failure or Ctrl-C between step_start and step_done must not leave a thread drawing over the shell prompt."""
    out = Stream(tty=True)
    ui = Console(out, color="always", tick=0.01)
    ui.step_start(1, 1, "host", "t")
    time.sleep(0.05)
    ui.close()
    n = len(out.getvalue())
    time.sleep(0.1)
    assert len(out.getvalue()) == n and ui._thread is None


def test_no_timer_without_a_real_terminal():
    out = Stream(tty=False)
    ui = Console(out, color="always")
    ui.step_start(1, 1, "host", "t")
    ui.step_done(1)
    assert not ui.live and ui._thread is None and "\r" not in out.getvalue()


def test_ascii_terminals_get_ascii_symbols_only():
    out = Stream(tty=False, encoding="ascii")
    ui = Console(out, color="always")
    ui.banner("gpu-notebooks", "launch", "t")
    ui.step_start(1, 1, "host", "A very long title " * 20)
    ui.step_done(2)
    ui.card("Ready", ["a line"], "green")
    ui.log("  warning: careful")
    assert all(ord(c) < 128 for c in strip_ansi(out.getvalue()))


def test_a_long_title_is_cut_to_the_terminal_width():
    out = Stream()
    ui = Console(out, color="always", width=60)
    ui.step_start(1, 1, "setup", "word " * 50)
    assert all(len(l) <= 62 for l in strip_ansi(out.getvalue()).splitlines())


def test_log_lines_get_their_own_look_but_keep_their_words():
    out = Stream()
    ui = Console(out, color="always")
    ui.log("  warning: shared mode")
    ui.log("  shadeform: H100 at $2.19/h, see https://example.com/a?b=1")
    ui.log("plain line")
    ui.log("")
    text = strip_ansi(out.getvalue())
    assert "shared mode" in text and "H100 at $2.19/h, see https://example.com/a?b=1" in text and "plain line" in text
    assert "\x1b[33" in out.getvalue() and "\x1b[36;4mhttps://example.com/a?b=1" in out.getvalue()      # yellow warning, underlined link


def test_later_runs_after_the_tick_and_on_close():
    out = Stream()
    ui = Console(out, color="always")
    ui.later(lambda: ui.line("CARD"))
    ui.step_start(1, 1, "report", "t")
    ui.step_done(0.1)
    ui.later(lambda: ui.line("LAST"))
    ui.close()
    lines = strip_ansi(out.getvalue()).splitlines()
    assert lines.index("CARD") > max(i for i, l in enumerate(lines) if "✔" in l) - 0 and lines[-1] == "LAST"


# ---- the same facts, plain or styled ----

def launch(ui=None, log=None, **kw):
    logs = []
    ssh = gpu.FakeSsh()
    env = run_pack(GpuNotebooks(), "launch", "t", gpu.launch_params(**kw), services=gpu.svc(gpu.FakeShadeform(), ssh=ssh),
                   log=log or (ui.log if ui else logs.append), ui=ui)
    return env, logs, ssh


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    return tmp_path


def test_the_styled_report_says_everything_the_plain_one_does(home):
    env, plain, ssh = launch()
    assert env.status == "ok"
    out = Stream()
    ui = Console(out, color="always", width=100)
    env2, _, _ = launch(ui=ui)
    ui.close()
    assert env2.status == "ok"
    styled = strip_ansi(out.getvalue())
    st = load_state("gpu-notebooks", "t")
    plain_text = "\n".join(plain)
    for must in ([f"lab-{i:02d}" for i in range(1, 8)] + [st["domain"], st["credentials_file"], st["login_file"], "labagent destroy gpu-notebooks --name t"]):
        assert must in plain_text and must in styled, must
    # every link with its token, and the shell password, appear in both
    import csv
    for r in csv.DictReader(open(st["credentials_file"], encoding="utf-8")):
        assert r["login_url"] in styled
    pw = next(csv.DictReader(open(st["login_file"], encoding="utf-8")))["password"]
    assert pw in styled and pw in plain_text
    for i in range(1, 8):
        assert f"[{i}/7]" in styled


def test_each_step_shows_its_header_then_a_tick_in_order(home):
    out = Stream()
    ui = Console(out, color="always")
    launch(ui=ui)
    ui.close()
    lines = [l for l in strip_ansi(out.getvalue()).splitlines() if "[" in l and "/7]" in l or "✔" in l]
    assert [("hdr" if "/7]" in l else "ok") for l in lines] == ["hdr", "ok"] * 7


def test_a_failed_step_shows_a_cross_and_the_error_card(home, capsys):
    ssh = gpu.FakeSsh(partition=[(1, "", "ERROR: cannot enable MIG on GPU 0\n")])
    out = Stream()
    ui = Console(out, color="always", width=300)          # wide, so the error is not wrapped across two lines
    env = run_pack(GpuNotebooks(), "launch", "t", gpu.launch_params(), services=gpu.svc(gpu.FakeShadeform(), ssh=ssh), log=ui.log, ui=ui)
    cli._finish(ui, env, "gpu-notebooks", "launch", "t", False, shown_json=False)
    ui.close()
    text = strip_ansi(out.getvalue())
    assert env.status == "failed" and "✖ failed after" in text and "cannot enable MIG on GPU 0" in text
    assert "step setup failed" in text and "labagent destroy gpu-notebooks --name t" in text and "Run the same command again to resume" in text


def test_shared_mode_titles_do_not_talk_about_slices(home):
    shared = GpuNotebooks().deploy_steps(type("C", (), {"params": {"mode": "shared"}})())
    mig = GpuNotebooks().deploy_steps(type("C", (), {"params": {"mode": "mig"}})())
    assert not any("slice" in s.title or "MIG slices" in s.title for s in shared)
    assert any("MIG slices" in s.title for s in mig)


# ---- the command line ----

def tty_stdout(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_piped_output_is_exactly_what_it_was(home, capsys):
    assert cli.main(["launch", "gpu-notebooks", "--name", "t", "--dry-run"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["mode"] == "dry-run"
    assert "\x1b" not in cap.err and "[1/7]" not in cap.err                     # stderr is a capture, not a terminal: nothing styled


def test_no_colour_on_a_terminal_still_gives_the_old_output(home, capsys, monkeypatch):
    tty_stdout(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.main(["launch", "gpu-notebooks", "--name", "t", "--dry-run"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["mode"] == "dry-run" and "\x1b" not in cap.err


def test_a_styled_terminal_run_prints_no_json_unless_asked(home, capsys, monkeypatch):
    tty_stdout(monkeypatch)
    assert cli.main(["launch", "gpu-notebooks", "--name", "t", "--dry-run", "--color", "always"]) == 0
    cap = capsys.readouterr()
    assert cap.out == "" and "DRY RUN" in strip_ansi(cap.err) and "[7/7]" in strip_ansi(cap.err) and "add --json" in strip_ansi(cap.err)
    assert cli.main(["launch", "gpu-notebooks", "--name", "t", "--dry-run", "--color", "always", "--json"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["command"] == "launch:gpu-notebooks"


def test_output_file_still_gets_the_envelope(home, capsys, monkeypatch, tmp_path):
    tty_stdout(monkeypatch)
    f = tmp_path / "result.json"
    assert cli.main(["launch", "gpu-notebooks", "--name", "t", "--dry-run", "--color", "always", "--output", str(f)]) == 0
    assert json.loads(f.read_text())["mode"] == "dry-run" and capsys.readouterr().out == ""


def test_a_bad_colour_value_is_refused(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["launch", "gpu-notebooks", "--color", "rainbow"])


# ---- tables ----

@pytest.fixture
def api(monkeypatch):
    fake = offers.Fake()
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=fake))
    return fake


def test_a_coloured_offers_table_is_the_plain_table_plus_colour(api, capsys):
    cli.main(["offers", "--all", "--color", "never"])
    plain = capsys.readouterr().out
    cli.main(["offers", "--all", "--color", "always"])
    colored = capsys.readouterr().out
    assert "\x1b[" in colored and strip_ansi(colored) == plain


def test_a_coloured_key_table_is_the_plain_table_plus_colour(api, capsys):
    cli.main(["ssh-keys", "--color", "never"])
    plain = capsys.readouterr().out
    cli.main(["ssh-keys", "--color", "always"])
    colored = capsys.readouterr().out
    assert "\x1b[" in colored and strip_ansi(colored) == plain


def test_json_output_is_never_coloured(api, capsys):
    cli.main(["offers", "--json", "--color", "always"])
    assert "\x1b" not in capsys.readouterr().out
    cli.main(["ssh-keys", "--json", "--color", "always"])
    assert "\x1b" not in capsys.readouterr().out


def test_a_failure_before_any_step_ran_does_not_name_a_step():
    from labagent.envelope import Envelope
    out = Stream(tty=True)
    con = Console(out, "always", width=90)
    cli._finish(con, Envelope(command="launch").fail("unknown parameter(s) for gpu-notebooks: profile"), "gpu-notebooks", "launch", "demo", False, True)
    text = strip_ansi(out.getvalue())
    assert "launch stopped before any step ran" in text and "step ?" not in text
    assert "unknown parameter(s) for gpu-notebooks: profile" in text and "labagent destroy gpu-notebooks --name demo" in text
