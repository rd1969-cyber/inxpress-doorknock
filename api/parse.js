// Vercel serverless function — turns a rep's spoken/typed door-knock notes into
// structured fields using Claude. The API key NEVER reaches the browser.
//
// Required environment variables (Vercel → Project → Settings → Environment Variables):
//   ANTHROPIC_API_KEY = your Anthropic API key (sk-ant-...)
//   APP_PASSWORD      = the same shared team password the app already uses
//
// Frontend posts { transcript, password } and gets back { data: {...fields} }.

const Anthropic = require('@anthropic-ai/sdk');

// Allowed values so Claude maps messy speech ("ups and fedex") onto real options.
const PARCEL_CARRIERS = ['UPS', 'FedEx', 'DHL', 'Purolator', 'Canpar', 'FreightCom', 'Third Party Portal', 'Other'];
// Options of HubSpot company property current_carrier__freight__canada (keep in sync with knock.html CFG.LTL_CARRIERS)
const LTL_CARRIERS = ['Day and Ross', 'Accufrate', 'DHL Global Forwarding', 'JP Cargo', 'CH Robinson', 'QuickX', 'APEX',
  'WCE', 'Maritime Ontario', 'Overland', 'Manitoulin', 'Comox', 'MaltaCourt', 'Kindersley', 'Laneway Logistics',
  'One Reimer', 'Bellore', 'TransForce', 'Midland', 'Armour', 'Other'];
const SHIPPING_TYPES = ['Small Parcel', 'Freight (LTL)', 'Ocean', 'Air Cargo'];

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    company: { type: 'string', description: 'Business / company name' },
    spoke_first: { type: 'string', description: 'First name of the person the rep spoke to' },
    spoke_last: { type: 'string' },
    spoke_title: { type: 'string', description: 'Their role, e.g. receptionist, office manager' },
    spoke_phone: { type: 'string', description: 'Their phone, digits only if given' },
    spoke_email: { type: 'string', description: 'Their email, lowercase, only if clearly stated' },
    dm_first: { type: 'string', description: 'Decision maker first name (only if different from the person spoken to)' },
    dm_last: { type: 'string' },
    dm_phone: { type: 'string' },
    dm_email: { type: 'string' },
    parcel_carriers: { type: 'array', items: { type: 'string', enum: PARCEL_CARRIERS }, description: 'Parcel carriers they currently use' },
    ltl_carriers: { type: 'array', items: { type: 'string', enum: LTL_CARRIERS }, description: 'LTL / freight carriers they currently use (empty array if none mentioned)' },
    shipping_types: { type: 'array', items: { type: 'string', enum: SHIPPING_TYPES }, description: 'What kinds of shipping they do' },
    parcel_volume_per_week: { type: 'string', description: 'Parcels/week, number only if stated' },
    freight_volume_per_week: { type: 'string', description: 'Freight shipments/week, number only if stated' },
    weekly_margin: { type: 'string', description: 'Projected weekly margin in dollars, number only if stated' },
    notes: { type: 'string', description: 'Anything else worth remembering: pain points, timing, context — but NOT data already captured in the other fields' }
  },
  required: [
    'company', 'spoke_first', 'spoke_last', 'spoke_title', 'spoke_phone', 'spoke_email',
    'dm_first', 'dm_last', 'dm_phone', 'dm_email', 'parcel_carriers', 'ltl_carriers',
    'shipping_types', 'parcel_volume_per_week', 'freight_volume_per_week', 'weekly_margin', 'notes'
  ]
};

const SYSTEM = [
  'You extract structured door-knock notes for an InXpress sales rep from their spoken or typed description of a visit.',
  'Return one field per property. Use an empty string "" (or empty array) for anything the rep did not mention — never guess or invent details.',
  'Map carrier names onto the allowed options (e.g. "ups" -> "UPS", "fedex" -> "FedEx", "day and ross" or "day & ross" -> "Day and Ross"). If a parcel or LTL carrier is mentioned but does not match its list, use "Other".',
  'Phone numbers: digits only. Emails: lowercase. Volumes and margin: numbers only, no words or symbols.',
  'The "person spoken to" and the "decision maker" may be the same person or different people — only fill decision-maker fields if a distinct decision maker is named.',
  'Put genuinely extra context (pain points, timing, who to ask for) in notes; do not repeat data already placed in other fields.'
].join(' ');

module.exports = async (req, res) => {
  if (req.method !== 'POST') { res.status(405).json({ error: 'POST only' }); return; }

  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch (e) { body = {}; } }
  body = body || {};
  const { transcript, password } = body;

  if (!process.env.APP_PASSWORD || password !== process.env.APP_PASSWORD) {
    res.status(401).json({ error: 'unauthorized' }); return;
  }
  if (!process.env.ANTHROPIC_API_KEY) { res.status(500).json({ error: 'ANTHROPIC_API_KEY not configured' }); return; }
  if (!transcript || !String(transcript).trim()) { res.status(400).json({ error: 'no transcript' }); return; }

  try {
    const client = new Anthropic(); // reads ANTHROPIC_API_KEY from env
    const msg = await client.messages.create({
      model: 'claude-haiku-4-5',   // cheapest + fastest; swap to 'claude-opus-4-8' if you ever want max accuracy
      max_tokens: 1024,
      system: SYSTEM,
      output_config: { format: { type: 'json_schema', schema: SCHEMA } },
      messages: [{ role: 'user', content: String(transcript).slice(0, 6000) }]
    });
    const textBlock = (msg.content || []).find(b => b.type === 'text');
    const data = JSON.parse(textBlock.text);
    res.status(200).json({ data });
  } catch (e) {
    res.status(200).json({ error: (e && e.message) ? e.message : String(e) });
  }
};
