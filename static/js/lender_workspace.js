'use strict';

/* =================================================================
   LENDER WORKSPACE — CREDIT RISK INTELLIGENCE (Phase 1)

   Frontend-only controller. Every value rendered here comes from the
   existing endpoints in routes/credit_risk.py:

       POST /api/credit-risk/assess
       POST /api/credit-risk/explain
       POST /api/credit-risk/anomaly
       POST /api/credit-risk/affordability
       GET  /api/credit-risk/financial-behavior
       POST /api/credit-risk/scenario
       GET  /api/credit-risk/responsible-ai

   No thresholds, decisions, SHAP values, anomaly scores, or fairness
   numbers are computed in this file — it only formats what the API
   returns. The backend decision (APPROVE / MANUAL REVIEW / REJECT)
   is always shown verbatim.
================================================================== */

let lwInitialized = false;
let lwSubmitInFlight = false;

let lwCurrentApplicant = null;   // baseline payload used for the last successful assessment
let lwAssessData = null;
let lwExplainData = null;
let lwAnomalyData = null;
let lwAffordabilityData = null;
let lwBehaviorData = null;

// Exact field schema expected by /api/credit-risk/assess.
// NOTE: the Phase 1 brief describes this as a "19-field schema", but the
// actual production schema (predict.py::get_example_applicant /
// scenario.py::ALL_FEATURES) has 20 fields. This list matches the real
// schema found in the code, not the brief's stated count.
const LW_FIELD_IDS = [
    'lw_checking_account', 'lw_duration_months', 'lw_credit_history', 'lw_purpose',
    'lw_credit_amount', 'lw_savings_account', 'lw_employment_since', 'lw_installment_rate',
    'lw_personal_status_sex', 'lw_other_debtors', 'lw_residence_since', 'lw_property',
    'lw_age', 'lw_other_installment_plans', 'lw_housing', 'lw_existing_credits',
    'lw_job', 'lw_dependents', 'lw_telephone', 'lw_foreign_worker'
];

const LW_NUMERIC_IDS = new Set([
    'lw_duration_months', 'lw_credit_amount', 'lw_installment_rate', 'lw_existing_credits',
    'lw_residence_since', 'lw_age', 'lw_dependents'
]);

// Application-level record metadata (name, reference IDs, borrower link).
// These are DISPLAY/RECORD fields only — never part of LW_FIELD_IDS, so
// gatherLwFormData() never includes them in the /assess model payload.
const LW_META_IDS = [
    'lw_meta_applicant_name', 'lw_meta_application_id', 'lw_meta_application_date', 'lw_meta_borrower_id'
];

let lwCurrentMeta = null; // { applicantName, applicationId, applicationDate, borrowerId }

function gatherLwMetaData() {
    const val = (id) => {
        const el = document.getElementById(id);
        return el ? el.value.trim() : '';
    };
    const applicationId = val('lw_meta_application_id') || `APP-${Date.now().toString(36).toUpperCase()}`;
    return {
        applicantName: val('lw_meta_applicant_name') || null,
        applicationId,
        applicationDate: val('lw_meta_application_date') || null,
        borrowerId: val('lw_meta_borrower_id') || null
    };
}

/* -----------------------------------------------------------------
   INIT
----------------------------------------------------------------- */
function initLenderWorkspace() {
    if (lwInitialized) return;
    const form = document.getElementById('lenderAssessmentForm');
    if (!form) return;
    lwInitialized = true;

    initLwNav();
    initLwConfirmModal();
    initLwRiskAssistant();
    form.addEventListener('submit', handleLwSubmit);

    const resetBtn = document.getElementById('lwResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', handleLwReset);

    const backBtn = document.getElementById('lwBackToQueueBtn');
    if (backBtn) backBtn.addEventListener('click', () => lwSwitchTab('queue'));

    loadLwResponsibleAi();
    loadLenderQueue();
}

function initLwNav() {
    const items = document.querySelectorAll('.lw-nav-item');
    items.forEach(btn => {
        btn.addEventListener('click', (e) => {
            items.forEach(b => b.classList.remove('active'));
            e.currentTarget.classList.add('active');

            const target = e.currentTarget.getAttribute('data-section');
            document.querySelectorAll('.lw-section').forEach(sec => {
                sec.classList.toggle('lw-section-active', sec.getAttribute('data-section') === target);
            });

            // Switching tabs must never lose the selected application or
            // reset its assessment — re-render from currentApplication.
            lwRenderTabForCurrentApplication(target);
        });
    });
}

function lwSwitchTab(sectionId) {
    const tab = document.querySelector(`.lw-nav-item[data-section="${sectionId}"]`);
    if (tab) tab.click();
}

/* -----------------------------------------------------------------
   UTILITIES
----------------------------------------------------------------- */
async function lwRequest(url, options) {
    if (typeof authFetch === 'function') return authFetch(url, options);
    return fetch(url, Object.assign({ credentials: 'same-origin' }, options));
}

async function lwSafeJsonFetch(url, options) {
    try {
        const res = await lwRequest(url, options);
        if (!res.ok) return null;
        const data = await res.json();
        return data && data.status === 'success' ? data : null;
    } catch (e) {
        return null;
    }
}

function lwApiFieldName(inputId) { return inputId.replace(/^lw_/, ''); }

function lwLabelFor(inputId) {
    const label = document.querySelector(`label[for="${inputId}"]`);
    return label ? label.textContent.replace(/\s+/g, ' ').trim() : inputId;
}

function escapeLwHtml(str) {
    const div = document.createElement('div');
    div.textContent = str === null || str === undefined ? '' : String(str);
    return div.innerHTML;
}

function lwRiskClass(riskLevel) {
    const n = String(riskLevel || '').toLowerCase();
    if (n.includes('low')) return 'lw-risk-low';
    if (n.includes('high')) return 'lw-risk-high';
    return 'lw-risk-medium';
}

function lwDecisionClass(decision) {
    const n = String(decision || '').toLowerCase();
    if (n.includes('approve')) return 'lw-decision-approve';
    if (n.includes('reject')) return 'lw-decision-reject';
    return 'lw-decision-review';
}

// Renders a value, or an explicit "Unavailable" state — never a fabricated zero.
function lwVal(v, opts) {
    opts = opts || {};
    if (v === null || v === undefined || v === '') {
        return `<span class="lw-stat-value na">${opts.emptyLabel || 'Unavailable'}</span>`;
    }
    const prefix = opts.prefix || '';
    const suffix = opts.suffix || '';
    return `<span class="lw-stat-value ${opts.stateClass || ''}">${prefix}${escapeLwHtml(v)}${suffix}</span>`;
}

/* -----------------------------------------------------------------
   PENDING APPLICATION QUEUE (PHASE 2)

   Reads only from GET /lender/applications and
   GET /lender/applications/<id>. No risk score, decision, SHAP,
   anomaly, scenario, or fairness data is requested or rendered here —
   that stays out of scope until a later phase.
----------------------------------------------------------------- */
let lwQueueApplications = [];
let lwDetailApplicationId = null;   // which application is currently open in the detail view
let lwDetailAssessInFlight = false;

/* -----------------------------------------------------------------
   SINGLE APPLICATION CONTEXT (PHASE 3.5)

   currentApplication is the one canonical record of "which application
   is the lender currently looking at". It is set only when an
   application is opened from the queue, and is REPLACED wholesale
   (never merged) whenever a different application is opened. Every
   tab that renders "the current application" (Assessment Overview,
   etc.) reads from this object rather than keeping its own copy of
   application state, so switching tabs never loses or mixes up which
   application is being reviewed.
----------------------------------------------------------------- */
let currentApplication = null;

// PHASE 4 — Decision Explanation is not persisted; it's loaded on demand
// for whichever application is selected, and cached only in-memory on
// currentApplication.explanation. Because lwSetCurrentApplication always
// replaces currentApplication wholesale, opening a different application
// automatically drops the previous one's cached explanation.
let lwExplainInFlight = false;

// PHASE 6 — Application Anomaly is not persisted; it's loaded on demand
// for whichever application is selected, and cached only in-memory on
// currentApplication.anomaly. Because lwSetCurrentApplication always
// replaces currentApplication wholesale, opening a different application
// automatically drops the previous one's cached anomaly result.
let lwAnomalyInFlight = false;

function lwSetCurrentApplication(app) {
    // PHASE 9 — the Risk Analyst panel is scoped to one application's
    // evidence. Any time currentApplication is about to be replaced
    // (a different application opened, or cleared entirely), close the
    // panel and drop its history first so a stale answer can never be
    // shown against the newly selected application.
    lwCloseRiskAssistant();
    lwRaMessages = [];
    lwRaMessagesAppId = null;

    currentApplication = app ? {
        application_id: app.application_id,
        borrower: app.borrower || null,
        fields: app.fields || [],
        loan_amount: app.loan_amount,
        tenure_months: app.tenure_months,
        purpose: app.purpose,
        submitted_at: app.submitted_at,
        status: app.status,
        assessment_result: app.assessment_result || null,
        assessed_at: app.assessed_at || null,
        scenarioBaseline: app.scenario_baseline || null
    } : null;
    lwUpdateContextStrip();
    // The decision dock is persistent (outside the tab sections), so it
    // is driven from the one place currentApplication is ever replaced
    // wholesale — never bled over from a previously selected application.
    renderLwFinalDecisionBlock();
}

// Re-renders whichever tab was just switched to, using currentApplication.
// Tabs not listed here (Responsible AI) aren't per-application and
// aren't persisted, so switching to them intentionally leaves their
// existing content untouched rather than clearing anything.
function lwRenderTabForCurrentApplication(sectionId) {
    if (sectionId === 'assessment') {
        renderLwAssessmentOverview();
    } else if (sectionId === 'affordability') {
        renderLwAffordability();
    } else if (sectionId === 'evidence') {
        renderLwBehavior();
    } else if (sectionId === 'explain') {
        renderLwExplainability();
    } else if (sectionId === 'anomaly') {
        renderLwAnomaly();
    } else if (sectionId === 'scenario') {
        renderLwScenario();
    }
}

function lwFormatCurrency(amount) {
    if (amount === null || amount === undefined || amount === '') return '—';
    const n = Number(amount);
    if (Number.isNaN(n)) return escapeLwHtml(amount);
    return '₹' + n.toLocaleString('en-IN');
}

// credit_amount / loan_amount / requested_amount / estimated installment
// are native model units — never format them as INR.
function lwFormatModelUnits(amount) {
    if (amount === null || amount === undefined || amount === '') return '—';
    const n = Number(amount);
    const core = Number.isNaN(n) ? String(amount) : n.toLocaleString('en-IN');
    return core + ' model units';
}

function lwUpdateContextStrip() {
    const strip = document.getElementById('lwContextStrip');
    if (!strip) return;

    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };

    const raBtn = document.getElementById('lwAskRiskAnalystBtn');

    if (!currentApplication) {
        strip.classList.remove('active');
        setText('lwContextApplicant', '—');
        setText('lwContextReference', '—');
        setText('lwContextPurpose', '—');
        setText('lwContextAmount', '—');
        setText('lwContextStatus', '—');
        if (raBtn) raBtn.style.display = 'none';
        return;
    }

    const name = currentApplication.borrower && currentApplication.borrower.name;
    setText('lwContextApplicant', name || '—');
    setText('lwContextReference', currentApplication.application_id != null
        ? '#' + currentApplication.application_id
        : '—');
    setText('lwContextPurpose', currentApplication.purpose || '—');
    setText('lwContextAmount', lwFormatModelUnits(currentApplication.loan_amount));
    setText('lwContextStatus', currentApplication.status || '—');
    strip.classList.add('active');

    // PHASE 9 — the trigger only ever appears once this application has
    // a PERSISTED assessment_result (POST .../assess). No persisted
    // assessment means the risk-assistant endpoint would just 422, so
    // the button stays hidden rather than surfacing a dead end.
    if (raBtn) raBtn.style.display = currentApplication.assessment_result ? '' : 'none';
}

function lwRiskGaugeHtml(pct, pctLabel, riskClass, riskLevel) {
    const r = 42;
    const circ = 2 * Math.PI * r;
    const clamped = Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0;
    const offset = circ * (1 - clamped / 100);
    return `
        <div class="lw-risk-gauge-wrap">
            <svg class="lw-risk-gauge" viewBox="0 0 100 100" aria-hidden="true">
                <circle class="lw-risk-gauge-track" cx="50" cy="50" r="${r}"></circle>
                <circle class="lw-risk-gauge-fill ${riskClass}" cx="50" cy="50" r="${r}"
                    stroke-dasharray="${circ.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"></circle>
            </svg>
            <div class="lw-risk-gauge-center">
                <div class="lw-risk-probability">${escapeLwHtml(pctLabel)}</div>
                <div class="lw-risk-level ${riskClass}">${escapeLwHtml(riskLevel || '')}</div>
            </div>
        </div>`;
}

function lwSnapshotRowHtml(section, name, stateText, stateClass) {
    return `
        <button type="button" class="lw-snapshot-row" onclick="lwSwitchTab('${section}')">
            <span class="lw-snapshot-name">${escapeLwHtml(name)}</span>
            <span class="lw-snapshot-state ${stateClass || ''}">${escapeLwHtml(stateText)}</span>
        </button>`;
}

function lwCashflowBarHtml(cap, aff) {
    cap = cap || {};
    aff = aff || {};
    const income = Number(cap.monthly_income);
    const expenses = Number(cap.monthly_expenses);
    const surplus = Number(aff.available_surplus);
    if (!Number.isFinite(income) || income <= 0) return '';

    const oblPct = Number.isFinite(expenses) ? Math.max(0, Math.min(100, (expenses / income) * 100)) : 0;
    const rawSurPlusPct = Number.isFinite(surplus) ? (Math.abs(surplus) / income) * 100 : 0;
    const surPct = Math.max(0, Math.min(100 - oblPct, rawSurPlusPct));
    const deficit = Number.isFinite(surplus) && surplus < 0;

    return `
        <div class="lw-cashflow-bar">
            <div class="lw-cashflow-bar-track">
                <div class="lw-cashflow-bar-segment lw-cashflow-obligations" style="width:${oblPct}%;"></div>
                <div class="lw-cashflow-bar-segment lw-cashflow-surplus${deficit ? ' state-bad' : ''}" style="width:${surPct}%;"></div>
            </div>
            <div class="lw-cashflow-bar-legend">
                <span><i class="lw-legend-dot lw-legend-obligations"></i> Existing obligations</span>
                <span><i class="lw-legend-dot lw-legend-surplus${deficit ? ' state-bad' : ''}"></i> ${deficit ? 'Deficit' : 'Surplus'}</span>
            </div>
        </div>`;
}

function lwFormatDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return escapeLwHtml(iso);
    return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

function lwStatusBadgeClass(status) {
    switch (String(status || '').toUpperCase()) {
        case 'APPROVED': return 'state-good';
        case 'REJECTED': return 'state-bad';
        case 'WITHDRAWN': return 'state-warn';
        default: return '';
    }
}

async function loadLenderQueue() {
    const body = document.getElementById('lwQueueBody');
    if (!body) return;
    body.innerHTML = '<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading applications...</div>';

    const data = await lwSafeJsonFetch('/lender/applications');
    if (!data) {
        body.innerHTML = '<div class="lw-empty-sub" style="text-align:center; padding:24px;">Could not load your applications. Please refresh.</div>';
        return;
    }

    lwQueueApplications = Array.isArray(data.applications) ? data.applications : [];
    renderLenderQueue();
}

