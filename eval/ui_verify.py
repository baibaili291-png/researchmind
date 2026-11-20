"""Headless UI verification via CDP against the demo server (port 8001).

Run: python eval/ui_verify.py
Checks that the rendered DOM really contains what the backend claims.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request

from websocket import create_connection

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9361
URL = "http://127.0.0.1:8001/?autorun=1"

proc = subprocess.Popen(
    [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
     f"--user-data-dir=/tmp/rm-ui-{PORT}", f"--remote-debugging-port={PORT}",
     "--window-size=1600,1000", URL],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

out: list[str] = []
try:
    page = None
    for _ in range(40):
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1).read())
            page = next((t for t in tabs if t.get("type") == "page" and "8001" in t.get("url", "")), None)
            if page:
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not page:
        raise SystemExit("no page target")

    ws = create_connection(page["webSocketDebuggerUrl"], timeout=30)
    counter = [0]

    def cmd(method: str, params: dict | None = None):
        counter[0] += 1
        ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == counter[0]:
                return msg

    def ev(expr: str):
        r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")

    ready = False
    for _ in range(120):
        if ev("!!document.querySelector('.cite-ref')"):
            ready = True
            break
        time.sleep(0.5)
    out.append(f"report rendered: {ready}")
    out.append("badge text: " + str(ev("document.getElementById('providerText')?.textContent")))
    out.append("badge class: " + str(ev("document.getElementById('providerPill')?.className")))
    out.append("citation refs: " + str(ev("document.querySelectorAll('.cite-ref').length")))
    out.append("report sections: " + str(ev("Array.from(document.querySelectorAll('.md h2')).map(e=>e.textContent).join(' | ')")))
    out.append("malformed nested bullet in DOM: " + str(ev("document.querySelector('.md')?.textContent.includes('- - ')")))
    out.append("provenance shown: " + str(ev("document.querySelector('.md')?.textContent.includes('数据来源说明')")))

    ev("document.querySelector('.tab[data-tab=evidence]').click(); 'ok'")
    time.sleep(0.5)
    out.append("evidence cards: " + str(ev("document.querySelectorAll('.ev-card').length")))
    out.append("depth badges: " + str(ev("Array.from(document.querySelectorAll('.ev-depth')).map(e=>e.textContent).join('|')")))
    out.append("depth badge classes: " + str(ev("Array.from(document.querySelectorAll('.ev-depth')).map(e=>e.className).join('|')")))

    ev("document.querySelector('.tab[data-tab=report]').click(); 'ok'")
    time.sleep(0.4)
    ev("document.querySelector('.cite-ref').click(); 'ok'")
    time.sleep(0.8)
    out.append("drawer open: " + str(ev("document.getElementById('detailDrawer')?.classList.contains('open')")))
    out.append("drawer title: " + str(ev("document.querySelector('.detail-title')?.textContent")))
    out.append("drawer raw present: " + str(ev("!!document.querySelector('.detail-raw')")))

    shot = cmd("Page.captureScreenshot", {"format": "png"})
    data = shot.get("result", {}).get("data")
    if data:
        with open("eval/ui-verify.png", "wb") as fh:
            fh.write(base64.b64decode(data))
        out.append("screenshot: eval/ui-verify.png")
    ws.close()
finally:
    proc.terminate()

print("\n".join(out))
with open("eval/ui_verify_output.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(out))
