# "New freight request" page backend (public/freight.html). Team password = APP_PASSWORD (same as the other team tools).
#   POST {password, action:"owners"}                         -> {owners:[{id,name}]}
#   POST {password, action:"search", q}                      -> {companies:[...]}
#   POST {password, action:"read", text, files:[{name,b64}]}  -> customer + shipment read by Claude
#   POST {password, action:"create", text, files, transcript, company_id, contact_id, owner_id, owner_name, origin, dest}
# Engine code in api/_freight (synced from OneDrive\InXpress\Claude Collab\20 Freight\Accufrate by sync_to_vercel.py).
import base64
import hmac
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_freight"))
os.environ.setdefault("FREIGHT_SERVER", "1")
os.environ.setdefault("FREIGHT_OUT_DIR", "/tmp/quotes")

import freight_quick  # noqa: E402
import freight_page  # noqa: E402


def _files(req):
    out = []
    for f in (req.get("files") or [])[:10]:
        try:
            out.append({"name": str(f.get("name") or "file")[:120], "data": base64.b64decode(f.get("b64") or "")})
        except Exception:
            pass
    return out


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad request"})
        pw = os.environ.get("APP_PASSWORD") or ""
        if not pw or not hmac.compare_digest(str(req.get("password") or ""), pw):
            return self._send(401, {"error": "wrong password"})
        try:
            a = req.get("action")
            if a == "owners":
                return self._send(200, {"owners": freight_quick.owners()})
            if a == "search":
                return self._send(200, {"companies": freight_quick.candidates(req.get("q") or "", 10)})
            if a == "read":
                return self._send(200, freight_quick.read(req.get("text") or "", _files(req)))
            if a == "create":
                return self._send(200, freight_quick.create(req.get("text") or "", _files(req), req.get("transcript") or "",
                                                            req.get("company_id"), req.get("contact_id"), req.get("owner_id"),
                                                            req.get("owner_name") or "", req.get("origin") or "", req.get("dest") or "",
                                                            req.get("shipment") or None))
            # quote view, carrier switch, margin/price, booking (freight_page)
            did = str(req.get("deal_id") or "").strip()
            if a == "quotes":
                return self._send(200, {"quotes": freight_page.quotes(req.get("q") or "")})
            if a in ("deal", "set_option", "set_margin", "set_amount", "set_direct_rate", "request_booking", "approve") and not did.isdigit():
                return self._send(400, {"error": "missing deal"})
            if a == "deal":
                return self._send(200, freight_page.view(did, fill=bool(req.get("fill"))))
            if a == "set_option":
                return self._send(200, freight_page.set_option(did, req.get("n")))
            if a == "set_margin":
                return self._send(200, freight_page.set_margin(did, req.get("pct")))
            if a == "set_amount":
                return self._send(200, freight_page.set_amount(did, req.get("amount")))
            if a == "set_direct_rate":
                return self._send(200, freight_page.set_direct_rate(did, req.get("cost_cad"), req.get("days"), req.get("dr_quote_no"),
                                                                    req.get("who") or ""))
            if a == "request_booking":
                return self._send(200, freight_page.request_booking(did, req.get("fields") or {}, req.get("pickup_date") or "",
                                                                    req.get("booking_action") or "", req.get("who") or ""))
            if a == "approve":
                return self._send(200, freight_page.approve(did, req.get("pin") or "", req.get("fields"), req.get("pickup_date") or "",
                                                            req.get("booking_action") or ""))
            return self._send(400, {"error": "unknown action"})
        except ValueError as e:
            return self._send(200, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
