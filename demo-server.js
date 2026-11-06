// Demo server for the TEST ENVIRONMENT (preview panel).
// Serves the real public/ pages but fakes /api/hs with sample data —
// so you can click through everything with NO connection to real HubSpot.
// Login: type ANY password (the mock accepts anything).
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, 'public');
const PORT = 5599;
const MIME = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.png':'image/png',
  '.svg':'image/svg+xml', '.webmanifest':'application/manifest+json', '.json':'application/json', '.ico':'image/x-icon',
  '.jpg':'image/jpeg', '.woff2':'font/woff2' };

// ---- sample data ----
const COMPANIES = [
  { id:'9001', properties:{ name:'Demo Freight Co', phone:'902-555-0101', city:'Halifax', state:'NS',
    website:'demofreight.ca', address:'1 Demo Street', zip:'B3J1A1', industry:'Manufacturing', of_locations:'2',
    lifecyclestage:'customer', current_carrier__prospect_:'UPS', current_carrier__freight__canada:'Day and Ross',
    ups_volume:'40', current_freight_volume:'5', projected_weekly_margin:'350',
    any_other_information_about_the_shipment:'(DEMO) Existing customer — prior notes appear here.' } },
  { id:'9002', properties:{ name:'Acme Widgets Ltd', phone:'416-555-0199', city:'Toronto', state:'ON', lifecyclestage:'lead' } },
  { id:'9003', properties:{ name:'Maritime Movers', phone:'506-555-0143', city:'Moncton', state:'NB', lifecyclestage:'salesqualifiedlead' } }
];

function mockHS(body){
  const tool = body.tool, a = body.args || {};
  if(tool==='search_crm_objects'){
    if(a.objectType==='companies'){
      const q=(a.query||'').toLowerCase();
      const hit=COMPANIES.filter(c=>c.properties.name.toLowerCase().includes(q));
      return { results: hit.length?hit:COMPANIES, total:(hit.length||COMPANIES.length) };
    }
    if(a.objectType==='contacts'){
      return { results:[{ id:'7001', properties:{ firstname:'Dana', lastname:'Buyer', jobtitle:'Operations Manager',
        email:'dana@demofreight.ca', phone:'+19025550102' } }], total:1 };
    }
    if(a.objectType==='deals'){
      // dedup lookup: company 9001 has an OPEN deal in the sales pipeline → demonstrates "updated existing deal"
      const assoc=a.filterGroups&&a.filterGroups[0]&&a.filterGroups[0].associatedWith&&a.filterGroups[0].associatedWith[0];
      const cid=assoc&&assoc.objectIdValues&&assoc.objectIdValues[0];
      if(String(cid)==='9001') return { results:[{ id:'6001', properties:{ dealname:'Demo Freight Co — InXpress',
        dealstage:'1334197903', pipeline:'145577884', hs_is_closed:'false' } }], total:1 };
      return { results:[], total:0 };
    }
    return { results:[], total:0 };
  }
  if(tool==='get_crm_objects'){
    const id=(a.objectIds||[])[0];
    const c=COMPANIES.find(x=>String(x.id)===String(id))||COMPANIES[0];
    return { objects:[{ id:c.id, properties:c.properties }] };
  }
  if(tool==='manage_crm_objects'){
    if(a.createRequest) return { createResults:{ results:[{ objectId:'99999', object:{ id:'99999' } }], summary:{ created:1, failed:0 } } };
    if(a.updateRequest){ const id=a.updateRequest.objects[0].objectId||'99999'; return { updateResults:{ results:[{ id }], summary:{ updated:1, failed:0 } }, results:[{ id }] }; }
  }
  return { results:[] };
}

