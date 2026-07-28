#!/usr/bin/env python3
"""
adpause — a one-button pause for AdGuard Home.

Serves:
  GET  /        the page (bookmark this, add to home screen)
  GET  /go      pause for PAUSE_MINUTES, plain text  <- point the physical button here
  GET  /state   JSON: {"paused": bool, "seconds_left": int}
  POST /pause   pause
  POST /resume  un-pause immediately

AdGuard Home does the timing itself, so protection always comes back
even if this process dies, the phone sleeps, or the button is mashed.

Config via environment:
  AGH_URL         default http://127.0.0.1:3000
  AGH_USER        default admin
  AGH_PASS        (required)
  PAUSE_MINUTES   default 15
  PORT            default 8099
"""

import base64
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGH_URL = os.environ.get("AGH_URL", "http://127.0.0.1:3000").rstrip("/")
AGH_USER = os.environ.get("AGH_USER", "admin")
AGH_PASS = os.environ.get("AGH_PASS", "")
PAUSE_MINUTES = int(os.environ.get("PAUSE_MINUTES", "15"))
PORT = int(os.environ.get("PORT", "8099"))

_AUTH = "Basic " + base64.b64encode(f"{AGH_USER}:{AGH_PASS}".encode()).decode()


def _call(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{AGH_URL}{path}",
        data=data,
        method="POST" if data else "GET",
        headers={"Authorization": _AUTH, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read()
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # AdGuard's /control/protection replies with plain "OK".
        return {}


def state():
    """Ask AdGuard Home what's actually true right now."""
    s = _call("/control/status")
    if s.get("protection_enabled", True):
        return {"paused": False, "seconds_left": 0}
    # AdGuard reports remaining disable time in milliseconds.
    left = int((s.get("protection_disabled_duration") or 0) / 1000)
    return {"paused": left > 0, "seconds_left": max(left, 0)}


def pause():
    _call("/control/protection",
          {"enabled": False, "duration": PAUSE_MINUTES * 60 * 1000})


def resume():
    _call("/control/protection", {"enabled": True})


PAGE = """<!doctype html>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=theme-color content="#12161c">
<title>Ads</title>
<style>
  :root {
    --ink: #12161c;
    --paper: #f2efe9;
    --block: #4fb286;
    --allow: #e0a458;
    --dim: rgba(242,239,233,.5);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--ink);
    color: var(--paper);
    font: 400 17px/1.4 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 2.5rem; padding: 2rem;
    transition: background .5s ease;
  }
  body.paused { background: #241d12; }
  #tap {
    all: unset; cursor: pointer;
    width: min(74vw, 17rem); aspect-ratio: 1; border-radius: 50%;
    display: grid; place-items: center; text-align: center;
    background: var(--block); color: var(--ink);
    font-weight: 650; font-size: 1.35rem; letter-spacing: -.01em;
    padding: 2rem;
    box-shadow: 0 0 0 0 var(--block);
    transition: transform .12s ease, background .4s ease, box-shadow .4s ease;
  }
  #tap:active { transform: scale(.955); }
  #tap:focus-visible { outline: 3px solid var(--paper); outline-offset: 6px; }
  body.paused #tap {
    background: none; color: var(--allow);
    box-shadow: inset 0 0 0 2px rgba(224,164,88,.35);
    font-variant-numeric: tabular-nums;
    font-size: clamp(3rem, 17vw, 4.75rem); font-weight: 300;
    letter-spacing: -.04em;
  }
  #note { color: var(--dim); font-size: .95rem; text-align: center; min-height: 1.4em; }
  #note b { color: var(--paper); font-weight: 500; }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<button id=tap></button>
<p id=note></p>

<script>
const tap = document.getElementById('tap'), note = document.getElementById('note');
let left = 0, busy = false;

const mmss = s => Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');

function paint() {
  const on = left > 0;
  document.body.classList.toggle('paused', on);
  tap.textContent = on ? mmss(left) : 'Let ads through';
  note.innerHTML = on
    ? 'Ads are getting through. Blocking comes back on its own — <b>tap to end early</b>.'
    : 'Ads are blocked everywhere in the house.<br>Tap for ' + MINUTES + ' minutes of normal internet.';
}

async function sync() {
  try {
    const r = await fetch('/state', {cache: 'no-store'});
    left = (await r.json()).seconds_left;
    paint();
  } catch { note.textContent = "Can't reach the blocker right now."; }
}

tap.onclick = async () => {
  if (busy) return;
  busy = true;
  try {
    await fetch(left > 0 ? '/resume' : '/pause', {method: 'POST'});
    if (navigator.vibrate) navigator.vibrate(left > 0 ? 12 : [12, 60, 12]);
    await sync();
  } finally { busy = false; }
};

setInterval(() => { if (left > 0) { left--; paint(); } }, 1000);
setInterval(sync, 10000);
document.addEventListener('visibilitychange', () => !document.hidden && sync());
sync();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/":
                self._send(200, PAGE.replace("MINUTES", str(PAUSE_MINUTES)), "text/html; charset=utf-8")
            elif self.path == "/state":
                self._send(200, json.dumps(state()), "application/json")
            elif self.path == "/go":  # physical button
                pause()
                self._send(200, f"ads allowed for {PAUSE_MINUTES} min\n", "text/plain")
            else:
                self._send(404, "no\n", "text/plain")
        except Exception as e:
            self._send(502, f"adguard unreachable: {e}\n", "text/plain")

    def do_POST(self):
        try:
            if self.path == "/pause":
                pause()
            elif self.path == "/resume":
                resume()
            else:
                return self._send(404, "no\n", "text/plain")
            self._send(200, json.dumps(state()), "application/json")
        except Exception as e:
            self._send(502, json.dumps({"error": str(e)}), "application/json")

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    if not AGH_PASS:
        raise SystemExit("Set AGH_PASS to your AdGuard Home web password.")
    print(f"adpause on :{PORT} -> {AGH_URL} ({PAUSE_MINUTES} min)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
