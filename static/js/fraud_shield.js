/* FraudShield — Phase-1 institutional transaction monitoring UI. */
let lwFraudInitialized = false;
let lwFraudLastSummary = null;
let lwFraudTransactions = [];
let lwFraudSelectedTransaction = null;
let lwFraudBootstrap = null;
let lwFraudLivePoll = null;

function initLwFraudShield() {
    if (typeof lwGetSystemMode === 'function' && lwGetSystemMode() !== 'fraud') return;
    if (lwFraudInitialized) return;
    lwFraudInitialized = true;
    document.getElementById('lwRefreshFraudBtn')?.addEventListener('click', loadLwFraudMonitor);
    document.getElementById('lwRefreshInvestigationsBtn')?.addEventListener('click', loadLwFraudInvestigations);
    document.getElementById('lwNewFraudTransactionBtn')?.addEventListener('click', openLwNewFraudTransaction);
    // Move the modal to <body> so viewport-fixed positioning cannot be clipped
    // by workspace/section overflow or transforms.
    const fraudModal = document.getElementById('lwFraudDetailModal');
    if (fraudModal && fraudModal.parentElement !== document.body) {
        document.body.appendChild(fraudModal);
    }
    loadLwFraudMonitor();
    if (!lwFraudLivePoll) lwFraudLivePoll=setInterval(()=>{ if(typeof lwGetSystemMode==='function' && lwGetSystemMode()==='fraud' && document.visibilityState==='visible') loadLwFraudMonitor(); },20000);
    initFraudAdvancedUI();
    document.getElementById('lwFraudDetailModal')?.addEventListener('click', e => { if (e.target.id === 'lwFraudDetailModal' || e.target.closest('[data-fraud-modal-close]')) closeLwFraudDetail(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLwFraudDetail(); });
}

function lwFraudFormatCurrency(amount) {
    const n = Number(amount);
    return Number.isNaN(n) ? '—' : '₹' + n.toLocaleString('en-IN');
}
function lwFraudBadgeClass(level) { return level === 'HIGH_RISK' ? 'state-bad' : (level === 'REVIEW' || level === 'SUSPICIOUS') ? 'state-warn' : 'state-good'; }
function lwFraudStatusBadgeClass(status) { return ['OPEN','ESCALATED'].includes(status) ? 'state-bad' : status === 'REVIEWED' ? 'state-warn' : 'state-good'; }
function lwFraudPct(v) { return `${Number(v || 0).toFixed(1)}%`; }
function lwFraudTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return String(ts).slice(11, 16) || String(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function lwFraudDateTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}
function lwFraudSignalHtml(t) {
    const s = t.signals || {};
    const chips = [];
    if (s.new_device) chips.push('<span class="lw-fraud-signal lw-fraud-signal-risk">New device</span>');
    else chips.push('<span class="lw-fraud-signal lw-fraud-signal-ok">Trusted device</span>');
    if (Number(s.velocity_1h || 0) >= 3) chips.push(`<span class="lw-fraud-signal lw-fraud-signal-risk">${Number(s.velocity_1h)} txns/1h</span>`);
    if (Number(s.location_distance_km || 0) >= 200) chips.push(`<span class="lw-fraud-signal lw-fraud-signal-risk">${Math.round(Number(s.location_distance_km))} km shift</span>`);
    if (Number(s.merchant_risk || 0) >= .5) chips.push('<span class="lw-fraud-signal lw-fraud-signal-risk">Merchant risk</span>');
    if (s.merchant_seen_before === false) chips.push('<span class="lw-fraud-signal lw-fraud-signal-risk">New merchant</span>');
    if (Number(s.failed_auth_count || 0) >= 2) chips.push(`<span class="lw-fraud-signal lw-fraud-signal-risk">${Number(s.failed_auth_count)} failed auth</span>`);
    if (Number(t.takeover_risk || 0) >= 60) chips.push('<span class="lw-fraud-signal lw-fraud-signal-risk">Account takeover risk</span>');
    if (chips.length === 0) chips.push('<span class="lw-fraud-signal lw-fraud-signal-ok">No major rule flags</span>');
    return chips.slice(0, 3).join('');
}

function lwFraudRenderModelCard(model) {
    const features = (model.features || []).map(f => String(f).replaceAll('_', ' ')).map(f => f.replace(/\b\w/g, x => x.toUpperCase()));
    return `
      <div class="lw-fraud-model-card">
        <div class="lw-fraud-model-main">
          <span class="lw-fraud-model-dot"></span>
          <div>
            <div class="lw-fraud-model-title">${escapeLwHtml(model.model_version || 'FraudShield Model')}</div>
            <div class="lw-fraud-model-sub">${escapeLwHtml(model.dataset || 'Fraud transaction dataset')} · ${escapeLwHtml(String(model.training_records || 0))} training records · Fraud detection engine</div>
          </div>
        </div>
        <div class="lw-fraud-model-metrics">
          <div class="lw-fraud-metric"><span>ROC-AUC</span><strong>${escapeLwHtml(model.roc_auc ?? '—')}</strong></div>
          <div class="lw-fraud-metric"><span>Precision</span><strong>${escapeLwHtml(model.precision ?? '—')}</strong></div>
          <div class="lw-fraud-metric"><span>Recall</span><strong>${escapeLwHtml(model.recall ?? '—')}</strong></div>
          <div class="lw-fraud-metric"><span>F1</span><strong>${escapeLwHtml(model.f1 ?? '—')}</strong></div>
        </div>
      </div>
      <div class="lw-fraud-engine-strip">
        <div><span class="lw-fraud-online-dot"></span>Detection engine online</div>
        ${(model.engine_stack || ['XGBoost','Isolation Forest','Rules','Behavior Signals','SHAP']).map(x => `<span class="lw-fraud-engine-chip">${escapeLwHtml(x)}</span>`).join('')}
      </div>
      <div class="lw-fraud-inputs-row">
        <span class="lw-fraud-inputs-title">MODEL INPUTS</span>
        ${features.slice(0, 9).map(x => `<span class="lw-fraud-input-chip">${escapeLwHtml(x)}</span>`).join('')}
      </div>`;
}

function lwFraudRenderTrend(txns) {
    const recent = [...txns].sort((a,b) => String(a.timestamp||'').localeCompare(String(b.timestamp||''))).slice(-8);
    const maxRisk = Math.max(1, ...recent.map(t => Number(t.risk_score || 0)));
    return recent.length ? recent.map(t => `<div class="lw-fraud-bar-col" title="${escapeLwHtml(t.merchant || '')} · ${t.risk_score}/100">
        <div class="lw-fraud-bar" style="height:${Math.max(8, Math.round((Number(t.risk_score||0)/maxRisk)*96))}px"></div>
        <div class="lw-fraud-bar-value">${t.risk_score}</div><div class="lw-fraud-bar-label">${escapeLwHtml(lwFraudTime(t.timestamp))}</div>
    </div>`).join('') : '<div class="lw-empty-sub">Not enough recent activity to show a trend.</div>';
}


function ensureLwNewFraudTransactionModal() {
    if (document.getElementById('lwNewFraudTransactionModal')) return document.getElementById('lwNewFraudTransactionModal');
    const modal=document.createElement('div');
    modal.id='lwNewFraudTransactionModal'; modal.className='lw-fraud-modal'; modal.setAttribute('aria-hidden','true');
    modal.innerHTML=`<div class="lw-fraud-modal-backdrop" data-new-fraud-close></div><div class="lw-fraud-modal-panel" role="document"><div class="lw-fraud-live-form"><div class="lw-fraud-detail-topbar"><div><div class="lw-panel-title"><i class="fa-solid fa-bolt"></i> New FraudShield Transaction</div><div class="lw-portfolio-panel-note">Inject a live/simulated event into the institution-owned fraud stream.</div></div><button type="button" class="lw-fraud-modal-close" data-new-fraud-close aria-label="Close"><i class="fa-solid fa-xmark"></i></button></div><form id="lwNewFraudTransactionForm" class="lw-fraud-form-grid"><label>Account ID<input name="account_ref" value="AC-LIVE-01" required></label><label>Amount (₹)<input name="amount" type="number" min="1" step="0.01" value="48500" required></label><label>Channel<select name="channel"><option>UPI</option><option>CARD</option><option>IMPS</option><option>NEFT</option></select></label><label>Merchant<input name="merchant" value="Unknown Merchant"></label><label>Location<input name="location" value="Mumbai"></label><label>Device ID<input name="device_id" value="DEV-LIVE-9001"></label><label>New Device<select name="is_new_device"><option value="true">Yes</option><option value="false">No</option></select></label><label>Location Distance (km)<input name="location_distance_km" type="number" min="0" value="1120"></label><label>Merchant Risk (0–1)<input name="merchant_risk" type="number" min="0" max="1" step="0.01" value="0.91"></label><label>Failed Auth Attempts<input name="failed_auth_count" type="number" min="0" value="4"></label><label>Beneficiary Ref<input name="beneficiary_ref" value="BEN-LIVE-01"></label><label>Network Risk (0–1)<input name="network_risk" type="number" min="0" max="1" step="0.01" value="0.90"></label><div class="lw-fraud-form-actions"><button type="button" class="lw-btn" data-fraud-preset="normal">Load Normal</button><button type="button" class="lw-btn" data-fraud-preset="account_takeover">Load Account Takeover</button><button type="submit" class="lw-btn lw-btn-primary"><i class="fa-solid fa-shield-halved"></i> Analyze Transaction</button></div></form><div id="lwNewFraudResult" class="lw-fraud-live-result"></div></div></div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click',e=>{if(e.target.closest('[data-new-fraud-close]')) closeLwNewFraudTransaction();});
    modal.querySelectorAll('[data-fraud-preset]').forEach(b=>b.addEventListener('click',()=>loadFraudPreset(b.dataset.fraudPreset)));
    modal.querySelector('#lwNewFraudTransactionForm').addEventListener('submit', submitLwNewFraudTransaction);
    return modal;
}
function openLwNewFraudTransaction(){ const modal=ensureLwNewFraudTransactionModal(); modal.classList.add('is-open'); modal.setAttribute('aria-hidden','false'); document.body.classList.add('lw-fraud-modal-open'); modal.querySelector('input[name="account_ref"]')?.focus(); }
function closeLwNewFraudTransaction(){ const modal=document.getElementById('lwNewFraudTransactionModal'); if(!modal)return; modal.classList.remove('is-open'); modal.setAttribute('aria-hidden','true'); document.body.classList.remove('lw-fraud-modal-open'); }
function loadFraudPreset(kind){
    const f=document.getElementById('lwNewFraudTransactionForm'); if(!f)return;
    const set=(n,v)=>{const x=f.elements[n];if(x)x.value=v;};
    if(kind==='normal'){set('account_ref','AC-LIVE-NORMAL');set('amount','1200');set('merchant','Amazon');set('location','Bhubaneswar');set('device_id','DEV-1001');set('is_new_device','false');set('location_distance_km','4');set('merchant_risk','0.08');set('failed_auth_count','0');set('network_risk','0.04');}
    else {set('account_ref','AC-LIVE-TAKEOVER');set('amount','48500');set('merchant','Unknown Merchant');set('location','Mumbai');set('device_id','DEV-ATTACK-9001');set('is_new_device','true');set('location_distance_km','1120');set('merchant_risk','0.92');set('failed_auth_count','4');set('network_risk','0.93');}
}
async function submitLwNewFraudTransaction(e){
    e.preventDefault(); const form=e.currentTarget, btn=form.querySelector('button[type="submit"]'), out=document.getElementById('lwNewFraudResult'); const body=Object.fromEntries(new FormData(form).entries());
    ['amount','location_distance_km','merchant_risk','failed_auth_count','network_risk'].forEach(k=>{if(body[k]!==undefined)body[k]=Number(body[k]);}); body.is_new_device=body.is_new_device==='true';
    btn.disabled=true; btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Analyzing...'; out.innerHTML='<div class="lw-fraud-live-pending">Running feature extraction, ML inference, rules and risk fusion…</div>';
    const data=await lwSafeJsonFetch('/lender/fraudshield/intelligence/ingest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); btn.disabled=false; btn.innerHTML='<i class="fa-solid fa-shield-halved"></i> Analyze Transaction';
    if(!data){out.innerHTML='<div class="lw-empty-sub">Transaction could not be analyzed.</div>';return;}
    if(data.duplicate){out.innerHTML=`<div class="lw-fraud-live-warning"><strong>Duplicate / replay blocked</strong><span>${escapeLwHtml(data.message||'This event was already processed.')}</span></div>`;return;}
    const r=data.risk||{}, ap=data.autopilot||{}; out.innerHTML=`<div class="lw-fraud-live-success"><div><span>FINAL FRAUD RISK</span><strong>${r.score}/100</strong><em>${escapeLwHtml(r.level_label||'')}</em></div><div class="lw-fraud-live-components"><span>ML ${(Number(r.model_probability||0)*100).toFixed(1)}%</span><span>Anomaly ${r.anomaly_score||0}</span><span>Rules ${r.rule_score||0}/60</span><span>Autopilot ${escapeLwHtml((ap.action||'ALLOW').replaceAll('_',' '))}</span></div><p>${escapeLwHtml(r.reason||'Transaction analyzed.')}</p><small>Stored as ${escapeLwHtml(data.transaction_ref||'FraudShield event')} · ${ap.case_number?`case ${escapeLwHtml(ap.case_number)} opened automatically · `:''}response policy applied.</small></div>`;
    await loadLwFraudMonitor(); setTimeout(()=>closeLwNewFraudTransaction(),1200);
}

async function loadLwFraudMonitor() {
    const summaryEl = document.getElementById('lwFraudSummary');
    const body = document.getElementById('lwFraudMonitorBody');
    const freshMeta = document.getElementById('lwFraudFreshMeta');
    if (!body) return;
    body.innerHTML = '<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading transaction monitor...</div>';
    const data = await lwSafeJsonFetch('/lender/fraudshield/intelligence/bootstrap');
    if (!data) { body.innerHTML = '<div class="lw-empty-sub">Transaction monitoring is unavailable right now.</div>'; return; }
    lwFraudBootstrap = data;
    lwFraudLastSummary = data.summary || {};
    lwFraudTransactions = data.transactions || [];
    if (freshMeta) freshMeta.textContent = 'Updated ' + new Date().toLocaleTimeString();
    const s = data.summary || {}, model = data.model || {}, dist = s.risk_distribution || {}, txns = lwFraudTransactions;
    if (summaryEl) summaryEl.innerHTML = `${lwFraudRenderModelCard(model)}
      <div class="lw-portfolio-hero">
        <div class="lw-portfolio-kpi"><div class="lw-portfolio-kpi-top"><span>Total Transactions</span><i class="fa-solid fa-list"></i></div><strong>${s.total ?? 0}</strong><small>100.0% monitored</small></div>
        <div class="lw-portfolio-kpi"><div class="lw-portfolio-kpi-top"><span>Normal</span><i class="fa-solid fa-circle-check"></i></div><strong>${s.normal ?? 0}</strong><small>${lwFraudPct(dist.normal_pct)} of activity</small></div>
        <div class="lw-portfolio-kpi"><div class="lw-portfolio-kpi-top"><span>Suspicious</span><i class="fa-solid fa-triangle-exclamation"></i></div><strong>${s.suspicious ?? 0}</strong><small>${lwFraudPct(dist.suspicious_pct)} of activity</small></div>
        <div class="lw-portfolio-kpi"><div class="lw-portfolio-kpi-top"><span>Review</span><i class="fa-solid fa-eye"></i></div><strong>${s.review ?? 0}</strong><small>${lwFraudPct(dist.review_pct)} of activity</small></div>
        <div class="lw-portfolio-kpi lw-portfolio-kpi-alert"><div class="lw-portfolio-kpi-top"><span>High Risk / Investigation</span><i class="fa-solid fa-shield-halved"></i></div><strong>${s.high_risk ?? 0}</strong><small>Priority review</small></div>
        <div class="lw-portfolio-kpi"><div class="lw-portfolio-kpi-top"><span>Open Investigations</span><i class="fa-solid fa-magnifying-glass"></i></div><strong>${s.open_investigations ?? 0}</strong><small>Analyst queue</small></div>
      </div>
      <div class="lw-fraud-analytics">
        <div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Risk Activity — Recent Transactions</div><div class="lw-fraud-trend">${lwFraudRenderTrend(txns)}</div></div>
        <div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Risk Distribution</div>
          <div class="lw-fraud-dist-row"><div class="lw-fraud-dist-head"><span>Normal</span><strong>${lwFraudPct(dist.normal_pct)}</strong></div><div class="lw-fraud-dist-track"><div class="lw-fraud-dist-fill lw-fraud-dist-normal" style="width:${Math.min(100,Number(dist.normal_pct||0))}%"></div></div></div>
          <div class="lw-fraud-dist-row"><div class="lw-fraud-dist-head"><span>Suspicious</span><strong>${lwFraudPct(dist.suspicious_pct)}</strong></div><div class="lw-fraud-dist-track"><div class="lw-fraud-dist-fill lw-fraud-dist-suspicious" style="width:${Math.min(100,Number(dist.suspicious_pct||0))}%"></div></div></div>
          <div class="lw-fraud-dist-row"><div class="lw-fraud-dist-head"><span>Review</span><strong>${lwFraudPct(dist.review_pct)}</strong></div><div class="lw-fraud-dist-track"><div class="lw-fraud-dist-fill lw-fraud-dist-review" style="width:${Math.min(100,Number(dist.review_pct||0))}%"></div></div></div>
          <div class="lw-fraud-dist-row"><div class="lw-fraud-dist-head"><span>High Risk</span><strong>${lwFraudPct(dist.high_risk_pct)}</strong></div><div class="lw-fraud-dist-track"><div class="lw-fraud-dist-fill lw-fraud-dist-high" style="width:${Math.min(100,Number(dist.high_risk_pct||0))}%"></div></div></div>
        </div>
      </div>`;

    if (!txns.length) { body.innerHTML = '<div class="lw-empty-sub">No monitored banking transactions are available yet.</div>'; return; }
    const rows = txns.map(t => `<tr class="lw-fraud-row" data-txn-id="${t.transaction_id}" tabindex="0">
      <td><strong>${escapeLwHtml(t.account_ref || 'Account')}</strong><div class="lw-fraud-location">${escapeLwHtml(t.location || 'Location unavailable')}</div></td>
      <td><div class="lw-fraud-merchant">${escapeLwHtml(t.merchant || 'Merchant')}</div><div class="lw-fraud-signal-row">${lwFraudSignalHtml(t)}</div></td>
      <td>${lwFraudFormatCurrency(t.amount)}<div class="lw-table-sub">${escapeLwHtml(t.channel || 'UPI')}</div></td>
      <td><div>${escapeLwHtml(t.date || '—')}</div><div class="lw-table-sub">${escapeLwHtml(lwFraudTime(t.timestamp))}</div></td>
      <td><strong>${t.risk_score}</strong> / 100</td>
      <td><span class="lw-badge ${lwFraudBadgeClass(t.risk_level)}">${escapeLwHtml(t.risk_level_label)}</span></td>
      <td>${t.case ? `<span class="lw-badge ${lwFraudStatusBadgeClass(t.case.status)}">${escapeLwHtml(t.case.case_number)} · ${escapeLwHtml(t.case.status)}</span>` : (Number(t.risk_score)>=71 ? '<button type="button" class="lw-btn lw-fraud-investigate-row">Investigate</button>' : '—')}</td>
    </tr>`).join('');
    body.innerHTML = `<div class="lw-blotter-wrap"><table class="lw-table lw-fraud-table"><thead><tr><th>Account</th><th>Merchant / Signals</th><th>Amount</th><th>Date / Time</th><th>Risk Score</th><th>Level</th><th>Case / Action</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    body.querySelectorAll('.lw-fraud-row').forEach(row => {
        row.addEventListener('click', e => { if (!e.target.closest('.lw-fraud-investigate-row')) openLwFraudDetail(Number(row.dataset.txnId)); });
        row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openLwFraudDetail(Number(row.dataset.txnId)); }});
    });
    body.querySelectorAll('.lw-fraud-investigate-row').forEach(btn => btn.addEventListener('click', e => { e.stopPropagation(); openLwFraudDetail(Number(btn.closest('.lw-fraud-row').dataset.txnId)); }));
}