function renderLenderQueue() {
    const body = document.getElementById('lwQueueBody');
    if (!body) return;

    if (lwQueueApplications.length === 0) {
        body.innerHTML = `
            <div class="lw-empty">
                <i class="fa-solid fa-inbox"></i>
                <div class="lw-empty-title">No pending applications</div>
                <div class="lw-empty-sub">Borrower applications assigned to you will appear here.</div>
            </div>`;
        return;
    }

    const rows = lwQueueApplications.map(app => `
        <tr>
            <td>${escapeLwHtml(app.applicant_name || 'Unknown')}</td>
            <td>#${escapeLwHtml(app.application_id)}</td>
            <td>${escapeLwHtml(lwFormatModelUnits(app.requested_amount))}</td>
            <td>${escapeLwHtml(app.purpose || '—')}</td>
            <td>${lwFormatDate(app.submitted_at)}</td>
            <td><span class="lw-badge ${lwStatusBadgeClass(app.status)}">${escapeLwHtml(app.status)}</span></td>
            <td>${app.assessed ? `<span class="lw-badge ${lwDecisionClass(app.decision)}">${escapeLwHtml(app.decision || 'Assessed')}</span>` : `<span class="lw-badge">Not assessed</span>`}</td>
            <td><button type="button" class="lw-btn lw-btn-primary lw-review-btn" data-app-id="${escapeLwHtml(app.application_id)}"><i class="fa-solid fa-magnifying-glass"></i> Review Application</button></td>
        </tr>
    `).join('');

    body.innerHTML = `
        <div class="lw-blotter-wrap">
        <table class="lw-table">
            <thead>
                <tr>
                    <th>Applicant</th>
                    <th>Application ID</th>
                    <th>Requested Amount</th>
                    <th>Purpose</th>
                    <th>Submitted</th>
                    <th>Status</th>
                    <th>AI Assessment</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
        </div>
    `;

    body.querySelectorAll('.lw-review-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            openLenderApplicationDetail(e.currentTarget.getAttribute('data-app-id'));
        });
    });
}

function lwShowDetailSection() {
    document.querySelectorAll('.lw-section').forEach(sec => sec.classList.remove('lw-section-active'));
    const detailSection = document.querySelector('.lw-section[data-section="detail"]');
    if (detailSection) detailSection.classList.add('lw-section-active');

    document.querySelectorAll('.lw-nav-item').forEach(b => b.classList.remove('active'));
    const queueTab = document.querySelector('.lw-nav-item[data-section="queue"]');
    if (queueTab) queueTab.classList.add('active');
}

async function openLenderApplicationDetail(applicationId) {
    lwShowDetailSection();

    // New application opened: any in-flight assessment for a previous
    // application is now stale and must not be rendered if it resolves
    // after this point (checked in runLenderAssessment).
    lwDetailApplicationId = applicationId;
    lwDetailAssessInFlight = false;

    const body = document.getElementById('lwDetailBody');
    if (body) body.innerHTML = '<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading application...</div>';

    const data = await lwSafeJsonFetch(`/lender/applications/${encodeURIComponent(applicationId)}`);

    // The lender may have opened a different application while this
    // request was in flight — never let a stale application land here
    // or overwrite the newer currentApplication.
    if (String(lwDetailApplicationId) !== String(applicationId)) return;

    if (!data || !data.application) {
        if (body) body.innerHTML = '<div class="lw-empty-sub" style="text-align:center; padding:24px;">Could not load this application. It may not belong to your queue.</div>';
        return;
    }

    // Opening application A (or reopening it later) replaces
    // currentApplication completely, including any persisted assessment.
    lwSetCurrentApplication(data.application);
    renderLenderApplicationDetail(data.application);
}

function renderLenderApplicationDetail(app) {
    const body = document.getElementById('lwDetailBody');
    if (!body) return;

    const borrowerName = app.borrower && app.borrower.name;
    const fieldRows = (app.fields || []).map(f => `
        <tr><td>${escapeLwHtml(f.label)}</td><td>${escapeLwHtml(f.value)}</td></tr>
    `).join('');

    body.innerHTML = `
        <div class="lw-disclaimer">
            <i class="fa-solid fa-circle-info"></i>
            <span>Review the submitted application and use the underwriting instruments before recording the final
                lending decision.</span>
        </div>
        <div class="lw-stat-row" style="margin-bottom:18px;">
            <div class="lw-stat-card"><div class="lw-stat-label">Applicant</div>${lwVal(borrowerName)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Application ID</div>${lwVal('#' + app.application_id)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Submitted</div>${lwVal(lwFormatDate(app.submitted_at))}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Loan Amount</div>${lwVal(lwFormatModelUnits(app.loan_amount))}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Tenure</div>${lwVal(app.tenure_months, { suffix: ' months' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Purpose</div>${lwVal(app.purpose)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Status</div><span id="lwDetailStatusBadge" class="lw-badge ${lwStatusBadgeClass(app.status)}">${escapeLwHtml(app.status)}</span></div>
        </div>
        <div class="lw-panel-title" style="margin-bottom:10px;"><i class="fa-solid fa-list"></i> Submitted
            Application Fields</div>
        <table class="lw-table">
            <thead><tr><th>Field</th><th>Value</th></tr></thead>
            <tbody>${fieldRows}</tbody>
        </table>

        <div class="lw-panel" style="margin-top:18px;">
            <div id="lwAssessBlock"></div>
        </div>
    `;

    // Restore the persisted assessment on reopen, if one exists — do
    // NOT rerun the model just because the lender left and came back.
    // The "Re-run Assessment" button inside the result state remains
    // available for an explicit rerun.
    if (app.assessment_result) {
        setLwAssessBlock(lwAssessmentBlockHtml(app.assessment_result, 'result'), app.application_id);
    } else {
        setLwAssessBlock(lwAssessmentBlockHtml(null, 'idle'), app.application_id);
    }
}

/* -----------------------------------------------------------------
   AI CREDIT ASSESSMENT (PHASE 3)

   Reuses the application's own stored application_data as the model
   payload — the lender never re-enters fields here. Calls
   POST /lender/applications/<id>/assess and renders exactly what the
   backend returns (risk_probability / risk_percentage, risk_level,
   decision) with no recalculation in JS.
----------------------------------------------------------------- */
function setLwAssessBlock(html, applicationId) {
    const block = document.getElementById('lwAssessBlock');
    if (!block) return;
    block.innerHTML = html;
    const btn = document.getElementById('lwRunAssessBtn');
    if (btn) btn.addEventListener('click', () => runLenderAssessment(applicationId));
}

function lwAssessmentBlockHtml(assessment, state, errorMessage) {
    if (state === 'loading') {
        return `
            <div class="lw-panel-header">
                <div class="lw-panel-title"><i class="fa-solid fa-shield-halved"></i> AI Credit Assessment</div>
            </div>
            <div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Assessing application...</div>
        `;
    }

    if (state === 'error') {
        return `
            <div class="lw-panel-header">
                <div class="lw-panel-title"><i class="fa-solid fa-shield-halved"></i> AI Credit Assessment</div>
            </div>
            <div class="lw-error-list">
                <div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>${escapeLwHtml(errorMessage || 'Could not assess this application.')}</span></div>
            </div>
            <div class="lw-form-actions">
                <button type="button" class="lw-btn lw-btn-primary" id="lwRunAssessBtn"><i class="fa-solid fa-rotate"></i> Retry Assessment</button>
            </div>
        `;
    }

    if (state === 'result' && assessment) {
        const riskClass = lwRiskClass(assessment.risk_level);
        const decisionClass = lwDecisionClass(assessment.decision);
        const pct = typeof assessment.risk_percentage === 'number'
            ? assessment.risk_percentage
            : (typeof assessment.risk_probability === 'number' ? assessment.risk_probability * 100 : NaN);
        const pctLabel = Number.isFinite(pct) ? pct.toFixed(1) + '%' : 'Unavailable';

        return `
            <div class="lw-panel-header">
                <div class="lw-panel-title"><i class="fa-solid fa-shield-halved"></i> AI Credit Assessment</div>
                <div class="lw-panel-note">POST /lender/applications/&lt;id&gt;/assess</div>
            </div>
            <div class="lw-overview-top">
                <div class="lw-risk-block">
                    <div class="lw-risk-icon ${riskClass}"><i class="fa-solid fa-shield-halved"></i></div>
                    <div>
                        <div class="lw-risk-probability">${pctLabel}</div>
                        <div class="lw-risk-level ${riskClass}">${escapeLwHtml(assessment.risk_level)}</div>
                    </div>
                </div>
                <div class="lw-decision-badge ${decisionClass}">${escapeLwHtml(assessment.decision)}</div>
            </div>
            <div class="lw-probability-track"><div class="lw-probability-fill ${riskClass}" style="width:${Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0}%;"></div></div>
            <div class="lw-form-actions">
                <button type="button" class="lw-btn" id="lwRunAssessBtn"><i class="fa-solid fa-rotate"></i> Re-run Assessment</button>
            </div>
        `;
    }

    // idle — not yet run for this application
    return `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-shield-halved"></i> AI Credit Assessment</div>
        </div>
        <div class="lw-empty-sub" style="text-align:center; padding:18px;">AI assessment has not been run for this
            application.</div>
        <div class="lw-form-actions">
            <button type="button" class="lw-btn lw-btn-primary" id="lwRunAssessBtn"><i
                    class="fa-solid fa-shield-halved"></i> Run AI Credit Assessment</button>
        </div>
    `;
}

