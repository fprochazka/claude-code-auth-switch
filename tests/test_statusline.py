import json
import shlex
import stat
import statistics
import subprocess
import sys
import time

import pytest

from claude_code_auth_switch.statusline import LOCK_FILE, RATE_LIMITS_FILE, merge

TAP = ["cc-auth-switch", "statusline"]
SESSION_A = "0b6f2c1e-1111-4222-8333-444455556666"
SESSION_B = "0b6f2c1e-2222-4222-8333-444455556666"
NOW_MS = 1_800_000_000_000
NOW_S = NOW_MS // 1000
HOUR = 3600


def payload(session_id=SESSION_A, **windows):
    return {
        "session_id": session_id,
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "version": "2.1.325",
        "workspace": {"current_dir": "/home/user/devel/service", "project_dir": "/home/user/devel/service"},
        **({"rate_limits": windows} if windows else {}),
    }


def window(used, resets_in_s):
    return {"used_percentage": used, "resets_at": NOW_S + resets_in_s}


def merged(stored, data, *, now_ms=NOW_MS):
    return merge(stored, data, now_ms=now_ms, profile="work-team")


def test_the_first_payload_writes_every_window_with_its_observation():
    data = merged(None, payload(five_hour=window(23.5, HOUR), seven_day=window(41, 3 * 86400)))

    assert data == {
        "v": 1,
        "profile": "work-team",
        "written_at": NOW_MS,
        "session_id": SESSION_A,
        "model": "claude-opus-5-5",
        "claude_version": "2.1.325",
        "windows": {
            "five_hour": {**window(23.5, HOUR), "observed_at": NOW_MS, "changed_at": NOW_MS, "session_id": SESSION_A},
            "seven_day": {
                **window(41, 3 * 86400),
                "observed_at": NOW_MS,
                "changed_at": NOW_MS,
                "session_id": SESSION_A,
            },
        },
        "sessions": {
            SESSION_A: {
                "five_hour": {**window(23.5, HOUR), "observed_at": NOW_MS},
                "seven_day": {**window(41, 3 * 86400), "observed_at": NOW_MS},
            },
        },
    }


def test_a_payload_without_rate_limits_writes_nothing():
    assert merged(None, payload()) is None
    assert merged(None, {"rate_limits": {"five_hour": {"used_percentage": "12", "resets_at": NOW_S}}}) is None
    assert merged(None, ["not", "an", "object"]) is None


def windows_after(stored, data):
    return (data if data is not None else stored)["windows"]


def test_an_idle_session_cannot_lower_a_value_or_bring_back_an_older_window():
    stored = merged(None, payload(five_hour=window(60, HOUR), seven_day=window(30, 86400)))

    # An idle session still reports its last response: a lower value of the same window, and the week before.
    stale = payload(SESSION_B, five_hour=window(40, HOUR + 20), seven_day=window(90, 86400 - 7 * 86400))
    assert windows_after(stored, merged(stored, stale, now_ms=NOW_MS + 1000)) == stored["windows"]


def test_an_idle_session_repeating_an_old_low_value_cannot_replace_a_value_30_points_higher():
    stored = merged(None, payload(five_hour=window(10, HOUR)))
    stored = merged(stored, payload(SESSION_B, five_hour=window(45, HOUR)), now_ms=NOW_MS + 1000)

    assert merged(stored, payload(five_hour=window(10, HOUR)), now_ms=NOW_MS + 2000) is None


def three_windows(five_hour, seven_day, spend_limit):
    return {
        "five_hour": window(five_hour, HOUR),
        "seven_day": window(seven_day, 86400),
        "spend_limit": window(spend_limit, 86400),
    }


def three_windows_used(used):
    return {"five_hour": used, "seven_day": used, "spend_limit": used}


def test_a_drop_of_30_points_or_more_replaces_the_stored_value_when_the_sessions_own_value_changed():
    stored = merged(None, payload(**three_windows(82, 82, 90)))

    # A reset bought within a window lowers the usage without moving resets_at.
    fresh = payload(five_hour=window(52, HOUR), seven_day=window(9, 86400 + 20), spend_limit=window(45, 86400))
    data = merged(stored, fresh, now_ms=NOW_MS + 1000)

    assert data is not None
    assert data["windows"]["five_hour"] == {
        **window(52, HOUR),
        "observed_at": NOW_MS + 1000,
        "changed_at": NOW_MS + 1000,
        "session_id": SESSION_A,
    }
    assert data["windows"]["seven_day"]["used_percentage"] == 9
    assert data["windows"]["spend_limit"]["used_percentage"] == 45


