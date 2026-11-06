"""accuFRATE API quote tool used by the /accufrate-quote skill.

Usage:
    python quote_cli.py request.json [--mode C|T] [--top 10]

request.json (only postal codes, weights and dimensions are required):
{
  "customer": "Maple Ridge Fabrication",          # optional, for file names / notes
  "from_zip": "L5T 2N7", "to_zip": "43228",
  "from_city": "", "from_state": "",              # optional; looked up from the postal code if blank
  "to_city": "", "to_state": "",
  "pickup_date": "2026-10-05",                    # optional; default next business day
  "accessorials": ["Liftgate Delivery"],          # optional; names or accuFRATE codes
  "items": [ {"qty": 2, "length": 48, "width": 40, "height": 50,
              "weight_each": 650, "units": "in-lb",   # or "cm-kg"
              "class": "", "description": "Steel brackets", "type": "pallets"} ]
}

Credentials: user env vars ACCUFRATE_USER / ACCUFRATE_PASS (read from the Windows user
environment if the current process doesn't have them). Never printed.
Output: a markdown report on stdout + CSV/JSON in .\\quotes\\
"""
import csv
import json
import os
import re
import sys
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import accufrate_quote as aq  # noqa: E402

ACCESSORIALS = {
    "airport delivery": 50, "airport pickup": 49, "amazon fba delivery": 93, "appointment": 1, "appointment fee delivery": 1,
    "cfs delivery": 99, "cfs pickup": 98, "construction site delivery": 48, "construction site pickup": 47,
    "farm delivery": 4, "farm pickup": 97, "hazmat": 5, "hotel delivery": 100, "inside delivery": 6, "inside pickup": 7,
    "liftgate delivery": 8, "liftgate pickup": 9, "limited access delivery": 59, "limited access pickup": 58,
    "military delivery": 95, "military pickup": 94, "notification prior to delivery": 90, "notify": 90,
    "over 8 ft": 103, "over 12 ft": 104, "over 16 ft": 105, "over 20 ft": 106, "place of worship delivery": 11,
    "protect from freezing": 18, "residential delivery": 12, "residential pickup": 13, "saturday delivery": 14,
    "saturday pickup": 15, "school delivery": 16, "school pickup": 96, "shopping mall delivery": 101,
    "storage facility delivery": 102, "trade show delivery": 46, "trade show pickup": 45,
}
ACC_NAME = {}
for k, v in ACCESSORIALS.items():
    ACC_NAME.setdefault(v, k.title())

ITEM_TYPES = {"pallets": 11, "pallet": 11, "skids": 11, "skid": 11, "boxes": 2, "box": 2, "crates": 6, "crate": 6,
              "cartons": 4, "drums": 8, "pieces": 12, "rolls": 13, "totes": 16, "bundles": 3, "cases": 5}

DOCK_LINE = ("This price is dock to dock (commercial address, shipping and receiving dock, standard hours). "
             "Unless specifically quoted, it does not include: liftgate pickup or delivery, residential pickup or delivery, "
             "appointment or notification, inside pickup or delivery, limited-access locations (schools, churches, farms, "
             "construction sites, storage units, military bases, trade shows), hazmat, protect from freezing, "
             "over-length pieces (8 ft+), redelivery or storage. These are billed at the carrier's rates if required.")


TRAILER_WIDTH_IN = 96   # usable width used for floor-position math (53 ft van ~98-100 in)
MAX_SKIDS, MAX_LINEAR_FT = 6, 10  # owner rule: above either -> not accuFRATE (volume / FTL RFQ)
MAX_RUNS_EXPECTED = 2   # was 4 (re-ask until expected carriers answer); Bob 2026-10-17: quote with what came back, don't wait
LOW_CARRIER_COUNT = 8   # fewer distinct rates than this = accuFRATE connections probably down
RESELLERS = r"exfreight|blue grace|express save|ils company"