async function runLenderAssessment(applicationId) {
    if (lwDetailAssessInFlight) return;
    lwDetailAssessInFlight = true;
    setLwAssessBlock(lwAssessmentBlockHtml(null, 'loading'), applicationId);

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/assess`, {
            method: 'POST'
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale result land here.
        if (String(lwDetailApplicationId) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            const message = (data && data.message) || 'Could not assess this application. Please try again.';
            setLwAssessBlock(lwAssessmentBlockHtml(null, 'error', message), applicationId);
            return;
        }

        setLwAssessBlock(lwAssessmentBlockHtml(data, 'result'), applicationId);

        // Keep currentApplication's persisted assessment in sync so the
        // Assessment Overview tab (and reopening this application later)
        // reflects the just-run result without a page reload. Guarded by
        // the same staleness check above, so a slow response for an
        // application the lender has since navigated away from can never
        // land on the wrong currentApplication.
        if (currentApplication && String(currentApplication.application_id) === String(applicationId)) {
            currentApplication.assessment_result = {
                risk_probability: data.risk_probability,
                risk_percentage: data.risk_percentage,
                risk_level: data.risk_level,
                decision: data.decision
            };
            currentApplication.assessed_at = data.assessed_at || currentApplication.assessed_at;
            // A re-run assessment can change the underlying decision, so
            // any previously cached explanation is now stale — drop it
            // and let the Decision Explanation tab reload it fresh.
            currentApplication.explanation = null;
            // The Final Lending Decision panel shows the AI Recommendation
            // read from currentApplication.assessment_result — refresh it
            // so a re-run's new decision doesn't sit next to a stale one.
            renderLwFinalDecisionBlock();
        }
    } catch (networkErr) {
        if (String(lwDetailApplicationId) === String(applicationId)) {
            setLwAssessBlock(lwAssessmentBlockHtml(null, 'error', 'Could not reach the server. Please try again.'), applicationId);
        }
    } finally {
        lwDetailAssessInFlight = false;
    }
}

/* -----------------------------------------------------------------
   FINAL UNDERWRITING SUMMARY (PHASE 3 FINAL)

   A compact synthesis of whatever underwriting evidence is ALREADY
   cached on currentApplication, shown directly above the Final
   Lending Decision controls so the lender doesn't need to remember
   results from several tabs. This reads existing cached fields only —
   the exact same objects the individual tabs already render from —
   and never triggers a fetch of its own:

       app.assessment_result   -> risk_percentage / risk_level / decision
       app.repaymentCapacity   -> affordability.status
       app.borrowerEvidence    -> data_coverage.history_months
       app.anomaly             -> anomaly_level / available
       app.explanation         -> risk_increasing_factors / risk_reducing_factors

   Any instrument not yet loaded for this application renders as
   "Not reviewed" (never fetched) rather than being fetched here.
----------------------------------------------------------------- */
function lwFinalSummaryRowHtml(key, valueHtml) {
    return `<div class="lw-summary-row"><span class="lw-summary-key">${escapeLwHtml(key)}</span><span class="lw-summary-val">${valueHtml}</span></div>`;
}

function lwFinalUnderwritingSummaryHtml(app) {
    if (!app) return '';

    const naHtml = `<span class="lw-stat-value na">Not reviewed</span>`;
    const unavailableHtml = `<span class="lw-stat-value na">Unavailable</span>`;

    // RISK + AI RECOMMENDATION — from the same assessment_result already
    // used by the Assessment Overview tab and the decision dock above.
    let riskHtml = naHtml;
    let aiRecHtml = naHtml;
    const assessment = app.assessment_result;
    if (assessment) {
        const riskClass = lwRiskClass(assessment.risk_level);
        const pct = typeof assessment.risk_percentage === 'number'
            ? assessment.risk_percentage
            : (typeof assessment.risk_probability === 'number' ? assessment.risk_probability * 100 : null);
        riskHtml = `<span class="lw-risk-level ${riskClass}">${pct !== null ? pct.toFixed(1) + '% · ' : ''}${escapeLwHtml(assessment.risk_level)}</span>`;
        aiRecHtml = assessment.decision
            ? `<span class="lw-decision-badge ${lwDecisionClass(assessment.decision)}">${escapeLwHtml(assessment.decision)}</span>`
            : unavailableHtml;
    }

    // REPAYMENT CAPACITY — same affordability.status field/classing as
    // renderLwAffordabilityFull.
    let repaymentHtml = naHtml;
    if (app.repaymentCapacity) {
        const aff = app.repaymentCapacity.affordability || {};
        if (aff.status) {
            const statusClass = aff.status === 'affordable' ? 'state-good'
                : (aff.status === 'strained' ? 'state-warn'
                    : (aff.status === 'insufficient_data' ? '' : 'state-bad'));
            repaymentHtml = `<span class="lw-badge ${statusClass}">${escapeLwHtml(aff.status.replace('_', ' '))}</span>`;
        } else {
            repaymentHtml = unavailableHtml;
        }
    }

    // BORROWER EVIDENCE — same data_coverage.history_months field already
    // shown as "History Available" on the Borrower Evidence tab.
    let evidenceHtml = naHtml;
    if (app.borrowerEvidence) {
        const months = app.borrowerEvidence.data_coverage ? app.borrowerEvidence.data_coverage.history_months : null;
        evidenceHtml = (months !== null && months !== undefined)
            ? `<span class="lw-summary-val">${escapeLwHtml(months)} month(s) available</span>`
            : unavailableHtml;
    }

    // APPLICATION ANOMALY — same anomaly_level + severity classing as
    // renderLwAnomalyFull. Anomaly is never labeled as fraud here.
    let anomalyHtml = naHtml;
    if (app.anomaly) {
        if (app.anomaly.available === false || !app.anomaly.anomaly_level) {
            anomalyHtml = unavailableHtml;
        } else {
            const level = String(app.anomaly.anomaly_level).toLowerCase();
            const cls = level.includes('high') ? 'state-bad' : (level.includes('medium') || level.includes('low anomaly') ? 'state-warn' : 'state-good');
            anomalyHtml = `<span class="lw-badge ${cls}">${escapeLwHtml(app.anomaly.anomaly_level)}</span>`;
        }
    }

    // TOP RISK DRIVER — same risk_increasing_factors[0] and raw impact
    // value already used by the Decision Explanation tab. Only the top
    // risk-increasing factor is shown here; the full increasing/reducing
    // breakdown belongs on the Decision Explanation (SHAP) tab, not this
    // briefing strip. No causal claim is made; impact is shown as-is.
    let topDriverHtml = naHtml;
    if (app.explanation && app.explanation.explanation_available !== false) {
        const top = (app.explanation.risk_increasing_factors || [])[0];
        topDriverHtml = top
            ? `<span class="lw-summary-val">${escapeLwHtml(top.feature)}${typeof top.impact === 'number' ? ` <span class="lw-context-muted">+${top.impact.toFixed(3)}</span>` : ''}</span>`
            : unavailableHtml;
    }

    return `
        <div class="lw-final-summary" style="margin-bottom:10px;">
            <div class="lw-panel-title" style="margin:0 0 6px 0; font-size:0.78rem; letter-spacing:0.04em;">
                <i class="fa-solid fa-list-check"></i> Final Underwriting Summary
            </div>
            ${lwFinalSummaryRowHtml('Risk', riskHtml)}
            ${lwFinalSummaryRowHtml('AI Recommendation', aiRecHtml)}
            ${lwFinalSummaryRowHtml('Repayment Capacity', repaymentHtml)}
            ${lwFinalSummaryRowHtml('Borrower Evidence', evidenceHtml)}
            ${lwFinalSummaryRowHtml('Application Anomaly', anomalyHtml)}
            ${lwFinalSummaryRowHtml('Top Risk Driver', topDriverHtml)}
        </div>
        <div class="lw-decision-panel-divider"></div>
    `;
}

/* -----------------------------------------------------------------
   FINAL LENDING DECISION (PHASE 8)

   The AI decision (currentApplication.assessment_result.decision) is
   advisory only and is only ever displayed here — it is never sent to
   or required by the decision endpoint. loan_applications.status is
   the single source of truth for application status; this panel reads
   it from currentApplication and writes it via
   POST /lender/applications/<id>/decision, then updates
   currentApplication.status, the context strip, the detail status
   badge, and the queue from that one response — no separate status
   store anywhere on the frontend.
----------------------------------------------------------------- */
let lwDecisionInFlight = false;

function lwFinalDecisionHtml(app) {
    if (!app) return '';

    const status = app.status;
    const isFinalized = status === 'APPROVED' || status === 'REJECTED';

    // PENDING has no dedicated class in lwStatusBadgeClass (it deliberately
    // stays neutral in the queue table), but the dock calls for an
    // amber/neutral treatment here specifically — scoped to this dock only.
    const statusClass = status === 'PENDING' ? 'state-warn' : lwStatusBadgeClass(status);

    const actionsHtml = isFinalized
        ? `
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span class="lw-context-label">Final Decision</span>
                <span class="lw-badge ${lwStatusBadgeClass(status)}">${escapeLwHtml(status)}</span>
                <span class="lw-context-muted" style="font-weight:600; text-transform:none; letter-spacing:0;">This application has been finalized and cannot be changed.</span>
            </div>
        `
        : `
            <div class="lw-decision-panel-actions">
                <button type="button" class="lw-btn lw-decision-panel-keep" id="lwKeepPendingBtn"><i class="fa-solid fa-clock"></i> Keep Pending</button>
                <div class="lw-decision-panel-actions-row">
                    <button type="button" class="lw-btn" style="border-color:var(--danger, #ef4444); color:var(--danger, #ef4444);" id="lwRejectBtn"><i class="fa-solid fa-xmark"></i> Reject</button>
                    <button type="button" class="lw-btn lw-btn-primary" style="background:var(--success, #22c55e); border-color:var(--success, #22c55e);" id="lwApproveBtn"><i class="fa-solid fa-check"></i> Approve</button>
                </div>
            </div>
        `;

    return `
        <div class="lw-decision-panel-header">
            <div class="lw-panel-title" style="margin:0;"><i class="fa-solid fa-gavel"></i> Final Lending Decision</div>
            <button type="button" class="lw-decision-panel-close" id="lwDecisionDockClose" aria-label="Minimize panel" title="Minimize">
                <i class="fa-solid fa-minus"></i>
            </button>
        </div>
        <div class="lw-decision-panel-body">
            ${lwFinalUnderwritingSummaryHtml(app)}
            <div class="lw-decision-panel-info">
                <div class="lw-decision-panel-field">
                    <div class="lw-context-label">Current Status</div>
                    <span class="lw-badge ${statusClass}">${escapeLwHtml(status)}</span>
                </div>
            </div>
            <div class="lw-decision-panel-divider"></div>
            ${actionsHtml}
        </div>
        <div id="lwFinalDecisionAlert" class="lw-decision-dock-alert"></div>
        <div class="lw-context-muted lw-decision-dock-note">AI recommendation is advisory. Final decision is made by the lender.</div>
    `;
}

// The dock lives outside the tab sections (in the HTML, right after
// .lw-desk) so it survives every tab switch. It is the ONE visible
// decision action area for the selected application — the case-file
// detail body no longer mounts its own copy.
function renderLwFinalDecisionBlock() {
    const dock = document.getElementById('lwDecisionDock');
    const mount = document.getElementById('lwDecisionDockBody');
    if (!dock || !mount) return;

    if (!currentApplication) {
        dock.style.display = 'none';
        mount.innerHTML = '';
        return;
    }

    dock.style.display = '';
    mount.innerHTML = lwFinalDecisionHtml(currentApplication);
    attachLwFinalDecisionHandlers(currentApplication.application_id);
}

function attachLwFinalDecisionHandlers(applicationId) {
    const approveBtn = document.getElementById('lwApproveBtn');
    const rejectBtn = document.getElementById('lwRejectBtn');
    const keepBtn = document.getElementById('lwKeepPendingBtn');
    if (approveBtn) approveBtn.addEventListener('click', () => lwConfirmDecision(applicationId, 'APPROVED'));
    if (rejectBtn) rejectBtn.addEventListener('click', () => lwConfirmDecision(applicationId, 'REJECTED'));
    if (keepBtn) keepBtn.addEventListener('click', () => lwSubmitDecision(applicationId, 'PENDING'));

    // Minimize control is purely visual — it toggles a CSS class on the
    // panel and never touches currentApplication or decision state, so
    // the panel is always fully functional again on the next click.
    const closeBtn = document.getElementById('lwDecisionDockClose');
    const dock = document.getElementById('lwDecisionDock');
    if (closeBtn && dock) {
        closeBtn.addEventListener('click', () => {
            const collapsed = dock.classList.toggle('lw-decision-dock-collapsed');
            const icon = closeBtn.querySelector('i');
            if (icon) icon.className = collapsed ? 'fa-solid fa-plus' : 'fa-solid fa-minus';
            closeBtn.title = collapsed ? 'Expand' : 'Minimize';
            closeBtn.setAttribute('aria-label', collapsed ? 'Expand panel' : 'Minimize panel');
        });
    }
}

// Approve/Reject are dangerous actions and always go through the
// confirmation modal first. Keep Pending does not change anything the
// lender would need to undo, so it submits directly.
async function lwConfirmDecision(applicationId, decision) {
    const isApprove = decision === 'APPROVED';
    const confirmed = await lwOpenConfirmModal(
        isApprove ? 'Approve Application?' : 'Reject Application?',
        `Are you sure you want to record ${decision} as the final lending decision?`,
        isApprove ? 'Confirm Approval' : 'Confirm Rejection',
        isApprove ? 'approve' : 'reject'
    );
    if (!confirmed) return;
    lwSubmitDecision(applicationId, decision);
}

function lwSetDecisionButtonsDisabled(disabled) {
    ['lwApproveBtn', 'lwRejectBtn', 'lwKeepPendingBtn'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = disabled;
    });
}

function lwDecisionAlertHtml(message, kind) {
    const color = kind === 'success' ? 'var(--success, #22c55e)' : 'var(--danger, #ef4444)';
    const icon = kind === 'success' ? 'fa-circle-check' : 'fa-circle-exclamation';
    return `<div style="display:flex; align-items:center; gap:8px; color:${color}; font-size:13px; margin:8px 0 14px;"><i class="fa-solid ${icon}"></i><span>${escapeLwHtml(message)}</span></div>`;
}

async function lwSubmitDecision(applicationId, decision) {
    if (lwDecisionInFlight) return;
    if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

    lwDecisionInFlight = true;
    lwSetDecisionButtonsDisabled(true);
    const preAlertBox = document.getElementById('lwFinalDecisionAlert');
    if (preAlertBox) preAlertBox.innerHTML = '';

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/decision`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision })
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale result land here or
        // overwrite the newer currentApplication.
        if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            const message = (data && Array.isArray(data.errors) && data.errors[0])
                || 'Could not record the decision. Please try again.';
            const alertBox = document.getElementById('lwFinalDecisionAlert');
            if (alertBox) alertBox.innerHTML = lwDecisionAlertHtml(message, 'error');
            return;
        }

        // loan_applications.status is the single source of truth — update
        // it on currentApplication, then re-render everything that reads
        // status from it: the Final Decision panel, the context strip,
        // the detail status badge, and (if visible) the queue.
        currentApplication.status = data.application_status;
        lwUpdateContextStrip();
        renderLwFinalDecisionBlock();
        renderLwAssessmentOverview();

        const detailBadge = document.getElementById('lwDetailStatusBadge');
        if (detailBadge) {
            detailBadge.className = `lw-badge ${lwStatusBadgeClass(data.application_status)}`;
            detailBadge.textContent = data.application_status;
        }

        if (data.application_status === 'PENDING') {
            const queued = lwQueueApplications.find(a => String(a.application_id) === String(applicationId));
            if (queued) queued.status = data.application_status;
        } else {
            // Only PENDING applications belong in the pending-review queue.
            lwQueueApplications = lwQueueApplications.filter(a => String(a.application_id) !== String(applicationId));
        }
        renderLenderQueue();

        const successMessage = data.application_status === 'APPROVED' ? 'Application approved'
            : (data.application_status === 'REJECTED' ? 'Application rejected' : 'Application remains pending');
        const successBox = document.getElementById('lwFinalDecisionAlert');
        if (successBox) successBox.innerHTML = lwDecisionAlertHtml(successMessage, 'success');
    } catch (networkErr) {
        const alertBox = document.getElementById('lwFinalDecisionAlert');
        if (alertBox) alertBox.innerHTML = lwDecisionAlertHtml("Couldn't reach the server. Check your connection and try again.", 'error');
    } finally {
        lwDecisionInFlight = false;
        lwSetDecisionButtonsDisabled(false);
    }
}

/* -----------------------------------------------------------------
   CONFIRMATION MODAL

   Small, generic confirm/cancel modal reused by the Final Lending
   Decision panel for the two dangerous actions (Approve/Reject).
   Returns a Promise<boolean> — true only if the lender clicked the
   confirm button.
----------------------------------------------------------------- */
let lwConfirmModalResolve = null;

function initLwConfirmModal() {
    const cancelBtn = document.getElementById('lwConfirmModalCancel');
    const confirmBtn = document.getElementById('lwConfirmModalConfirm');
    const overlay = document.getElementById('lwConfirmModal');
    if (cancelBtn) cancelBtn.addEventListener('click', () => lwCloseConfirmModal(false));
    if (confirmBtn) confirmBtn.addEventListener('click', () => lwCloseConfirmModal(true));
    if (overlay) overlay.addEventListener('click', (e) => {
        if (e.target === overlay) lwCloseConfirmModal(false);
    });
}

function lwOpenConfirmModal(title, message, confirmLabel, variant) {
    return new Promise((resolve) => {
        const overlay = document.getElementById('lwConfirmModal');
        if (!overlay) { resolve(false); return; }
        const confirmBtn = document.getElementById('lwConfirmModalConfirm');
        document.getElementById('lwConfirmModalTitle').textContent = title;
        document.getElementById('lwConfirmModalMessage').textContent = message;
        confirmBtn.textContent = confirmLabel;
        // Semantic color per action: approve=green, reject=red, anything
        // else falls back to the existing neutral/primary look. Purely
        // visual — does not affect which decision gets submitted.
        confirmBtn.className = 'lw-btn ' + (
            variant === 'approve' ? 'lw-btn-approve' :
                variant === 'reject' ? 'lw-btn-reject' :
                    'lw-btn-primary'
        );
        overlay.style.display = 'flex';
        lwConfirmModalResolve = resolve;
    });
}

function lwCloseConfirmModal(result) {
    const overlay = document.getElementById('lwConfirmModal');
    if (overlay) overlay.style.display = 'none';
    if (lwConfirmModalResolve) {
        const resolve = lwConfirmModalResolve;
        lwConfirmModalResolve = null;
        resolve(result);
    }
}

/* -----------------------------------------------------------------
   FORM GATHER / VALIDATE
----------------------------------------------------------------- */
function gatherLwFormData() {
    const payload = {};
    LW_FIELD_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const raw = el.value;
        payload[lwApiFieldName(id)] = LW_NUMERIC_IDS.has(id) ? (raw === '' ? null : Number(raw)) : raw;
    });
    return payload;
}

function validateLwForm() {
    const errors = [];
    LW_FIELD_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const value = el.value;
        if (value === '' || value === null) {
            errors.push({ field: id, message: `${lwLabelFor(id)} is required.` });
            return;
        }
        if (LW_NUMERIC_IDS.has(id)) {
            const num = Number(value);
            if (Number.isNaN(num)) {
                errors.push({ field: id, message: `${lwLabelFor(id)} must be a number.` });
                return;
            }
            const min = el.hasAttribute('min') ? Number(el.min) : null;
            const max = el.hasAttribute('max') ? Number(el.max) : null;
            if (min !== null && num < min) errors.push({ field: id, message: `${lwLabelFor(id)} must be at least ${min}.` });
            else if (max !== null && num > max) errors.push({ field: id, message: `${lwLabelFor(id)} must be at most ${max}.` });
        }
    });
    return errors;
}

function renderLwFormErrors(errors) {
    const box = document.getElementById('lwFormErrors');
    if (!box) return;
    if (!errors || errors.length === 0) {
        box.innerHTML = '';
        box.classList.add('hidden');
        return;
    }
    box.innerHTML = errors.map(err => `
        <div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>${escapeLwHtml(err.message)}</span></div>
    `).join('');
    box.classList.remove('hidden');
}

function setLwSubmitLoading(isLoading) {
    const btn = document.getElementById('lwSubmitBtn');
    const resetBtn = document.getElementById('lwResetBtn');
    if (!btn) return;
    if (isLoading) {
        if (!btn.dataset.originalHtml) btn.dataset.originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Assessing...';
    } else {
        btn.disabled = false;
        if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
    }
    if (resetBtn) resetBtn.disabled = isLoading;
}

/* -----------------------------------------------------------------
   SUBMIT / ORCHESTRATION
----------------------------------------------------------------- */
async function handleLwSubmit(e) {
    e.preventDefault();
    if (lwSubmitInFlight) return;

    renderLwFormErrors([]);
    const clientErrors = validateLwForm();
    if (clientErrors.length > 0) {
        renderLwFormErrors(clientErrors);
        return;
    }

    const payload = gatherLwFormData();

    lwSubmitInFlight = true;
    setLwSubmitLoading(true);
    renderLwAssessmentLoading();
    lwSwitchTab('assessment');

    try {
        const assessRes = await lwRequest('/api/credit-risk/assess', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        let data = null;
        try { data = await assessRes.json(); } catch (parseErr) { data = null; }

        if (!assessRes.ok || !data || data.status !== 'success') {
            const errs = data && Array.isArray(data.errors) ? data.errors.map(m => ({ field: null, message: String(m) })) : null;
            if (errs && errs.length) {
                renderLwFormErrors(errs);
            } else {
                const message = (data && (data.message || data.error)) || 'We couldn\'t assess this application. Please check the details and try again.';
                renderLwFormErrors([{ field: null, message }]);
            }
            renderLwAssessmentEmpty();
            lwSwitchTab('application');
            return;
        }

        // Reset previous results before showing the new ones.
        lwResetResultState();
        lwCurrentApplicant = payload;
        lwCurrentMeta = gatherLwMetaData();
        lwAssessData = data;

        const borrowerId = lwCurrentMeta.borrowerId;
        const behaviorUrl = '/api/credit-risk/financial-behavior' + (borrowerId ? `?borrower_id=${encodeURIComponent(borrowerId)}` : '');

        const [explainData, anomalyData, affordData, behaviorData] = await Promise.all([
            lwSafeJsonFetch('/api/credit-risk/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
            lwSafeJsonFetch('/api/credit-risk/anomaly', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
            lwSafeJsonFetch('/api/credit-risk/affordability', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ applicant: payload, borrower_id: borrowerId }) }),
            lwSafeJsonFetch(behaviorUrl, { method: 'GET' })
        ]);

        lwExplainData = explainData;
        lwAnomalyData = anomalyData;
        lwAffordabilityData = affordData;
        lwBehaviorData = behaviorData;

        renderLwAssessmentOverview();
        renderLwSummary();
        renderLwAffordability();
        renderLwBehavior();
        renderLwExplainability();
        renderLwAnomaly();
        initLwScenario();

    } catch (networkErr) {
        renderLwAssessmentError('Couldn\'t reach the credit risk service. Check your connection and try again.');
        lwSwitchTab('application');
    } finally {
        lwSubmitInFlight = false;
        setLwSubmitLoading(false);
    }
}