def test_a_drop_of_less_than_30_points_is_ignored_even_when_the_sessions_own_value_changed():
    stored = merged(None, payload(**three_windows(82, 82, 90)))

    data = merged(stored, payload(**three_windows(52.1, 53, 60.5)), now_ms=NOW_MS + 1000)

    assert windows_after(stored, data) == stored["windows"]
    assert data is not None and data["sessions"][SESSION_A]["five_hour"]["used_percentage"] == 52.1


def test_a_session_new_in_the_window_is_only_recorded_and_replaces_the_value_with_its_next_changed_one():
    stored = merged(None, payload(**three_windows(82, 82, 90)))

    first = merged(stored, payload(SESSION_B, **three_windows(9, 9, 9)), now_ms=NOW_MS + 1000)
    assert first is not None
    assert first["windows"] == stored["windows"]
    assert first["sessions"][SESSION_B]["five_hour"] == {**window(9, HOUR), "observed_at": NOW_MS + 1000}

    assert merged(first, payload(SESSION_B, **three_windows(9, 9, 9)), now_ms=NOW_MS + 2000) is None
    second = merged(first, payload(SESSION_B, **three_windows(9.5, 9.5, 9.5)), now_ms=NOW_MS + 3000)
    assert second is not None
    assert {name: w["used_percentage"] for name, w in second["windows"].items()} == three_windows_used(9.5)
    assert second["windows"]["five_hour"]["session_id"] == SESSION_B


def test_a_sessions_record_expires_when_its_window_reset_or_after_6_hours_without_a_record():
    stored = merged(None, payload(five_hour=window(10, 600), seven_day=window(10, 3 * 86400)))

    after_reset = merged(stored, payload(SESSION_B, seven_day=window(11, 3 * 86400)), now_ms=NOW_MS + 601_000)
    assert after_reset is not None
    assert sorted(after_reset["sessions"][SESSION_A]) == ["seven_day"]

    six_hours_later = NOW_MS + 6 * HOUR * 1000
    unseen = merged(stored, payload(SESSION_B, seven_day=window(11, 3 * 86400)), now_ms=six_hours_later)
    assert unseen is not None
    assert sorted(unseen["sessions"]) == [SESSION_B]


def test_only_the_100_sessions_with_the_newest_records_are_kept():
    stored = merged(None, payload(five_hour=window(10, HOUR)))
    stored["sessions"] = {
        f"session-{i:03}": {"five_hour": {**window(10, HOUR), "observed_at": NOW_MS - i}} for i in range(100)
    }

    data = merged(stored, payload(SESSION_B, five_hour=window(10, HOUR)), now_ms=NOW_MS + 1000)

    assert data is not None
    assert len(data["sessions"]) == 100
    assert SESSION_B in data["sessions"]
    assert "session-099" not in data["sessions"]
    assert "session-098" in data["sessions"]


def test_a_higher_value_and_a_later_window_replace_the_stored_ones():
    stored = merged(None, payload(five_hour=window(90, 600), seven_day=window(30, 86400)))

    later = merged(
        stored,
        payload(SESSION_B, five_hour=window(5, 600 + 5 * HOUR), seven_day=window(31, 86400)),
        now_ms=NOW_MS + 5000,
    )

    assert later is not None
    assert later["windows"]["five_hour"] == {
        **window(5, 600 + 5 * HOUR),
        "observed_at": NOW_MS + 5000,
        "changed_at": NOW_MS + 5000,
        "session_id": SESSION_B,
    }
    assert later["windows"]["seven_day"]["used_percentage"] == 31
    assert later["session_id"] == SESSION_B


def test_a_window_the_payload_dropped_is_kept():
    stored = merged(None, payload(five_hour=window(87, -60), seven_day=window(30, 86400)))

    # Claude Code drops a window once its reset time has passed.
    data = merged(stored, payload(seven_day=window(35, 86400)), now_ms=NOW_MS + 1000)

    assert data is not None
    assert data["windows"]["five_hour"] == stored["windows"]["five_hour"]


