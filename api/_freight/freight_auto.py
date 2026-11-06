"""Automatic freight pricing: HubSpot deal -> Claude reads the request -> accuFRATE -> results written back.

Runs on Vercel (api/freight_price.py, triggered by the HubSpot webhook and a daily sweep) and locally:
  python freight_auto.py <deal_id> [--mode C|T] [--dry-run] [--force]
  python freight_auto.py sweep [--mode C|T] [--dry-run]

Only acts on Freight Pipeline deals at Quote Request Received that have no Quote Route yet.
Same rules as freight_queue (never overwrites values a person typed; nothing is sent to the customer).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import freight_queue as fq
from freight_parse import parse_request

LOCK = "Auto-pricing in progress"
LOCK_MINUTES = 6
DEFAULT_MARGIN = 0.25
STAGE_QS = os.environ.get("FREIGHT_STAGE_QS_ID", "993307651")  # Quote Submitted


def _set(deal_id, props):
    return fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})


def fix_postal(code, notes):
    """Canadian postal codes are A1A 1A1: fix letter/digit typos in the digit slots (eie3w8 -> E1E 3W8)."""
    c = re.sub(r"\s+", "", (code or "").upper())
    if len(c) == 6 and c[0].isalpha():
        digits = {"I": "1", "L": "1", "O": "0", "S": "5"}
        fixed = "".join(digits.get(ch, ch) if i in (1, 3, 5) else ch for i, ch in enumerate(c))
        out = fixed[:3] + " " + fixed[3:]
        if fixed != c:
            notes.append(f"Postal code '{code}' read as {out} (typo corrected)")
        return out
    return (code or "").strip().upper()


def build_parsed(x, has_rule):
    """Claude's extraction -> freight_queue parsed dict."""
    assumptions = list(x.get("assumptions") or [])
    parsed = {"request_type": x.get("request_type") or "Quote", "from_zip": fix_postal(x.get("from_zip", ""), assumptions),
              "to_zip": fix_postal(x.get("to_zip", ""), assumptions),
              "accessorials": x.get("accessorials") or [], "items": x.get("items") or [], "assumptions": assumptions}
    if x.get("pickup_date"):
        parsed["pickup_date"] = x["pickup_date"]
    mode = x.get("shipment_mode")
    equip = x.get("equipment") or "Not stated"
    if mode in ("International", "Air", "Ocean"):
        pass
    elif equip in ("Flatbed", "Step Deck", "RGN / Lowboy", "Conestoga", "Hot Shot"):
        parsed["rfq"] = "flatbed"
    elif x.get("volume_or_ftl") or mode == "FTL":
        parsed["rfq"] = "volume"
    parsed["equipment"] = equip
    if (x.get("carrier_requirements") or "").strip():
        parsed["carrier_requirements"] = x["carrier_requirements"].strip()
    if x.get("booking"):
        parsed["booking"] = x["booking"]
    if x.get("load_length_ft"):
        parsed["load_length_ft"] = x["load_length_ft"]
    if parsed.get("rfq"):
        pass  # flatbed / volume: no dimensions needed, never "Missing information"
    elif mode in ("International", "Air", "Ocean"):
        parsed.update({"route": "Manual", "route_reason": f"{mode} shipment - forwarder quote needed",
                       "task": "Get international quote (air/ocean forwarder RFQ)" if mode != "Air" else "Get air cargo quote (RFQ)"})
    elif x.get("missing"):
        parsed["missing"] = x["missing"]
    if has_rule and x.get("margin_plan") and x["margin_plan"].get("pct") is not None:
        parsed["margin_plan"] = x["margin_plan"]
    elif not has_rule:
        parsed["margin_plan"] = {"pct": DEFAULT_MARGIN, "flat_cad": None, "min_margin_cad": None, "approval_over_cad": None,
                                 "note": "no company margin rule - default 25%"}
        parsed["margin_note"] = "No company margin rule: default 25% applied - Bob to approve the price.\n"
    else:
        parsed["margin_note"] = "Company margin rule could not be applied automatically - set the price by hand.\n"
    return parsed