function lwResetResultState() {
    lwAssessData = null;
    lwExplainData = null;
    lwAnomalyData = null;
    lwAffordabilityData = null;
    lwBehaviorData = null;
}

function handleLwReset() {
    const form = document.getElementById('lenderAssessmentForm');
    if (form) form.reset();
    renderLwFormErrors([]);
    lwResetResultState();
    lwCurrentApplicant = null;
    lwCurrentMeta = null;

    renderLwAssessmentEmpty();
    document.getElementById('lwSummaryPanel').style.display = 'none';

    document.getElementById('lwAffordabilityBody').innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Submit an application to load repayment capacity.</div>`;
    document.getElementById('lwBehaviorBody').innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Submit an application to load borrower evidence.</div>`;

    const explainBody = document.getElementById('lwExplainBody');
    explainBody.querySelectorAll('.lw-factor-groups, .lw-empty-sub').forEach(n => n.remove());
    explainBody.insertAdjacentHTML('beforeend', `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Submit an application to see the risk factors behind this decision.</div>`);

    document.getElementById('lwAnomalyBody').innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Submit an application to check for anomalies.</div>`;
    document.getElementById('lwScenarioControls').innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Submit an application first — scenario analysis compares against that baseline.</div>`;

    lwSwitchTab('application');
}

/* -----------------------------------------------------------------
   3 & 10. ASSESSMENT OVERVIEW + FINAL DECISION SUMMARY
----------------------------------------------------------------- */
function renderLwAssessmentEmpty() {
    document.getElementById('lwAssessmentBody').innerHTML = `
        <div class="lw-empty" id="lwEmptyState">
            <i class="fa-solid fa-file-circle-question"></i>
            <div class="lw-empty-title">No application assessed yet</div>
            <div class="lw-empty-sub">Select an application from the Queue and run the AI Credit
                Assessment to see risk and decision here.</div>
        </div>`;
    lwFillOverviewSideMounts();
}

function renderLwAssessmentLoading() {
    document.getElementById('lwAssessmentBody').innerHTML = `<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Running credit risk assessment...</div>`;
}

function renderLwAssessmentError(message) {
    document.getElementById('lwAssessmentBody').innerHTML = `
        <div class="lw-empty">
            <i class="fa-solid fa-triangle-exclamation" style="color: var(--danger, #ef4444);"></i>
            <div class="lw-empty-title">Assessment failed</div>
            <div class="lw-empty-sub">${escapeLwHtml(message)}</div>
        </div>`;
}

// Dispatcher: the manual-form flow (lwAssessData, legacy — its nav entry
// point is hidden but the code path is kept intact) has the full
// affordability/anomaly/explain pipeline alongside it, so it renders the
// richer overview. The queue-driven flow only ever has the persisted
// core assessment (currentApplication.assessment_result) — no fabricated
// affordability/anomaly stat cards are shown for it. If neither exists,
// show the empty state — never the wrong application's data.
function renderLwAssessmentOverview() {
    if (lwAssessData) {
        renderLwAssessmentOverviewFull();
        return;
    }
    if (currentApplication && currentApplication.assessment_result) {
        renderLwAssessmentOverviewFromCurrentApplication();
        return;
    }
    renderLwAssessmentEmpty();
}

function renderLwAssessmentOverviewFromCurrentApplication() {
    const d = currentApplication.assessment_result;
    const riskClass = lwRiskClass(d.risk_level);
    const decisionClass = lwDecisionClass(d.decision);
    const pct = typeof d.risk_percentage === 'number'
        ? d.risk_percentage
        : (typeof d.risk_probability === 'number' ? d.risk_probability * 100 : NaN);
    const pctLabel = Number.isFinite(pct) ? pct.toFixed(1) + '%' : 'Unavailable';

    document.getElementById('lwAssessmentBody').innerHTML = `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-shield-halved"></i> AI Credit Assessment</div>
            <div class="lw-panel-note">Application #${escapeLwHtml(currentApplication.application_id)}</div>
        </div>

        <div class="lw-overview-top lw-overview-top-dominant">
            <div class="lw-risk-block">
                ${lwRiskGaugeHtml(pct, pctLabel, riskClass, d.risk_level)}
            </div>
            <div class="lw-decision-badge ${decisionClass}">${escapeLwHtml(d.decision)}</div>
        </div>

        <div class="lw-probability-track"><div class="lw-probability-fill ${riskClass}" style="width:${Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0}%;"></div></div>
    `;

    lwFillOverviewSideMounts();
}

function lwFillOverviewSideMounts() {
    const snap = document.getElementById('lwSnapshotBody');
    if (snap) {
        snap.innerHTML = `<div class="lw-snapshot-list">${lwOverviewSnapshotRowsHtml()}</div>`;
    }
    const decomp = document.getElementById('lwRiskDecompositionBody');
    if (decomp) decomp.innerHTML = lwExplainPreviewHtml();
}

function lwOverviewSnapshotRowsHtml() {
    const app = currentApplication;
    const notReviewed = { text: 'Not reviewed', cls: '' };
    const unavailable = { text: 'Unavailable', cls: '' };

    const repayment = (() => {
        const data = app && app.repaymentCapacity;
        if (!data) return notReviewed;
        if (data.borrower_linked === false) return unavailable;
        const status = data.affordability && data.affordability.status;
        if (!status) return unavailable;
        const cls = status === 'affordable' ? 'state-good' : (status === 'strained' ? 'state-warn' : (status === 'insufficient_data' ? '' : 'state-bad'));
        return { text: String(status).replace(/_/g, ' '), cls };
    })();

    const evidence = (() => {
        const data = app && app.borrowerEvidence;
        if (!data) return notReviewed;
        if (data.borrower_linked === false) return unavailable;
        const months = data.data_coverage && data.data_coverage.history_months;
        if (months === null || months === undefined || months === '') return unavailable;
        return { text: months + ' month(s)', cls: '' };
    })();

    const anomaly = (() => {
        const data = app && app.anomaly;
        if (!data) return notReviewed;
        if (data.available === false) return unavailable;
        if (data.manual_review) return { text: 'Manual Review Recommended', cls: 'state-warn' };
        if (data.is_anomaly) return { text: data.anomaly_level || 'Anomalous', cls: 'state-warn' };
        return { text: data.anomaly_level || 'Normal', cls: 'state-good' };
    })();

    const scenario = (() => {
        const data = app && app.scenarioResult;
        if (!data) return notReviewed;
        if (data.scenario_decision) return { text: String(data.scenario_decision), cls: lwDecisionClass(data.scenario_decision) };
        if (data.scenario_risk_level) return { text: String(data.scenario_risk_level), cls: lwRiskClass(data.scenario_risk_level) };
        return unavailable;
    })();

    return [
        lwSnapshotRowHtml('affordability', 'Repayment Capacity', repayment.text, repayment.cls),
        lwSnapshotRowHtml('evidence', 'Borrower Evidence', evidence.text, evidence.cls),
        lwSnapshotRowHtml('anomaly', 'Application Anomaly', anomaly.text, anomaly.cls),
        lwSnapshotRowHtml('scenario', 'Scenario Analysis', scenario.text, scenario.cls)
    ].join('');
}

// Small, non-duplicating summary card for the Overview tab. Only shows
// once the Application Anomaly tab has actually loaded a result for
// this application (currentApplication.anomaly) — this never triggers
// a fetch of its own, and never repeats the full anomaly panel (score,
// severity, disclaimer). If anomaly hasn't been loaded yet, no card is
// added at all.
function lwAnomalyOverviewCardHtml() {
    const anomaly = currentApplication && currentApplication.anomaly;
    if (!anomaly || anomaly.available === false) return '';
    const summary = anomaly.manual_review ? 'Manual Review Recommended' : 'Normal';
    return `<div class="lw-stat-card"><div class="lw-stat-label">Anomaly</div>${lwVal(summary, { stateClass: anomaly.manual_review ? 'state-warn' : 'state-good' })}</div>`;
}

// Small, non-duplicating preview for the Overview tab. Only shows real
// top factors once the Decision Explanation tab has actually loaded
// them for this application (currentApplication.explanation); otherwise
// it just points at the button rather than fetching or fabricating
// anything here.
function lwExplainPreviewHtml() {
    const explanation = currentApplication && currentApplication.explanation;
    if (!explanation || explanation.explanation_available === false) {
        return `<div class="lw-empty-sub">Open Decision Explanation once to load SHAP factors for this
            application. They will appear here without a second fetch.</div>`;
    }
    const increasing = explanation.risk_increasing_factors || [];
    const reducing = explanation.risk_reducing_factors || [];
    return `
        <div class="lw-factor-groups">
            <div class="lw-factor-group">
                <div class="lw-factor-group-title increasing"><i class="fa-solid fa-arrow-trend-up"></i> Risk-Increasing Factors</div>
                <div class="lw-factor-list">${lwFactorList(increasing, 'increasing')}</div>
            </div>
            <div class="lw-factor-group">
                <div class="lw-factor-group-title reducing"><i class="fa-solid fa-arrow-trend-down"></i> Risk-Reducing Factors</div>
                <div class="lw-factor-list">${lwFactorList(reducing, 'reducing')}</div>
            </div>
        </div>`;
}

function renderLwAssessmentOverviewFull() {
    const d = lwAssessData;
    const riskClass = lwRiskClass(d.risk_level);
    const decisionClass = lwDecisionClass(d.decision);
    const pct = typeof d.risk_percentage === 'number' ? d.risk_percentage : (d.risk_probability * 100);

    const affordability = (lwAffordabilityData && lwAffordabilityData.affordability) || null;
    const anomaly = lwAnomalyData || null;

    let affordBadge = `<span class="lw-badge">Unavailable</span>`;
    if (affordability && affordability.status) {
        const s = affordability.status;
        const cls = s === 'affordable' ? 'state-good' : (s === 'strained' ? 'state-warn' : 'state-bad');
        affordBadge = `<span class="lw-badge ${cls}">${escapeLwHtml(s.replace('_', ' '))}</span>`;
    }

    let anomalyBadge = `<span class="lw-badge">Unavailable</span>`;
    if (anomaly && anomaly.available) {
        const level = String(anomaly.anomaly_level || '').toLowerCase();
        const cls = level.includes('high') || level.includes('medium') ? 'state-warn' : 'state-good';
        anomalyBadge = `<span class="lw-badge ${cls}">${escapeLwHtml(anomaly.anomaly_level)}</span>`;
    }

    const topIncreasing = (lwExplainData && lwExplainData.risk_increasing_factors && lwExplainData.risk_increasing_factors[0]) || null;
    const topReducing = (lwExplainData && lwExplainData.risk_reducing_factors && lwExplainData.risk_reducing_factors[0]) || null;

    const meta = lwCurrentMeta || {};
    const requestedLoan = lwCurrentApplicant ? lwCurrentApplicant.credit_amount : null;

    document.getElementById('lwAssessmentBody').innerHTML = `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-gauge-high"></i> Assessment Overview</div>
            <div class="lw-panel-note">/api/credit-risk/assess</div>
        </div>

        <div class="lw-stat-row" style="margin-bottom:16px;">
            <div class="lw-stat-card"><div class="lw-stat-label">Applicant</div>${lwVal(meta.applicantName)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Application ID</div>${lwVal(meta.applicationId)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Requested Loan</div>${lwVal(requestedLoan, { suffix: ' (model units)' })}</div>
        </div>

        <div class="lw-overview-top">
            <div class="lw-risk-block">
                <div class="lw-risk-icon ${riskClass}"><i class="fa-solid fa-shield-halved"></i></div>
                <div>
                    <div class="lw-risk-probability">${pct.toFixed(1)}%</div>
                    <div class="lw-risk-level ${riskClass}">${escapeLwHtml(d.risk_level)}</div>
                </div>
            </div>
            <div class="lw-decision-badge ${decisionClass}">${escapeLwHtml(d.decision)}</div>
        </div>

        <div class="lw-probability-track"><div class="lw-probability-fill ${riskClass}" style="width: ${Math.min(100, Math.max(0, pct))}%;"></div></div>

        <div class="lw-stat-row">
            <div class="lw-stat-card">
                <div class="lw-stat-label">Repayment Capacity</div>
                ${affordBadge}
            </div>
            <div class="lw-stat-card">
                <div class="lw-stat-label">Application Anomaly</div>
                ${anomalyBadge}
            </div>
            <div class="lw-stat-card">
                <div class="lw-stat-label">Top Risk Factor</div>
                ${lwVal(topIncreasing ? topIncreasing.feature : null)}
            </div>
            <div class="lw-stat-card">
                <div class="lw-stat-label">Top Risk-Reducing Factor</div>
                ${lwVal(topReducing ? topReducing.feature : null)}
            </div>
        </div>

        <div class="lw-stat-row" style="margin-top:12px;">
            <div class="lw-stat-card">
                <div class="lw-stat-label">Est. Monthly Payment</div>
                ${lwVal(lwAffordabilityData && lwAffordabilityData.loan ? lwAffordabilityData.loan.estimated_monthly_payment : null, { suffix: ' (model units)' })}
            </div>
            <div class="lw-stat-card">
                <div class="lw-stat-label">Manual Review Flag</div>
                ${lwVal(anomaly && anomaly.available ? (anomaly.manual_review ? 'Yes' : 'No') : null)}
            </div>
        </div>
    `;
    lwFillOverviewSideMounts();
}

function renderLwSummary() {
    const d = lwAssessData;
    const panel = document.getElementById('lwSummaryPanel');
    const card = document.getElementById('lwSummaryCard');
    panel.style.display = '';

    const affordability = (lwAffordabilityData && lwAffordabilityData.affordability) || null;
    const anomaly = lwAnomalyData || null;
    const topIncreasing = (lwExplainData && lwExplainData.risk_increasing_factors && lwExplainData.risk_increasing_factors[0]) || null;

    let nextAction = 'Route to standard underwriting queue.';
    const decisionLower = String(d.decision || '').toLowerCase();
    if (decisionLower.includes('reject')) nextAction = 'Decline application; issue adverse-action notice per policy.';
    else if (decisionLower.includes('review')) nextAction = 'Escalate to manual underwriting review before proceeding.';
    else if (decisionLower.includes('approve')) nextAction = 'Proceed with standard approval workflow.';
    if (anomaly && anomaly.available && anomaly.manual_review) {
        nextAction += ' Additional manual review recommended due to anomaly flag.';
    }

    card.innerHTML = `
        <div class="lw-summary-row"><span class="lw-summary-key">Decision</span><span class="lw-summary-val">${escapeLwHtml(d.decision)}</span></div>
        <div class="lw-summary-row"><span class="lw-summary-key">Risk Level</span><span class="lw-summary-val">${escapeLwHtml(d.risk_level)} (${(d.risk_percentage ?? d.risk_probability * 100).toFixed(1)}%)</span></div>
        <div class="lw-summary-row"><span class="lw-summary-key">Repayment Capacity</span><span class="lw-summary-val">${affordability ? escapeLwHtml(affordability.status.replace('_', ' ')) : 'Unavailable'}</span></div>
        <div class="lw-summary-row"><span class="lw-summary-key">Top Risk Factor</span><span class="lw-summary-val">${topIncreasing ? escapeLwHtml(topIncreasing.feature) : 'Unavailable'}</span></div>
        <div class="lw-summary-row"><span class="lw-summary-key">Anomaly Result</span><span class="lw-summary-val">${anomaly && anomaly.available ? escapeLwHtml(anomaly.anomaly_level) : 'Unavailable'}</span></div>
        <div class="lw-summary-row"><span class="lw-summary-key">Recommended Next Action</span><span class="lw-summary-val">${escapeLwHtml(nextAction)}</span></div>
    `;
}