def expected_carriers(lane, from_state, to_state):
    """Owner rule: Day & Ross direct + Manitoulin on every Canada-Canada lane; APEX when either end is in Ontario."""
    exp = []
    if lane == "CA":
        exp += ["Day & Ross direct", "Manitoulin"]
        if "ON" in (from_state, to_state):
            exp.insert(1, "APEX")
    return exp


def carrier_returned(name, quotes):
    pat = {"Day & Ross direct": r"day\s*(and|&)\s*ross", "APEX": r"\bapex\b", "Manitoulin": r"manitoulin"}[name]
    for q in quotes:
        d = q.get("TariffDesc", "") + " " + q.get("CarrierName", "")
        if q.get("cost") and not q.get("ErrorMessage") and re.search(pat, d, re.I):
            if name == "Day & Ross direct" and re.search(RESELLERS, d, re.I):
                continue  # Day & Ross resold via ExFreight etc. doesn't count as direct
            return True
    return False


def missing_expected(expected, quotes):
    return [c for c in expected if not carrier_returned(c, quotes)]


# Customer-specific tariffs: (pattern in the tariff description, customer it belongs to). Such a tariff is only
# used when the quote is for that customer; on anyone else's quote it is dropped (it is their contract price).
CUSTOMER_TARIFFS = [(r"big\s*eric", "Big Eric's")]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def tariff_reserved_for(desc, customer):
    """Name of the customer a tariff is reserved for, when it is NOT this quote's customer; else None."""
    for pat, owner in CUSTOMER_TARIFFS:
        if re.search(pat, desc or "", re.I) and _norm(owner).rstrip("s") not in _norm(customer):
            return owner
    return None


STACK_MAX_IN = 96      # stackable pieces are stacked up to this height (dry van door ~100+ in)


