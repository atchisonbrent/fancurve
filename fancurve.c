// fancurve: a quiet fan curve for Apple Silicon Macs with one fan.
//
// macOS keeps the fan until the average of the hottest sensor over the last
// `window` seconds reaches `start`. From there the controller holds the fan in
// manual mode at a speed that rises in a straight line from the fan's minimum
// at `start` to `max_rpm` at `full`, and returns the fan to macOS once the
// average falls below `start - hysteresis`. It never asks for more than
// `max_rpm`, which is the ceiling on noise, and it changes speed by at most
// `ramp` rpm a second, so the fan never jumps.
//
// Every tick it reads the fan's mode and target back from the SMC and corrects
// whatever differs from what it wants, so a mode reset behind its back or a
// fan left held by a killed run is put right on the next tick. Whenever it
// cannot do its job (temperature readings failing, the fan's state unreadable,
// a write refused) it gives the fan back to macOS, whose own control is the
// baseline this Mac shipped with, and takes it again only after a fresh full
// window of readings. It leaves the fan alone when macOS is already running
// it faster than `max_rpm`.
//
//   fancurve check CONF      validate the settings and the hardware; changes nothing
//   fancurve status CONF     one reading of the fan and the sensors
//   fancurve run CONF        the controller (root)
//   fancurve auto            return the fan to macOS and confirm it (root)
//   fancurve selftest CONF   test fan writes, then return the fan to macOS (root)
//   fancurve simulate CONF   run the control loop against readings on stdin, no hardware
//   fancurve run-simulated CONF [held]   `run` itself with the hardware replaced by a stand-in (tests)
//   fancurve selftest-simulated CONF [WORD...]   test fan writes with stand-in hardware (tests)
//
// The SMC access and the idea of following the hottest of a set of sensors
// chosen by key prefix come from HDZucht/Mac-mini-M6-fan-control-LLM (MIT, see
// LICENSE); the control loop is ours.
#include <IOKit/IOKitLib.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <time.h>
#include <unistd.h>

// ---- settings ----
#define MAX_PREFIXES 16
typedef struct {
    char prefix[MAX_PREFIXES][5];
    int nprefix;
    double start, full, hysteresis, interval, window, max_rpm, ramp;
} Conf;

static int parse_number(const char *text, double *out) {
    char *end;
    double d = strtod(text, &end);
    if (end == text || *end || !isfinite(d)) return -1;
    *out = d;
    return 0;
}

static int parse_prefixes(char *list, Conf *c) {
    c->nprefix = 0;
    for (char *p = strtok(list, ","); p; p = strtok(NULL, ",")) {
        size_t n = strlen(p);
        if (c->nprefix == MAX_PREFIXES || n < 2 || n > 4 || p[0] != 'T') return -1;
        for (size_t i = 0; i < n; i++)
            if (!((p[i] >= 'A' && p[i] <= 'Z') || (p[i] >= 'a' && p[i] <= 'z') || (p[i] >= '0' && p[i] <= '9'))) return -1;
        strcpy(c->prefix[c->nprefix++], p);
    }
    return c->nprefix ? 0 : -1;
}

// Every key must be present, numeric and in range: a typing mistake here must
// stop the deploy, not produce a curve that never raises the fan.
static int load_conf(const char *path, Conf *c) {
    FILE *f = fopen(path, "r");
    if (!f) { fprintf(stderr, "cannot read %s\n", path); return -1; }
    c->nprefix = 0;
    c->start = c->full = c->hysteresis = c->interval = c->window = c->max_rpm = c->ramp = NAN;
    char line[300];
    int bad = 0;
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!line[0] || line[0] == '#') continue;
        char *value = strchr(line, '=');
        double *number = NULL;
        if (value) {
            *value++ = 0;
            number = !strcmp(line, "start") ? &c->start : !strcmp(line, "full") ? &c->full
                   : !strcmp(line, "hysteresis") ? &c->hysteresis : !strcmp(line, "interval") ? &c->interval
                   : !strcmp(line, "window") ? &c->window : !strcmp(line, "max_rpm") ? &c->max_rpm
                   : !strcmp(line, "ramp") ? &c->ramp : NULL;
        }
        if (value && !strcmp(line, "sensors") ? parse_prefixes(value, c) : (!number || parse_number(value, number))) {
            fprintf(stderr, "%s: cannot use the line starting '%s'\n", path, line);
            bad = 1;
        }
    }
    fclose(f);
    if (bad) return -1;
    // A comparison with a missing value (NaN) is false, so a missing key fails here.
    if (!(c->nprefix >= 1 && c->start >= 40 && c->full <= 110 && c->full - c->start >= 5 &&
          c->hysteresis >= 1 && c->start - c->hysteresis >= 30 &&
          c->interval >= 1 && c->interval <= 10 && c->window >= c->interval && c->window <= 120 &&
          c->max_rpm >= 1500 && c->max_rpm <= 20000 && c->ramp >= 10 && c->ramp <= 1000)) {
        fprintf(stderr, "%s: needs sensors, 40 <= start, start + 5 <= full <= 110, 1 <= hysteresis <= start - 30, "
                        "1 <= interval <= 10, interval <= window <= 120, 1500 <= max_rpm <= 20000 and 10 <= ramp <= 1000\n", path);
        return -1;
    }
    return 0;
}