/* -----------------------------------------------------------------
   4. REPAYMENT CAPACITY
----------------------------------------------------------------- */
// Dispatcher, same pattern as renderLwAssessmentOverview /
// renderLwExplainability: the legacy manual-form pipeline
// (lwAffordabilityData) renders its own result when present; otherwise
// the queue-driven flow loads/renders repayment capacity for
// currentApplication on demand.
function renderLwAffordability() {
    if (lwAffordabilityData) {
        renderLwAffordabilityFull(lwAffordabilityData);
        return;
    }
    renderLwRepaymentCapacityForCurrentApplication();
}

function renderLwAffordabilityFull(data) {
    const container = document.getElementById('lwAffordabilityBody');
    if (!data) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Repayment capacity is currently unavailable for this application.</div>`;
        return;
    }
    if (data.borrower_linked === false) {
        container.innerHTML = `
            <div class="lw-disclaimer" style="margin-bottom:0;">
                <i class="fa-solid fa-triangle-exclamation"></i>
                <span>${escapeLwHtml(data.message || 'No borrower is linked to this application, so repayment capacity cannot be calculated.')}</span>
            </div>`;
        return;
    }
    const cap = data.financial_capacity || {};
    const loan = data.loan || {};
    const aff = data.affordability || {};

    const statusClass = aff.status === 'affordable' ? 'state-good' : (aff.status === 'strained' ? 'state-warn' : (aff.status === 'insufficient_data' ? '' : 'state-bad'));

    const surplusIsDeficit = typeof aff.available_surplus === 'number' && aff.available_surplus < 0;

    container.innerHTML = `
        <div class="lw-panel-title" style="margin-bottom:10px;"><i class="fa-solid fa-wallet"></i> Current Cash Flow
            <span class="lw-context-muted" style="font-weight:600; text-transform:none; letter-spacing:0;">(real ₹, FinTrust history)</span>
        </div>
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Income</div>${lwVal(cap.monthly_income, { prefix: '₹' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Existing Obligations</div>${lwVal(cap.monthly_expenses, { prefix: '₹' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Available Surplus</div>${lwVal(aff.available_surplus, { prefix: '₹', stateClass: surplusIsDeficit ? 'state-bad' : 'state-good' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Recurring Burden</div>${lwVal(cap.recurring_burden, { prefix: '₹' })}</div>
        </div>

        <div class="lw-panel-title" style="margin:20px 0 10px 0;"><i class="fa-solid fa-file-invoice-dollar"></i> Repayment Capacity
            <span class="lw-context-muted" style="font-weight:600; text-transform:none; letter-spacing:0;">(installment in model units)</span>
        </div>
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Estimated Installment</div>${lwVal(lwFormatModelUnits(loan.estimated_monthly_payment))}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Payment / Income</div>${lwVal(aff.payment_to_income_ratio, { suffix: '%' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Payment / Surplus</div>${lwVal(aff.payment_to_surplus_ratio, { suffix: '%' })}</div>
            <div class="lw-stat-card">
                <div class="lw-stat-label">Affordability Status</div>
                ${aff.status ? `<span class="lw-badge ${statusClass}">${escapeLwHtml(aff.status.replace('_', ' '))}</span>` : lwVal(null)}
            </div>
        </div>

        <div class="lw-panel-note" style="margin-top:16px; opacity:0.8;">
            <i class="fa-solid fa-circle-info"></i>
            The requested loan amount and tenure are in the risk model's native units, while income,
            surplus, and recurring burden above are real ₹ figures from the borrower's FinTrust history. The
            payment-to-income/surplus ratios and "Estimated Installment" mix these two unit systems and
            should be treated as indicative only, not a true ₹ affordability figure.
        </div>
        ${aff.reason ? `<div class="lw-disclaimer" style="margin-top:10px; margin-bottom:0;"><i class="fa-solid fa-circle-info"></i><span>${escapeLwHtml(aff.reason)}</span></div>` : ''}
    `;
}

/* -----------------------------------------------------------------
   5. BORROWER EVIDENCE (financial-behavior, reframed for underwriting)
----------------------------------------------------------------- */
// Dispatcher, same pattern as renderLwAssessmentOverview /
// renderLwExplainability: the legacy manual-form pipeline
// (lwBehaviorData) renders its own result when present; otherwise the
// queue-driven flow loads/renders borrower evidence for
// currentApplication on demand.
function renderLwBehavior() {
    if (lwBehaviorData) {
        renderLwBehaviorFull(lwBehaviorData);
        return;
    }
    renderLwBorrowerEvidenceForCurrentApplication();
}

function renderLwBehaviorFull(data) {
    const container = document.getElementById('lwBehaviorBody');
    if (!data) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Borrower evidence is currently unavailable for this application.</div>`;
        return;
    }
    if (data.borrower_linked === false) {
        container.innerHTML = `
            <div class="lw-disclaimer" style="margin-bottom:0;">
                <i class="fa-solid fa-triangle-exclamation"></i>
                <span>${escapeLwHtml(data.message || 'No borrower is linked to this application, so borrower evidence cannot be shown.')}</span>
            </div>`;
        return;
    }
    const income = data.income || {};
    const spending = data.spending || {};
    const savings = data.savings || {};
    const recurring = data.recurring || {};
    const cashFlow = data.cash_flow || {};
    const flags = data.behavioral_flags || [];

    const flagsHtml = flags.length
        ? flags.map(f => `
            <div class="lw-flag-item severity-${escapeLwHtml(f.severity)}">
                <i class="fa-solid ${f.severity === 'high' ? 'fa-triangle-exclamation' : 'fa-circle-info'}" style="color: var(--${f.severity === 'high' ? 'danger' : 'warning'}, #f59e0b); margin-top:2px;"></i>
                <div>
                    <div class="lw-flag-title">${escapeLwHtml(f.message)}</div>
                    <div class="lw-flag-evidence">${escapeLwHtml(f.evidence)}</div>
                </div>
            </div>`).join('')
        : `<div class="lw-empty-sub">No behavioral flags recorded.</div>`;

    const cashFlowState = cashFlow.current_surplus !== null && cashFlow.current_surplus !== undefined
        ? (cashFlow.current_surplus < 0 ? 'state-bad' : 'state-good')
        : '';

    container.innerHTML = `
        <div class="lw-panel-title" style="margin-bottom:10px;"><i class="fa-solid fa-chart-line"></i> Financial Stability</div>
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Income Stability</div>${lwVal(income.stability)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Spending Volatility</div>${lwVal(spending.volatility)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Savings Behavior</div>${lwVal(savings.stability)}</div>
        </div>

        <div class="lw-panel-title" style="margin:18px 0 10px 0;"><i class="fa-solid fa-wallet"></i> Cash Flow</div>
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Recurring Burden</div>${lwVal(recurring.burden_ratio, { suffix: '%' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Current Cash-Flow Pressure</div>${lwVal(cashFlow.current_surplus !== null && cashFlow.current_surplus !== undefined ? (cashFlow.current_surplus < 0 ? 'Deficit' : 'Positive') : null, { stateClass: cashFlowState })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">History Available</div>${lwVal(data.data_coverage ? `${data.data_coverage.history_months} month(s)` : null)}</div>
        </div>

        <div style="margin-top:18px;">
            <div class="lw-panel-title" style="margin-bottom:10px;"><i class="fa-solid fa-flag"></i> Underwriting Flags</div>
            ${flagsHtml}
        </div>
    `;
}

/* -----------------------------------------------------------------
   REPAYMENT CAPACITY FOR currentApplication (PHASE 5)

   Loaded on demand via POST /lender/applications/<id>/repayment-capacity
   — never computed in JS, never persisted server-side beyond the
   request. Cached only on currentApplication.repaymentCapacity for the
   lifetime of that selection; switching applications replaces
   currentApplication (and therefore drops the cache) before this can
   ever show the wrong application's figures. Same staleness-guard
   pattern as lwFetchAndRenderExplanation.
----------------------------------------------------------------- */
let lwRepaymentCapacityInFlight = false;

function renderLwRepaymentCapacityForCurrentApplication() {
    const container = document.getElementById('lwAffordabilityBody');
    if (!container) return;

    if (!currentApplication) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Select an application from the queue.</div>`;
        return;
    }

    // Already loaded for this application this session — render
    // straight from the cache, no refetch.
    if (currentApplication.repaymentCapacity) {
        renderLwAffordabilityFull(currentApplication.repaymentCapacity);
        return;
    }

    lwFetchAndRenderRepaymentCapacity(currentApplication.application_id);
}

async function lwFetchAndRenderRepaymentCapacity(applicationId) {
    const container = document.getElementById('lwAffordabilityBody');
    if (!container) return;

    lwRepaymentCapacityInFlight = true;
    container.innerHTML = `<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Calculating repayment capacity...</div>`;

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/repayment-capacity`, {
            method: 'POST'
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale result land under a
        // different currentApplication.
        if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            renderLwRepaymentCapacityError(applicationId);
            return;
        }

        currentApplication.repaymentCapacity = data;
        renderLwAffordabilityFull(data);
        // Keep the floating Final Underwriting Summary in sync now that
        // repaymentCapacity is cached — same staleness guard as above.
        renderLwFinalDecisionBlock();
    } catch (networkErr) {
        if (currentApplication && String(currentApplication.application_id) === String(applicationId)) {
            renderLwRepaymentCapacityError(applicationId);
        }
    } finally {
        lwRepaymentCapacityInFlight = false;
    }
}

function renderLwRepaymentCapacityError(applicationId) {
    const container = document.getElementById('lwAffordabilityBody');
    if (!container) return;
    container.innerHTML = `
        <div class="lw-error-list">
            <div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>Repayment capacity is currently unavailable for this application.</span></div>
        </div>
        <div class="lw-form-actions">
            <button type="button" class="lw-btn lw-btn-primary" id="lwRetryAffordabilityBtn"><i class="fa-solid fa-rotate"></i> Retry</button>
        </div>
    `;
    const btn = document.getElementById('lwRetryAffordabilityBtn');
    if (btn) btn.addEventListener('click', () => lwFetchAndRenderRepaymentCapacity(applicationId));
}

/* -----------------------------------------------------------------
   BORROWER EVIDENCE FOR currentApplication (PHASE 5)

   Loaded on demand via GET /lender/applications/<id>/borrower-evidence
   — never computed in JS, never persisted server-side beyond the
   request. Cached only on currentApplication.borrowerEvidence for the
   lifetime of that selection; switching applications replaces
   currentApplication (and therefore drops the cache) before this can
   ever show the wrong application's evidence. Same staleness-guard
   pattern as lwFetchAndRenderExplanation.
----------------------------------------------------------------- */
let lwBorrowerEvidenceInFlight = false;

function renderLwBorrowerEvidenceForCurrentApplication() {
    const container = document.getElementById('lwBehaviorBody');
    if (!container) return;

    if (!currentApplication) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Select an application from the queue.</div>`;
        return;
    }

    // Already loaded for this application this session — render
    // straight from the cache, no refetch.
    if (currentApplication.borrowerEvidence) {
        renderLwBehaviorFull(currentApplication.borrowerEvidence);
        return;
    }

    lwFetchAndRenderBorrowerEvidence(currentApplication.application_id);
}

async function lwFetchAndRenderBorrowerEvidence(applicationId) {
    const container = document.getElementById('lwBehaviorBody');
    if (!container) return;

    lwBorrowerEvidenceInFlight = true;
    container.innerHTML = `<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading borrower evidence...</div>`;

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/borrower-evidence`, {
            method: 'GET'
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale result land under a
        // different currentApplication.
        if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            renderLwBorrowerEvidenceError(applicationId);
            return;
        }

        currentApplication.borrowerEvidence = data;
        renderLwBehaviorFull(data);
        // Keep the floating Final Underwriting Summary in sync now that
        // borrowerEvidence is cached — same staleness guard as above.
        renderLwFinalDecisionBlock();
    } catch (networkErr) {
        if (currentApplication && String(currentApplication.application_id) === String(applicationId)) {
            renderLwBorrowerEvidenceError(applicationId);
        }
    } finally {
        lwBorrowerEvidenceInFlight = false;
    }
}

function renderLwBorrowerEvidenceError(applicationId) {
    const container = document.getElementById('lwBehaviorBody');
    if (!container) return;
    container.innerHTML = `
        <div class="lw-error-list">
            <div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>Borrower evidence is currently unavailable for this application.</span></div>
        </div>
        <div class="lw-form-actions">
            <button type="button" class="lw-btn lw-btn-primary" id="lwRetryEvidenceBtn"><i class="fa-solid fa-rotate"></i> Retry</button>
        </div>
    `;
    const btn = document.getElementById('lwRetryEvidenceBtn');
    if (btn) btn.addEventListener('click', () => lwFetchAndRenderBorrowerEvidence(applicationId));
}

/* -----------------------------------------------------------------
   6. DECISION EXPLANATION
----------------------------------------------------------------- */
function lwFactorList(factors, direction) {
    if (!factors || factors.length === 0) return `<div class="lw-empty-sub">No ${direction === 'increasing' ? 'risk-increasing' : 'risk-reducing'} factors reported.</div>`;
    const maxImpact = factors.reduce((max, f) => Math.max(max, f.impact || 0), 0) || 1;
    return factors.map((f, i) => {
        const widthPct = Math.max(4, Math.round(((f.impact || 0) / maxImpact) * 100));
        const impactLabel = typeof f.impact === 'number'
            ? (direction === 'increasing' ? '+' : '−') + Math.abs(f.impact).toFixed(3)
            : '';
        return `
            <div class="lw-factor-item lw-factor-${direction}">
                <div class="lw-factor-row">
                    <span class="lw-factor-name">${i + 1}. ${escapeLwHtml(f.feature)}</span>
                    <span class="lw-factor-impact">${impactLabel}</span>
                </div>
                <div class="lw-factor-bar-track"><div class="lw-factor-bar-fill" style="width:${widthPct}%;"></div></div>
            </div>`;
    }).join('');
}

// Dispatcher, same pattern as renderLwAssessmentOverview: the legacy
// manual-form pipeline (lwExplainData) renders its own full result when
// present; otherwise the queue-driven flow loads/renders the explanation
// for currentApplication on demand (not persisted).
function renderLwExplainability() {
    if (lwExplainData) {
        renderLwExplainabilityFull();
        return;
    }
    renderLwDecisionExplanationForCurrentApplication();
}

function renderLwExplainabilityFull() {
    const container = document.getElementById('lwExplainBody');
    const data = lwExplainData;

    if (!data || data.explanation_available === false) {
        container.innerHTML = `
            <div class="lw-panel-header">
                <div class="lw-panel-title"><i class="fa-solid fa-magnifying-glass-chart"></i> Decision Explanation</div>
                <div class="lw-panel-note">/api/credit-risk/explain</div>
            </div>
            <div class="lw-empty-sub" style="text-align:center; padding:24px;">${data && data.message ? escapeLwHtml(data.message) : 'Explanation unavailable for this assessment.'}</div>
        `;
        return;
    }

    const increasing = data.risk_increasing_factors || [];
    const reducing = data.risk_reducing_factors || [];

    container.innerHTML = `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-magnifying-glass-chart"></i> Decision Explanation</div>
            <div class="lw-panel-note">/api/credit-risk/explain — SHAP model contribution, not a causal claim</div>
        </div>
        <div class="lw-factor-groups">
            <div class="lw-factor-group">
                <div class="lw-factor-group-title increasing"><i class="fa-solid fa-arrow-trend-up"></i> Top Risk-Increasing Factors</div>
                <div class="lw-factor-list">${lwFactorList(increasing, 'increasing')}</div>
            </div>
            <div class="lw-factor-group">
                <div class="lw-factor-group-title reducing"><i class="fa-solid fa-arrow-trend-down"></i> Top Risk-Reducing Factors</div>
                <div class="lw-factor-list">${lwFactorList(reducing, 'reducing')}</div>
            </div>
        </div>
    `;
}

/* -----------------------------------------------------------------
   DECISION EXPLANATION FOR currentApplication (PHASE 4)

   Loaded on demand via POST /lender/applications/<id>/explain — never
   computed in JS, never persisted. Cached only on
   currentApplication.explanation for the lifetime of that selection;
   switching applications replaces currentApplication (and therefore
   drops the cache) before this can ever show the wrong app's factors.
----------------------------------------------------------------- */
function lwExplainPanelHeader(title) {
    return `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-magnifying-glass-chart"></i> ${escapeLwHtml(title || 'Decision Explanation')}</div>
        </div>`;
}

function renderLwDecisionExplanationForCurrentApplication() {
    const container = document.getElementById('lwExplainBody');
    if (!container) return;

    if (!currentApplication) {
        container.innerHTML = `
            ${lwExplainPanelHeader()}
            <div class="lw-empty-sub" style="text-align:center; padding:24px;">Select an application from the queue.</div>`;
        return;
    }

    if (!currentApplication.assessment_result) {
        container.innerHTML = `
            ${lwExplainPanelHeader()}
            <div class="lw-empty-sub" style="text-align:center; padding:24px;">Run AI Credit Assessment first.</div>`;
        return;
    }

    // Already loaded for this application this session — render straight
    // from the cache, no refetch.
    if (currentApplication.explanation) {
        renderLwExplainResult(currentApplication.explanation);
        return;
    }

    lwFetchAndRenderExplanation(currentApplication.application_id);
}

async function lwFetchAndRenderExplanation(applicationId) {
    const container = document.getElementById('lwExplainBody');
    if (!container) return;

    lwExplainInFlight = true;
    container.innerHTML = `
        ${lwExplainPanelHeader()}
        <div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Analyzing decision factors...</div>`;

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/explain`, {
            method: 'POST'
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale explanation land
        // under a different currentApplication (same staleness guard
        // pattern as runLenderAssessment).
        if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            renderLwExplainError(applicationId);
            return;
        }

        // Assessment result must remain visible even though explanation
        // succeeded/failed independently — it lives untouched on
        // currentApplication.assessment_result the whole time.
        currentApplication.explanation = data;
        renderLwExplainResult(data);
        // Keep the floating Final Underwriting Summary in sync now that
        // explanation is cached — same staleness guard as above.
        renderLwFinalDecisionBlock();
    } catch (networkErr) {
        if (currentApplication && String(currentApplication.application_id) === String(applicationId)) {
            renderLwExplainError(applicationId);
        }
    } finally {
        lwExplainInFlight = false;
    }
}

function renderLwExplainError(applicationId) {
    const container = document.getElementById('lwExplainBody');
    if (!container) return;
    container.innerHTML = `
        ${lwExplainPanelHeader()}
        <div class="lw-error-list">
            <div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>Decision explanation is currently unavailable.</span></div>
        </div>
        <div class="lw-form-actions">
            <button type="button" class="lw-btn lw-btn-primary" id="lwRetryExplainBtn"><i class="fa-solid fa-rotate"></i> Retry</button>
        </div>
    `;
    const btn = document.getElementById('lwRetryExplainBtn');
    if (btn) btn.addEventListener('click', () => lwFetchAndRenderExplanation(applicationId));
}

function renderLwExplainResult(data) {
    const container = document.getElementById('lwExplainBody');
    if (!container) return;

    if (data.explanation_available === false) {
        container.innerHTML = `
            ${lwExplainPanelHeader()}
            <div class="lw-empty-sub" style="text-align:center; padding:24px;">${escapeLwHtml(data.message || 'Explanation unavailable for this assessment.')}</div>`;
        return;
    }

    const increasing = data.risk_increasing_factors || [];
    const reducing = data.risk_reducing_factors || [];

    container.innerHTML = `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-magnifying-glass-chart"></i> Why This Result?</div>
            <div class="lw-panel-note">POST /lender/applications/&lt;id&gt;/explain — SHAP model contribution, not a causal claim</div>
        </div>
        <div class="lw-factor-groups">
            <div class="lw-factor-group">
                <div class="lw-factor-group-title increasing"><i class="fa-solid fa-arrow-trend-up"></i> Risk-Increasing Factors</div>
                <div class="lw-factor-list">${lwFactorList(increasing, 'increasing')}</div>
            </div>
            <div class="lw-factor-group">
                <div class="lw-factor-group-title reducing"><i class="fa-solid fa-arrow-trend-down"></i> Risk-Reducing Factors</div>
                <div class="lw-factor-list">${lwFactorList(reducing, 'reducing')}</div>
            </div>
        </div>
    `;
}

/* -----------------------------------------------------------------
   7. APPLICATION ANOMALY

   Dispatcher, same pattern as renderLwAssessmentOverview /
   renderLwExplainability: the legacy manual-form pipeline
   (lwAnomalyData) renders its own result when present; otherwise the
   queue-driven flow loads/renders anomaly for currentApplication on
   demand via POST /lender/applications/<id>/anomaly — never the old
   manual assessment form.
----------------------------------------------------------------- */
function renderLwAnomaly() {
    if (lwAnomalyData) {
        renderLwAnomalyFull(lwAnomalyData);
        return;
    }
    renderLwAnomalyForCurrentApplication();
}

function renderLwAnomalyFull(data) {
    const container = document.getElementById('lwAnomalyBody');

    if (!data || data.available === false) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">${data && data.message ? escapeLwHtml(data.message) : 'Anomaly detection is currently unavailable.'}</div>`;
        return;
    }

    const level = String(data.anomaly_level || '').toLowerCase();
    const cls = level.includes('high') ? 'state-bad' : (level.includes('medium') || level.includes('low anomaly') ? 'state-warn' : 'state-good');

    container.innerHTML = `
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Anomaly Status</div>${lwVal(data.is_anomaly ? 'Anomalous' : 'Normal', { stateClass: data.is_anomaly ? 'state-warn' : 'state-good' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Severity</div><span class="lw-badge ${cls}">${escapeLwHtml(data.anomaly_level)}</span></div>
            <div class="lw-stat-card"><div class="lw-stat-label">Confidence Score</div>${lwVal(typeof data.anomaly_score === 'number' ? data.anomaly_score.toFixed(4) : null)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Manual Review</div>${lwVal(data.manual_review ? 'Recommended' : 'Not required', { stateClass: data.manual_review ? 'state-warn' : 'state-good' })}</div>
        </div>
        <div class="lw-disclaimer" style="margin-top:16px; margin-bottom:0;">
            <i class="fa-solid fa-circle-info"></i>
            <span>An anomaly means the application is statistically unusual relative to the training population. It is not a fraud determination.</span>
        </div>
    `;
}

/* -----------------------------------------------------------------
   APPLICATION ANOMALY FOR currentApplication (PHASE 6)

   Loaded on demand via POST /lender/applications/<id>/anomaly — never
   computed in JS, never persisted. Cached only on
   currentApplication.anomaly for the lifetime of that selection;
   switching applications replaces currentApplication (and therefore
   drops the cache) before this can ever show the wrong app's result —
   same application-id staleness guard used by assess/explain/etc.
----------------------------------------------------------------- */
function renderLwAnomalyForCurrentApplication() {
    const container = document.getElementById('lwAnomalyBody');
    if (!container) return;

    if (!currentApplication) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Select an application from the queue.</div>`;
        return;
    }

    // Already loaded for this application this session — render straight
    // from the cache, no refetch.
    if (currentApplication.anomaly) {
        renderLwAnomalyResultForCurrentApplication(currentApplication.anomaly);
        return;
    }

    lwFetchAndRenderAnomaly(currentApplication.application_id);
}

async function lwFetchAndRenderAnomaly(applicationId) {
    const container = document.getElementById('lwAnomalyBody');
    if (!container) return;

    lwAnomalyInFlight = true;
    container.innerHTML = `<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Checking application anomaly...</div>`;

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/anomaly`, {
            method: 'POST'
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale anomaly result land
        // under a different currentApplication (same staleness guard
        // pattern as runLenderAssessment / lwFetchAndRenderExplanation).
        if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            renderLwAnomalyErrorForCurrentApplication(applicationId);
            return;
        }

        currentApplication.anomaly = data;
        renderLwAnomalyResultForCurrentApplication(data);
        // Keep the floating Final Underwriting Summary in sync now that
        // anomaly is cached — same staleness guard as above.
        renderLwFinalDecisionBlock();
    } catch (networkErr) {
        if (currentApplication && String(currentApplication.application_id) === String(applicationId)) {
            renderLwAnomalyErrorForCurrentApplication(applicationId);
        }
    } finally {
        lwAnomalyInFlight = false;
    }
}

function renderLwAnomalyErrorForCurrentApplication(applicationId) {
    const container = document.getElementById('lwAnomalyBody');
    if (!container) return;
    container.innerHTML = `
        <div class="lw-error-list">
            <div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>Application anomaly analysis is currently unavailable.</span></div>
        </div>
        <div class="lw-form-actions">
            <button type="button" class="lw-btn lw-btn-primary" id="lwRetryAnomalyBtn"><i class="fa-solid fa-rotate"></i> Retry</button>
        </div>
    `;
    const btn = document.getElementById('lwRetryAnomalyBtn');
    if (btn) btn.addEventListener('click', () => lwFetchAndRenderAnomaly(applicationId));
}

function renderLwAnomalyResultForCurrentApplication(data) {
    const container = document.getElementById('lwAnomalyBody');
    if (!container) return;

    if (data.available === false) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">${data.message ? escapeLwHtml(data.message) : 'Anomaly detection is currently unavailable.'}</div>`;
        return;
    }

    const level = String(data.anomaly_level || '').toLowerCase();
    const cls = level.includes('high') ? 'state-bad' : (level.includes('medium') || level.includes('low anomaly') ? 'state-warn' : 'state-good');

    container.innerHTML = `
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Anomaly Status</div>${lwVal(data.is_anomaly ? 'Anomalous' : 'Normal', { stateClass: data.is_anomaly ? 'state-warn' : 'state-good' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Severity</div><span class="lw-badge ${cls}">${escapeLwHtml(data.anomaly_level)}</span></div>
            <div class="lw-stat-card"><div class="lw-stat-label">Anomaly Score</div>${lwVal(typeof data.anomaly_score === 'number' ? data.anomaly_score.toFixed(4) : null)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Manual Review</div>${lwVal(data.manual_review ? 'Recommended' : 'Not required', { stateClass: data.manual_review ? 'state-warn' : 'state-good' })}</div>
        </div>
        <div class="lw-disclaimer" style="margin-top:16px; margin-bottom:0;">
            <i class="fa-solid fa-circle-info"></i>
            <span>An anomaly means the application is statistically unusual relative to the model's reference population. It is not a fraud determination.</span>
        </div>
    `;
}

/* -----------------------------------------------------------------
   8. SCENARIO ANALYSIS
----------------------------------------------------------------- */
function buildLwScenarioControl(id, label, baselineValue, type, min, max, step, unitSuffix) {
    // data-baseline carries the stored value (as a string) so a later
    // live-diff listener can compare the field's current value against
    // it without needing a separate lookup table. unitSuffix is purely
    // a display label (e.g. " model units") — it is never sent to the
    // backend and never changes what value is submitted.
    return `
        <div class="lw-field" data-baseline="${baselineValue ?? ''}">
            <label for="${id}">${escapeLwHtml(label)}${unitSuffix ? ` <span class="lw-panel-note" style="display:inline;">(${escapeLwHtml(unitSuffix.trim())})</span>` : ''}</label>
            <input type="${type || 'number'}" id="${id}" value="${baselineValue ?? ''}" ${min !== undefined ? `min="${min}"` : ''} ${max !== undefined ? `max="${max}"` : ''} ${step !== undefined ? `step="${step}"` : ''}>
            <div id="${id}Diff" class="lw-empty-sub" style="font-size:0.72rem; margin-top:4px; min-height:1em;"></div>
        </div>`;
}

// Wires a live "baseline → hypothetical" indicator on each scenario
// input so a changed field is visually obvious the moment it's edited
// — before Run Scenario is ever clicked. Purely cosmetic: it never
// touches currentApplication, never calls the backend, and never
// computes a risk value.
function wireLwScenarioDiffIndicators(ids, unitSuffixes) {
    ids.forEach((id) => {
        const input = document.getElementById(id);
        const diffEl = document.getElementById(id + 'Diff');
        if (!input || !diffEl) return;
        const field = input.closest('.lw-field');
        const baseline = field ? field.getAttribute('data-baseline') : '';
        const suffix = (unitSuffixes && unitSuffixes[id]) || '';
        const update = () => {
            const cur = input.value;
            if (cur === '' || cur === baseline) {
                diffEl.textContent = '';
                input.style.borderColor = '';
            } else {
                const from = baseline === '' ? '—' : baseline;
                diffEl.textContent = `${from} → ${cur}${suffix}`;
                input.style.borderColor = 'var(--warning, #f59e0b)';
            }
        };
        input.addEventListener('input', update);
        update();
    });
}

function initLwScenario() {
    const container = document.getElementById('lwScenarioControls');
    if (!lwCurrentApplicant) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Submit an application first — scenario analysis compares against that baseline.</div>`;
        return;
    }
    const a = lwCurrentApplicant;
    container.innerHTML = `
        <div class="lw-scenario-controls">
            ${buildLwScenarioControl('lwScenarioDuration', 'Loan Duration (months)', a.duration_months, 'number', 1, undefined, 1)}
            ${buildLwScenarioControl('lwScenarioAmount', 'Credit Amount', a.credit_amount, 'number', 0, undefined, 1)}
            ${buildLwScenarioControl('lwScenarioInstallment', 'Installment Rate', a.installment_rate, 'number', 1, 4, 1)}
            ${buildLwScenarioControl('lwScenarioExisting', 'Existing Credits', a.existing_credits, 'number', 0, undefined, 1)}
        </div>
        <button type="button" id="lwRunScenarioBtn" class="lw-btn lw-btn-primary"><i class="fa-solid fa-play"></i> Run Scenario</button>
        <div id="lwScenarioErrors" class="lw-error-list hidden" style="margin-top:14px;"></div>
        <div id="lwScenarioResult" style="margin-top:18px;"></div>
    `;
    document.getElementById('lwRunScenarioBtn').addEventListener('click', runLwScenario);
}

async function runLwScenario() {
    if (!lwCurrentApplicant) return;
    const changes = {};
    const duration = document.getElementById('lwScenarioDuration');
    const amount = document.getElementById('lwScenarioAmount');
    const installment = document.getElementById('lwScenarioInstallment');
    const existing = document.getElementById('lwScenarioExisting');

    if (duration && duration.value !== '') changes.duration_months = Number(duration.value);
    if (amount && amount.value !== '') changes.credit_amount = Number(amount.value);
    if (installment && installment.value !== '') changes.installment_rate = Number(installment.value);
    if (existing && existing.value !== '') changes.existing_credits = Number(existing.value);

    const errBox = document.getElementById('lwScenarioErrors');
    const resultBox = document.getElementById('lwScenarioResult');
    errBox.classList.add('hidden');
    resultBox.innerHTML = `<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Running scenario...</div>`;

    try {
        const res = await lwRequest('/api/credit-risk/scenario', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ applicant: lwCurrentApplicant, changes })
        });
        const data = await res.json();

        if (!res.ok || data.status !== 'success') {
            const msgs = Array.isArray(data.errors) ? data.errors : [data.message || 'Scenario analysis failed.'];
            errBox.innerHTML = msgs.map(m => `<div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>${escapeLwHtml(m)}</span></div>`).join('');
            errBox.classList.remove('hidden');
            resultBox.innerHTML = '';
            return;
        }
        renderLwScenarioResult(data);
    } catch (e) {
        errBox.innerHTML = `<div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>Couldn't reach the scenario service. Try again.</span></div>`;
        errBox.classList.remove('hidden');
        resultBox.innerHTML = '';
    }
}

function renderLwScenarioResult(data) {
    const baselineRiskClass = lwRiskClass(data.baseline_risk_level);
    const scenarioRiskClass = lwRiskClass(data.scenario_risk_level);
    const deltaPp = data.delta * 100;
    const deltaLabel = deltaPp > 0 ? `+${deltaPp.toFixed(2)} pp` : `${deltaPp.toFixed(2)} pp`;

    document.getElementById('lwScenarioResult').innerHTML = `
        <div class="lw-compare-row">
            <div class="lw-compare-card">
                <div class="lw-compare-label">Baseline</div>
                <div class="lw-risk-probability" style="font-size:1.5rem;">${(data.baseline_probability * 100).toFixed(1)}%</div>
                <div class="lw-risk-level ${baselineRiskClass}">${escapeLwHtml(data.baseline_risk_level)}</div>
                <div style="margin-top:8px;"><span class="lw-decision-badge ${lwDecisionClass(data.baseline_decision)}" style="font-size:0.78rem; padding:6px 12px;">${escapeLwHtml(data.baseline_decision)}</span></div>
            </div>
            <div class="lw-compare-arrow"><i class="fa-solid fa-arrow-right"></i></div>
            <div class="lw-compare-card">
                <div class="lw-compare-label">Scenario</div>
                <div class="lw-risk-probability" style="font-size:1.5rem;">${(data.scenario_probability * 100).toFixed(1)}%</div>
                <div class="lw-risk-level ${scenarioRiskClass}">${escapeLwHtml(data.scenario_risk_level)}</div>
                <div style="margin-top:8px;"><span class="lw-decision-badge ${lwDecisionClass(data.scenario_decision)}" style="font-size:0.78rem; padding:6px 12px;">${escapeLwHtml(data.scenario_decision)}</span></div>
            </div>
        </div>
        <div class="lw-disclaimer" style="margin-top:16px; margin-bottom:0;">
            <i class="fa-solid fa-circle-info"></i>
            <span>Change: ${deltaLabel}. ${escapeLwHtml(data.interpretation)} Scenario results are model-estimated outcomes, not guaranteed financial outcomes.</span>
        </div>
    `;
}

/* -----------------------------------------------------------------
   SCENARIO ANALYSIS FOR currentApplication (PHASE 7)

   Dispatcher, same pattern as renderLwAssessmentOverview /
   renderLwExplainability / renderLwAffordability / renderLwBehavior:
   the legacy manual-form pipeline (lwCurrentApplicant) renders its own
   scenario UI when present; otherwise the queue-driven flow builds
   scenario controls from currentApplication's own stored data and
   runs scenarios against POST /lender/applications/<id>/scenario —
   never against lwCurrentApplicant, which is not the authoritative
   source for a submitted application.

   The baseline shown/edited here is currentApplication.scenarioBaseline
   (the application's own stored duration_months / credit_amount /
   installment_rate / existing_credits, as persisted in
   loan_applications.application_data). Running a scenario never writes
   to that row — it's a read-only what-if call every time.

   Both the controls and any run result are cached only on
   currentApplication (scenarioBaseline / scenarioResult), so switching
   tabs preserves them but switching applications — which replaces
   currentApplication wholesale — always drops them first.
----------------------------------------------------------------- */
function renderLwScenario() {
    if (lwCurrentApplicant) {
        initLwScenario();
        return;
    }
    renderLwScenarioForCurrentApplication();
}

function renderLwScenarioForCurrentApplication() {
    const container = document.getElementById('lwScenarioControls');
    if (!container) return;

    if (!currentApplication) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Select an application from the queue.</div>`;
        return;
    }

    const baseline = currentApplication.scenarioBaseline;
    if (!baseline) {
        container.innerHTML = `<div class="lw-empty-sub" style="text-align:center; padding:24px;">Scenario baseline is unavailable for this application.</div>`;
        return;
    }

    container.innerHTML = `
        <div class="lw-empty-sub" style="margin-bottom:16px;">Model-estimated outcome under a hypothetical scenario — this is advisory only and never changes the submitted application. Final lending authority remains with you.</div>
        <div class="lw-panel-title" style="margin-bottom:8px;"><i class="fa-solid fa-file-invoice"></i> Current Application</div>
        <div class="lw-stat-row" style="margin-bottom:18px;">
            <div class="lw-stat-card"><div class="lw-stat-label">Loan Tenure</div>${lwVal(baseline.duration_months, { suffix: ' months' })}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Loan Amount</div>${lwVal(lwFormatModelUnits(baseline.credit_amount))}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Installment Commitment</div>${lwVal(baseline.installment_rate)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Existing Credit Lines</div>${lwVal(baseline.existing_credits)}</div>
        </div>
        <div class="lw-panel-title" style="margin-bottom:8px;"><i class="fa-solid fa-flask"></i> Hypothetical Scenario</div>
        <div class="lw-scenario-hypo-note"><i class="fa-solid fa-triangle-exclamation"></i> These values do not modify the submitted application.</div>
        <div class="lw-scenario-controls">
            ${buildLwScenarioControl('lwqScenarioDuration', 'Loan Tenure', baseline.duration_months, 'number', 1, undefined, 1, ' months')}
            ${buildLwScenarioControl('lwqScenarioAmount', 'Loan Amount', baseline.credit_amount, 'number', 0, undefined, 1, ' model units')}
            ${buildLwScenarioControl('lwqScenarioInstallment', 'Installment Commitment', baseline.installment_rate, 'number', 1, 4, 1)}
            ${buildLwScenarioControl('lwqScenarioExisting', 'Existing Credit Lines', baseline.existing_credits, 'number', 0, undefined, 1)}
        </div>
        <div class="lw-form-actions" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <button type="button" id="lwqRunScenarioBtn" class="lw-btn lw-btn-primary"><i class="fa-solid fa-play"></i> Run Scenario</button>
            <button type="button" id="lwqResetScenarioBtn" class="lw-btn" style="font-size:0.82rem;"><i class="fa-solid fa-rotate-left"></i> Reset to Current Application</button>
        </div>
        <div class="lw-disclaimer" style="margin-top:14px; margin-bottom:0;">
            <i class="fa-solid fa-circle-info"></i>
            <span>This is a hypothetical model simulation. It does not modify the application or guarantee a future lending decision.</span>
        </div>
        <div id="lwqScenarioErrors" class="lw-error-list hidden" style="margin-top:14px;"></div>
        <div id="lwqScenarioResult" style="margin-top:18px;"></div>
    `;

    const scenarioInputIds = ['lwqScenarioDuration', 'lwqScenarioAmount', 'lwqScenarioInstallment', 'lwqScenarioExisting'];
    wireLwScenarioDiffIndicators(scenarioInputIds, {
        lwqScenarioDuration: ' months',
        lwqScenarioAmount: ' model units'
    });

    const btn = document.getElementById('lwqRunScenarioBtn');
    if (btn) btn.addEventListener('click', () => runLwScenarioForCurrentApplication(currentApplication.application_id));

    const resetBtn = document.getElementById('lwqResetScenarioBtn');
    if (resetBtn) resetBtn.addEventListener('click', resetLwScenarioForCurrentApplication);

    // Switching tabs (not applications) must not lose an already-run
    // result for this application — re-render it from the cache.
    if (currentApplication.scenarioResult) {
        renderLwScenarioResultForCurrentApplication(currentApplication.scenarioResult);
    }
}

// "Reset to Current Application" — restores the four scenario inputs
// to the stored baseline and clears any previous scenario result. This
// only touches the in-memory scenario controls/cache; it never reads
// from or writes to currentApplication.application_data, so the actual
// submitted application is untouched either way.
function resetLwScenarioForCurrentApplication() {
    if (!currentApplication || !currentApplication.scenarioBaseline) return;
    const baseline = currentApplication.scenarioBaseline;

    const fields = [
        ['lwqScenarioDuration', baseline.duration_months],
        ['lwqScenarioAmount', baseline.credit_amount],
        ['lwqScenarioInstallment', baseline.installment_rate],
        ['lwqScenarioExisting', baseline.existing_credits],
    ];
    fields.forEach(([id, value]) => {
        const input = document.getElementById(id);
        if (input) {
            input.value = value ?? '';
            input.style.borderColor = '';
        }
        const diffEl = document.getElementById(id + 'Diff');
        if (diffEl) diffEl.textContent = '';
    });

    const errBox = document.getElementById('lwqScenarioErrors');
    if (errBox) {
        errBox.innerHTML = '';
        errBox.classList.add('hidden');
    }
    const resultBox = document.getElementById('lwqScenarioResult');
    if (resultBox) resultBox.innerHTML = '';

    currentApplication.scenarioResult = null;
}

async function runLwScenarioForCurrentApplication(applicationId) {
    const errBox = document.getElementById('lwqScenarioErrors');
    const resultBox = document.getElementById('lwqScenarioResult');
    if (!errBox || !resultBox) return;

    const duration = document.getElementById('lwqScenarioDuration');
    const amount = document.getElementById('lwqScenarioAmount');
    const installment = document.getElementById('lwqScenarioInstallment');
    const existing = document.getElementById('lwqScenarioExisting');

    // Only these four scenario changes are ever sent — the stored
    // application itself is the baseline and is never re-sent or
    // replaced by this request.
    const changes = {};
    if (duration && duration.value !== '') changes.duration_months = Number(duration.value);
    if (amount && amount.value !== '') changes.credit_amount = Number(amount.value);
    if (installment && installment.value !== '') changes.installment_rate = Number(installment.value);
    if (existing && existing.value !== '') changes.existing_credits = Number(existing.value);

    errBox.classList.add('hidden');
    resultBox.innerHTML = `<div class="lw-loading"><i class="fa-solid fa-spinner fa-spin"></i> Running scenario...</div>`;

    try {
        const res = await lwRequest(`/lender/applications/${encodeURIComponent(applicationId)}/scenario`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ changes })
        });

        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        // The lender may have opened a different application while this
        // request was in flight — never let a stale scenario result
        // land under a different currentApplication.
        if (!currentApplication || String(currentApplication.application_id) !== String(applicationId)) return;

        if (!res.ok || !data || data.status !== 'success') {
            renderLwScenarioErrorForCurrentApplication(applicationId, data);
            return;
        }

        currentApplication.scenarioResult = data;
        renderLwScenarioResultForCurrentApplication(data);
    } catch (networkErr) {
        if (currentApplication && String(currentApplication.application_id) === String(applicationId)) {
            renderLwScenarioErrorForCurrentApplication(applicationId, null);
        }
    }
}

function renderLwScenarioErrorForCurrentApplication(applicationId, data) {
    const errBox = document.getElementById('lwqScenarioErrors');
    const resultBox = document.getElementById('lwqScenarioResult');
    if (!errBox || !resultBox) return;

    const msgs = data && Array.isArray(data.errors)
        ? data.errors
        : ['Scenario analysis is currently unavailable.'];
    errBox.innerHTML = msgs.map(m => `<div class="lw-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>${escapeLwHtml(m)}</span></div>`).join('');
    errBox.classList.remove('hidden');
    resultBox.innerHTML = `
        <div class="lw-form-actions">
            <button type="button" class="lw-btn lw-btn-primary" id="lwqRetryScenarioBtn"><i class="fa-solid fa-rotate"></i> Retry</button>
        </div>`;
    const retryBtn = document.getElementById('lwqRetryScenarioBtn');
    if (retryBtn) retryBtn.addEventListener('click', () => runLwScenarioForCurrentApplication(applicationId));
}

function renderLwScenarioResultForCurrentApplication(data) {
    const container = document.getElementById('lwqScenarioResult');
    if (!container) return;

    const baselineRiskClass = lwRiskClass(data.baseline_risk_level);
    const scenarioRiskClass = lwRiskClass(data.scenario_risk_level);
    const deltaPp = data.delta * 100;
    const deltaLabel = Math.abs(deltaPp) < 0.05
        ? 'No change'
        : (deltaPp > 0 ? `+${deltaPp.toFixed(1)} pp` : `${deltaPp.toFixed(1)} pp`);

    container.innerHTML = `
        <div class="lw-compare-row">
            <div class="lw-compare-card lw-compare-current">
                <span class="lw-compare-tag lw-compare-tag-current">Current Application</span>
                <div class="lw-risk-probability" style="font-size:1.5rem;">${(data.baseline_probability * 100).toFixed(1)}%</div>
                <div class="lw-risk-level ${baselineRiskClass}">${escapeLwHtml(data.baseline_risk_level)}</div>
                <div style="margin-top:8px;"><span class="lw-decision-badge ${lwDecisionClass(data.baseline_decision)}" style="font-size:0.78rem; padding:6px 12px;">${escapeLwHtml(data.baseline_decision)}</span></div>
            </div>
            <div class="lw-compare-arrow"><i class="fa-solid fa-arrow-right"></i></div>
            <div class="lw-compare-card lw-compare-hypothetical">
                <span class="lw-compare-tag lw-compare-tag-hypothetical">Hypothetical Scenario</span>
                <div class="lw-risk-probability" style="font-size:1.5rem;">${(data.scenario_probability * 100).toFixed(1)}%</div>
                <div class="lw-risk-level ${scenarioRiskClass}">${escapeLwHtml(data.scenario_risk_level)}</div>
                <div style="margin-top:8px;"><span class="lw-decision-badge ${lwDecisionClass(data.scenario_decision)}" style="font-size:0.78rem; padding:6px 12px;">${escapeLwHtml(data.scenario_decision)}</span></div>
            </div>
        </div>
        <div class="lw-stat-card" style="margin-top:14px; max-width:240px;">
            <div class="lw-stat-label">Risk Change</div>
            <div class="lw-stat-value">${deltaLabel}</div>
        </div>
        <div class="lw-disclaimer" style="margin-top:16px; margin-bottom:0;">
            <i class="fa-solid fa-circle-info"></i>
            <span>${escapeLwHtml(data.interpretation)} These hypothetical values do not modify the submitted application.</span>
        </div>
    `;
}

/* -----------------------------------------------------------------
   9. RESPONSIBLE AI
----------------------------------------------------------------- */
async function loadLwResponsibleAi() {
    const container = document.getElementById('lwResponsibleBody');
    try {
        const res = await lwRequest('/api/credit-risk/responsible-ai', { method: 'GET' });
        const data = await res.json();
        if (!res.ok || data.status === 'error') {
            container.innerHTML = `
                <div class="lw-panel-header"><div class="lw-panel-title"><i class="fa-solid fa-scale-balanced"></i> Responsible AI Monitoring</div></div>
                <div class="lw-empty-sub" style="text-align:center; padding:24px;">Responsible AI data is currently unavailable.</div>`;
            return;
        }
        renderLwResponsibleAi(data);
    } catch (e) {
        container.innerHTML = `
            <div class="lw-panel-header"><div class="lw-panel-title"><i class="fa-solid fa-scale-balanced"></i> Responsible AI Monitoring</div></div>
            <div class="lw-empty-sub" style="text-align:center; padding:24px;">Couldn't reach the Responsible AI service.</div>`;
    }
}

function renderLwResponsibleAi(data) {
    const container = document.getElementById('lwResponsibleBody');
    const perf = data.model_performance || {};
    const fairness = data.fairness || {};

    let fairnessHtml = `<div class="lw-empty-sub">Fairness data not currently available.</div>`;
    if (fairness && fairness.available && Array.isArray(fairness.groups)) {
        fairnessHtml = `
            <table class="lw-table">
                <thead><tr><th>Attribute</th><th>Group</th><th>Metric</th><th>Value</th></tr></thead>
                <tbody>
                    ${fairness.groups.map(g => `<tr><td>${escapeLwHtml(g.attribute)}</td><td>${escapeLwHtml(g.group)}</td><td>${escapeLwHtml(g.metric)}</td><td>${escapeLwHtml(g.value)}</td></tr>`).join('')}
                </tbody>
            </table>`;
    } else if (fairness && fairness.message) {
        fairnessHtml = `<div class="lw-empty-sub">${escapeLwHtml(fairness.message)}</div>`;
    }

    container.innerHTML = `
        <div class="lw-panel-header">
            <div class="lw-panel-title"><i class="fa-solid fa-scale-balanced"></i> Responsible AI Monitoring
                <span class="lw-badge">Dataset / Model Level</span>
            </div>
            <div class="lw-panel-note">/api/credit-risk/responsible-ai — dataset-level, offline</div>
        </div>
        <div class="lw-disclaimer">
            <i class="fa-solid fa-triangle-exclamation"></i>
            <span>These are dataset-level fairness metrics, computed offline against the training population. They are <strong>not</strong> an individual protected-attribute verdict for this applicant.</span>
        </div>
        <div class="lw-stat-row">
            <div class="lw-stat-card"><div class="lw-stat-label">Accuracy</div>${lwVal(perf.accuracy)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">ROC AUC</div>${lwVal(perf.roc_auc)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">PR AUC</div>${lwVal(perf.pr_auc)}</div>
            <div class="lw-stat-card"><div class="lw-stat-label">Brier Score</div>${lwVal(perf.brier_score)}</div>
        </div>
        <div style="margin-top:18px;">
            <div class="lw-panel-title" style="margin-bottom:10px;"><i class="fa-solid fa-users"></i> Fairness by Group</div>
            ${fairnessHtml}
        </div>
    `;
}

/* =================================================================
   AI RISK ANALYST (PHASE 9)

   Frontend controller for the read-only Q&A panel over an
   application's ALREADY-PERSISTED assessment_result. This file never
   computes a risk score or a decision — every answer comes verbatim
   from POST /lender/risk-assistant (services/risk_assistant_service.py),
   which itself only explains existing, already-verified evidence.

   Scoping rules enforced here:
     - The trigger is only shown once currentApplication.assessment_result
       is present (see lwUpdateContextStrip).
     - lwSetCurrentApplication() always closes this panel and drops its
       history BEFORE currentApplication is reassigned, so a different
       (or cleared) application can never inherit a stale conversation.
     - Every request sent from this panel is scoped to
       currentApplication.application_id, re-read at send time — never
       cached from when the panel was opened.
     - No request is ever fired automatically; the panel only calls the
       backend in response to an explicit lender click.
================================================================== */

const LW_RA_SUGGESTED_QUESTIONS = [
    'Why is this applicant risky?',
    'What is the strongest factor driving this risk?',
    'What are the strongest risk factors?',
    'Can this borrower afford the requested loan?',
    'What evidence supports this assessment?',
    'What should I verify before approving?'
];

let lwRaInFlight = false;
let lwRaMessages = [];          // [{ role: 'question'|'answer'|'error', text }]
let lwRaMessagesAppId = null;   // application_id these messages belong to

function initLwRiskAssistant() {
    const trigger = document.getElementById('lwAskRiskAnalystBtn');
    const closeBtn = document.getElementById('lwRaCloseBtn');
    const overlay = document.getElementById('lwRiskAssistantModal');
    const askBtn = document.getElementById('lwRaAskBtn');
    const input = document.getElementById('lwRaQuestionInput');

    if (trigger) trigger.addEventListener('click', lwOpenRiskAssistant);
    if (closeBtn) closeBtn.addEventListener('click', lwCloseRiskAssistant);
    if (overlay) {
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) lwCloseRiskAssistant();
        });
    }
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && overlay && overlay.style.display !== 'none') {
            lwCloseRiskAssistant();
        }
    });

    if (askBtn) askBtn.addEventListener('click', () => lwAskRiskAssistant());
    if (input) {
        // Enter sends; Shift+Enter inserts a newline, matching standard
        // chat-composer behavior.
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                lwAskRiskAssistant();
            }
        });
        input.addEventListener('input', () => {
            // Auto-grow up to a small cap so a long question stays
            // readable without the composer taking over the panel.
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        });
    }
}

