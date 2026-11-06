"""accuFRATE emails in the platinumsupport@ inbox -> HubSpot freight deals (called by freight_intake for mail from @accufrate.com).

accuFRATE sends (to the email list on each accuFRATE account, which includes platinumsupport@):
  "Dispatch Notification from <customer>"  BOL ID, customer, origin/destination (company, address, contact, email, phone),
                                           pallets, weight, class, pickup window, pickup #, carrier (= tariff name)
  "Bill Of Lading"                          BOL PDF (BOL_<date>_<BOL>.pdf) + from/to/carrier; also sent when NOT dispatched
  "In Transit Status for BOL <n>" / "Delivered Status for BOL <n>"   carrier status updates

What happens:
  Dispatch -> deal with that BOL # exists (booked from HubSpot, or seen before): fill blanks (pickup #), stage -> Carrier
             Informed and Dispatched, note. No deal (customer booked it in accuFRATE themselves, e.g. O'Dell, or we booked in
             the portal): new Freight deal at Carrier Informed and Dispatched with everything from the email, company found by
             name, and an ESTIMATED carrier cost (re-quote of the same tariff; check against the carrier invoice) + task.
  Bill Of Lading -> waits, then only acts if no deal has that BOL yet (BOL created but not dispatched): new deal at
             Quote Submitted with a "not dispatched yet" task.
  Status -> deal with that BOL: In Transit / Delivered stage (never backwards), Latest Tracking, Actual Pickup / Delivered Date.
             No deal -> ignored (older shipments).
"""
import html
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import freight_queue as fq

STAGES = {"qrr": fq.STAGE_QRR, "qs": os.environ.get("FREIGHT_STAGE_QS_ID", "993307651"),
          "dispatched": os.environ.get("FREIGHT_STAGE_DISPATCHED_ID", "993307653"),
          "transit": os.environ.get("FREIGHT_STAGE_TRANSIT_ID", "1363475382"),
          "delivered": os.environ.get("FREIGHT_STAGE_DELIVERED_ID", "993307652"),
          "invoiced": os.environ.get("FREIGHT_STAGE_INVOICED_ID", "994376729"),
          "lost": os.environ.get("FREIGHT_STAGE_LOST_ID", "993307654")}
ORDER = [STAGES[k] for k in ("qrr", "qs", "dispatched", "transit", "delivered", "invoiced")]
OUR_ACCOUNT = re.compile(r"^inxpress\b", re.I)          # "InXpress #140CA Halifax" = booked on our own account
SUB_CUSTOMERS = {"o'dell hvac": "30530"}                 # accuFRATE customer name -> ClientReferenceId (quotes with their tariffs)
LABELS = ["BOL ID", "Customer Name", "Origin", "Destination", "3rd Party", "Total Pallets", "Total Weight (lbs)", "Class",
          "Pickup Date and Time Window", "Pickup Number", "Carrier Name", "Origin Terminal Phone", "Destination Terminal Phone"]
CITY = re.compile(r"^(.+?),\s*([A-Z]{2})\.?\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d|\d{5}(?:-\d{4})?)\s*$", re.I)
PHONE = re.compile(r"^\(?\+?[\d][\d\s().-]{6,}\d$")
DEAL_FIELDS = ["dealname", "dealstage", "pipeline", "hubspot_owner_id", "bill_of_lading____bol_", "pickup_confirmation", "pro",
               "inxpress_quote_number_140", "actual_pickup_date", "delivered_date"]


def kind(subject):
    s = (subject or "").lower()
    if s.startswith("dispatch notification"):
        return "dispatch"
    if "bill of lading" in s:
        return "bol"
    m = re.search(r"(in transit|delivered|out for delivery|picked up|exception)\s+status for bol\s+(\d+)", s)
    return ("status", m.group(1), m.group(2)) if m else None


# ---------------------------------------------------------------- parsing
def _clean(s):
    s = re.sub(r"<mailto:[^>]*>|<https?:[^>]*>", "", s or "")
    return re.sub(r"[ \t\xa0]+", " ", s).strip(" ,\t")