function lwFraudRenderCompare(base, similar) {
    if (!similar.length) return '<div class="lw-empty-sub">No nearby-amount transaction is available for comparison.</div>';
    return `<div class="lw-fraud-compare-grid">
      <div class="lw-fraud-compare-card lw-fraud-compare-base"><div class="lw-fraud-compare-kicker">SELECTED TRANSACTION</div><strong>${escapeLwHtml(base.account_ref || 'Account')}</strong><span>${escapeLwHtml(base.merchant || '')}</span><b>${lwFraudFormatCurrency(base.amount)}</b><em>${base.risk_score}/100 · ${escapeLwHtml(base.risk_level_label || '')}</em></div>
      ${similar.slice(0,2).map(t => `<div class="lw-fraud-compare-card"><div class="lw-fraud-compare-kicker">SIMILAR AMOUNT</div><strong>${escapeLwHtml(t.account_ref || 'Account')}</strong><span>${escapeLwHtml(t.merchant || '')}</span><b>${lwFraudFormatCurrency(t.amount)}</b><em>${t.risk_score}/100 · ${escapeLwHtml(t.risk_level_label || '')}</em><small>${t.signals?.new_device ? 'New device' : 'Trusted device'} · ${Number(t.signals?.location_distance_km || 0).toFixed(0)} km shift · ${Number(t.signals?.velocity_1h || 0)} txns/1h</small></div>`).join('')}
    </div><div class="lw-fraud-compare-note"><i class="fa-solid fa-scale-balanced"></i> Similar amount ≠ same risk. FraudShield combines contextual and behavioral signals instead of using amount alone.</div>`;
}