function lwOpenRiskAssistant() {
    // Guard against the trigger being clicked in a state it shouldn't
    // even be visible in (no application, or no persisted assessment).
    if (!currentApplication || !currentApplication.assessment_result) return;

    const overlay = document.getElementById('lwRiskAssistantModal');
    if (!overlay) return;

    // A fresh application (never opened in this panel before) starts
    // with a clean transcript; re-opening the SAME application keeps
    // its in-memory history for this session.
    if (lwRaMessagesAppId !== currentApplication.application_id) {
        lwRaMessages = [];
        lwRaMessagesAppId = currentApplication.application_id;
    }

    const label = document.getElementById('lwRaAppLabel');
    if (label) {
        const name = currentApplication.borrower && currentApplication.borrower.name;
        label.textContent = `Application #${currentApplication.application_id}` + (name ? ` — ${name}` : '');
    }

    lwRenderRaSuggested();
    lwRenderRaLog();

    const errBox = document.getElementById('lwRaComposerError');
    if (errBox) { errBox.textContent = ''; errBox.classList.add('hidden'); }

    overlay.style.display = 'flex';
    const panel = overlay.querySelector('.lw-ra-panel');
    if (panel) panel.focus();

    const input = document.getElementById('lwRaQuestionInput');
    if (input) input.focus();
}