def _pc(z):
    return re.sub(r"[^A-Z0-9]", "", (z or "").upper())


def company_address(name):
    """HubSpot company by name (the one clear match) -> {name, address, address2, city, state, zip} if it has a postal code, else None."""
    import freight_bol_intake as fb
    co = fb.find_company(name)
    if not co:
        return None
    st, c = fq.hs("GET", f"/crm/v3/objects/companies/{co['id']}?properties=name,address,address2,city,state,zip,country")
    p = c.get("properties", {}) if st == 200 else {}
    if not _pc(p.get("zip")):
        return None
    return {k: (p.get(k) or "").strip() for k in ("name", "address", "address2", "city", "state", "zip")}


def places_from_hubspot(x):
    """Bob 2026-10-19: a pickup / delivery named only by company ("pick up at Tarrison") -> that company's HubSpot address.
    No postal code in the request -> use the company's (flagged in assumptions, removed from missing). Street address goes into the
    shipper / consignee booking fields when blank and the postal code agrees. A postal code in the request always wins."""
    bk = x.get("booking") or {}
    notes = []
    for side, zkey, ckey, akey, a2key, words in (("Pickup", "from_zip", "shipper_company", "shipper_address", "shipper_address2", ("origin", "pickup")),
                                                 ("Delivery", "to_zip", "consignee_company", "consignee_address", "consignee_address2", ("destination", "delivery"))):
        name = (bk.get(ckey) or "").strip()
        if not name:
            continue
        try:
            co = company_address(name)
        except Exception:
            co = None
        if not co:
            continue
        have = _pc(x.get(zkey))
        where = ", ".join(v for v in (co["address"], co["city"], co["state"], co["zip"]) if v)
        if not have:
            x[zkey] = co["zip"]
            notes.append(f"{side} postal code taken from HubSpot company {co['name']} ({where}) - check it is the right location")
            x["missing"] = [m for m in (x.get("missing") or []) if not (any(w in m.lower() for w in words) and re.search(r"postal|zip", m, re.I))]
        elif have != _pc(co["zip"]):
            continue   # different branch / location: keep the request's postal code, don't guess the street
        if co["address"] and not (bk.get(akey) or "").strip():
            bk[akey] = co["address"]
            if co["address2"] and not (bk.get(a2key) or "").strip():
                bk[a2key] = co["address2"]
    if bk:
        x["booking"] = bk
    if notes:
        x["assumptions"] = list(x.get("assumptions") or []) + notes
    return x


FLATBED_EQUIP = ("Flatbed", "Step Deck", "RGN / Lowboy", "Conestoga", "Hot Shot")
OVERRIDE_KEYS = ("from_zip", "to_zip", "pickup_date", "items", "accessorials", "equipment", "carrier_requirements", "volume_or_ftl")


def apply_override(x, o):
    """Shipment details checked / corrected by a person on the New freight request page win over Claude's read.
    The required-info list is recomputed from the corrected values (postal codes, weights, dimensions with piece count)."""
    for k in OVERRIDE_KEYS:
        if k in o and o[k] is not None:
            x[k] = o[k]
    no_dims = x.get("equipment") in FLATBED_EQUIP or x.get("volume_or_ftl") or x.get("shipment_mode") in ("FTL", "International", "Air", "Ocean")
    need = []
    if not (x.get("from_zip") or "").strip():
        need.append("origin postal code")
    if not (x.get("to_zip") or "").strip():
        need.append("destination postal code")
    items = x.get("items") or []
    if not no_dims:
        if not items or not all(float(i.get("qty") or 0) > 0 for i in items):
            need.append("piece count")
        if not items or not all(float(i.get("weight_each") or 0) > 0 for i in items):
            need.append("weights")
        if not items or not all(all(float(i.get(d) or 0) > 0 for d in ("length", "width", "height")) for i in items):
            need.append("dimensions")
    x["missing"] = need
    x["assumptions"] = list(x.get("assumptions") or []) + ["Shipment details were checked / corrected on the New freight request page."]
    return x