// ---- SMC (AppleSMC user client, selector 2) ----
typedef struct { char major, minor, build, reserved[1]; UInt16 release; } Vers;
typedef struct { UInt16 version, length; UInt32 cpu, gpu, mem; } PLimit;
typedef struct { UInt32 dataSize, dataType; char dataAttributes; } KeyInfo;
typedef struct { UInt32 key; Vers vers; PLimit pl; KeyInfo ki; char result, status, data8; UInt32 data32; unsigned char bytes[32]; } SMCData;
enum { SMC_READ = 5, SMC_WRITE = 6, SMC_KEY_AT_INDEX = 8, SMC_KEY_INFO = 9 };

static io_connect_t con;
static UInt32 k2u(const char *s) { return ((UInt32)s[0] << 24) | ((UInt32)s[1] << 16) | ((UInt32)s[2] << 8) | (UInt32)s[3]; }
static void u2k(UInt32 u, char *s) { s[0] = (char)(u >> 24); s[1] = (char)(u >> 16); s[2] = (char)(u >> 8); s[3] = (char)u; s[4] = 0; }
static int smc_call(SMCData *in, SMCData *out) {
    size_t n = sizeof(SMCData);
    return IOConnectCallStructMethod(con, 2, in, sizeof(SMCData), out, &n) != KERN_SUCCESS || n != sizeof(SMCData) || out->result;
}
static int smc_open(void) {
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
    return !service || IOServiceOpen(service, mach_task_self(), 0, &con) != KERN_SUCCESS;
}
// Reads and writes insist on the key's type and size, so a key that changes
// meaning in a later macOS is refused instead of misread.
static int smc_is(const char *key, const char *type, UInt32 size) {
    SMCData in = {0}, out = {0};
    in.key = k2u(key); in.data8 = SMC_KEY_INFO;
    return !smc_call(&in, &out) && out.ki.dataType == k2u(type) && out.ki.dataSize == size;
}
static const char *mode_key;
static int detect_mode_key(void) {
    if (smc_is("F0md", "ui8 ", 1)) mode_key = "F0md";
    else if (smc_is("F0Md", "ui8 ", 1)) mode_key = "F0Md";
    else { fprintf(stderr, "the fan's mode key cannot be found\n"); return -1; }
    return 0;
}
static int smc_read(const char *key, const char *type, UInt32 size, unsigned char *bytes) {
    if (!smc_is(key, type, size)) return -1;
    SMCData in = {0}, out = {0};
    in.key = k2u(key); in.ki.dataSize = size; in.data8 = SMC_READ;
    if (smc_call(&in, &out)) return -1;
    memcpy(bytes, out.bytes, size);
    return 0;
}
static int smc_write(const char *key, const char *type, UInt32 size, const void *bytes) {
    if (!smc_is(key, type, size)) return -1;
    SMCData in = {0}, out = {0};
    in.key = k2u(key); in.ki.dataSize = size; in.data8 = SMC_WRITE;
    memcpy(in.bytes, bytes, size);
    return smc_call(&in, &out) ? -1 : 0;
}
static int smc_float(const char *key, double *value) {
    unsigned char b[4]; float f;
    if (smc_read(key, "flt ", 4, b)) return -1;
    memcpy(&f, b, 4);
    if (!isfinite(f)) return -1;
    *value = f;
    return 0;
}
static int smc_byte(const char *key, int *value) {
    unsigned char b[1];
    if (smc_read(key, "ui8 ", 1, b)) return -1;
    *value = b[0];
    return 0;
}