def test_unchanged_values_are_written_again_only_after_a_minute():
    stored = merged(None, payload(five_hour=window(10, HOUR)))

    assert merged(stored, payload(five_hour=window(10, HOUR)), now_ms=NOW_MS + 59_000) is None
    refreshed = merged(stored, payload(five_hour=window(10, HOUR)), now_ms=NOW_MS + 60_000)
    assert refreshed is not None
    assert refreshed["windows"]["five_hour"]["observed_at"] == NOW_MS + 60_000
    assert refreshed["windows"]["five_hour"]["changed_at"] == NOW_MS
    assert refreshed["sessions"][SESSION_A]["five_hour"]["observed_at"] == NOW_MS + 60_000


def test_spend_limit_and_unknown_windows_are_kept_as_given():
    spend = {**window(120.5, 10 * 86400), "used_usd": 602.5, "limit_usd": 500, "period": "monthly"}

    data = merged(None, payload(spend_limit=spend, weekly_opus=window(3, 86400), five_hour=window(101, HOUR)))

    assert data is not None
    assert sorted(data["windows"]) == ["spend_limit", "weekly_opus"]
    assert data["windows"]["spend_limit"]["period"] == "monthly"


def test_a_spend_limit_reset_time_that_moved_by_seconds_is_the_same_period():
    stored = merged(None, payload(spend_limit=window(40, 86400)))

    jittered = merged(stored, payload(SESSION_B, spend_limit=window(30, 86400 + 2)), now_ms=NOW_MS + 1000)
    later = merged(stored, payload(SESSION_B, spend_limit=window(1, 86400 + 30 * 86400)), now_ms=NOW_MS + 1000)

    assert windows_after(stored, jittered) == stored["windows"]
    assert later is not None and later["windows"]["spend_limit"]["used_percentage"] == 1


def test_a_stored_file_of_another_version_is_replaced():
    data = merged({"v": 2, "windows": {"five_hour": window(99, HOUR)}}, payload(five_hour=window(10, HOUR)))

    assert data is not None and data["windows"]["five_hour"]["used_percentage"] == 10


@pytest.fixture
def profile_env(sandbox):
    profile_dir = sandbox.mkdir("profiles/work-team")
    env = {**sandbox.env, "CLAUDE_CONFIG_DIR": str(profile_dir), "FP_CC_AUTH_SWITCH_PROFILE": "work-team"}
    return env, profile_dir


def run_tap(env, stdin: bytes, *args: str, cwd=None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([*TAP, *args], input=stdin, env=env, capture_output=True, cwd=cwd)


def test_the_tap_writes_the_file_and_prints_nothing_without_a_delegate(profile_env):
    env, profile_dir = profile_env

    result = run_tap(env, json.dumps(payload(five_hour=window(23.5, HOUR))).encode())

    assert (result.returncode, result.stdout) == (0, b"")
    path = profile_dir / RATE_LIMITS_FILE
    data = json.loads(path.read_text())
    assert data["profile"] == "work-team"
    assert data["windows"]["five_hour"]["used_percentage"] == 23.5
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in profile_dir.iterdir()) == sorted([LOCK_FILE, RATE_LIMITS_FILE])


DELEGATE = """\
import os, sys
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(f"{len(data)} {os.getcwd()} {os.environ.get('FP_CC_AUTH_SWITCH_PROFILE')}\\n".encode())
sys.stdout.buffer.write(data)
sys.stderr.write("to the debug log\\n")
sys.exit(int(os.environ.get("EXIT", "0")))
"""


@pytest.mark.parametrize("exit_code", [0, 3])
def test_the_delegate_gets_the_same_stdin_and_its_output_and_exit_status_pass_through(profile_env, sandbox, exit_code):
    env, profile_dir = profile_env
    script = sandbox.write("status line.py", DELEGATE)
    delegate = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    stdin = json.dumps(payload(five_hour=window(10, HOUR))).encode() + "\n é".encode()
    env = {**env, "EXIT": str(exit_code)}

    tapped = run_tap(env, stdin, "--delegate", delegate, cwd=sandbox.home)
    direct = subprocess.run(["/bin/sh", "-c", delegate], input=stdin, env=env, capture_output=True, cwd=sandbox.home)

    assert (tapped.returncode, tapped.stdout, tapped.stderr) == (direct.returncode, direct.stdout, direct.stderr)
    assert tapped.stdout.endswith(stdin)


