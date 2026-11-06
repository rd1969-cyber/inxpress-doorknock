"""New freight request page (inxpress-sales.vercel.app/freight): see the quote, switch carrier, change margin/price, and book.

Booking from the page (Bob, 2026-10-17): anyone with the team password can REQUEST a booking (fields saved, Shipment Action set,
Booking Approval = Pending, tasks to the approvers). Only an approver can APPROVE: with their own PIN on the page
(Vercel env FREIGHT_APPROVER_PIN_BOB / FREIGHT_APPROVER_PIN_MICHAE, set by Bob) or by setting Booking Approval = Approved in
HubSpot (freight_book.approved_by checks who did it). Approving runs freight_book.book right away.
"""
import hmac
import html as _html
import json
import os
import re
import time
from datetime import date

import freight_queue as fq

PORTAL = os.environ.get("HUBSPOT_PORTAL_ID", "47829927")
STAGE_QS = os.environ.get("FREIGHT_STAGE_QS_ID", "993307651")
REQUESTABLE = ("Create BOL only", "Create BOL + dispatch")
EDITABLE = [k for k in fq.BOOKING_KEYS]   # booking fields the form may write
VIEW_PROPS = sorted(set(fq.DEAL_PROPS + [
    "freight_quote_request", "freight_margin_note", "freight_booking_status", "freight_booking_approval", "freight_shipment_action",
    "bill_of_lading____bol_", "pickup_confirmation", "pro", "requested_pickup_date", "freight_recommended_carrier", "freight_carrier",
    "freight_di_percent", "freight_missing_info", "freight_carrier_requirements", "freight_accessorials", "createdate"]))


def approver_pins():
    """{name: pin} from env; PINs shorter than 4 characters are ignored."""
    out = {}
    for k, v in os.environ.items():
        if k.startswith("FREIGHT_APPROVER_PIN_") and len((v or "").strip()) >= 4:
            out[k[len("FREIGHT_APPROVER_PIN_"):].title()] = v.strip()
    return out


def check_pin(pin):
    pin = (pin or "").strip()
    for name, real in approver_pins().items():
        if pin and hmac.compare_digest(pin, real):
            return name
    time.sleep(1.5)   # slow down guessing
    return None


def _url(deal_id):
    return f"https://app.hubspot.com/contacts/{PORTAL}/record/0-3/{deal_id}"


def _get(deal_id):
    st, d = fq.hs("GET", f"/crm/v3/objects/deals/{deal_id}?properties={','.join(VIEW_PROPS)}&associations=companies")
    if st != 200:
        raise ValueError(f"Deal {deal_id} not found")
    if d["properties"].get("pipeline") != fq.PIPELINE:
        raise ValueError("That deal is not in the Freight Pipeline")
    return d


def option_rows(saved):
    """Structured version of fq.carrier_options_text: cost, DI, days and price for each option."""
    import freight_carriers
    opts = saved.get("options") or []
    try:
        di_map = freight_carriers.di_by_carrier(fq.hs)
    except Exception:
        di_map = {}
    plan, m, sel = saved.get("plan"), saved.get("margin"), int(saved.get("selected", 1) or 0)   # 0 = waiting for the D&R direct rate
    rows = []
    for o in opts[:10]:
        cname, cid = freight_carriers.canonical(o["carrier"])
        di = di_map.get(cname) if cid and o.get("source") != fq.DR_SOURCE else None
        cost_true = float(o["cost_cad"]) * (1 + (di or 0) / 100)
        price = None
        if plan and plan.get("pct") is not None:
            price = fq.price_from_plan(cost_true, plan)
        elif m:
            price = round(cost_true / (1 - float(m)), 2)
        rows.append({"n": o["n"], "carrier": o["carrier"], "source": o.get("source") or "", "cost": float(o["cost"]),
                     "cur": (o.get("cur") or "CAD").rstrip("?"), "cost_cad": float(o["cost_cad"]), "di": di, "days": o.get("days") or "",
                     "price": price, "selected": o["n"] == sel})
    return rows