// ---- the hardware as the control loop sees it; `simulate` swaps in a stand-in ----
#define MAX_SENSORS 256
static char sensor[MAX_SENSORS][5];
static int sensor_prefix[MAX_SENSORS];   // the first configured prefix each sensor matched
static int nsensors;
static int matched[MAX_PREFIXES];
static double fan_min, fan_max;
static double ceiling;       // min(max_rpm, the fan's own maximum)

static int sim;
static struct {
    double temp, target, actual;
    int plausible, mode, temps_fail, mode_unreadable, target_unreadable, actual_unreadable, mode_refused, target_refused, mode_stuck;
    int mode_reset, target_changed, never_follows, falls_short, release_stuck, interrupt;
} S;

// The hottest plausible reading, and how many sensors gave one.
static int hw_temps(double *hottest, char *which, int *plausible) {
    int failed = 0;
    *plausible = 0; *hottest = -1;
    if (sim) {
        if (S.temps_fail) return -1;
        *hottest = S.temp; *plausible = S.plausible; strcpy(which, "sim");
    } else for (int i = 0; i < nsensors; i++) {
        double v;
        if (smc_float(sensor[i], &v)) { failed++; continue; }
        if (v < 5 || v > 130) continue;
        (*plausible)++;
        if (v > *hottest) { *hottest = v; strcpy(which, sensor[i]); }
    }
    // Sensors that answer with nothing plausible are no reading at all.
    return (!sim && failed == nsensors) || !*plausible ? -1 : 0;
}
static int hw_mode(int *mode) {
    if (sim) { if (S.mode_unreadable) return -1; *mode = S.mode; return 0; }
    return smc_byte(mode_key, mode);
}
static int hw_target(double *rpm) {
    if (sim) { if (S.target_unreadable) return -1; *rpm = S.target; return 0; }
    return smc_float("F0Tg", rpm);
}
static int hw_actual(double *rpm) {
    if (sim) { if (S.actual_unreadable) return -1; *rpm = S.actual; return 0; }
    return smc_float("F0Ac", rpm);
}
static int hw_set_mode(int mode) {
    if (sim) { if (S.mode_refused) return -1; if (!S.mode_stuck) S.mode = mode; return 0; }
    unsigned char b = (unsigned char)mode;
    return smc_write(mode_key, "ui8 ", 1, &b);
}
static int hw_set_target(double rpm) {
    if (sim) { if (S.target_refused) return -1; S.target = rpm; return 0; }
    float f = (float)rpm;
    return smc_write("F0Tg", "flt ", 4, &f);
}
// Returns the fan to macOS and confirms it by reading the mode back: an
// accepted write is not proof.
static int hw_release_once(void) {
    int mode;
    return hw_set_mode(0) || hw_mode(&mode) || mode != 0 ? -1 : 0;
}
static int hw_release(void) {
    for (int attempt = 0; attempt < 3; attempt++) {
        if (!hw_release_once()) return 0;
        if (!sim) usleep(200000);
    }
    return -1;
}

// One fan with sane limits and a readable mode, and at least one sensor
// matching the configured prefixes. Unmatched prefixes are reported separately.
static int hw_init(const Conf *c) {
    int fans, mode;
    if (smc_byte("FNum", &fans) || fans != 1) { fprintf(stderr, "expected exactly one fan\n"); return -1; }
    if (smc_float("F0Mn", &fan_min) || smc_float("F0Mx", &fan_max) || !(fan_min > 0 && fan_max > fan_min && fan_max <= 20000)) {
        fprintf(stderr, "the fan's speed limits cannot be read or make no sense\n"); return -1;
    }
    if (hw_mode(&mode) || (mode != 0 && mode != 1)) { fprintf(stderr, "the fan's mode cannot be read\n"); return -1; }
    ceiling = c->max_rpm < fan_max ? c->max_rpm : fan_max;
    if (ceiling < fan_min + 100) { fprintf(stderr, "max_rpm leaves no room above the fan's minimum of %.0f\n", fan_min); return -1; }
    unsigned char b[4];
    if (smc_read("#KEY", "ui32", 4, b)) { fprintf(stderr, "the SMC key count cannot be read\n"); return -1; }
    UInt32 keys = ((UInt32)b[0] << 24) | ((UInt32)b[1] << 16) | ((UInt32)b[2] << 8) | b[3];
    if (keys < 1 || keys > 20000) { fprintf(stderr, "implausible SMC key count %u\n", keys); return -1; }
    memset(matched, 0, sizeof matched);
    nsensors = 0;
    for (UInt32 i = 0; i < keys; i++) {
        SMCData in = {0}, out = {0};
        in.data8 = SMC_KEY_AT_INDEX; in.data32 = i;
        if (smc_call(&in, &out)) continue;
        char k[5];
        u2k(out.key, k);
        int added = 0;
        for (int p = 0; p < c->nprefix; p++) {
            if (strncmp(k, c->prefix[p], strlen(c->prefix[p])) || !smc_is(k, "flt ", 4)) continue;
            matched[p] = 1;
            if (!added && nsensors < MAX_SENSORS) { sensor_prefix[nsensors] = p; strcpy(sensor[nsensors++], k); }
            added = 1;
        }
    }
    if (!nsensors) { fprintf(stderr, "no temperature sensor matches any configured prefix\n"); return -1; }
    return 0;
}