def parse_dispatch(text):
    pat = re.compile(r"^\s*(" + "|".join(re.escape(l) for l in LABELS) + r")\s*:", re.M)
    hits = list(pat.finditer(text or ""))
    out = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out[m.group(1)] = text[m.end():end].split("Please click on the unsubscribe")[0].strip()
    return out


def parse_party(block):
    """Address block -> company, address, address2, city, state, postal, contact, email, phone."""
    lines = [_clean(l) for l in re.split(r"\n", block or "")]
    lines = [l for l in lines if l]
    p = {k: "" for k in ("company", "address", "address2", "city", "state", "postal", "contact", "email", "phone")}
    ci = next((i for i, l in enumerate(lines) if CITY.match(l)), None)
    if ci is None:
        return p
    m = CITY.match(lines[ci])
    p.update(city=m.group(1).strip().title(), state=m.group(2).upper(), postal=m.group(3).upper())
    if re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", p["postal"]):
        p["postal"] = p["postal"][:3] + " " + p["postal"][3:]
    before = lines[:ci]
    if before:
        p["company"] = before[0]
        p["address"] = before[1] if len(before) > 1 else ""
        p["address2"] = ", ".join(before[2:])
    for l in lines[ci + 1:]:
        if "@" in l and not p["email"]:
            p["email"] = l.split()[0]
        elif PHONE.match(l) and not p["phone"]:
            p["phone"] = l
        elif not p["contact"] and not l.lower().startswith(("phone", "account")):
            p["contact"] = l
    if p["email"].lower().endswith("@inxpress.com"):  # our own booking address, not the shipper's
        p["email"] = ""
    return p


