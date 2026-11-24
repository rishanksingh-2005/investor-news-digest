"""Run the digest automatically: `newsdigest --schedule 07:00` sets up the
operating system's own scheduler for this folder, `--unschedule` removes it.

  Windows  Task Scheduler (catches up after sleep: runs when the PC wakes)
  macOS    launchd (a LaunchAgent in ~/Library/LaunchAgents)
  Linux    cron (one line in your crontab)

Each folder gets its own schedule, so one folder per client works. The scheduled
run starts a small launcher script in the folder (run-digest.cmd / run-digest.sh)
that runs the digest there with the same settings (config.yaml, .env, flags) and
saves its output to logs/last-scheduled-run.txt.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from xml.sax.saxutils import escape

from . import settings

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ALIASES = {"daily": DAYS, "everyday": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}
OUTPUT_LOG = os.path.join("logs", "last-scheduled-run.txt")


class ScheduleError(ValueError):
    pass


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a scheduler command (replaced in tests)."""
    return subprocess.run(args, capture_output=True, text=True, **kw)


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    return "mac" if sys.platform == "darwin" else "linux"


# --- what and when ------------------------------------------------------------------------

def parse_when(text: str) -> tuple[list[str], int, int]:
    """'07:00' -> every day; 'weekdays 07:00', 'mon 7:30', 'mon,thu 18:00' -> those days.
    Returns (days, hour, minute)."""
    parts = (text or "").strip().lower().replace("at ", " ").split()
    if not parts:
        raise ScheduleError("give a time, e.g. --schedule 07:00 or --schedule \"weekdays 07:00\"")
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", parts[-1])
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError(f"'{parts[-1]}' isn't a time: use 24-hour HH:MM, e.g. 07:00 or 18:30")
    hour, minute = int(m.group(1)), int(m.group(2))
    if len(parts) == 1:
        return list(DAYS), hour, minute
    days: list[str] = []
    for token in re.split(r"[,\s]+", " ".join(parts[:-1])):
        if not token:
            continue
        if token in _DAY_ALIASES:
            days += _DAY_ALIASES[token]
            continue
        day = next((d for d in DAYS if token.startswith(d)), None)
        if not day:
            raise ScheduleError(f"'{token}' isn't a day: use mon..sun, weekdays, weekends or daily")
        days.append(day)
    return [d for d in DAYS if d in days], hour, minute


def describe(days: list[str], hour: int, minute: int) -> str:
    when = f"{hour:02d}:{minute:02d}"
    if days == DAYS:
        return f"every day at {when}"
    if days == DAYS[:5]:
        return f"weekdays at {when}"
    return f"{', '.join(d.capitalize() for d in days)} at {when}"


def run_args(argv: list[str]) -> list[str]:
    """The flags the scheduled run should use: everything given with --schedule except
    the scheduling flags themselves, with file paths made absolute, plus --headless."""
    out: list[str] = []
    skip_next = False
    path_flags = {"--watchlist", "-w", "--config"}
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in ("--schedule", "--unschedule"):
            skip_next = arg == "--schedule"
            continue
        if arg.startswith("--schedule="):
            continue
        if arg in path_flags and i + 1 < len(argv):
            out += [arg, os.path.abspath(argv[i + 1])]
            skip_next = True
            continue
        flag, eq, value = arg.partition("=")
        if eq and flag in path_flags:
            out.append(f"{flag}={os.path.abspath(value)}")
            continue
        out.append(arg)
    if "--headless" not in out:
        out.append("--headless")
    return out


def task_name(home: str) -> str:
    """One schedule per folder: the name carries a short fingerprint of the folder path."""
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(home)).encode("utf-8")).hexdigest()[:8]
    return f"News digest {os.path.basename(os.path.abspath(home)) or 'home'} {digest}"


def _slug(home: str) -> str:
    return "com.newsdigest." + task_name(home).split()[-1]


# --- the launcher script --------------------------------------------------------------------

def launcher_path(home: str, system: str | None = None) -> str:
    system = system or platform_name()
    return os.path.join(home, "run-digest.cmd" if system == "windows" else "run-digest.sh")


def _source_checkout() -> str | None:
    """The checkout folder when running from source (not pip-installed): the
    scheduled run must be told where the package is, since it starts elsewhere."""
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return parent if os.path.exists(os.path.join(parent, "news digest.py")) else None