// ---- the control loop ----
#define RISE_RPM 50          // a new target is written when the line is at least this far above the fan's target
#define FALL_RPM 150         // ... or this far below it, so the fan does not hunt
#define FAULT_TICKS 3        // consecutive failed readings before the fan goes back to macOS
#define GIVE_UP_TICKS 30     // ... and before the controller exits, so that launchd starts it afresh
#define SAME_NOTE_TICKS 30   // a repeating complaint is logged once in this many ticks
#define MAX_WINDOW 128

typedef struct {
    double window[MAX_WINDOW];
    int size, next, filled;
    int active;              // the average says the fan should be ours
    int held;                // we wrote manual mode and a target, and nothing has contradicted it since
    double floor;            // never slower than macOS was running the fan when we took it
    int failing, most_plausible;
    long ticks, status_every, complained_at;
    const char *complaint;
} Loop;

static void note(const char *format, ...) {
    va_list args;
    if (sim) printf("| ");
    else { time_t now = time(NULL); char stamp[32]; strftime(stamp, sizeof stamp, "%Y-%m-%d %H:%M:%S", localtime(&now)); printf("%s ", stamp); }
    va_start(args, format); vprintf(format, args); va_end(args);
    printf("\n");
    fflush(stdout);
}
static void complain(Loop *l, const char *text) {
    if (l->complaint == text && l->ticks - l->complained_at < SAME_NOTE_TICKS) return;
    l->complaint = text; l->complained_at = l->ticks;
    note("%s", text);
}

// Gives the fan to macOS and starts the window again, so that nothing is tried
// until a full window of fresh readings says it is still needed.
static void back_off(Loop *l, const char *released, const char *not_released) {
    complain(l, hw_release_once() ? not_released : released);
    l->active = l->held = 0;
    l->floor = 0;
    l->filled = l->next = 0;
}