function closeLwFraudDetail() {
    const modal = document.getElementById('lwFraudDetailModal');
    if (!modal) return;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('lw-fraud-modal-open');
    lwFraudSelectedTransaction = null;
}

function lwFraudEvidenceRows(txn, features) {
    const txHour = features.transaction_hour;
    const timeText = txn.timestamp ? lwFraudTime(txn.timestamp) : (txHour !== undefined ? `${String(txHour).padStart(2, '0')}:00` : '—');
    return [
        ['Amount', lwFraudFormatCurrency(txn.amount)],
        ['Velocity', `${features.txn_count_1h || 0} / hour`],
        ['24h Activity', `${features.txn_count_24h || 0} transactions`],
        ['Location Shift', `${Math.round(Number(features.location_distance_km || 0))} km`],
        ['Device', features.is_new_device ? 'New device' : 'Trusted device'],
        ['Merchant Risk', `${(Number(features.merchant_risk || 0) * 100).toFixed(0)}%`],
        ['Account Age', `${features.account_age_days || 0} days`],
        ['Related Accounts', `${features.relationship_count || 0}`],
        ['Transaction Time', timeText],
        ['Network Risk', `${(Number(features.network_risk || 0) * 100).toFixed(0)}%`],
        ['Failed Auth', `${Number(features.failed_auth_count || 0)} attempts`],
        ['Browser / OS', `${features.browser || '—'} / ${features.os || '—'}`],
        ['Beneficiary', features.beneficiary_ref || '—'],
        ['IP', features.ip_address ? String(features.ip_address).replace(/\b(\d+\.\d+\.)\d+(\.\d+)\b/, '$1***$2') : '—'],
    ];
}

