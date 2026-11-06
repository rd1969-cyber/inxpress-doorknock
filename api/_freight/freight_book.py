"""Book from HubSpot: create the accuFRATE BOL, dispatch the pickup, or cancel - driven by two deal fields.

Trigger (HubSpot webhook -> api/freight_price.py action "book"): a Freight Pipeline deal where
  Shipment Action  = Create BOL only | Create BOL + dispatch | Dispatch now | Cancel shipment
  Booking Approval = Approved            (nothing goes to accuFRATE without both)

Create BOL: accuFRATE needs a same-day quote, so the shipment saved at pricing time (Quote Request (app data)) is
re-quoted and the carrier picked on the deal (Use Option #, default 1 = recommended) is matched. If its cost went up
more than PRICE_TOLERANCE, nothing is booked and a task shows old vs new cost.
Results (BOL #, Pickup Confirmation #, PRO #, Booking Status, a note, a task) are written back; Shipment Action is
cleared after every attempt so a retry is a deliberate re-selection.

Mode: FREIGHT_BOOK_MODE = T (default, accuFRATE test mode: nothing real is booked; BOL # is saved as "TEST <id>")
      or C (live). Cancel has no test mode in accuFRATE, so in T it is never sent.
Local: python freight_book.py <deal_id> [--dry-run]     (dry run: validates + re-quotes, sends no BOL/dispatch)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

import freight_queue as fq

ENDPOINT = "https://webservice.accufrate.com/PickupService.svc"
NS_DC = "http://schemas.datacontract.org/2004/07/accufRATE.accufRATE"
NS_ARR = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
NS_T = "http://tempuri.org/"
MODE = (os.environ.get("FREIGHT_BOOK_MODE") or "T").upper()
PRICE_TOLERANCE = float(os.environ.get("FREIGHT_BOOK_TOLERANCE") or 0.03)
STAGE_DISPATCHED = os.environ.get("FREIGHT_STAGE_DISPATCHED_ID", "993307653")   # Carrier Informed and Dispatched
EARLY_STAGES = {fq.STAGE_QRR, os.environ.get("FREIGHT_STAGE_QS_ID", "993307651")}
LOCK, LOCK_MIN = "Working", 6
ACTIONS = ("Create BOL only", "Create BOL + dispatch", "Dispatch now", "Cancel shipment")
# Only these HubSpot users may approve a booking (Bob 2026-10-17: Bob + Michae). Set FREIGHT_APPROVER_USER_IDS="" to let anyone approve.
APPROVER_USERS = {x.strip(): n for x, n in zip((os.environ.get("FREIGHT_APPROVER_USER_IDS", "65054396,79374585")).split(","), ("Bob", "Michae", "Approver 3", "Approver 4")) if x.strip()}
APPROVER_OWNERS = [x.strip() for x in os.environ.get("FREIGHT_APPROVER_OWNER_IDS", "1640189485,79374585").split(",") if x.strip()]
APP_ID = os.environ.get("HUBSPOT_APP_ID", "41703642")
PROPS = fq.DEAL_PROPS + ["freight_shipment_action", "freight_booking_approval", "freight_booking_status", "freight_quote_request",
                         "bill_of_lading____bol_", "pickup_confirmation", "pro", "requested_pickup_date", "freight_accessorials",
                         "freight_recommended_carrier"]


# ---------------------------------------------------------------- SOAP
def _el(tag, val):
    return "" if val in (None, "") else f"<a:{tag}>{escape(str(val))}</a:{tag}>"


def _auth():
    fq_env = fq.env
    return ("<a:Authentication>" + _el("Mode", MODE) + _el("Password", fq_env("ACCUFRATE_PASS"))
            + _el("UserName", fq_env("ACCUFRATE_USER")) + "</a:Authentication>")


def _post(op, inner):
    env = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
           f'<{op} xmlns="{NS_T}">{inner}</{op}></s:Body></s:Envelope>')
    r = urllib.request.Request(ENDPOINT, data=env.encode("utf-8"), method="POST",
                               headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{NS_T}IPickupService/{op}"'})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            txt = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
    root = ET.fromstring(txt)
    res = root.find(f".//{{{NS_T}}}{op}Result")
    if res is None:
        f = root.find(".//faultstring")
        return {"ok": False, "errors": [f.text if f is not None else "no result from accuFRATE"]}
    out = {c.tag.split("}")[1]: (c.text or "").strip() for c in res if len(c) == 0}
    out["errors"] = [e.text.strip() for e in res.iter(f"{{{NS_ARR}}}string") if e.text and e.text.strip()]
    out["ok"] = (out.get("IsRequestValid") or out.get("IsValid") or "").lower() == "true"
    return out


def create_shipment(b):
    """b = dict of BOLRequest fields (python names = accuFRATE names). Elements go in data-contract (alphabetical) order."""
    items = "".join("<a:FreightItem>" + "".join(_el(k, it.get(k)) for k in
                    ("ClassCode", "Description", "Height", "ItemTypeCode", "Length", "NMFCCode", "Quantity", "Reference", "Weight", "Width"))
                    + "</a:FreightItem>" for it in b["FreightItems"])
    order = ["ConsigneeAddress1", "ConsigneeAddress2", "ConsigneeCompany", "ConsigneeContact", "ConsigneeEmail", "ConsigneePhone",
             "FreightItems", "IsPickUpRequired", "PONumber", "PickUpCloseTime", "PickUpDate", "PickUpReadyTime", "QuoteDetailId",
             "ReferenceNumber", "ReferenceNumber2", "ReferenceNumber3", "ReferenceNumber4", "ReferenceNumber5",
             "ShipperAddress1", "ShipperAddress2", "ShipperCompany", "ShipperContact", "ShipperEmail", "ShipperNumber", "ShipperPhone",
             "SpecialInstructions"]
    body = _auth() + "".join(f"<a:FreightItems>{items}</a:FreightItems>" if k == "FreightItems" else _el(k, b.get(k)) for k in order)
    return _post("CreateShipment", f'<request xmlns:a="{NS_DC}" xmlns:b="{NS_ARR}">{body}</request>')


def dispatch_shipment(bol, pickup_iso, ready, close, phone):
    body = (_auth() + _el("BolID", bol) + _el("PickUpCloseTime", close) + _el("PickUpDate", pickup_iso)
            + _el("PickUpReadyTime", ready) + _el("ShipperPhone", phone))
    return _post("DispatchShipment", f'<request xmlns:a="{NS_DC}">{body}</request>')


def cancel_shipment(bol):
    inner = (f"<bolId>{int(bol)}</bolId><username>{escape(fq.env('ACCUFRATE_USER'))}</username>"
             f"<password>{escape(fq.env('ACCUFRATE_PASS'))}</password>")
    return _post("CancelShipment", inner)


# ---------------------------------------------------------------- helpers
def norm_time(s):
    """'9' / '9am' / '9:30 am' / '1630' / '16:30' -> 'HH:MM' (24 h), or '' if unreadable."""
    t = (s or "").strip().lower().replace(".", "")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm|a|p)?", t)
    if not m:
        return ""
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap and ap.startswith("p") and h < 12:
        h += 12
    if ap and ap.startswith("a") and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else ""


def pickup_date(p):
    v = (p.get("requested_pickup_date") or "").strip()
    if not v:
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).date() if v.isdigit() else date.fromisoformat(v[:10])
    except ValueError:
        return None


def next_business_day(d=None):
    d = (d or date.today()) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def bol_number(p):
    m = re.search(r"\d+", p.get("bill_of_lading____bol_") or "")
    return m.group(0) if m else ""


def g(p, key):
    return (p.get(fq.booking_prop(key)) or "").strip()


def _now():
    return datetime.now(timezone.utc)


def _stamp():  # team's local time (Vercel runs in UTC)
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(os.environ.get("FREIGHT_TZ", "America/Halifax"))).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- main entry
def approved_by(deal_id):
    """Who set Booking Approval = Approved last: (name, None) if allowed, (None, reason) if not."""
    if not APPROVER_USERS:
        return "anyone", None
    st, d = fq.hs("GET", f"/crm/v3/objects/deals/{deal_id}?propertiesWithHistory=freight_booking_approval")
    hist = ((d.get("propertiesWithHistory") or {}).get("freight_booking_approval") or []) if st == 200 else []
    last = hist[0] if hist else {}
    if last.get("value") != "Approved":
        return None, "Booking Approval is not Approved"
    if last.get("sourceType") == "INTEGRATION" and str(last.get("sourceId")) == APP_ID:
        return "XpressQuote page (approver PIN)", None   # only the page sets it this way, after an approver PIN
    uid = str(last.get("updatedByUserId") or "")
    if uid in APPROVER_USERS:
        return APPROVER_USERS[uid], None
    return None, "only " + " or ".join(APPROVER_USERS.values()) + " can approve bookings"


def book(deal_id, dry=False, approver=None):
    st, d = fq.hs("GET", f"/crm/v3/objects/deals/{deal_id}?properties={','.join(PROPS)}")
    if st != 200:
        return {"deal": deal_id, "error": f"deal not found ({st})"}
    p = d["properties"]
    action, approval = p.get("freight_shipment_action") or "", p.get("freight_booking_approval") or ""
    if p.get("pipeline") != fq.PIPELINE:
        return {"deal": deal_id, "skipped": "not a Freight Pipeline deal"}
    if action not in ACTIONS:
        return {"deal": deal_id, "skipped": "no Shipment Action"}
    if approval != "Approved":
        return {"deal": deal_id, "skipped": f"Booking Approval is '{approval or 'blank'}', not Approved"}
    if not approver:
        approver, why = approved_by(deal_id)
        if not approver:
            if not dry:
                fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": {
                    "freight_booking_approval": "Pending", "freight_booking_status": f"NOT BOOKED {_stamp()}: {why}. Approval set back to Pending."}})
                fq.add_note(deal_id, f"<p><strong>Booking not sent:</strong> {why}. Booking Approval was set back to Pending; "
                                     f"Shipment Action ({action}) is still set, so it books as soon as an approver approves.</p>")
            return {"deal": deal_id, "skipped": why}
    status = p.get("freight_booking_status") or ""
    if status.startswith(LOCK):
        try:
            if (_now() - datetime.fromisoformat(status.split("|")[1].strip())).total_seconds() < LOCK_MIN * 60:
                return {"deal": deal_id, "skipped": "already working on it"}
        except Exception:
            pass
    owner = p.get("hubspot_owner_id") or fq.BOB
    qnum = (p.get("inxpress_quote_number_140") or "").strip() or deal_id
    if not dry:
        fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}",
              {"properties": {"freight_booking_status": f"{LOCK} | {_now().isoformat(timespec='seconds')}"}})
    try:
        if action == "Dispatch now":
            out = do_dispatch(deal_id, p, qnum, dry)
        elif action == "Cancel shipment":
            out = do_cancel(deal_id, p, qnum, dry)
        else:
            out = do_create(deal_id, p, qnum, action == "Create BOL + dispatch", dry)
    except Exception as e:
        out = {"fail": f"{type(e).__name__}: {e}"}
    out["approver"] = approver
    return finish(deal_id, p, owner, qnum, action, out, dry)


def finish(deal_id, p, owner, qnum, action, out, dry):
    """Write the result: fields, status, note, task; always clear Shipment Action."""
    test = " (TEST MODE - nothing real was booked)" if MODE == "T" else ""
    props = dict(out.get("props") or {})
    props["freight_shipment_action"] = ""
    props["freight_booking_approval"] = ""   # every booking action needs its own approval
    if out.get("fail"):
        props["freight_booking_status"] = f"FAILED {_stamp()}: {out['fail']}"[:250]
        task = (f"Booking problem ({action}) - {qnum}",
                f"{action} did not go through{test}:\n{out['fail']}\n\nFix the deal fields, then pick the Shipment Action again.")
    else:
        props["freight_booking_status"] = (out["status"] + (f" (approved by {out['approver']})" if out.get("approver") else "") + test)[:250]
        task = out.get("task")
    result = {"deal": deal_id, "action": action, "mode": MODE, "ok": not out.get("fail"), "status": props["freight_booking_status"],
              "set": props, "task": task[0] if task else None}
    if dry:
        result["dry_run"] = True
        return result
    fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
    if out.get("note"):
        fq.add_note(deal_id, out["note"] + (f"<p><em>{test.strip(' ()')}</em></p>" if test else ""))
    if task:
        fq.add_task(deal_id, owner, task[0], task[1] + (f"\n{test.strip()}" if test else ""))
    try:
        import freight_page
        freight_page._close_approval_tasks(deal_id)   # the "Approve booking" tasks are done either way
    except Exception:
        pass
    return result


def missing_fields(p, dispatch):
    need = [("shipper_company", "Shipper Company"), ("shipper_address", "Shipper Address"),
            ("consignee_company", "Consignee Company"), ("consignee_address", "Consignee Address"), ("commodity", "Commodity")]
    if dispatch:
        need += [("shipper_contact", "Shipper Contact"), ("shipper_phone", "Shipper Phone"), ("shipper_email", "Shipper Email")]
    miss = [label for k, label in need if not g(p, k)]
    if dispatch:
        miss += dispatch_problems(p)
    return miss


def dispatch_problems(p):
    out = []
    pd = pickup_date(p)
    ready, close = norm_time(g(p, "pickup_ready_time")), norm_time(g(p, "pickup_close_time"))
    if not pd:
        out.append("Requested Pickup Date")
    elif pd < date.today():
        out.append("Requested Pickup Date (it is in the past)")
    if not ready:
        out.append("Pickup Ready Time (e.g. 09:00)")
    if not close:
        out.append("Pickup Close Time (e.g. 16:30)")
    if ready and close and close <= ready:
        out.append("Pickup Close Time must be after the Ready Time")
    return out


def requote(deal_id, p):
    """Same-day re-quote of the saved shipment; returns (row, option, res) or raises ValueError with a plain message."""
    raw = p.get("freight_quote_request") or ""
    if not raw:
        raise ValueError("This deal has no saved quote data (it was priced before booking existed, or by hand). "
                         "Clear Quote Route to re-price it, or book this one in the accuFRATE portal.")
    saved = json.loads(raw)
    req = dict(saved["request"])
    pd = pickup_date(p)
    req["pickup_date"] = (pd if pd and pd >= date.today() else next_business_day()).isoformat()
    n = int(float(p.get("freight_use_option") or 1))
    opts = saved.get("options") or []
    if not 1 <= n <= len(opts):
        raise ValueError(f"Use Option # is {n} but the cost table has {len(opts)} options.")
    opt = opts[n - 1]
    if opt.get("source") == fq.DR_SOURCE:
        raise ValueError(f"This rate is from the Day & Ross portal. Book it there (account {fq.DR_ACCOUNT}), then enter the BOL / PRO # on the deal.")
    code, res, log = fq.run_quote(req, MODE, deal_id)
    if res is None:
        raise ValueError("accuFRATE re-quote failed: " + log.strip()[-300:])
    same = [r for r in res["standard"] + res["guaranteed"]
            if r["carrier"] == opt["carrier"] and r["source"] == opt["source"] and r["service"] == opt["service"]]
    if not same:
        raise ValueError(f"{opt['carrier']} ({opt['source'] or 'accuFRATE'}) did not return a price on today's re-quote "
                         f"(accuFRATE quote #{res['quote_id']}). Pick another Use Option # or book in the portal.")
    row = min(same, key=lambda r: r["cost_cad"])
    return row, opt, res


def do_create(deal_id, p, qnum, dispatch, dry):
    if bol_number(p):
        return {"fail": f"this deal already has BOL {p['bill_of_lading____bol_']}. Use 'Dispatch now' or 'Cancel shipment'."}
    miss = missing_fields(p, dispatch)
    if miss:
        return {"fail": "missing or wrong: " + ", ".join(miss)}
    try:
        row, opt, res = requote(deal_id, p)
    except ValueError as e:
        return {"fail": str(e)}
    old, new = float(opt["cost_cad"]), float(row["cost_cad"])
    if new > old * (1 + PRICE_TOLERANCE):
        return {"fail": f"price went up: {opt['carrier']} was C${old:,.2f}, today C${new:,.2f} (+{(new / old - 1) * 100:.1f}%, "
                        f"limit {PRICE_TOLERANCE * 100:g}%). Not booked. Check the margin/price with the customer, then re-price or "
                        f"pick another Use Option #, and choose the Shipment Action again."}
    pd = pickup_date(p) or next_business_day()
    ready, close = norm_time(g(p, "pickup_ready_time")), norm_time(g(p, "pickup_close_time"))
    refs = [r.strip() for r in re.split(r"[,;\n]", g(p, "reference_numbers")) if r.strip()][:4]
    acc = res.get("accessorials") or []
    instr = "; ".join(x for x in [g(p, "special_instructions"), ("Services booked: " + ", ".join(acc)) if acc else ""] if x)
    commodity = g(p, "commodity")
    items = [{"ClassCode": it["class"], "Description": commodity[:255], "Height": it["height"], "ItemTypeCode": it.get("item_type", 11),
              "Length": it["length"], "Quantity": it["quantity"], "Weight": int(round(it["weight"])), "Width": it["width"]}
             for it in res["items_priced"]]
    b = {"QuoteDetailId": row["quote_detail_id"], "ShipperNumber": qnum, "PONumber": (p.get("customer_po") or "").strip(),
         "ReferenceNumber": qnum, **{f"ReferenceNumber{i}": r for i, r in enumerate(refs, 2)},
         "ShipperCompany": g(p, "shipper_company"), "ShipperAddress1": g(p, "shipper_address"), "ShipperAddress2": g(p, "shipper_address2"),
         "ShipperContact": g(p, "shipper_contact"), "ShipperPhone": g(p, "shipper_phone"), "ShipperEmail": g(p, "shipper_email"),
         "ConsigneeCompany": g(p, "consignee_company"), "ConsigneeAddress1": g(p, "consignee_address"),
         "ConsigneeAddress2": g(p, "consignee_address2"), "ConsigneeContact": g(p, "consignee_contact"),
         "ConsigneePhone": g(p, "consignee_phone"), "ConsigneeEmail": g(p, "consignee_email"),
         "FreightItems": items, "IsPickUpRequired": "true" if dispatch else "false", "SpecialInstructions": instr[:8000]}
    if dispatch:
        b.update({"PickUpDate": pd.isoformat() + "T00:00:00.000", "PickUpReadyTime": ready, "PickUpCloseTime": close})
    summary = (f"{row['carrier']} via {row['source'] or 'accuFRATE'} - {row['cost']:,.2f} {row['cur']} (C${new:,.2f}; quoted C${old:,.2f}), "
               f"accuFRATE quote #{res['quote_id']}")
    if dry:
        return {"status": f"DRY RUN OK {_stamp()}: would book {summary}", "props": {}, "bol_request": {k: v for k, v in b.items() if k != "FreightItems"}}
    r = create_shipment(b)
    if not r["ok"]:
        return {"fail": "accuFRATE refused the BOL: " + "; ".join(r["errors"] or ["no reason given"])}
    bol = r.get("BolID") or ""
    pick, pro = r.get("PickUpNumber") or r.get("PickupNumber") or "", r.get("PRONumber") or ""
    import freight_carriers
    cname, _ = freight_carriers.canonical(row["carrier"])
    props = {"bill_of_lading____bol_": ("TEST " if MODE == "T" else "") + bol, "pickup_confirmation": pick, "pro": pro,
             "freight_accufrate_quote_id": str(res["quote_id"]), "carrier_quote_number": row.get("carrier_quote_no") or "",
             "requested_pickup_date": fq.date_ms(pd.isoformat())}
    if abs(new - old) >= 0.01:
        props["cost_cad"] = f"{new:.2f}"
    if not p.get("carriers___canada_freight"):   # the HubSpot tracking-link workflow keys on this: Accufrate -> accuFRATE tracking by BOL
        props["carriers___canada_freight"] = "Accufrate"
    if dispatch and pick and MODE != "T" and (p.get("dealstage") in EARLY_STAGES):
        props["dealstage"] = STAGE_DISPATCHED
    props = {k: v for k, v in props.items() if v not in (None, "")}
    try:  # attach the BOL PDF to the deal (needs the HubSpot 'files' scope; never blocks the booking)
        import freight_bol_intake
        docs, doc_problems = freight_bol_intake.attach_docs(deal_id, bol, freight_bol_intake.BOOKED_DOCS, test=MODE == "T")
    except Exception as e:
        docs, doc_problems = [], [f"{type(e).__name__}: {e}"]
    did = "BOL + pickup booked" if dispatch and pick else ("BOL created, PICKUP NOT CONFIRMED" if dispatch else "BOL created (not dispatched)")
    status = f"{did} {_stamp()}: BOL {bol}" + (f", pickup # {pick}" if pick else "") + (f", PRO {pro}" if pro else "")
    note = (f"<p><strong>{did} &ndash; quote {qnum}</strong></p><ul><li>BOL #: {bol}</li>"
            + (f"<li>Pickup confirmation #: {pick}</li>" if pick else "") + (f"<li>PRO #: {pro}</li>" if pro else "")
            + f"<li>Carrier: {escape(summary)}</li>"
            + (f"<li>COST CHANGED: C${old:,.2f} &rarr; C${new:,.2f}; Cost CAD updated, Amount (price) NOT changed - check the margin.</li>" if abs(new - old) >= 0.01 else "")
            + f"<li>Pickup {pd.isoformat()}" + (f" {ready}-{close}" if dispatch else "") + f" from {escape(g(p, 'shipper_company'))}, "
            f"to {escape(g(p, 'consignee_company'))}</li><li>References: {escape(', '.join([qnum] + refs))}"
            + (f"; PO {escape(p.get('customer_po') or '')}" if p.get("customer_po") else "") + "</li></ul>")
    if dispatch and not pick:
        task = (f"PICKUP NOT CONFIRMED - BOL {bol} - {qnum}", f"The BOL was created but the carrier gave no pickup number. "
                f"Call {row['carrier']} or use 'Dispatch now'.\n{summary}")
    else:
        task = (f"Send BOL {bol} to the customer - {qnum}", f"{did}. On the deal: Email -> Templates -> 'Booked Shipment - BOL and Pickup (XpressQuote)' "
                f"(pickup date and window, carrier, BOL #, pickup #, PRO #, PO #, SO #, references and tracking fill in) -> attach the BOL "
                f"from the deal's Attachments -> Send.\n{summary}" + ("" if dispatch else "\nNot dispatched yet: choose 'Dispatch now' when ready."))
    if doc_problems:
        note += "<p>BOL PDF not attached: " + escape("; ".join(doc_problems)) + "</p>"
    return {"status": status, "props": props, "note": note, "task": task}


def do_dispatch(deal_id, p, qnum, dry):
    bol = bol_number(p)
    if not bol:
        return {"fail": "no BOL # on the deal. Use 'Create BOL + dispatch'."}
    if (p.get("bill_of_lading____bol_") or "").upper().startswith("TEST") and MODE != "T":
        return {"fail": f"BOL {p['bill_of_lading____bol_']} was created in test mode and can't be dispatched live."}
    if p.get("pickup_confirmation"):
        return {"fail": f"already dispatched (pickup # {p['pickup_confirmation']})."}
    miss = dispatch_problems(p) + (["Shipper Phone"] if not g(p, "shipper_phone") else [])
    if miss:
        return {"fail": "missing or wrong: " + ", ".join(miss)}
    pd = pickup_date(p)
    ready, close = norm_time(g(p, "pickup_ready_time")), norm_time(g(p, "pickup_close_time"))
    if dry:
        return {"status": f"DRY RUN OK {_stamp()}: would dispatch BOL {bol} for {pd} {ready}-{close}", "props": {}}
    r = dispatch_shipment(bol, pd.isoformat() + "T00:00:00.000", ready, close, g(p, "shipper_phone"))
    if not r["ok"]:
        return {"fail": "accuFRATE refused the dispatch: " + "; ".join(r["errors"] or ["no reason given"])}
    pick, pro = r.get("PickUpNumber") or "", r.get("PRONumber") or ""
    props = {"pickup_confirmation": pick, "pro": pro or p.get("pro") or ""}
    if MODE != "T" and p.get("dealstage") in EARLY_STAGES:
        props["dealstage"] = STAGE_DISPATCHED
    status = f"Dispatched {_stamp()}: BOL {bol}, pickup # {pick or '-'}" + (f", PRO {pro}" if pro else "")
    return {"status": status, "props": {k: v for k, v in props.items() if v},
            "note": f"<p><strong>Pickup dispatched &ndash; quote {qnum}</strong></p><ul><li>BOL {bol}</li><li>Pickup # {pick or '-'}</li>"
                    + (f"<li>PRO {pro}</li>" if pro else "") + f"<li>{pd} {ready}-{close}</li></ul>",
            "task": (f"Confirm pickup with the customer - BOL {bol} - {qnum}", status)}


def do_cancel(deal_id, p, qnum, dry):
    bol = bol_number(p)
    if not bol:
        return {"fail": "no BOL # on the deal - nothing to cancel."}
    if MODE == "T" or (p.get("bill_of_lading____bol_") or "").upper().startswith("TEST"):
        return {"status": f"Cancel NOT sent {_stamp()}: accuFRATE cancel has no test mode (BOL {bol})"}
    if dry:
        return {"status": f"DRY RUN OK {_stamp()}: would cancel BOL {bol}", "props": {}}
    r = cancel_shipment(bol)
    if not r["ok"]:
        return {"fail": "accuFRATE refused the cancel: " + "; ".join(r["errors"] or ["no reason given"])}
    status = f"CANCELLED {_stamp()}: BOL {bol}" + (f" ({r.get('Status')})" if r.get("Status") else "")
    return {"status": status, "note": f"<p><strong>Shipment cancelled &ndash; quote {qnum}</strong></p><p>BOL {bol}. "
                                      f"Carrier {escape(r.get('Carrier') or '')} {escape(r.get('CarrierPhone') or '')}</p>",
            "task": (f"Shipment cancelled - BOL {bol} - {qnum}", "Tell the customer, then move the deal to Closed Lost "
                     "(a re-booking is a new deal with a re-run quote).")}


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(1)
    print(json.dumps(book(a[0], "--dry-run" in a), indent=1, default=str)[:6000])