// One pass. Returns 1 when the controller should exit.
static int tick(Loop *l, const Conf *c) {
    l->ticks++;
    double now = -1; char which[5] = "-"; int plausible = 0;
    // A reading counts only if at least half the sensors that have answered
    // at once still do: the hottest one dropping out must not read as cooling.
    if (!hw_temps(&now, which, &plausible) && plausible * 2 >= l->most_plausible) {
        if (plausible > l->most_plausible) l->most_plausible = plausible;
        l->window[l->next] = now; l->next = (l->next + 1) % l->size;
        if (l->filled < l->size) l->filled++;
        l->failing = 0;
    } else l->failing++;
    double average = 0;
    for (int i = 0; i < l->filled; i++) average += l->window[i];
    if (l->filled) average /= l->filled;

    if (l->failing >= FAULT_TICKS || !l->filled) {
        if (l->failing == FAULT_TICKS) {
            note("temperature readings are failing; the fan is left to macOS");
            l->filled = l->next = 0;   // readings from before the gap are not the last `window` seconds
        }
        l->active = 0;
    } else if (!l->active && l->filled == l->size && average >= c->start) l->active = 1;   // only on a full window
    else if (l->active && average < c->start - c->hysteresis) l->active = 0;
    int give_up = l->failing >= GIVE_UP_TICKS;

    int mode = -1, mode_known = !hw_mode(&mode);
    if (!l->active) {
        if (mode_known && mode == 0) l->held = 0;
        else if (hw_release_once()) complain(l, "the fan is NOT confirmed back with macOS; trying again each pass");
        else {
            if (!l->held) note("found the fan held in manual mode; returned it to macOS");
            else if (l->failing) note("fan returned to macOS");
            else note("%.1f °C: fan back to macOS", average);
            l->held = 0;
        }
        l->floor = 0;
        return give_up;
    }

    // Driving the fan needs its mode and its target read back, and taking it
    // from macOS also needs the speed it is turning at. Without them the fan
    // goes to macOS: it is never held blind.
    double at = 0, target;
    int taking = mode_known && mode != 1;
    if (!mode_known || hw_target(&target) || (taking && hw_actual(&at))) {
        back_off(l, "the fan's state cannot be read; it is left to macOS",
                 "the fan's state cannot be read, and it is NOT confirmed back with macOS");
        return give_up;
    }
    if (!taking) at = target;
    if (taking && at > ceiling) {
        // macOS is already doing more than this controller is allowed to.
        complain(l, "macOS is running the fan above max_rpm; it keeps the fan");
        return give_up;
    }
    // A held target outside what this controller would ever write is rewritten whatever the dead band says.
    int off = !taking && (at < fan_min || at > ceiling);
    if (at < fan_min) at = fan_min;
    if (at > fan_max) at = fan_max;
    if (taking) l->floor = at;   // never slower than macOS was running it when we took it, this time

    double x = (average - c->start) / (c->full - c->start);
    if (x < 0) x = 0;
    if (x > 1) x = 1;
    double rpm = fan_min + (ceiling - fan_min) * x, step = c->ramp * c->interval;
    if (rpm < l->floor) rpm = l->floor;
    if (taking || off || rpm - at >= RISE_RPM || at - rpm >= FALL_RPM) {
        if (rpm > at + step) rpm = at + step;
        if (rpm < at - step) rpm = at - step;
        if (rpm > ceiling) rpm = ceiling;   // the ceiling outranks the ramp: down to it at once
        if (hw_set_mode(1) || hw_set_target(rpm)) {
            // Manual mode with a stale target is the one state that must not last.
            back_off(l, "cannot set the fan (write refused); it is left to macOS",
                     "cannot set the fan (write refused), and it is NOT confirmed back with macOS");
            return give_up;
        }
        if (!l->held) note("%.1f °C over the last %g s (%.1f now at %s): fan taken, %.0f rpm", average, c->window, now, which, rpm);
        else if (taking) complain(l, "the fan's mode had been reset; taken again");
        l->held = 1;
    }
    if (l->ticks % l->status_every == 0) {
        double actual = -1, target = -1;
        hw_actual(&actual); hw_target(&target);
        note("%.1f °C average, %.1f now at %s, fan %.0f rpm (target %.0f)", average, now, which, actual, target);
    }
    return give_up;
}

static void loop_init(Loop *l, const Conf *c) {
    memset(l, 0, sizeof *l);
    l->size = (int)ceil(c->window / c->interval);
    if (l->size > MAX_WINDOW) l->size = MAX_WINDOW;
    l->status_every = (long)ceil(60 / c->interval);
}

static volatile sig_atomic_t stopping;
static void on_signal(int s) { (void)s; stopping = 1; }
static void catch_signals(void) { signal(SIGTERM, on_signal); signal(SIGINT, on_signal); }

static int run(const Conf *c) {
    Loop l;
    loop_init(&l, c);
    note("started: %d sensors, macOS below %g °C, %.0f rpm at %g °C to %.0f rpm at %g °C, average over %g s, %g rpm/s",
         nsensors, c->start, fan_min, c->start, ceiling, c->full, c->window, c->ramp);
    while (!stopping) {
        if (tick(&l, c)) {
            note(hw_release() ? "no usable temperature reading for %d passes and the fan could NOT be returned to macOS; exiting to start afresh"
                              : "no usable temperature reading for %d passes; the fan is with macOS; exiting to start afresh", GIVE_UP_TICKS);
            return 1;
        }
        for (int i = 0; i < c->interval * 10 && !stopping; i++) usleep(100000);
    }
    int failed = hw_release();
    note(failed ? "stopping: could NOT return the fan to macOS" : "stopping: fan returned to macOS");
    return failed ? 1 : 0;
}

// The stand-in hardware: a cool Mac with ten sensors and the fan with macOS.
static void sim_begin(void) {
    sim = 1; fan_min = 1000; fan_max = 4900;
    S.temp = 50; S.plausible = 10; S.mode = 0; S.target = S.actual = 1000;
}
static void sim_limits(const Conf *c) { ceiling = c->max_rpm < fan_max ? c->max_rpm : fan_max; }

