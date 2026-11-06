// Vercel serverless function — proxies the door-knock app to HubSpot.
// The HubSpot private-app token NEVER reaches the browser; it lives only here as an env var.
//
// Required environment variables (set in Vercel → Project → Settings → Environment Variables):
//   HUBSPOT_TOKEN  = your HubSpot private-app token (pat-na1-...)
//   APP_PASSWORD   = the shared team password reps type once per device
//
// The frontend speaks the same "tool" shape it used inside Cowork, so this returns the
// same response shapes (search → {results}, get → {objects}, create → {createResults}).

const HS = 'https://api.hubapi.com';

async function hs(method, path, body) {
  const r = await fetch(HS + path, {
    method,
    headers: { Authorization: `Bearer ${process.env.HUBSPOT_TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  });
  const text = await r.text();
  let json = {};
  try { json = text ? JSON.parse(text) : {}; } catch (e) { json = { raw: text }; }
  if (!r.ok) throw new Error(json.message || `HubSpot ${r.status}`);
  return json;
}

// PUT a default association (v4) — avoids hard-coding association type ids.
// Best-effort: a failed link must NOT fail the whole save (the record already exists,
// and failing here would make a rep retry and create a duplicate).
async function associate(fromType, fromId, assocs) {
  for (const a of (assocs || [])) {
    try {
      await hs('PUT', `/crm/v4/objects/${fromType}/${fromId}/associations/default/${a.targetObjectType}/${a.targetObjectId}`);
    } catch (e) { /* leave unlinked rather than fail the save */ }
  }
}

async function manage(args) {
  // CREATE
  if (args.createRequest && args.createRequest.objects && args.createRequest.objects[0]) {
    const o = args.createRequest.objects[0];
    const created = await hs('POST', `/crm/v3/objects/${o.objectType}`, { properties: o.properties || {} });
    await associate(o.objectType, created.id, o.associations);
    return { createResults: { results: [{ objectType: o.objectType, objectId: created.id, object: { id: created.id } }], summary: { created: 1, failed: 0 } } };
  }
  // UPDATE
  if (args.updateRequest && args.updateRequest.objects && args.updateRequest.objects[0]) {
    const o = args.updateRequest.objects[0];
    if (o.properties && Object.keys(o.properties).length) {
      await hs('PATCH', `/crm/v3/objects/${o.objectType}/${o.objectId}`, { properties: o.properties });
    }
    await associate(o.objectType, o.objectId, o.associations);
    return { updateResults: { results: [{ id: o.objectId }], summary: { updated: 1, failed: 0 } }, results: [{ id: o.objectId }] };
  }
  throw new Error('Nothing to create or update');
}

module.exports = async (req, res) => {
  if (req.method !== 'POST') { res.status(405).json({ error: 'POST only' }); return; }
  if (!process.env.HUBSPOT_TOKEN) { res.status(500).json({ error: 'HUBSPOT_TOKEN not configured' }); return; }

  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch (e) { body = {}; } }
  body = body || {};
  const { tool, args = {}, password } = body;

  if (!process.env.APP_PASSWORD || password !== process.env.APP_PASSWORD) {
    res.status(401).json({ error: 'unauthorized' }); return;
  }

  try {
    if (tool === 'search_crm_objects') {
      // association-scoped lookup (e.g. "contacts already on this company") — used for de-dupe
      const assoc = args.filterGroups && args.filterGroups[0] && args.filterGroups[0].associatedWith && args.filterGroups[0].associatedWith[0];
      if (assoc && (assoc.objectIdValues || []).length) {
        const a = await hs('GET', `/crm/v4/objects/${assoc.objectType}/${assoc.objectIdValues[0]}/associations/${args.objectType}?limit=100`);
        const toIds = (a.results || []).map(x => x.toObjectId).filter(Boolean);
        let results = [];
        if (toIds.length) {
          const out = await hs('POST', `/crm/v3/objects/${args.objectType}/batch/read`, {
            properties: args.properties || [], inputs: toIds.map(id => ({ id: String(id) }))
          });
          results = (out.results || []).map(o => ({ id: o.id, properties: o.properties }));
        }
        res.status(200).json({ results, total: results.length });
        return;
      }
      // plain text search
      const out = await hs('POST', `/crm/v3/objects/${args.objectType}/search`, {
        query: args.query || undefined,
        properties: args.properties || [],
        limit: args.limit || 10
      });
      res.status(200).json({ results: (out.results || []).map(o => ({ id: o.id, properties: o.properties })), total: out.total });
      return;
    }
    if (tool === 'get_crm_objects') {
      const out = await hs('POST', `/crm/v3/objects/${args.objectType}/batch/read`, {
        properties: args.properties || [],
        inputs: (args.objectIds || []).map(id => ({ id: String(id) }))
      });
      res.status(200).json({ objects: (out.results || []).map(o => ({ id: o.id, properties: o.properties })) });
      return;
    }
    if (tool === 'manage_crm_objects') {
      res.status(200).json(await manage(args));
      return;
    }
    res.status(400).json({ error: 'unknown tool: ' + tool });
  } catch (e) {
    // 200 with an error payload the frontend's firstError() understands
    res.status(200).json({ createResults: { results: [{ error: e.message }], summary: { created: 0, failed: 1 } }, error: e.message });
  }
};