function lwCloseRiskAssistant() {
    const overlay = document.getElementById('lwRiskAssistantModal');
    if (overlay) overlay.style.display = 'none';
}

function lwRenderRaSuggested() {
    const mount = document.getElementById('lwRaSuggested');
    if (!mount) return;
    mount.innerHTML = LW_RA_SUGGESTED_QUESTIONS.map(q =>
        `<button type="button" class="lw-ra-chip" data-q="${escapeLwHtml(q)}">${escapeLwHtml(q)}</button>`
    ).join('');
    mount.querySelectorAll('.lw-ra-chip').forEach(btn => {
        btn.addEventListener('click', () => lwAskRiskAssistant(btn.getAttribute('data-q')));
    });
}

function lwRenderRaLog() {
    const log = document.getElementById('lwRaLog');
    if (!log) return;

    if (!lwRaMessages.length) {
        log.innerHTML = `
            <div class="lw-ra-empty">
                <i class="fa-solid fa-magnifying-glass-chart"></i>
                <div>Ask a question about this application's assessment, or pick one above.</div>
            </div>`;
        return;
    }

    log.innerHTML = lwRaMessages.map(m => {
        if (m.role === 'question') {
            return `<div class="lw-ra-msg lw-ra-msg-question"><div class="lw-ra-bubble">${escapeLwHtml(m.text)}</div></div>`;
        }
        if (m.role === 'loading') {
            return `
                <div class="lw-ra-msg lw-ra-msg-answer" id="lwRaLoadingMsg">
                    <div class="lw-ra-bubble lw-ra-loading">
                        <span class="lw-ra-dot"></span><span class="lw-ra-dot"></span><span class="lw-ra-dot"></span>
                    </div>
                </div>`;
        }
        if (m.role === 'error') {
            return `
                <div class="lw-ra-msg lw-ra-msg-answer">
                    <div class="lw-ra-bubble lw-ra-bubble-error">
                        <i class="fa-solid fa-circle-exclamation"></i> ${escapeLwHtml(m.text)}
                    </div>
                </div>`;
        }
        return `<div class="lw-ra-msg lw-ra-msg-answer"><div class="lw-ra-bubble"><i class="fa-solid fa-user-shield lw-ra-bubble-icon"></i><div class="lw-ra-answer-content">${lwRenderRaMarkdown(m.text)}</div></div></div>`;
    }).join('');

    log.scrollTop = log.scrollHeight;
}