// The initial reads deliberately precede every write: another controller's
// held fan, or a fan whose starting state is unknown, must be left alone.
static int selftest(void) {
    int mode;
    double before, measured, target;
    if (hw_mode(&mode) || (mode != 0 && mode != 1)) {
        puts("the fan's mode cannot be read"); return 1;
    }
    if (mode == 1) {
        puts("the fan is already held in manual mode (another fan tool or a running fancurve); stop it first"); return 1;
    }
    if (hw_actual(&before)) { puts("the fan's measured speed cannot be read"); return 1; }
    // A 300 rpm step the fan can be seen to make: up if that stays within
    // max_rpm, otherwise down, and up past max_rpm only when neither fits.
    double base = fmin(fmax(before, fan_min), fan_max);
    target = base + 300 <= ceiling ? base + 300 : base - 300 >= fan_min ? base - 300 : fmin(base + 300, fan_max);
    if (fabs(target - base) < 200) { puts("the fan's speed range leaves no room for a write test"); return 1; }
    measured = before;
    int near = 0;   // consecutive readings within 100 rpm of the target
    const char *reason = "the SMC accepted the target but the fan did not follow it";
    int passed = 0;
    catch_signals();
    if (stopping) reason = "interrupted";
    else if (hw_set_mode(1) || hw_set_target(target)) reason = "this Mac refused the fan write";
    else for (int second = 1; second <= 10; second++) {
        if (!sim && !stopping) sleep(1);
        if (sim) {
            if (S.mode_reset) S.mode = 0;
            if (S.target_changed) S.target = target + 2;
            if (S.mode == 1 && !S.never_follows) S.actual = S.falls_short ? (S.target + before) / 2 : S.target;
            if (S.interrupt) raise(SIGTERM);
        }
        if (stopping) { reason = "interrupted"; break; }
        double readback;
        if (hw_mode(&mode) || mode != 1 || hw_target(&readback) || fabs(readback - target) > 1) {
            reason = "the fan did not stay held: macOS took it back or the target changed"; break;
        }
        if (hw_actual(&measured)) { reason = "the fan's measured speed cannot be read"; break; }
        // Settled at the target, not merely passing through it.
        near = fabs(measured - target) <= 100 ? near + 1 : 0;
        if (second >= 3 && near >= 2) { passed = 1; break; }
    }
    if (stopping) { passed = 0; reason = "interrupted"; }
    // In this test, mode-stuck models a release that is accepted but ignored.
    if (sim && S.release_stuck) S.mode_stuck = 1;
    int failed_release = hw_release();
    if (!passed) puts(reason);
    if (failed_release) { puts("could NOT return the fan to macOS"); return 1; }
    if (!passed) { puts("fan returned to macOS"); return 1; }
    printf("write test passed: fan held at %.0f rpm, measured %.0f rpm (was %.0f), and returned to macOS\n",
           target, measured, before);
    return 0;
}

// Fault words persist for the whole write test. The existing simulation's
// per-tick faults and its fan-following behavior are unchanged.
static int selftest_simulated(const char *path, int nwords, char **words) {
    Conf c;
    sim_begin();
    int result = 1;
    for (int i = 0; i < nwords; i++) {
        const char *w = words[i];
        if (!strcmp(w, "held")) S.mode = 1;
        else if (!strcmp(w, "mode-unreadable")) S.mode_unreadable = 1;
        else if (!strcmp(w, "target-unreadable")) S.target_unreadable = 1;
        else if (!strcmp(w, "actual-unreadable")) S.actual_unreadable = 1;
        else if (!strcmp(w, "mode-refused")) S.mode_refused = 1;
        else if (!strcmp(w, "target-refused")) S.target_refused = 1;
        else if (!strcmp(w, "mode-reset")) S.mode_reset = 1;
        else if (!strcmp(w, "target-changed")) S.target_changed = 1;
        else if (!strcmp(w, "never-follows")) S.never_follows = 1;
        else if (!strcmp(w, "falls-short")) S.falls_short = 1;
        else if (!strcmp(w, "mode-stuck")) S.release_stuck = 1;
        else if (!strcmp(w, "interrupt")) S.interrupt = 1;
        else if (!strncmp(w, "actual=", 7) && !parse_number(w + 7, &S.actual)) { }
        else { printf("unknown word: %s\n", w); goto done; }
    }
    if (load_conf(path, &c)) puts("cannot load settings");
    else { sim_limits(&c); result = selftest(); }
done:
    printf("mode=%d\n", S.mode);
    return result;
}

