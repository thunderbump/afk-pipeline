import json
from pathlib import Path
import os
import signal
import subprocess
import sys
import time


scenario = sys.argv[1]

if scenario == "hang":
    marker = Path(sys.argv[2])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker.write_text(json.dumps({"process": os.getpid(), "descendant": child.pid}))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    while True:
        time.sleep(1)
else:
    raise SystemExit(f"unknown validation fixture scenario: {scenario}")
