"""Carrier RFQ email tasks. Who gets them is managed in HubSpot (Bob, 2026-10-13):
  contact field "Send RFQs for" (rfq_types: flatbed / volume / ltl) on the carrier person, and the lanes from their
  company's Pickup area (must cover the pickup) and Delivery area (must cover the destination); blank = all areas.
  - flatbed / volume requests: skip accuFRATE, fill the deal, one task per ticked contact.
  - accuFRATE-priced LTL deals: Day & Ross Spot when D&R direct didn't price, plus every contact ticked "LTL".
Each task is linked to the deal and the carrier contact. The person opens the deal, clicks Email, picks the contact and the
shared template named in the task (the greeting fills in from the contact's first name), checks it and sends. Nothing is sent here.
"""
import re
from datetime import datetime, timezone

import freight_queue as fq
import quote_cli

ATLANTIC = ("A", "B", "C", "E")  # first letter of NL, NS, PE, NB postal codes
SHARED_TEMPLATE = {"flatbed": "RFQ - Flatbed", "volume": "RFQ - Volume / FTL", "ltl": "RFQ - LTL"}
# per-carrier templates made before 2026-10-13 (still usable instead of the shared one)
OWN_TEMPLATE = {"161385003057": "RFQ - Day & Ross Flatbed (Harkamalpreet)", "89515902760": "RFQ - QuikX (Jeeta)",
                "132630252406": "RFQ - Titanium (Federico)", "89523336758": "RFQ - Laneway (Victor)",
                "253366236257": "RFQ - NovaXpress (Gurneet)", "144575993914": "RFQ - One Reimer (Brent)",
                "96567455124": "RFQ - Ross Logistics (Howard)", "76406551179": "RFQ - M-O (Bo)",
                "89558949669": "RFQ - Day & Ross Spot (Team)", "247054365369": "RFQ - Express Save (Claude)",
                "252899264628": "RFQ - APPS Cargo (Team)", "252807924901": "RFQ - Challenger (Andre)",
                "240348914612": "RFQ - APPS Express Transport (Elchanan)"}
DR_SPOT = {"carrier": "Day & Ross Spot", "contact": "89558949669", "name": "Team", "template": SHARED_TEMPLATE["ltl"]}  # drspot@
LABEL = {"flatbed": "flatbed", "volume": "volume / FTL", "ltl": "LTL"}
REGION = {**dict.fromkeys("ABCE", "atlantic_canada"), **dict.fromkeys("GHJ", "quebec"),
          **dict.fromkeys("KLMNP", "ontario"), **dict.fromkeys("RSTV", "western_canada")}


def is_atlantic(*postals):
    return any((p or "").strip().upper()[:1] in ATLANTIC for p in postals if p)


def region(postal):
    p = (postal or "").strip().upper()
    return "us" if p[:1].isdigit() else REGION.get(p[:1], "")


def serves_lane(areas, from_zip, to_zip):
    """Carrier Service area (HubSpot checkboxes) covers this lane."""
    ends = [region(from_zip), region(to_zip)]
    if all(e == "us" for e in ends):
        return "us_domestic" in areas
    if "us" in ends:
        return "cross_border" in areas
    return "canada_wide" in areas or all(e and e in areas for e in ends)


PROVINCE = {"A": "nl", "B": "ns", "C": "pe", "E": "nb", "G": "qc", "H": "qc", "J": "qc", "K": "on", "L": "on", "M": "on",
            "N": "on", "P": "on", "R": "mb", "S": "sk", "T": "ab", "V": "bc", "Y": "yt"}


def province(postal):
    """Postal / ZIP -> Pickup/Delivery area value: a province code, or 'us'."""
    p = (postal or "").strip().upper().replace(" ", "")
    if p[:1].isdigit():
        return "us"
    if p[:1] == "X":  # X0A-X0C = Nunavut, rest of X = Northwest Territories
        return "nu" if p[:3] in ("X0A", "X0B", "X0C") else "nt"
    return PROVINCE.get(p[:1], "")


def covers(areas, postal):
    pv = province(postal)
    return bool(pv) and (pv in areas or (pv != "us" and "canada_wide" in areas))


