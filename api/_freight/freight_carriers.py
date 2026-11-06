"""Carrier names: accuFRATE's many spellings -> one clean name (the deal's Carrier dropdown) + the HubSpot
Vendor company (which holds the carrier's DI %).

DI % (company property di_percent): 0 = confirmed none; blank = not known yet (treated as 0 but flagged).
"""
import re

# (clean name, pattern matched against the accuFRATE tariff description / carrier name, Vendor company ID or None)
CARRIERS = [
    ("Day & Ross", r"day\s*(and|&)\s*ross", "59069877883"),
    ("APEX", r"\bapex\b", "36319537196"),
    ("CCT / WCE", r"\bcct\b|\bwce\b", "58956696452"),
    ("Manitoulin", r"manitoulin", "53366776399"),
    ("Maritime-Ontario", r"maritime[\s-]*ontario", "36317876866"),
    ("TST-CF", r"tst[\s-]*cf", "58962268767"),
    ("Vitran", r"vitran|kindersley", "36297220194"),
    ("Overland West", r"overland", "25649612849"),
    ("Comox", r"comox", "36318910636"),
    ("Central Transport", r"central\s*transport", "58968543102"),
    ("ABF Freight", r"\babf\b|arcbest", "55402737054"),
    ("TForce Freight", r"t\s*-?\s*force", "58948953641"),
    ("XPO", r"\bxpo\b", "58967998488"),
    ("Estes", r"estes", None),
    ("Saia", r"\bsaia\b", None),
    ("Old Dominion", r"old\s*dominion|\bodfl\b", None),
    ("R+L", r"\br\s*(\+|&)\s*l\b", None),
    ("Ward", r"\bward\b", None),
    ("Forward Air", r"forward\s*air", None),
    ("A. Duie Pyle", r"duie\s*pyle", None),
    ("Roadrunner", r"road\s*runner", None),
    ("AAA Cooper", r"aaa\s*cooper", None),
    ("Averitt", r"averitt", None),
    ("FedEx Freight", r"fedex", None),
    ("Southeastern", r"southeastern|\bsefl\b", None),
    ("Pitt Ohio", r"pitt\s*ohio", None),
    ("Dayton", r"\bdayton\b", None),
]


def canonical(text):
    """(clean name, vendor company id) for an accuFRATE carrier/tariff text; (text, None) if unknown."""
    t = text or ""
    for name, pat, cid in CARRIERS:
        if re.search(pat, t, re.I):
            return name, cid
    return (t.split(" - ")[0].strip()[:40] or "Other"), None


def di_by_carrier(hs):
    """{clean name: DI % (float) or None if not set} for every mapped carrier. hs = freight_queue.hs."""
    ids = [cid for _, _, cid in CARRIERS if cid]
    st, r = hs("POST", "/crm/v3/objects/companies/batch/read", {"properties": ["di_percent"], "inputs": [{"id": i} for i in ids]})
    pct = {}
    for c in r.get("results", []) if st in (200, 207) else []:
        v = (c.get("properties") or {}).get("di_percent")
        pct[c["id"]] = float(v) if v not in (None, "") else None
    return {name: (pct.get(cid) if cid else None) for name, _, cid in CARRIERS}
