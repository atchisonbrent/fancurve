# fancurve

A small fan controller for Apple Silicon Macs with one fan, written on and for the Mac mini.

Left to macOS, a Mac mini holds its fan near minimum while the chip sits at about 100 °C under load. macOS has no setting for that. fancurve raises the fan earlier and gradually, up to a speed you choose, and leaves the fan to macOS the rest of the time.

## What it does

- Reads the hottest of a group of chip sensors every 2 seconds and acts on the **average of the last 30 seconds**, so a short spike does not move the fan.
- Below 80 °C, macOS keeps the fan. Nothing changes at the desktop.
- From 80 °C it holds the fan on a straight line from the fan's minimum to **2,400 rpm at 90 °C**, and the curve never asks for more. That ceiling is the limit on noise.
- It changes speed by at most 25 rpm a second, so the fan does not jump. (The one exception goes downward: a target it finds above the ceiling is brought down to it at once.)
- Below 74 °C it gives the fan back to macOS.
- Whenever it cannot do its job (a sensor reading fails, the fan's state cannot be read, a write is refused), it gives the fan back to macOS and says so in its log.

Every number above is a setting in [`fancurve.conf`](fancurve.conf).

The defaults favour quiet over cool. On an M6 Mac mini, which runs games at 97 to 105 °C with the fan at 1,000 rpm when left alone, they hold a light game at about 83 °C and 1,500 rpm. A heavy, sustained one still reaches about 100 °C with the fan at the 2,400 rpm ceiling. Raise `max_rpm` if you want lower temperatures and accept the noise: on that Mac the fan is clearly audible from about 2,100 rpm and loud at its 4,900 rpm maximum.

## Which Macs

| Mac | State |
|---|---|
| M6 Mac mini (`Mac18,5`), macOS 27.0 | In daily use. |
| M2 Mac mini (`Mac14,3`), macOS 27.0.1 | Reads work (`fancurve check`). Fan writes have not been tried. |
| Other Apple Silicon Macs with one fan, such as the M5 and M5 Pro Mac mini | Not tried. The installer finds out: see below. |
| Macs with two or more fans, Macs with none, Intel Macs | Not supported; `check` or the installer refuses them. |

It talks to the SMC, the chip that runs the fan, through keys Apple does not document. Two things differ between Macs. The installer tests the first before it installs anything; the second it can only show you:

- **Whether the Mac accepts fan writes.** The installer runs a write test: it holds the fan 300 rpm away from where it is for a few seconds, checks that the fan follows, and returns it to macOS. If that fails, nothing is installed. Some Macs guard fan writes behind a second key, `Ftst`, which fancurve does not use; `check` tells you if yours has it. (The source of the [Stats](https://github.com/exelban/stats) app says M1 to M4 Macs need it and M5 and later do not. The M2 above, on macOS 27, no longer has the key.)
- **Which sensors are chip temperatures.** Sensor names differ between chip families. `check`, which the installer runs and prints, lists what each configured group reads. On a Mac that is idle and has had time to cool, they should all be well under 80 °C. A group that still reads hot then is probably not a chip temperature on your Mac; see "Other Macs" below before taking it out, and also if no group matches. This judgment is yours: the installer does not stop for it.

If you try it on a Mac not listed, the output of `fancurve check` and of the install is worth sending in an issue, whether it worked or not.

## Install

You need the Xcode Command Line Tools (`xcode-select --install`), because it is built from source on your Mac.

```sh
git clone https://github.com/atchisonbrent/fancurve.git
cd fancurve
./install.sh
```

The installer builds the program, runs the read-only check, asks for your password, runs the write test, and installs. It refuses if `/usr/local`, `/usr/local/libexec` or the install directory is a symbolic link, is not owned by root, or is writable by group or others, because a root job under such a path could be swapped out. (It reads the permission bits, not access control lists.)

| Where | What |
|---|---|
| `/usr/local/libexec/fancurve/` | the program, its settings, and the script launchd starts |
| `/Library/LaunchDaemons/io.github.atchisonbrent.fancurve.plist` | the job: runs as root, starts at boot, restarted if it exits |
| `/var/log/fancurve.log` | when the fan is taken and returned, one line a minute while it is held, and anything that goes wrong |

It runs as root because fan writes need root. It is one C file of about 700 lines; read it before you trust it.

```sh
./install.sh status       # is it running, what is the fan doing, the last log lines
./install.sh uninstall    # stop it, confirm macOS has the fan, delete everything above
```

To change a setting, edit `fancurve.conf` in the checkout and run `./install.sh` again. To see what it would read on your Mac without installing anything:

```sh
cc -O2 fancurve.c -o fancurve -framework IOKit -framework CoreFoundation
./fancurve check fancurve.conf
```

## Settings

| Key | Default | Meaning |
|---|---|---|
| `sensors` | `TVD,TCM,TP,Tp,Tg,Te` | SMC key prefixes; the hottest matching sensor is the reading |
| `start` | `80` | average temperature (°C) at which fancurve takes the fan |
| `full` | `90` | temperature at which the line reaches `max_rpm` |
| `hysteresis` | `6` | the fan goes back to macOS below `start - hysteresis` |
| `max_rpm` | `2400` | the most the curve will ask for |
| `ramp` | `25` | the most the speed changes in a second, in rpm (a target above `max_rpm` comes down at once) |
| `interval` | `2` | seconds between readings |
| `window` | `30` | seconds of readings averaged |

All keys are required. Out-of-range values are refused by `check`, and so by the installer, before the running controller is touched.

The installer's write test moves the fan 300 rpm from wherever it is turning: up when that stays within `max_rpm`, otherwise down. Only when neither fits, which needs `max_rpm` set within 600 rpm of the fan's minimum, does it step up past `max_rpm`, for a few seconds.

### Other Macs: choosing sensors

The default prefixes are chip temperatures on the M6 and M2 Mac mini: regulators beside the chip (`TVD`, `TCM`), performance cores (`TP`, `Tp`), GPU (`Tg`) and efficiency cores (`Te`). On another chip family, run `./fancurve check fancurve.conf` with the Mac idle and cool, and again under load. Keep the groups that sit at 30 to 60 °C at idle and climb together under load; take a group out only after it has read hot on a Mac that has had time to cool. A prefix that is a core temperature on one family can be something else on another: `Tf` is the cores on M3 Macs according to Stats, and on the M2 it includes a sensor that reads 97 °C at idle.

If no default prefix matches, `./fancurve sensors` lists every temperature key on your Mac by its first two letters, with how many there are and what they read. Run it idle and under load to find the groups that behave like the chip, and put their prefixes in `sensors`.

## When something goes wrong

- A reading counts only if at least half the sensors that have ever answered still do. Three failed readings in a row return the fan to macOS; after 30 in a row (a minute at the default interval) the controller exits and launchd starts it afresh.
- Each pass it reads the fan's mode and target back and corrects what differs. A fan it finds held while the Mac is cool is released.
- A release counts only when the mode reads back as automatic. Until then it is retried every pass and the log says the release is not confirmed.
- If macOS is already running the fan faster than `max_rpm`, fancurve leaves it alone.
- To take the fan away from fancurve until the next restart: `sudo launchctl bootout system/io.github.atchisonbrent.fancurve`. It returns the fan to macOS as it stops. With the job stopped, `sudo /usr/local/libexec/fancurve/fancurve auto` returns the fan by hand and confirms it; while the job is running it would simply take the fan again.
- The write test and the controller return the fan when they are interrupted, hung up on or stopped. Nothing can do that for a process that is killed outright (`kill -9`): the controller is restarted by launchd about 30 seconds later and puts it right, and after a killed write test the `auto` command above, or the one the installer names in its last line, returns the fan.

## Limits

- The SMC keys are undocumented, so a macOS update can change them. Reads and writes insist on each key's type and size, so the likely result is that the controller refuses to start or leaves the fan with macOS. Nothing alerts you; run `./install.sh status` after a macOS update.
- While fancurve holds the fan, macOS's own fan control is not consulted. macOS still slows the chip if it overheats, but a load that would have made macOS spin the fan past `max_rpm` is held at `max_rpm`.
- It checks that the SMC accepted a target, not that the fan reached it. The once-a-minute log line shows both.
- A controller killed outright leaves the fan at its last speed until launchd restarts it, about 30 seconds later.
- The reading is the hottest sensor that answers. If the hottest few stop answering, the reading comes from the rest.
- One fan tool at a time: TG Pro, Macs Fan Control or Stats's fan control would fight it.

## Tests

```sh
python3 -m unittest discover -s tests
```

The control loop, the write test and the installer are tested without hardware, root or launchd: the program has a simulation mode with stand-in hardware, and the installer runs against stand-ins for `sudo` and `launchctl`. The code that talks to the SMC (finding the fan's mode key, enumerating sensors, `check`, `sensors`, `auto` and the write test) is compiled for the tests against a stand-in SMC. What no test reaches is a real one; that is what `check` and the write test are for.

## Credit

The SMC access and the idea of following the hottest of a group of sensors chosen by key prefix come from [HDZucht/Mac-mini-M6-fan-control-LLM](https://github.com/HDZucht/Mac-mini-M6-fan-control-LLM). The control loop, the write test and the installer are new. MIT license, see [`LICENSE`](LICENSE).