// Each stdin line is one tick. Words: t=<°C> or t=fail (the reading), p=<n>
// (sensors answering), mode=<0|1> and target=<rpm> (something else changed the
// fan), actual=<rpm>, and for this tick only: mode-unreadable,
// target-unreadable, actual-unreadable, mode-refused, target-refused, and
// mode-stuck (the mode write is accepted and does nothing). `mode-stuck` on
// the last line also applies to the release at the end. Prints what the loop
// logged and the state after each tick.
static int simulate(const Conf *c) {
    Loop l;
    loop_init(&l, c);
    sim_begin();
    sim_limits(c);
    char line[300];
    while (fgets(line, sizeof line, stdin)) {
        S.temps_fail = S.mode_unreadable = S.target_unreadable = S.actual_unreadable = S.mode_refused = S.target_refused = S.mode_stuck = 0;
        for (char *w = strtok(line, " \t\r\n"); w; w = strtok(NULL, " \t\r\n")) {
            if (!strcmp(w, "t=fail")) S.temps_fail = 1;
            else if (!strncmp(w, "t=", 2)) S.temp = atof(w + 2);
            else if (!strncmp(w, "p=", 2)) S.plausible = atoi(w + 2);
            else if (!strncmp(w, "mode=", 5)) S.mode = atoi(w + 5);
            else if (!strncmp(w, "target=", 7)) S.target = atof(w + 7);
            else if (!strncmp(w, "actual=", 7)) S.actual = atof(w + 7);
            else if (!strcmp(w, "mode-unreadable")) S.mode_unreadable = 1;
            else if (!strcmp(w, "target-unreadable")) S.target_unreadable = 1;
            else if (!strcmp(w, "actual-unreadable")) S.actual_unreadable = 1;
            else if (!strcmp(w, "mode-stuck")) S.mode_stuck = 1;
            else if (!strcmp(w, "mode-refused")) S.mode_refused = 1;
            else if (!strcmp(w, "target-refused")) S.target_refused = 1;
            else { fprintf(stderr, "unknown word: %s\n", w); return 2; }
        }
        int give_up = tick(&l, c);
        if (S.mode == 1) S.actual = S.target;   // a held fan follows its target
        printf("mode=%d target=%.0f active=%d held=%d%s\n", S.mode, S.target, l.active, l.held, give_up ? " give-up" : "");
        if (give_up) return 1;
    }
    S.mode_unreadable = S.mode_refused = 0;
    printf(hw_release() ? "stop: NOT released\n" : "stop: released\n");
    return 0;
}

// The controller as launchd starts it. `simulated` replaces the hardware with
// the stand-in (a hot Mac, the fan with macOS or, with `found_held`, left held
// at full speed by an earlier run) and changes nothing else.
static int serve(const char *conf_path, int simulated, int found_held) {
    Conf c;
    catch_signals();
    if (simulated) {
        sim_begin();
        S.temp = 120;
        if (found_held) { S.mode = 1; S.target = S.actual = fan_max; }
    } else if (smc_open()) { fprintf(stderr, "cannot open the SMC\n"); return 1; }
    if (!simulated && detect_mode_key()) return 1;
    // Before anything that can fail: a fan left held by a killed run must not
    // wait on the settings or on the sensors being found.
    if (hw_release()) note("starting: could NOT return the fan to macOS");
    int result = load_conf(conf_path, &c) ? 2 : simulated ? (sim_limits(&c), 0) : hw_init(&c) ? 1 : 0;
    if (!result) result = run(&c);
    if (simulated) printf("mode=%d\n", S.mode);
    return result;
}

static int status(const Conf *c) {
    double actual = -1, target = -1, hottest = -1; int mode = -1, plausible = 0; char which[5] = "-";
    int unreadable = hw_actual(&actual) | hw_target(&target) | hw_mode(&mode) | hw_temps(&hottest, which, &plausible);
    if (mode != 0 && mode != 1) unreadable = 1;
    printf("fan %.0f rpm (target %.0f, range %.0f to %.0f), %s\n", actual, target, fan_min, fan_max,
           mode == 0 ? "macOS in control" : mode == 1 ? "held by a fan controller" : "mode unreadable");
    printf("hottest of %d sensors (%d answering): %s %.1f °C\n", nsensors, plausible, which, hottest);
    printf("curve: macOS below %g °C, %.0f rpm at %g °C and above, back to macOS below %g °C, average over %g s\n",
           c->start, ceiling, c->full, c->start - c->hysteresis, c->window);
    int missing = 0;
    for (int p = 0; p < c->nprefix; p++) {
        if (!matched[p]) {
            printf("%s%s", missing++ ? ", " : "no sensor on this Mac starts with: ", c->prefix[p]);
        }
    }
    if (missing) putchar('\n');
    return unreadable ? 1 : 0;
}