def parse_window(s):
    """'9/1/2026 Between 10:00 AM - 04:00 PM' -> (date, '10:00', '16:00')."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    d = date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
    import freight_book
    t = re.findall(r"(\d{1,2}:\d{2}\s*[AP]M)", s or "", re.I)
    return d, (freight_book.norm_time(t[0]) if t else ""), (freight_book.norm_time(t[1]) if len(t) > 1 else "")


def bol_from_files(files, text=""):
    for n, _ in files:
        m = re.search(r"BOL_\d+_(\d+)", n or "")
        if m:
            return m.group(1)
    m = re.search(r"\bBOL\D{0,10}(\d{6,9})\b", text or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- CRM
def deal_by_bol(bol):
    st, r = fq.hs("POST", "/crm/v3/objects/deals/search", {
        "filterGroups": [{"filters": [{"propertyName": "bill_of_lading____bol_", "operator": "CONTAINS_TOKEN", "value": str(bol)}]}],
        "properties": DEAL_FIELDS, "limit": 5})
    for d in r.get("results", []) if st == 200 else []:
        if re.search(rf"\b{bol}\b", d["properties"].get("bill_of_lading____bol_") or ""):
            return d
    return None


def find_company(name):
    """The ONE HubSpot company whose name matches (case/punctuation-insensitive), else None."""
    if not name or OUR_ACCOUNT.match(name):
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    st, r = fq.hs("POST", "/crm/v3/objects/companies/search", {"query": name, "properties": ["name", "hubspot_owner_id"], "limit": 20})
    res = r.get("results") or [] if st == 200 else []
    hits = [c for c in res if norm(c["properties"].get("name")) == norm(name)]
    if not hits:  # "O'Dell HVAC" (accuFRATE) vs "O'Dell HVAC Group" (HubSpot)
        hits = [c for c in res if norm(c["properties"].get("name")).startswith(norm(name)) or norm(name).startswith(norm(c["properties"].get("name")) or "~")]
    return hits[0] if len(hits) == 1 else None


def later(stage_a, stage_b):
    """True if stage_a is after stage_b in the pipeline (Closed Lost / unknown never counts as later)."""
    return stage_a in ORDER and stage_b in ORDER and ORDER.index(stage_a) > ORDER.index(stage_b)


# ---------------------------------------------------------------- BOL PDF (real dims) via the accuFRATE Image API
DOC_NAMES = {"ACCUBOL": "BOL", "BOL": "Carrier BOL", "POD": "POD", "WGPOD": "POD", "WI": "Weight inspection", "INS": "Insurance certificate",
             "LBL": "Label", "ZLBL": "Label 4x6"}
BOOKED_DOCS = ("ACCUBOL", "LBL", "ZLBL")        # exist as soon as the BOL is created
DELIVERED_DOCS = ("POD", "BOL", "WI", "INS")    # carrier-signed BOL, POD, weight inspection, insurance: after delivery / when they apply


def get_image(bol, image_type="ACCUBOL", mode="C"):
    """A shipment document from the accuFRATE Image API as (bytes, format) or (None, error). Types: ACCUBOL (accuFRATE BOL),
    BOL (carrier BOL), POD, WGPOD, WI (weight inspection), INS (insurance), LBL/ZLBL (labels). Works for sub-customers' BOLs too."""
    import base64
    import urllib.request
    from xml.sax.saxutils import escape
    import quote_cli
    quote_cli.ensure_creds()
    body = (f'<GetImage xmlns="http://tempuri.org/"><request xmlns:a="http://schemas.datacontract.org/2004/07/accufRATE.accufRATE">'
            f'<a:Authentication><a:Mode>{mode}</a:Mode><a:Password>{escape(os.environ["ACCUFRATE_PASS"])}</a:Password>'
            f'<a:UserName>{escape(os.environ["ACCUFRATE_USER"])}</a:UserName></a:Authentication>'
            f'<a:BolId>{int(bol)}</a:BolId><a:ImageType>{image_type}</a:ImageType></request></GetImage>')
    env = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>{body}</s:Body></s:Envelope>'
    rq = urllib.request.Request("https://webservice.accufrate.com/ImageService.svc", data=env.encode(), method="POST",
                                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"http://tempuri.org/IImageService/GetImage"'})
    try:
        x = urllib.request.urlopen(rq, timeout=90).read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    m, f = re.search(r"ImageData>([^<]+)<", x), re.search(r"ImageFormat>([^<]+)<", x)
    if m and re.search(r"IsValid>true<", x, re.I):
        return base64.b64decode(m.group(1)), (f.group(1).lower() if f else "pdf")
    e = re.search(r"ErrorMessage>([^<]*)<", x)
    return None, (e.group(1) if e else "not available")


def get_bol_pdf(bol):
    data, _ = get_image(bol, "ACCUBOL")
    return data


def _upload(name, data, fmt):
    """Upload to HubSpot File Manager (private, folder /Freight Documents) -> file id. Needs the 'files' scope."""
    import json
    import urllib.error
    import urllib.request
    import uuid
    b = "----ix" + uuid.uuid4().hex
    ctype = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "tif": "image/tiff"}.get(fmt, "application/octet-stream")
    parts = [("folderPath", "/Freight Documents"), ("fileName", name),
             ("options", json.dumps({"access": "PRIVATE", "overwrite": False, "duplicateValidationStrategy": "NONE"}))]
    crlf = chr(13) + chr(10)
    body = b"".join(f'--{b}{crlf}Content-Disposition: form-data; name="{k}"{crlf}{crlf}{v}{crlf}'.encode() for k, v in parts)
    body += (f'--{b}{crlf}Content-Disposition: form-data; name="file"; filename="{name}"{crlf}Content-Type: {ctype}{crlf}{crlf}'.encode()
             + data + f"{crlf}--{b}--{crlf}".encode())
    rq = urllib.request.Request("https://api.hubapi.com/files/v3/files", data=body, method="POST",
                                headers={"Authorization": "Bearer " + fq.env("HUBSPOT_TOKEN"), "Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        return json.loads(urllib.request.urlopen(rq, timeout=90).read())["id"], ""
    except urllib.error.HTTPError as e:
        return None, f"HubSpot file upload {e.code}: {e.read()[:200]!r}"


def file_exists(base):
    st, r = fq.hs("GET", f"/files/v3/files/search?name={base}&limit=5")
    return st == 200 and any((f.get("name") or "").startswith(base) for f in r.get("results", []))


def attach_docs(deal_id, bol, types=("ACCUBOL",), test=False):
    """Fetch the accuFRATE documents for a BOL and attach them to the deal (one note, files in the deal's Attachments).
    Returns (attached names, problems)."""
    ids, names, problems = [], [], []
    for t in types:
        if file_exists(f"{DOC_NAMES.get(t, t).replace(' ', '_')}_{bol}"):
            continue
        data, fmt = get_image(bol, t, "T" if test else "C")
        if not data:
            problems.append(f"{DOC_NAMES.get(t, t)}: {fmt}")
            continue
        name = f"{DOC_NAMES.get(t, t).replace(' ', '_')}_{bol}.{fmt}"
        fid, err = _upload(name, data, fmt)
        if fid:
            ids.append(str(fid))
            names.append(name)
        else:
            problems.append(err)
    if ids:
        fq.hs("POST", "/crm/v3/objects/notes", {"properties": {"hs_timestamp": fq.ms(datetime.now(timezone.utc)),
                                                               "hs_note_body": f"<p>accuFRATE documents for BOL {bol}: {html.escape(', '.join(names))}</p>",
                                                               "hs_attachment_ids": ";".join(ids)},
                                                "associations": [{"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]}]})
    return names, problems


def bol_items(pdf_bytes):
    """Item lines from the accuFRATE BOL PDF: [{qty, length, width, height, class, weight (line total lb), description}].
    The table has fixed columns (Units x~59, Commodity x~136, L ~379, W ~410, H ~441, Class ~470, Weight ~541)."""
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        ws = pdf.pages[0].extract_words()
    head = next((w for w in ws if w["text"] == "Commodity"), None)
    tot = next((w for w in ws if w["text"] == "Total" and head and w["top"] > head["top"]), None)
    if not head:
        return []
    rows = {}
    for w in ws:
        if head["top"] + 5 < w["top"] < (tot["top"] - 3 if tot else head["top"] + 95):
            rows.setdefault(round(w["top"]), []).append(w)
    col = lambda r, a, b: "".join(w["text"] for w in sorted(r, key=lambda w: w["x0"]) if a <= w["x0"] < b)
    items = []
    for y in sorted(rows):
        r = rows[y]
        L, W, H = col(r, 365, 400), col(r, 400, 430), col(r, 430, 462)
        if not (L and W and H):
            continue
        try:
            items.append({"qty": int(col(r, 50, 75) or 1), "length": float(L), "width": float(W), "height": float(H),
                          "class": col(r, 462, 495), "weight": float(col(r, 525, 580) or 0),
                          "description": " ".join(w["text"] for w in sorted(r, key=lambda w: w["x0"]) if 130 <= w["x0"] < 365)})
        except ValueError:
            continue
    return items


# ---------------------------------------------------------------- estimated cost
def estimate_cost(info, o, d, items=None):
    """Re-quote the same shipment (pallets, weight, class, no dims) and take the cost of the SAME tariff as the BOL's carrier."""
    import accufrate_quote as aq
    import quote_cli
    quote_cli.ensure_creds()
    carrier = re.sub(r"\s+", " ", info.get("Carrier Name") or "").strip()
    try:
        weight = float(re.sub(r"[^\d.]", "", info.get("Total Weight (lbs)") or "") or 0)
        pallets = int(re.sub(r"\D", "", info.get("Total Pallets") or "") or 1)
    except ValueError:
        return None, "weight/pallets not readable"
    cls = re.sub(r"[^\d.]", "", info.get("Class") or "")
    if not (carrier and weight and cls and o["postal"] and d["postal"]):
        return None, "carrier, weight, class or postal code missing in the email"
    ca = lambda pc: bool(re.match(r"^[A-Z]\d[A-Z]", pc))
    pd = date.today() + timedelta(days=1)
    while pd.weekday() >= 5:
        pd += timedelta(days=1)
    req = {"from_zip": o["postal"], "from_city": o["city"].upper(), "from_state": o["state"], "from_country": "CAN" if ca(o["postal"]) else "USA",
           "to_zip": d["postal"], "to_city": d["city"].upper(), "to_state": d["state"], "to_country": "CAN" if ca(d["postal"]) else "USA",
           "pickup_date": pd.isoformat(), "accessorials": [], "pallets": pallets,
           # real item lines from the BOL PDF when we have them; else assume 48x40x48 pallets (accuFRATE returns nothing without dims)
           "items": ([{"class": it["class"] or cls, "weight": it["weight"], "item_type": 11, "description": "Freight", "length": it["length"],
                       "width": it["width"], "height": it["height"], "quantity": it["qty"]} for it in items[:5]] if items else
                     [{"class": cls, "weight": weight, "item_type": 11, "description": "Freight", "length": 48, "width": 40, "height": 48,
                       "quantity": pallets}]),
           "client_reference_id": SUB_CUSTOMERS.get((info.get("Customer Name") or "").strip().lower())}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for _ in range(3):  # carriers answer intermittently
        st, txt = aq.get_quotes(req, "C")
        r = aq.parse(txt)
        same = [q for q in r.get("quotes", []) if q.get("cost") and not q.get("ErrorMessage") and norm(q.get("TariffDesc")) == norm(carrier)]
        if same:
            q = min(same, key=lambda q: float(q["cost"]))
            lane = "CA" if ca(o["postal"]) and ca(d["postal"]) else ("US" if not ca(o["postal"]) and not ca(d["postal"]) else "XB")
            cur, _ = quote_cli.currency_of(carrier, quote_cli.source_of(carrier), lane)
            cost = float(q["cost"])
            fx, _ = quote_cli.fx_usdcad() if cur.startswith("USD") else (None, None)
            return {"cost": cost, "cur": cur, "cost_cad": round(cost * fx, 2) if fx else cost, "quote_id": r.get("quote_id"),
                    "carrier_quote_no": q.get("QuoteNumber") or "", "dims": "BOL dims" if items else "48x40x48 assumed"}, ""
    return None, f"'{carrier}' did not return a price on the re-quote"


# ---------------------------------------------------------------- handlers
def handle(thread_id, subject, text, files, received, dry):
    k = kind(subject)
    if not k:
        return {"thread": thread_id, "skipped": f"accuFRATE email not handled: {subject}"}
    if k == "dispatch":
        return on_dispatch(thread_id, subject, text, files, received, dry)
    if k == "bol":
        return on_bol(thread_id, subject, text, files, received, dry)
    return on_status(thread_id, k[1], k[2], text, received, dry)


def on_status(thread_id, status, bol, text, received, dry):
    deal = deal_by_bol(bol)
    if not deal:
        return {"thread": thread_id, "skipped": f"status '{status}' for BOL {bol}: no deal with that BOL"}
    p = deal["properties"]
    line = _clean(re.split(r"\n|Please click", text or "")[0])[:200]
    props = {"freight_latest_tracking": f"{received[:10]}: {line}"}
    target = {"in transit": STAGES["transit"], "picked up": STAGES["transit"], "delivered": STAGES["delivered"]}.get(status)
    if target and later(target, p.get("dealstage")):
        props["dealstage"] = target
    day = fq.date_ms(received[:10])
    if status in ("in transit", "picked up") and not p.get("actual_pickup_date"):
        props["actual_pickup_date"] = day
    if status == "delivered" and not p.get("delivered_date"):
        props["delivered_date"] = day
    docs = None
    if not dry:
        fq.hs("PATCH", f"/crm/v3/objects/deals/{deal['id']}", {"properties": props})
        if status == "delivered":
            docs = _safe_attach(deal["id"], bol, DELIVERED_DOCS)
    return {"thread": thread_id, "deal": deal["id"], "status": status, "set": props, "docs": docs, "dry_run": dry}


def _safe_attach(deal_id, bol, types):
    try:
        return attach_docs(deal_id, bol, types)
    except Exception as e:  # documents never block the deal update
        return [], [f"{type(e).__name__}: {e}"]


def on_bol(thread_id, subject, text, files, received, dry):
    bol = bol_from_files(files, text)
    if not bol:
        return {"thread": thread_id, "skipped": "Bill Of Lading email without a BOL number"}
    if not dry:
        time.sleep(int(os.environ.get("FREIGHT_BOL_WAIT_S", "75")))  # the Dispatch Notification (if any) arrives seconds later
    if deal_by_bol(bol):
        return {"thread": thread_id, "skipped": f"BOL {bol} already on a deal"}
    m = lambda lab: (re.search(rf"shipped {lab} (.+)", text or "", re.I) or [None, ""])[1].strip()
    o, d = parse_party(m("from")), parse_party(m("to"))
    info = {"BOL ID": bol, "Carrier Name": m("with"), "Customer Name": ""}
    return create_deal(thread_id, info, o, d, "qs", text, files, received, dry,
                       note_extra="BOL created in accuFRATE but NOT dispatched (no Dispatch Notification).")


def on_dispatch(thread_id, subject, text, files, received, dry):
    info = parse_dispatch(text)
    bol = re.sub(r"\D", "", info.get("BOL ID") or "") or bol_from_files(files, text)
    if not bol:
        return {"thread": thread_id, "skipped": "Dispatch Notification without a BOL ID"}
    info["BOL ID"] = bol
    o, d = parse_party(info.get("Origin")), parse_party(info.get("Destination"))
    pick = _clean(info.get("Pickup Number"))
    deal = deal_by_bol(bol)
    if deal:  # booked from HubSpot (or already captured): fill blanks, move stage, note
        p = deal["properties"]
        props = {}
        if pick and not p.get("pickup_confirmation"):
            props["pickup_confirmation"] = pick
        if later(STAGES["dispatched"], p.get("dealstage")):
            props["dealstage"] = STAGES["dispatched"]
        if not dry:
            if props:
                fq.hs("PATCH", f"/crm/v3/objects/deals/{deal['id']}", {"properties": props})
            fq.add_note(deal["id"], f"<p><strong>accuFRATE Dispatch Notification</strong> &ndash; BOL {bol}, pickup # {html.escape(pick or 'not given')}, "
                                    f"{html.escape(info.get('Pickup Date and Time Window') or '')}, {html.escape(info.get('Carrier Name') or '')}</p>")
            return {"thread": thread_id, "deal": deal["id"], "updated": props, "docs": _safe_attach(deal["id"], bol, BOOKED_DOCS)}
        return {"thread": thread_id, "deal": deal["id"], "updated": props, "dry_run": dry}
    return create_deal(thread_id, info, o, d, "dispatched", text, files, received, dry)


def create_deal(thread_id, info, o, d, stage, text, files, received, dry, note_extra=""):
    import freight_carriers
    bol, cust = info["BOL ID"], (info.get("Customer Name") or "").strip()
    own = bool(OUR_ACCOUNT.match(cust)) or not cust
    company = find_company(cust) if not own else None
    flags = []
    if own:  # booked on our own accuFRATE account: the customer is usually the shipper or the consignee
        company = find_company(o["company"]) or find_company(d["company"])
        if not company:
            flags.append("Booked on our own accuFRATE account - link the customer company (shipper or consignee) by hand.")
    elif not company:
        flags.append(f"No single HubSpot company named '{cust}' - link the customer company by hand.")
    owner = (company or {}).get("properties", {}).get("hubspot_owner_id") or fq.BOB
    cname = (company or {}).get("properties", {}).get("name") or (cust if not own else o["company"] or "accuFRATE booking")
    pd, ready, close = parse_window(info.get("Pickup Date and Time Window"))
    carrier = re.sub(r"\s+", " ", info.get("Carrier Name") or "").strip()
    canon, _ = freight_carriers.canonical(carrier)
    try:
        pdf = get_bol_pdf(bol)
        items = bol_items(pdf) if pdf else []
    except Exception:
        items = []
    est, why = estimate_cost(info, o, d, items) if carrier else (None, "no carrier")
    qnum = fq.next_quote_number()
    lane = f"{o['city']} to {d['city']}".strip() if o["city"] or d["city"] else ""
    props = {"dealname": f"{qnum} - {cname}" + (f" {lane}" if lane else ""), "pipeline": fq.PIPELINE, "dealstage": STAGES[stage],
             "hubspot_owner_id": owner, "inxpress_quote_number_140": qnum, "bill_of_lading____bol_": bol,
             "pickup_confirmation": _clean(info.get("Pickup Number")), "freight_request_type": "Book now",
             "freight_booked_through": "accuFRATE", "carriers___canada_freight": "Accufrate",   # -> tracking-link workflow
             "freight_carrier": canon if any(canon == n for n, _, _ in freight_carriers.CARRIERS) else ("Other" if carrier else ""),
             "freight_origin_postal": o["postal"], "freight_origin_city": f"{o['city']} {o['state']}".strip(),
             "freight_dest_postal": d["postal"], "freight_dest_city": f"{d['city']} {d['state']}".strip(),
             "freight_pieces": re.sub(r"\D", "", info.get("Total Pallets") or ""),
             "freight_total_weight_lb": re.sub(r"[^\d.]", "", info.get("Total Weight (lbs)") or ""),
             "freight_class": re.sub(r"[^\d.]", "", info.get("Class") or ""),
             "freight_dimensions": chr(10).join(f"{it['qty']} x {it['length']:g} x {it['width']:g} x {it['height']:g} in @ {it['weight'] / max(it['qty'], 1):,.0f} lb each, class {it['class']}"
                                             for it in items),
             "freight_pickup_ready_time": ready, "freight_pickup_close_time": close,
             "freight_booking_status": f"Captured from accuFRATE email {received[:10]}: BOL {bol}",
             "description": (f"Email thread: {thread_id}\nBooked in accuFRATE ({cust or 'unknown account'}), captured from the accuFRATE email "
                             f"{received}.\nCarrier/tariff: {carrier}\n\n" + (text or "")[:6000])}
    if pd:
        props["requested_pickup_date"] = fq.date_ms(pd.isoformat())
    for side, x in (("shipper", o), ("consignee", d)):
        props.update({f"freight_{side}_company": x["company"], f"freight_{side}_address": x["address"], f"freight_{side}_address2": x["address2"],
                      f"freight_{side}_contact": x["contact"], f"freight_{side}_phone": x["phone"], f"freight_{side}_email": x["email"]})
    # The estimate is NOT written to Cost CAD (it drives price/margin). Tested 2026-10-09 on BOL 1573427: with 48x40x48 assumed it was
    # 26% under the real cost; with the real dims from the BOL PDF it was within 0.5% (C$3,020.99 vs C$3,034.09). Guide only for now.
    if est and est.get("dims") == "BOL dims":  # Bob 2026-10-09: put the estimate in Cost CAD (flagged), only when based on the real dims
        props.update({"cost_cad": f"{est['cost_cad']:.2f}", "cost_usd": f"{est['cost']:.2f}" if est["cur"].startswith("USD") else "",
                      "freight_quote_currency": "USD" if est["cur"].startswith("USD") else "CAD",
                      "freight_accufrate_quote_id": str(est["quote_id"] or "")})
        props["freight_booking_status"] += f" | Cost CAD = ESTIMATE {est['cost']:,.2f} {est['cur']} (re-quote with BOL dims) - check the invoice"
    elif est:
        props["freight_booking_status"] += f" | est. cost ~{est['cost']:,.0f} {est['cur']} (assumed dims, not in Cost CAD)"
    props = {k: v for k, v in props.items() if v not in (None, "")}
    if dry:
        return {"thread": thread_id, "dry_run": True, "would_create": props, "company": (company or {}).get("id"), "flags": flags,
                "estimate": est, "estimate_note": why}
    assoc = [{"to": {"id": company["id"]}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}]}] if company else []
    st, deal = fq.hs("POST", "/crm/v3/objects/deals", {"properties": props, "associations": assoc})
    if st != 201:
        raise RuntimeError(f"deal create {st}: {deal.get('message', '')}")
    in_cost = bool(est and est.get("dims") == "BOL dims")
    est_line = (f"<li>{'ESTIMATED cost entered in Cost CAD' if in_cost else 'ROUGH cost guide (not entered in Cost CAD)'}: "
                f"{est['cost']:,.2f} {est['cur']} (C${est['cost_cad']:,.2f}) from a re-quote of the "
                f"same tariff today (accuFRATE quote #{est['quote_id']}; {est.get('dims', '')}, no extra services). "
                f"{'Replace it with the real cost from the carrier invoice if different.' if in_cost else 'Enter the real cost in Cost CAD from accuFRATE (Quote History / BOL) or the carrier invoice.'}</li>"
                if est else f"<li>No cost guide: {html.escape(why or 'not attempted')}. Enter Cost CAD from accuFRATE / the invoice.</li>")
    chk = "".join(f"<li>{html.escape(f)}</li>" for f in flags)
    fq.add_note(deal["id"], f"<p><strong>Shipment booked in accuFRATE &ndash; BOL {bol}</strong> ({html.escape(cust or 'account unknown')})</p><ul>"
                            f"<li>Carrier/tariff: {html.escape(carrier)}</li>{est_line}{chk}"
                            + (f"<li>{html.escape(note_extra)}</li>" if note_extra else "")
                            + "<li>Attachments: " + html.escape(", ".join(n for n, _ in files) or "none") + "</li></ul>")
    todo = [("check the ESTIMATED Cost CAD against the carrier invoice" if in_cost else "enter the real carrier cost in Cost CAD (accuFRATE or the invoice)")
            + ", then set margin / Amount (the price billed to the customer)"] + [f.split(" - ")[-1] for f in flags]
    if stage == "qs":
        todo.insert(0, "the BOL is NOT dispatched yet - dispatch it (accuFRATE or Shipment Action 'Dispatch now')")
    fq.add_task(deal["id"], owner, f"New accuFRATE booking - BOL {bol} - {cname}", "Captured from the accuFRATE email. To do:\n- " + "\n- ".join(todo))
    docs = _safe_attach(deal["id"], bol, BOOKED_DOCS)
    return {"thread": thread_id, "deal": deal["id"], "created": props["dealname"], "estimate": est, "flags": flags, "docs": docs}


def pod_sweep(days=21, limit=40):
    """Daily (Vercel cron): Delivered/Invoiced deals changed in the last `days` days with a BOL # -> attach POD, carrier BOL, weight
    inspection, insurance certificate when accuFRATE has them (often not ready on the delivery day). Already-attached files are skipped."""
    since = fq.ms(datetime.now(timezone.utc) - timedelta(days=days))
    st, r = fq.hs("POST", "/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": "pipeline", "operator": "EQ", "value": fq.PIPELINE},
        {"propertyName": "dealstage", "operator": "IN", "values": [STAGES["delivered"], STAGES["invoiced"]]},
        {"propertyName": "bill_of_lading____bol_", "operator": "HAS_PROPERTY"},
        {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": since}]}],
        "properties": ["bill_of_lading____bol_"], "limit": limit})
    out = []
    for d in r.get("results", []) if st == 200 else []:
        m = re.search(r"\d{6,9}", d["properties"].get("bill_of_lading____bol_") or "")
        if m and not (d["properties"].get("bill_of_lading____bol_") or "").upper().startswith("TEST"):
            names, problems = _safe_attach(d["id"], m.group(0), DELIVERED_DOCS)
            if names:
                out.append({"deal": d["id"], "attached": names})
    return out
