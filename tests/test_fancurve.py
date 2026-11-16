"""The fan controller's control loop, write test, start script and installation.

The loop is the real one: `fancurve simulate` runs the same `tick` the daemon
runs, with only the hardware calls replaced by values given on stdin. Each
scenario is a way the fan could be left wrong (too loud for no reason, or held
in manual mode at a stale speed) and asserts what the loop does about it.
Nothing here touches a fan.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DEPLOY = HERE / "install.sh"
DIR = HERE
CONF = DIR / "fancurve.conf"
LABEL = "io.github.atchisonbrent.fancurve"
DEST = "/usr/local/libexec/fancurve"
FAN_MIN, FAN_MAX = 1000, 4900


def settings(text: str | None = None) -> dict[str, str]:
    lines = (CONF.read_text() if text is None else text).splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def line_rpm(temperature: float) -> float:
    conf = settings()
    start, full, top = float(conf["start"]), float(conf["full"]), min(float(conf["max_rpm"]), FAN_MAX)
    return FAN_MIN + (top - FAN_MIN) * min(1, max(0, (temperature - start) / (full - start)))


class Loop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="fancurve-test-")
        cls.binary = f"{cls.tmp.name}/fancurve"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", cls.binary, str(DIR / "fancurve.c"),
                        "-framework", "IOKit", "-framework", "CoreFoundation"], check=True)
        conf = settings()
        cls.fill = round(float(conf["window"]) / float(conf["interval"]))
        cls.start, cls.full, cls.top = float(conf["start"]), float(conf["full"]), float(conf["max_rpm"])
        cls.release = cls.start - float(conf["hysteresis"])
        cls.step = float(conf["ramp"]) * float(conf["interval"])
        cls.mid = (cls.start + cls.full) / 2

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def sim(self, ticks: list[str], conf: Path = CONF, code: int = 0) -> list[dict]:
        """One entry per tick: the state after it and what the loop logged during it."""
        done = subprocess.run([self.binary, "simulate", str(conf)], input="\n".join(ticks) + "\n",
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        out, notes = [], []
        for line in done.stdout.splitlines():
            if line.startswith("| "):
                notes.append(line[2:])
            elif line.startswith("mode="):
                state = dict(word.split("=") for word in line.split() if "=" in word)
                out.append({"mode": int(state["mode"]), "target": float(state["target"]), "held": int(state["held"]),
                            "log": " / ".join(notes), "give_up": line.endswith("give-up")})
                notes = []
            else:
                self.stop_line = line
        return out

    def hot(self, temperature: float | None = None) -> list[str]:
        """Long enough at one temperature (the top of the curve unless given) to take the fan and finish the ramp."""
        temperature = self.full + 5 if temperature is None else temperature
        return [f"t={temperature}"] * (self.fill + round((self.top - FAN_MIN) / self.step) + 3)

    # -- too loud for no reason --

    def test_a_spike_does_not_move_the_fan(self) -> None:
        ticks = self.sim(["t=55"] * self.fill + ["t=99"] + ["t=55"] * self.fill)
        self.assertEqual({t["mode"] for t in ticks}, {0})
        self.assertEqual({t["target"] for t in ticks}, {FAN_MIN})

    def test_nothing_is_taken_on_a_part_filled_window(self) -> None:
        ticks = self.sim(["t=99"] * (self.fill - 1))
        self.assertEqual({t["mode"] for t in ticks}, {0})
        self.assertEqual(self.sim(["t=99"] * self.fill)[-1]["mode"], 1)

    def test_a_fan_found_held_while_cool_is_released(self) -> None:
        first = self.sim(["mode=1 target=4900 t=50"])[0]
        self.assertEqual((first["mode"], first["held"]), (0, 0))
        self.assertIn("found the fan held", first["log"])

    def test_the_fan_goes_back_when_it_cools_and_a_refused_release_is_retried(self) -> None:
        cool = ["t=50"] * self.fill
        ticks = self.sim(self.hot() + cool)
        released = next(i for i, t in enumerate(ticks) if t["mode"] == 0 and i > self.fill)
        self.assertIn("fan back to macOS", ticks[released]["log"])
        self.assertEqual({t["mode"] for t in ticks[released:]}, {0})
        # The same run with the release refused on that tick only.
        cool[released - len(self.hot())] += " mode-refused"
        ticks = self.sim(self.hot() + cool)
        self.assertEqual(ticks[released]["mode"], 1)
        self.assertIn("NOT confirmed back with macOS", ticks[released]["log"])
        self.assertEqual(ticks[released + 1]["mode"], 0)
        self.assertIn("fan back to macOS", ticks[released + 1]["log"])
        # A release the SMC accepts and ignores is not reported as a release.
        cool[released - len(self.hot())] = "t=50 mode-stuck"
        stuck = self.sim(self.hot() + cool)[released]
        self.assertEqual(stuck["mode"], 1)
        self.assertIn("NOT confirmed back with macOS", stuck["log"])
        self.assertNotIn("fan back to macOS", stuck["log"])

    # -- held at a stale speed --

    def test_sustained_heat_takes_the_fan_at_the_line(self) -> None:
        ticks = self.sim(["t=55"] * self.fill + self.hot(self.mid))
        hot = ticks[self.fill:]
        # The average of the cool and the warm readings first reaches `start` on this warm tick.
        crossing = next(k for k in range(1, self.fill + 1) if (55 * (self.fill - k) + self.mid * k) / self.fill >= self.start)
        self.assertEqual({t["mode"] for t in hot[:crossing - 1]}, {0})
        self.assertEqual(hot[crossing - 1]["mode"], 1)
        self.assertIn("fan taken", hot[crossing - 1]["log"])
        self.assertTrue(line_rpm(self.mid) - 50 <= hot[-1]["target"] <= line_rpm(self.mid), hot[-1])

    def test_the_fan_never_jumps_and_never_passes_the_ceiling(self) -> None:
        ticks = self.sim(["t=55"] * self.fill + ["t=125"] * (len(self.hot()) + 5) + ["t=50"] * (len(self.hot()) + self.fill))
        speeds = [FAN_MIN] + [t["target"] for t in ticks]
        self.assertEqual(max(speeds), self.top)
        self.assertLess(self.top, FAN_MAX)
        self.assertLessEqual(max(abs(b - a) for a, b in zip(speeds, speeds[1:])), self.step)
        self.assertEqual(ticks[-1]["mode"], 0)

    def test_a_refused_target_releases_the_fan_until_a_fresh_window(self) -> None:
        settled = len(self.hot(self.mid))
        ticks = self.sim(self.hot(self.mid) + ["t=120 target-refused"] + ["t=120"] * self.fill)
        refused = ticks[settled]
        self.assertEqual((refused["mode"], refused["held"]), (0, 0), "manual mode with the old target must not last")
        self.assertIn("cannot set the fan", refused["log"])
        self.assertEqual({t["mode"] for t in ticks[settled:-1]}, {0})
        self.assertEqual(ticks[-1]["mode"], 1)
        self.assertEqual(ticks[-1]["target"], ticks[settled - 1]["target"] + self.step)

    def test_a_refused_mode_write_is_retried(self) -> None:
        ticks = self.sim(["t=120"] * (self.fill - 1) + ["t=120 mode-refused"] + ["t=120"] * self.fill)
        self.assertIn("cannot set the fan", ticks[self.fill - 1]["log"])
        self.assertEqual({t["mode"] for t in ticks[:-1]}, {0})
        self.assertEqual(ticks[-1]["mode"], 1)

    def test_a_mode_or_target_changed_behind_its_back_is_put_right(self) -> None:
        hot = f"t={self.full + 5}"
        ticks = self.sim(self.hot() + [f"{hot} mode=0", f"{hot} target=1000", hot])
        # A lost mode is taken again at the speed the fan is turning at.
        self.assertEqual((ticks[-3]["mode"], ticks[-3]["target"]), (1, self.top))
        self.assertIn("had been reset", ticks[-3]["log"])
        # A changed target is brought back up the ramp.
        self.assertEqual([t["target"] for t in ticks[-2:]], [1000 + self.step, 1000 + 2 * self.step])

    def test_a_fan_whose_state_cannot_be_read_is_left_to_macos(self) -> None:
        hot = f"t={self.full + 5}"
        for word in ("mode-unreadable", "target-unreadable"):
            with self.subTest(word):
                ticks = self.sim(self.hot() + [f"{hot} {word}"] * 5)
                self.assertEqual({t["mode"] for t in ticks[-5:]}, {0}, "never held blind at a speed it cannot see")
                self.assertIn("state cannot be read", ticks[-5]["log"])
        # Not taken at all while the speed macOS is running it at, or the
        # target it would have to read back, cannot be read: no cycling in and
        # out of manual mode however long that lasts.
        for word in ("actual-unreadable", "target-unreadable"):
            with self.subTest(word, taking=True):
                ticks = self.sim([f"{hot} {word}"] * (4 * self.fill))
                self.assertEqual({t["mode"] for t in ticks}, {0})
        # Giving it up is only claimed once the mode reads automatic.
        stuck = self.sim(self.hot() + [f"{hot} target-unreadable mode-stuck"])[-1]
        self.assertEqual(stuck["mode"], 1)
        self.assertIn("NOT confirmed back with macOS", stuck["log"])
        self.assertNotIn("left to macOS", stuck["log"])

    def test_a_held_target_outside_the_controllers_range_is_rewritten(self) -> None:
        between = f"t={(self.release + self.start) / 2}"
        settle = [between] * (len(self.hot()) + 3 * self.fill)
        ticks = self.sim(self.hot() + settle + [f"{between} target=0"])
        self.assertLess(ticks[-2]["target"] - FAN_MIN, 150)   # at rest inside the dead band
        self.assertEqual(ticks[-1]["target"], FAN_MIN, "a target of 0 must not be left standing")
        # With the ceiling at the fan's own maximum, a target beyond it is still brought back.
        conf = Path(self.tmp.name) / "wide.conf"
        conf.write_text(re.sub(r"^max_rpm=.*$", f"max_rpm={FAN_MAX}", CONF.read_text(), flags=re.M))
        long_hot = [f"t={self.full + 5}"] * (self.fill + round((FAN_MAX - FAN_MIN) / self.step) + 3)
        ticks = self.sim(long_hot + [f"t={self.full + 5} target={FAN_MAX + 1100}"], conf=conf)
        self.assertEqual((ticks[-2]["target"], ticks[-1]["target"]), (FAN_MAX, FAN_MAX))

    def test_the_floor_is_the_speed_at_this_takeover_not_an_earlier_one(self) -> None:
        warm = f"t={self.start + 1}"
        ticks = self.sim(["t=55"] * self.fill + [f"actual=1900 {warm}"] * self.fill + [f"{warm} mode=0 actual=1000"] + [warm] * 6)
        self.assertEqual(ticks[2 * self.fill - 1]["target"], 1900)
        self.assertEqual({t["mode"] for t in ticks[-7:]}, {1})
        self.assertLessEqual(max(t["target"] for t in ticks[-7:]), line_rpm(self.start + 1))

    def test_the_ceiling_outranks_everything_else(self) -> None:
        hot = f"t={self.full + 5}"
        # macOS already running the fan faster than the ceiling: it keeps it.
        ticks = self.sim(["t=55"] * self.fill + [f"actual={self.top + 600} {hot}"] * (self.fill + 3))
        self.assertEqual({t["mode"] for t in ticks}, {0})
        self.assertIn("above max_rpm", " ".join(t["log"] for t in ticks))
        # A held target above the ceiling comes straight down, however small the excess.
        for excess in (100, FAN_MAX - self.top):
            with self.subTest(excess=excess):
                ticks = self.sim(self.hot() + [f"{hot} target={self.top + excess}", hot])
                self.assertEqual([t["target"] for t in ticks[-2:]], [self.top, self.top])

    def test_readings_from_before_an_outage_do_not_retake_a_cool_fan(self) -> None:
        ticks = self.sim(self.hot() + ["t=fail"] * 29 + ["t=50"] * (self.fill + 2))
        self.assertEqual({t["mode"] for t in ticks[len(self.hot()) + 3:]}, {0})

    def test_the_fan_is_kept_between_the_release_and_start_temperatures(self) -> None:
        between = (self.release + self.start) / 2
        ticks = self.sim(self.hot() + [f"t={between}"] * (3 * self.fill) + [f"t={self.release - 1}"] * (self.fill + 1))
        held = ticks[len(self.hot()):len(self.hot()) + 3 * self.fill]
        self.assertEqual({t["mode"] for t in held}, {1})
        self.assertEqual(ticks[-1]["mode"], 0)

    def test_failed_readings_are_not_cooling(self) -> None:
        ticks = self.sim(self.hot() + ["t=fail"] * 30, code=1)
        failing = ticks[len(self.hot()):]
        self.assertEqual([t["mode"] for t in failing[:3]], [1, 1, 0])
        self.assertIn("readings are failing", failing[2]["log"])
        self.assertNotIn("°C: fan back", " ".join(t["log"] for t in failing[:2]))
        self.assertTrue(failing[-1]["give_up"])
        self.assertEqual(len(failing), 30)

    def test_most_sensors_dropping_out_is_a_failure_not_a_cool_reading(self) -> None:
        held = self.sim(self.hot())[-1]["target"]
        for answering in (4, 0):
            with self.subTest(answering=answering):
                ticks = self.sim(self.hot() + [f"t=40 p={answering}"] * 3)
                self.assertEqual([(t["mode"], t["target"]) for t in ticks[-3:-1]], [(1, held), (1, held)])
                self.assertEqual(ticks[-1]["mode"], 0)
                self.assertIn("readings are failing", ticks[-1]["log"])
                self.assertNotIn("°C: fan back", ticks[-1]["log"])
        # Sensors that have never given a plausible value are no reading at all.
        ticks = self.sim([f"t={self.full + 5} p=0"] * (self.fill + 2))
        self.assertEqual({t["mode"] for t in ticks}, {0})

    def test_never_slower_than_macos_was_running_it(self) -> None:
        ticks = self.sim(["t=55"] * self.fill + [f"actual=1900 t={self.start + 1}"] * self.fill)
        taken = next(t for t in ticks if t["mode"] == 1)
        self.assertLess(line_rpm(self.start + 1), 1900)
        self.assertEqual(taken["target"], 1900)

    def test_release_is_confirmed_by_reading_the_mode_back(self) -> None:
        self.sim(self.hot())
        self.assertEqual(self.stop_line, "stop: released")
        ticks = self.hot()
        ticks[-1] += " mode-stuck"
        self.sim(ticks)
        self.assertEqual(self.stop_line, "stop: NOT released")

    def test_the_stop_signal_releases_the_fan(self) -> None:
        # The daemon's own start, loop and stop, with only the hardware replaced.
        conf = Path(self.tmp.name) / "fast.conf"
        text = re.sub(r"^interval=.*$", "interval=1", CONF.read_text(), flags=re.M)
        conf.write_text(re.sub(r"^window=.*$", "window=1", text, flags=re.M))
        daemon = subprocess.Popen([self.binary, "run-simulated", str(conf)], stdout=subprocess.PIPE, text=True)
        try:
            lines = [daemon.stdout.readline(), daemon.stdout.readline()]
            self.assertIn("fan taken", lines[1])
            daemon.send_signal(signal.SIGTERM)
            rest, _ = daemon.communicate(timeout=10)
        finally:
            daemon.kill()
        # The stand-in fan's mode after the process has gone, not the message alone.
        self.assertEqual(rest.splitlines()[-2:], ["| stopping: fan returned to macOS", "mode=0"])
        self.assertEqual(daemon.returncode, 0)

    def test_a_start_that_fails_still_releases_a_fan_left_held(self) -> None:
        bad = Path(self.tmp.name) / "bad.conf"
        bad.write_text("start=7o\n")
        done = subprocess.run([self.binary, "run-simulated", str(bad), "held"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stdout.splitlines()[-1], "mode=0")

    # -- settings --

    def test_shipped_curve_reaches_a_moderate_speed_by_90_degrees(self) -> None:
        self.sim([])
        # The quiet default reaches its ceiling by 90 °C, with a gentle ramp.
        self.assertLessEqual(self.full, 90)
        self.assertEqual(line_rpm(90), self.top)
        self.assertTrue(2000 <= self.top <= 2900, self.top)
        self.assertLessEqual(self.step / float(settings()["interval"]), 50)
        self.assertEqual(line_rpm(self.start), FAN_MIN)
        self.assertGreaterEqual(self.release, 65)

    def test_unusable_settings_are_refused(self) -> None:
        good = CONF.read_text()
        cases = {
            "full below start": re.sub(r"^full=.*$", "full=60", good, flags=re.M),
            "missing key": re.sub(r"^window=.*\n", "", good, flags=re.M),
            "not a number": re.sub(r"^start=.*$", "start=7o", good, flags=re.M),
            "not finite": re.sub(r"^full=.*$", "full=nan", good, flags=re.M),
            "trailing text": re.sub(r"^start=.*$", "start=80 # warm", good, flags=re.M),
            "unknown key": good + "hold=10\n",
            "no sensors": re.sub(r"^sensors=.*$", "sensors=", good, flags=re.M),
            "bad prefix": re.sub(r"^sensors=.*$", "sensors=TVD,../x", good, flags=re.M),
            "interval zero": re.sub(r"^interval=.*$", "interval=0", good, flags=re.M),
            "ceiling at the minimum": re.sub(r"^max_rpm=.*$", "max_rpm=1000", good, flags=re.M),
            "no ramp": re.sub(r"^ramp=.*\n", "", good, flags=re.M),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertNotEqual(text, good)
                path = Path(self.tmp.name) / "case.conf"
                path.write_text(text)
                done = subprocess.run([self.binary, "simulate", str(path)], input="", capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stderr)

    @unittest.skipIf(os.geteuid() == 0, "as root these would reach the real fan")
    def test_run_and_auto_refuse_without_root(self) -> None:
        for command in (["run", str(CONF)], ["auto"]):
            done = subprocess.run([self.binary, *command], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, done.stderr)

    @unittest.skipIf(os.geteuid() == 0, "as root this would reach the real fan")
    def test_selftest_refuses_without_root(self) -> None:
        done = subprocess.run([self.binary, "selftest", str(CONF)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (1, "selftest writes to the SMC and needs root\n"))

    def test_selftest_outcomes_and_final_mode(self) -> None:
        cases = {
            "success": ([], 0, ["write test passed: fan held at 1300 rpm, measured 1300 rpm (was 1000), and returned to macOS"], 0),
            "already held": (["held"], 1, ["the fan is already held in manual mode (another fan tool or a running fancurve); stop it first"], 1),
            "mode refused": (["mode-refused"], 1, ["this Mac refused the fan write; the fan was never taken and is with macOS"], 0),
            "target refused": (["target-refused"], 1, ["this Mac refused the fan write", "fan returned to macOS"], 0),
            "mode reset": (["mode-reset"], 1, ["manual mode did not hold: the write was ignored or macOS took the fan back", "fan returned to macOS"], 0),
            "target changed": (["target-changed"], 1, ["the fan's target changed after it was written", "fan returned to macOS"], 0),
            "target unreadable": (["target-unreadable"], 1, ["the fan's target could not be read back", "fan returned to macOS"], 0),
            "never follows": (["never-follows"], 1, ["the SMC accepted the target but the fan did not follow it", "fan returned to macOS"], 0),
            "speed unreadable": (["actual-unreadable"], 1, ["the fan's measured speed cannot be read"], 0),
            "mode unreadable": (["mode-unreadable"], 1, ["the fan's mode cannot be read"], 0),
            "release ignored": (["mode-stuck"], 1, ["could NOT return the fan to macOS"], 1),
            "interrupted": (["interrupt"], 1, ["interrupted", "fan returned to macOS"], 0),
            "failure and release ignored": (["never-follows", "mode-stuck"], 1,
                                            ["the SMC accepted the target but the fan did not follow it", "could NOT return the fan to macOS"], 1),
        }
        for name, (words, code, lines, mode) in cases.items():
            with self.subTest(name):
                done = subprocess.run([self.binary, "selftest-simulated", str(CONF), *words],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                self.assertEqual(done.stdout.splitlines(), lines + [f"mode={mode}"])
                self.assertEqual(done.stderr, "")

    def test_selftest_target_is_chosen_within_the_curve_limits(self) -> None:
        # Up 300 rpm while that stays within max_rpm (2,700 here), otherwise down 300.
        for before, target in ((500, 1300), (2400, 2700), (2401, 2101), (2700, 2400), (4900, 4600)):
            with self.subTest(before=before):
                done = subprocess.run([self.binary, "selftest-simulated", str(CONF), f"actual={before}"],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(done.stdout.splitlines(), [f"write test passed: fan held at {target} rpm, measured {target} rpm (was {before}), and returned to macOS", "mode=0"])

    def test_selftest_goes_past_max_rpm_only_when_no_other_step_fits(self) -> None:
        tight = Path(self.tmp.name) / "tight.conf"
        tight.write_text(CONF.read_text().replace("max_rpm=2700", "max_rpm=1500"))
        for before, target in ((1000, 1300), (1250, 1550), (1300, 1000)):
            with self.subTest(before=before):
                done = subprocess.run([self.binary, "selftest-simulated", str(tight), f"actual={before}"],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(f"fan held at {target} rpm", done.stdout)

    def test_selftest_needs_the_fan_to_settle_at_the_target(self) -> None:
        # A fan that only drifts part of the way (here: stays 150 rpm short) has not followed the write.
        done = subprocess.run([self.binary, "selftest-simulated", str(CONF), "falls-short"], capture_output=True, text=True, timeout=5)
        self.assertEqual((done.returncode, done.stdout.splitlines()),
                         (1, ["the SMC accepted the target but the fan did not follow it", "fan returned to macOS", "mode=0"]))

    def test_selftest_signals_after_taking_the_fan_release_it(self) -> None:
        for word in ("interrupt", "hangup", "quit", "suspend"):
            with self.subTest(word):
                done = subprocess.run([self.binary, "selftest-simulated", str(CONF), word],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual((done.returncode, done.stdout.splitlines()),
                                 (1, ["interrupted", "fan returned to macOS", "mode=0"]))

    def test_selftest_speed_paths_must_settle_for_three_seconds(self) -> None:
        cases = {
            "1100,1200,1300,1400,1500,1600,1700,1800,1900,2000": 1,
            "1000,1100,1200,1290,1300,1300": 0,
            # Two close readings must not erase an earlier outlier.
            "1000,1300,1300,1700,1700,1700,1700,1700,1700,1700": 1,
            "1199": 1,
            "1401": 1,
            "1200": 0,
            "1400": 0,
            "1250,1300": 0,
        }
        for path, code in cases.items():
            with self.subTest(path):
                done = subprocess.run([self.binary, "selftest-simulated", str(CONF), f"path={path}"],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                self.assertEqual(done.stdout.splitlines()[-1], "mode=0")
                self.assertIn("did not follow it" if code else "write test passed", done.stdout)

    def test_selftest_invalid_settings_do_not_release_a_held_fan(self) -> None:
        bad = Path(self.tmp.name) / "bad-selftest.conf"
        bad.write_text("start=7o\n")
        done = subprocess.run([self.binary, "selftest-simulated", str(bad), "held"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines(), ["cannot load settings", "mode=1"])


class Hardware(unittest.TestCase):
    """Exercise SMC callers with the transport replaced, never a real device."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="fancurve-smc-")
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.conf = root / "fancurve.conf"
        cls.conf.write_text(re.sub(r"^sensors=.*$", "sensors=TA", CONF.read_text(), flags=re.M))
        source = root / "standin.c"
        source.write_text(r'''
#include <IOKit/IOKitLib.h>
#include <sys/sysctl.h>
#include <unistd.h>
static kern_return_t fake_call(mach_port_t, uint32_t, const void *, size_t, void *, size_t *);
static int fake_sysctl(const char *, void *, size_t *, void *, size_t);
static unsigned int fake_sleep(unsigned int);
static uid_t fake_uid;
#define IOConnectCallStructMethod fake_call
#define IOServiceGetMatchingService(...) ((io_service_t)1)
#define IOServiceOpen(...) KERN_SUCCESS
#define sysctlbyname fake_sysctl
#define geteuid() fake_uid
#define sleep fake_sleep
#define main fancurve_main
#include "fancurve.c"
#undef main

static const char *scenario;
static int mode_writes, target_writes, fan_reads, device_mode, seconds;
static int count = 256, listing, no_mode, fans = 1;
static struct { const char *key; float value; int unreadable; } entries[] = {
    {"TB01", 40, 0}, {"TA01", 5, 0}, {"TB02", 80, 0}, {"TA02", 130, 0},
    {"TA03", 4, 0}, {"TA04", 131, 0}, {"TA05", NAN, 0}, {"TA06", 50, 1},
    {"TC01", 60, 0}, {"TD01", 60, 0}, {"ZZ01", 60, 0}, {"TZ01", 0, 0}
};
static int is(const char *s) { return !strcmp(scenario, s); }
static int fake_sysctl(const char *name, void *out, size_t *size, void *in, size_t n) {
    (void)name; (void)out; (void)size; (void)in; (void)n;
    return -1;
}
static unsigned int fake_sleep(unsigned int n) {
    (void)n;
    seconds++;
    if (is("reset-refuses-release")) device_mode = 0;
    return 0;
}
static kern_return_t fake_call(mach_port_t port, uint32_t selector, const void *input,
                               size_t input_size, void *output, size_t *output_size) {
    (void)port; (void)selector; (void)input_size;
    const SMCData *in = input;
    SMCData *out = output;
    memset(out, 0, sizeof *out);
    *output_size = sizeof *out;
    char key[5];
    u2k(in->key, key);
    if (in->data8 == SMC_KEY_AT_INDEX) {
        if (listing) out->key = k2u(entries[in->data32].key);
        else {
            char k[5];
            if (in->data32 == 256) strcpy(k, "TB00");
            else snprintf(k, sizeof k, "TA%02X", (unsigned)(in->data32 & 255));
            out->key = k2u(k);
        }
        return KERN_SUCCESS;
    }
    int mode = !strcmp(key, "F0md");
    int num = !strcmp(key, "FNum");
    int keys = !strcmp(key, "#KEY");
    if ((mode && no_mode) || !strcmp(key, "F0Md") || !strcmp(key, "Ftst")) {
        out->result = 1; return KERN_SUCCESS;
    }
    if (in->data8 == SMC_KEY_INFO) {
        out->ki.dataType = k2u(mode || num ? "ui8 " : keys ? "ui32" : "flt ");
        out->ki.dataSize = mode || num ? 1 : 4;
        if (!strcmp(key, "TC01")) out->ki.dataType = k2u("ui32");
        if (!strcmp(key, "TD01")) out->ki.dataSize = 2;
    } else if (in->data8 == SMC_WRITE) {
        if (mode) {
            mode_writes++;
            if (is("refused") || is("auto-already") || (is("reset-refuses-release") && seconds)) out->result = 1;
            else if (is("refused-but-taken") && mode_writes == 1) { device_mode = 1; out->result = 1; }
            else device_mode = in->bytes[0];
        } else target_writes++;
    } else if (in->data8 == SMC_READ) {
        if (mode) {
            if (is("mode-unreadable-after-write") && mode_writes) out->result = 1;
            out->bytes[0] = (unsigned char)device_mode;
        } else if (num) { fan_reads++; out->bytes[0] = (unsigned char)fans; }
        else if (keys) {
            out->bytes[2] = (unsigned char)(count >> 8);
            out->bytes[3] = (unsigned char)count;
        } else {
            float value = !strcmp(key, "F0Mx") ? 4900 : !strcmp(key, "F0Mn") ? 1000 :
                          !strcmp(key, "F0Tg") ? 1300 : !strcmp(key, "F0Ac") ? (mode_writes ? 1300 : 1000) : 50;
            if (listing) for (size_t i = 0; i < sizeof entries / sizeof entries[0]; i++) {
                if (!strcmp(entries[i].key, key)) { value = entries[i].value; out->result = entries[i].unreadable; }
            }
            memcpy(out->bytes, &value, sizeof value);
        }
    }
    return KERN_SUCCESS;
}
int main(int argc, char **argv) {
    if (argc != 3) return 99;
    scenario = argv[1];
    int result;
    if (!strncmp(scenario, "sensors", 7)) {
        listing = 1; count = (int)(sizeof entries / sizeof entries[0]);
        no_mode = is("sensors-no-fan"); fans = no_mode ? 0 : 3; fake_uid = 501;
        char *args[] = {"fancurve", "sensors"};
        result = fancurve_main(2, args);
    } else if (!strncmp(scenario, "auto", 4)) {
        device_mode = is("auto-held");
        char *args[] = {"fancurve", "auto"};
        result = fancurve_main(2, args);
    } else if (is("full") || is("overflow") || is("overlap")) {
        Conf c = {.nprefix = 2, .prefix = {"TA", "TB"}, .max_rpm = 2400};
        count = is("overflow") ? MAX_SENSORS + 1 : MAX_SENSORS;
        if (is("overlap")) strcpy(c.prefix[1], "TA0");
        mode_key = "F0md";
        result = hw_init(&c) ? 1 : 0;
        printf("sensors=%d matched=%d,%d\n", nsensors, matched[0], matched[1]);
    } else {
        signal(SIGTSTP, SIG_IGN);
        char *args[] = {"fancurve", "selftest", argv[2]};
        result = fancurve_main(3, args);
        if (signal(SIGTSTP, SIG_IGN) != SIG_IGN) return 98;
    }
    fprintf(stderr, "mode-writes=%d target-writes=%d fan-reads=%d mode=%d\n",
            mode_writes, target_writes, fan_reads, device_mode);
    return result;
}
''')
        cls.binary = str(root / "standin")
        subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(DIR), str(source), "-o", cls.binary,
                        "-framework", "IOKit", "-framework", "CoreFoundation"], check=True)

    def invoke(self, scenario: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.binary, scenario, str(self.conf)], capture_output=True, text=True, timeout=5)

    def test_auto_does_not_write_if_already_automatic(self) -> None:
        done = self.invoke("auto-already")
        self.assertEqual((done.returncode, done.stdout), (0, "the fan is with macOS\n"))
        self.assertEqual(done.stderr, "mode-writes=0 target-writes=0 fan-reads=0 mode=0\n")
        done = self.invoke("auto-held")
        self.assertEqual((done.returncode, done.stdout), (0, "fan returned to macOS\n"))
        self.assertEqual(done.stderr, "mode-writes=1 target-writes=0 fan-reads=0 mode=0\n")

    def test_sensor_capacity_fails_without_marking_or_counting_the_extra_key(self) -> None:
        for scenario, code, count, matched in (("full", 0, 256, "1,0"), ("overflow", 1, 256, "1,0"), ("overlap", 0, 256, "1,1")):
            with self.subTest(scenario):
                done = self.invoke(scenario)
                self.assertEqual((done.returncode, done.stdout), (code, f"sensors={count} matched={matched}\n"))
                error = "more than 256 sensors match the configured prefixes; narrow sensors\n" if code else ""
                self.assertEqual(done.stderr, error + "mode-writes=0 target-writes=0 fan-reads=1 mode=0\n")

    def test_selftest_refusal_never_attempts_release_of_an_untaken_fan(self) -> None:
        done = self.invoke("refused")
        self.assertEqual((done.returncode, done.stdout),
                         (1, "this Mac refused the fan write; the fan was never taken and is with macOS\n"))
        self.assertEqual(done.stderr, "mode-writes=1 target-writes=0 fan-reads=1 mode=0\n")
        done = self.invoke("refused-but-taken")
        self.assertEqual((done.returncode, done.stdout.splitlines()),
                         (1, ["this Mac refused the fan write", "fan returned to macOS"]))
        self.assertEqual(done.stderr, "mode-writes=2 target-writes=0 fan-reads=1 mode=0\n")

    def test_selftest_confirms_an_already_released_fan_without_writing(self) -> None:
        done = self.invoke("reset-refuses-release")
        self.assertEqual((done.returncode, done.stdout.splitlines()),
                         (1, ["manual mode did not hold: the write was ignored or macOS took the fan back", "fan returned to macOS"]))
        self.assertEqual(done.stderr, "mode-writes=1 target-writes=1 fan-reads=1 mode=0\n")

    def test_selftest_mode_becoming_unreadable_has_its_own_reason(self) -> None:
        done = self.invoke("mode-unreadable-after-write")
        self.assertEqual((done.returncode, done.stdout.splitlines()),
                         (1, ["the fan's mode could not be read back", "could NOT return the fan to macOS"]))
        self.assertEqual(done.stderr, "mode-writes=4 target-writes=1 fan-reads=1 mode=0\n")

    def test_sensors_lists_sorted_groups_without_needing_fans_or_root(self) -> None:
        for scenario, mode in (("sensors-no-fan", "none"), ("sensors-many-fans", "F0md")):
            with self.subTest(scenario):
                done = self.invoke(scenario)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(done.stdout.splitlines(), [
                    f"unknown model, macOS unknown version, fan mode key {mode}",
                    "  TA   6 sensors, 2 answering: 5.0 to 130.0 °C (hottest TA02)",
                    "  TB   2 sensors, 2 answering: 40.0 to 80.0 °C (hottest TB02)",
                    "  TZ   1 sensors, 0 answering: no plausible temperatures",
                    "sensors that are chip temperatures sit near each other at idle and climb together under load",
                ])
                self.assertEqual(done.stderr, "mode-writes=0 target-writes=0 fan-reads=0 mode=0\n")


