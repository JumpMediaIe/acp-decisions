"""Local receiver + ref queue for the Donegal MCP-browser scrape.

- POST /            {ref, docName, b64}  -> saves PDF to data/donegal-pdfs/<ref>.pdf
- GET  /next?n=40                        -> JSON array of up to n pending refs
                                            (refs not yet on disk), and marks them
                                            issued so overlapping calls don't repeat.
- GET  /status                           -> {done, issued, remaining}

The pending list is the council's refused refs minus whatever PDFs already exist
on disk, computed fresh from acp.db at startup. localhost is exempt from the
HTTPS mixed-content block, so the Donegal page can talk to this.
"""
import base64, json, http.server, pathlib, sqlite3
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "donegal-pdfs"
OUT.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(ROOT / "acp.db")
ALL_REFS = [r[0] for r in con.execute(
    "SELECT application_number FROM planning_applications "
    "WHERE planning_authority='Donegal County Council' AND decision LIKE '%Refuse%' "
    "AND application_number IS NOT NULL AND application_number!='' "
    "AND NOT EXISTS (SELECT 1 FROM council_reasons_fetch f WHERE f.object_id=planning_applications.object_id) "
    "ORDER BY decision_date DESC")]
con.close()
issued: set[str] = set()


def _pending():
    done = {p.stem for p in OUT.glob("*.pdf")}
    return [r for r in ALL_REFS if r not in done and r not in issued]


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body=b"ok", ct="text/plain"):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", ct)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/next":
            n = int((parse_qs(u.query).get("n") or ["40"])[0])
            batch = _pending()[:n]
            issued.update(batch)
            self._send(200, json.dumps(batch), "application/json")
        elif u.path == "/status":
            done = len(list(OUT.glob("*.pdf")))
            self._send(200, json.dumps({"done": done, "issued": len(issued),
                                        "pending": len(_pending()), "total": len(ALL_REFS)}),
                       "application/json")
        else:
            self._send(404, b"?")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n))
        ref = str(data["ref"]).strip().replace("/", "_")
        (OUT / f"{ref}.pdf").write_bytes(base64.b64decode(data["b64"]))
        self._send(200, b"ok")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"sink -> {OUT}  pending={len(ALL_REFS)}", flush=True)
    http.server.HTTPServer(("127.0.0.1", 8731), H).serve_forever()
