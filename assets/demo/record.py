"""Record a real terminal session as an asciicast v2 file, without a pty (asciinema has no Windows build).

    uv run python assets/demo/record.py assets/demo/demo.cast

The script prints a prompt and "types" each command (the only synthetic part: the typing pace and the
comment lines), then runs the command through a subprocess and writes every chunk of its stdout/stderr as
an `[t, "o", data]` event with the real wall-clock offset at which it arrived. Newlines are converted to
CRLF because there is no pty to do it. Nothing else touches the output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLS, ROWS = 110, 30
AMBER, GREEN, RESET = "\x1b[1;33m", "\x1b[1;32m", "\x1b[0m"

# (kind, text, pause after). "note" prints a comment; "run" types and executes the command in ROOT.
SCRIPT: list[tuple[str, str, float]] = [
    ("note", "llmfailover: a breaker trips, traffic reroutes, the breaker heals itself", 0.8),
    ("note", "local stack: redis, gateway :8103, chaos proxy :8113, ollama (GPU) + ollama_cpu", 0.8),
    ("run", "uv run python assets/demo/demo.py", 1.5),
    ("note", "breaker states come from the gateway's own transition log (/admin/breaker/events)", 3.0),
]


def main() -> None:
    dst = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.cast")
    events: list[tuple[float, str]] = []
    lock = threading.Lock()
    t0 = time.monotonic()

    def emit(data: str) -> None:
        with lock:
            events.append((round(time.monotonic() - t0, 4), data.replace("\r\n", "\n").replace("\n", "\r\n")))

    def pump(stream: object) -> None:
        reader = stream  # binary pipe; read whatever has arrived
        while True:
            chunk = reader.read1(4096)  # type: ignore[attr-defined]
            if not chunk:
                return
            emit(chunk.decode("utf-8", "replace"))

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "FORCE_COLOR": "1"}
    for kind, text, pause in SCRIPT:
        if kind == "note":
            emit(f"{AMBER}# {text}{RESET}\n")
        else:
            emit(f"{GREEN}${RESET} ")
            for ch in text:
                emit(ch)
                time.sleep(0.035)
            emit("\n")
            time.sleep(0.3)
            # Fixed, repo-local commands from SCRIPT only; no shell, no untrusted input.
            p = subprocess.Popen(  # noqa: S603  # nosec B603
                text.split(), cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            th = threading.Thread(target=pump, args=(p.stdout,))
            th.start()
            rc = p.wait()
            th.join()
            if rc != 0:
                sys.exit(f"command failed ({rc}): {text}")
        time.sleep(pause)
    emit("")
    header = {
        "version": 2,
        "width": COLS,
        "height": ROWS,
        "timestamp": int(time.time()),
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
        "title": "llmfailover - real session",
    }
    with dst.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(header) + "\n")
        for t, data in events:
            if data:
                f.write(json.dumps([t, "o", data]) + "\n")
    print(f"wrote {dst}: {len(events)} events, {events[-1][0]:.1f} s")


if __name__ == "__main__":
    main()
