# InXpress Door Knock — phone app

A mobile web app (PWA) for reps to capture door-knock prospects straight into HubSpot.
Reps open a URL, add it to their home screen, and it behaves like a native app.

## What's in here
- `public/index.html` — the app (same form you tested).
- `public/manifest.webmanifest`, `public/sw.js`, `public/icon-*.png` — make it installable.
- `api/hs.js` — serverless function that talks to HubSpot. Your token stays here, never in the browser.

## One-time setup

### 1. Create a HubSpot private-app token
HubSpot → **Settings → Integrations → Private Apps → Create a private app**.
On the **Scopes** tab add:
- `crm.objects.companies.read`, `crm.objects.companies.write`
- `crm.objects.contacts.read`, `crm.objects.contacts.write`
- `crm.objects.deals.read`, `crm.objects.deals.write`
- `crm.objects.owners.read`

Create it and copy the **access token** (starts with `pat-na1-...`).

### 2. Deploy to Vercel (free, ~5 minutes)
Easiest path — no command line:
1. Put this folder in a GitHub repo (or drag-and-drop deploy at vercel.com/new).
2. At **vercel.com/new**, import the repo. Framework preset: **Other**. Click Deploy.
3. In the project → **Settings → Environment Variables**, add:
   - `HUBSPOT_TOKEN` = your `pat-na1-...` token
   - `APP_PASSWORD` = a shared team password (reps type this once per phone)
4. **Redeploy** (Deployments → ⋯ → Redeploy) so the variables take effect.

CLI alternative: `npm i -g vercel`, then run `vercel` in this folder and `vercel env add HUBSPOT_TOKEN` / `vercel env add APP_PASSWORD`.

### 3. Put it on reps' phones
- Send reps the Vercel URL (e.g. `https://inxpress-doorknock.vercel.app`).
- They enter the team password **once**, pick their name **once**.
- **iPhone:** Share → *Add to Home Screen*. **Android:** Chrome menu → *Install app / Add to Home screen*.
- It now opens full-screen like an app. Each knock: fill, pick a disposition, tap **Next knock**. No re-login.

## Notes
- The Zoom/meeting link lives in `public/index.html` → `CFG.MEETING_LINK`. Update it before go-live.
- Rep roster, pipeline, and field mappings are all in `CFG` at the top of `public/index.html`.
- The same file also runs inside Claude/Cowork unchanged (it auto-detects and uses the Cowork bridge there instead of this backend).
