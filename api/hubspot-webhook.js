// HubSpot webhook -> freight auto-pricing.
// The "InXpress Sales - Bob" private app sends deal events here (deal created, Description changed,
// Quote Route cleared). We verify HubSpot's signature, answer 200 right away, then hand each deal
// to the Python worker (api/freight_price.py), which only acts on Freight Pipeline deals at
// Quote Request Received that have not been routed yet.
//
// Env: HUBSPOT_WEBHOOK_SECRET (the private app's client secret), FREIGHT_WORKER_KEY (internal key).

const crypto = require('crypto');
const { waitUntil } = require('@vercel/functions');

const PUBLIC_URL = (process.env.PUBLIC_URL || 'https://inxpress-sales.vercel.app').replace(/\/$/, '');
const WATCHED = new Set(['description', 'freight_quote_route', 'dealstage', 'pipeline']);
const CARRIER_PROPS = new Set(['freight_carrier']);
const BOOK_PROPS = new Set(['freight_shipment_action', 'freight_booking_approval']);
const OPTION_PROPS = new Set(['freight_use_option']);
const MARGIN_PROPS = new Set(['margin_on_freight_shipment']);

function rawBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function safeEq(a, b) {
  const x = Buffer.from(String(a || '')), y = Buffer.from(String(b || ''));
  return x.length === y.length && x.length > 0 && crypto.timingSafeEqual(x, y);
}

function validSignature(req, body, secret) {
  const uri = PUBLIC_URL + req.url;
  const v3 = req.headers['x-hubspot-signature-v3'];
  const ts = req.headers['x-hubspot-request-timestamp'];
  if (v3 && ts && Math.abs(Date.now() - Number(ts)) <= 5 * 60 * 1000) {
    const mac = crypto.createHmac('sha256', secret).update(req.method + uri + body + ts, 'utf8').digest('base64');
    if (safeEq(mac, v3)) return true;
  }
  const sig = req.headers['x-hubspot-signature'];
  const ver = (req.headers['x-hubspot-signature-version'] || 'v1').toLowerCase();
  if (!sig) return false;
  const src = ver === 'v2' ? secret + req.method + uri + body : secret + body;
  return safeEq(crypto.createHash('sha256').update(src, 'utf8').digest('hex'), sig);
}