class Install(unittest.TestCase):
    def test_job_runs_the_installed_start_script(self) -> None:
        job = plistlib.loads((DIR / f"{LABEL}.plist").read_bytes())
        self.assertEqual(job["Label"], LABEL)
        self.assertEqual(job["ProgramArguments"], [f"{DEST}/start"])
        self.assertTrue(job["RunAtLoad"] and job["KeepAlive"])
        deploy, start = DEPLOY.read_text(), (DIR / "start.sh").read_text()
        self.assertIn(f"LABEL={LABEL}\n", deploy)
        self.assertIn(f"    dest={DEST}\n", deploy)
        self.assertIn(f"D={DEST}\n", start)
        self.assertEqual(job["StandardOutPath"], re.search(r"^L=(\S+)$", start, re.M).group(1))
        # The controller replaces the shell, so launchd's stop signal reaches it.
        self.assertTrue(start.endswith('exec "$D/fancurve" run "$D/fancurve.conf"\n'))

    def test_start_caps_the_log_only_after_copying_its_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fancurve-start-") as tmp:
            root = Path(tmp)
            stub = root / "fancurve"
            stub.write_text(f'#!/bin/sh\necho "$1 $2" >> "{root}/calls"\n')
            stub.chmod(0o755)
            log, kept = root / "log", root / "log.1"
            script = (DIR / "start.sh").read_text().replace(f"D={DEST}\n", f"D={root}\n")
            script = re.sub(r"^L=\S+$", f"L={log}", script, flags=re.M)

            def start(size: int) -> None:
                log.write_bytes(b"a" * (size - 262144) + b"z" * 262144)
                (root / "calls").unlink(missing_ok=True)
                subprocess.run(["sh", "-c", script], check=True, capture_output=True)
                self.assertEqual((root / "calls").read_text(), f"run {root}/fancurve.conf\n")

            start(1048576)
            self.assertEqual(log.stat().st_size, 1048576)
            self.assertFalse(kept.exists())
            start(1048577)
            self.assertEqual((log.stat().st_size, kept.read_bytes()), (0, b"z" * 262144), "the newest part is what is kept")
            # The copy cannot be written: the log is left as it was.
            kept.unlink()
            kept.mkdir()
            start(1048577)
            self.assertEqual(log.stat().st_size, 1048577)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="fancurve install ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dest, self.jobs, self.logs = (self.root / name for name in ("dest", "jobs", "logs"))
        self.bin_dir = self.root / "bin"
        for folder in (self.jobs, self.logs, self.bin_dir, self.root / "build"):
            folder.mkdir()
        (self.root / "fancurve-test-root").touch()
        self.calls = self.root / "calls"
        self.calls.touch()
        (self.root / "privileged").touch()
        (self.root / "trace").touch()
        self.env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "TMPDIR": str(self.root / "build"),
            "TEST_ROOT": str(self.root),
            "FANCURVE_TEST_ROOT": str(self.root),
        }
        self.controller = '''#!/bin/sh
printf 'fancurve %s\n' "$1" >> "$TEST_ROOT/calls"
printf 'fancurve %s\n' "$1" >> "$TEST_ROOT/trace"
case "$1" in
check) exit "${CHECK:-0}" ;;
auto)
    printf '%s\n' "$0" >> "$TEST_ROOT/auto-binaries"
    if [ -f "$TEST_ROOT/tested-before-replacement" ]; then exit "${AUTO_AFTER_TEST:-0}"; fi
    exit "${AUTO:-0}" ;;
selftest)
    [ -f "$2" ] || exit 89
    [ "$0" = "$FANCURVE_TEST_ROOT/dest.new/fancurve" ] || exit 90
    [ "$2" = "$FANCURVE_TEST_ROOT/dest.new/fancurve.conf" ] || exit 91
    [ ! -f "$TEST_ROOT/loaded" ] || exit 92
    [ ! -e "$FANCURVE_TEST_ROOT/dest.old" ] || exit 93
    if [ -e "$FANCURVE_TEST_ROOT/dest" ]; then
        [ "$(cat "$FANCURVE_TEST_ROOT/dest/fancurve")" = old ] || exit 94
        [ "$(cat "$FANCURVE_TEST_ROOT/jobs/io.github.atchisonbrent.fancurve.plist")" = job ] || exit 95
    fi
    touch "$TEST_ROOT/tested-before-replacement"
    if [ "${SELFTEST_SLEEP:-0}" = 1 ]; then /bin/sleep 2; fi
    exit "${SELFTEST:-0}" ;;
status) [ -f "$2" ] || exit 89; exit "${READING:-0}" ;;
*) exit 96 ;;
esac
'''
        (self.root / "controller").write_text(self.controller)
        (self.root / "controller").chmod(0o755)
        stubs = {
            "id": 'echo "${TEST_UID:-501}"',
            "sudo": '''printf '%s\n' "$*" >> "$TEST_ROOT/privileged"
if [ "$*" = "-n true" ]; then echo 'sudo -n true' >> "$TEST_ROOT/trace"; exit "${NOPASS:-1}"; fi
[ "$1" != -n ] || exit 97
if [ "$1" = -v ]; then echo 'sudo -v' >> "$TEST_ROOT/trace"; exit "${AUTH:-0}"; fi
if [ "${2:-}" = selftest ] && [ "${SUDO_SELFTEST:-0}" = 1 ]; then exit 1; fi
exec "$@"''',
            "sysctl": '[ "$*" = "-n hw.optional.arm64" ] || exit 99\necho "${ARM64:-1}"',
            "stat": 'printf "%s\\n" "$*" >> "$TEST_ROOT/stat-calls"\necho "${OWNERSHIP:-0 drwxr-xr-x}"',
            "sleep": "exit 0",
            "xcode-select": 'exit "${NOTOOLS:-0}"',
            "plutil": 'echo plutil >> "$TEST_ROOT/trace"\nexit "${PLUTIL:-0}"',
            "cc": '''echo cc >> "$TEST_ROOT/trace"
printf '%s\n' "$@" > "$TEST_ROOT/compiler-args"
[ "${COMPILE:-0}" = 0 ] || exit 1
while [ "$1" != -o ]; do shift; done
cp "$TEST_ROOT/controller" "$2"
printf '%s\n' "$2" > "$TEST_ROOT/build-output"''',
            "install": '''d=0 owner= group= mode=
while :; do case "$1" in
-d) d=1; shift;; -o) owner=$2; shift 2;; -g) group=$2; shift 2;; -m) mode=$2; shift 2;; *) break;; esac; done
printf '%s\t%s\t%s\t%s\n' "$owner" "$group" "$mode" "$*" >> "$TEST_ROOT/install-calls"
if [ "$d" = 1 ]; then mkdir -p "$@"; else cp "$1" "$2"; chmod "$mode" "$2"; fi''',
            "mv": '''echo mv >> "$TEST_ROOT/trace"
if [ "$1" = "$FANCURVE_TEST_ROOT/dest.new" ] && [ "${REPLACE:-0}" = 1 ]; then exit 1; fi
if [ "$1" = "$FANCURVE_TEST_ROOT/dest.old" ] && [ "${RESTORE:-0}" = 1 ]; then exit 1; fi
if [ "$2" = "$FANCURVE_TEST_ROOT/jobs/io.github.atchisonbrent.fancurve.plist" ] && [ "${JOB_MOVE:-0}" = 1 ]; then exit 1; fi
exec /bin/mv "$@"''',
            "launchctl": '''case "$1" in
print)
    [ "${QUERY:-0}" = 0 ] || exit "$QUERY"
    [ -f "$TEST_ROOT/loaded" ] || exit 113
    echo "${JOB_STATE:-state = running}"
    if [ -f "$TEST_ROOT/started" ]; then exit "${POST_QUERY:-0}"; fi ;;
bootout)
    echo 'launchctl bootout' >> "$TEST_ROOT/calls"
    echo 'launchctl bootout' >> "$TEST_ROOT/trace"
    [ "${BOOTOUT:-0}" = 0 ] || exit 1
    [ "${STILL_LOADED:-0}" = 1 ] || rm -f "$TEST_ROOT/loaded" ;;
bootstrap)
    echo 'launchctl bootstrap' >> "$TEST_ROOT/calls"
    echo 'launchctl bootstrap' >> "$TEST_ROOT/trace"
    [ "${BOOTSTRAP:-0}" = 0 ] || exit 1
    touch "$TEST_ROOT/started"
    [ "${UNSEEN:-0}" = 1 ] || touch "$TEST_ROOT/loaded" ;;
*) exit 98 ;;
esac''',
        }
        for stub, text in stubs.items():
            self.stub(stub, text)

    def stub(self, name: str, text: str) -> None:
        path = self.bin_dir / name
        path.write_text(f"#!/bin/sh\n{text}\n")
        path.chmod(0o755)

    def old_install(self, binary: bool | None = True, loaded: bool = True) -> None:
        if binary is not None:
            self.dest.mkdir()
            (self.jobs / f"{LABEL}.plist").write_text("job")
            for name in ("fancurve.log", "fancurve.log.1"):
                (self.logs / name).write_text("log")
        if binary:
            (self.dest / "fancurve").write_text("old")
        if loaded:
            (self.root / "loaded").touch()

    def invoke(self, *args: str, **faults: str) -> subprocess.CompletedProcess:
        # An unrelated working directory also checks the script's own-directory cd.
        return subprocess.run(["/bin/sh", str(DEPLOY), *args], cwd=self.root, capture_output=True, text=True,
                              env={**self.env, **faults}, timeout=15)

    def assert_build_removed(self) -> None:
        self.assertEqual(list((self.root / "build").iterdir()), [], "the build folder is removed however it ends")
        self.assertFalse(Path(f"{self.dest}.new").exists(), "staged files are removed however it ends")

    def test_install_checks_before_stopping_and_releases_before_replacing(self) -> None:
        order = ["fancurve check", "launchctl bootout", "fancurve auto", "fancurve selftest", "launchctl bootstrap"]
        cases = {
            "all well": ({}, 0, order + ["fancurve status"], "new", ""),
            "check fails": ({"CHECK": "1"}, 1, order[:1], "old", None),
            "release not confirmed": ({"AUTO": "1"}, 1, order[:3], "old", "the fan is NOT confirmed back with macOS"),
            "start fails": ({"BOOTSTRAP": "1"}, 1, order, "new", "the new controller is not confirmed running: it may or may not have started. Run status."),
            "started but not seen running": ({"UNSEEN": "1"}, 1, order, "new", "the new controller is not confirmed running: it may or may not have started. Run status."),
            "cannot ask launchd": ({"QUERY": "5"}, 1, order[:1], "old", "cannot tell whether"),
            "failed query with running text": ({"POST_QUERY": "5"}, 1, order, "new", "the new controller is not confirmed running: it may or may not have started. Run status."),
            "bootout fails": ({"BOOTOUT": "1"}, 1, order[:2], "old", "it may or may not be running"),
            "job will not stop": ({"STILL_LOADED": "1"}, 1, order[:2], "old", "it may or may not be running"),
        }
        for name, (faults, code, calls, installed, message) in cases.items():
            with self.subTest(name):
                self.old_install()
                done = self.invoke("install", **faults)
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                self.assertEqual(self.calls.read_text().splitlines(), calls)
                self.assertEqual("old" if (self.dest / "fancurve").read_text() == "old" else "new", installed)
                # Nothing was stopped when check fails, so nothing is said about the fan.
                if message is None:
                    self.assertEqual(done.stderr, "")
                    self.assertEqual((self.root / "privileged").read_text(), "")
                else:
                    self.assertIn(message, done.stderr)
                    self.assertNotIn("the controller is NOT running", done.stderr.replace(message, ""))
                self.assert_build_removed()
                if code == 0:
                    self.assert_installed_set()
                    self.assertTrue((self.jobs / f"{LABEL}.plist").exists() and not Path(f"{self.dest}.old").exists())
                    self.assertEqual(done.stderr, "")
                    self.assertIn("fancurve is installed and running", done.stdout)
                    self.assertIn(f"{self.logs}/fancurve.log", done.stdout)
                    self.assertEqual(done.stdout.splitlines()[-2:], ["Status: ./install.sh status", "Uninstall: ./install.sh uninstall"])
                    trace = (self.root / "trace").read_text().splitlines()
                    self.assertEqual(trace[:5], ["cc", "plutil", "fancurve check", "sudo -n true", "sudo -v"])
                    self.assertLess(trace.index("fancurve auto"), trace.index("fancurve selftest"))
                    self.assertLess(trace.index("fancurve selftest"), trace.index("mv"))
                    self.assertLess(trace.index("mv"), trace.index("launchctl bootstrap"))
                    self.assertTrue((self.root / "tested-before-replacement").exists())
                    args = (self.root / "compiler-args").read_text().splitlines()
                    for flag in ("-O2", "-Wall", "-Wextra"):
                        self.assertIn(flag, args)
                    self.assertNotIn("-Werror", args)
                    self.assertTrue(Path(args[args.index("-o") + 1]).is_relative_to(self.root / "build"))
                self.reset_case()

    def test_no_password_is_asked_for_where_sudo_needs_none(self) -> None:
        done = self.invoke("install", NOPASS="0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        trace = (self.root / "trace").read_text().splitlines()
        self.assertIn("sudo -n true", trace)
        self.assertNotIn("sudo -v", trace)

    def assert_installed_set(self) -> None:
        self.assertEqual({p.name for p in self.dest.iterdir()}, {"fancurve", "fancurve.conf", "start"})
        self.assertTrue((self.jobs / f"{LABEL}.plist").is_file())
        records = [line.split("\t", 3) for line in (self.root / "install-calls").read_text().splitlines()]
        for name, mode in (("fancurve", "755"), ("start", "755"), ("fancurve.conf", "644"), (f"{LABEL}.plist", "644")):
            record, = [r for r in records if r[3].endswith(f" {self.dest}.new/{name}")]
            self.assertEqual(record[:3], ["root", "wheel", mode])
        self.assertEqual((self.dest / "fancurve.conf").read_bytes(), CONF.read_bytes())

    def test_unmarked_test_root_is_refused_before_any_command(self) -> None:
        (self.root / "fancurve-test-root").unlink()
        self.stub("dirname", 'echo dirname >> "$TEST_ROOT/trace"; exit 1')
        done = self.invoke("install")
        self.assertEqual((done.returncode, done.stderr),
                         (1, "FANCURVE_TEST_ROOT is set but is not a test directory\n"))
        self.assertEqual((self.root / "trace").read_text(), "")
        self.assertEqual((self.root / "privileged").read_text(), "")
        self.assertEqual(self.calls.read_text(), "")

    def test_old_path_overrides_have_no_effect(self) -> None:
        overrides = {f"FANCURVE_{name}": str(self.root / f"unused-{name}") for name in ("DEST", "JOBS", "LOGS")}
        done = self.invoke("install", **overrides)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assert_installed_set()
        self.assertTrue(all(not Path(value).exists() for value in overrides.values()))
        # Stop a temporary copy before its command dispatch: observing real-mode
        # path selection must never reach a real privileged command.
        script = self.root / "path-probe.sh"
        self.assertEqual(DEPLOY.read_text().count('label=$LABEL\n'), 1, "the probe must stop the copy before it does anything")
        text = DEPLOY.read_text().replace('label=$LABEL\n',
                                         'label=$LABEL\nprintf "%s\\n" "$dest" "$jobs" "$logs" "$PATH"\nexit 0\n')
        script.write_text(text)
        self.stub("dirname", 'echo caller-path-used >&2; exit 1')
        env = {**self.env, **overrides}
        del env["FANCURVE_TEST_ROOT"]
        done = subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout.splitlines(), [DEST, "/Library/LaunchDaemons", "/var/log", "/usr/bin:/bin:/usr/sbin:/sbin"])

    def test_install_checks_each_existing_ancestor_before_privilege(self) -> None:
        # The normal test destination is directly below the excluded test root.
        # Nest only this fixture's destination to exercise the same walk.
        parent = self.root / "parent"
        parent.mkdir()
        nested = parent / "nested"
        nested.mkdir()
        script = self.root / "ancestor-probe.sh"
        text = DEPLOY.read_text()
        self.assertEqual(text.count('dest=$FANCURVE_TEST_ROOT/dest'), 1)
        script.write_text(text.replace('dest=$FANCURVE_TEST_ROOT/dest', 'dest=$FANCURVE_TEST_ROOT/parent/nested/dest'))
        for ownership, safe in (("0 drwxr-xr-x", True), ("501 drwxr-xr-x", False),
                                ("0 drwxrwxr-x", False), ("0 drwxr-xrwx", False)):
            with self.subTest(ownership):
                done = subprocess.run(["/bin/sh", str(script), "install"], cwd=self.root,
                                      env={**self.env, "OWNERSHIP": ownership, "COMPILE": "1"},
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(done.returncode, 1)
                expected = "" if safe else f"{nested} is not owned by root or is writable by others; a root job cannot be installed safely under it\n"
                self.assertEqual(done.stderr, expected)
                paths = (self.root / "stat-calls").read_text().splitlines()
                self.assertEqual(paths, [f"-f %u %Sp {p}" for p in ([nested, parent] if safe else [nested])])
                self.assertEqual((self.root / "privileged").read_text(), "")
                self.assertEqual((self.root / "trace").read_text().splitlines(), ["cc"] if safe else [])
                self.assert_build_removed()
                self.reset_case()

    def test_a_symbolic_link_in_the_install_path_is_refused(self) -> None:
        # `stat` describes a link, not where it leads, so ownership cannot vouch for one.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.dest.symlink_to(elsewhere)
        done = self.invoke("install")
        self.assertEqual((done.returncode, done.stderr),
                         (1, f"{self.dest} is a symbolic link; a root job cannot be installed safely through it\n"))
        self.assertEqual((self.root / "privileged").read_text(), "")
        self.assertEqual((self.root / "trace").read_text(), "")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.dest.unlink()
        # The same for a directory above it.
        (self.root / "real-parent").mkdir()
        (self.root / "parent").symlink_to(self.root / "real-parent")
        script = self.root / "link-probe.sh"
        text = DEPLOY.read_text()
        self.assertEqual(text.count('dest=$FANCURVE_TEST_ROOT/dest'), 1)
        script.write_text(text.replace('dest=$FANCURVE_TEST_ROOT/dest', 'dest=$FANCURVE_TEST_ROOT/parent/dest'))
        done = subprocess.run(["/bin/sh", str(script), "install"], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual((done.returncode, done.stderr),
                         (1, f"{self.root / 'parent'} is a symbolic link; a root job cannot be installed safely through it\n"))
        self.assertEqual((self.root / "privileged").read_text(), "")
        self.assertEqual(list((self.root / "real-parent").iterdir()), [])

    def test_a_stranded_previous_copy_is_not_restored_from_a_symbolic_link(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        Path(f"{self.dest}.old").symlink_to(elsewhere)
        done = self.invoke("install")
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"{self.dest}.old is a symbolic link; a root job cannot be installed safely through it", done.stderr)
        self.assertFalse(self.dest.exists() or self.dest.is_symlink(), "the link was not moved into place")
        self.assertNotIn("fancurve auto", self.calls.read_text())
        Path(f"{self.dest}.old").unlink()

    def test_uninstall_runs_nothing_as_root_from_an_unsafe_path(self) -> None:
        self.old_install(loaded=False)
        shutil.copy(self.root / "controller", self.dest / "fancurve")
        done = self.invoke("uninstall", OWNERSHIP="501 drwxr-xr-x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("is not owned by root or is writable by others", done.stderr)
        self.assertEqual(done.stderr.splitlines()[-1], "nothing was deleted")
        self.assertNotIn("fancurve auto", self.calls.read_text())
        self.assertTrue((self.dest / "fancurve").exists())

    def test_a_relative_test_root_is_refused(self) -> None:
        done = subprocess.run(["/bin/sh", str(DEPLOY), "uninstall"], cwd=self.root, capture_output=True, text=True, timeout=5,
                              env={**self.env, "FANCURVE_TEST_ROOT": "."})
        self.assertEqual((done.returncode, done.stderr), (1, "FANCURVE_TEST_ROOT is set but is not a test directory\n"))
        self.assertEqual((self.root / "privileged").read_text(), "")

    def reset_case(self) -> None:
        for path in (self.dest, Path(f"{self.dest}.old"), Path(f"{self.dest}.new")):
            if path.exists():
                shutil.rmtree(path)
        for folder in (self.jobs, self.logs):
            for path in folder.iterdir():
                path.unlink()
        for name in ("loaded", "started", "tested-before-replacement"):
            (self.root / name).unlink(missing_ok=True)
        for name in ("calls", "privileged", "trace", "install-calls", "auto-binaries", "stat-calls"):
            (self.root / name).write_text("")

    def test_failed_write_test_installs_nothing_and_reports_the_remaining_state(self) -> None:
        for loaded, previous in ((False, False), (True, True), (False, True)):
            with self.subTest(loaded=loaded, previous=previous):
                if previous:
                    self.old_install(loaded=loaded)
                done = self.invoke("install", SELFTEST="1")
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                expected = ["fancurve check"] + (["launchctl bootout"] if loaded else []) + ["fancurve auto", "fancurve selftest", "fancurve auto"]
                self.assertEqual(self.calls.read_text().splitlines(), expected)
                self.assert_build_removed()
                self.assertFalse((self.root / "loaded").exists())
                self.assertFalse(Path(f"{self.dest}.old").exists())
                self.assertTrue((self.root / "tested-before-replacement").exists())
                self.assertNotIn("mv", (self.root / "trace").read_text().splitlines())
                if previous:
                    self.assertEqual((self.dest / "fancurve").read_text(), "old")
                    self.assertEqual((self.jobs / f"{LABEL}.plist").read_text(), "job")
                    self.assertEqual(done.stderr.splitlines()[-1], "write test failed; nothing new was installed; macOS has the fan; the previous controller is stopped and its files are still in place.")
                    self.assertEqual((self.logs / "fancurve.log").read_text(), "log")
                else:
                    self.assertFalse(self.dest.exists())
                    self.assertEqual(list(self.jobs.iterdir()), [])
                    self.assertEqual(done.stderr.splitlines()[-1], "write test failed; nothing was installed; macOS has the fan.")
                self.reset_case()

    def test_failed_write_test_keeps_recovery_binary_when_auto_fails(self) -> None:
        for update in (False, True):
            with self.subTest(update=update):
                if update:
                    self.old_install()
                done = self.invoke("install", SELFTEST="1", AUTO_AFTER_TEST="1")
                self.assertEqual(done.returncode, 1)
                self.assertEqual(self.calls.read_text().splitlines()[-2:], ["fancurve selftest", "fancurve auto"])
                self.assertEqual((self.root / "auto-binaries").read_text().splitlines(), [f"{self.dest}.new/fancurve"] * 2)
                self.assertTrue(Path(f"{self.dest}.new/fancurve").is_file())
                self.assertEqual(list((self.root / "build").iterdir()), [])
                expected = [f"write test failed and the fan is NOT confirmed back with macOS. Nothing was installed. To return the fan: sudo {self.dest}.new/fancurve auto"]
                if update:
                    expected.append("the previous controller is stopped and its files are still in place.")
                    self.assertEqual((self.dest / "fancurve").read_text(), "old")
                else:
                    self.assertFalse(self.dest.exists())
                self.assertEqual(done.stderr.splitlines(), expected)
                self.assertFalse((self.root / "loaded").exists())
                self.reset_case()

    def test_sudo_failing_before_selftest_still_confirms_the_fan(self) -> None:
        done = self.invoke("install", SUDO_SELFTEST="1")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.calls.read_text().splitlines(), ["fancurve check", "fancurve auto", "fancurve auto"])
        self.assertEqual(done.stderr.splitlines()[-1], "write test failed; nothing was installed; macOS has the fan.")
        self.assert_build_removed()

    def test_signals_during_write_test_reach_cleanup(self) -> None:
        for sig, code in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGQUIT, 131), (signal.SIGTERM, 143)):
            with self.subTest(signal=sig):
                installer = subprocess.Popen(["/bin/sh", str(DEPLOY), "install"], cwd=self.root,
                                             env={**self.env, "SELFTEST_SLEEP": "1"},
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while not (self.root / "tested-before-replacement").exists():
                        self.assertIsNone(installer.poll(), "installer exited before the write test")
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    installer.send_signal(sig)
                    out, err = installer.communicate(timeout=8)
                finally:
                    if installer.poll() is None:
                        installer.kill()
                        installer.communicate()
                self.assertEqual(installer.returncode, code, out + err)
                self.assertEqual(self.calls.read_text().splitlines()[-2:], ["fancurve selftest", "fancurve auto"])
                self.assertEqual((self.root / "auto-binaries").read_text().splitlines()[-1], f"{self.dest}.new/fancurve")
                self.assertEqual(err.splitlines()[-1], "write test failed; nothing was installed; macOS has the fan.")
                self.assert_build_removed()
                self.reset_case()

    def test_cleanup_failure_preserves_status_and_report(self) -> None:
        self.stub("rm", '''case "$*" in
*fancurve-build.*) exit 1 ;;
*) exec /bin/rm "$@" ;;
esac''')
        done = self.invoke("install", SELFTEST="7")
        self.assertEqual(done.returncode, 7)
        self.assertEqual(done.stderr.splitlines()[-1], "write test failed; nothing was installed; macOS has the fan.")
        self.assertEqual(self.calls.read_text().splitlines()[-1], "fancurve auto")
        self.assertFalse(Path(f"{self.dest}.new").exists())

    def test_failed_replacement_restores_old_files_or_keeps_them_for_recovery(self) -> None:
        for restore_fails in (False, True):
            with self.subTest(restore_fails=restore_fails):
                self.old_install()
                done = self.invoke("install", REPLACE="1", RESTORE=str(int(restore_fails)))
                self.assertEqual(done.returncode, 1)
                old = Path(f"{self.dest}.old")
                if restore_fails:
                    self.assertFalse(self.dest.exists())
                    self.assertEqual((old / "fancurve").read_text(), "old")
                    self.assertIn(f"the previous files are at {old}", done.stderr)
                else:
                    self.assertFalse(old.exists())
                    self.assertEqual((self.dest / "fancurve").read_text(), "old")
                    self.assertEqual(done.stderr.splitlines()[-1], "could not replace the installed files; the previous ones were put back. The controller is NOT running; macOS had the fan when the write test ended.")
                self.assertFalse((self.root / "loaded").exists())
                self.assertEqual((self.jobs / f"{LABEL}.plist").read_text(), "job")
                self.assertNotIn("launchctl bootstrap", self.calls.read_text())
                self.assert_build_removed()
                self.reset_case()

    def test_stranded_old_files_are_restored_before_staging(self) -> None:
        self.old_install(loaded=False)
        self.dest.rename(f"{self.dest}.old")
        done = self.invoke("install", SELFTEST="1")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue((self.dest / "fancurve").is_file(), "the stranded previous binary must be restored")
        self.assertEqual((self.dest / "fancurve").read_text(), "old")
        self.assertFalse(Path(f"{self.dest}.old").exists())
        self.assertTrue((self.root / "tested-before-replacement").exists())
        trace = (self.root / "trace").read_text().splitlines()
        self.assertLess(trace.index("mv"), trace.index("fancurve selftest"))
        self.assert_build_removed()

    def test_first_install_failures_say_only_what_was_established(self) -> None:
        cases = [
            ({"AUTO": "1"}, "automatic fan mode could not be confirmed; nothing was installed."),
            ({"JOB_MOVE": "1"}, "the controller is NOT running; macOS had the fan when the write test ended. Fix the error above and install again."),
            ({"BOOTSTRAP": "1"}, "the new controller is not confirmed running: it may or may not have started. Run status."),
            ({"READING": "1"}, "fancurve is installed, but the status check found a problem (above)"),
        ]
        for faults, message in cases:
            with self.subTest(faults):
                done = self.invoke("install", **faults)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stderr.splitlines()[-1], message)
                self.assertNotIn("old controller", done.stderr)
                self.assertNotIn("previous controller", done.stderr)
                self.assert_build_removed()
                self.reset_case()

    def test_default_install_and_root_invocation(self) -> None:
        done = self.invoke(TEST_UID="0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.root / "privileged").read_text(), "")
        self.assertEqual(self.calls.read_text().splitlines(), ["fancurve check", "fancurve auto", "fancurve selftest", "launchctl bootstrap", "fancurve status"])
        self.assert_build_removed()

    def test_install_refuses_non_arm64_before_privileged_commands(self) -> None:
        done = self.invoke("install", ARM64="0")
        self.assertEqual((done.returncode, done.stderr), (1, "fancurve needs an Apple Silicon Mac\n"))
        self.assertEqual((self.root / "privileged").read_text(), "")
        self.assertEqual((self.root / "trace").read_text(), "")
        self.assertEqual(self.calls.read_text(), "")
        self.assertFalse(self.dest.exists())
        self.assert_build_removed()

    def test_install_accepts_arm64_even_when_uname_reports_rosetta(self) -> None:
        self.stub("uname", 'echo x86_64')
        done = self.invoke("install", ARM64="1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assert_installed_set()

    def test_install_refuses_missing_compiler_before_privileged_commands(self) -> None:
        done = self.invoke("install", NOTOOLS="2")
        self.assertEqual((done.returncode, done.stderr), (1, "install the Xcode Command Line Tools first: xcode-select --install\n"))
        self.assertEqual((self.root / "privileged").read_text(), "")
        self.assertEqual((self.root / "trace").read_text(), "")
        self.assertEqual(self.calls.read_text(), "")
        self.assertFalse(self.dest.exists())
        self.assert_build_removed()

    def test_build_or_plist_failure_is_unprivileged_and_cleans_up(self) -> None:
        for fault in ("COMPILE", "PLUTIL"):
            with self.subTest(fault):
                done = self.invoke("install", **{fault: "1"})
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual((self.root / "privileged").read_text(), "")
                self.assertEqual(self.calls.read_text(), "")
                self.assert_build_removed()
                self.reset_case()

    def test_uninstall_stops_the_job_then_confirms_the_release_then_deletes(self) -> None:
        # (job loaded, bootout works, binary present, auto exit) -> (removed, calls)
        cases = {
            "running": ((True, True, True, 0), True, ["launchctl bootout", "fancurve auto"]),
            "not loaded": ((False, True, True, 0), True, ["fancurve auto"]),
            "will not stop": ((True, False, True, 0), False, ["launchctl bootout"]),
            "release not confirmed": ((True, True, True, 1), False, ["launchctl bootout", "fancurve auto"]),
            "no binary to confirm with": ((False, True, False, 0), False, []),
            "files gone but still loaded": ((True, True, None, 0), False, ["launchctl bootout"]),
            "cannot ask launchd": ((5, True, True, 0), False, []),
        }
        for name, ((loaded, stops, binary, auto_exit), removed, calls) in cases.items():
            with self.subTest(name):
                self.old_install(binary=binary, loaded=loaded is True)
                if binary:
                    shutil.copy(self.root / "controller", self.dest / "fancurve")
                query = "0" if isinstance(loaded, bool) else str(loaded)
                done = self.invoke("uninstall", QUERY=query, STILL_LOADED=str(int(not stops)), AUTO=str(auto_exit))
                self.assertEqual(done.returncode == 0, removed, done.stderr)
                self.assertEqual(self.calls.read_text().splitlines(), calls)
                left = self.dest.exists() or (self.jobs / f"{LABEL}.plist").exists() or any(self.logs.iterdir())
                if removed:
                    self.assertFalse(left)
                    self.assertIn("macOS has the fan", done.stdout)
                else:
                    self.assertNotIn("macOS has the fan", done.stdout)
                    self.assertNotIn("nothing to remove", done.stdout)
                    if binary is not None:
                        self.assertTrue((self.jobs / f"{LABEL}.plist").exists())
                        for log in ("fancurve.log", "fancurve.log.1"):
                            self.assertTrue((self.logs / log).exists())
                self.reset_case()
        # Nothing installed and nothing loaded is the one case with nothing to do.
        done = self.invoke("uninstall")
        self.assertEqual((done.returncode, done.stdout), (0, "nothing to remove\n"))
        self.assertEqual(self.calls.read_text(), "")

    def test_uninstall_finds_leftovers_and_prefers_the_first_executable(self) -> None:
        for suffixes in ((".new",), (".old",), ("", ".new", ".old"), (".new", ".old")):
            with self.subTest(suffixes=suffixes):
                for suffix in suffixes:
                    folder = Path(f"{self.dest}{suffix}")
                    folder.mkdir()
                    shutil.copy(self.root / "controller", folder / "fancurve")
                done = self.invoke("uninstall")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(done.stdout, "uninstalled; macOS has the fan\n")
                self.assertEqual(self.calls.read_text().splitlines(), ["fancurve auto"])
                self.assertEqual((self.root / "auto-binaries").read_text().splitlines(), [f"{self.dest}{suffixes[0]}/fancurve"])
                self.assertTrue(all(not Path(f"{self.dest}{suffix}").exists() for suffix in ("", ".new", ".old")))
                self.reset_case()
        for leftover in (Path(f"{self.dest}.new"), Path(f"{self.dest}.old"),
                         self.jobs / f"{LABEL}.plist", self.logs / "fancurve.log", self.logs / "fancurve.log.1"):
            with self.subTest(leftover=leftover.name):
                leftover.touch()
                done = self.invoke("uninstall")
                self.assertEqual(done.returncode, 1)
                self.assertIn("no controller binary to confirm", done.stderr)
                self.assertNotIn("nothing to remove", done.stdout)
                self.assertTrue(leftover.exists())
                leftover.unlink()
                self.reset_case()

    def test_uninstall_signals_exit_with_the_signal_status(self) -> None:
        self.stub("launchctl", 'touch "$TEST_ROOT/querying"\n/bin/sleep 2\nexit 113')
        for sig, code in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGQUIT, 131), (signal.SIGTERM, 143)):
            with self.subTest(signal=sig):
                installer = subprocess.Popen(["/bin/sh", str(DEPLOY), "uninstall"], cwd=self.root,
                                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while not (self.root / "querying").exists():
                        self.assertIsNone(installer.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    installer.send_signal(sig)
                    out, err = installer.communicate(timeout=8)
                finally:
                    if installer.poll() is None:
                        installer.kill()
                        installer.communicate()
                self.assertEqual(installer.returncode, code, out + err)
                self.assertNotIn("nothing to remove", out)
                (self.root / "querying").unlink()

    def test_status_is_healthy_only_when_the_job_is_running_and_readable(self) -> None:
        cases = {"running": ("state = running", 0, 0), "waiting to restart": ("state = spawn scheduled", 0, 1),
                 "not loaded": (113, 0, 1), "cannot ask launchd": (5, 0, 1), "a reading failed": ("state = running", 1, 1)}
        for name, (job, reading, code) in cases.items():
            with self.subTest(name):
                self.dest.mkdir()
                shutil.copy(self.root / "controller", self.dest / "fancurve")
                (self.dest / "fancurve.conf").touch()
                (self.root / "loaded").touch()
                done = self.invoke("status", QUERY=str(job) if isinstance(job, int) else "0",
                                   JOB_STATE=job if isinstance(job, str) else "", READING=str(reading))
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                self.assertEqual("is not loaded" in done.stdout, job == 113)
                self.assertEqual("cannot tell whether" in done.stdout, job == 5)
                self.assertEqual(self.calls.read_text().splitlines(), [] if job == 5 else ["fancurve status"])
                self.reset_case()

    def test_scripts_parse(self) -> None:
        for script in (DEPLOY, DIR / "start.sh"):
            subprocess.run(["sh", "-n", str(script)], check=True)


if __name__ == "__main__":
    unittest.main()