// What each configured prefix contributes right now. Sensor names differ between
// chip families, and a prefix can match sensors that are not chip temperatures
// on a given Mac; this is where that shows.
static void describe_prefixes(const Conf *c) {
    double hottest = -1;
    for (int p = 0; p < c->nprefix; p++) {
        int n = 0, plausible = 0; double low = 0, high = 0; char which[5] = "-";
        for (int i = 0; i < nsensors; i++) {
            double v;
            if (sensor_prefix[i] != p) continue;
            n++;
            if (smc_float(sensor[i], &v) || v < 5 || v > 130) continue;
            if (!plausible || v < low) low = v;
            if (!plausible || v > high) { high = v; strcpy(which, sensor[i]); }
            plausible++;
        }
        if (!n) continue;
        if (plausible && high > hottest) hottest = high;
        if (plausible) printf("  %-4s %3d sensors, %d answering: %.1f to %.1f °C (hottest %s)\n", c->prefix[p], n, plausible, low, high, which);
        else printf("  %-4s %3d sensors, none answering with a plausible temperature\n", c->prefix[p], n);
    }
    if (hottest >= c->start)
        printf("the reading is already at or above start (%g °C): if this Mac is idle, a prefix above matches sensors that are not chip temperatures; take it out of `sensors`\n", c->start);
}

static void identify_mac(void) {
    char model[128] = "unknown model", version[128] = "unknown version";
    size_t size = sizeof model;
    if (sysctlbyname("hw.model", model, &size, NULL, 0)) strcpy(model, "unknown model");
    size = sizeof version;
    if (sysctlbyname("kern.osproductversion", version, &size, NULL, 0)) strcpy(version, "unknown version");
    printf("%s, macOS %s, fan mode key %s\n", model, version, mode_key);
    SMCData in = {0}, out = {0};
    in.key = k2u("Ftst"); in.data8 = SMC_KEY_INFO;
    if (!smc_call(&in, &out))
        puts("this Mac has the older fan-unlock key (Ftst); fan writes may be refused, and fancurve does not use that key");
}

int main(int argc, char **argv) {
    const char *command = argc > 1 ? argv[1] : "";
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (!strcmp(command, "auto") && argc == 2) {
        if (geteuid() != 0) { fprintf(stderr, "auto writes to the SMC and needs root\n"); return 2; }
        // Nothing but the mode key: this is the tool of last resort for a held fan.
        if (smc_open()) { fprintf(stderr, "cannot open the SMC\n"); return 1; }
        if (detect_mode_key()) return 1;
        int failed = hw_release();
        printf(failed ? "could NOT return the fan to macOS\n" : "fan returned to macOS\n");
        return failed ? 1 : 0;
    }
    if (!strcmp(command, "run-simulated") && (argc == 3 || (argc == 4 && !strcmp(argv[3], "held"))))
        return serve(argv[2], 1, argc == 4);
    if (!strcmp(command, "selftest-simulated") && argc >= 3)
        return selftest_simulated(argv[2], argc - 3, argv + 3);
    Conf c;
    if (argc != 3 || (strcmp(command, "check") && strcmp(command, "status") && strcmp(command, "run") && strcmp(command, "simulate") && strcmp(command, "selftest"))) {
        fprintf(stderr, "usage: fancurve check|status|run|selftest|simulate CONF, fancurve auto, fancurve run-simulated CONF [held], or fancurve selftest-simulated CONF [WORD...]\n");
        return 2;
    }
    if (!strcmp(command, "run")) {
        if (geteuid() != 0) { fprintf(stderr, "run writes to the SMC and needs root\n"); return 2; }
        return serve(argv[2], 0, 0);
    }
    int testing = !strcmp(command, "selftest");
    if (testing && geteuid() != 0) { puts("selftest writes to the SMC and needs root"); return 1; }
    if (load_conf(argv[2], &c)) {
        if (testing) puts("cannot load settings");
        return testing ? 1 : 2;
    }
    if (!strcmp(command, "simulate")) return simulate(&c);
    if (smc_open()) {
        if (testing) puts("cannot open the SMC");
        else fprintf(stderr, "cannot open the SMC\n");
        return 1;
    }
    if (detect_mode_key()) {
        if (testing) puts("the fan's mode key cannot be found");
        return 1;
    }
    if (!strcmp(command, "check")) identify_mac();
    if (hw_init(&c)) {
        if (testing) puts("cannot initialise the fan");
        return 1;
    }
    if (testing) return selftest();
    int result = status(&c);
    if (!strcmp(command, "check")) {
        describe_prefixes(&c);
        printf(result ? "check FAILED: a reading above could not be taken\n" : "check passed\n");
    }
    return result;
}