module.exports = async (req, res) => {
  if (req.method !== 'POST') { res.status(405).json({ error: 'POST only' }); return; }
  const secret = (process.env.HUBSPOT_WEBHOOK_SECRET || '').trim();
  if (!secret || !process.env.FREIGHT_WORKER_KEY) { res.status(500).json({ error: 'not configured' }); return; }
  let body = await rawBody(req);
  if (!body && req.body !== undefined) body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body);
  if (!validSignature(req, body, secret)) {
    console.log('webhook signature failed', JSON.stringify({
      url: req.url, method: req.method, bodyLen: body.length, hasV3: !!req.headers['x-hubspot-signature-v3'],
      hasTs: !!req.headers['x-hubspot-request-timestamp'], hasV1: !!req.headers['x-hubspot-signature'],
      ver: req.headers['x-hubspot-signature-version'] || '', secretLen: secret.length,
      tsAgeMs: Date.now() - Number(req.headers['x-hubspot-request-timestamp'] || 0) }));
    res.status(401).json({ error: 'bad signature' }); return;
  }

  let events = [];
  try { events = JSON.parse(body || '[]'); } catch (e) { res.status(400).json({ error: 'bad json' }); return; }
  const all = Array.isArray(events) ? events : [events];
  const carrierIds = [...new Set(all.filter((e) => e && e.objectId && String(e.subscriptionType || '').endsWith('.propertyChange')
    && CARRIER_PROPS.has(e.propertyName)).map((e) => String(e.objectId)))];
  const marginIds = [...new Set(all.filter((e) => e && e.objectId && String(e.subscriptionType || '') === 'deal.propertyChange'
    && MARGIN_PROPS.has(e.propertyName)).map((e) => String(e.objectId)))];
  const optionIds = [...new Set(all.filter((e) => e && e.objectId && String(e.subscriptionType || '') === 'deal.propertyChange'
    && OPTION_PROPS.has(e.propertyName)).map((e) => String(e.objectId)))];
  const bookIds = [...new Set(all.filter((e) => e && e.objectId && String(e.subscriptionType || '') === 'deal.propertyChange'
    && BOOK_PROPS.has(e.propertyName)).map((e) => String(e.objectId)))];
  const ids = [...new Set(all
    .filter((e) => e && e.objectId && String(e.subscriptionType || '').startsWith('deal.')
      && (String(e.subscriptionType || '').endsWith('.creation')
      || (String(e.subscriptionType || '').endsWith('.propertyChange') && WATCHED.has(e.propertyName))))
    .map((e) => String(e.objectId)))];

  // Conversation events (email intake inbox): one worker call per thread/message.
  const convo = all.filter((e) => e && e.objectId && String(e.subscriptionType || '').startsWith('conversation.')
    && /\.(creation|newMessage)$/.test(String(e.subscriptionType)));
  const seen = new Set();
  const intake = convo.filter((e) => {
    const k = `${e.objectId}:${e.messageId || ''}:${e.subscriptionType}`;
    if (seen.has(k)) return false; seen.add(k); return true;
  });

  // Answer HubSpot immediately (it retries if we take > 5 s); price in the background.
  res.status(200).json({ received: events.length, deals: ids.length, threads: intake.length });
  for (const e of intake) {
    waitUntil(fetch(`${PUBLIC_URL}/api/freight_price`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-freight-key': process.env.FREIGHT_WORKER_KEY },
      body: JSON.stringify({ action: 'intake', thread_id: String(e.objectId), message_id: e.messageId ? String(e.messageId) : null,
        event: String(e.subscriptionType) }),
    }).then((r) => r.text()).then((t) => console.log('intake', e.objectId, t.slice(0, 800)))
      .catch((err) => console.error('intake failed', e.objectId, err.message)));
  }
  for (const id of marginIds) {
    waitUntil(fetch(`${PUBLIC_URL}/api/freight_price`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-freight-key': process.env.FREIGHT_WORKER_KEY },
      body: JSON.stringify({ deal_ids: [id], action: 'margin' }),
    }).then((r) => r.text()).then((t) => console.log('margin', id, t.slice(0, 500)))
      .catch((e) => console.error('margin failed', id, e.message)));
  }
  for (const id of optionIds) {
    waitUntil(fetch(`${PUBLIC_URL}/api/freight_price`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-freight-key': process.env.FREIGHT_WORKER_KEY },
      body: JSON.stringify({ deal_ids: [id], action: 'option' }),
    }).then((r) => r.text()).then((t) => console.log('option', id, t.slice(0, 500)))
      .catch((e) => console.error('option failed', id, e.message)));
  }
  for (const id of bookIds) {
    waitUntil(fetch(`${PUBLIC_URL}/api/freight_price`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-freight-key': process.env.FREIGHT_WORKER_KEY },
      body: JSON.stringify({ deal_ids: [id], action: 'book' }),
    }).then((r) => r.text()).then((t) => console.log('book', id, t.slice(0, 500)))
      .catch((e) => console.error('book failed', id, e.message)));
  }
  for (const id of carrierIds) {
    waitUntil(fetch(`${PUBLIC_URL}/api/freight_price`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-freight-key': process.env.FREIGHT_WORKER_KEY },
      body: JSON.stringify({ deal_ids: [id], action: 'carrier' }),
    }).then((r) => r.text()).then((t) => console.log('carrier', id, t.slice(0, 300)))
      .catch((e) => console.error('carrier failed', id, e.message)));
  }
  for (const id of ids) {
    waitUntil(fetch(`${PUBLIC_URL}/api/freight_price`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-freight-key': process.env.FREIGHT_WORKER_KEY },
      body: JSON.stringify({ deal_ids: [id] }),
    }).then((r) => r.text()).then((t) => console.log('freight_price', id, t.slice(0, 500)))
      .catch((e) => console.error('freight_price failed', id, e.message)));
  }
};