async function openLwFraudDetail(transactionId) {
    const modal = document.getElementById('lwFraudDetailModal');
    const detailBody = document.getElementById('lwFraudDetailModalBody');
    if (!modal || !detailBody) return;

    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('lw-fraud-modal-open');
    modal.querySelector('.lw-fraud-modal-panel > div')?.scrollTo(0, 0);
    detailBody.innerHTML = '<div class="lw-fraud-modal-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading transaction risk analysis...</div>';

    const data = await lwSafeJsonFetch(`/lender/fraudshield/transactions/${transactionId}`);
    if (!data) {
        detailBody.innerHTML = '<div class="lw-empty-sub">Risk detail is unavailable.</div>';
        return;
    }

    const txn = data.transaction || {}, risk = data.risk || {}, features = txn.features || {};
    lwFraudSelectedTransaction = { ...txn, risk_score: risk.score, risk_level: risk.level, risk_level_label: risk.level_label };
    const shapRows = (risk.contributors || []).map(c => `<div class="lw-fraud-shap-row"><span>${escapeLwHtml(c.label)}</span><strong class="${Number(c.value) >= 0 ? 'lw-fraud-pos' : 'lw-fraud-neg'}">${Number(c.value) >= 0 ? '+' : ''}${Number(c.value).toFixed(3)}</strong><small>${escapeLwHtml(c.direction || '')}</small></div>`).join('') || '<div class="lw-empty-sub">No SHAP explanation available.</div>';
    const evidence = lwFraudEvidenceRows(txn, features);
    const seenBefore = Boolean(features.merchant_seen_before);
    const merchantContext = seenBefore ? 'Known merchant' : 'Merchant not seen before';

    const caseBlock = data.case
        ? `<div class="lw-fraud-case-row"><span class="lw-badge ${lwFraudStatusBadgeClass(data.case.status)}">${escapeLwHtml(data.case.case_number)} · ${escapeLwHtml(data.case.status)}</span><div class="lw-fraud-case-actions"><button class="lw-btn" data-case-action="review">Mark Reviewed</button><button class="lw-btn" data-case-action="escalate">Escalate</button>${data.case.status !== 'CLOSED' ? '<button class="lw-btn" data-case-action="close">Close Case</button>' : ''}</div></div>`
        : `<div class="lw-fraud-action-box"><div><div class="lw-fraud-card-title">Recommended Action</div><strong>${escapeLwHtml(risk.recommended_action || (risk.score >= 71 ? 'Send for human investigation' : 'Review transaction context'))}</strong><p>AI prioritizes and explains. An analyst makes the final decision.</p></div><button class="lw-btn lw-btn-primary" id="lwSendForInvestigationBtn"><i class="fa-solid fa-magnifying-glass"></i> Send for Investigation</button></div>`;

    detailBody.innerHTML = `<div class="lw-fraud-detail-sheet">
      <div class="lw-fraud-detail-topbar">
        <div><div class="lw-panel-title"><i class="fa-solid fa-shield-halved"></i> Transaction Risk Analysis</div><div class="lw-portfolio-panel-note">${escapeLwHtml(txn.merchant || 'Transaction')} · ${escapeLwHtml(txn.account_ref || 'Account')} · ${escapeLwHtml(txn.channel || '')}</div></div>
        <button type="button" class="lw-fraud-modal-close" id="lwFraudDetailCloseBtn" aria-label="Close transaction details"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="lw-fraud-detail-hero">
        <div><div class="lw-fraud-detail-kicker">${escapeLwHtml(txn.account_ref || 'Account')}</div><strong>${lwFraudFormatCurrency(txn.amount)}</strong><span>${escapeLwHtml(lwFraudDateTime(txn.timestamp))} · ${escapeLwHtml(txn.location || 'Location unavailable')}</span></div>
        <div class="lw-fraud-score-box"><span>FINAL FRAUD RISK</span><strong>${risk.score} <small>/ 100</small></strong><em>${escapeLwHtml(risk.level_label)}</em></div>
      </div>
      <div class="lw-fraud-detail-callout"><div><span>MODEL PROBABILITY</span><strong>${(Number(risk.model_probability || 0) * 100).toFixed(1)}%</strong></div><div><span>ANOMALY SIGNAL</span><strong>${Number(risk.anomaly_score || 0)}/100</strong></div><div><span>RULE SIGNAL</span><strong>${Number(risk.rule_score || 0)}/60</strong></div><div><span>ACCOUNT TAKEOVER RISK</span><strong>${Number(risk.account_takeover_risk || 0)}/100</strong></div></div>
      <div class="lw-fraud-detail-grid-two">
        <div class="lw-fraud-detail-card"><div class="lw-fraud-card-title">Transaction Evidence</div><div class="lw-fraud-feature-grid">${evidence.map(([k, v]) => `<div class="lw-fraud-feature"><span>${escapeLwHtml(k)}</span><strong>${escapeLwHtml(v)}</strong></div>`).join('')}</div><div class="lw-fraud-merchant-context"><i class="fa-solid fa-store"></i> ${escapeLwHtml(merchantContext)}</div></div>
        <div class="lw-fraud-detail-card"><div class="lw-fraud-card-title">Explainable Model Evidence · SHAP</div><div class="lw-fraud-shap-list">${shapRows}</div></div>
      </div>
      ${risk.impossible_travel?.detected ? `<div class="fw-travel-alert"><div class="fw-travel-icon"><i class="fa-solid fa-route"></i></div><div><strong>Impossible Travel Detected</strong><p>${escapeLwHtml(risk.impossible_travel.previous_location)} → ${escapeLwHtml(risk.impossible_travel.current_location)} · ${risk.impossible_travel.distance_km} km in ${risk.impossible_travel.minutes} min</p></div></div>` : ''}
      <div class="lw-fraud-why"><div class="lw-fraud-card-title">Why was this flagged?</div><p>${escapeLwHtml(risk.reason || 'The model detected a combination of transaction and behavioral signals that warrant review.')}</p></div>
      ${data.ai_explanation ? `<div class="lw-fraud-ai-note"><i class="fa-solid fa-robot"></i><strong>AI summary:</strong> ${escapeLwHtml(data.ai_explanation)}</div>` : ''}
      <div class="fw-autopilot-inline">${data.autopilot?`<div class="fw-autopilot-inline-head"><div><span>AUTONOMOUS RESPONSE</span><strong>${escapeLwHtml(String(data.autopilot.action||'').replaceAll('_',' '))}</strong></div><span class="fw-action-state">${escapeLwHtml(data.autopilot.state||'ACTIVE')}</span></div><p>${data.autopilot.case_number?`Case ${escapeLwHtml(data.autopilot.case_number)} created automatically. `:''}${data.autopilot.notification_status&&data.autopilot.notification_status!=='NOT_REQUIRED'?`SOC notification: ${escapeLwHtml(data.autopilot.notification_status)}. `:''}External cybercrime reporting remains analyst-approved.</p><div class="fw-autopilot-inline-actions"><a class="lw-btn" href="/lender/fraudshield/transactions/${transactionId}/evidence" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-file-lines"></i> Incident Report</a><a class="lw-btn" href="https://cybercrime.gov.in/" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square"></i> Open NCRP</a></div>`:`<div class="fw-autopilot-inline-head"><div><span>AUTONOMOUS RESPONSE</span><strong>Not evaluated yet</strong></div><button class="lw-btn lw-btn-primary" id="lwRunAutopilotBtn"><i class="fa-solid fa-bolt"></i> Run Autopilot</button></div><p>Apply the institution's response policy to this transaction. High-risk activity can be held and assigned to an investigation automatically.</p>`}</div>
      <p class="lw-fraud-disclaimer"><i class="fa-solid fa-circle-info"></i> XGBoost produces the fraud probability, Isolation Forest adds an anomaly signal, explicit rules capture high-risk conditions, and SHAP explains model contributions. AI summarizes structured evidence only. A human investigator makes the final decision.</p>
      <div id="lwFraudCompareBlock" class="lw-fraud-compare-block"><button type="button" class="lw-btn" id="lwCompareSimilarBtn"><i class="fa-solid fa-scale-balanced"></i> Compare Similar Transactions</button> <button type="button" class="lw-btn" id="lwCounterfactualBtn"><i class="fa-solid fa-wand-magic-sparkles"></i> Risk Counterfactual</button></div>
      <div id="lwFraudCounterfactualBlock" class="lw-fraud-counterfactual-block"></div>
      <div class="lw-fraud-case-block">${caseBlock}</div>
    </div>`;

    document.getElementById('lwFraudDetailCloseBtn')?.addEventListener('click', closeLwFraudDetail);
    document.getElementById('lwCompareSimilarBtn')?.addEventListener('click', async btn => {
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Finding similar transactions...';
        const cmp = await lwSafeJsonFetch(`/lender/fraudshield/transactions/${transactionId}/similar`);
        const block = document.getElementById('lwFraudCompareBlock');
        if (cmp?.transactions && block) block.innerHTML = lwFraudRenderCompare(lwFraudSelectedTransaction, cmp.transactions);
        else { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-scale-balanced"></i> Compare Similar Transactions'; }
    });
    document.getElementById('lwCounterfactualBtn')?.addEventListener('click', async btn => {
        btn.disabled=true; btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Testing signals...';
        const data=await lwSafeJsonFetch(`/lender/fraudshield/transactions/${transactionId}/counterfactual`);
        const block=document.getElementById('lwFraudCounterfactualBlock');
        if(data&&block){
            block.innerHTML=`<div class="fw-counterfactual"><div class="lw-fraud-card-title">Risk Counterfactual Lab</div><p>What would happen to the risk if one condition changed? This is a simulation only and never mutates the transaction.</p><div class="fw-counterfactual-grid"><div class="fw-cf-base"><span>Current</span><strong>${data.base.risk_score}/100</strong><small>${escapeLwHtml(data.base.risk_level)}</small></div>${(data.scenarios||[]).map(x=>`<div class="fw-cf-card"><span>${escapeLwHtml(x.scenario)}</span><strong>${x.risk_score}/100</strong><small class="${x.delta<0?'fw-cf-down':'fw-cf-up'}">${x.delta>0?'+':''}${x.delta} vs current</small><em>${escapeLwHtml(x.risk_level)}</em></div>`).join('')}</div><div class="fw-cf-insight"><i class="fa-solid fa-lightbulb"></i><span>The largest drop is the most influential contextual lever in this scenario—not proof that the change alone prevents fraud.</span></div></div>`;
        }
        btn.disabled=false; btn.innerHTML='<i class="fa-solid fa-wand-magic-sparkles"></i> Risk Counterfactual';
    });
    document.getElementById('lwRunAutopilotBtn')?.addEventListener('click', async btn => {
        btn.disabled=true; btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Applying policy...';
        const resp=await lwSafeJsonFetch(`/lender/fraudshield/transactions/${transactionId}/autopilot`,{method:'POST'});
        if(resp) { await loadLwFraudMonitor(); await openLwFraudDetail(transactionId); }
        else { btn.disabled=false; btn.innerHTML='<i class="fa-solid fa-bolt"></i> Run Autopilot'; }
    });
    document.getElementById('lwSendForInvestigationBtn')?.addEventListener('click', async btn => {
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Opening case...';
        const r = await lwSafeJsonFetch(`/lender/fraudshield/transactions/${transactionId}/investigate`, { method: 'POST' });
        if (r?.case) { closeLwFraudDetail(); await loadLwFraudMonitor(); openLwFraudDetail(transactionId); }
        else { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> Send for Investigation'; }
    });
    detailBody.querySelectorAll('[data-case-action]').forEach(btn => btn.addEventListener('click', async () => {
        const action = btn.dataset.caseAction;
        const caseId = (data.case.case_number || '').replace('FS-', '');
        btn.disabled = true;
        const r = await lwSafeJsonFetch(`/lender/fraudshield/investigations/${caseId}/status`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
        if (r) { await loadLwFraudMonitor(); await openLwFraudDetail(transactionId); }
        else btn.disabled = false;
    }));
}

async function loadLwFraudInvestigations() {
    const body=document.getElementById('lwInvestigationsBody'); if(!body) return;
    body.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading investigations...</div>';
    const data=await lwSafeJsonFetch('/lender/fraudshield/investigations');
    if(!data){body.innerHTML='<div class="lw-empty-sub">Investigations are unavailable.</div>';return;}
    const cases=data.investigations||[];
    if(!cases.length){body.innerHTML='<div class="lw-empty-sub">No investigation cases yet. Open a case from Transaction Monitor.</div>';return;}
    body.innerHTML=`<div class="lw-blotter-wrap"><table class="lw-table"><thead><tr><th>Case</th><th>Account</th><th>Merchant</th><th>Amount</th><th>Risk</th><th>Status</th><th>Opened</th><th></th></tr></thead><tbody>${cases.map(c=>`<tr><td><strong>${escapeLwHtml(c.case_number)}</strong></td><td>${escapeLwHtml(c.account_ref||'—')}</td><td>${escapeLwHtml(c.merchant||'—')}</td><td>${lwFraudFormatCurrency(c.amount)}</td><td><strong>${c.risk_score}</strong> / 100</td><td><span class="lw-badge ${lwFraudStatusBadgeClass(c.status)}">${escapeLwHtml(c.status)}</span></td><td>${escapeLwHtml(c.opened_at?c.opened_at.slice(0,10):'—')}</td><td><button class="lw-btn" data-txn-id="${c.transaction_id}">View</button></td></tr>`).join('')}</tbody></table></div>`;
    body.querySelectorAll('[data-txn-id]').forEach(btn=>btn.addEventListener('click',()=>{document.querySelector('.lw-nav-item[data-section="fraud-monitor"]')?.click();setTimeout(()=>openLwFraudDetail(Number(btn.dataset.txnId)),50);}));
}


/* ============================================================================
   POST-PHASE-1 FRAUDSHIELD INTELLIGENCE UI
   ============================================================================ */
let fraudAdvancedInitialized = false;
let fraudBehaviorAccount = 'AC-10426';
const fraudAdvancedLoaded = new Set();

function initFraudAdvancedUI() {
    if (fraudAdvancedInitialized) return;
    fraudAdvancedInitialized = true;
    const buttons = document.querySelectorAll('.fraud-system-nav .lw-nav-item[data-section^="fraud-"]');
    buttons.forEach(btn => {
        const section = btn.getAttribute('data-section');
        if (!['fraud-monitor','fraud-investigations'].includes(section)) {
            btn.addEventListener('click', () => {
                if (!fraudAdvancedLoaded.has(section)) setTimeout(() => loadFraudAdvancedSection(section), 0);
            });
        }
    });
    document.querySelector('.lw-nav-item[data-section="fraud-investigations"]')?.addEventListener('click', () => {
        if (!fraudAdvancedLoaded.has('fraud-investigations')) {
            if (lwFraudBootstrap) renderFraudInvestigationsFromSnapshot(lwFraudBootstrap);
            else loadLwFraudInvestigations();
        }
    });
    document.getElementById('lwFraudRulesSaveBtn')?.addEventListener('click', saveFraudRules);
    document.getElementById('lwFraudRetrainBtn')?.addEventListener('click', async e => {
        const btn=e.currentTarget; btn.disabled=true; btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Retraining...';
        const r=await lwSafeJsonFetch('/lender/fraudshield/model/retrain',{method:'POST'});
        btn.disabled=false; btn.innerHTML='<i class="fa-solid fa-rotate"></i> Retrain';
        if(r){ await loadFraudModelCenter(); await loadLwFraudMonitor(); }
    });
}

function loadFraudAdvancedSection(section, silent=false) {
    // Most advanced pages can render instantly from the monitor bootstrap.
    // Fraud Network is deliberately loaded on demand because it performs the
    // one-time relationship enrichment needed to build the graph.
    if (lwFraudBootstrap && ['fraud-alerts','fraud-behavior','fraud-model','fraud-security'].includes(section)) {
        const renderers={
            'fraud-alerts':renderFraudAlertsFromSnapshot,
            'fraud-behavior':renderFraudBehaviorFromSnapshot,
            'fraud-model':renderFraudModelFromSnapshot,
            'fraud-security':renderFraudSecurityFromSnapshot,
        };
        renderers[section]?.(lwFraudBootstrap);
        return;
    }
    const loaders={'fraud-rules':loadFraudRules,'fraud-autopilot':loadFraudAutopilot,'fraud-network':loadFraudNetwork,'fraud-simulator':loadFraudSimulator,'fraud-copilot':loadFraudCopilot,'fraud-investigations':loadLwFraudInvestigations};
    const loader=loaders[section];
    if(loader) loader(silent).catch(()=>{});
}

function lwFraudFeatureValue(value) {
    if (value === null || value === undefined || value === '') return '—';
    return typeof value === 'number' ? value.toLocaleString('en-IN', {maximumFractionDigits:2}) : String(value);
}

function renderFraudAlertsFromSnapshot(data) {
    const el=document.getElementById('lwFraudAlertsBody'); if(!el)return; const alerts=data.alerts||[];
    if(!alerts.length){el.innerHTML='<div class="lw-empty-sub">No alerts above the monitoring threshold.</div>'; fraudAdvancedLoaded.add('fraud-alerts'); return;}
    el.innerHTML=`<div class="fw-intel-banner"><div><strong>Smart triage</strong><span>Alerts are prioritized instead of flagging every anomaly.</span></div><div class="fw-triage-legend"><span class="fw-dot green"></span>Monitor <span class="fw-dot amber"></span>Review <span class="fw-dot red"></span>Investigate</div></div><div class="lw-blotter-wrap"><table class="lw-table"><thead><tr><th>Priority</th><th>Account</th><th>Merchant</th><th>Amount</th><th>Risk</th><th>Why it surfaced</th><th></th></tr></thead><tbody>${alerts.map(a=>`<tr><td><span class="fw-triage-pill ${a.triage?.color||'amber'}">${escapeLwHtml(a.triage?.decision||'REVIEW')}</span></td><td><strong>${escapeLwHtml(a.account_ref)}</strong></td><td>${escapeLwHtml(a.merchant)}</td><td>${lwFraudFormatCurrency(a.amount)}</td><td><strong>${a.score}</strong>/100</td><td>${(a.signals||[]).slice(0,2).map(x=>escapeLwHtml(x.label||x)).join(' · ')}</td><td><button class="lw-btn" data-alert-txn="${a.transaction_id}">Explain</button></td></tr>`).join('')}</tbody></table></div>`;
    el.querySelectorAll('[data-alert-txn]').forEach(btn=>btn.addEventListener('click',()=>openLwFraudDetail(Number(btn.dataset.alertTxn)))); fraudAdvancedLoaded.add('fraud-alerts');
}
function renderFraudBehaviorFromSnapshot(data) {
    const el=document.getElementById('lwFraudBehaviorBody'); if(!el)return; const p=(data.behaviors||{})[fraudBehaviorAccount]||{account:fraudBehaviorAccount,samples:0};
    el.innerHTML=`<div class="fw-behavior-toolbar"><label>Account <select id="fwBehaviorAccount"><option>AC-10426</option><option>AC-10428</option><option>AC-RING-01</option><option>AC-10433</option></select></label><span>Profile is calculated only from the FraudShield transaction stream.</span></div><div class="fw-profile-grid"><div class="fw-profile-card"><span>Typical Amount</span><strong>${lwFraudFormatCurrency(p.typical_amount||0)}</strong><small>median observed</small></div><div class="fw-profile-card"><span>Average Amount</span><strong>${lwFraudFormatCurrency(p.average_amount||0)}</strong><small>historical mean</small></div><div class="fw-profile-card"><span>Peak Hour</span><strong>${String(p.peak_hour??'—').padStart(2,'0')}:00</strong><small>most common</small></div><div class="fw-profile-card"><span>Avg Velocity</span><strong>${lwFraudFeatureValue(p.avg_velocity_1h)}</strong><small>transactions / hour</small></div><div class="fw-profile-card"><span>New Device Rate</span><strong>${lwFraudFeatureValue(p.new_device_rate)}%</strong><small>historical</small></div><div class="fw-profile-card"><span>Risk Baseline</span><strong>${lwFraudFeatureValue(p.risk_baseline)}</strong><small>network-risk baseline</small></div></div><div class="fw-behavior-split"><div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Normal Locations</div><div class="fw-chip-list">${(p.common_locations||[]).map(x=>`<span>${escapeLwHtml(x)}</span>`).join('')||'<span>Not enough history</span>'}</div></div><div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Common Merchants</div><div class="fw-chip-list">${(p.common_merchants||[]).map(x=>`<span>${escapeLwHtml(x)}</span>`).join('')||'<span>Not enough history</span>'}</div></div></div><div class="fw-profile-insight"><i class="fa-solid fa-fingerprint"></i><div><strong>Behavioral fingerprint</strong><p>${escapeLwHtml(p.note||'FraudShield compares new events against observed account behavior rather than relying only on global thresholds.')}</p></div></div>`;
    document.getElementById('fwBehaviorAccount')?.addEventListener('change',e=>{fraudBehaviorAccount=e.target.value;renderFraudBehaviorFromSnapshot(lwFraudBootstrap);}); const select=document.getElementById('fwBehaviorAccount'); if(select)select.value=fraudBehaviorAccount; fraudAdvancedLoaded.add('fraud-behavior');
}
function renderFraudNetworkGraph(data, markLoaded=true) {
    const el=document.getElementById('lwFraudNetworkBody');
    if(!el)return;
    const g=data.graph||{};
    const allNodes=g.nodes||[];
    const allEdges=g.edges||[];
    const comps=g.communities||[];
    if(!allNodes.length){
        el.innerHTML='<div class="lw-empty-sub">No relationship data is available yet.</div>';
        if(markLoaded) fraudAdvancedLoaded.add('fraud-network');
        return;
    }

    const nodeById=new Map(allNodes.map(n=>[String(n.id),n]));
    const degree=new Map(allNodes.map(n=>[String(n.id),0]));
    allEdges.forEach(e=>{
        degree.set(String(e.source),(degree.get(String(e.source))||0)+1);
        degree.set(String(e.target),(degree.get(String(e.target))||0)+1);
    });

    // Keep the canvas presentation-ready: prioritize nodes from the riskiest
    // candidate communities, then high-degree/high-risk nodes and their neighbors.
    const selected=new Set();
    comps.slice().sort((a,b)=>Number(b.network_risk||0)-Number(a.network_risk||0)).slice(0,3)
        .forEach(c=>(c.accounts||[]).forEach(id=>selected.add(String(id))));
    allNodes.slice().sort((a,b)=>
        (degree.get(String(b.id))||0)-(degree.get(String(a.id))||0) || Number(b.risk||0)-Number(a.risk||0)
    ).forEach(n=>{if(selected.size<18) selected.add(String(n.id));});
    // Pull in direct context for selected high-value nodes.
    allEdges.forEach(e=>{
        if(selected.size>=28)return;
        const a=String(e.source),b=String(e.target);
        if(selected.has(a)&&!selected.has(b))selected.add(b);
        else if(selected.has(b)&&!selected.has(a))selected.add(a);
    });
    const visibleNodes=allNodes.filter(n=>selected.has(String(n.id))).slice(0,28);
    const visibleIds=new Set(visibleNodes.map(n=>String(n.id)));
    const visibleEdges=allEdges.filter(e=>visibleIds.has(String(e.source))&&visibleIds.has(String(e.target))).slice(0,45);

    const groups={account:[],device:[],merchant:[],beneficiary:[]};
    visibleNodes.forEach(n=>(groups[n.type]||(groups[n.type]=[])).push(n));
    Object.values(groups).forEach(arr=>arr.sort((a,b)=>Number(b.risk||0)-Number(a.risk||0)||(degree.get(String(b.id))||0)-(degree.get(String(a.id))||0)));

    const W=1200,H=620;
    const xByType={account:145,device:455,merchant:760,beneficiary:1060};
    const positions=new Map();
    Object.entries(groups).forEach(([type,arr])=>{
        if(!arr.length)return;
        const usable=H-110;
        const step=Math.min(92,usable/Math.max(1,arr.length));
        const total=step*Math.max(0,arr.length-1);
        const startY=(H-total)/2;
        arr.forEach((n,i)=>positions.set(String(n.id),{x:xByType[type]||600,y:startY+i*step}));
    });

    const typeLabel={account:'ACCOUNT',device:'DEVICE',merchant:'MERCHANT',beneficiary:'BENEFICIARY'};
    const edgeLabel={USES_DEVICE:'uses device',PAYS_MERCHANT:'pays merchant',PAYS_BENEFICIARY:'pays beneficiary',SHARED_DEVICE:'shared device',SHARED_MERCHANT:'shared merchant'};
    const nodeClass=n=>`fw-svg-node fw-svg-${escapeLwHtml(n.type||'account')}${Number(n.risk||0)>=70?' is-risk':''}`;
    const displayLabel=n=>String(n.label||n.id||'').replace(/^merchant:|^beneficiary:|^device:/,'');

    const edgeSvg=visibleEdges.map((e,i)=>{
        const s=positions.get(String(e.source)),t=positions.get(String(e.target));
        if(!s||!t)return '';
        const isShared=String(e.type).startsWith('SHARED_');
        const bend=isShared?Math.max(45,Math.abs(t.y-s.y)*.35):0;
        let d;
        if(isShared && Math.abs(t.x-s.x)<30){
            const dir=i%2?1:-1;
            d=`M ${s.x} ${s.y} C ${s.x+dir*150} ${s.y}, ${t.x+dir*150} ${t.y}, ${t.x} ${t.y}`;
        } else {
            const mx=(s.x+t.x)/2;
            d=`M ${s.x} ${s.y} C ${mx} ${s.y-bend}, ${mx} ${t.y+bend}, ${t.x} ${t.y}`;
        }
        const title=`${displayLabel(nodeById.get(String(e.source))||{id:e.source})} → ${displayLabel(nodeById.get(String(e.target))||{id:e.target})}: ${edgeLabel[e.type]||String(e.type).replaceAll('_',' ').toLowerCase()}`;
        return `<path class="fw-svg-edge fw-edge-${escapeLwHtml(String(e.type||'').toLowerCase())}" data-source="${escapeLwHtml(String(e.source))}" data-target="${escapeLwHtml(String(e.target))}" d="${d}"><title>${escapeLwHtml(title)}</title></path>`;
    }).join('');

    const nodeSvg=visibleNodes.map(n=>{
        const p=positions.get(String(n.id)); if(!p)return '';
        const label=displayLabel(n),risk=Number(n.risk||0),deg=degree.get(String(n.id))||0;
        const width=n.type==='merchant'?170:150;
        const x=p.x-width/2,y=p.y-29;
        const clipped=label.length>20?label.slice(0,18)+'…':label;
        return `<g class="${nodeClass(n)}" data-node-id="${escapeLwHtml(String(n.id))}" tabindex="0" transform="translate(${x},${y})">
            <rect width="${width}" height="58" rx="12"></rect>
            <circle class="fw-svg-node-dot" cx="15" cy="17" r="4"></circle>
            <text class="fw-svg-node-label" x="27" y="21">${escapeLwHtml(clipped)}</text>
            <text class="fw-svg-node-type" x="15" y="42">${escapeLwHtml(typeLabel[n.type]||String(n.type).toUpperCase())} · ${deg} link${deg===1?'':'s'}${risk?` · risk ${Math.round(risk)}`:''}</text>
            <title>${escapeLwHtml(label)} — ${typeLabel[n.type]||n.type}; ${deg} relationships${risk?`; risk ${Math.round(risk)}/100`:''}</title>
        </g>`;
    }).join('');

    const legend=`<div class="fw-network-legend">
        <span><i class="fw-legend-dot account"></i>Account</span>
        <span><i class="fw-legend-dot device"></i>Device</span>
        <span><i class="fw-legend-dot merchant"></i>Merchant</span>
        <span><i class="fw-legend-dot beneficiary"></i>Beneficiary</span>
        <span><i class="fw-legend-line"></i>Relationship</span>
        <span><i class="fw-legend-line shared"></i>Shared signal</span>
    </div>`;

    el.innerHTML=`<div class="fw-network-hero"><div><strong>Relationship Intelligence</strong><span>${allNodes.length} nodes · ${allEdges.length} relationships · showing ${visibleNodes.length} high-value nodes</span></div><div class="fw-ring-count">${comps.length}<small>candidate networks</small></div></div>
        ${legend}
        <div class="fw-network-canvas fw-network-canvas-svg">
            <svg class="fw-network-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Fraud relationship graph">
                <defs><marker id="fwArrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L7,3 z" class="fw-arrow-head"></path></marker></defs>
                ${edgeSvg}${nodeSvg}
            </svg>
            <div class="fw-network-hint"><i class="fa-solid fa-arrow-pointer"></i> Hover or focus a node to isolate its relationships</div>
        </div>
        <div class="fw-network-findings">${comps.length?comps.slice(0,3).map(c=>`<div class="fw-network-finding"><span class="fw-network-score">${c.network_risk}</span><div><strong>Potential coordinated network</strong><p>${(c.accounts||[]).map(escapeLwHtml).join(' · ')}</p><small>${escapeLwHtml(c.reason)} · ${c.edge_count} linked relationships</small></div></div>`).join(''):'<div class="lw-empty-sub">No multi-account community crossed the network-analysis threshold.</div>'}<div class="fw-network-note"><i class="fa-solid fa-circle-info"></i> Graph relationships are investigation signals, not proof of coordinated fraud.</div></div>`;

    const svg=el.querySelector('.fw-network-svg');
    if(svg){
        svg.querySelectorAll('.fw-svg-node').forEach(node=>{
            const focus=()=>{
                const id=node.dataset.nodeId;
                svg.classList.add('is-filtering');
                svg.querySelectorAll('.fw-svg-node').forEach(n=>n.classList.toggle('is-related',n.dataset.nodeId===id));
                svg.querySelectorAll('.fw-svg-edge').forEach(edge=>{
                    const related=edge.dataset.source===id||edge.dataset.target===id;
                    edge.classList.toggle('is-related',related);
                    if(related){
                        const other=edge.dataset.source===id?edge.dataset.target:edge.dataset.source;
                        svg.querySelector(`.fw-svg-node[data-node-id="${CSS.escape(other)}"]`)?.classList.add('is-related');
                    }
                });
            };
            const clear=()=>{
                svg.classList.remove('is-filtering');
                svg.querySelectorAll('.is-related').forEach(x=>x.classList.remove('is-related'));
            };
            node.addEventListener('mouseenter',focus); node.addEventListener('mouseleave',clear);
            node.addEventListener('focus',focus); node.addEventListener('blur',clear);
        });
    }
    if(markLoaded) fraudAdvancedLoaded.add('fraud-network');
}

function renderFraudNetworkFromSnapshot(data) {
    renderFraudNetworkGraph(data, true);
}

function renderFraudModelFromSnapshot(data){ const el=document.getElementById('lwFraudModelCenterBody'); if(!el)return; const m=data.model||{}; el.innerHTML=`<div class="fw-model-center"><div class="fw-model-identity"><div class="fw-model-orb"><i class="fa-solid fa-microchip"></i></div><div><strong>${escapeLwHtml(m.model_version||'FraudShield Model')}</strong><p>${escapeLwHtml(m.dataset||'—')} · ${m.training_records||0} training records</p><span class="fw-model-ready">● ${escapeLwHtml(m.status||'READY')}</span></div></div><div class="fw-model-score-grid"><div><span>ROC-AUC</span><strong>${m.roc_auc??'—'}</strong></div><div><span>Precision</span><strong>${m.precision??'—'}</strong></div><div><span>Recall</span><strong>${m.recall??'—'}</strong></div><div><span>F1</span><strong>${m.f1??'—'}</strong></div></div><div id="fwModelEval" class="fw-model-contract"><div class="lw-fraud-card-title">AI evaluation</div><p>Running synthetic red-team benchmark…</p></div><div class="fw-model-contract"><div class="lw-fraud-card-title">Replaceable model contract</div><p>The UI consumes a stable fraud-probability + feature contract. Replace the trained artifact without changing the dashboard or investigation workflow.</p><code>ml/fraudshield/fraudshield_model.joblib</code></div><div class="fw-model-features"><div class="lw-fraud-card-title">Feature contract</div>${(m.features||[]).map(f=>`<span>${escapeLwHtml(String(f).replaceAll('_',' '))}</span>`).join('')}</div></div>`; loadFraudEvaluation(); fraudAdvancedLoaded.add('fraud-model'); }
async function loadFraudEvaluation(){ const el=document.getElementById('fwModelEval'); if(!el)return; const d=await lwSafeJsonFetch('/lender/fraudshield/intelligence/evaluation'); const e=d?.evaluation; if(!e){el.innerHTML='<div class="lw-fraud-card-title">AI evaluation</div><p>Evaluation unavailable.</p>';return;} el.innerHTML=`<div class="lw-fraud-card-title">AI evaluation · ${escapeLwHtml(e.benchmark)}</div><div class="fw-model-score-grid"><div><span>Accuracy</span><strong>${(Number(e.accuracy)*100).toFixed(1)}%</strong></div><div><span>Precision</span><strong>${(Number(e.precision)*100).toFixed(1)}%</strong></div><div><span>Recall</span><strong>${(Number(e.recall)*100).toFixed(1)}%</strong></div><div><span>Avg latency</span><strong>${Number(e.latency_ms?.avg||0).toFixed(1)} ms</strong></div></div><p>${escapeLwHtml(e.note||'')}</p><small>Explainability ${(Number(e.explainability_coverage)*100).toFixed(0)}% · deterministic scoring cost $${Number(e.ai_cost_usd||0).toFixed(2)}</small>`; }
function renderFraudSecurityFromSnapshot(data){ const el=document.getElementById('lwFraudSecurityBody'); if(!el)return; const controls=(data.security||{}).controls||[]; el.innerHTML=`<div class="fw-security-grid">${controls.map(c=>`<div class="fw-security-card"><span class="fw-status-dot"></span><div><strong>${escapeLwHtml(c.name)}</strong><small>${escapeLwHtml(c.status)}</small><p>${escapeLwHtml(c.detail)}</p></div></div>`).join('')}</div><div class="fw-security-note"><i class="fa-solid fa-shield-heart"></i><div><strong>FraudShield security posture</strong><p>Institution data is isolated from the consumer/lending transaction domain. FraudShield records security-relevant events and blocks duplicate/replay fingerprints before they can be processed twice.</p></div></div>`; fraudAdvancedLoaded.add('fraud-security'); }
function renderFraudInvestigationsFromSnapshot(data){ const body=document.getElementById('lwInvestigationsBody'); if(!body)return; const cases=data.investigations||[]; if(!cases.length){body.innerHTML='<div class="lw-empty-sub">No investigation cases yet. Open a case from Transaction Monitor.</div>'; fraudAdvancedLoaded.add('fraud-investigations'); return;} body.innerHTML=`<div class="lw-blotter-wrap"><table class="lw-table"><thead><tr><th>Case</th><th>Account</th><th>Merchant</th><th>Amount</th><th>Risk</th><th>Status</th><th>Opened</th><th></th></tr></thead><tbody>${cases.map(c=>`<tr><td><strong>${escapeLwHtml(c.case_number)}</strong></td><td>${escapeLwHtml(c.account_ref||'—')}</td><td>${escapeLwHtml(c.merchant||'—')}</td><td>${lwFraudFormatCurrency(c.amount)}</td><td><strong>${c.risk_score}</strong> / 100</td><td><span class="lw-badge ${lwFraudStatusBadgeClass(c.status)}">${escapeLwHtml(c.status)}</span></td><td>${escapeLwHtml(c.opened_at?c.opened_at.slice(0,10):'—')}</td><td><button class="lw-btn" data-txn-id="${c.transaction_id}">View</button></td></tr>`).join('')}</tbody></table></div>`; body.querySelectorAll('[data-txn-id]').forEach(btn=>btn.addEventListener('click',()=>{document.querySelector('.lw-nav-item[data-section="fraud-monitor"]')?.click();setTimeout(()=>openLwFraudDetail(Number(btn.dataset.txnId)),50);})); fraudAdvancedLoaded.add('fraud-investigations'); }

async function loadFraudAlerts() {
    const el=document.getElementById('lwFraudAlertsBody'); if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Prioritizing alerts...</div>';
    const data=await lwSafeJsonFetch('/lender/fraudshield/intelligence/alerts');
    if(!data){el.innerHTML='<div class="lw-empty-sub">Risk alerts are unavailable.</div>';return;}
    const alerts=data.alerts||[];
    if(!alerts.length){el.innerHTML='<div class="lw-empty-sub">No alerts above the monitoring threshold.</div>';return;}
    el.innerHTML=`<div class="fw-intel-banner"><div><strong>Smart triage</strong><span>Alerts are prioritized instead of flagging every anomaly.</span></div><div class="fw-triage-legend"><span class="fw-dot green"></span>Monitor <span class="fw-dot amber"></span>Review <span class="fw-dot red"></span>Investigate</div></div><div class="lw-blotter-wrap"><table class="lw-table"><thead><tr><th>Priority</th><th>Account</th><th>Merchant</th><th>Amount</th><th>Risk</th><th>Why it surfaced</th><th></th></tr></thead><tbody>${alerts.map(a=>`<tr><td><span class="fw-triage-pill ${a.triage?.color||'amber'}">${escapeLwHtml(a.triage?.decision||'REVIEW')}</span></td><td><strong>${escapeLwHtml(a.account_ref)}</strong></td><td>${escapeLwHtml(a.merchant)}</td><td>${lwFraudFormatCurrency(a.amount)}</td><td><strong>${a.score}</strong>/100</td><td>${(a.signals||[]).slice(0,2).map(x=>escapeLwHtml(x.label)).join(' · ')}</td><td><button class="lw-btn" data-alert-txn="${a.transaction_id}">Explain</button></td></tr>`).join('')}</tbody></table></div>`;
    el.querySelectorAll('[data-alert-txn]').forEach(btn=>btn.addEventListener('click',()=>openLwFraudDetail(Number(btn.dataset.alertTxn))));
}

async function loadFraudBehavior() {
    const el=document.getElementById('lwFraudBehaviorBody'); if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Learning behavioral baseline...</div>';
    const data=await lwSafeJsonFetch(`/lender/fraudshield/intelligence/behavior/${encodeURIComponent(fraudBehaviorAccount)}`);
    if(!data){el.innerHTML='<div class="lw-empty-sub">Behavioral profile unavailable.</div>';return;}
    const p=data.profile||{};
    el.innerHTML=`<div class="fw-behavior-toolbar"><label>Account <select id="fwBehaviorAccount"><option>AC-10426</option><option>AC-10428</option><option>AC-RING-01</option><option>AC-10433</option></select></label><span>Profile is calculated only from the FraudShield transaction stream.</span></div><div class="fw-profile-grid"><div class="fw-profile-card"><span>Typical Amount</span><strong>${lwFraudFormatCurrency(p.typical_amount||0)}</strong><small>median observed</small></div><div class="fw-profile-card"><span>Average Amount</span><strong>${lwFraudFormatCurrency(p.average_amount||0)}</strong><small>historical mean</small></div><div class="fw-profile-card"><span>Peak Hour</span><strong>${String(p.peak_hour??'—').padStart(2,'0')}:00</strong><small>most common</small></div><div class="fw-profile-card"><span>Avg Velocity</span><strong>${lwFraudFeatureValue(p.avg_velocity_1h)}</strong><small>transactions / hour</small></div><div class="fw-profile-card"><span>New Device Rate</span><strong>${lwFraudFeatureValue(p.new_device_rate)}%</strong><small>historical</small></div><div class="fw-profile-card"><span>Risk Baseline</span><strong>${lwFraudFeatureValue(p.risk_baseline)}</strong><small>network-risk baseline</small></div></div><div class="fw-behavior-split"><div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Normal Locations</div><div class="fw-chip-list">${(p.common_locations||[]).map(x=>`<span>${escapeLwHtml(x)}</span>`).join('')||'<span>Not enough history</span>'}</div></div><div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Common Merchants</div><div class="fw-chip-list">${(p.common_merchants||[]).map(x=>`<span>${escapeLwHtml(x)}</span>`).join('')||'<span>Not enough history</span>'}</div></div></div><div class="fw-profile-insight"><i class="fa-solid fa-fingerprint"></i><div><strong>Behavioral fingerprint</strong><p>${escapeLwHtml(p.note||'FraudShield compares new events against observed account behavior rather than relying only on global thresholds.')}</p></div></div>`;
    document.getElementById('fwBehaviorAccount')?.addEventListener('change',e=>{fraudBehaviorAccount=e.target.value;loadFraudBehavior();});
    const select=document.getElementById('fwBehaviorAccount'); if(select)select.value=fraudBehaviorAccount;
}

async function loadFraudNetwork() {
    const el=document.getElementById('lwFraudNetworkBody'); if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Building relationship graph...</div>';
    const data=await lwSafeJsonFetch('/lender/fraudshield/intelligence/network');
    if(!data){el.innerHTML='<div class="lw-empty-sub">Network intelligence unavailable.</div>';return;}
    renderFraudNetworkGraph(data, true);
}

async function loadFraudSimulator() {
    const el=document.getElementById('lwFraudSimulatorBody'); if(!el)return;
    el.innerHTML=`<div class="fw-sim-controls"><label>Attack Scenario<select id="fwSimScenario"><option value="account_takeover">Account Takeover</option><option value="rapid_upi_burst">Rapid UPI Burst</option><option value="impossible_travel">Impossible Travel</option><option value="merchant_fraud">Merchant Fraud</option><option value="fraud_ring">Coordinated Fraud Ring</option><option value="replay_attack">Replay Attack</option></select></label><label class="fw-check"><input type="checkbox" id="fwSimPersist" checked> Add generated events to monitor</label><button class="lw-btn lw-btn-primary" id="fwRunSimulation"><i class="fa-solid fa-play"></i> Simulate Attack</button></div><div id="fwSimResults" class="fw-sim-results"><div class="lw-empty-sub">Select a scenario and simulate an attack against the same detection pipeline.</div></div>`;
    document.getElementById('fwRunSimulation')?.addEventListener('click',async()=>{const b=document.getElementById('fwRunSimulation');b.disabled=true;b.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Simulating...';const scenario=document.getElementById('fwSimScenario').value,persist=document.getElementById('fwSimPersist').checked;const d=await lwSafeJsonFetch('/lender/fraudshield/intelligence/simulate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scenario,persist})});b.disabled=false;b.innerHTML='<i class="fa-solid fa-play"></i> Simulate Attack';const out=document.getElementById('fwSimResults');if(!d){out.innerHTML='<div class="lw-empty-sub">Simulation failed.</div>';return;}out.innerHTML=`<div class="fw-sim-kpis"><div><span>Generated</span><strong>${d.stats.generated}</strong></div><div><span>Detected</span><strong>${d.stats.detected}</strong></div><div><span>Detection Rate</span><strong>${d.stats.detection_rate}%</strong></div><div><span>Avg Risk</span><strong>${d.stats.avg_risk}</strong></div>${Number(d.stats.replay_blocked||0)>0?`<div><span>Replay Blocked</span><strong>${d.stats.replay_blocked}</strong></div>`:''}</div><div class="fw-sim-story"><strong>${escapeLwHtml(scenario.replaceAll('_',' ').replace(/\b\w/g,m=>m.toUpperCase()))}</strong><p>The scenario was passed through the same scoring pipeline used for monitored events. ${persist?'The generated event(s) were persisted — open Fraud Autopilot and click <b>Scan Current Alerts</b> to demonstrate autonomous response.':'Persistence was disabled, so this run will not appear in an Autopilot scan.'} Detection is treated as prioritization, not proof of fraud.</p></div><div class="lw-blotter-wrap"><table class="lw-table"><thead><tr><th>Account</th><th>Merchant</th><th>Amount</th><th>Risk</th><th>Top signals</th></tr></thead><tbody>${(d.results||[]).map(r=>`<tr><td>${escapeLwHtml(r.account_ref)}</td><td>${escapeLwHtml(r.merchant)}</td><td>${lwFraudFormatCurrency(r.amount)}</td><td><strong>${r.risk_score}</strong>/100</td><td>${(r.signals||[]).slice(0,2).map(x=>escapeLwHtml(x.label)).join(' · ')}</td></tr>`).join('')}</tbody></table></div>`;if(persist)loadLwFraudMonitor();});
}

async function loadFraudModelCenter() {
    const el=document.getElementById('lwFraudModelCenterBody'); if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading model center...</div>';
    const data=await lwSafeJsonFetch('/lender/fraudshield/model');
    if(!data){el.innerHTML='<div class="lw-empty-sub">Model center unavailable.</div>';return;}
    const m=data.model||{};
    el.innerHTML=`<div class="fw-model-center"><div class="fw-model-identity"><div class="fw-model-orb"><i class="fa-solid fa-microchip"></i></div><div><strong>${escapeLwHtml(m.model_version||'FraudShield Model')}</strong><p>${escapeLwHtml(m.dataset||'—')} · ${m.training_records||0} training records</p><span class="fw-model-ready">● ${escapeLwHtml(m.status||'READY')}</span></div></div><div class="fw-model-score-grid"><div><span>ROC-AUC</span><strong>${m.roc_auc??'—'}</strong></div><div><span>Precision</span><strong>${m.precision??'—'}</strong></div><div><span>Recall</span><strong>${m.recall??'—'}</strong></div><div><span>F1</span><strong>${m.f1??'—'}</strong></div></div><div class="fw-model-contract"><div class="lw-fraud-card-title">Replaceable model contract</div><p>The UI consumes a stable fraud-probability + feature contract. Replace the trained artifact with a compatible IEEE-CIS/LightGBM model without changing the dashboard or investigation workflow.</p><code>ml/fraudshield/fraudshield_model.joblib</code></div><div class="fw-model-features"><div class="lw-fraud-card-title">Feature contract</div>${(m.features||[]).map(f=>`<span>${escapeLwHtml(String(f).replaceAll('_',' '))}</span>`).join('')}</div></div>`;
}

async function loadFraudCopilot() {
    const el=document.getElementById('lwFraudCopilotBody'); if(!el)return;
    el.innerHTML=`<div class="fw-copilot"><div class="fw-copilot-head"><div class="fw-copilot-icon"><i class="fa-solid fa-robot"></i></div><div><strong>Evidence-grounded Fraud Copilot</strong><p>Ask questions about monitored activity. Answers are limited to structured FraudShield evidence.</p></div></div><div class="fw-copilot-suggestions"><button data-copilot-q="Why is the highest-risk transaction suspicious?">Why is the highest-risk transaction suspicious?</button><button data-copilot-q="Show me the main risk patterns.">Show me the main risk patterns.</button><button data-copilot-q="Which activity should an analyst review first?">Which activity should an analyst review first?</button></div><div id="fwCopilotAnswer" class="fw-copilot-answer"><div class="lw-empty-sub">Ask a question to start an evidence-grounded investigation.</div></div><div class="fw-copilot-compose"><input id="fwCopilotInput" maxlength="500" placeholder="Ask FraudShield Copilot..."/><button class="lw-btn lw-btn-primary" id="fwCopilotAsk"><i class="fa-solid fa-paper-plane"></i> Ask</button></div></div>`;
    el.querySelectorAll('[data-copilot-q]').forEach(b=>b.addEventListener('click',()=>{document.getElementById('fwCopilotInput').value=b.dataset.copilotQ;}));
    document.getElementById('fwCopilotAsk')?.addEventListener('click',async()=>{const input=document.getElementById('fwCopilotInput'),out=document.getElementById('fwCopilotAnswer'),q=input.value.trim();if(!q)return;out.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Reviewing evidence...</div>';const d=await lwSafeJsonFetch('/lender/fraudshield/intelligence/copilot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});out.innerHTML=d?`<div class="fw-copilot-response"><div class="fw-copilot-response-title"><i class="fa-solid fa-robot"></i> Copilot</div><p>${escapeLwHtml(d.answer||'No answer available.')}</p><div class="fw-copilot-evidence"><strong>Evidence used</strong>${(d.evidence||[]).slice(0,5).map(e=>`<span>${escapeLwHtml(e.account)} · ${escapeLwHtml(e.merchant)} · ${e.risk}/100</span>`).join('')}</div></div>`:'<div class="lw-empty-sub">Copilot is unavailable.</div>';});
}

async function loadFraudSecurity() {
    const el=document.getElementById('lwFraudSecurityBody'); if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Checking FraudShield controls...</div>';
    const [data,ops]=await Promise.all([lwSafeJsonFetch('/lender/fraudshield/intelligence/security'),lwSafeJsonFetch('/lender/fraudshield/intelligence/operations')]);
    if(!data){el.innerHTML='<div class="lw-empty-sub">Security status unavailable.</div>';return;}
    const q=ops?.async_queue||{};
    el.innerHTML=`<div class="fw-security-grid">${(data.controls||[]).map(c=>`<div class="fw-security-card"><span class="fw-status-dot"></span><div><strong>${escapeLwHtml(c.name)}</strong><small>${escapeLwHtml(c.status)}</small><p>${escapeLwHtml(c.detail)}</p></div></div>`).join('')}<div class="fw-security-card"><span class="fw-status-dot"></span><div><strong>Durable async queue</strong><small>ACTIVE</small><p>${Number(q.QUEUED||0)} queued · ${Number(q.RETRY||0)} retry · ${Number(q.DEAD||0)} dead-letter</p></div></div><div class="fw-security-card"><span class="fw-status-dot"></span><div><strong>Operational audit</strong><small>ACTIVE</small><p>${Number(ops?.audit_events||0)} audit events · ${Number(ops?.replays_blocked||0)} replays blocked</p></div></div></div><div class="fw-security-note"><i class="fa-solid fa-shield-heart"></i><div><strong>FraudShield production posture</strong><p>Institution isolation, authorization, replay protection, rate limiting, security headers, structured request telemetry, durable retries and human-gated escalation are enforced server-side.</p></div></div>`;
}

/* Phase 2 — configurable institution fraud rules */
const fraudRuleHelp = {
    high_amount: ['High transaction amount', 'Amount ₹'],
    velocity: ['Transaction velocity', 'Txns / hour'],
    location: ['Location deviation', 'Distance km'],
    new_device: ['New device', 'Boolean trigger'],
    merchant_risk: ['Merchant risk', 'Risk 0–1'],
    new_merchant: ['New merchant', 'Boolean trigger'],
    unusual_time: ['Unusual transaction time', 'Hour ≤'],
    relationships: ['Related-account density', 'Relationships'],
    failed_auth: ['Failed authentication burst', 'Attempts'],
};

async function loadFraudRules() {
    const el=document.getElementById('lwFraudRulesBody');
    if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading detection policy...</div>';
    const data=await lwSafeJsonFetch('/lender/fraudshield/rules');
    if(!data){el.innerHTML='<div class="lw-empty-sub">Rule configuration is unavailable.</div>';return;}
    const rules=data.rules||{};
    el.innerHTML=`<div class="fw-rules-intro"><i class="fa-solid fa-circle-nodes"></i><div><strong>Hybrid policy engine</strong><p>Rules contribute 15% of the fused risk score. ML, behavior and network signals remain active, helping avoid single-rule false positives.</p></div></div>
    <div class="fw-rules-grid">${Object.entries(rules).map(([key,r])=>{
        const meta=fraudRuleHelp[key]||[r.label||key,'Threshold'];
        const booleanRule=['new_device','new_merchant'].includes(key);
        return `<div class="fw-rule-card" data-rule-key="${escapeLwHtml(key)}">
          <div class="fw-rule-head"><div><strong>${escapeLwHtml(meta[0])}</strong><small>${escapeLwHtml(r.label||meta[0])}</small></div><label class="fw-rule-toggle"><input type="checkbox" data-rule-enabled ${r.enabled?'checked':''}><span></span></label></div>
          <div class="fw-rule-controls">
            <label>${escapeLwHtml(meta[1])}<input data-rule-threshold type="number" ${key==='merchant_risk'?'step="0.05" min="0" max="1"':'step="1" min="0"'} value="${Number(r.threshold??0)}" ${booleanRule?'disabled':''}></label>
            <label>Risk points<input data-rule-points type="number" min="0" max="30" step="1" value="${Number(r.points??0)}"></label>
          </div>
          <div class="fw-rule-foot"><span>IF ${escapeLwHtml(meta[0].toLowerCase())}</span><b>+${Number(r.points||0)} pts</b></div>
        </div>`}).join('')}</div>
    <div class="fw-rules-note"><i class="fa-solid fa-user-shield"></i><span>Changes are scoped to this institution and are recorded in the FraudShield audit trail.</span></div>`;
    fraudAdvancedLoaded.add('fraud-rules');
}

async function saveFraudRules(){
    const btn=document.getElementById('lwFraudRulesSaveBtn'),status=document.getElementById('lwFraudRulesStatus');
    const rules={};
    document.querySelectorAll('#lwFraudRulesBody [data-rule-key]').forEach(card=>{
        rules[card.dataset.ruleKey]={
            enabled:!!card.querySelector('[data-rule-enabled]')?.checked,
            threshold:Number(card.querySelector('[data-rule-threshold]')?.value||0),
            points:Number(card.querySelector('[data-rule-points]')?.value||0)
        };
    });
    if(btn){btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Saving...';}
    if(status)status.textContent='Applying policy…';
    const data=await lwSafeJsonFetch('/lender/fraudshield/rules',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules})});
    if(btn){btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-floppy-disk"></i> Save & Re-score';}
    if(!data){if(status)status.textContent='Save failed';return;}
    fraudAdvancedLoaded.delete('fraud-rules');
    lwFraudBootstrap=null;
    if(status)status.textContent='Saved · scores refreshed';
    await loadFraudRules();
    await loadLwFraudMonitor();
}


/* Phase 2 — Fraud Autopilot autonomous response orchestration */
function fwActionLabel(action){return String(action||'').replaceAll('_',' ').replace(/\b\w/g,m=>m.toUpperCase());}
function fwActionClass(action){return String(action||'').includes('CRITICAL')?'critical':String(action||'').includes('HOLD')?'hold':String(action||'').includes('STEP')?'step':String(action||'').includes('MONITOR')?'monitor':'allow';}

async function loadFraudAutopilot(){
    const el=document.getElementById('lwFraudAutopilotBody'); if(!el)return;
    el.innerHTML='<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading response policy...</div>';
    const [policyData,eventData]=await Promise.all([
        lwSafeJsonFetch('/lender/fraudshield/response-policy'),
        lwSafeJsonFetch('/lender/fraudshield/autopilot/events')
    ]);
    if(!policyData||!eventData){el.innerHTML='<div class="lw-empty-sub">Fraud Autopilot is unavailable.</div>';return;}
    const p=policyData.policy||{}, events=eventData.events||[];
    el.innerHTML=`
      <div class="fw-autopilot-hero"><div><span class="fw-autopilot-kicker">AUTONOMOUS FRAUD RESPONSE</span><strong>Detect → Contain → Investigate → Preserve Evidence</strong><p>FraudShield can take internal protective action immediately while external reporting remains under analyst control.</p></div><div class="fw-autopilot-shield"><i class="fa-solid fa-shield-halved"></i><span>HUMAN-GATED<br>EXTERNAL REPORTING</span></div></div>
      <div class="fw-policy-ladder">
        <div class="fw-policy-step allow"><span>0–${Math.max(0,Number(p.monitor_from||40)-1)}</span><strong>Allow</strong><small>No intervention</small></div>
        <div class="fw-policy-step monitor"><span>${p.monitor_from}–${Math.max(Number(p.monitor_from),Number(p.step_up_from)-1)}</span><strong>Monitor</strong><small>Watch activity</small></div>
        <div class="fw-policy-step step"><span>${p.step_up_from}–${Math.max(Number(p.step_up_from),Number(p.hold_from)-1)}</span><strong>Step-up</strong><small>OTP / verification</small></div>
        <div class="fw-policy-step hold"><span>${p.hold_from}–${Math.max(Number(p.hold_from),Number(p.critical_from)-1)}</span><strong>Hold + Case</strong><small>Contain & investigate</small></div>
        <div class="fw-policy-step critical"><span>${p.critical_from}–100</span><strong>Critical</strong><small>Hold + SOC alert</small></div>
      </div>
      <div class="fw-autopilot-config">
        <div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Response thresholds</div><div class="fw-autopilot-fields">
          <label>Monitor from<input id="fwApMonitor" type="number" min="0" max="100" value="${Number(p.monitor_from||40)}"></label>
          <label>Step-up from<input id="fwApStep" type="number" min="0" max="100" value="${Number(p.step_up_from||70)}"></label>
          <label>Hold from<input id="fwApHold" type="number" min="0" max="100" value="${Number(p.hold_from||85)}"></label>
          <label>Critical from<input id="fwApCritical" type="number" min="0" max="100" value="${Number(p.critical_from||95)}"></label>
          <label>Analyst SLA (min)<input id="fwApSla" type="number" min="1" max="1440" value="${Number(p.analyst_sla_minutes||5)}"></label>
        </div></div>
        <div class="lw-fraud-analytics-card"><div class="lw-fraud-card-title">Automated controls</div><label class="fw-autopilot-check"><input id="fwApCase" type="checkbox" ${p.auto_create_case?'checked':''}><span><strong>Auto-create investigation</strong><small>Open a case automatically when the hold threshold is crossed.</small></span></label><label class="fw-autopilot-check"><input id="fwApNotify" type="checkbox" ${p.auto_notify_soc?'checked':''}><span><strong>Notify internal SOC</strong><small>Uses FRAUDSHIELD_SOC_WEBHOOK_URL when configured; otherwise recorded as a demo queue event.</small></span></label><div class="fw-ncrp-box"><i class="fa-solid fa-landmark"></i><div><strong>External cybercrime escalation</strong><p>Prepared evidence can be reviewed by an analyst before opening India's official National Cyber Crime Reporting Portal. Financial-fraud helpline: <b>1930</b>.</p><a class="lw-btn" href="https://cybercrime.gov.in/" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square"></i> Open official portal</a></div></div></div>
      </div>
      <div class="fw-autopilot-events"><div class="lw-fraud-card-title">Autonomous Response Timeline</div>${events.length?`<div class="lw-blotter-wrap"><table class="lw-table"><thead><tr><th>Action</th><th>Account</th><th>Merchant</th><th>Amount</th><th>Risk</th><th>Case</th><th>SOC</th><th>Evidence</th></tr></thead><tbody>${events.map(e=>`<tr><td><span class="fw-ap-pill ${fwActionClass(e.action)}">${escapeLwHtml(fwActionLabel(e.action))}</span></td><td>${escapeLwHtml(e.account_ref||'—')}</td><td>${escapeLwHtml(e.merchant||'—')}</td><td>${lwFraudFormatCurrency(e.amount)}</td><td><strong>${e.risk_score}</strong>/100</td><td>${escapeLwHtml(e.case_number||'—')}</td><td>${escapeLwHtml(e.notification_status||'—')}</td><td><a class="lw-btn" href="/lender/fraudshield/transactions/${e.transaction_id}/evidence" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-file-lines"></i> Report</a></td></tr>`).join('')}</tbody></table></div>`:'<div class="lw-empty-sub">No response events yet. Click “Scan Current Alerts” or inject a high-risk live transaction.</div>'}</div>`;
    document.getElementById('lwFraudAutopilotSaveBtn')?.addEventListener('click',saveFraudAutopilot);
    document.getElementById('lwFraudAutopilotScanBtn')?.addEventListener('click',scanFraudAutopilot);
    fraudAdvancedLoaded.add('fraud-autopilot');
}

async function saveFraudAutopilot(){
    const btn=document.getElementById('lwFraudAutopilotSaveBtn'), status=document.getElementById('lwFraudAutopilotStatus');
    const policy={monitor_from:Number(document.getElementById('fwApMonitor')?.value||40),step_up_from:Number(document.getElementById('fwApStep')?.value||70),hold_from:Number(document.getElementById('fwApHold')?.value||85),critical_from:Number(document.getElementById('fwApCritical')?.value||95),analyst_sla_minutes:Number(document.getElementById('fwApSla')?.value||5),auto_create_case:!!document.getElementById('fwApCase')?.checked,auto_notify_soc:!!document.getElementById('fwApNotify')?.checked};
    if(btn){btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Saving...';} if(status)status.textContent='Applying response policy…';
    const data=await lwSafeJsonFetch('/lender/fraudshield/response-policy',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({policy})});
    if(btn){btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-floppy-disk"></i> Save Policy';}
    if(!data){if(status)status.textContent='Invalid policy';return;} if(status)status.textContent='Policy saved'; fraudAdvancedLoaded.delete('fraud-autopilot'); await loadFraudAutopilot();
}

async function scanFraudAutopilot(){
    const btn=document.getElementById('lwFraudAutopilotScanBtn'),status=document.getElementById('lwFraudAutopilotStatus');
    if(btn){btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Scanning...';} if(status)status.textContent='Evaluating alerts…';
    const data=await lwSafeJsonFetch('/lender/fraudshield/autopilot/scan',{method:'POST'});
    if(btn){btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-crosshairs"></i> Scan Current Alerts';}
    if(!data){if(status)status.textContent='Scan failed';return;} if(status)status.textContent=`${data.actioned||0} events actioned`; fraudAdvancedLoaded.delete('fraud-autopilot'); lwFraudBootstrap=null; await loadFraudAutopilot(); await loadLwFraudMonitor();
}
