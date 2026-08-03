#!/usr/bin/env python3
"""
snapshot-demo — state that visibly moves, so you can watch a restore undo it.

A background thread appends one UTC timestamp per line to $STATE_DIR/ticks.log
every TICK_SECONDS. The endpoint reads that file back:

  GET /       plain text, the human-readable summary
  GET /state  the same thing as JSON

Nothing is cached in memory, so what you read is what is on the volume right
now. Snapshot the app, let it tick a few more times, restore, and the tick
count and last timestamp both jump back to where the snapshot left them.

Config via environment:
  LABEL           the configured value, echoed back (required)
  STATE_DIR       default /state
  PORT            default 8080
  TICK_SECONDS    default 30
"""

import json
import os
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LABEL = os.environ.get("LABEL", "")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
PORT = int(os.environ.get("PORT", "8080"))
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "30"))

TICKS = STATE_DIR / "ticks.log"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read_ticks():
    """Every timestamp recorded so far, oldest first."""
    if not TICKS.is_file():
        return []
    return [line for line in TICKS.read_text().splitlines() if line]


def append_tick():
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    # Append rather than rewrite: a torn write can then cost at most the one
    # line being added, not the whole history the demo is about.
    with TICKS.open("a") as f:
        f.write(stamp + "\n")
    return stamp


def summary():
    ticks = read_ticks()
    return {
        "label": LABEL,
        "last_tick": ticks[-1] if ticks else None,
        "first_tick": ticks[0] if ticks else None,
        "tick_count": len(ticks),
        "state_file": str(TICKS),
    }


def ticker():
    while True:
        try:
            log(f"tick {append_tick()}")
        except OSError as e:
            log(f"could not write {TICKS}: {e}")
        threading.Event().wait(TICK_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = summary()
        last = data["last_tick"] or "(nothing recorded yet)"

        if self.path.rstrip("/") == "/state":
            body = json.dumps(data, indent=2) + "\n"
            content_type = "application/json"
        else:
            body = (
                f"Your config param was: {LABEL}, "
                f"the last recorded timestamp was {last}\n"
                f"\n"
                f"ticks recorded: {data['tick_count']}\n"
                f"first tick:     {data['first_tick'] or '(none)'}\n"
                f"state file:     {data['state_file']}\n"
                f"tick interval:  {TICK_SECONDS}s\n"
            )
            content_type = "text/plain; charset=utf-8"

        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        # The whole point is to see state change, so never let a browser or a
        # proxy answer from cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")


def main():
    if not LABEL:
        sys.exit("LABEL is not set; run `harbor config demo-snapshot --set label=...`")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_ticks()
    log(f"state at {TICKS}: {len(existing)} tick(s) already recorded")
    if existing:
        log(f"most recent was {existing[-1]}")

    threading.Thread(target=ticker, daemon=True).start()

    log(f"listening on :{PORT} with label {LABEL!r}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
