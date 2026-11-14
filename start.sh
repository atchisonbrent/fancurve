#!/bin/sh
# Started by launchd as root (io.github.atchisonbrent.fancurve).
D=/usr/local/libexec/fancurve
L=/var/log/fancurve.log
# Nothing rotates the log, so each start keeps only the last part of a large
# one. It is emptied only once that part is safely copied.
if [ "$(stat -f %z "$L" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    if tail -c 262144 "$L" > "$L.1"; then : > "$L"; fi
fi
exec "$D/fancurve" run "$D/fancurve.conf"
