"""
Runs a generated program and records the robot status its own vex client receives.

The robot's ws_status endpoint doesn't tolerate a second client well: a separate
harness connection made the program's client drop and reconnect every few seconds
("lost a status packet"), feeding the model's code stale/empty sensor data. So the
harness never connects itself during a run; instead this launcher hooks the vex
library's status thread and appends each status (≤5 Hz) to $BENCH_TELEMETRY_FILE.

Usage: python -u robot/launcher.py <program.py>
"""
import json
import os
import runpy
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
program = Path(sys.argv[1]).resolve()

# Behave as if the program were run directly: its own dir first on sys.path
# (not robot/, whose modules could shadow the program's imports).
sys.path[0] = str(program.parent)
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(1, str(BENCH_DIR))
sys.argv = [str(program)] + sys.argv[2:]

out_path = os.environ.get("BENCH_TELEMETRY_FILE")
if out_path:
    import vex.aim as _aim

    _out = open(out_path, "a", buffering=1)
    _last = [0.0]
    _orig = _aim.WSStatusThread.update_status_flags

    def _record(self, *args, **kwargs):
        now = time.time()
        if now - _last[0] >= 0.2:
            _last[0] = now
            try:
                _out.write(json.dumps({"ts": now, "status": self.current_status}) + "\n")
            except Exception:
                pass
        return _orig(self, *args, **kwargs)

    # Called once per valid status packet, from the library's status thread
    _aim.WSStatusThread.update_status_flags = _record

try:
    runpy.run_path(str(program), run_name="__main__")
except SystemExit:
    raise
except BaseException as exc:
    # Print the traceback as plain `python program.py` would: without this
    # launcher's and runpy's frames, which would only confuse the model reading it.
    import traceback
    tb = exc.__traceback__
    while tb is not None and Path(tb.tb_frame.f_code.co_filename).name in ("launcher.py", "runpy.py"):
        tb = tb.tb_next
    traceback.print_exception(type(exc), exc, tb)
    sys.exit(1)