def view(deal_id, fill=False):
    """Everything the page shows for one deal."""
    d = _get(deal_id)
    p = d["properties"]
    if fill and not ((p.get("freight_shipper_company") or "").strip() and (p.get("freight_consignee_company") or "").strip()):
        try:
            import freight_auto
            freight_auto.fill_booking(d)
            d = _get(deal_id)
            p = d["properties"]
        except Exception:
            pass
    saved = {}
    try:
        saved = json.loads(p.get("freight_quote_request") or "{}")
    except ValueError:
        pass
    req = saved.get("request") or {}
    m = p.get("margin_on_freight_shipment")
    opts = option_rows(saved) if saved.get("options") else []
    sel = next((o for o in opts if o["selected"]), None)
    dr_row = next((o for o in (saved.get("options") or []) if o.get("source") == fq.DR_SOURCE), None)
    return {
        "dr_direct": saved.get("dr_direct") or "", "dr_portal": fq.DR_PORTAL, "dr_account": fq.DR_ACCOUNT, "dr_copy": saved.get("dr_copy") or "",
        "dr_rate": {k: dr_row.get(k) for k in ("cost_cad", "days", "quote_no")} if dr_row else None,
        "dr_booking": bool(sel and sel["source"] == fq.DR_SOURCE),
        "deal_id": deal_id, "dealname": p.get("dealname"), "url": _url(deal_id), "quote": p.get("inxpress_quote_number_140"),
        "route": p.get("freight_quote_route") or "", "route_reason": p.get("freight_route_reason") or "",
        "missing": p.get("freight_missing_info") or "",
        "lane": f"{req.get('from_zip', '?')} → {req.get('to_zip', '?')}" if req else "",
        "items": req.get("items") or [], "accessorials": req.get("accessorials") or [],
        "options": opts, "can_book": bool(sel) and sel["source"] != fq.DR_SOURCE,
        "cost_cad": p.get("cost_cad"), "amount": p.get("amount"),
        "margin_pct": round(float(m) * 100, 2) if m not in (None, "") else None, "margin_note": p.get("freight_margin_note") or "",
        "booking": {k: p.get(fq.booking_prop(k)) or "" for k in EDITABLE},
        "pickup_date": (p.get("requested_pickup_date") or "")[:10],
        "action": p.get("freight_shipment_action") or "", "approval": p.get("freight_booking_approval") or "",
        "status": p.get("freight_booking_status") or "",
        "bol": p.get("bill_of_lading____bol_") or "", "pickup_no": p.get("pickup_confirmation") or "", "pro": p.get("pro") or "",
        "approvers": list(approver_pins()) or ["Bob", "Michae"],
        "tasks": open_tasks(deal_id),
    }


def open_tasks(deal_id):
    """Open tasks on the deal (send the quote, ask for missing info, RFQs, approvals) for the page's Next steps box."""
    st, r = fq.hs("GET", f"/crm/v4/objects/deals/{deal_id}/associations/tasks?limit=30")
    ids = [str(t["toObjectId"]) for t in (r.get("results") or [] if st == 200 else [])]
    if not ids:
        return []
    st, b = fq.hs("POST", "/crm/v3/objects/tasks/batch/read", {"inputs": [{"id": i} for i in ids],
                                                                "properties": ["hs_task_subject", "hs_task_body", "hs_task_status", "hs_timestamp"]})
    out = []
    for t in (b.get("results") or [] if st in (200, 207) else []):
        p = t.get("properties") or {}
        if p.get("hs_task_status") == "COMPLETED":
            continue
        body = _html.unescape(re.sub(r"<[^>]+>", " ", p.get("hs_task_body") or ""))
        out.append({"id": t["id"], "subject": p.get("hs_task_subject") or "", "body": re.sub(r"\s+", " ", body).strip()[:400],
                    "due": (p.get("hs_timestamp") or "")[:10]})
    return sorted(out, key=lambda x: x["due"])


def set_option(deal_id, n):
    import freight_auto
    saved = json.loads(_get(deal_id)["properties"].get("freight_quote_request") or "{}")
    if saved.get("dr_direct") == "waiting":
        raise ValueError("This customer is priced with Day & Ross direct: enter the D&R rate first. The accuFRATE rows are for reference only.")
    fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": {"freight_use_option": str(int(n))}})
    freight_auto.option_changed(deal_id)
    return view(deal_id)


