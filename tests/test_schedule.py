"""--schedule / --unschedule: parsing, the launcher script, and each OS scheduler.
Every system command is faked: no test touches the real Task Scheduler, launchd
or crontab."""
import os
import plistlib
import subprocess

import pytest

import newsdigest.scheduling as sch


# --- when ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text,days,hm", [
    ("07:00", sch.DAYS, (7, 0)),
    ("7:30", sch.DAYS, (7, 30)),
    ("weekdays 07:00", ["mon", "tue", "wed", "thu", "fri"], (7, 0)),
    ("Mon,Thu 18:30", ["mon", "thu"], (18, 30)),
    ("friday at 6.15", ["fri"], (6, 15)),
    ("sun mon 08:00", ["mon", "sun"], (8, 0)),
    ("daily 23:59", sch.DAYS, (23, 59)),
])
def test_parse_when(text, days, hm):
    assert sch.parse_when(text) == (days, *hm)


@pytest.mark.parametrize("text,message", [
    ("", "give a time"), ("25:00", "isn't a time"), ("7am", "isn't a time"),
    ("someday 07:00", "isn't a day"),
])
def test_parse_when_explains_mistakes(text, message):
    with pytest.raises(sch.ScheduleError, match=message):
        sch.parse_when(text)


def test_describe():
    assert sch.describe(sch.DAYS, 7, 0) == "every day at 07:00"
    assert sch.describe(sch.DAYS[:5], 7, 5) == "weekdays at 07:05"
    assert sch.describe(["mon", "thu"], 18, 30) == "Mon, Thu at 18:30"


def test_scheduled_run_keeps_other_flags_with_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = sch.run_args(["--schedule", "07:00", "--tier", "1", "--watchlist", "list.csv",
                         "--config=my.yaml", "--email-to", "a@x.com"])
    assert args == ["--tier", "1", "--watchlist", str(tmp_path / "list.csv"),
                    f"--config={tmp_path / 'my.yaml'}", "--email-to", "a@x.com", "--headless"]
    assert sch.run_args(["--schedule=07:00", "--headless"]) == ["--headless"]


def test_each_folder_gets_its_own_schedule(tmp_path):
    a, b = tmp_path / "client-a", tmp_path / "client-b"
    assert sch.task_name(str(a)) != sch.task_name(str(b))
    assert sch.task_name(str(a)) == sch.task_name(str(a)) and "client-a" in sch.task_name(str(a))


# --- launcher scripts ---------------------------------------------------------------------

def test_windows_launcher(tmp_path):
    home = str(tmp_path / "My Client")
    text = sch.launcher_text(home, ["--tier", "1", "--headless"], "windows", python=r"C:\Py 3\python.exe")
    assert text.startswith("@echo off\r\n") and "\r\n" in text
    assert f'cd /d "{home}"' in text and f'set "NEWS_DIGEST_HOME={home}"' in text
    assert 'set "PYTHONIOENCODING=utf-8"' in text          # redirected output can't crash on "→"
    assert '"C:\\Py 3\\python.exe" -m newsdigest --tier 1 --headless > "logs\\last-scheduled-run.txt" 2>&1' in text


def test_unix_launcher_quotes_paths(tmp_path):
    home = str(tmp_path / "My Client")
    text = sch.launcher_text(home, ["--watchlist", "/data/my list.csv", "--headless"], "linux",
                             python="/usr/bin/python3")
    assert text.startswith("#!/bin/sh\n")
    assert f"cd '{home}' || exit 1" in text
    assert "exec /usr/bin/python3 -m newsdigest --watchlist '/data/my list.csv' --headless" in text


# --- Windows: Task Scheduler --------------------------------------------------------------

class FakeRun:
    """Records scheduler commands; `answers` maps the first word(s) to a result."""

    def __init__(self, answers=None):
        self.calls, self.answers, self.files = [], answers or {}, {}

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if "/XML" in args:                                   # read the task file before it's deleted
            with open(args[args.index("/XML") + 1], encoding="utf-16") as f:
                self.files["xml"] = f.read()
        for key, (code, out) in self.answers.items():
            if " ".join(args).startswith(key):
                return subprocess.CompletedProcess(args, code, out, "" if code == 0 else out)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def on(monkeypatch, tmp_path):
    """Pretend to be on a given OS with a fake scheduler; returns (home, fake)."""
    def use(system, answers=None):
        fake = FakeRun(answers)
        monkeypatch.setattr(sch, "platform_name", lambda: system)
        monkeypatch.setattr(sch, "_run", fake)
        home = tmp_path / "Acme & Co digest"
        home.mkdir()
        return str(home), fake
    return use


def test_windows_install_registers_a_catch_up_task(on):
    home, fake = on("windows")
    msg = sch.install("weekdays 07:00", ["--schedule", "weekdays 07:00", "--tier", "1"], home)
    create = fake.calls[0]
    assert create[:4] == ["schtasks", "/Create", "/TN", sch.task_name(home)] and create[-1] == "/F"
    xml = fake.files["xml"]
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml           # runs after sleep
    assert "<Monday />" in xml and "<Friday />" in xml and "<Saturday />" not in xml
    assert "T07:00:00</StartBoundary>" in xml
    assert "Acme &amp; Co digest" in xml and "<Command>" in xml          # path escaped for XML
    launcher = os.path.join(home, "run-digest.cmd")
    assert os.path.exists(launcher) and "--tier 1 --headless" in open(launcher, encoding="utf-8").read()
    assert "Task Scheduler" in msg and "weekdays at 07:00" in msg and "--unschedule" in msg


def test_windows_daily_uses_a_day_trigger(on):
    home, fake = on("windows")
    sch.install("07:00", [], home)
    assert "<DaysInterval>1</DaysInterval>" in fake.files["xml"]


