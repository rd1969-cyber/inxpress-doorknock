// Welcome-letter lookup: find a company by XMS customer number, domain or name and return
// everything the letter needs (company, contacts, owner). Password-gated like /api/hs.
// The HubSpot token never reaches the browser.
//
// POST { password, q }          -> { matches: [{ id, name, domain, customer_number, lifecyclestage }] }
// POST { password, companyId }  -> { company, contacts, owner }

const HS = 'https://api.hubapi.com';

const COMPANY_PROPS = ['name', 'domain', 'customer_number', 'franchise_number', 'lifecyclestage', 'status',
  'xms_creation', 'xms_account_creation_date', 'hubspot_owner_id', 'phone', 'address', 'city', 'state', 'zip',
  // Carrier account / login fields. They are read if they exist in HubSpot; missing ones come back empty.
  'welcome_dhl_import_acct', 'welcome_dhl_export_acct', 'welcome_ups_acct', 'welcome_webship_username'];
const CONTACT_PROPS = ['firstname', 'lastname', 'email', 'jobtitle', 'phone'];

async function hs(method, path, body) {
  const r = await fetch(HS + path, {
    method,
    headers: { Authorization: `Bearer ${process.env.HUBSPOT_TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let json = {};
  try { json = text ? JSON.parse(text) : {}; } catch (e) { json = { raw: text }; }
  if (!r.ok) { const err = new Error(json.message || `HubSpot ${r.status}`); err.status = r.status; throw err; }
  return json;
}

// HubSpot rejects a whole read if one requested property doesn't exist, so ask for the
// known-good set first and the optional welcome_* fields separately.
async function readCompany(id) {
  const base = COMPANY_PROPS.filter((p) => !p.startsWith('welcome_'));
  const c = await hs('GET', `/crm/v3/objects/companies/${id}?properties=${base.join(',')}`);
  try {
    const extra = await hs('GET', `/crm/v3/objects/companies/${id}?properties=${COMPANY_PROPS.filter((p) => p.startsWith('welcome_')).join(',')}`);
    Object.assign(c.properties, extra.properties || {});
  } catch (e) { /* optional fields not created yet */ }
  return { id: c.id, properties: c.properties };
}

function classify(q) {
  const s = String(q || '').trim();
  const digits = s.replace(/\D/g, '');
  if (/^\d[\d\s-]*$/.test(s) && digits.length >= 5) return { kind: 'number', value: digits };
  const dom = s.toLowerCase().replace(/^https?:\/\//, '').replace(/^www\./, '').replace(/\/.*$/, '');
  if (s.includes('@')) return { kind: 'domain', value: s.split('@').pop().toLowerCase() };
  if (/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(dom)) return { kind: 'domain', value: dom };
  return { kind: 'name', value: s };
}

async function search(q) {
  const { kind, value } = classify(q);
  const props = ['name', 'domain', 'customer_number', 'lifecyclestage', 'city'];
  let body;
  if (kind === 'number') body = { filterGroups: [{ filters: [{ propertyName: 'customer_number', operator: 'EQ', value }] }] };
  else if (kind === 'domain') body = { filterGroups: [{ filters: [{ propertyName: 'domain', operator: 'EQ', value }] }] };
  else body = { query: value };
  const out = await hs('POST', '/crm/v3/objects/companies/search', { ...body, properties: props, limit: 10 });
  return { kind, matches: (out.results || []).map((o) => ({ id: o.id, ...o.properties })) };
}

async function contactsFor(companyId) {
  const a = await hs('GET', `/crm/v4/objects/companies/${companyId}/associations/contacts?limit=50`);
  const ids = (a.results || []).map((x) => x.toObjectId).filter(Boolean);
  if (!ids.length) return [];
  const out = await hs('POST', '/crm/v3/objects/contacts/batch/read', { properties: CONTACT_PROPS, inputs: ids.map((id) => ({ id: String(id) })) });
  return (out.results || []).map((o) => ({ id: o.id, ...o.properties }));
}

async function owner(id) {
  if (!id) return null;
  try {
    const o = await hs('GET', `/crm/v3/owners/${id}`);
    return { id: String(o.id), firstName: o.firstName, lastName: o.lastName, email: o.email };
  } catch (e) { return { id: String(id) }; } // owners scope missing: page falls back to its roster
}

module.exports = async (req, res) => {
  if (req.method !== 'POST') { res.status(405).json({ error: 'POST only' }); return; }
  if (!process.env.HUBSPOT_TOKEN) { res.status(500).json({ error: 'HUBSPOT_TOKEN not configured' }); return; }
  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch (e) { body = {}; } }
  body = body || {};
  if (!process.env.APP_PASSWORD || body.password !== process.env.APP_PASSWORD) { res.status(401).json({ error: 'unauthorized' }); return; }

  try {
    if (body.companyId) {
      const company = await readCompany(String(body.companyId).replace(/\D/g, ''));
      const [contacts, own] = await Promise.all([contactsFor(company.id), owner(company.properties.hubspot_owner_id)]);
      res.status(200).json({ company, contacts, owner: own });
      return;
    }
    if (body.q) { res.status(200).json(await search(body.q)); return; }
    res.status(400).json({ error: 'Send q or companyId' });
  } catch (e) {
    res.status(200).json({ error: e.message });
  }
};
