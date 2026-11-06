"""Minimal accuFRATE SOAP client: LTL rate quote -> carrier cost table.

Follows "accufRATE Rate Quote API 2.6" (api-docs folder): FreightService.svc / GetQuotes.
Credentials: the API-specific username/password issued by accuFRATE (NOT the portal login;
the API allows max 20 / 14 characters), from env vars ACCUFRATE_USER / ACCUFRATE_PASS (never printed).
Mode: T = testing (default here), C = customer (live), S = sub-customer.
Cost = BaseCharge ("before markup & commissions"); NetCharge = after markup.

Usage (test):  python accufrate_quote.py          # Mode T
               python accufrate_quote.py C        # live
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from xml.sax.saxutils import escape

import urllib.error
import urllib.request

ENDPOINT = "https://webservice.accufrate.com/FreightService.svc"
ACTION = "http://tempuri.org/IFreightService/GetQuotes"
NS_DC = "http://schemas.datacontract.org/2004/07/accufRATE.accufRATE"
NS_ARR = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
ITEM_PALLETS = 11  # Appendix C item type code


def _el(tag, val):
    return "" if val is None else f"<a:{tag}>{escape(str(val))}</a:{tag}>"


def build_envelope(req, mode="T"):
    # Only the documented fields, in the data-contract (alphabetical) order.
    acc = "".join(f"<b:string>{escape(str(a))}</b:string>" for a in req.get("accessorials", []))
    items = "".join(
        "<a:FreightItem>"
        + _el("ClassCode", it["class"])
        + _el("Description", it.get("description", "Freight"))
        + _el("ItemTypeCode", it.get("item_type", ITEM_PALLETS))
        + _el("NMFCCode", it.get("nmfc", ""))
        + _el("Weight", int(round(it["weight"])))
        + "</a:FreightItem>"
        for it in req["items"]
    )
    dims = "".join(
        "<a:Dimension>"
        + _el("Height", it["height"]) + _el("Length", it["length"])
        + _el("Pieces", it.get("quantity", 1)) + _el("Width", it["width"])
        + "</a:Dimension>"
        for it in req["items"]
    ) if all(k in it for it in req["items"] for k in ("length", "width", "height")) else ""
    auth = (
        "<a:Authentication>"
        + _el("Mode", mode)
        + _el("Password", os.environ["ACCUFRATE_PASS"])
        + _el("UserName", os.environ["ACCUFRATE_USER"])
        + "</a:Authentication>"
    )
    body = (
        f"<a:Accessorial>{acc}</a:Accessorial>"
        + auth
        + _el("ClientReferenceId", req.get("client_reference_id"))  # sub-customer's account (parent quoting for them)
        + f"<a:FreightItems>{items}</a:FreightItems>"
        + _el("FromCity", req["from_city"])
        + _el("FromCountry", req.get("from_country", "USA"))
        + _el("FromState", req["from_state"])
        + _el("FromZip", req["from_zip"])
        + (f"<a:PackageDimensions>{dims}</a:PackageDimensions>" if dims else "")
        + _el("Pallets", req.get("pallets", len(req["items"])))
        + _el("PickupDate", req["pickup_date"] + "T00:00:00.000")
        + _el("QuoteTypeId", req.get("quote_type", 1))
        + _el("ToCity", req["to_city"])
        + _el("ToCountry", req.get("to_country", "USA"))
        + _el("ToState", req["to_state"])
        + _el("ToZip", req["to_zip"])
    )
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<GetQuotes xmlns="http://tempuri.org/"><request xmlns:a="{NS_DC}" xmlns:b="{NS_ARR}">'
        f"{body}</request></GetQuotes></s:Body></s:Envelope>"
    )


def get_quotes(req, mode="T"):
    env = build_envelope(req, mode)
    r = urllib.request.Request(ENDPOINT, data=env.encode("utf-8"), method="POST",
                               headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{ACTION}"'})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # SOAP faults come back as HTTP 500 with an XML body
        return e.code, e.read().decode("utf-8", "replace")


def parse(xml_text):
    root = ET.fromstring(xml_text)
    ns = {"a": NS_DC}
    res = root.find(".//{http://tempuri.org/}GetQuotesResult")
    if res is None:
        fault = root.find(".//faultstring")
        return {"error": fault.text if fault is not None else "no result"}
    g = lambda e, t: (e.findtext(f"a:{t}", namespaces=ns) or "").strip()
    out = {"valid": g(res, "IsValid"), "error": g(res, "ErrorMessage"), "quote_id": g(res, "QuoteID"), "quotes": []}
    for q in res.findall("a:Quotes/a:Quote", ns):
        row = {k: g(q, k) for k in ("QuoteDetailID", "CarrierName", "CarrierSCAC", "TariffDesc", "BaseCharge",
                                    "NetCharge", "FuelCharge", "TransitTime", "QuoteNumber", "ErrorMessage")}
        row["cost"] = row.pop("BaseCharge")  # before markup & commissions
        out["quotes"].append(row)
    out["quotes"].sort(key=lambda r: float(r["cost"] or 1e12))
    return out


if __name__ == "__main__":
    nxt = date.today() + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    sample = {
        "from_zip": "30336", "from_city": "ATLANTA", "from_state": "GA",
        "to_zip": "75247", "to_city": "DALLAS", "to_state": "TX",
        "pickup_date": nxt.isoformat(),
        "items": [{"class": "100", "weight": 500, "length": 48, "width": 40, "height": 48, "quantity": 1}],
    }
    mode = sys.argv[1] if len(sys.argv) > 1 else "T"
    status, text = get_quotes(sample, mode)
    print("HTTP", status, "| Mode", mode)
    try:
        print(json.dumps(parse(text), indent=1)[:6000])
    except ET.ParseError:
        print(text[:1500])