def test_windows_refusal_is_explained(on):
    home, _ = on("windows", {"schtasks /Create": (1, "ERROR: Access is denied.")})
    with pytest.raises(sch.ScheduleError, match="Access is denied"):
        sch.install("07:00", [], home)


def test_windows_status_and_remove(on):
    query = ("Folder: \\\nHostName: PC\nTaskName: \\News digest x\n"
             "Next Run Time:   25/11/2026 07:00:00\nStatus: Ready\nLast Result: 0\n")
    home, fake = on("windows", {"schtasks /Query": (0, query)})
    assert sch.status(home) == ("OK", "next run 25/11/2026 07:00:00, last result 0")
    open(os.path.join(home, "run-digest.cmd"), "w").close()
    assert "Removed the Task Scheduler schedule" in sch.remove(home)
    assert fake.calls[-1][:2] == ["schtasks", "/Delete"]
    assert not os.path.exists(os.path.join(home, "run-digest.cmd"))


def test_nothing_scheduled(on):
    home, _ = on("windows", {"schtasks": (1, "ERROR: The system cannot find the file specified.")})
    status, detail = sch.status(home)
    assert status == "--" and "--schedule 07:00" in detail
    assert "No schedule was set up" in sch.remove(home)


# --- Linux: cron --------------------------------------------------------------------------

class FakeCrontab(FakeRun):
    def __init__(self, existing):
        super().__init__()
        self.table = existing

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if args == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(self.table) + "\n", "")
        if args == ["crontab", "-"]:
            self.table = kw["input"].splitlines()
        return subprocess.CompletedProcess(args, 0, "", "")


def test_linux_cron_line_is_replaced_not_duplicated_and_others_kept(on, monkeypatch):
    home, _ = on("linux")
    cron = FakeCrontab(["0 3 * * * /usr/bin/backup"])
    monkeypatch.setattr(sch, "_run", cron)
    sch.install("mon,thu 07:30", [], home)
    sch.install("weekdays 06:45", [], home)
    ours = [ln for ln in cron.table if "newsdigest" in ln]
    assert cron.table[0] == "0 3 * * * /usr/bin/backup" and len(ours) == 1
    assert ours[0].startswith("45 6 * * 1,2,3,4,5 ") and "run-digest.sh" in ours[0]
    assert os.access(os.path.join(home, "run-digest.sh"), os.X_OK) or os.name == "nt"
    assert sch.status(home) == ("OK", "weekdays at 06:45")
    sch.remove(home)
    assert cron.table == ["0 3 * * * /usr/bin/backup"]


def test_linux_sunday_is_cron_day_zero(on, monkeypatch):
    home, _ = on("linux")
    cron = FakeCrontab([])
    monkeypatch.setattr(sch, "_run", cron)
    sch.install("sun 09:00", [], home)
    assert cron.table[0].startswith("0 9 * * 0 ")
    assert sch.status(home) == ("OK", "Sun at 09:00")


# --- macOS: launchd -----------------------------------------------------------------------

def test_mac_launch_agent(on, monkeypatch, tmp_path):
    home, fake = on("mac")
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(sch, "_mac_plist_path", lambda h: str(plist))
    sch.install("weekdays 07:00", [], home)
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [os.path.join(home, "run-digest.sh")]
    assert [e["Weekday"] for e in data["StartCalendarInterval"]] == [1, 2, 3, 4, 5]
    assert ["launchctl", "load", "-w", str(plist)] in fake.calls
    assert sch.status(home) == ("OK", "weekdays at 07:00")
    assert "Removed the launchd schedule" in sch.remove(home) and not plist.exists()


# --- command line -------------------------------------------------------------------------

def test_cli_schedule_checks_the_watchlist_first(nd, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sch, "install", lambda when, argv: calls.append((when, argv)) or "Scheduled.")
    missing = str(tmp_path / "nope.csv")
    assert nd.main(["--schedule", "07:00", "--watchlist", missing]) == 1
    assert "Not scheduled: watchlist not found" in capsys.readouterr().out and calls == []

    good = tmp_path / "w.csv"
    good.write_text("Company,Tier\nAcme Widgets Plc,1\n", encoding="utf-8")
    assert nd.main(["--schedule", "25:00", "--watchlist", str(good)]) == 1
    assert "isn't a time" in capsys.readouterr().out

    argv = ["--schedule", "weekdays 07:00", "--watchlist", str(good), "--tier", "1"]
    assert nd.main(argv) == 0 and calls == [("weekdays 07:00", argv)]


def test_cli_unschedule_and_doctor_line(nd, cfg, monkeypatch, capsys):
    monkeypatch.setattr(sch, "remove", lambda: "Removed it.")
    assert nd.main(["--unschedule"]) == 0 and "Removed it." in capsys.readouterr().out
    assert nd.parse_args(["--schedule", "07:00"]).schedule == "07:00"
    assert nd.parse_args([]).schedule is None


def test_source_checkout_runs_are_told_where_the_package_is(tmp_path):
    """Run from a checkout (not pip-installed), a scheduled run in another folder
    couldn't import newsdigest without this."""
    home = str(tmp_path / "client")
    win = sch.launcher_text(home, [], "windows", python="python", checkout=r"D:\src\digest")
    assert r'set "PYTHONPATH=D:\src\digest;%PYTHONPATH%"' in win
    unix = sch.launcher_text(home, [], "linux", python="python3", checkout="/src/my digest")
    assert "export PYTHONPATH='/src/my digest'\"${PYTHONPATH:+:$PYTHONPATH}\"" in unix
    assert "PYTHONPATH" not in sch.launcher_text(home, [], "windows", python="python", checkout=None)
    assert sch._source_checkout() == str(sch.os.path.dirname(sch.os.path.dirname(sch.__file__)))
