"""Freight queue: price new freight deals from their Description and write the results back to HubSpot.

Interim "queue mode" (until the Vercel engine exists): Claude reads each Description and writes a
parsed-request JSON; this tool does everything else.

  python freight_queue.py list                       # deals waiting: Freight Pipeline, Quote Request Received, not yet routed
  python freight_queue.py apply <deal_id> parsed.json [--mode C|T] [--dry-run]

parsed.json = the quote_cli request format, plus optional keys:
  "request_type": "Quote" | "Book now",  "missing": ["the weight of each skid", ...],
  "margin": 0.20 (fraction; set only when the company rule allows a simple %),  "margin_note": "why / approval needed"

What apply does:
  missing info   -> route "Missing information", Missing Information filled, task "Ask customer for missing details"
  > 6 skids / > 10 LF -> route "Volume / FTL - RFQ" + reason, task "Get volume/FTL quotes (RFQ)"; nothing sent to accuFRATE
  fits           -> accuFRATE priced (quote_cli), fields filled, cost-table note, task "Review quote <n>"
Never overwrites a value a person already set (quote number, margin, amount, Use option #).
Tokens: HUBSPOT_TOKEN / ACCUFRATE_* from the Windows user environment; never printed.
"""
import html
import json
import os
import re

import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.hubapi.com"
# Portal-specific settings (env vars for other franchises; defaults = franchise 140)
PIPELINE = os.environ.get("FREIGHT_PIPELINE_ID", "677348723")       # Freight Pipeline
STAGE_QRR = os.environ.get("FREIGHT_STAGE_ID", "993307648")        # Quote Request Received
BOB = os.environ.get("FREIGHT_DEFAULT_OWNER_ID", "1640189485")    # task owner when the deal has none
TASK_QUEUE = os.environ.get("FREIGHT_TASK_QUEUE_ID", "13519715")   # HubSpot task queue "Freight" ("" = none)
ACC_OPTIONS = ["Liftgate Delivery", "Liftgate Pickup", "Residential Delivery", "Residential Pickup", "Appointment",
               "Notification Prior To Delivery", "Inside Delivery", "Inside Pickup", "Limited Access Delivery",
               "Limited Access Pickup", "Hazmat", "Protect From Freezing", "Over 8 ft", "Over 12 ft", "Over 16 ft", "Over 20 ft",
               "Trade Show Delivery", "Construction Site Delivery", "School Delivery", "Farm Delivery"]
DOCK = ("Dock to dock (commercial address, shipping and receiving dock, standard hours). Unless specifically quoted, does not include: "
        "liftgate, residential, appointment/notification, inside pickup or delivery, limited-access locations, hazmat, protect from "
        "freezing, over-length (8 ft+), redelivery or storage.")
# Bob 2026-10-29: these customers are always priced with Day & Ross direct (their own D&R contract). accuFRATE still runs (places,
# class, linear feet, backup rates). Only the customer's OWN D&R tariff in accuFRATE is used; any other D&R price (InXpress tariff,
# ExFreight...) is reference only and the quote waits for the rate from the D&R portal, typed in on the /freight page
# (freight_page.set_direct_rate). Until the D&R API arrives.
# (pattern for the customer / deal name, pattern for the customer's own tariff description in accuFRATE)
DR_DIRECT_CUSTOMERS = [(r"big\s*eric", r"big\s*eric")]
DR_SOURCE = "D&R direct"           # source of a row typed in from the D&R portal (booked in the portal, never in accuFRATE)
DR_PORTAL = "https://dayross.com/login"
DR_ACCOUNT = os.environ.get("FREIGHT_DR_ACCOUNT", "153542")


def dr_direct_customer(name):
    """The customer's own-tariff pattern when this customer is priced with D&R direct, else None."""
    return next((tariff for pat, tariff in DR_DIRECT_CUSTOMERS if re.search(pat, name or "", re.I)), None)


def dr_direct_row(r, tariff):
    """An accuFRATE Day & Ross row priced on the customer's own D&R tariff (description matches `tariff`)."""
    return bool(re.search(r"day\s*(and|&)\s*ross", (r.get("carrier") or "") + " " + (r.get("tariff") or ""), re.I)
                and re.search(tariff, r.get("tariff") or "", re.I))


