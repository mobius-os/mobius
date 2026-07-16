"""Disposable same-process-network Tandoor stand-in for browser topology tests."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
  def log_message(self, _format, *_args):
    pass

  def do_GET(self):
    if self.path == "/services/tandoor/api/ping":
      has_cookie = "fake_tandoor=session" in self.headers.get("Cookie", "")
      body = b'{"ok":true,"cookie":true}' if has_cookie else b'{"ok":false,"cookie":false}'
      self.send_response(200 if has_cookie else 401)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)
      return
    if self.path == "/services/tandoor/go-bad":
      self.send_response(302)
      self.send_header("Location", "http://127.0.0.1:9/unreachable")
      self.end_headers()
      return
    if self.path != "/services/tandoor/":
      self.send_error(404)
      return
    body = b"""<!doctype html><meta charset=utf-8><title>Fake Tandoor</title>
<main id=tandoor>Real service document <output id=status>checking</output>
<button id=go-bad onclick=\"location.href='/services/tandoor/go-bad'\">Navigate to failure</button></main>
<script>
localStorage.setItem('fake-tandoor-storage','works');
const pingStatus=document.getElementById('status');
fetch('/services/tandoor/api/ping',{credentials:'same-origin'})
 .then(r=>r.json()).then(v=>pingStatus.textContent=v.ok&&v.cookie?'ready':'bad')
 .catch(()=>pingStatus.textContent='fetch-failed');
</script>"""
    self.send_response(200)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    # Deliberately incompatible upstream policy: the dedicated proxy must
    # remove/replace it for the adapter -> service -> shell ancestor chain.
    self.send_header("X-Frame-Options", "SAMEORIGIN")
    self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; frame-ancestors 'self'")
    self.send_header(
      "Set-Cookie",
      "fake_tandoor=session; Domain=.localhost; Path=/services/tandoor; SameSite=Lax",
    )
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)


ThreadingHTTPServer((
  "127.0.0.1", int(os.environ.get("FAKE_TANDOOR_PORT", "8123")),
), Handler).serve_forever()
