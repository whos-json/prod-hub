"""Checks the 'tell the hosted app our new address' step against a local stand-in endpoint. No gateway needs to be running."""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import gateway as g

got = {}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        got["auth"] = self.headers.get("Authorization"); got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()

hosted = g.DATA / "hosted_app.txt"; old = hosted.read_text() if hosted.exists() else None
try:
    hosted.write_text(f"http://127.0.0.1:{srv.server_port}/api/gateway-url")
    g.push_url("https://example-name.trycloudflare.com", once=True)
    assert got["body"] == {"url": "https://example-name.trycloudflare.com"}, got
    assert got["auth"] == f"Bearer {g.TOKEN}", "key not sent"
    assert "updated" in g.state["push"], g.state["push"]
    hosted.write_text("http://insecure.example/x"); g.push_url("https://x.trycloudflare.com", once=True)
    assert "refused" in g.state["push"], g.state["push"]
    # two apps listed: both get the address
    got.clear(); srv2 = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv2.serve_forever, daemon=True).start()
    hits = []
    class H2(H):
        def do_POST(self):
            hits.append(1); super().do_POST()
    srv2.RequestHandlerClass = H2
    hosted.write_text(f"# app one\nhttp://127.0.0.1:{srv.server_port}/a\nhttp://127.0.0.1:{srv2.server_port}/b\n")
    g.push_url("https://two.trycloudflare.com", once=True)
    assert len(hits) == 1 and g.state["push"].count("updated") == 2, (hits, g.state["push"])
    print("PASS push: sends new address with the key to every listed app; refuses plain http")
finally:
    hosted.unlink(missing_ok=True)
    if old is not None: hosted.write_text(old)