def process_deal(deal_id, mode="C", dry=False, force=False, override=None):
    deal = fq.get_deal(deal_id)
    p = deal["properties"]
    if p.get("pipeline") == fq.PIPELINE and p.get("dealstage") == STAGE_QS:
        return fill_booking(deal, dry)
    if p.get("pipeline") != fq.PIPELINE or p.get("dealstage") != fq.STAGE_QRR:
        return {"deal": deal_id, "skipped": "not a Freight Pipeline deal at Quote Request Received"}
    if p.get("freight_quote_route") and not force:
        return {"deal": deal_id, "skipped": "already routed: " + p["freight_quote_route"]}
    reason = p.get("freight_route_reason") or ""
    if reason.startswith(LOCK) and not force:
        try:
            started = datetime.fromisoformat(reason.split("|")[1].strip())
            if (datetime.now(timezone.utc) - started).total_seconds() < LOCK_MINUTES * 60:
                return {"deal": deal_id, "skipped": "already being priced"}
        except Exception:
            pass
    text = (p.get("description") or "").strip()
    notes = fq.freight_notes(deal_id)
    if notes:
        text = (text + "\n\n" + "\n\n".join(notes)).strip()
    if not text:
        return {"deal": deal_id, "skipped": "no Description or FREIGHT REQUEST note yet"}

    if not dry:
        _set(deal_id, {"freight_route_reason": f"{LOCK} | {datetime.now(timezone.utc).isoformat(timespec='seconds')}"})
    try:
        company, rule = fq.company_margin_text(deal)
        x = places_from_hubspot(parse_request(text, company, rule))
        if override:
            x = apply_override(x, override)
        parsed = build_parsed(x, bool((rule or "").strip()))
        parsed["customer"] = company or p.get("dealname") or ""
        result = fq.apply_parsed(deal_id, parsed, mode, dry)
        result["parsed"] = x
        return result
    except SystemExit as e:  # freight_queue exits on HubSpot errors
        err = str(e)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    # never leave a deal stuck: route to Manual with a task
    if not dry:
        _set(deal_id, {"freight_quote_route": "Manual", "freight_route_reason": "Auto-pricing failed - quote manually"})
        fq.add_task(deal_id, p.get("hubspot_owner_id") or fq.BOB, "Quote manually (auto-pricing failed)",
                    "The automatic pricing could not finish this deal:\n" + err[:800])
    return {"deal": deal_id, "error": err[:800]}


def fill_booking(deal, dry=False):
    """Quote Submitted: (re)read the Description + FREIGHT REQUEST notes for BOL details; fill only blank booking fields."""
    deal_id, p = deal["id"], deal["properties"]
    if all((p.get(fq.booking_prop(k)) or "").strip() for k in fq.BOOKING_KEYS):
        return {"deal": deal_id, "skipped": "booking details already complete"}
    text = (p.get("description") or "").strip()
    notes = fq.freight_notes(deal_id)
    if notes:
        text = (text + "\n\n" + "\n\n".join(notes)).strip()
    if not text:
        return {"deal": deal_id, "skipped": "no Description"}
    company, rule = fq.company_margin_text(deal)
    props = fq.booking_props(places_from_hubspot(parse_request(text, company, None)), p)
    if props and not dry:
        _set(deal_id, props)
    return {"deal": deal_id, "booking_filled": sorted(props), "dry_run": dry}


