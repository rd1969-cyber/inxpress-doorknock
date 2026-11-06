# Freight auto-pricing worker (Python). Called by api/hubspot-webhook.js and the daily Vercel cron.
#   POST {deal_ids:[...], mode:"C"|"T", dry:bool, force:bool}  header x-freight-key: FREIGHT_WORKER_KEY
#   GET  (Vercel cron, Authorization: Bearer CRON_SECRET)       -> sweep of waiting deals
# Engine code lives in api/_freight (synced from OneDrive\InXpress\Claude Collab\20 Freight\Accufrate by sync_to_vercel.py).
import hmac
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_freight"))
os.environ.setdefault("FREIGHT_SERVER", "1")
os.environ.setdefault("FREIGHT_OUT_DIR", "/tmp/quotes")

import freight_auto  # noqa: E402


def _eq(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # daily safety sweep (Vercel cron)
        if not _eq(self.headers.get("authorization"), "Bearer " + os.environ.get("CRON_SECRET", "")):
            return self._send(401, {"error": "unauthorized"})
        try:
            out = {"sweep": freight_auto.sweep(os.environ.get("FREIGHT_MODE", "C"))}
            try:  # PODs that weren't ready on the delivery day
                import freight_bol_intake
                out["pods"] = freight_bol_intake.pod_sweep()
            except Exception as e:
                out["pods_error"] = f"{type(e).__name__}: {e}"
            self._send(200, out)
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not _eq(self.headers.get("x-freight-key"), os.environ.get("FREIGHT_WORKER_KEY")):
            return self._send(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("content-length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            mode = req.get("mode") or os.environ.get("FREIGHT_MODE", "C")
            if req.get("action") == "intake":  # email in the intake inbox -> Freight deal (+ priced)
                import freight_intake
                out = [freight_intake.process_thread(str(req.get("thread_id")), req.get("message_id"), req.get("event") or "",
                                                     True if req.get("dry") else None)]
            elif req.get("action") == "book":  # Shipment Action / Booking Approval changed -> BOL, dispatch or cancel
                import freight_book
                out = [freight_book.book(str(d), bool(req.get("dry"))) for d in (req.get("deal_ids") or [])[:3]]
            elif req.get("action") == "margin":  # Margin % changed by a person -> Amount + option prices follow
                out = [freight_auto.margin_changed(str(d), bool(req.get("dry"))) for d in (req.get("deal_ids") or [])[:5]]
            elif req.get("action") == "option":  # Use Option # changed -> switch carrier, cost, DI, Amount
                out = [freight_auto.option_changed(str(d), bool(req.get("dry"))) for d in (req.get("deal_ids") or [])[:5]]
            elif req.get("action") == "carrier":  # deal's Carrier changed -> DI % follows
                out = [freight_auto.carrier_changed(str(d), bool(req.get("dry"))) for d in (req.get("deal_ids") or [])[:5]]
            else:
                out = [freight_auto.process_deal(str(d), mode, bool(req.get("dry")), bool(req.get("force")))
                       for d in (req.get("deal_ids") or [])[:5]]
            print(json.dumps(out, default=str)[:3000])
            self._send(200, {"results": out})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
