#!/usr/bin/env python3
"""
VAT Bot status dashboard — a local web page that answers one question:
"is the bot working right now?"

Written for a non-technical reader. Every label is plain language, every
status carries an icon AND a word (never colour alone), and the page is
fully self-contained: no CDN, no webfonts, no outbound requests. That
matters because the dashboard is most needed exactly when the internet
is down.

Run:    .venv/bin/python scripts/dashboard.py
Then:   http://127.0.0.1:8808
The Desktop app "VAT Bot Dashboard" does both for you.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path("/Users/tashkent/Tashkent/vat-bot")
LOG = PROJECT / "logs" / "bot.log"
LABEL = "com.vatbot.bot"
PORT = 8808
BIN = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def sh(cmd: str, timeout: int = 10) -> str:
    """Run a shell command, returning stdout ('' on any failure)."""
    try:
        env = {**os.environ, "PATH": BIN}
        out = subprocess.run(cmd, shell=True, capture_output=True,
                             text=True, timeout=timeout, env=env)
        return out.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- checks

def check_bot() -> dict:
    out = sh(f'/bin/launchctl print "gui/$(id -u)/{LABEL}" 2>/dev/null')
    running = "state = running" in out
    pid = ""
    m = re.search(r"pid = (\d+)", out)
    if m:
        pid = m.group(1)
    uptime = ""
    if pid:
        et = sh(f"ps -o etime= -p {pid}").strip()
        uptime = _pretty_etime(et)
    return {
        "key": "bot", "label": "Receipt bot",
        "ok": running,
        "detail": f"running for {uptime}" if running and uptime
                  else ("running" if running else "not running"),
    }


def check_storage() -> dict:
    running = bool(sh("/usr/bin/pgrep -x mongod"))
    detail = "not running — receipts cannot be saved"
    count = None
    if running:
        try:
            sys.path.insert(0, str(PROJECT))
            from pymongo import MongoClient
            c = MongoClient("mongodb://localhost:27017",
                            serverSelectionTimeoutMS=2000)
            count = c["vat_bot"]["receipts"].count_documents({})
            detail = f"{count:,} receipts stored safely"
        except Exception as e:
            running = False
            detail = f"unreachable ({type(e).__name__})"
    return {"key": "storage", "label": "Receipt storage",
            "ok": running, "detail": detail, "count": count}


def _recent_log(nbytes: int = 200_000) -> str:
    try:
        with LOG.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def check_telegram(tail: str) -> dict:
    """Connected if the most recent start-up block reached Telegram and
    nothing has since reported a fatal stop."""
    connected = "Connected to Telegram" in tail
    started = tail.rfind("Application started")
    stopped = tail.rfind("Application is stopping")
    alive = started > stopped
    ok = connected and alive
    return {"key": "telegram", "label": "Telegram connection",
            "ok": ok,
            "detail": "users can reach the bot" if ok
                      else "bot is not answering Telegram"}


def check_reader(tail: str) -> dict:
    launched = tail.rfind("Playwright Chromium launched")
    stopped = tail.rfind("Application is stopping")
    ok = launched > stopped
    return {"key": "reader", "label": "Tax-website reader",
            "ok": ok,
            "detail": "can read VAT amounts from soliq.uz" if ok
                      else "VAT amounts may not be read"}


def check_internet() -> dict:
    ok = bool(sh("/sbin/ping -c1 -t2 1.1.1.1 >/dev/null 2>&1 && echo up"))
    return {"key": "internet", "label": "Internet",
            "ok": ok,
            "detail": "online" if ok else "OFFLINE — this breaks everything"}


def check_power() -> dict:
    out = sh("/usr/bin/pmset -g")
    auto = re.search(r"autorestart\s+(\d)", out)
    awake = "sleep           0" in out or "sleep                0" in out
    ok = bool(auto and auto.group(1) == "1")
    bits = []
    bits.append("restarts itself after a power cut" if ok
                else "will NOT restart after a power cut")
    if awake:
        bits.append("stays awake")
    return {"key": "power", "label": "Power settings",
            "ok": ok, "detail": ", ".join(bits)}


# ------------------------------------------------------------ statistics

def gather_stats(tail: str) -> dict:
    stats = {"today": 0, "week": 0, "last_receipt": None,
             "daily": [], "errors_today": 0, "backup": None, "disk": None}
    try:
        from pymongo import MongoClient
        c = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
        col = c["vat_bot"]["receipts"]
        now = datetime.utcnow()
        stats["today"] = col.count_documents(
            {"created_at": {"$gte": now - timedelta(hours=24)}})
        stats["week"] = col.count_documents(
            {"created_at": {"$gte": now - timedelta(days=7)}})
        last = col.find_one({}, sort=[("created_at", -1)])
        if last and last.get("created_at"):
            stats["last_receipt"] = last["created_at"].isoformat()
        # 14-day daily counts for the activity chart
        start = (now - timedelta(days=13)).replace(hour=0, minute=0,
                                                   second=0, microsecond=0)
        buckets = {(start + timedelta(days=i)).strftime("%Y-%m-%d"): 0
                   for i in range(14)}
        for doc in col.find({"created_at": {"$gte": start}},
                            {"created_at": 1}):
            # stored UTC; show in local time (+05)
            k = (doc["created_at"] + timedelta(hours=5)).strftime("%Y-%m-%d")
            if k in buckets:
                buckets[k] += 1
        stats["daily"] = [{"date": k, "count": v} for k, v in buckets.items()]
    except Exception:
        pass

    stats["errors_today"] = tail.count(" ERROR ")

    b = PROJECT / "backups"
    try:
        snaps = sorted([p for p in b.iterdir() if p.is_dir()],
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if snaps:
            age = time.time() - snaps[0].stat().st_mtime
            stats["backup"] = {"name": snaps[0].name,
                               "hours": round(age / 3600, 1)}
    except Exception:
        pass

    try:
        t, u, f = shutil.disk_usage("/")
        stats["disk"] = {"pct": int(u * 100 / t), "free_gb": round(f / 1024**3)}
    except Exception:
        pass
    return stats


def _pretty_etime(et: str) -> str:
    """ps etime (dd-hh:mm:ss | hh:mm:ss | mm:ss) -> '3 days', '4 hours'."""
    if not et:
        return ""
    days = 0
    if "-" in et:
        d, et = et.split("-", 1)
        days = int(d)
    parts = [int(p) for p in et.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m = parts[0], parts[1]
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minute{'s' if m != 1 else ''}"


def build_status() -> dict:
    tail = _recent_log()
    checks = [check_bot(), check_storage(), check_telegram(tail),
              check_reader(tail), check_internet(), check_power()]
    # Overall verdict. The first four decide working/broken; internet and
    # power settings are advisories that downgrade to "attention".
    core = [c for c in checks if c["key"] in
            ("bot", "storage", "telegram", "reader")]
    advisory = [c for c in checks if c["key"] in ("internet", "power")]
    if all(c["ok"] for c in core):
        overall = "working" if all(a["ok"] for a in advisory) else "attention"
    else:
        overall = "broken"
    return {
        "overall": overall,
        "checks": checks,
        "stats": gather_stats(tail),
        "generated": datetime.now().strftime("%H:%M:%S"),
        "generated_full": datetime.now().strftime("%A %d %B %Y, %H:%M"),
    }


# ------------------------------------------------------------------ page
# Self-contained: no CDN, no webfonts. Colours are the data-viz skill's
# reserved STATUS palette (good/warning/critical), which is fixed and
# never themed; every status also carries an icon and a word, so meaning
# never rests on colour alone. Dark mode is a selected set of steps for
# the dark surface, not an automatic inversion.
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VAT Bot Status</title>
<style>
  :root{
    color-scheme: light;
    --surface:#fcfcfb; --card:#ffffff; --line:#e4e3df;
    --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#77766f;
    --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
    --good-ink:#076b07; --warn-ink:#7a5300; --crit-ink:#a32020;
    --bar:#2a78d6;
  }
  @media (prefers-color-scheme: dark){
    :root:where(:not([data-theme="light"])){
      color-scheme: dark;
      --surface:#1a1a19; --card:#232322; --line:#38383550;
      --ink:#f0efec; --ink-2:#c3c2b7; --ink-3:#8d8c84;
      --good-ink:#5ed35e; --warn-ink:#fab219; --crit-ink:#f08a8a;
      --bar:#3987e5;
    }
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --surface:#1a1a19; --card:#232322; --line:#38383550;
    --ink:#f0efec; --ink-2:#c3c2b7; --ink-3:#8d8c84;
    --good-ink:#5ed35e; --warn-ink:#fab219; --crit-ink:#f08a8a;
    --bar:#3987e5;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--surface); color:var(--ink);
    font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:0 16px 48px;
  }
  .wrap{max-width:900px;margin:0 auto}
  header{display:flex;justify-content:space-between;align-items:baseline;
    gap:12px;flex-wrap:wrap;padding:24px 0 8px}
  h1{font-size:19px;margin:0;letter-spacing:-.01em}
  .stamp{color:var(--ink-3);font-size:13px;font-variant-numeric:tabular-nums}

  /* ---- hero verdict: the one thing a passer-by must read ---- */
  .hero{border-radius:16px;padding:28px 24px;margin:12px 0 20px;
    border:1px solid var(--line);background:var(--card);
    display:flex;align-items:center;gap:20px}
  .hero .mark{flex:0 0 auto;width:64px;height:64px;border-radius:50%;
    display:grid;place-items:center;font-size:34px;font-weight:700;color:#fff}
  .hero.working  .mark{background:var(--good)}
  .hero.attention .mark{background:var(--warning);color:#2b1d00}
  .hero.broken   .mark{background:var(--critical)}
  .hero h2{margin:0;font-size:30px;letter-spacing:-.02em;line-height:1.15}
  .hero.working  h2{color:var(--good-ink)}
  .hero.attention h2{color:var(--warn-ink)}
  .hero.broken   h2{color:var(--crit-ink)}
  .hero p{margin:6px 0 0;color:var(--ink-2);font-size:15px;max-width:52ch}

  /* ---- the fix button ---- */
  .fixwrap{margin:0 0 24px}
  button.fix{
    width:100%;padding:18px 20px;border-radius:14px;border:0;cursor:pointer;
    background:var(--critical);color:#fff;font-size:18px;font-weight:650;
    font-family:inherit;letter-spacing:-.01em}
  button.fix.calm{background:var(--card);color:var(--ink-2);
    border:1px solid var(--line);font-weight:500;font-size:15px;padding:13px}
  button.fix:disabled{opacity:.6;cursor:progress}
  .fixnote{margin:8px 2px 0;color:var(--ink-3);font-size:13px}
  pre.out{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);
    border-radius:12px;padding:14px;font-size:12.5px;color:var(--ink-2);
    margin:12px 0 0;max-height:260px;overflow:auto}

  h3{font-size:12px;text-transform:uppercase;letter-spacing:.07em;
    color:var(--ink-3);margin:26px 0 10px;font-weight:650}

  /* ---- component checks ---- */
  ul.checks{list-style:none;margin:0;padding:0;border:1px solid var(--line);
    border-radius:14px;overflow:hidden;background:var(--card)}
  ul.checks li{display:flex;align-items:center;gap:12px;padding:13px 16px;
    border-bottom:1px solid var(--line)}
  ul.checks li:last-child{border-bottom:0}
  .dot{flex:0 0 auto;width:22px;height:22px;border-radius:50%;
    display:grid;place-items:center;font-size:13px;font-weight:700;color:#fff}
  .dot.ok{background:var(--good)}
  .dot.no{background:var(--critical)}
  .cname{font-weight:550;flex:0 0 190px}
  .cdetail{color:var(--ink-2);font-size:14px;flex:1 1 auto}
  .cword{margin-left:auto;font-size:12px;font-weight:650;letter-spacing:.04em;
    text-transform:uppercase}
  .cword.ok{color:var(--good-ink)} .cword.no{color:var(--crit-ink)}

  /* ---- stat tiles ---- */
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .tile{border:1px solid var(--line);border-radius:14px;padding:14px 16px;background:var(--card)}
  .tile .v{font-size:26px;font-weight:650;letter-spacing:-.02em;
    font-variant-numeric:tabular-nums}
  .tile .k{color:var(--ink-3);font-size:12.5px;margin-top:2px}

  figure{margin:0;border:1px solid var(--line);border-radius:14px;
    padding:16px;background:var(--card)}
  figcaption{font-size:13px;color:var(--ink-2);margin-bottom:10px}
  .bars{display:flex;align-items:flex-end;gap:4px;height:110px}
  .bcol{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
    height:100%;position:relative}
  .bcol .b{background:var(--bar);border-radius:4px 4px 0 0;min-height:2px}
  .bcol:hover .tip{display:block}
  .tip{display:none;position:absolute;bottom:calc(100% + 6px);left:50%;
    transform:translateX(-50%);background:var(--ink);color:var(--surface);
    padding:5px 9px;border-radius:7px;font-size:12px;white-space:nowrap;z-index:5}
  .xlab{display:flex;gap:4px;margin-top:6px}
  .xlab span{flex:1;text-align:center;font-size:10.5px;color:var(--ink-3)}
  @media (max-width:620px){
    ul.checks li{flex-wrap:wrap}
    .cname{flex:0 0 auto}
    .cdetail{flex:1 0 100%;margin-left:34px}
    .cword{margin-left:auto}
    .hero{flex-direction:column;align-items:flex-start;gap:14px}
    .hero h2{font-size:25px}
  }
  footer{margin-top:28px;padding-top:16px;border-top:1px solid var(--line);
    color:var(--ink-3);font-size:13px}
  footer b{color:var(--ink-2)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>VAT Refund Bot — Status</h1>
    <div class="stamp" id="stamp">checking…</div>
  </header>
  <div id="app">
    <div class="hero attention"><div class="mark">…</div>
      <div><h2>Checking…</h2><p>Reading the current state of the bot.</p></div></div>
  </div>
  <footer id="foot"></footer>
</div>

<script>
const EL = document.getElementById('app');
const STAMP = document.getElementById('stamp');
const FOOT = document.getElementById('foot');
let busy = false;

const VERDICT = {
  working:  {icon:'✓', title:'Everything is working',
             sub:'The bot is online and people can send in their receipts. Nothing for you to do.'},
  attention:{icon:'!', title:'Working, but check below',
             sub:'Receipts are still being processed, but something needs attention.'},
  broken:   {icon:'✕', title:'The bot is NOT working',
             sub:'People cannot submit receipts right now. Press the red button below — it usually fixes it.'}
};

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function ago(iso){
  if(!iso) return {v:'never', k:'Last receipt'};
  const mins = Math.round((Date.now() - new Date(iso+'Z').getTime())/60000);
  if (mins < 60)  return {v: mins+'m', k:'Since last receipt'};
  if (mins < 1440)return {v: Math.round(mins/60)+'h', k:'Since last receipt'};
  return {v: Math.round(mins/1440)+'d', k:'Since last receipt'};
}

function render(d){
  const v = VERDICT[d.overall];
  const broken = d.overall === 'broken';
  let h = '';

  h += `<div class="hero ${d.overall}">
          <div class="mark" aria-hidden="true">${v.icon}</div>
          <div><h2>${v.title}</h2><p>${v.sub}</p></div>
        </div>`;

  h += `<div class="fixwrap">
          <button class="fix ${broken?'':'calm'}" id="fixbtn">
            ${broken ? 'Fix it now — restart the bot' : 'Restart the bot (safe to press anytime)'}
          </button>
          <div class="fixnote">Takes about 20 seconds. You can press it more than once.</div>
          <div id="fixout"></div>
        </div>`;

  h += `<h3>System checks</h3><ul class="checks">`;
  for (const c of d.checks){
    h += `<li>
            <span class="dot ${c.ok?'ok':'no'}" aria-hidden="true">${c.ok?'✓':'✕'}</span>
            <span class="cname">${esc(c.label)}</span>
            <span class="cdetail">${esc(c.detail)}</span>
            <span class="cword ${c.ok?'ok':'no'}">${c.ok?'OK':'Problem'}</span>
          </li>`;
  }
  h += `</ul>`;

  const s = d.stats, a = ago(s.last_receipt);
  h += `<h3>Activity</h3><div class="tiles">
      <div class="tile"><div class="v">${s.today}</div><div class="k">Receipts today</div></div>
      <div class="tile"><div class="v">${s.week}</div><div class="k">Receipts this week</div></div>
      <div class="tile"><div class="v">${a.v}</div><div class="k">${a.k}</div></div>
      <div class="tile"><div class="v">${s.errors_today}</div><div class="k">Errors in today's log</div></div>
    </div>`;

  if (s.daily && s.daily.length){
    const max = Math.max(1, ...s.daily.map(x=>x.count));
    h += `<h3>Last 14 days</h3><figure>
            <figcaption>Receipts processed per day</figcaption><div class="bars">`;
    for (const day of s.daily){
      const pct = (day.count / max) * 100;
      const nice = new Date(day.date+'T00:00:00').toLocaleDateString(undefined,{weekday:'short',day:'numeric'});
      h += `<div class="bcol"><span class="tip">${nice}: ${day.count}</span>
              <div class="b" style="height:${pct}%"></div></div>`;
    }
    h += `</div><div class="xlab">`;
    for (const day of s.daily){
      h += `<span>${new Date(day.date+'T00:00:00').getDate()}</span>`;
    }
    h += `</div></figure>`;
  }

  EL.innerHTML = h;
  document.getElementById('fixbtn').addEventListener('click', doFix);
  STAMP.textContent = 'Updated ' + d.generated;

  const bk = s.backup ? `Last backup <b>${s.backup.hours}h</b> ago` : 'No backup found';
  const dk = s.disk ? ` · Disk <b>${s.disk.pct}%</b> full, ${s.disk.free_gb} GB free` : '';
  FOOT.innerHTML = `${bk}${dk}<br>This page refreshes by itself every 15 seconds.
     If the red button does not fix it, check that the internet is working, then call DT.`;
}

async function refresh(){
  if (busy) return;
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    render(await r.json());
  }catch(e){
    STAMP.textContent = 'dashboard lost connection';
  }
}

async function doFix(){
  const btn = document.getElementById('fixbtn');
  const out = document.getElementById('fixout');
  busy = true;
  btn.disabled = true;
  btn.textContent = 'Restarting… please wait';
  out.innerHTML = '';
  try{
    const r = await fetch('/api/restore', {method:'POST'});
    const j = await r.json();
    out.innerHTML = '<pre class="out">'+esc(j.output)+'</pre>';
  }catch(e){
    out.innerHTML = '<pre class="out">Could not run the restart.\n'+esc(e)+'</pre>';
  }
  busy = false;
  btn.disabled = false;
  refresh();
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            self._send(200, json.dumps(build_status()), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/api/restore"):
            script = PROJECT / "scripts" / "vatbot_restore.sh"
            try:
                p = subprocess.run(["/bin/bash", str(script)],
                                   capture_output=True, text=True, timeout=180)
                out = (p.stdout or "") + (p.stderr or "")
                self._send(200, json.dumps({"ok": p.returncode == 0,
                                            "output": out.strip()}),
                           "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False,
                                            "output": f"{type(e).__name__}: {e}"}),
                           "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass  # keep the terminal quiet


if __name__ == "__main__":
    # Bind to loopback only — this page can restart the service, so it
    # must never be reachable from the network.
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"VAT Bot dashboard running at http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