def lane_ok(co_props, from_zip, to_zip):
    """Bob 2026-10-13: Pickup area must cover the pickup; Delivery area must cover the destination. Either one blank = all areas
    (many carriers pick up and interline for delivery). Service area is no longer used."""
    def ok(field, postal, other_postal):
        areas = [a for a in (co_props.get(field) or "").split(";") if a]
        if not areas:
            return True
        if province(postal) == province(other_postal) == "us":  # US-to-US needs "US domestic", not the cross-border "United States"
            return "us_domestic" in areas
        return covers(areas, postal) or (province(postal) == "us" and "us_domestic" in areas)
    return ok("carrier_pickup_area", from_zip, to_zip) and ok("carrier_delivery_area", to_zip, from_zip)


def contacts_for(kind, from_zip, to_zip, hs=None):
    """HubSpot contacts ticked "Send RFQs for" = kind whose company serves the lane -> task dicts. Returns (carriers, warnings)."""
    hs = hs or fq.hs
    st, r = hs("POST", "/crm/v3/objects/contacts/search", {
        "filterGroups": [{"filters": [{"propertyName": "rfq_types", "operator": "IN", "values": [kind]}]}],
        "properties": ["firstname", "lastname", "email", "associatedcompanyid", "carrier_contact_role"], "limit": 100})
    out, warn = [], []
    for c in r.get("results", []) if st == 200 else []:
        cp = c["properties"]
        st, a = hs("GET", f"/crm/v4/objects/contacts/{c['id']}/associations/companies")
        assoc = [str(x["toObjectId"]) for x in a.get("results", [])] if st == 200 else []
        # every company the person is linked to, primary first: the first one whose areas cover the lane names the task
        # (e.g. Sean Muise: APEX (ON/QC) primary, also One Reimer (Canada-wide) - same group, he handles both)
        prim = cp.get("associatedcompanyid")
        order = ([prim] if prim in assoc else []) + [x for x in assoc if x != prim] or ([prim] if prim else [])
        cos = []
        for cid in order:
            st, x = hs("GET", f"/crm/v3/objects/companies/{cid}?properties=name,hub_card_name,carrier_pickup_area,carrier_delivery_area")
            if st == 200:
                cos.append(x.get("properties", {}))
        if not cos:
            warn.append(f"{cp.get('firstname') or cp.get('email')}: ticked for {LABEL[kind]} RFQs but not linked to a company - all lanes used")
            cos = [{}]
        co = next((c for c in cos if lane_ok(c, from_zip, to_zip)), None)
        if co is None:
            continue
        name = (co.get("hub_card_name") or co.get("name") or cp.get("email") or "Carrier").strip()
        first = (cp.get("firstname") or "").strip()
        if not first or first.lower() in name.lower():  # shared inbox, e.g. "APPS Cargo Quotes"
            first = "Team"
        out.append({"carrier": name, "contact": c["id"], "name": first, "template": SHARED_TEMPLATE[kind],
                    "own_template": OWN_TEMPLATE.get(c["id"])})
    out.sort(key=lambda x: x["carrier"].lower())
    return out, warn


def ltl_carriers(hs, from_zip, to_zip, missing_expected=()):
    """LTL RFQ list for an accuFRATE-priced deal: Day & Ross Spot when D&R direct didn't price, plus every contact ticked
    "Send RFQs for" = LTL whose company serves the lane. Returns (carriers, warnings)."""
    out, warn = [], []
    if "Day & Ross direct" in missing_expected:
        out.append(dict(DR_SPOT, own_template=OWN_TEMPLATE.get(DR_SPOT["contact"]), why="Day & Ross didn't return a price in accuFRATE"))
    more, warn = contacts_for("ltl", from_zip, to_zip, hs)
    out += [c for c in more if c["contact"] not in {x["contact"] for x in out}]
    return out, warn