def env(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return ""


def hs(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + env("HUBSPOT_TOKEN"), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


DEAL_PROPS = ["dealname", "description", "hubspot_owner_id", "inxpress_quote_number_140", "margin_on_freight_shipment", "amount",
              "cost_cad", "cost_usd", "carrier_quote_number", "carriers___canada_freight",
              "freight_quote_route", "freight_route_reason", "freight_use_option", "dealstage", "pipeline", "customer_po"]
BOOKING_KEYS = ["shipper_company", "shipper_address", "shipper_address2", "shipper_contact", "shipper_phone", "shipper_email",
                "consignee_company", "consignee_address", "consignee_address2", "consignee_contact", "consignee_phone",
                "consignee_email", "pickup_ready_time", "pickup_close_time", "commodity", "po_number", "so_number", "reference_numbers",
                "special_instructions"]


def booking_prop(key):
    return "customer_po" if key == "po_number" else "freight_" + key


DEAL_PROPS += [booking_prop(k) for k in BOOKING_KEYS if k != "po_number"] + ["freight_carrier_requirements"]


def booking_props(parsed, p):
    """BOL details Claude read from the request -> deal fields, only where the deal field is still blank."""
    out = {}
    for k, v in ((parsed or {}).get("booking") or {}).items():
        v = (v or "").strip()
        if k in BOOKING_KEYS and v and not (p.get(booking_prop(k)) or "").strip():
            out[booking_prop(k)] = v[:2000]
    return out


def quote_request_json(parsed, res, extra=None):
    """What booking needs to re-quote later: the shipment as priced + the carrier options in table order."""
    keep = {k: parsed.get(k) for k in ("customer", "from_zip", "to_zip", "pickup_date", "accessorials", "items") if parsed.get(k) is not None}
    opts = [{"n": i, "carrier": r["carrier"], "source": r["source"], "service": r["service"], "cost": r["cost"], "cur": r["cur"],
             "cost_cad": r["cost_cad"], "days": r["days"], "quote_no": r.get("carrier_quote_no") or ""} for i, r in enumerate(res["standard"][:10], 1)]
    gua = [{"carrier": r["carrier"], "source": r["source"], "service": r["service"], "cost": r["cost"], "cur": r["cur"],
            "cost_cad": r["cost_cad"], "days": r["days"]} for r in res["guaranteed"][:3]]
    return json.dumps({"request": keep, "quote_id": res["quote_id"], "priced": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "options": opts, "guaranteed": gua, **(extra or {})}, separators=(",", ":"))


def get_deal(deal_id):
    st, d = hs("GET", f"/crm/v3/objects/deals/{deal_id}?properties={','.join(DEAL_PROPS)}&associations=companies,contacts")
    if st != 200:
        sys.exit(f"Deal {deal_id} not found: {st} {d.get('message', '')}")
    return d


def company_margin_text(deal):
    comps = deal.get("associations", {}).get("companies", {}).get("results", [])
    if not comps:
        return None, None
    cid = comps[0]["id"]
    st, c = hs("GET", f"/crm/v3/objects/companies/{cid}?properties=name,standard_freight_margin")
    p = c.get("properties", {}) if st == 200 else {}
    return p.get("name"), p.get("standard_freight_margin")


def freight_notes(deal_id):
    """Notes on the deal that start with / contain 'FREIGHT REQUEST' (the #freightcall snippet), newest first, as plain text."""
    st, r = hs("GET", f"/crm/v4/objects/deals/{deal_id}/associations/notes?limit=50")
    ids = [x["toObjectId"] for x in r.get("results", [])] if st == 200 else []
    out = []
    for nid in ids:
        st, n = hs("GET", f"/crm/v3/objects/notes/{nid}?properties=hs_note_body,hs_timestamp")
        body = (n.get("properties", {}).get("hs_note_body") or "") if st == 200 else ""
        text = html.unescape(re.sub(r"<br\s*/?>|</p>|</li>", "\n", body, flags=re.I))
        text = re.sub(r"<[^>]+>", "", text).strip()
        if "FREIGHT REQUEST" in text.upper():
            out.append((n["properties"].get("hs_timestamp") or "", text))
    return [t for _, t in sorted(out, reverse=True)]


def carrier_options_text(saved, hs_fn=None):
    """Deal field 'Carrier Options': the numbered cost table with cost, days and the price to the customer
    (cost + carrier DI, customer margin rule); the arrow marks the option in use (Use Option #)."""
    import freight_carriers
    opts = saved.get("options") or []
    if not opts:
        return ""
    try:
        di_map = freight_carriers.di_by_carrier(hs_fn or hs)
    except Exception:
        di_map = {}
    plan, m, sel = saved.get("plan"), saved.get("margin"), int(saved.get("selected", 1) or 0)   # 0 = waiting for the D&R direct rate
    lines = []
    for o in opts:
        cname, cid = freight_carriers.canonical(o["carrier"])
        di = di_map.get(cname) if cid and o.get("source") != DR_SOURCE else None   # D&R portal rate is entered all-in
        cost_true = float(o["cost_cad"]) * (1 + (di or 0) / 100)
        price = None
        if plan and plan.get("pct") is not None:
            price = price_from_plan(cost_true, plan)
        elif m:
            price = round(cost_true / (1 - float(m)), 2)
        cur = (o.get("cur") or "CAD").rstrip("?")
        cost = f"{float(o['cost']):,.2f} {cur}" + (f" (C${float(o['cost_cad']):,.2f})" if cur == "USD" else "")
        lines.append(f"{'▶' if o['n'] == sel else '  '} {o['n']}. {o['carrier']}" + (f" ({o['source']})" if o.get("source") else "")
                     + f" | cost {cost}" + (f" + DI {di:g}%" if di else "") + (f" | {o['days']} days" if o.get("days") else "")
                     + (f" | price C${price:,.2f}" if price is not None else ""))
    return ("\n".join(lines) + "\nTo use another carrier, set Use Option # to its number.")[:65000]


def option_fields(text):
    """Carrier Options text -> {'freight_option_1'..'freight_option_6': one line each} (blank when fewer options)."""
    lines = [l for l in (text or "").splitlines() if re.match(r"\s*▶?\s*\d+\.", l)]
    out = {f"freight_option_{i}": "" for i in range(1, 7)}
    for l in lines[:6]:
        n = int(re.match(r"\s*▶?\s*(\d+)\.", l).group(1))
        if 1 <= n <= 6:
            out[f"freight_option_{n}"] = l.strip()[:250]
    return out


def cmd_list():
    body = {"filterGroups": [{"filters": [
        {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE},
        {"propertyName": "dealstage", "operator": "EQ", "value": STAGE_QRR},
        {"propertyName": "freight_quote_route", "operator": "NOT_HAS_PROPERTY"}]}],
        "properties": DEAL_PROPS, "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}], "limit": 50}
    st, r = hs("POST", "/crm/v3/objects/deals/search", body)
    if st != 200:
        sys.exit(f"Search failed: {st} {r.get('message', '')}")
    out = []
    for d in r.get("results", []):
        full = get_deal(d["id"])
        cname, margin = company_margin_text(full)
        p = d["properties"]
        out.append({"deal_id": d["id"], "dealname": p.get("dealname"), "company": cname, "company_margin_rule": margin,
                    "quote_number": p.get("inxpress_quote_number_140"), "description": (p.get("description") or "").strip(),
                    "freight_notes": freight_notes(d["id"])})
    print(json.dumps(out, indent=1))


def next_quote_number():
    yy = datetime.now().strftime("%y")
    body = {"filterGroups": [{"filters": [{"propertyName": "inxpress_quote_number_140", "operator": "HAS_PROPERTY"}]}],
            "properties": ["inxpress_quote_number_140"], "sorts": [{"propertyName": "inxpress_quote_number_140", "direction": "DESCENDING"}],
            "limit": 100}
    st, r = hs("POST", "/crm/v3/objects/deals/search", body)
    nums = []
    for d in r.get("results", []) if st == 200 else []:
        v = (d["properties"].get("inxpress_quote_number_140") or "").strip()
        if re.fullmatch(yy + r"\d{4}", v):
            nums.append(int(v))
    return str(max(nums) + 1) if nums else f"{yy}0001"


def ms(dt):
    return str(int(dt.timestamp() * 1000))


def date_ms(iso):  # HubSpot date fields = midnight UTC
    y, m, d = map(int, iso.split("-"))
    return ms(datetime(y, m, d, tzinfo=timezone.utc))


def acc_values(names):
    out = []
    for n in names:
        hit = next((o for o in ACC_OPTIONS if o.lower() == n.lower() or o.lower().replace(" fee delivery", "") == n.lower()), None)
        if not hit and n.lower().startswith("appointment"):
            hit = "Appointment"
        if hit and hit not in out:
            out.append(hit)
    return ";".join(out)


def dims_text(items):
    return "\n".join(f"{it['quantity']} x {it['length']:g} x {it['width']:g} x {it['height']:g} in @ {it['weight'] / it['quantity']:,.0f} lb each, class {it['class']}"
                     for it in items)


def quote_summary(pl, el, res, items, days, pickup=None):
    """Deal field 'Quote Summary' (freight_quote_summary): the shipment as quoted, for the customer quote email template."""
    def place(x):
        return " ".join(p for p in (str(x[0]).title(), x[1], x[2]) if p)
    pieces = "; ".join(f"{it['quantity']} pc {it['length']:g} x {it['width']:g} x {it['height']:g} in, {it['weight'] / it['quantity']:,.0f} lb each"
                       for it in items)
    acc = res.get("accessorials") or []
    acc_line = (f"Accessorials included: {', '.join(acc)}. Any accessorial not listed here is not included." if acc else
                "Accessorials: none - dock to dock pickup and delivery. Any accessorial not listed here is not included.")
    lines = [f"From: {place(pl['from'])}", f"To: {place(pl['to'])}",
             f"Shipment: {pieces} ({el['pieces']} pc, {res['total_lb']:,.0f} lb total)",
             f"Transit: about {re.sub(chr(92) + 'D', '', str(days)) or '?'} business days from pickup",
             acc_line]
    if pickup:
        lines.insert(3, f"Pickup date: {pickup}")
    return "<br>".join(html.escape(x, quote=False) for x in lines)   # rich-text field: <br> keeps the lines apart in the email token (plain newlines run together)


def note_html(res, qnum):
    rec = res.get("recommended")
    def money(r):
        return f"{r['cost']:,.2f} {r['cur']}"
    rows = "".join(
        f"<tr><td>{i}</td><td>{'<strong>' if r is rec or (rec and r['quote_detail_id'] == rec['quote_detail_id']) else ''}{html.escape(r['carrier'])}"
        f"{' (RECOMMENDED)</strong>' if rec and r['quote_detail_id'] == rec['quote_detail_id'] else ''}</td><td>{html.escape(r['source'] or '')}</td>"
        f"<td>{money(r)}</td><td>{r['cost_cad']:,.2f}</td><td>{r['days']}</td><td>{html.escape(r['carrier_quote_no'] or '-')}</td>"
        f"<td>{html.escape(r['flags'])}</td></tr>"
        for i, r in enumerate(res["standard"][:10], 1))
    gua = "".join(f"<li>{html.escape(r['carrier'])} ({html.escape(r['service'])}): {money(r)} (C${r['cost_cad']:,.2f}), {r['days']} days</li>"
                  for r in res["guaranteed"][:3])
    pl = res["places"]
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in res["notes"])
    return (f"<p><strong>Freight quote {qnum} &ndash; accuFRATE quote #{res['quote_id']} &ndash; {len(res['standard']) + len(res['guaranteed'])} options</strong><br>"
            f"{html.escape(' '.join(pl['from']))} &rarr; {html.escape(' '.join(pl['to']))} &middot; {res['eligibility']['pieces']} pc &middot; "
            f"{res['total_lb']:,.0f} lb &middot; {res['eligibility']['linear_ft']} LF &middot; pickup {res['pickup_date']}<br>"
            f"Included: {html.escape(', '.join(res['accessorials']) or 'no extra services')}</p>"
            f"<table border='1' cellpadding='4' cellspacing='0'><thead><tr><th>#</th><th>Carrier</th><th>Source</th><th>Cost</th><th>CAD equiv.</th>"
            f"<th>Days</th><th>Carrier quote #</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table>"
            + (f"<p><strong>Guaranteed / expedited</strong></p><ul>{gua}</ul>" if gua else "")
            + f"<ul>{notes}<li>FX: Bank of Canada USD/CAD {res['fx']} ({res['fx_date']})</li><li>{DOCK}</li>"
            f"<li>To use another carrier, set Use Option # to its row number.</li>"
            + ("" if os.environ.get("FREIGHT_SERVER") else f"<li>Full list: {html.escape(res['csv'])}</li>") + "</ul>")


def add_note(deal_id, body_html):
    return hs("POST", "/crm/v3/objects/notes", {"properties": {"hs_timestamp": ms(datetime.now(timezone.utc)), "hs_note_body": body_html},
                                                  "associations": [{"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]}]})


def add_task(deal_id, owner, subject, body):
    return hs("POST", "/crm/v3/objects/tasks", {"properties": {"hs_timestamp": ms(datetime.now(timezone.utc)), "hs_task_subject": subject,
                                                               "hs_task_body": body, "hs_task_status": "NOT_STARTED", "hs_task_priority": "HIGH",
                                                               "hubspot_owner_id": owner or BOB, "hs_queue_membership_ids": TASK_QUEUE or None},
                                                  "associations": [{"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 216}]}]})


def run_quote(parsed, mode, deal_id):
    """Price via quote_cli in-process. Returns (exit_code, summary_or_None, captured_output)."""
    import contextlib
    import io
    import quote_cli
    tmp = tempfile.gettempdir()
    req_path, out = os.path.join(tmp, f"fq_req_{deal_id}.json"), os.path.join(tmp, f"fq_{deal_id}.json")
    json.dump(parsed, open(req_path, "w", encoding="utf-8"))
    if os.path.exists(out):
        os.remove(out)
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf):
            quote_cli.main([req_path, "--mode", mode, "--json-out", out])
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:  # network / parsing failure
        buf.write(f"\n{type(e).__name__}: {e}")
        code = 1
    res = json.load(open(out, encoding="utf-8")) if code in (0, 3) and os.path.exists(out) else None
    return code, res, buf.getvalue()


def price_from_plan(cost_cad, plan):
    """Company margin rule -> customer price. plan = {pct (fraction), flat_cad, min_margin_cad}."""
    pct = float(plan.get("pct") or 0)
    price = cost_cad / (1 - pct) if pct < 1 else cost_cad
    price += float(plan.get("flat_cad") or 0)
    mn = float(plan.get("min_margin_cad") or 0)
    if mn and price - cost_cad < mn:
        price = cost_cad + mn
    return round(price, 2)


def cmd_apply(deal_id, parsed_path, mode, dry):
    return apply_parsed(deal_id, json.load(open(parsed_path, encoding="utf-8")), mode, dry)


def apply_parsed(deal_id, parsed, mode, dry):
    deal = get_deal(deal_id)
    p = deal["properties"]
    owner = p.get("hubspot_owner_id") or BOB
    qnum = (p.get("inxpress_quote_number_140") or "").strip() or next_quote_number()
    props = {"freight_request_type": parsed.get("request_type", "Quote")}
    props.update(booking_props(parsed, p))
    if parsed.get("carrier_requirements") and not (p.get("freight_carrier_requirements") or "").strip():
        props["freight_carrier_requirements"] = parsed["carrier_requirements"][:2000]
    if not (p.get("inxpress_quote_number_140") or "").strip():
        props["inxpress_quote_number_140"] = qnum
    if parsed.get("pickup_date"):
        props["requested_pickup_date"] = date_ms(parsed["pickup_date"])
    for k_in, k_out in (("from_zip", "freight_origin_postal"), ("to_zip", "freight_dest_postal")):
        if parsed.get(k_in):
            props[k_out] = parsed[k_in].upper()

    if parsed.get("rfq"):  # flatbed / volume: never sent to accuFRATE, one email task per carrier
        return route_rfq(deal_id, parsed["rfq"], parsed, props, qnum, owner, dry)

    if parsed.get("route"):  # e.g. international / air / ocean: never sent to accuFRATE
        props.update({"freight_quote_route": parsed["route"], "freight_route_reason": parsed.get("route_reason", "")})
        return finish(deal_id, props, None, (parsed.get("task", "Quote manually") + " – " + qnum,
                      parsed.get("route_reason", "")), owner, dry)

    missing = parsed.get("missing") or []
    if missing:
        props.update({"freight_quote_route": "Missing information", "freight_route_reason": "Missing: " + ", ".join(missing),
                      "freight_missing_info": "\n".join("- " + m for m in missing)})
        return finish(deal_id, props, None, ("Ask customer for missing details – " + qnum,
                      "Before we can quote, we need:\n" + "\n".join("- " + m for m in missing)
                      + "\nUse the 'Customer - Missing Information' template."), owner, dry)

    code, res, log = run_quote(parsed, mode, deal_id)
    if res is None:
        print(log[-1500:])
        props.update({"freight_quote_route": "Manual", "freight_route_reason": "accuFRATE error - quote manually"})
        return finish(deal_id, props, None, ("Quote manually (accuFRATE error) – " + qnum, log[-800:]), owner, dry)
    el, pl, items = res["eligibility"], res["places"], res["items_priced"]
    props.update({
        "freight_origin_city": f"{pl['from'][0].title()} {pl['from'][1]}".strip(), "freight_dest_city": f"{pl['to'][0].title()} {pl['to'][1]}".strip(),
        "freight_pieces": str(el["pieces"]), "freight_total_weight_lb": str(res["total_lb"]), "freight_dimensions": dims_text(items),
        "freight_linear_feet": str(el["linear_ft"]), "freight_longest_piece_in": str(el["longest_in"]),
        "freight_overlength": "true" if el["overlength_code"] else "false",
        "freight_class": ", ".join(sorted({str(it["class"]) for it in items})),
        "freight_accessorials": acc_values(res["accessorials"]),
    })
    if not el["eligible"]:
        reason = "; ".join(el["reasons"])
        parsed = dict(parsed, rfq_reason="Not sent to accuFRATE: " + reason)
        return route_rfq(deal_id, "volume", parsed, props, qnum, owner, dry)

    rec = res.get("recommended")
    props.update({"freight_quote_route": "accuFRATE", "freight_route_reason": "", "freight_accufrate_quote_id": str(res["quote_id"]),
                  "freight_priced_at": ms(datetime.now(timezone.utc))})
    task_body = ""
    dr = dr_direct_customer(parsed.get("customer") or p.get("dealname"))
    if dr:
        dr_row = next((r for r in res["standard"] if dr_direct_row(r, dr)), None)
        if not dr_row:
            return wait_for_dr(deal_id, parsed, res, props, p, qnum, owner, dry)
        res["standard"] = [dr_row] + [r for r in res["standard"] if r is not dr_row]   # option 1, cheapest or not
        rec = res["recommended"] = dr_row
        task_body += (f"{parsed.get('customer') or 'This customer'} is priced with Day & Ross direct: accuFRATE returned their own D&R tariff "
                      f"({dr_row.get('tariff') or 'customer tariff'}), so it is option 1.\n")
    if rec:
        cur = rec["cur"].rstrip("?")
        props["freight_quote_summary"] = quote_summary(pl, el, res, items, rec["days"], parsed.get("pickup_date") or res.get("pickup_date"))
        props.update({"freight_recommended_carrier": f"{rec['carrier']} ({rec['source']})" if rec["source"] else rec["carrier"],
                      "freight_transit_days": re.sub(r"\D", "", str(rec["days"])) or "",
                      "freight_quote_currency": "USD" if cur == "USD" else "CAD"})
        hand_cost = (p.get("cost_cad") or "").strip("0. ")
        if hand_cost:  # a person already priced this deal: keep their cost, report ours in the note/task
            props.pop("carriers___canada_freight", None)
            task_body += (f"Existing cost on the deal kept: C${float(p['cost_cad']):,.2f} (quote # {p.get('carrier_quote_number') or '-'}). "
                          f"This run's best: C${rec['cost_cad']:,.2f}. To switch, update Cost CAD / Use Option #.\n")
        else:
            props.update({"cost_cad": f"{rec['cost_cad']:.2f}", "cost_usd": f"{rec['cost']:.2f}" if cur == "USD" else "",
                          "carrier_quote_number": rec["carrier_quote_no"] or ""})
        # carrier + DI (LTL only; DI lives on the carrier's Vendor company, like markup lives on the customer)
        cost_true = rec["cost_cad"]
        if not hand_cost:
            import freight_carriers
            cname, cid = freight_carriers.canonical(rec["carrier"])
            di = freight_carriers.di_by_carrier(hs).get(cname) if cid else None
            props.update({"freight_carrier": cname if any(cname == n for n, _, _ in freight_carriers.CARRIERS) else "Other",
                          "freight_booked_through": "accuFRATE", "freight_di_percent": f"{di:g}" if di is not None else ""})
            cost_true = rec["cost_cad"] * (1 + (di or 0) / 100)
            if di:
                task_body += f"DI {di:g}% on {cname}: +C${rec['cost_cad'] * di / 100:,.2f} included in the price.\n"
            elif di is None:
                task_body += (f"DI % not set for {cname}" + (" (set it on its Vendor company)" if cid else " (no Vendor company yet)")
                              + " - priced with no DI.\n")
        if not p.get("freight_use_option"):
            props["freight_use_option"] = "1"
        if p.get("margin_on_freight_shipment"):
            props["freight_margin_note"] = f"Margin {float(p['margin_on_freight_shipment']) * 100:g}% already set on the deal by a person (kept)."
        if parsed.get("margin") is not None and not p.get("margin_on_freight_shipment"):
            props["margin_on_freight_shipment"] = str(parsed["margin"])
        m = p.get("margin_on_freight_shipment") or (str(parsed["margin"]) if parsed.get("margin") is not None else None)
        if m and not (p.get("amount") or "").strip("0. "):  # quoted price = Amount; never overwrite a typed price
            props["amount"] = f"{cost_true / (1 - float(m)):.2f}"
        if parsed.get("amount"):  # an explicit price from a company rule (e.g. % + flat, minimum) wins when Amount is empty
            if not (p.get("amount") or "").strip("0. "):
                props["amount"] = f"{float(parsed['amount']):.2f}"
        plan = parsed.get("margin_plan")
        if plan and not hand_cost and not (p.get("amount") or "").strip("0. ") and not parsed.get("amount"):
            price = price_from_plan(cost_true, plan)
            props["amount"] = f"{price:.2f}"
            if plan.get("pct") is not None and not p.get("margin_on_freight_shipment"):
                props["margin_on_freight_shipment"] = str(plan["pct"])
            task_body += f"Price C${price:,.2f} from the margin rule: {plan.get('note') or ''}\n"
            company = parsed.get("customer") or "this company"
            if (plan.get("note") or "").lower().startswith("no company margin rule"):
                props["freight_margin_note"] = (f"DEFAULT {float(plan.get('pct') or 0.25) * 100:g}% applied by XpressQuote: {company} has no margin rule in HubSpot "
                                                "(company field 'Margin to be added to Freight Cost'). Approve or change the margin.")[:250]
            else:
                props["freight_margin_note"] = f"From {company}'s margin rule in HubSpot: {plan.get('note') or ''}"[:250]
            if plan.get("approval_over_cad") and price > float(plan["approval_over_cad"]):
                task_body += f"APPROVAL NEEDED: price is over C${float(plan['approval_over_cad']):,.0f} (company rule).\n"
        task_body += (f"Recommended: {rec['carrier']} via {rec['source'] or 'accuFRATE'} – {rec['cost']:,.2f} {rec['cur']} "
                     f"(C${rec['cost_cad']:,.2f}), {rec['days']} days. {rec['flags']}\n")
    ck = res.get("checks") or {}
    missing = ck.get("missing_carriers") or []
    if ck.get("expected"):  # only lanes with expected carriers; cleared when all of them priced
        props["freight_carriers_not_returned"] = ";".join(missing)
    warn = [n for n in res.get("notes", []) if n.startswith(("PRICING NOT RETURNED", "Only "))]
    if warn:
        task_body = "\n".join("Note: " + w for w in warn) + "\n" + task_body
    if parsed.get("assumptions"):
        task_body += "Assumed (check): " + "; ".join(parsed["assumptions"]) + "\n"
    try:  # LTL RFQs to carriers that aren't in accuFRATE (never blocks the pricing)
        import freight_rfq
        rfq, rfq_warn = ([], []) if dr else freight_rfq.ltl_carriers(hs, parsed.get("from_zip"), parsed.get("to_zip"), missing)
    except Exception as e:
        rfq, rfq_warn = [], [f"LTL RFQ list failed: {type(e).__name__}: {e}"]
    if rfq:
        task_body += ("Optional LTL RFQ tasks created for: " + ", ".join(c["carrier"] for c in rfq)
                      + " (their prices may beat accuFRATE; not needed to send this quote).\n")
    task_body += "".join("WARNING: " + w + "\n" for w in rfq_warn)
    task_body += (parsed.get("margin_note") or "") + "\n" + SEND_HOWTO
    # Bob 2026-10-17: carriers that didn't price (e.g. Day & Ross direct / Manitoulin) don't hold up the quote - no CHECK CARRIERS
    # title; it's noted in the task and still logged for the weekly "Carriers Not Returned" report.
    subject = review_subject(p, qnum)
    notes = [note_html(res, qnum)]
    if missing:
        notes.insert(0, missing_note_html(res, qnum, missing))
    rec_n = next((i for i, r in enumerate(res["standard"][:10], 1) if rec and r["quote_detail_id"] == rec["quote_detail_id"]), 1)
    props["freight_quote_request"] = quote_request_json(parsed, res, {
        "selected": rec_n, "plan": parsed.get("margin_plan"), "margin": props.get("margin_on_freight_shipment") or p.get("margin_on_freight_shipment"),
        "auto_amount": props.get("amount")})
    try:
        props["freight_carrier_options"] = carrier_options_text(json.loads(props["freight_quote_request"]))
        props.update(option_fields(props["freight_carrier_options"]))
    except Exception as e:
        print("carrier options:", e)
    result = finish(deal_id, props, notes, (subject, task_body), owner, dry)
    if rfq:
        result["rfq_tasks"] = freight_rfq.create_rfq_tasks(deal_id, owner, "ltl", qnum, rfq, [], dry)
    return result


SEND_HOWTO = ("Check the cost note, set/confirm margin and Amount, then send the quote: Email -> Templates -> 'Customer - Quote (XpressQuote)' "
              "(price and shipment details fill in automatically).")


def review_subject(p, qnum):
    """Bob 2026-10-19: lead with "Quote Review" and the deal name (e.g. "Quote Review – 260650 - Tarrison Oakville to Dartmouth")."""
    dname = (p.get("dealname") or "").strip()
    return "Quote Review – " + (dname if qnum in dname else f"{qnum} - {dname}" if dname else qnum)


def dr_copy_text(res, parsed):
    """The shipment in the order the D&R portal quote form asks for it (task body + the /freight page's copy box)."""
    pl, el = res["places"], res["eligibility"]
    def place(x):
        return " ".join(v for v in (str(x[0]).title(), x[1], x[2]) if v)
    acc = res.get("accessorials") or []
    lines = [f"From: {place(pl['from'])}", f"To: {place(pl['to'])}", f"Pickup date: {parsed.get('pickup_date') or res.get('pickup_date') or '?'}"]
    lines += [f"{it['quantity']} pc {it['length']:g} x {it['width']:g} x {it['height']:g} in, {it['weight']:,.0f} lb total, class {it['class']}"
              + (", stackable" if it.get("stackable") else "") for it in res["items_priced"]]
    lines += [f"Total: {el['pieces']} pc, {res['total_lb']:,.0f} lb, {el['linear_ft']} linear ft",
              "Accessorials: " + (", ".join(acc) if acc else "none (dock to dock)")]
    return "\n".join(lines)


def wait_for_dr(deal_id, parsed, res, props, p, qnum, owner, dry):
    """D&R-direct customer and accuFRATE gave no D&R direct price: no cost, no Amount, no carrier picked. One task to get the rate from
    the D&R portal; the accuFRATE rows are saved for reference. freight_page.set_direct_rate finishes the quote."""
    who = parsed.get("customer") or p.get("dealname") or "this customer"
    el, pl, items = res["eligibility"], res["places"], res["items_priced"]
    copy = dr_copy_text(res, parsed)
    props.update({"freight_route_reason": f"Waiting for Day & Ross direct rate ({who})"[:250],
                  "freight_quote_summary": quote_summary(pl, el, res, items, "", parsed.get("pickup_date") or res.get("pickup_date"))})
    plan = parsed.get("margin_plan")
    if plan and plan.get("pct") is not None and not p.get("margin_on_freight_shipment"):
        props["margin_on_freight_shipment"] = str(plan["pct"])
        props["freight_margin_note"] = f"From {who}'s margin rule in HubSpot: {plan.get('note') or ''}"[:250]
    ck = res.get("checks") or {}
    if ck.get("expected"):   # still logged for the weekly "Carriers Not Returned" report
        props["freight_carriers_not_returned"] = ";".join(ck.get("missing_carriers") or [])
    saved = json.loads(quote_request_json(parsed, res, {
        "selected": 0, "dr_direct": "waiting", "dr_copy": copy, "plan": plan, "auto_amount": None,
        "margin": props.get("margin_on_freight_shipment") or p.get("margin_on_freight_shipment")}))
    props["freight_quote_request"] = json.dumps(saved, separators=(",", ":"))
    try:
        props["freight_carrier_options"] = carrier_options_text(saved)
        props.update(option_fields(props["freight_carrier_options"]))
    except Exception as e:
        print("carrier options:", e)
    note = (f"<p><strong>For reference only:</strong> {html.escape(who)} is priced with Day &amp; Ross direct. accuFRATE did not return "
            f"their own D&amp;R tariff, so the rate comes from the D&amp;R portal. The rows below are backup prices.</p>" + note_html(dict(res, recommended=None), qnum))
    body = (f"{who} is priced with Day & Ross direct, and accuFRATE didn't return their own D&R tariff "
            "(any other Day & Ross price in the cost note is reference only).\n"
            f"1. Get a quote in the D&R portal: {DR_PORTAL} (account {DR_ACCOUNT})\n{copy}\n"
            "2. Enter the ALL-IN rate (incl. fuel and the accessorials above), the transit days and the D&R quote # on the New freight request "
            "page: Book a quote > this deal > Day & Ross direct rate > Save rate.\n"
            "XpressQuote then prices it with the margin rule and creates the 'Quote Review' task. Book it in the D&R portal, not accuFRATE."
            + ("\nAssumed (check): " + "; ".join(parsed["assumptions"]) if parsed.get("assumptions") else ""))
    return finish(deal_id, props, [note], (f"Get Day & Ross direct rate – {qnum} ({who})"[:250], body), owner, dry)


def missing_note_html(res, qnum, missing):
    pl, el = res["places"], res["eligibility"]
    return (f"<p><strong>Pricing not returned in accuFRATE</strong> &ndash; quote {qnum}, accuFRATE quote #{res['quote_id']} "
            f"({res['checks']['runs']} tries, {res['checks']['carriers_answered']} carriers answered)</p><ul>"
            f"<li>Carrier(s): {html.escape(', '.join(missing))}</li>"
            f"<li>Pickup: {html.escape(' '.join(pl['from']))}</li><li>Destination: {html.escape(' '.join(pl['to']))}</li>"
            f"<li>Shipment: {el['pieces']} pc, {res['total_lb']:,.0f} lb, {el['linear_ft']} LF, pickup {res['pickup_date']}</li></ul>")



def route_rfq(deal_id, kind, parsed, props, qnum, owner, dry):
    import freight_rfq
    fields, missing = freight_rfq.request_fields(parsed)
    for k, v in fields.items():
        props.setdefault(k, v)
    equip = parsed.get("equipment") or ""
    reason = parsed.get("rfq_reason") or (f"{equip} request" if kind == "flatbed" else "Volume / FTL request")
    props.update({"freight_quote_route": "Volume / FTL - RFQ", "freight_route_reason": reason,
                  "freight_equipment": equip if equip and equip != "Not stated" else ("Flatbed" if kind == "flatbed" else "")})
    carriers = freight_rfq.carriers_for(kind, parsed.get("from_zip"), parsed.get("to_zip"))
    result = finish(deal_id, props, None, None, owner, dry)
    result["rfq_tasks"] = freight_rfq.create_rfq_tasks(deal_id, owner, kind, qnum, carriers, missing, dry,
                                                       props.get("freight_carrier_requirements") or "")
    result["missing"] = missing
    return result

def finish(deal_id, props, note, task, owner, dry):
    props = {k: v for k, v in props.items() if v is not None}
    result = {"deal": deal_id, "route": props.get("freight_quote_route"), "reason": props.get("freight_route_reason", ""),
              "task": task[0] if task else None, "task_body": task[1] if task else "", "note": bool(note), "set": props}
    if dry:
        print(json.dumps({"deal": deal_id, "would_set": props, "note": bool(note), "task": task[0] if task else None}, indent=1))
        result["dry_run"] = True
        return result
    st, r = hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
    print("deal update:", st, r.get("message", ""))
    result["deal_update"] = st
    for n in ([note] if isinstance(note, str) else (note or [])):
        st, r = add_note(deal_id, n)
        print("note:", st, r.get("message", ""))
        result["note_status"] = st
    if task:
        st, r = add_task(deal_id, owner, task[0], task[1])
        print("task:", st, r.get("message", ""), "|", task[0])
        result["task_status"] = st
    print("route:", props.get("freight_quote_route"), "|", props.get("freight_route_reason", ""))
    return result


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "apply"):
        print(__doc__)
        sys.exit(1)
    if a[0] == "list":
        cmd_list()
    else:
        cmd_apply(a[1], a[2], a[a.index("--mode") + 1] if "--mode" in a else "C", "--dry-run" in a)