// Recorder: when a page is opened with ?rec=<scene>, put it into a clean captured-state
// (used only for building the walkthrough GIF; no effect on normal use).
const RECORDER = `
<script>
(function(){
  var step=new URLSearchParams(location.search).get('rec'); if(!step) return;
  try{ localStorage.setItem('ix_pw','demo'); localStorage.setItem('ix_rep','1640189485'); }catch(e){}
  function $(id){ return document.getElementById(id); }
  function setv(id,v){ var e=$(id); if(e) e.value=v; }
  window.addEventListener('load', function(){ setTimeout(function(){
    var lg=$('login'); if(lg) lg.classList.add('hidden');
    if(step==='knock'){
      setv('fCompany','Demo Freight Co'); setv('fSpokeFirst','Sam'); setv('fSpokeLast','Greeter'); setv('fSpokeTitle','Front desk'); setv('fSpokePhone','9025550142');
      setv('fDMFirst','Dana'); setv('fDMLast','Buyer'); setv('fDMEmail','dana@demofreight.ca');
      setv('fParcelVol','40'); setv('fFreightVol','5'); setv('fWeeklyMargin','350');
      var pcb=document.querySelectorAll('#mselParcelMenu input'); if(pcb[0])pcb[0].checked=true; if(pcb[1])pcb[1].checked=true; if(typeof updateParcelField==='function')updateParcelField(); var pmn=document.getElementById('mselParcelMenu'); if(pmn)pmn.classList.remove('hidden');
      if(typeof updateQual==='function'){ try{ updateQual(); }catch(e){} }
      var m=$('saveMsg'); if(m){ m.className='save-msg ok'; m.innerHTML='&#9989; Demo Freight Co &#8594; <b>Sales Qualified Lead</b>'; }
      var nb=$('nextBtn'); if(nb){ nb.classList.remove('hidden'); nb.disabled=false; }
    } else if(step==='dictate'){
      var dt=$('fDictate'); if(dt){ dt.value='Talked to Sam the receptionist at Demo Freight, they ship about 40 parcels a week with UPS and FedEx, decision maker is Dana Buyer, dana@demofreight.com, margin around 350'; }
      if(typeof fillFromDictation==='function'){ try{ fillFromDictation(); }catch(e){} }
    } else if(step==='qualify'){
      var sc=$('searchCard'); if(sc) sc.classList.add('hidden');
      var bg=$('badge'); if(bg) bg.classList.remove('hidden');
      var fa=$('formArea'); if(fa) fa.classList.remove('hidden');
      var cn=$('cfgNote'); if(cn) cn.style.display='none';
      var bn=$('badgeName'); if(bn) bn.innerHTML='Demo Freight Co <span class="tag tag-exist">Existing</span>';
      var bm=$('badgeMeta'); if(bm) bm.innerHTML='Halifax, NS &#183; HubSpot ID: 9001 &#183; <span style="color:#2E8B6A;font-weight:bold;">&#10003; saved data loaded</span>';
      setv('fCarrierParcel','UPS'); setv('fCarrierFreight','Day and Ross'); setv('fParcelVol','40'); setv('fFreightVol','5'); setv('fWeeklyMargin','350');
      setv('fDMFirst','Dana'); setv('fDMLast','Buyer'); setv('fDMTitle','Operations Manager'); setv('fDMEmail','dana@demofreight.ca');
      if(typeof updateQual==='function'){ try{ updateQual(); }catch(e){} }
    } else if(step==='customer'){
      var sc2=$('searchCard'); if(sc2) sc2.classList.add('hidden');
      var ed=$('editor'); if(ed) ed.classList.remove('hidden');
      var bn2=$('bName'); if(bn2) bn2.textContent='Demo Freight Co';
      var bm2=$('bMeta'); if(bm2) bm2.innerHTML='HubSpot ID: 9001 &#183; <span class="chip chip-cust">&#11088; Customer</span>';
      setv('cPhone','902-555-0101'); setv('cWebsite','demofreight.ca'); setv('cAddress','1 Demo Street'); setv('cCity','Halifax'); setv('cState','NS'); setv('cZip','B3J1A1');
      setv('cIndustry','Manufacturing'); setv('cLocations','2'); setv('cCarrierParcel','UPS'); setv('cCarrierFreight','Day and Ross');
      setv('cParcelVol','40'); setv('cFreightVol','5'); setv('cMargin','425');
      setv('dmFirst','Dana'); setv('dmLast','Buyer'); setv('dmTitle','Operations Manager'); setv('dmPhone','902-555-0102'); setv('dmEmail','dana@demofreight.ca');
      var sm=$('saveMsg'); if(sm){ sm.className='save-msg ok'; sm.innerHTML='&#9989; Saved &#8212; <b>Demo Freight Co</b>. Status &amp; lifecycle unchanged.'; }
    }
  }, 150); });
})();
</script>
`;