def launcher_text(home: str, args: list[str], system: str | None = None, python: str | None = None,
                  checkout: str | None = "auto") -> str:
    system = system or platform_name()
    python = python or sys.executable
    checkout = _source_checkout() if checkout == "auto" else checkout
    if system == "windows":
        cmd = subprocess.list2cmdline([python, "-m", "newsdigest", *args])
        return ("@echo off\r\n"
                "rem Created by newsdigest --schedule: runs the news digest for this folder.\r\n"
                "rem Remove the schedule with: newsdigest --unschedule\r\n"
                "chcp 65001 >nul\r\n"
                f'cd /d "{home}"\r\n'
                f'set "NEWS_DIGEST_HOME={home}"\r\n'
                'set "PYTHONIOENCODING=utf-8"\r\n'
                + (f'set "PYTHONPATH={checkout};%PYTHONPATH%"\r\n' if checkout else "")
                + 'if not exist logs mkdir logs\r\n'
                f'{cmd} > "{OUTPUT_LOG}" 2>&1\r\n')
    cmd = shlex.join([python, "-m", "newsdigest", *args])
    return ("#!/bin/sh\n"
            "# Created by newsdigest --schedule: runs the news digest for this folder.\n"
            "# Remove the schedule with: newsdigest --unschedule\n"
            f"cd {shlex.quote(home)} || exit 1\n"
            f"export NEWS_DIGEST_HOME={shlex.quote(home)}\n"
            "export PYTHONIOENCODING=utf-8\n"
            + (f'export PYTHONPATH={shlex.quote(checkout)}"${{PYTHONPATH:+:$PYTHONPATH}}"\n' if checkout else "")
            + "mkdir -p logs\n"
            f"exec {cmd} > {shlex.quote(OUTPUT_LOG)} 2>&1\n")


def write_launcher(home: str, args: list[str], system: str | None = None) -> str:
    system = system or platform_name()
    path = launcher_path(home, system)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(launcher_text(home, args, system))
    if system != "windows":
        os.chmod(path, 0o755)
    return path


# --- Windows: Task Scheduler ----------------------------------------------------------------