// Renders a Risk Analyst answer as safe, compact HTML.
//
// SAFETY: escapeLwHtml() runs FIRST on the raw text, so any characters
// the AI response contains are neutralized before any markup exists.
// Only the literal <p>/<ul>/<li>/<strong> tags added below (by us, not
// by the AI text) ever end up in the DOM — the AI's own text can never
// introduce a tag, attribute, or script.
function lwRenderRaMarkdown(text) {
    const escaped = escapeLwHtml(text);

    // **bold** -> <strong>bold</strong>
    const withBold = escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    const lines = withBold.split(/\r?\n/);
    const blocks = [];
    let currentList = null;

    const flushList = () => {
        if (currentList && currentList.length) {
            blocks.push(`<ul class="lw-ra-list">${currentList.map(li => `<li>${li}</li>`).join('')}</ul>`);
        }
        currentList = null;
    };

    lines.forEach(line => {
        const trimmed = line.trim();
        const bulletMatch = trimmed.match(/^-\s+(.+)$/);
        if (bulletMatch) {
            if (!currentList) currentList = [];
            currentList.push(bulletMatch[1]);
            return;
        }
        flushList();
        if (trimmed) blocks.push(`<p class="lw-ra-p">${trimmed}</p>`);
    });
    flushList();

    return blocks.join('') || escaped;
}

async function lwAskRiskAssistant(presetQuestion) {
    if (lwRaInFlight) return;
    if (!currentApplication || !currentApplication.assessment_result) return;

    // Re-read the scoped application id at send time (not captured
    // earlier) so a question can never be attributed to whichever
    // application was current when the panel was first opened.
    const applicationId = currentApplication.application_id;

    const input = document.getElementById('lwRaQuestionInput');
    const errBox = document.getElementById('lwRaComposerError');
    const askBtn = document.getElementById('lwRaAskBtn');

    const question = (typeof presetQuestion === 'string' ? presetQuestion : (input ? input.value : '')).trim();

    if (errBox) { errBox.textContent = ''; errBox.classList.add('hidden'); }

    if (!question) {
        if (errBox) { errBox.textContent = 'Enter a question first.'; errBox.classList.remove('hidden'); }
        return;
    }
    if (question.length > 500) {
        if (errBox) { errBox.textContent = 'Question is too long (500 characters max).'; errBox.classList.remove('hidden'); }
        return;
    }

    lwRaInFlight = true;
    if (askBtn) askBtn.disabled = true;
    if (input) { input.value = ''; input.style.height = 'auto'; }

    lwRaMessages.push({ role: 'question', text: question });
    lwRaMessages.push({ role: 'loading' });
    lwRenderRaLog();

    let data = null;
    let networkError = false;
    try {
        const res = await lwRequest('/lender/risk-assistant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ application_id: applicationId, question })
        });
        data = await res.json().catch(() => null);
    } catch (e) {
        networkError = true;
    }

    // The application (or the whole panel) may have changed while this
    // request was in flight. If so, drop the response silently instead
    // of grafting an answer onto a conversation it no longer belongs to.
    if (lwRaMessagesAppId !== applicationId) {
        lwRaInFlight = false;
        if (askBtn) askBtn.disabled = false;
        return;
    }

    // Replace the loading placeholder with the real result.
    lwRaMessages = lwRaMessages.filter(m => m.role !== 'loading');

    if (networkError || !data) {
        lwRaMessages.push({ role: 'error', text: 'The AI risk analyst is temporarily unavailable. Please try again.' });
    } else if (data.success && typeof data.answer === 'string' && data.answer.trim()) {
        lwRaMessages.push({ role: 'answer', text: data.answer.trim() });
    } else {
        lwRaMessages.push({ role: 'error', text: (data && data.error) || 'The AI risk analyst returned an unexpected response.' });
    }

    lwRenderRaLog();
    lwRaInFlight = false;
    if (askBtn) askBtn.disabled = false;
    if (input) input.focus();
}

/* =================================================================
   VISUAL POLISH — presentation-only entrance animations

   Purely cosmetic. Does not read/derive/alter any risk value, SHAP
   number, decision, or API payload — it only animates the *rendering*
   of values (gauge stroke-dashoffset, bar widths) that the functions
   above already computed and set as the final inline style/attribute.
   Uses a MutationObserver so it works uniformly across every render
   path (initial load, tab switch, re-run, retry) without touching the
   render functions themselves.
================================================================== */
(function lwInitVisualPolish() {
    function lwReducedMotion() {
        return typeof window.matchMedia === 'function' &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    }

    function lwAnimateWidthFill(el, delayMs) {
        const targetWidth = el.style.width;
        if (!targetWidth || targetWidth === '0%') return;
        el.style.transition = 'none';
        el.style.width = '0%';
        void el.getBoundingClientRect(); // force reflow so the 0% state paints first
        requestAnimationFrame(() => {
            el.style.transition = `width .6s cubic-bezier(0.16, 1, 0.3, 1) ${delayMs || 0}ms`;
            requestAnimationFrame(() => { el.style.width = targetWidth; });
        });
    }

    function lwAnimateGaugeCircle(circle) {
        const target = circle.getAttribute('stroke-dashoffset');
        const dasharray = circle.getAttribute('stroke-dasharray');
        if (target === null || dasharray === null) return;
        circle.style.transition = 'none';
        circle.setAttribute('stroke-dashoffset', dasharray); // start empty
        void circle.getBoundingClientRect();
        requestAnimationFrame(() => {
            circle.style.transition = 'stroke-dashoffset .7s cubic-bezier(0.16, 1, 0.3, 1)';
            requestAnimationFrame(() => { circle.setAttribute('stroke-dashoffset', target); });
        });
    }

    function lwScanAndAnimate(node) {
        if (!(node instanceof Element)) return;
        if (lwReducedMotion()) return;

        const gauges = node.matches('.lw-risk-gauge-fill') ? [node] : Array.from(node.querySelectorAll('.lw-risk-gauge-fill'));
        gauges.forEach(lwAnimateGaugeCircle);

        const simpleSel = '.lw-probability-fill, .lw-cashflow-bar-segment';
        const simpleFills = (node.matches(simpleSel) ? [node] : []).concat(Array.from(node.querySelectorAll(simpleSel)));
        simpleFills.forEach(el => lwAnimateWidthFill(el, 0));

        const factorSel = '.lw-factor-bar-fill';
        const factorFills = (node.matches(factorSel) ? [node] : []).concat(Array.from(node.querySelectorAll(factorSel)));
        factorFills.forEach((el, i) => lwAnimateWidthFill(el, i * 45));
    }

    const observer = new MutationObserver(mutations => {
        mutations.forEach(m => {
            m.addedNodes.forEach(n => lwScanAndAnimate(n));
        });
    });

    document.addEventListener('DOMContentLoaded', () => {
        const root = document.querySelector('.lender-workspace');
        if (!root) return;
        observer.observe(root, { childList: true, subtree: true });
    });
})();