def request_fields(parsed):
    """Deal fields from whatever the request has (no accuFRATE). Missing values are left for the person to fill."""
    props, notes = {}, []
    for k_in, k_post, k_city in (("from_zip", "freight_origin_postal", "freight_origin_city"), ("to_zip", "freight_dest_postal", "freight_dest_city")):
        z = (parsed.get(k_in) or "").strip().upper()
        if z:
            props[k_post] = z
            city, st = quote_cli.lookup_place(z)
            if city:
                props[k_city] = f"{city.title()} {st}".strip()
    items = parsed.get("items") or []
    pieces = sum(int(it.get("qty") or 0) for it in items)
    total_lb, dims, has_dims, lit = 0.0, [], True, []
    for it in items:
        metric = str(it.get("units", "in-lb")).lower().startswith("cm")
        qty = int(it.get("qty") or 0)
        each = float(it.get("weight_each") or 0) * (2.20462 if metric else 1)
        total_lb += each * qty
        L, W, H = (float(it.get(k) or 0) / (2.54 if metric else 1) for k in ("length", "width", "height"))
        if L and W and H:
            dims.append(f"{qty} x {L:g} x {W:g} x {H:g} in" + (f" @ {each:,.0f} lb each" if each else "")
                        + (", stackable" if it.get("stackable") else ""))
            lit.append({"length": L, "width": W, "height": H, "quantity": qty or 1, "stackable": bool(it.get("stackable"))})
        else:
            has_dims = False
    if pieces:
        props["freight_pieces"] = str(pieces)
    if total_lb:
        props["freight_total_weight_lb"] = f"{total_lb:.0f}"
    props["freight_dimensions"] = "\n".join(dims) if dims and has_dims else "Not provided"
    if dims and has_dims:
        try:
            props["freight_linear_feet"] = str(quote_cli.linear_feet(lit))
        except Exception:
            pass
    if not pieces:
        notes.append("number of pieces")
    if not total_lb:
        notes.append("weight")
    if not (dims and has_dims):
        notes.append("dimensions")
    if not parsed.get("from_zip"):
        notes.append("pickup postal code")
    if not parsed.get("to_zip"):
        notes.append("delivery postal code")
    if parsed.get("pickup_date"):
        props["requested_pickup_date"] = fq.date_ms(parsed["pickup_date"])
    props["freight_accessorials"] = fq.acc_values(parsed.get("accessorials") or [])
    return props, notes


def carriers_for(kind, from_zip, to_zip):
    return contacts_for(kind, from_zip, to_zip)[0]


def create_rfq_tasks(deal_id, owner, kind, qnum, carriers, missing, dry, requirements=""):
    """One email task per carrier, linked to the deal and the carrier contact. Returns task subjects."""
    subjects = []
    due = fq.ms(datetime.now(timezone.utc))
    for c in carriers:
        optional = kind == "ltl"   # Bob 2026-10-17: the deal is already priced in accuFRATE, so LTL RFQs (e.g. Day & Ross Spot) are optional
        subj = ("Optional: " if optional else "") + f"Send {LABEL[kind]} RFQ – {c['carrier']} ({c['name']}) – {qnum}"
        tpl = f"\"{c['template']}\"" + (f" (or the carrier's own \"{c['own_template']}\")" if c.get("own_template") else "")
        body = (("OPTIONAL: the quote is already priced and can go to the customer without this. Send it if you want to compare.\n"
                 if optional else "") + (c["why"] + ".\n" if c.get("why") else "")
                + f"On this deal: click Email → To: {c['carrier']} contact ({c['name']}) → Templates → {tpl} → check → Send.\n"
                "Who gets these tasks: the contact's 'Send RFQs for' field in HubSpot.\n"
                + (f"Carrier requirements (in the email from the deal's Carrier Requirements field): {requirements}\n" if requirements else "")
                + "Then mark this task complete.\n"
                + (f"Not in the request yet: {', '.join(missing)}. The email shows 'Not provided' for dimensions; add what you have or ask the customer.\n" if missing else "")
                + ("When they reply: if their price beats the accuFRATE cost on the deal, update Cost CAD, Carrier Quote Number, "
                   "Freight Carrier, Pricing Source (Carrier Direct) and Amount before the quote goes out." if kind == "ltl" else
                   "When they reply: enter their cost in Cost CAD, their quote # in Carrier Quote Number, and set Amount."))
        subjects.append(subj)
        if dry:
            continue
        fq.hs("PUT", f"/crm/v4/objects/contacts/{c['contact']}/associations/default/deals/{deal_id}")  # carrier shows on the deal
        fq.hs("POST", "/crm/v3/objects/tasks", {
            "properties": {"hs_timestamp": due, "hs_task_subject": subj, "hs_task_body": body, "hs_task_type": "EMAIL",
                           "hs_task_status": "NOT_STARTED", "hs_task_priority": "LOW" if optional else "HIGH", "hubspot_owner_id": owner or fq.BOB,
                           "hs_queue_membership_ids": fq.TASK_QUEUE or None},
            "associations": [
                {"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 216}]},
                {"to": {"id": c["contact"]}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}]})
    return subjects