def waiting_deal_ids(limit=10):
    body = {"filterGroups": [{"filters": [
        {"propertyName": "pipeline", "operator": "EQ", "value": fq.PIPELINE},
        {"propertyName": "dealstage", "operator": "EQ", "value": fq.STAGE_QRR},
        {"propertyName": "freight_quote_route", "operator": "NOT_HAS_PROPERTY"}]}],
        "properties": ["dealname"], "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}], "limit": limit}
    st, r = fq.hs("POST", "/crm/v3/objects/deals/search", body)
    return [d["id"] for d in r.get("results", [])] if st == 200 else []


def sweep(mode="C", dry=False, budget_s=240):
    t0, out = time.time(), []
    for did in waiting_deal_ids():
        if time.time() - t0 > budget_s:
            out.append({"deal": did, "skipped": "time budget - next sweep"})
            continue
        out.append(process_deal(did, mode, dry))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(1)
    m = a[a.index("--mode") + 1] if "--mode" in a else "C"
    if a[0] == "sweep":
        r = sweep(m, "--dry-run" in a)
    else:
        r = process_deal(a[0], m, "--dry-run" in a, "--force" in a)
    print(json.dumps(r, indent=1, default=str)[:6000])


def carrier_changed(deal_id, dry=False):
    """The deal's Carrier changed: set DI % from that carrier's Vendor company (LTL only; blank = not known)."""
    import freight_carriers
    st, d = fq.hs("GET", f"/crm/v3/objects/deals/{deal_id}?properties=freight_carrier,freight_quote_route,freight_di_percent,pipeline")
    p = d.get("properties", {}) if st == 200 else {}
    if p.get("pipeline") != fq.PIPELINE:
        return {"deal": deal_id, "skipped": "not a Freight Pipeline deal"}
    carrier = p.get("freight_carrier") or ""
    if (p.get("freight_quote_route") or "") == "Volume / FTL - RFQ" or not carrier:
        new = ""  # DI is LTL only / no carrier yet
    else:
        cid = next((c for n, _, c in freight_carriers.CARRIERS if n == carrier), None)
        di = freight_carriers.di_by_carrier(fq.hs).get(carrier) if cid else None
        new = f"{di:g}" if di is not None else ""
    if (p.get("freight_di_percent") or "") == new:
        return {"deal": deal_id, "di": new, "changed": False}
    if not dry:
        fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": {"freight_di_percent": new}})
    return {"deal": deal_id, "carrier": carrier, "di": new, "changed": True}


def option_changed(deal_id, dry=False):
    """Use Option # changed: switch the deal to that row of the saved cost table (Bob, 2026-10-13).
    Updates Cost CAD/USD, currency, Freight Carrier, Recommended Carrier, Transit Days, Carrier Quote #, DI %, Pricing Source and
    Amount (re-applied margin rule) - Amount is kept when a person typed their own price. Adds a note."""
    import html
    import freight_carriers
    keys = ("freight_use_option,freight_quote_request,amount,margin_on_freight_shipment,cost_cad,pipeline,inxpress_quote_number_140,"
            "freight_carrier,freight_quote_summary")
    st, d = fq.hs("GET", f"/crm/v3/objects/deals/{deal_id}?properties={keys}")
    p = d.get("properties", {}) if st == 200 else {}
    if p.get("pipeline") != fq.PIPELINE or not p.get("freight_quote_request"):
        return {"deal": deal_id, "skipped": "no saved cost table"}
    saved = json.loads(p["freight_quote_request"])
    opts = saved.get("options") or []
    try:
        n = int(float(p.get("freight_use_option") or 0))
    except ValueError:
        n = 0
    sel = int(saved.get("selected", 1) or 0)   # 0 = waiting for the Day & Ross direct rate (nothing chosen yet)
    if n == sel:
        return {"deal": deal_id, "skipped": f"option {n} already applied"}
    if not 1 <= n <= len(opts):
        if not dry and p.get("freight_use_option"):
            fq.add_note(deal_id, f"<p>Use Option # {html.escape(str(p.get('freight_use_option')))} is not in the cost table "
                                 f"(1 to {len(opts)}). Nothing was changed.</p>")
        return {"deal": deal_id, "skipped": "option out of range"}
    o, old = opts[n - 1], opts[sel - 1] if 1 <= sel <= len(opts) else {}
    direct = o.get("source") == fq.DR_SOURCE   # typed in from the D&R portal: all-in cost, booked in the portal
    cname, cid = freight_carriers.canonical(o["carrier"])
    di = freight_carriers.di_by_carrier(fq.hs).get(cname) if cid and not direct else None
    cost_true = float(o["cost_cad"]) * (1 + (di or 0) / 100)
    cur = (o.get("cur") or "CAD").rstrip("?")
    props = {"cost_cad": f"{float(o['cost_cad']):.2f}", "cost_usd": f"{float(o['cost']):.2f}" if cur == "USD" else "",
             "freight_quote_currency": "USD" if cur == "USD" else "CAD",
             "freight_carrier": cname if any(cname == x for x, _, _ in freight_carriers.CARRIERS) else "Other",
             "freight_recommended_carrier": f"{o['carrier']} ({o['source']})" if o.get("source") else o["carrier"],
             "freight_transit_days": re.sub(r"\D", "", str(o.get("days") or "")), "carrier_quote_number": o.get("quote_no") or "",
             "freight_di_percent": f"{di:g}" if di is not None else "", "freight_booked_through": "Carrier Direct" if direct else "accuFRATE"}
    if p.get("freight_quote_summary") and props["freight_transit_days"]:   # quote email shows the new carrier's transit
        props["freight_quote_summary"] = re.sub(r"Transit: about [^<\n]*",f"Transit: about {props['freight_transit_days']} business days from pickup",
                                                p["freight_quote_summary"])
    amt_now = (p.get("amount") or "").strip()
    auto = saved.get("auto_amount")
    typed = bool(amt_now) and (auto is None or abs(float(amt_now) - float(auto)) > 0.01)
    new_amt = None
    if not typed:
        plan, m = saved.get("plan"), saved.get("margin") or p.get("margin_on_freight_shipment")
        if plan and plan.get("pct") is not None:
            new_amt = fq.price_from_plan(cost_true, plan)
        elif m:
            new_amt = round(cost_true / (1 - float(m)), 2)
        if new_amt is not None:
            props["amount"] = f"{new_amt:.2f}"
    saved.update({"selected": n, "auto_amount": props.get("amount", auto)})
    props["freight_quote_request"] = json.dumps(saved, separators=(",", ":"))
    try:
        props["freight_carrier_options"] = fq.carrier_options_text(saved)
        props.update(fq.option_fields(props["freight_carrier_options"]))
    except Exception:
        pass
    di_txt = (f"DI {di:g}% (C${float(o['cost_cad']) * di / 100:,.2f}) included. " if di else
              "" if direct else "DI % not set for this carrier. " if di is None else "")
    amt_txt = (f"Amount kept at C${float(amt_now):,.2f} (typed by hand): check the margin." if typed else
               f"Amount recalculated: C${new_amt:,.2f}." if new_amt is not None else "No margin on the deal: set Amount.")
    note = (f"<p><strong>Carrier switched to option {n}</strong> &ndash; quote {html.escape(p.get('inxpress_quote_number_140') or '')}</p><ul>"
            f"<li>Now: {html.escape(o['carrier'])} via {html.escape(o.get('source') or 'accuFRATE')}, {float(o['cost']):,.2f} {html.escape(cur)} "
            f"(C${float(o['cost_cad']):,.2f}), {html.escape(str(o.get('days') or '-'))} days</li>"
            f"<li>Was: {html.escape(old.get('carrier', '-'))}, C${float(old.get('cost_cad') or 0):,.2f}</li>"
            f"<li>{di_txt}{amt_txt}</li><li>" + ("Rate from the Day &amp; Ross portal: book it in the D&amp;R portal, not from HubSpot."
            if direct else f"Booking from HubSpot will book {html.escape(o['carrier'])}.") + "</li></ul>")
    if not dry:
        fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
        fq.add_note(deal_id, note)
    return {"deal": deal_id, "option": n, "carrier": o["carrier"], "amount": props.get("amount"), "typed_price_kept": typed, "dry_run": dry}


def margin_changed(deal_id, dry=False):
    """A person changed Margin % on Freight Shipment: re-price Amount with the new margin (cost incl. carrier DI; a flat $ / minimum
    from the company rule still applies), refresh Option 1-6 prices and the Margin Note. A price typed by hand is kept."""
    import html
    import freight_carriers
    keys = "margin_on_freight_shipment,freight_quote_request,amount,cost_cad,pipeline,freight_carrier,freight_di_percent,inxpress_quote_number_140"
    st, d = fq.hs("GET", f"/crm/v3/objects/deals/{deal_id}?properties={keys}")
    p = d.get("properties", {}) if st == 200 else {}
    if p.get("pipeline") != fq.PIPELINE:
        return {"deal": deal_id, "skipped": "not a Freight Pipeline deal"}
    try:
        m = float(p.get("margin_on_freight_shipment") or "")
    except ValueError:
        return {"deal": deal_id, "skipped": "margin blank"}
    if m >= 1:  # typed as 35 instead of 0.35
        m = m / 100
    saved = json.loads(p["freight_quote_request"]) if p.get("freight_quote_request") else {}
    if saved and saved.get("margin") is not None and abs(float(saved["margin"]) - m) < 1e-9 and (saved.get("plan") or {}).get("pct") in (None, m):
        return {"deal": deal_id, "skipped": "margin unchanged"}
    cost = float(p.get("cost_cad") or 0)
    di = float(p.get("freight_di_percent") or 0)
    cost_true = cost * (1 + di / 100)
    plan = dict(saved.get("plan") or {}, pct=m)
    plan.setdefault("note", "")
    new_amt = fq.price_from_plan(cost_true, plan) if cost else None
    amt_now = (p.get("amount") or "").strip()
    auto = saved.get("auto_amount")
    typed = bool(amt_now) and auto is not None and abs(float(amt_now) - float(auto)) > 0.01
    props = {"freight_margin_note": f"Margin changed to {m * 100:g}% on the deal by a person ({datetime.now().strftime('%Y-%m-%d')})."}
    if p.get("margin_on_freight_shipment") and float(p["margin_on_freight_shipment"]) >= 1:
        props["margin_on_freight_shipment"] = f"{m:g}"
    if new_amt is not None and not typed:
        props["amount"] = f"{new_amt:.2f}"
    if saved:
        saved.update({"margin": m, "plan": plan if saved.get("plan") else None, "auto_amount": props.get("amount", auto)})
        props["freight_quote_request"] = json.dumps(saved, separators=(",", ":"))
        try:
            props["freight_carrier_options"] = fq.carrier_options_text(saved)
            props.update(fq.option_fields(props["freight_carrier_options"]))
        except Exception:
            pass
    if not dry:
        fq.hs("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
        fq.add_note(deal_id, f"<p><strong>Margin changed to {m * 100:g}%</strong> &ndash; quote {html.escape(p.get('inxpress_quote_number_140') or '')}. "
                             + (f"Amount kept at C${float(amt_now):,.2f} (typed by hand): check it." if typed else
                                f"Amount recalculated: C${new_amt:,.2f} (cost C${cost_true:,.2f} incl. DI)." if new_amt is not None else "No cost on the deal.")
                             + " Option 1-6 prices updated.</p>")
    return {"deal": deal_id, "margin": m, "amount": props.get("amount"), "typed_price_kept": typed, "dry_run": dry}