# The shell that the tap becomes reports its own ignored signals and LC_CTYPE.
PROCESS_STATE = "awk '/^SigIgn:/ {print $2}' /proc/$$/status; echo \"LC_CTYPE=${LC_CTYPE-unset}\""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("lc_ctype", [None, "C"])
def test_the_delegate_gets_the_signals_and_locale_it_would_get_from_claude_code(profile_env, lc_ctype):
    env, _ = profile_env
    env = {key: value for key, value in env.items() if not key.startswith("LC_") and key not in ("LANG", "LANGUAGE")}
    if lc_ctype is not None:
        env["LC_CTYPE"] = lc_ctype

    tapped = run_tap(env, b"{}", "--delegate", PROCESS_STATE)
    direct = subprocess.run(["/bin/sh", "-c", PROCESS_STATE], input=b"{}", env=env, capture_output=True)

    ignored_hex, locale = tapped.stdout.decode().splitlines()
    ignored = int(ignored_hex, 16)
    # SIGPIPE is signal 13 and SIGXFSZ 25; Python ignores both, and an ignored signal survives exec.
    assert not ignored & (1 << 12) and not ignored & (1 << 24)
    assert locale == f"LC_CTYPE={lc_ctype or 'unset'}"
    assert tapped.stdout == direct.stdout


def test_broken_input_or_an_unwritable_profile_still_runs_the_delegate(profile_env):
    env, profile_dir = profile_env
    profile_dir.chmod(0o500)
    try:
        written = run_tap(env, json.dumps(payload(five_hour=window(10, HOUR))).encode(), "--delegate", "echo ok")
        broken = run_tap(env, b"{not json", "--delegate", "cat")
    finally:
        profile_dir.chmod(0o700)

    assert (written.returncode, written.stdout) == (0, b"ok\n")
    assert b"could not record the rate limits" in written.stderr
    assert (broken.returncode, broken.stdout, broken.stderr) == (0, b"{not json", b"")
    assert not (profile_dir / RATE_LIMITS_FILE).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="flock")
def test_a_lock_held_elsewhere_delays_the_write_only_briefly(profile_env):
    import fcntl

    env, profile_dir = profile_env
    with open(profile_dir / LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        result = run_tap(env, json.dumps(payload(five_hour=window(10, HOUR))).encode(), "--delegate", "echo ok")
        elapsed = time.monotonic() - started

    assert (result.returncode, result.stdout) == (0, b"ok\n")
    assert b"writing without the lock" in result.stderr
    assert json.loads((profile_dir / RATE_LIMITS_FILE).read_text())["windows"]["five_hour"]["used_percentage"] == 10
    assert elapsed < 2


def test_a_corrupt_file_is_replaced(profile_env):
    env, profile_dir = profile_env
    (profile_dir / RATE_LIMITS_FILE).write_text("{trunc")

    result = run_tap(env, json.dumps(payload(seven_day=window(12, 86400))).encode())

    assert result.returncode == 0
    assert json.loads((profile_dir / RATE_LIMITS_FILE).read_text())["windows"]["seven_day"]["used_percentage"] == 12


@pytest.mark.perf
def test_the_tap_adds_little_to_the_status_line(profile_env):
    env, _ = profile_env
    stdin = json.dumps(payload(five_hour=window(10, HOUR), seven_day=window(20, 86400))).encode()
    delegate = "cat >/dev/null; echo ok"

    def timed(argv):
        started = time.perf_counter()
        subprocess.run(argv, input=stdin, env=env, capture_output=True, check=True)
        return time.perf_counter() - started

    direct, tapped = [], []
    for _ in range(50):
        direct.append(timed(["/bin/sh", "-c", delegate]))
        tapped.append(timed([*TAP, "--delegate", delegate]))
    added_ms = (statistics.median(tapped) - statistics.median(direct)) * 1000
    print(f"\nstatus line tap adds {added_ms:.1f} ms at the median")
    assert added_ms < 50


def test_the_cli_runs_the_tap_without_argparse(profile_env):
    env, _ = profile_env

    result = run_tap(env, b"", "--unknown-flag")

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