_WIN_DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def windows_task_xml(home: str, launcher: str, days: list[str], hour: int, minute: int) -> str:
    """A Task Scheduler definition. XML rather than schtasks flags because only XML
    can set StartWhenAvailable (run a missed time once the PC is back on)."""
    start = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
    if days == DAYS:
        schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    else:
        schedule = ("<ScheduleByWeek><DaysOfWeek>" + "".join(f"<{_WIN_DAYS[d]} />" for d in days)
                    + "</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(f"News digest for {home} ({describe(days, hour, minute)}). Remove with: newsdigest --unschedule")}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      {schedule}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(launcher)}</Command>
      <WorkingDirectory>{escape(home)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _windows_install(home, launcher, days, hour, minute):
    xml = windows_task_xml(home, launcher, days, hour, minute)
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        r = _run(["schtasks", "/Create", "/TN", task_name(home), "/XML", path, "/F"])
    finally:
        os.remove(path)
    if r.returncode != 0:
        raise ScheduleError(f"Task Scheduler refused the task: {(r.stderr or r.stdout).strip()}")


def _windows_remove(home) -> bool:
    return _run(["schtasks", "/Delete", "/TN", task_name(home), "/F"]).returncode == 0


def _windows_status(home) -> str | None:
    r = _run(["schtasks", "/Query", "/TN", task_name(home), "/FO", "LIST", "/V"])
    if r.returncode != 0:
        return None
    info = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info.setdefault(key.strip().lower(), value.strip())
    bits = []
    if info.get("next run time"):
        bits.append(f"next run {info['next run time']}")
    if info.get("last result") not in (None, "", "267011"):     # 267011 = has not run yet
        bits.append(f"last result {info['last result']}")
    return ", ".join(bits) or "scheduled"


# --- macOS: launchd -----------------------------------------------------------------------

_MAC_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _mac_plist_path(home) -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{_slug(home)}.plist")


def mac_plist(home, launcher, days, hour, minute) -> bytes:
    if days == DAYS:
        when = {"Hour": hour, "Minute": minute}
    else:
        when = [{"Weekday": _MAC_DAYS[d], "Hour": hour, "Minute": minute} for d in days]
    return plistlib.dumps({
        "Label": _slug(home),
        "ProgramArguments": [launcher],
        "WorkingDirectory": home,
        "StartCalendarInterval": when,
    })


def _mac_install(home, launcher, days, hour, minute):
    path = _mac_plist_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _run(["launchctl", "unload", path])
    with open(path, "wb") as f:
        f.write(mac_plist(home, launcher, days, hour, minute))
    r = _run(["launchctl", "load", "-w", path])
    if r.returncode != 0:
        raise ScheduleError(f"launchd refused the job: {(r.stderr or r.stdout).strip()}")


def _mac_remove(home) -> bool:
    path = _mac_plist_path(home)
    if not os.path.exists(path):
        return False
    _run(["launchctl", "unload", path])
    os.remove(path)
    return True


def _mac_status(home) -> str | None:
    path = _mac_plist_path(home)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        when = plistlib.load(f).get("StartCalendarInterval")
    entries = when if isinstance(when, list) else [when]
    days = [d for d, n in _MAC_DAYS.items() if any(e.get("Weekday") == n for e in entries)] or DAYS
    return describe([d for d in DAYS if d in days], entries[0]["Hour"], entries[0]["Minute"])


# --- Linux: cron --------------------------------------------------------------------------

def _cron_marker(home) -> str:
    return f"# newsdigest {task_name(home).split()[-1]}"


def cron_line(home, launcher, days, hour, minute) -> str:
    dow = "*" if days == DAYS else ",".join(str((DAYS.index(d) + 1) % 7) for d in days)
    return f"{minute} {hour} * * {dow} {shlex.quote(launcher)} {_cron_marker(home)}"


def _crontab() -> list[str]:
    r = _run(["crontab", "-l"])
    lines = r.stdout.splitlines() if r.returncode == 0 else []    # "no crontab" -> empty
    while lines and not lines[-1].strip():                          # no blank lines piling up
        lines.pop()
    return lines


def _write_crontab(lines: list[str]):
    r = _run(["crontab", "-"], input="\n".join(lines) + "\n")
    if r.returncode != 0:
        raise ScheduleError(f"couldn't update your crontab: {(r.stderr or r.stdout).strip()}")


def _linux_install(home, launcher, days, hour, minute):
    lines = [ln for ln in _crontab() if not ln.endswith(_cron_marker(home))]
    _write_crontab(lines + [cron_line(home, launcher, days, hour, minute)])


def _linux_remove(home) -> bool:
    lines = _crontab()
    kept = [ln for ln in lines if not ln.endswith(_cron_marker(home))]
    if len(kept) == len(lines):
        return False
    _write_crontab(kept)
    return True


def _linux_status(home) -> str | None:
    line = next((ln for ln in _crontab() if ln.endswith(_cron_marker(home))), None)
    if not line:
        return None
    minute, hour, _, _, dow = line.split()[:5]
    days = DAYS if dow == "*" else [DAYS[(int(n) - 1) % 7] for n in dow.split(",")]
    return describe([d for d in DAYS if d in days], int(hour), int(minute))


_BACKENDS = {
    "windows": (_windows_install, _windows_remove, _windows_status, "Task Scheduler"),
    "mac": (_mac_install, _mac_remove, _mac_status, "launchd"),
    "linux": (_linux_install, _linux_remove, _linux_status, "cron"),
}


# --- the three commands -------------------------------------------------------------------

def install(when: str, argv: list[str], home: str | None = None) -> str:
    """--schedule: (re)create this folder's schedule. Returns a message for the user."""
    home = os.path.abspath(home or settings.HERE)
    days, hour, minute = parse_when(when)
    system = platform_name()
    install_fn, _, _, scheduler = _BACKENDS[system]
    launcher = write_launcher(home, run_args(argv), system)
    install_fn(home, launcher, days, hour, minute)
    cmd = settings.command()
    return (f"Scheduled with {scheduler}: {describe(days, hour, minute)}.\n"
            f"  Runs:    {launcher}\n"
            f"  Folder:  {home} (its config.yaml, .env and watchlist are used)\n"
            f"  Output:  {os.path.join(home, OUTPUT_LOG)} (each run's full log is in logs/ too)\n"
            f"The computer must be on (or, on Windows, it runs as soon as it wakes).\n"
            f"Check it with: {cmd} --doctor   Remove it with: {cmd} --unschedule")


def remove(home: str | None = None) -> str:
    home = os.path.abspath(home or settings.HERE)
    system = platform_name()
    _, remove_fn, _, scheduler = _BACKENDS[system]
    removed = remove_fn(home)
    launcher = launcher_path(home, system)
    if os.path.exists(launcher):
        os.remove(launcher)
    return (f"Removed the {scheduler} schedule for {home}." if removed
            else f"No schedule was set up for {home}.")


def status(home: str | None = None) -> tuple[str, str]:
    """For --doctor: ("OK", "next run ...") or ("--", "not scheduled ...")."""
    home = os.path.abspath(home or settings.HERE)
    try:
        found = _BACKENDS[platform_name()][2](home)
    except (OSError, ValueError, IndexError, KeyError):
        found = None
    if found:
        return "OK", found
    return "--", f"not scheduled (run it automatically: {settings.command()} --schedule 07:00)"