http.createServer((req,res)=>{
  if(req.method==='POST' && req.url.startsWith('/api/parse')){
    let data=''; req.on('data',c=>data+=c); req.on('end',()=>{
      res.writeHead(200,{'Content-Type':'application/json'});
      res.end(JSON.stringify({ data:{
        company:'Demo Freight Co', spoke_first:'Sam', spoke_last:'', spoke_title:'Receptionist',
        spoke_phone:'', spoke_email:'', dm_first:'Dana', dm_last:'Buyer', dm_phone:'', dm_email:'dana@demofreight.com',
        parcel_carriers:['UPS','FedEx'], ltl_carriers:[], shipping_types:['Small Parcel'],
        parcel_volume_per_week:'40', freight_volume_per_week:'', weekly_margin:'350',
        notes:'(DEMO) parsed from your dictation — nothing sent to Claude in demo mode.'
      }}));
    });
    return;
  }
  if(req.method==='POST' && req.url.startsWith('/api/welcome')){
    let data=''; req.on('data',c=>data+=c); req.on('end',()=>{
      let b={}; try{ b=JSON.parse(data); }catch(e){}
      const co={ id:'9101', properties:{ name:'Sedna Technologies (DEMO)', domain:'sednatech.io', customer_number:'14030176',
        franchise_number:'140', lifecyclestage:'1326068382', hubspot_owner_id:'1640189485' } };
      res.writeHead(200,{'Content-Type':'application/json'});
      if(b.companyId) res.end(JSON.stringify({ company:co, owner:{ id:'1640189485' }, contacts:[
        { id:'1', firstname:'Jordan', lastname:'Lee', email:'jordan@sednatech.io' }, { id:'2', firstname:'Pat', lastname:'Kim', email:'pat@sednatech.io' } ] }));
      else res.end(JSON.stringify({ kind:'number', matches:[{ id:'9101', ...co.properties }] }));
    });
    return;
  }
  if(req.method==='POST' && req.url.startsWith('/api/hs')){
    let data=''; req.on('data',c=>data+=c); req.on('end',()=>{
      let body={}; try{ body=JSON.parse(data); }catch(e){}
      res.writeHead(200,{'Content-Type':'application/json'});
      res.end(JSON.stringify(mockHS(body)));
    });
    return;
  }
  let p=decodeURIComponent((req.url||'/').split('?')[0]);
  if(p==='/') p='/index.html';
  let file=path.join(ROOT,p);
  if(!path.extname(file) && fs.existsSync(file+'.html')) file=file+'.html';   // clean URLs: /knock → knock.html
  fs.readFile(file,(err,buf)=>{
    if(err){ res.writeHead(404); res.end('Not found'); return; }
    if(path.extname(file)==='.html'){
      let html=buf.toString('utf8').replace('</head>', RECORDER+'</head>');
      res.writeHead(200,{'Content-Type':'text/html'});
      res.end(html); return;
    }
    res.writeHead(200,{'Content-Type':MIME[path.extname(file)]||'application/octet-stream'});
    res.end(buf);
  });
}).listen(PORT,()=>console.log('DEMO (mock HubSpot, no real data) → http://localhost:'+PORT));