def floor_positions(it):
    """Pieces on the floor: stackable pieces are stacked as high as STACK_MAX_IN allows (Bob 2026-10-22)."""
    q = int(it["quantity"])
    h = float(it.get("height") or 0)
    if it.get("stackable") and h > 0:
        per = max(1, int(STACK_MAX_IN // h))
        return -(-q // per)
    return q


def linear_feet(items):
    """Trailer length used, laid out row by row across a TRAILER_WIDTH_IN trailer (Bob 2026-10-30: area / 8 ft
    undercounted wide skids that can't share a row). Each floor position is turned whichever way fits; a row
    is as long as its deepest piece. 6 standard 48x40 skids = 3 rows of 40 in = 10 LF, as before.
    Stackable pieces count once per stack."""
    spots = []
    for it in items:
        spots += [(float(it["length"]), float(it["width"]))] * floor_positions(it)
    spots.sort(key=lambda s: (max(s), min(s)), reverse=True)
    rows = []  # [width used, row depth]
    for a, b in spots:
        turns = [(x, y) for x, y in ((a, b), (b, a)) if x <= TRAILER_WIDTH_IN] or [(min(a, b), max(a, b))]
        # 1) slot into an existing row without making it longer
        fit = [(r, x, y) for r in rows for x, y in turns if r[0] + x <= TRAILER_WIDTH_IN and y <= r[1]]
        if fit:
            r, x, y = min(fit, key=lambda f: TRAILER_WIDTH_IN - f[0][0] - f[1])
            r[0] += x
            continue
        # 2) new row, turned so the row is as short as possible
        x, y = min(turns, key=lambda t: t[1])
        rows.append([x, y])
    return round(sum(r[1] for r in rows) / 12, 1)


def overlength_code(items):
    longest = max(max(it["length"], it["width"], it["height"]) for it in items)
    if longest >= 240: return 106, longest
    if longest >= 192: return 105, longest
    if longest >= 144: return 104, longest
    if longest >= 96: return 103, longest
    return None, longest


def density_class(pcf):
    # NMFC 13-tier density scale (matches the accuFRATE portal's DIM CLASS)
    for limit, cls in ((50, "50"), (35, "55"), (30, "60"), (22.5, "65"), (15, "70"), (12, "85"), (10, "92.5"),
                       (8, "100"), (6, "125"), (4, "175"), (2, "250"), (1, "300")):
        if pcf >= limit:
            return cls
    return "400"


def is_ca(postal):
    return bool(re.match(r"^[A-Za-z]\d[A-Za-z]", postal.strip()))


def lookup_place(postal):
    p = postal.strip().upper()
    url = f"https://api.zippopotam.us/ca/{p[:3]}" if is_ca(p) else f"https://api.zippopotam.us/us/{p[:5]}"
    try:
        d = json.load(urllib.request.urlopen(url, timeout=15))
        pl = d["places"][0]
        name = re.sub(r"\s*\(.*\)", "", pl["place name"]).strip().upper()
        # FSA names like "OAKVILLE NORTH" / "DARTMOUTH NORTH CENTRAL" -> plain city (some tariffs need it)
        name = re.sub(r"(\s+(NORTH|SOUTH|EAST|WEST|CENTRAL|NORTHEAST|NORTHWEST|SOUTHEAST|SOUTHWEST|DOWNTOWN|INNER|OUTER|REGION))+$", "", name)
        return name, pl.get("state abbreviation", "").upper()
    except Exception:
        return "", ""


def fx_usdcad():
    try:
        o = json.load(urllib.request.urlopen("https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1", timeout=15))["observations"][0]
        return float(o["FXUSDCAD"]["v"]), o["d"]
    except Exception:
        return None, None


def ensure_creds():
    if os.environ.get("ACCUFRATE_USER") and os.environ.get("ACCUFRATE_PASS") and os.environ["ACCUFRATE_USER"].endswith("@accufrate.com"):
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            os.environ["ACCUFRATE_USER"] = winreg.QueryValueEx(k, "ACCUFRATE_USER")[0]
            os.environ["ACCUFRATE_PASS"] = winreg.QueryValueEx(k, "ACCUFRATE_PASS")[0]
    except Exception:
        pass


def source_of(desc):
    for key, label in (("Express Save", "Express Save"), ("ExFreight", "ExFreight"), ("Blue Grace", "Blue Grace"),
                       ("ILS COMPANY", "ILS Company"), ("InXpress Canada", "InXpress Canada"), ("InXpress Corp", "InXpress Corp"),
                       ("InXpress", "InXpress"), ("Inxpress", "InXpress"), ("Master Tariff", "InXpress")):
        if key.lower() in desc.lower():
            return label
    return ""


def currency_of(desc, src, lane):
    d = desc
    if re.search(r"Inter Canada shipments are in CAD and Cross border are in USD", d, re.I):
        return ("CAD" if lane == "CA" else "USD"), "tariff note"
    if re.search(r"rates are (in|quoted in) CAD|\(CAD\)", d, re.I):
        return "CAD", "tariff note"
    if re.search(r"rates are (in|quoted in) USD", d, re.I):
        return "USD", "tariff note"
    if src == "Express Save":
        return "USD", "Express Save bills in USD"
    if lane == "US":
        return "USD", "US lane"
    return ("CAD?" if lane == "CA" else "USD?"), "not stated - unconfirmed"


def service_of(desc):
    m = re.search(r"\(([^)]*(GUARANTEE|Guarantee|MUST ARRIVE|Priority|Economy|Noon|AM Service|Day Definite|Expedit|Volume)[^)]*)\)", desc)
    s = m.group(1).strip() if m else ""
    guaranteed = bool(re.search(r"guarantee|must arrive|noon|am service|day definite|expedit|by 5pm|guaranteed by", s, re.I)
                      or re.search(r"guaranteed (5 ?pm|noon|am|by)|guaranteed(?! service by)", desc.split(" - ")[0], re.I))
    return s, guaranteed


def load_vendor_master():
    path = os.path.join(HERE, "carrier-vendor-master.xlsx")
    rules = []
    if not os.path.exists(path):  # server copy: vendor_rules.json (vendor key, contact mode, priority only; no logins)
        try:
            return [tuple(r) for r in json.load(open(os.path.join(HERE, "vendor_rules.json"), encoding="utf-8"))]
        except Exception:
            return rules
    try:
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True, data_only=True)["Vendors"]
        rows = list(ws.iter_rows(values_only=True))
        hdr = [str(h or "") for h in rows[0]]
        iv, im, ip = hdr.index("Vendor"), hdr.index("Contact mode"), hdr.index("Priority (1-3)")
        for r in rows[1:]:
            if not r[iv]:
                continue
            key = re.sub(r"[^a-z0-9 ]", " ", str(r[iv]).lower())
            key = re.sub(r"\b(inc|ltd|llc|logistics|freight|transport|transportation|lines|express|group)\b", " ", key)
            key = " ".join(key.split())
            if len(key) >= 3:
                rules.append((key, str(r[im] or ""), r[ip]))
    except Exception:
        pass
    return rules


def match_vendor(carrier, rules):
    c = " " + re.sub(r"[^a-z0-9 ]", " ", carrier.lower()) + " "
    best = None
    for key, mode, prio in rules:
        if f" {key} " in c or c.strip().startswith(key):
            if best is None or len(key) > len(best[0]):
                best = (key, mode, prio)
    return best


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        print(__doc__)
        sys.exit(1)
    req_in = json.load(open(args[0], encoding="utf-8"))
    mode = args[args.index("--mode") + 1] if "--mode" in args else "C"
    top = int(args[args.index("--top") + 1]) if "--top" in args else 10
    ensure_creds()

    # --- places
    fz, tz = req_in["from_zip"].strip().upper(), req_in["to_zip"].strip().upper()
    fc, fs = req_in.get("from_city") or "", req_in.get("from_state") or ""
    tc, ts = req_in.get("to_city") or "", req_in.get("to_state") or ""
    if not (fc and fs):
        fc, fs = lookup_place(fz)
    if not (tc and ts):
        tc, ts = lookup_place(tz)
    fca, tca = is_ca(fz), is_ca(tz)
    lane = "CA" if fca and tca else ("US" if not fca and not tca else "XB")

    # --- items
    items, notes, total_lb = [], [], 0
    for i, it in enumerate(req_in["items"][:5], 1):
        metric = str(it.get("units", "in-lb")).lower().startswith("cm")
        L, W, H = (float(it[k]) / (2.54 if metric else 1) for k in ("length", "width", "height"))
        each = float(it["weight_each"]) * (2.20462 if metric else 1)
        qty = int(it.get("qty", 1))
        line_lb = each * qty
        pcf = each / (L * W * H / 1728)
        cls = str(it.get("class") or "").strip() or density_class(pcf)
        if not str(it.get("class") or "").strip():
            notes.append(f"Item {i}: no class given; {L:.0f}x{W:.0f}x{H:.0f} in at {each:.0f} lb = {pcf:.1f} lb/ft3, priced at class {cls}.")
        total_lb += line_lb
        items.append({"class": cls, "weight": line_lb, "length": round(L, 1), "width": round(W, 1), "height": round(H, 1),
                      "quantity": qty, "description": it.get("description") or "Freight",
                      "stackable": bool(it.get("stackable")),
                      "item_type": ITEM_TYPES.get(str(it.get("type", "pallets")).lower(), 11)})

    if len(req_in["items"]) > 5:
        notes.append("accuFRATE allows 5 item lines; only the first 5 were priced.")

    # --- accessorials
    codes, acc_names = [], []
    for a in req_in.get("accessorials", []) or []:
        c = int(a) if str(a).isdigit() else ACCESSORIALS.get(str(a).lower().strip())
        if c and c not in codes:
            codes.append(c)
            acc_names.append(ACC_NAME.get(c, str(a)))
        elif not c:
            notes.append(f"Accessorial '{a}' not recognised - not priced.")

    # --- does it fit the accuFRATE model? (owner rule: max 6 skids and 10 linear feet)
    pieces = sum(x["quantity"] for x in items)
    lf = linear_feet(items)
    olc, longest = overlength_code(items)
    if olc and olc not in codes:
        codes.append(olc)
        acc_names.append(ACC_NAME.get(olc, "Over-length"))
        notes.append(f"OVERLENGTH: longest piece {longest:.0f} in ({longest/12:.1f} ft) - '{ACC_NAME.get(olc)}' accessorial added automatically.")
    reasons = []
    if pieces > MAX_SKIDS:
        reasons.append(f"{pieces} skids/pieces (max {MAX_SKIDS})")
    stacked = any(x.get("stackable") for x in items)
    if stacked:
        notes.append(f"Stackable pieces counted stacked (up to {STACK_MAX_IN} in high): {lf} linear ft.")
    plan_lf = float(req_in.get("load_length_ft") or 0)
    if plan_lf > lf:
        notes.append(f"Customer's load plan shows {plan_lf:g} ft of trailer (our row layout: {lf} ft); using {plan_lf:g} ft.")
        lf = round(plan_lf, 1)
    if lf > MAX_LINEAR_FT:
        reasons.append(f"{lf} linear ft (max {MAX_LINEAR_FT}, " + ("stacking where marked stackable)" if stacked else "assuming no stacking)"))
    elig = {"eligible": not reasons, "pieces": pieces, "linear_ft": lf, "longest_in": round(longest, 1),
            "overlength_code": olc, "reasons": reasons}
    json_out = args[args.index("--json-out") + 1] if "--json-out" in args else None
    summary = {"eligibility": elig, "places": {"from": [fc, fs, fz], "to": [tc, ts, tz], "lane": lane},
               "accessorials": acc_names, "items_priced": items, "total_lb": round(total_lb, 1), "notes": notes}
    if "--check-only" in args or reasons:
        print(json.dumps(elig))
        if json_out:
            json.dump(summary, open(json_out, "w", encoding="utf-8"), indent=1)
        if reasons:
            print("NOT SENT TO ACCUFRATE - route to volume/FTL quote requests (RFQ): " + "; ".join(reasons))
        sys.exit(0 if not reasons else 3)

    pd = req_in.get("pickup_date")
    if not pd:
        d = date.today() + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        pd = d.isoformat()

    req = {"from_zip": fz, "from_city": fc, "from_state": fs, "from_country": "CAN" if fca else "USA",
           "to_zip": tz, "to_city": tc, "to_state": ts, "to_country": "CAN" if tca else "USA",
           "pickup_date": pd, "accessorials": codes, "items": items, "pallets": sum(x["quantity"] for x in items)}
    runs = int(args[args.index("--runs") + 1]) if "--runs" in args else 2
    expected = expected_carriers(lane, fs, ts)
    customer = req_in.get("customer") or ""
    usable = lambda qs: [q for q in qs if not tariff_reserved_for(q.get("TariffDesc", ""), customer)]
    max_runs = max(runs, MAX_RUNS_EXPECTED) if expected else runs
    res, ids, done = None, [], 0
    # some carriers answer slowly / intermittently: merge several runs, and keep asking
    # (up to MAX_RUNS_EXPECTED) until the expected carriers for this lane have answered.
    # Each accuFRATE call takes ~20 s, so the runs go out in parallel rounds (2026-10-17): round 1 = `runs` calls at once,
    # round 2 = the rest up to max_runs, only if expected carriers are still missing.
    from concurrent.futures import ThreadPoolExecutor

    def one(_):
        try:
            st, txt = aq.get_quotes(req, mode)
            return st, aq.parse(txt)
        except Exception as e:
            return 0, {"valid": "false", "error": f"{type(e).__name__}: {e}"}

    last_err = ""
    while done < max_runs and (done < runs or (expected and (res is None or missing_expected(expected, usable(res["quotes"]))))):
        n = runs if done == 0 else max_runs - done
        with ThreadPoolExecutor(max_workers=n) as ex:
            batch = list(ex.map(one, range(n)))
        done += n
        for st, r in batch:
            if r.get("valid") != "true":
                last_err = f"accuFRATE error (HTTP {st}): {r.get('error')}"
                continue
            ids.append(r["quote_id"])
            if res is None:
                res = r
            else:
                have = {(q["TariffDesc"], q["cost"]) for q in res["quotes"]}
                res["quotes"] += [q for q in r["quotes"] if (q["TariffDesc"], q["cost"]) not in have]
    if res is None and last_err:
        print(last_err)
        sys.exit(2)
    if res is None:
        sys.exit(2)
    res["all_quote_ids"] = ids
    dropped = sorted({tariff_reserved_for(q.get("TariffDesc", ""), customer) for q in res["quotes"]} - {None})
    if dropped:
        res["quotes"] = usable(res["quotes"])
        notes.append("Customer-only tariff left out (not this customer): " + ", ".join(dropped) + ".")
    answered = len({q["TariffDesc"] for q in res["quotes"] if q.get("cost") and not q.get("ErrorMessage")})
    missing = missing_expected(expected, res["quotes"])
    checks = {"runs": done, "carriers_answered": answered, "expected": expected, "missing_carriers": missing}
    if missing:
        notes.append(f"PRICING NOT RETURNED IN ACCUFRATE: {', '.join(missing)} (after {done} tries). "
                     f"The quote uses the carriers that did price.")
    if answered < LOW_CARRIER_COUNT:
        notes.append(f"Only {answered} carrier rates came back (normally many more) - accuFRATE carrier connections may be down; re-run later.")

    # --- normalise
    fx, fxd = fx_usdcad()
    rules = load_vendor_master()
    rows, seen = [], set()
    for q in res["quotes"]:
        if q.get("ErrorMessage") or not q.get("cost"):
            continue
        desc = re.sub(r"\s+", " ", q["TariffDesc"])
        carrier = re.split(r"\s*-\s+|\s+-|\(", desc)[0].strip()[:40] or q["CarrierName"]
        src = source_of(desc)
        svc, guar = service_of(desc)
        cur, how = currency_of(desc, src, lane)
        cost = float(q["cost"])
        key = (q.get("CarrierSCAC"), carrier, cost, svc)
        if key in seen:
            continue  # e.g. Blue Grace "All Carriers" / "Reliable Carriers" duplicates
        seen.add(key)
        cad = cost * fx if (cur.startswith("USD") and fx) else cost
        vm = match_vendor(carrier, rules)
        flags = []
        if re.search(r"24 HOURS|SPOT", desc, re.I):
            flags.append("spot quote - valid 24 h")
        if src == "ExFreight" or cur.endswith("?"):
            flags.append("currency unconfirmed")
        if vm and vm[1].lower() == "inactive":
            flags.append("inactive in vendor master - not recommended")
        rows.append({"carrier": carrier, "source": src, "service": svc, "guaranteed": guar, "cost": cost, "cur": cur,
                     "cur_how": how, "cost_cad": round(cad, 2), "fuel": q.get("FuelCharge", ""), "days": q.get("TransitTime", "").lstrip("0") or q.get("TransitTime", ""),
                     "carrier_quote_no": q.get("QuoteNumber", ""), "quote_detail_id": q.get("QuoteDetailID", ""),
                     "scac": q.get("CarrierSCAC", ""), "tariff": q.get("TariffDesc", ""), "priority": (vm[2] if vm else None),
                     "inactive": bool(vm and vm[1].lower() == "inactive"), "flags": "; ".join(flags)})
    rows.sort(key=lambda r: r["cost_cad"])
    std = [r for r in rows if not r["guaranteed"]]
    gua = [r for r in rows if r["guaranteed"]]
    eligible = [r for r in std if not r["inactive"]]
    rec = eligible[0] if eligible else None
    if rec:  # priority tie-break within 5 %
        for r in eligible[1:]:
            if r["cost_cad"] <= rec["cost_cad"] * 1.05 and r["priority"] and (not rec["priority"] or float(r["priority"]) < float(rec["priority"])):
                rec = r
                break

    # --- save
    qdir = os.environ.get("FREIGHT_OUT_DIR") or os.path.join(HERE, "quotes")
    os.makedirs(qdir, exist_ok=True)
    base = f"{date.today().isoformat()}_{res['quote_id']}_{fz.replace(' ', '')}-{tz.replace(' ', '')}"
    with open(os.path.join(qdir, base + ".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["carrier"])
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(qdir, base + ".json"), "w", encoding="utf-8") as f:
        json.dump({"request": req_in, "priced_as": {k: v for k, v in req.items()}, "quote_id": res["quote_id"], "mode": mode,
                   "fx": fx, "fx_date": fxd, "recommended": rec, "rows": rows, "notes": notes}, f, indent=1)
    if json_out:
        summary.update({"quote_id": res["quote_id"], "all_quote_ids": res["all_quote_ids"], "mode": mode, "pickup_date": pd, "checks": checks,
                        "fx": fx, "fx_date": fxd, "recommended": rec, "standard": std, "guaranteed": gua,
                        "csv": os.path.join(qdir, base + ".csv")})
        json.dump(summary, open(json_out, "w", encoding="utf-8"), indent=1)

    # --- report
    def money(r):
        c = r["cur"].rstrip("?")
        return f"{r['cost']:,.2f} {c}" + ("?" if r["cur"].endswith("?") else "")
    print(f"accuFRATE quote #{res['quote_id']} (mode {mode}; merged runs {', '.join(res['all_quote_ids'])}) | {fc} {fs} {fz} -> {tc} {ts} {tz} | lane {lane}")
    print(f"{len(rows)} carrier options after removing duplicates | {pieces} pieces, {total_lb:,.0f} lb, {lf} linear ft | pickup {pd}")
    print(f"Accessorials priced: {', '.join(acc_names) if acc_names else 'none (dock to dock)'}")
    if fx:
        print(f"FX: Bank of Canada USD/CAD {fx} ({fxd})")
    if rec:
        print(f"\nRECOMMENDED: {rec['carrier']} via {rec['source'] or 'accuFRATE'} - {money(rec)} (C${rec['cost_cad']:,.2f}), {rec['days']} days"
              + (f" [{rec['flags']}]" if rec['flags'] else ""))
        if "spot" in rec["flags"]:
            alt = next((r for r in eligible if "spot" not in r["flags"]), None)
            if alt:
                print(f"NON-SPOT ALTERNATIVE: {alt['carrier']} via {alt['source'] or 'accuFRATE'} - {money(alt)} (C${alt['cost_cad']:,.2f}), "
                      f"{alt['days']} days, +C${alt['cost_cad'] - rec['cost_cad']:,.2f}")
    print(f"\n| # | Carrier | Source | Cost | CAD equiv. | Days | Carrier quote # | Notes |\n|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(std[:top], 1):
        print(f"| {i} | {r['carrier']}{' (RECOMMENDED)' if r is rec else ''} | {r['source']} | {money(r)} | {r['cost_cad']:,.2f} | {r['days']} | {r['carrier_quote_no'] or '-'} | {r['flags']} |")
    if gua:
        print("\nGuaranteed / expedited (cheapest 3):")
        for r in gua[:3]:
            print(f"- {r['carrier']} ({r['service']}) via {r['source']}: {money(r)} (C${r['cost_cad']:,.2f}), {r['days']} days")
    if notes:
        print("\nNotes:")
        for n in notes:
            print("- " + n)
    print("\nIncluded: " + (", ".join(acc_names) if acc_names else "no extra services"))
    print(DOCK_LINE)
    print(f"\nSaved: quotes\\{base}.csv and .json")


if __name__ == "__main__":
    main()