def set_direct_rate(deal_id, cost_cad, days, dr_quote_no, who):
    """The rate from the Day & Ross portal (D&R-direct customers, design 'Big Erics DR Direct'): it becomes option 1 and is selected,
    the customer's margin rule sets Amount (a price typed by hand is kept), the 'Get Day & Ross direct rate' task closes and the
    'Quote Review' task is created. Entering it again replaces the D&R row."""
    import freight_carriers
    try:
        cost = round(float(str(cost_cad).replace("$", "").replace(",", "")), 2)
    except ValueError:
        raise ValueError("Enter the all-in cost in CAD, e.g. 162.44")
    if not 0 < cost < 20000:
        raise ValueError("The cost must be above C$0 and under C$20,000")
    days = str(days or "").strip()
    if days and not (days.isdigit() and 1 <= int(days) <= 15):
        raise ValueError("Transit days must be a whole number from 1 to 15 (or blank)")
    qno = str(dr_quote_no or "").strip()[:60]
    d = _get(deal_id)
    p = d["properties"]
    saved = json.loads(p.get("freight_quote_request") or "{}")
    company, _ = fq.company_margin_text(d)
    if not (saved.get("dr_direct") or fq.dr_direct_customer(company or p.get("dealname"))):
        raise ValueError("This customer isn't set up for Day & Ross direct pricing.")
    if not saved.get("options") and not saved.get("request"):
        raise ValueError("This deal has no saved quote yet. Wait for pricing to finish.")
    if (p.get("bill_of_lading____bol_") or "").strip():
        raise ValueError("This deal is already booked. Change the cost in HubSpot if needed.")
    first = saved.get("dr_direct") != "entered"
    row = {"n": 1, "carrier": "Day & Ross", "source": fq.DR_SOURCE, "service": "Direct", "cost": cost, "cur": "CAD", "cost_cad": cost,
           "days": days, "quote_no": qno}
    opts = ([row] + [o for o in saved.get("options") or [] if o.get("source") != fq.DR_SOURCE])[:10]
    for i, o in enumerate(opts, 1):
        o["n"] = i
    amt_now = (p.get("amount") or "").strip()
    auto = saved.get("auto_amount")
    typed = bool(amt_now.strip("0.")) and (auto is None or abs(float(amt_now) - float(auto)) > 0.01)
    plan, m = saved.get("plan"), saved.get("margin") or p.get("margin_on_freight_shipment")
    new_amt = None
    if plan and plan.get("pct") is not None:
        new_amt = fq.price_from_plan(cost, plan)
    elif m:
        new_amt = round(cost / (1 - float(m)), 2)
    saved.update({"options": opts, "selected": 1, "dr_direct": "entered"})
    props = {"cost_cad": f"{cost:.2f}", "cost_usd": "", "freight_quote_currency": "CAD", "freight_carrier": "Day & Ross",
             "freight_recommended_carrier": f"Day & Ross ({fq.DR_SOURCE})", "freight_transit_days": days, "carrier_quote_number": qno,
             "freight_di_percent": "", "freight_booked_through": "Carrier Direct", "freight_use_option": "1", "freight_route_reason": ""}
    if new_amt is not None and not typed:
        props["amount"] = f"{new_amt:.2f}"
        saved["auto_amount"] = props["amount"]
    if plan and plan.get("pct") is not None and not p.get("margin_on_freight_shipment"):
        props["margin_on_freight_shipment"] = str(plan["pct"])
    summ = p.get("freight_quote_summary") or ""
    if summ and days:
        props["freight_quote_summary"] = re.sub(r"Transit: about [^<\n]*", f"Transit: about {days} business days from pickup", summ)
    props["freight_quote_request"] = json.dumps(saved, separators=(",", ":"))
    try:
        props["freight_carrier_options"] = fq.carrier_options_text(saved)
        props.update(fq.option_fields(props["freight_carrier_options"]))
    except Exception:
        pass
    st, r = fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
    if st != 200:
        raise ValueError(f"Could not save the D&R rate ({st}): {r.get('message', '')[:200]}")
    qnum = p.get("inxpress_quote_number_140") or deal_id
    price = props.get("amount")
    by = who or "the team"
    fq.add_note(deal_id, f"<p><strong>Day &amp; Ross direct rate entered</strong> by {_html.escape(by)}: C${cost:,.2f} all-in"
                         + (f", {days} days" if days else "") + (f", D&amp;R quote # {_html.escape(qno)}" if qno else "") + ". "
                         + (f"Amount kept at C${float(amt_now):,.2f} (typed by hand): check the margin." if typed else
                            f"Price C${new_amt:,.2f} from the margin rule." if new_amt is not None else "No margin rule: set Amount.")
                         + " Book it in the Day &amp; Ross portal, not accuFRATE.</p>")
    _close_tasks(deal_id, "Get Day & Ross direct rate")
    if first and not any(t["subject"].startswith("Quote Review") for t in open_tasks(deal_id)):
        body = f"Day & Ross direct rate (from the D&R portal, entered by {by}): C${cost:,.2f} all-in" + (f", {days} days" if days else "") + ".\n"
        if new_amt is not None and not typed:
            body += f"Price C${new_amt:,.2f} from the margin rule: {(plan or {}).get('note') or ''}\n"
            if plan and plan.get("approval_over_cad") and new_amt > float(plan["approval_over_cad"]):
                body += f"APPROVAL NEEDED: price is over C${float(plan['approval_over_cad']):,.0f} (company rule).\n"
        body += "After the customer accepts, book it in the Day & Ross portal and put the BOL / PRO # on the deal.\n" + fq.SEND_HOWTO
        fq.add_task(deal_id, p.get("hubspot_owner_id") or fq.BOB, fq.review_subject(p, qnum), body)
    return view(deal_id)


def set_margin(deal_id, pct):
    import freight_auto
    pct = float(pct)
    if not 0 < pct < 90:
        raise ValueError("Margin must be between 0 and 90 %")
    fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": {"margin_on_freight_shipment": f"{pct / 100:g}"}})
    freight_auto.margin_changed(deal_id)
    return view(deal_id)


def set_amount(deal_id, amount):
    a = round(float(str(amount).replace("$", "").replace(",", "")), 2)
    if a <= 0:
        raise ValueError("Enter a price above 0")
    fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": {"amount": f"{a:.2f}"}})
    return view(deal_id)


def quotes(q=""):
    """Priced Freight Pipeline deals not yet booked (Quote Request Received / Quote Submitted), newest first."""
    body = {"filterGroups": [{"filters": [
        {"propertyName": "pipeline", "operator": "EQ", "value": fq.PIPELINE},
        {"propertyName": "dealstage", "operator": "IN", "values": [fq.STAGE_QRR, STAGE_QS]},
        {"propertyName": "freight_quote_request", "operator": "HAS_PROPERTY"}]}],
        "properties": ["dealname", "inxpress_quote_number_140", "createdate", "amount", "freight_booking_status", "freight_booking_approval"],
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}], "limit": 25}
    if (q or "").strip():
        body["query"] = q.strip()[:80]
    st, r = fq.hs("POST", "/crm/v3/objects/deals/search", body)
    return [{"deal_id": d["id"], "dealname": d["properties"].get("dealname"), "created": (d["properties"].get("createdate") or "")[:10],
             "amount": d["properties"].get("amount"), "status": d["properties"].get("freight_booking_status") or "",
             "approval": d["properties"].get("freight_booking_approval") or ""} for d in (r.get("results") or [] if st == 200 else [])]


def _save_booking(deal_id, fields, pickup_date):
    props = {}
    for k in EDITABLE:
        if k in (fields or {}):
            props[fq.booking_prop(k)] = str(fields[k] or "").strip()[:2000]
    if pickup_date:
        date.fromisoformat(pickup_date[:10])
        props["requested_pickup_date"] = pickup_date[:10]
    if props:
        st, r = fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
        if st != 200:
            raise ValueError(f"Could not save the booking details ({st}): {r.get('message', '')[:200]}")


def request_booking(deal_id, fields, pickup_date, action, who):
    """Anyone: save the form, check it, set Shipment Action + Booking Approval = Pending, task the approvers."""
    import freight_book as fb
    if action not in REQUESTABLE:
        raise ValueError("Pick BOL only or BOL + dispatch")
    _save_booking(deal_id, fields, pickup_date)
    d = _get(deal_id)
    p = d["properties"]
    if (p.get("bill_of_lading____bol_") or "").strip() and not (p.get("bill_of_lading____bol_") or "").startswith("TEST"):
        raise ValueError(f"This deal already has BOL {p['bill_of_lading____bol_']}. Use HubSpot to dispatch or cancel it.")
    sel = next((o for o in option_rows(json.loads(p.get("freight_quote_request") or "{}")) if o["selected"]), None)
    if not sel:
        raise ValueError("No carrier is chosen on this quote yet (Day & Ross direct rate not entered).")
    if sel["source"] == fq.DR_SOURCE:
        raise ValueError(f"This rate is from the Day & Ross portal. Book it there (account {fq.DR_ACCOUNT}), then put the BOL / PRO # on the deal.")
    miss = fb.missing_fields(p, action == "Create BOL + dispatch")
    if miss:
        return {"missing": miss, **view(deal_id)}
    stamp = fb._stamp()
    fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": {
        "freight_shipment_action": action, "freight_booking_approval": "Pending",
        "freight_booking_status": f"Waiting for approval: {action}, requested by {who or 'the team'} {stamp}"}})
    qnum = p.get("inxpress_quote_number_140") or deal_id
    what =f"{sel['carrier']} C${sel['cost_cad']:,.2f} cost" if sel else (p.get("freight_recommended_carrier") or "")
    for owner in fb.APPROVER_OWNERS:
        fq.add_task(deal_id, owner, f"Approve booking: {p.get('dealname') or qnum}",
                    f"{who or 'The team'} asked to book this shipment ({action}) with {what}.\n"
                    f"Approve: open the New freight request page > Book a quote > this deal > Approve and book (your PIN), "
                    f"or set Booking Approval = Approved on the deal.\n{_url(deal_id)}")
    fq.add_note(deal_id, f"<p><strong>Booking requested</strong> on the New freight request page by {who or 'the team'}: {action}"
                         f"{', ' + what if what else ''}. Waiting for approval.</p>")
    return view(deal_id)


def approve(deal_id, pin, fields=None, pickup_date="", action=""):
    """Approver PIN: (optionally save the form +) set Approved and book now. Returns the view with the result."""
    import freight_book as fb
    if not approver_pins():
        return {"pin_error": "No approver PINs are set up on the server yet (Vercel FREIGHT_APPROVER_PIN_BOB / _MICHAE, at least 4 characters, then deploy)."}
    name = check_pin(pin)
    if not name:
        return {"pin_error": f"That approver PIN is not right. (The server has PINs for: {', '.join(approver_pins())}.)"}
    if fields is not None or pickup_date:
        _save_booking(deal_id, fields, pickup_date)
    props = {"freight_booking_approval": "Approved"}
    if action in REQUESTABLE:
        props["freight_shipment_action"] = action
    fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
    res = fb.book(deal_id, approver=name)
    if not res.get("skipped"):
        fq.add_note(deal_id, f"<p>Booking approved on the New freight request page by <strong>{name}</strong> (approver PIN).</p>")
        _close_approval_tasks(deal_id)
    return {"book": {k: res.get(k) for k in ("ok", "status", "skipped", "error", "mode")}, "approver": name, **view(deal_id)}


def _close_approval_tasks(deal_id):
    _close_tasks(deal_id, "Approve booking")


def _close_tasks(deal_id, prefix):
    st, r = fq.hs("GET", f"/crm/v4/objects/deals/{deal_id}/associations/tasks?limit=50")
    for t in (r.get("results") or [] if st == 200 else []):
        tid = t["toObjectId"]
        s2, task = fq.hs("GET", f"/crm/v3/objects/tasks/{tid}?properties=hs_task_subject,hs_task_status")
        tp = task.get("properties", {}) if s2 == 200 else {}
        if (tp.get("hs_task_subject") or "").startswith(prefix) and tp.get("hs_task_status") != "COMPLETED":
            fq.hs("PATCH", f"/crm/v3/objects/tasks/{tid}", {"properties": {"hs_task_status": "COMPLETED"}})
