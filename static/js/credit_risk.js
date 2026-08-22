'use strict';

/* ================================================================
   CREDIT RISK PAGE CONTROLLER
   Integrates:
   - Base Assessment
   - SHAP Explanations
   - Scenario Analysis
   - Application Anomaly Check 
   - Responsible AI / Fairness Monitoring
   - Financial Behavior
   - Affordability Assessment 
   - Unified Assessment Overview (Phase 9)
================================================================ */

let caPageInitialized = false;
let caSubmitInFlight = false;
let currentBaselineApplicant = null;

// Caches for the current assessment state
let currentAssessData = null;
let currentBehaviorData = null;
let currentAffordData = null;
let currentAnomalyData = null;

let behaviorLoaded = false;
let lastAffordabilityPayloadStr = null;
let isAffordabilityLoading = false;

const CA_FIELD_IDS = [
    'ca_duration_months', 'ca_credit_amount', 'ca_purpose', 'ca_installment_rate',
    'ca_credit_history', 'ca_existing_credits', 'ca_checking_account', 'ca_savings_account',
    'ca_other_debtors', 'ca_property', 'ca_other_installment_plans', 'ca_housing',
    'ca_employment_since', 'ca_personal_status_sex', 'ca_residence_since', 'ca_age',
    'ca_dependents', 'ca_job', 'ca_telephone', 'ca_foreign_worker'
];

const CA_NUMERIC_IDS = new Set([
    'ca_duration_months', 'ca_credit_amount', 'ca_installment_rate',
    'ca_existing_credits', 'ca_residence_since', 'ca_age', 'ca_dependents'
]);

/* ---------------------------------------------------------------
   INITIALIZATION & NAVIGATION
--------------------------------------------------------------- */
function initCreditAssessmentPage() {
    const form = document.getElementById('creditAssessmentForm');
    if (!form) return;

    if (caPageInitialized) return;
    caPageInitialized = true;

    initCreditAssessmentSections();
    form.addEventListener('submit', handleCreditAssessmentSubmit);

    const resetBtn = document.getElementById('resetCreditBtn');
    if (resetBtn) resetBtn.addEventListener('click', handleCreditAssessmentReset);

    renderCreditEmptyState();

    // Load dataset-level Responsible AI data independently on page init
    loadResponsibleAiData();
}

function initCreditAssessmentSections() {
    const navItems = document.querySelectorAll('.ca-nav-item');
    navItems.forEach(btn => {
        btn.addEventListener('click', (e) => {
            navItems.forEach(b => b.classList.remove('active'));
            e.currentTarget.classList.add('active');

            const target = e.currentTarget.getAttribute('data-section');
            document.querySelectorAll('.ca-section').forEach(sec => {
                if (sec.getAttribute('data-section') === target) {
                    sec.classList.remove('ca-section-hidden');
                    sec.classList.add('ca-section-active');

                    // Lazy loads if navigated to manually before an assessment
                    if (target === 'behavior' && !behaviorLoaded) {
                        loadFinancialBehavior();
                    } else if (target === 'affordability' && !currentAffordData) {
                        checkAndLoadAffordability();
                    }
                } else {
                    sec.classList.remove('ca-section-active');
                    sec.classList.add('ca-section-hidden');
                }
            });
        });
    });
}

function switchCaTab(sectionId) {
    const tab = document.querySelector(`.ca-nav-item[data-section="${sectionId}"]`);
    if (tab) tab.click();
}

/* ---------------------------------------------------------------
   UTILITIES & FORM HANDLING
--------------------------------------------------------------- */
async function caRequest(url, options) {
    if (typeof authFetch === 'function') return authFetch(url, options);
    return fetch(url, Object.assign({ credentials: 'same-origin' }, options));
}

async function safeJsonFetch(url, options) {
    try {
        const res = await caRequest(url, options);
        if (!res.ok) return null;
        const data = await res.json();
        return data.status === 'success' ? data : null;
    } catch (e) {
        return null;
    }
}

function caApiFieldName(inputId) { return inputId.replace(/^ca_/, ''); }

function caLabelFor(inputId) {
    const label = document.querySelector(`label[for="${inputId}"]`);
    return label ? label.textContent.replace(/\s+/g, ' ').trim() : inputId;
}

function escapeCaHtml(str) {
    const div = document.createElement('div');
    div.textContent = String(str == null ? '' : str);
    return div.innerHTML;
}

function caRiskClass(riskLevel) {
    const normalized = String(riskLevel || '').toLowerCase();
    if (normalized.includes('low')) return 'ca-risk-low';
    if (normalized.includes('high')) return 'ca-risk-high';
    return 'ca-risk-medium';
}

function caDecisionClass(decision) {
    const normalized = String(decision || '').toLowerCase();
    if (normalized.includes('approve')) return 'ca-decision-approve';
    if (normalized.includes('reject')) return 'ca-decision-reject';
    return 'ca-decision-review';
}

function gatherCreditFormData() {
    const payload = {};
    CA_FIELD_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const raw = el.value;
        if (CA_NUMERIC_IDS.has(id)) {
            payload[caApiFieldName(id)] = raw === '' ? null : Number(raw);
        } else {
            payload[caApiFieldName(id)] = raw;
        }
    });
    return payload;
}

function validateCreditForm() {
    const errors = [];
    CA_FIELD_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const value = el.value;
        if (value === '' || value === null) {
            errors.push({ field: id, message: `${caLabelFor(id)} is required.` });
            return;
        }
        if (CA_NUMERIC_IDS.has(id)) {
            const num = Number(value);
            if (Number.isNaN(num)) {
                errors.push({ field: id, message: `${caLabelFor(id)} must be a number.` });
                return;
            }
            const min = el.hasAttribute('min') ? Number(el.min) : null;
            const max = el.hasAttribute('max') ? Number(el.max) : null;
            if (min !== null && num < min) {
                errors.push({ field: id, message: `${caLabelFor(id)} must be at least ${min}.` });
            } else if (max !== null && num > max) {
                errors.push({ field: id, message: `${caLabelFor(id)} must be at most ${max}.` });
            }
        }
    });
    return errors;
}

function renderCreditFormErrors(errors) {
    const box = document.getElementById('creditFormErrors');
    if (!box) return;
    if (!errors || errors.length === 0) {
        box.innerHTML = '';
        box.classList.add('hidden');
        return;
    }
    box.innerHTML = errors.map(err => `
        <div class="ca-error-item">
            <i class="fa-solid fa-circle-exclamation" aria-hidden="true"></i>
            <span>${escapeCaHtml(err.message)}</span>
        </div>
    `).join('');
    box.classList.remove('hidden');
}

function clearCreditFormErrors() { renderCreditFormErrors([]); }

function setCreditSubmitLoading(isLoading) {
    const btn = document.getElementById('submitCreditBtn');
    const resetBtn = document.getElementById('resetCreditBtn');
    if (!btn) return;
    if (isLoading) {
        if (!btn.dataset.originalHtml) btn.dataset.originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.classList.add('ca-btn-loading');
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> Assessing...';
    } else {
        btn.disabled = false;
        btn.classList.remove('ca-btn-loading');
        if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
    }
    if (resetBtn) resetBtn.disabled = isLoading;
}

/* ---------------------------------------------------------------
   MAIN FORM SUBMISSION & ORCHESTRATION (Phase 9)
--------------------------------------------------------------- */
async function handleCreditAssessmentSubmit(e) {
    e.preventDefault();
    if (caSubmitInFlight) return;

    clearCreditFormErrors();

    const clientErrors = validateCreditForm();
    if (clientErrors.length > 0) {
        renderCreditFormErrors(clientErrors);
        return;
    }

    const payload = gatherCreditFormData();
    currentBaselineApplicant = payload;

    caSubmitInFlight = true;
    setCreditSubmitLoading(true);
    renderCreditLoadingState();

    try {
        // 1. Fetch Primary Credit Risk Assessment (Blocker)
        const assessRes = await caRequest('/api/credit-risk/assess', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        let data = null;
        try { data = await assessRes.json(); } catch (parseErr) { data = null; }

        if (!assessRes.ok) {
            const fieldErrors = data && (data.errors || data.field_errors);
            if (fieldErrors && typeof fieldErrors === 'object') {
                const normalized = Array.isArray(fieldErrors)
                    ? fieldErrors.map(m => ({ field: null, message: String(m) }))
                    : Object.entries(fieldErrors).map(([field, message]) => ({
                        field, message: Array.isArray(message) ? message.join(' ') : String(message)
                    }));
                renderCreditFormErrors(normalized);
                renderCreditEmptyState();
            } else {
                const message = (data && (data.message || data.error)) || 'We couldn\'t assess this application. Please check your details and try again.';
                renderCreditFormErrors([{ field: null, message }]);
                renderCreditEmptyState();
            }
            return;
        }

        if (!data || data.status !== 'success') {
            const message = (data && (data.message || data.error)) || 'Unexpected response from the credit risk service.';
            renderCreditErrorState(message);
            return;
        }

        currentAssessData = data;

        // 2. Concurrently fetch supporting evidence without blocking
        const [behaviorData, affordData, anomalyData, explainData] = await Promise.all([
            safeJsonFetch('/api/credit-risk/financial-behavior', { method: 'GET' }),
            safeJsonFetch('/api/credit-risk/affordability', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ applicant: payload }) }),
            safeJsonFetch('/api/credit-risk/anomaly', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
            safeJsonFetch('/api/credit-risk/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        ]);

        currentBehaviorData = behaviorData;
        currentAffordData = affordData;
        currentAnomalyData = anomalyData;

        // 3. Pre-render isolated section views in the background
        if (behaviorData) {
            renderFinancialBehavior(behaviorData, document.getElementById('caBehaviorBody'));
            behaviorLoaded = true;
        }
        if (affordData) {
            renderAffordability(affordData, document.getElementById('caAffordabilityBody'));
            lastAffordabilityPayloadStr = JSON.stringify(payload);
        }
        if (explainData) {
            renderExplainability(explainData, document.getElementById('caExplainBody'));
        }
        initScenarioAnalysis(); // Refresh scenario baseline

        // 4. Render the Unified Assessment Overview (Phase 9)
        renderUnifiedOverview(currentAssessData, currentBehaviorData, currentAffordData, currentAnomalyData);

    } catch (networkErr) {
        renderCreditErrorState('Couldn\'t reach the credit risk service. Check your connection and try again.');
    } finally {
        caSubmitInFlight = false;
        setCreditSubmitLoading(false);
    }
}

function handleCreditAssessmentReset() {
    const form = document.getElementById('creditAssessmentForm');
    if (form) form.reset();
    clearCreditFormErrors();

    // Overview Reset
    renderCreditEmptyState();

    // Explainability Reset
    const explainBody = document.getElementById('caExplainBody');
    if (explainBody) {
        explainBody.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-magnifying-glass-chart" aria-hidden="true"></i>
                <p>Complete an assessment to see which factors influenced the model's result.</p>
            </div>
        `;
    }

    // Financial Behavior Reset
    behaviorLoaded = false;
    const behaviorBody = document.getElementById('caBehaviorBody');
    if (behaviorBody) {
        behaviorBody.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-chart-line" aria-hidden="true"></i>
                <p>Complete an assessment to see the financial behavior profile.</p>
            </div>
        `;
    }

    // Affordability Reset
    lastAffordabilityPayloadStr = null;
    const affordBody = document.getElementById('caAffordabilityBody');
    if (affordBody) {
        affordBody.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-calculator" aria-hidden="true"></i>
                <p>Complete the applicant form to evaluate affordability.</p>
            </div>
        `;
    }

    // Scenario Reset
    currentBaselineApplicant = null;
    currentAssessData = null;
    currentBehaviorData = null;
    currentAffordData = null;
    currentAnomalyData = null;
    initScenarioAnalysis();

    setCreditSubmitLoading(false);
}

/* ---------------------------------------------------------------
   UNIFIED ASSESSMENT OVERVIEW (Phase 9)
--------------------------------------------------------------- */
function renderCreditEmptyState() {
    const area = document.getElementById('creditResultArea');
    if (!area) return;
    area.innerHTML = `
        <div class="empty-state" id="creditResultEmpty">
            <i class="fa-solid fa-shield-halved" aria-hidden="true"></i>
            <p>Complete the application intake and assess to view the underwriting decision.</p>
        </div>
    `;
}

function renderCreditLoadingState() {
    const area = document.getElementById('creditResultArea');
    if (!area) return;
    area.innerHTML = `
        <div class="ca-loading">
            <i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i>
            <p>Evaluating application…</p>
        </div>
    `;
}

function renderCreditErrorState(message) {
    const area = document.getElementById('creditResultArea');
    if (!area) return;
    area.innerHTML = `
        <div class="ca-error">
            <i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
            <p>${escapeCaHtml(message || 'Something went wrong while assessing this application. Please try again.')}</p>
        </div>
    `;
}

function renderUnifiedOverview(assessData, behaviorData, affordData, anomalyData) {
    const area = document.getElementById('creditResultArea');
    if (!area) return;

    // 1. PRIMARY CREDIT RISK
    const riskLevel = assessData.risk_level;
    const decision = assessData.decision;
    const riskClass = caRiskClass(riskLevel);
    const decisionClass = caDecisionClass(decision);
    const pctNum = Number.isFinite(Number(assessData.risk_percentage)) ? Number(assessData.risk_percentage) : 0;
    const pctDisplay = pctNum.toFixed(1).replace(/\.0$/, '');

    // 2. SUPPORTING SIGNALS EXTRACTION
    const affordStatus = affordData ? affordData.affordability.status : "insufficient_data";
    let affordLabel = "Insufficient Data";
    let affordColor = "var(--text-tertiary)";
    if (affordStatus === 'affordable') { affordLabel = "Affordable"; affordColor = "var(--success)"; }
    else if (affordStatus === 'strained') { affordLabel = "Strained"; affordColor = "var(--warning)"; }
    else if (affordStatus === 'unaffordable') { affordLabel = "Unaffordable"; affordColor = "var(--danger)"; }

    // Canonical Financial Behavior state — derived ONCE and reused for both
    // the Financial Behavior summary card and the "Why This Assessment"
    // evidence bullet, so the two can never disagree with each other.
    const behaviorSummary = behaviorData ? behaviorData.summary : "Insufficient Data";
    const normalizedBehavior = String(behaviorData?.summary || '').trim().toLowerCase();

    let behaviorState;
    if (!behaviorData || !normalizedBehavior) {
        behaviorState = 'unavailable';
    } else if (normalizedBehavior.includes('high') && normalizedBehavior.includes('pressure')) {
        behaviorState = 'high_pressure';
    } else if (normalizedBehavior.includes('moderate')) {
        behaviorState = 'moderate';
    } else if (normalizedBehavior.includes('healthy')) {
        behaviorState = 'healthy';
    } else if (normalizedBehavior.includes('limited')) {
        behaviorState = 'limited';
    } else if (normalizedBehavior.includes('insufficient')) {
        behaviorState = 'insufficient';
    } else {
        behaviorState = 'unavailable';
    }

    const BEHAVIOR_STATE_CONFIG = {
        healthy: {
            color: 'var(--success)',
            evidence: 'Recent financial behavior appears healthy.'
        },
        moderate: {
            color: 'var(--warning)',
            evidence: 'Recent financial behavior shows moderate pressure.'
        },
        high_pressure: {
            color: 'var(--danger)',
            evidence: 'Recent financial behavior indicates high financial pressure.'
        },
        limited: {
            color: 'var(--text-tertiary)',
            evidence: 'Financial history is limited, so the behavioral assessment is less certain.'
        },
        insufficient: {
            color: 'var(--text-tertiary)',
            evidence: 'Insufficient transaction history to form a behavioral profile.'
        },
        unavailable: {
            color: 'var(--text-tertiary)',
            evidence: 'Insufficient transaction history to form a behavioral profile.'
        }
    };

    const behaviorColor = BEHAVIOR_STATE_CONFIG[behaviorState].color;

    const anomalyAvailable = anomalyData ? anomalyData.available !== false : false;
    const isAnomalous = anomalyData ? anomalyData.is_anomaly === true : false;
    const anomalyLabel = anomalyAvailable ? (isAnomalous ? "Anomalous" : "Normal") : "Unavailable";
    const anomalyColor = anomalyAvailable ? (isAnomalous ? "var(--warning)" : "var(--success)") : "var(--text-tertiary)";

    // 3. DETERMINISTIC DECISION-SUPPORT LOGIC
    let overallStatus = "INSUFFICIENT DATA";
    let overallClass = "ca-decision-review";
    let overallIcon = "fa-circle-question";

    if (decision === "REJECT") {
        overallStatus = "CREDIT MODEL REJECT";
        overallClass = "ca-decision-reject";
        overallIcon = "fa-circle-xmark";
    } else if (decision === "MANUAL REVIEW") {
        overallStatus = "REVIEW RECOMMENDED";
        overallClass = "ca-decision-review";
        overallIcon = "fa-circle-exclamation";
    } else { // APPROVE
        if (affordStatus === "unaffordable" || isAnomalous || behaviorSummary === "High financial pressure") {
            overallStatus = "REVIEW RECOMMENDED";
            overallClass = "ca-decision-review";
            overallIcon = "fa-circle-exclamation";
        } else if (affordStatus === "insufficient_data" || behaviorSummary.includes("Limited") || behaviorSummary.includes("Insufficient") || !anomalyAvailable) {
            overallStatus = "INSUFFICIENT DATA";
            overallClass = "ca-decision-review";
            overallIcon = "fa-circle-question";
            overallClass = "ca-decision-review"; // We can style it muted later if needed
        } else {
            overallStatus = "SUPPORTS APPROVAL";
            overallClass = "ca-decision-approve";
            overallIcon = "fa-circle-check";
        }
    }

    // 4. GENERATE DETERMINISTIC EVIDENCE BULLETS
    const evidenceBullets = [];

    // Credit
    evidenceBullets.push(`Credit model estimates ${escapeCaHtml(String(riskLevel).toLowerCase())} at ${escapeCaHtml(pctDisplay)}%.`);
    // Affordability
    if (affordStatus === 'affordable') evidenceBullets.push("Estimated loan repayment is comfortably covered by available surplus.");
    else if (affordStatus === 'strained') evidenceBullets.push("Estimated loan repayment consumes a high portion of available surplus.");
    else if (affordStatus === 'unaffordable') evidenceBullets.push("Estimated monthly payment exceeds available monthly surplus.");
    else evidenceBullets.push("Insufficient financial history to assess cash-flow affordability.");
    // Behavior — same canonical state as the summary card above, so the two
    // can never say contradictory things.
    evidenceBullets.push(BEHAVIOR_STATE_CONFIG[behaviorState].evidence);
    // Anomaly
    if (!anomalyAvailable) evidenceBullets.push("Application anomaly check is currently unavailable.");
    else if (isAnomalous) evidenceBullets.push("Application profile differs from patterns seen in the reference population.");
    else evidenceBullets.push("Application anomaly check found no unusual profile pattern.");

    // 5. RENDER
    area.innerHTML = `
        <div class="ca-result">
            
            <!-- CREDIT RISK (PRIMARY) -->
            <div style="display: flex; flex-wrap: wrap; gap: var(--spacing-lg); align-items: center; border-bottom: 1px solid var(--border-subtle); padding-bottom: var(--spacing-lg);">
                <div class="ca-result-icon ${riskClass}"><i class="fa-solid fa-shield-halved"></i></div>
                <div style="flex: 1;">
                    <div style="font-size: 0.72rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); letter-spacing: 0.05em; margin-bottom: 4px;">Credit Risk Model</div>
                    <div style="display: flex; align-items: baseline; gap: 12px;">
                        <span style="font-family: var(--font-mono); font-size: 2rem; font-weight: 800; color: var(--text-primary);">${escapeCaHtml(pctDisplay)}%</span>
                        <span class="ca-risk-level ${riskClass}">${escapeCaHtml(riskLevel)}</span>
                    </div>
                </div>
                <div class="ca-decision-value ${decisionClass}" style="padding: 8px 16px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); font-weight: 800;">
                    ${escapeCaHtml(decision)}
                </div>
            </div>

            <!-- SECONDARY SIGNALS GRID -->
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--spacing-md);">
                <div style="padding: 14px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-tertiary); display: flex; flex-direction: column; justify-content: space-between;">
                    <div>
                        <div style="font-size: 0.65rem; font-weight: 800; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;">Financial Behavior</div>
                        <div style="font-size: 0.95rem; font-weight: 700; color: ${behaviorColor}; margin-bottom: 12px; line-height: 1.2;">${escapeCaHtml(behaviorSummary)}</div>
                    </div>
                    <a href="#" onclick="switchCaTab('behavior'); return false;" style="font-size: 0.75rem; color: var(--primary); text-decoration: none; font-weight: 700;">View details &rarr;</a>
                </div>

                <div style="padding: 14px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-tertiary); display: flex; flex-direction: column; justify-content: space-between;">
                    <div>
                        <div style="font-size: 0.65rem; font-weight: 800; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;">Repayment Capacity</div>
                        <div style="font-size: 0.95rem; font-weight: 700; color: ${affordColor}; margin-bottom: 12px; line-height: 1.2;">${affordLabel}</div>
                    </div>
                    <a href="#" onclick="switchCaTab('affordability'); return false;" style="font-size: 0.75rem; color: var(--primary); text-decoration: none; font-weight: 700;">View details &rarr;</a>
                </div>

                <div style="padding: 14px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-tertiary); display: flex; flex-direction: column; justify-content: space-between;">
                    <div>
                        <div style="font-size: 0.65rem; font-weight: 800; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;">Application Anomaly</div>
                        <div style="font-size: 0.95rem; font-weight: 700; color: ${anomalyColor}; margin-bottom: 12px; line-height: 1.2;">${anomalyLabel}</div>
                    </div>
                    <span style="font-size: 0.75rem; color: var(--text-tertiary); font-weight: 500;">Population analysis</span>
                </div>
            </div>

            <!-- UNIFIED OVERALL ASSESSMENT -->
            <div class="ca-decision ${overallClass}" style="flex-direction: column; align-items: flex-start; gap: 16px; margin-top: var(--spacing-md);">
                
                <div style="display: flex; align-items: center; gap: 12px;">
                    <i class="fa-solid ${overallIcon}"></i>
                    <div>
                        <div style="font-size: 0.7rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 2px; opacity: 0.8;">OVERALL ASSESSMENT</div>
                        <div style="font-size: 1.2rem; font-weight: 800; letter-spacing: 0.01em;">${overallStatus}</div>
                    </div>
                </div>
                
                <div style="width: 100%; border-top: 1px solid rgba(0,0,0,0.08); padding-top: 12px;">
                    <div style="font-size: 0.72rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; opacity: 0.8;">WHY THIS DECISION?</div>
                    <ul style="margin: 0; padding-left: 20px; font-size: 0.85rem; line-height: 1.5; font-weight: 500; display: flex; flex-direction: column; gap: 6px;">
                        ${evidenceBullets.map(b => `<li>${b}</li>`).join('')}
                    </ul>
                </div>
                
                <div style="width: 100%; margin-top: 4px; font-size: 0.7rem; font-weight: 500; opacity: 0.7; line-height: 1.4;">
                    * Secondary financial and behavioral evidence does not alter the trained credit-risk model probability.
                </div>
            </div>

        </div>
    `;
}

/* ---------------------------------------------------------------
   EXPLAINABILITY SECTION (Phase 3)
--------------------------------------------------------------- */
function caFactorList(factors, direction) {
    if (!factors || factors.length === 0) return '';
    const maxImpact = factors.reduce((max, f) => {
        const val = Number(f.impact);
        return Number.isFinite(val) && val > max ? val : max;
    }, 0);
    return factors.map(f => {
        const impactNum = Number(f.impact);
        const widthPct = maxImpact > 0 && Number.isFinite(impactNum)
            ? Math.max(6, Math.min(100, (impactNum / maxImpact) * 100))
            : 6;
        const impactDisplay = Number.isFinite(impactNum) ? impactNum.toFixed(3) : '';
        return `
            <div class="ca-factor-item ca-factor-${direction}">
                <div class="ca-factor-row">
                    <span class="ca-factor-name">${escapeCaHtml(f.feature)}</span>
                    ${impactDisplay ? `<span class="ca-factor-impact">${escapeCaHtml(impactDisplay)}</span>` : ''}
                </div>
                <div class="ca-factor-bar-track">
                    <div class="ca-factor-bar-fill" style="width: ${widthPct}%;"></div>
                </div>
            </div>
        `;
    }).join('');
}

function renderExplainability(data, body) {
    if (!body) return;

    if (!data || data.status !== 'success' || data.explanation_available === false) {
        body.innerHTML = `
            <div class="empty-state">
                <i class="fa-regular fa-circle-question" aria-hidden="true"></i>
                <p>${escapeCaHtml(data && data.message || 'Explanation unavailable for this assessment.')}</p>
            </div>
        `;
        return;
    }

    const increasing = data.risk_increasing_factors || [];
    const reducing = data.risk_reducing_factors || [];

    if (increasing.length === 0 && reducing.length === 0) {
        body.innerHTML = `
            <div class="empty-state">
                <i class="fa-regular fa-circle-question" aria-hidden="true"></i>
                <p>No individual factors stood out for this assessment.</p>
            </div>
        `;
        return;
    }

    body.innerHTML = `
        <div class="ca-factor-groups">
            ${increasing.length > 0 ? `
                <div class="ca-factor-group">
                    <div class="ca-factor-group-title ca-factor-increasing" style="color: var(--danger); font-size: 0.72rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                        <i class="fa-solid fa-arrow-trend-up" aria-hidden="true"></i> Factors increasing risk
                    </div>
                    <div class="ca-factor-list">${caFactorList(increasing, 'increasing')}</div>
                </div>
            ` : ''}
            <br>
            ${reducing.length > 0 ? `
                <div class="ca-factor-group">
                    <div class="ca-factor-group-title ca-factor-reducing" style="color: var(--success); font-size: 0.72rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                        <i class="fa-solid fa-arrow-trend-down" aria-hidden="true"></i> Factors reducing risk
                    </div>
                    <div class="ca-factor-list">${caFactorList(reducing, 'reducing')}</div>
                </div>
            ` : ''}
        </div>
    `;
}

/* ---------------------------------------------------------------
   SCENARIO ANALYSIS SECTION (Phase 4)
--------------------------------------------------------------- */
function buildScenarioControl(id, label, baselineValue, type = 'number', min, max, step) {
    return `
        <div style="background: var(--bg-tertiary); padding: 12px 14px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
            <label style="font-size: 0.76rem; font-weight: 800; color: var(--text-secondary); text-transform: uppercase; width: 140px;">${label}</label>
            <div style="display: flex; align-items: center; gap: 12px; flex: 1; min-width: 150px;">
                <span style="font-family: var(--font-mono); font-weight: 600; color: var(--text-tertiary); font-size: 0.9rem;">${baselineValue}</span>
                <i class="fa-solid fa-arrow-right" style="color: var(--text-tertiary); font-size: 0.8rem;"></i>
                <input type="${type}" id="${id}" class="form-input" style="flex: 1; min-width: 0;" value="${baselineValue}" ${min ? `min="${min}"` : ''} ${max ? `max="${max}"` : ''} ${step ? `step="${step}"` : ''}>
            </div>
        </div>
    `;
}

function initScenarioAnalysis() {
    const contentDiv = document.getElementById('caScenarioContent');
    if (!contentDiv) return;

    if (!currentBaselineApplicant) {
        contentDiv.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-code-branch" aria-hidden="true"></i>
                <p>Run a credit assessment first to explore scenarios.</p>
            </div>
        `;
        return;
    }

    contentDiv.innerHTML = `
        <div id="caScenarioControls" style="display: flex; flex-direction: column; gap: var(--spacing-md); margin-bottom: var(--spacing-md);">
            ${buildScenarioControl('sc_credit_amount', 'Credit Amount (₹)', currentBaselineApplicant.credit_amount, 'number', 1, 1000000)}
            ${buildScenarioControl('sc_duration_months', 'Loan Duration', currentBaselineApplicant.duration_months, 'number', 1, 120)}
            ${buildScenarioControl('sc_installment_rate', 'Installment Rate', currentBaselineApplicant.installment_rate, 'number', 1, 4)}
            ${buildScenarioControl('sc_existing_credits', 'Existing Credits', currentBaselineApplicant.existing_credits, 'number', 1, 10)}
            ${buildScenarioControl('sc_residence_since', 'Residence Since', currentBaselineApplicant.residence_since, 'number', 1, 4)}
        </div>
        <div id="caScenarioErrors" class="ca-error-list hidden" style="margin-bottom: var(--spacing-md);"></div>
        <div style="display: flex; gap: var(--spacing-md); align-items: center;">
            <button id="runScenarioBtn" class="btn-primary" style="flex: 1;">Run Scenario</button>
            <button id="resetScenarioBtn" class="btn-secondary" style="padding: 10px 16px;">Reset Scenario</button>
        </div>
        <div id="caScenarioResult" style="margin-top: var(--spacing-lg); display: none;"></div>
    `;

    document.getElementById('runScenarioBtn').addEventListener('click', runScenario);
    document.getElementById('resetScenarioBtn').addEventListener('click', () => {
        initScenarioAnalysis(); // resets DOM layout to baseline
    });
}

async function runScenario() {
    const changes = {};
    const scAmount = Number(document.getElementById('sc_credit_amount').value);
    const scDuration = Number(document.getElementById('sc_duration_months').value);
    const scInstallment = Number(document.getElementById('sc_installment_rate').value);
    const scExisting = Number(document.getElementById('sc_existing_credits').value);
    const scResidence = Number(document.getElementById('sc_residence_since').value);

    if (scAmount !== currentBaselineApplicant.credit_amount) changes.credit_amount = scAmount;
    if (scDuration !== currentBaselineApplicant.duration_months) changes.duration_months = scDuration;
    if (scInstallment !== currentBaselineApplicant.installment_rate) changes.installment_rate = scInstallment;
    if (scExisting !== currentBaselineApplicant.existing_credits) changes.existing_credits = scExisting;
    if (scResidence !== currentBaselineApplicant.residence_since) changes.residence_since = scResidence;

    const btn = document.getElementById('runScenarioBtn');
    const resetBtn = document.getElementById('resetScenarioBtn');
    const errorDiv = document.getElementById('caScenarioErrors');

    btn.disabled = true;
    resetBtn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Analyzing scenario...';
    errorDiv.classList.add('hidden');

    try {
        const res = await caRequest('/api/credit-risk/scenario', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ applicant: currentBaselineApplicant, changes })
        });

        let data = null;
        try { data = await res.json(); } catch (err) { }

        if (!res.ok || !data || data.status !== 'success') {
            const errs = data && data.errors ? data.errors : ['Failed to run scenario analysis.'];
            errorDiv.innerHTML = errs.map(e => `
                <div class="ca-error-item">
                    <i class="fa-solid fa-circle-exclamation"></i>
                    <span>${escapeCaHtml(e)}</span>
                </div>
            `).join('');
            errorDiv.classList.remove('hidden');
            return;
        }

        renderScenarioResult(data);
    } catch (err) {
        errorDiv.innerHTML = `
            <div class="ca-error-item">
                <i class="fa-solid fa-triangle-exclamation"></i>
                <span>Network error preventing scenario analysis.</span>
            </div>
        `;
        errorDiv.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        resetBtn.disabled = false;
        btn.innerHTML = 'Run Scenario';
    }
}

function renderScenarioResult(data) {
    const resultDiv = document.getElementById('caScenarioResult');
    resultDiv.style.display = 'block';

    const baselineRiskClass = caRiskClass(data.baseline_risk_level);
    const scenarioRiskClass = caRiskClass(data.scenario_risk_level);
    const deltaSign = data.delta > 0 ? '+' : '';
    const deltaPp = (data.delta * 100).toFixed(1);

    let decisionTransition = '';
    if (data.baseline_decision !== data.scenario_decision) {
        decisionTransition = `
            <div style="display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 16px; padding: 12px; background: var(--bg-secondary); border-radius: var(--radius-md);">
                <span class="ca-decision-value ${caDecisionClass(data.baseline_decision)}" style="font-size: 0.85rem; padding: 6px 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius-full);">${data.baseline_decision}</span>
                <i class="fa-solid fa-arrow-right" style="color: var(--text-tertiary);"></i>
                <span class="ca-decision-value ${caDecisionClass(data.scenario_decision)}" style="font-size: 0.85rem; padding: 6px 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius-full);">${data.scenario_decision}</span>
            </div>
        `;
    } else {
        const trendVerb = data.delta > 0 ? 'increased' : 'decreased';
        const trendAbs = Math.abs(data.delta * 100).toFixed(1);
        const dynamicDesc = data.delta !== 0
            ? `estimated risk ${trendVerb} by ${trendAbs} percentage points`
            : `estimated risk unchanged`;

        decisionTransition = `<div style="text-align: center; margin-bottom: 16px; font-weight: 700; font-size: 0.85rem; color: var(--text-secondary);">Risk level unchanged &middot; ${dynamicDesc}</div>`;
    }

    resultDiv.innerHTML = `
        <div style="padding: var(--spacing-lg); background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg);">
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: var(--spacing-md); margin-bottom: var(--spacing-md); text-align: center;">
                <div style="padding: 12px; background: var(--bg-secondary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                    <div style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); margin-bottom: 4px;">BASELINE</div>
                    <div class="ca-risk-probability" style="font-size: 1.5rem; margin-bottom: 2px;">${(data.baseline_probability * 100).toFixed(1)}%</div>
                    <div class="ca-risk-level ${baselineRiskClass}" style="font-size: 0.85rem;">${data.baseline_risk_level}</div>
                </div>
                <div style="padding: 12px; background: var(--bg-secondary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                    <div style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); margin-bottom: 4px;">SCENARIO</div>
                    <div class="ca-risk-probability" style="font-size: 1.5rem; margin-bottom: 2px;">${(data.scenario_probability * 100).toFixed(1)}%</div>
                    <div class="ca-risk-level ${scenarioRiskClass}" style="font-size: 0.85rem;">${data.scenario_risk_level}</div>
                </div>
            </div>
            ${decisionTransition}
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: var(--spacing-md);">
                <div>
                    <span style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); display: block; margin-bottom: 4px;">CHANGE</span>
                    <div style="font-weight: 700; font-size: 0.95rem; font-family: var(--font-mono); color: var(--text-primary);">${deltaSign}${deltaPp} percentage points</div>
                </div>
                <div>
                    <span style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); display: block; margin-bottom: 4px;">INTERPRETATION</span>
                    <div style="font-weight: 600; font-size: 0.95rem; color: var(--text-primary);">${escapeCaHtml(data.interpretation)}</div>
                </div>
            </div>
        </div>
    `;
}

/* ---------------------------------------------------------------
   FINANCIAL BEHAVIOR ASSESSMENT (Phase 7)
--------------------------------------------------------------- */
async function loadFinancialBehavior() {
    const body = document.getElementById('caBehaviorBody');
    if (!body) return;

    try {
        const res = await caRequest('/api/credit-risk/financial-behavior', { method: 'GET' });
        let data = null;
        try { data = await res.json(); } catch (e) { }

        if (!res.ok || !data || data.status !== 'success') {
            throw new Error();
        }

        behaviorLoaded = true;
        renderFinancialBehavior(data, body);
    } catch (err) {
        body.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                <p>Failed to load financial behavior profile.</p>
            </div>
        `;
    }
}

function renderFinancialBehavior(data, container) {
    const coverage = data.data_coverage;
    const historyText = coverage.history_months < 3
        ? `Limited data — ${coverage.history_months} month(s) total`
        : `${coverage.history_months} months analyzed`;

    const valOr = (v, suffix = '') => v != null && v !== "Insufficient data" ? `${v}${suffix}` : '<span style="color:var(--text-tertiary); font-weight:500;">Insufficient data</span>';

    const formatTrend = (trend) => {
        if (trend == null) return '<span style="color:var(--text-tertiary); font-weight:500;">Insufficient data</span>';
        if (trend > 0) return `<span style="color:var(--danger);"><i class="fa-solid fa-arrow-trend-up"></i> +${trend}%</span> vs last month`;
        if (trend < 0) return `<span style="color:var(--success);"><i class="fa-solid fa-arrow-trend-down"></i> ${trend}%</span> vs last month`;
        return `0% vs last month`;
    };

    let flagsHtml = '';
    if (data.behavioral_flags && data.behavioral_flags.length > 0) {
        flagsHtml = data.behavioral_flags.map(f => {
            const isHigh = f.severity === 'high';
            const colorClass = isHigh ? 'var(--danger)' : 'var(--warning)';
            const bgClass = isHigh ? 'var(--danger-light)' : 'var(--warning-light)';
            const borderClass = isHigh ? 'var(--danger-border)' : 'var(--warning-border)';
            const icon = isHigh ? 'fa-triangle-exclamation' : 'fa-circle-exclamation';

            return `
                <div style="padding: 12px; background: ${bgClass}; border: 1px solid ${borderClass}; border-left: 3px solid ${colorClass}; border-radius: var(--radius-sm); margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px; font-size: 0.85rem; font-weight: 700; color: ${colorClass}; margin-bottom: 4px;">
                        <i class="fa-solid ${icon}"></i> ${escapeCaHtml(f.message)}
                    </div>
                    <div style="font-size: 0.8rem; color: var(--text-secondary); margin-left: 24px; line-height: 1.4;">
                        ${escapeCaHtml(f.evidence)}
                    </div>
                </div>
            `;
        }).join('');
    }

    container.innerHTML = `
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 12px 16px; background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); margin-bottom: var(--spacing-lg);">
            <div style="display: flex; align-items: center; gap: 8px;">
                <i class="fa-solid fa-database" style="color: var(--text-tertiary);"></i>
                <span style="font-size: 0.82rem; font-weight: 600; color: var(--text-secondary);">History: <strong>${historyText}</strong></span>
            </div>
            <div style="display: flex; gap: 16px; font-size: 0.75rem; color: var(--text-tertiary);">
                <span>Income: ${coverage.income_months} mo</span>
                <span>Spending: ${coverage.spending_months} mo</span>
                <span>Savings: ${coverage.savings_months} mo</span>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: var(--spacing-md); margin-bottom: var(--spacing-xl);">
            
            <div style="padding: 14px; background: var(--bg-tertiary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                <div style="font-size: 0.7rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 8px; border-bottom: 1px solid var(--border-medium); padding-bottom: 4px;">Income Stability</div>
                <div style="margin-bottom: 8px;">
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">HISTORICAL STABILITY</div>
                    <div style="font-size: 1rem; font-weight: 700; color: var(--text-primary);">${valOr(data.income.stability)}</div>
                </div>
                <div>
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">RECENT TREND</div>
                    <div style="font-size: 0.85rem; font-weight: 600;">${formatTrend(data.income.trend)}</div>
                </div>
                ${data.income.monthly_average != null ? `<div style="font-size: 0.75rem; color: var(--text-tertiary); margin-top: 10px; font-family: var(--font-mono);">Avg: ₹${data.income.monthly_average}</div>` : ''}
            </div>

            <div style="padding: 14px; background: var(--bg-tertiary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                <div style="font-size: 0.7rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 8px; border-bottom: 1px solid var(--border-medium); padding-bottom: 4px;">Spending Volatility</div>
                <div style="margin-bottom: 8px;">
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">HISTORICAL VOLATILITY</div>
                    <div style="font-size: 1rem; font-weight: 700; color: var(--text-primary);">${valOr(data.spending.volatility)}</div>
                </div>
                <div>
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">RECENT TREND</div>
                    <div style="font-size: 0.85rem; font-weight: 600;">${formatTrend(data.spending.trend)}</div>
                </div>
                ${data.spending.monthly_average != null ? `<div style="font-size: 0.75rem; color: var(--text-tertiary); margin-top: 10px; font-family: var(--font-mono);">Avg: ₹${data.spending.monthly_average}</div>` : ''}
            </div>

            <div style="padding: 14px; background: var(--bg-tertiary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                <div style="font-size: 0.7rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 8px; border-bottom: 1px solid var(--border-medium); padding-bottom: 4px;">Savings Behavior</div>
                <div style="margin-bottom: 8px;">
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">HISTORICAL STABILITY</div>
                    <div style="font-size: 1rem; font-weight: 700; color: var(--text-primary);">${valOr(data.savings.stability)}</div>
                </div>
                <div>
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">CURRENT RATE</div>
                    <div style="font-size: 1.1rem; font-weight: 800; font-family: var(--font-mono); color: var(--primary);">${valOr(data.savings.savings_rate, '%')}</div>
                </div>
            </div>

            <div style="padding: 14px; background: var(--bg-tertiary); border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                <div style="font-size: 0.7rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 8px; border-bottom: 1px solid var(--border-medium); padding-bottom: 4px;">Recurring Burden & Budget Discipline</div>
                <div style="margin-bottom: 8px;">
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">RECURRING BURDEN RATIO</div>
                    <div style="font-size: 1rem; font-weight: 700; color: var(--text-primary);">${valOr(data.recurring.burden_ratio, '%')}</div>
                </div>
                <div>
                    <div style="font-size: 0.65rem; color: var(--text-secondary);">BUDGET ADHERENCE</div>
                    <div style="font-size: 0.9rem; font-weight: 700; color: var(--text-primary);">${valOr(data.budget.adherence)}</div>
                    ${data.budget.usage_percent != null ? `<div style="font-size: 0.7rem; color: var(--text-tertiary); font-family: var(--font-mono);">Usage: ${data.budget.usage_percent}%</div>` : ''}
                </div>
            </div>

        </div>

        <div style="margin-bottom: var(--spacing-xl);">
            <h4 style="font-size: 0.72rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 12px; letter-spacing: 0.05em;">Borrower Behavioral Flags</h4>
            ${flagsHtml || '<div style="padding: 12px; font-size: 0.85rem; color: var(--success); background: var(--success-light); border: 1px solid var(--success-border); border-radius: var(--radius-sm);"><i class="fa-solid fa-check-circle" style="margin-right: 6px;"></i> No concerning behavioral flags detected.</div>'}
        </div>

        <div style="padding: var(--spacing-md) var(--spacing-lg); background: var(--bg-elevated); border-radius: var(--radius-md); border: 1px solid var(--border-subtle); display: flex; align-items: center; justify-content: space-between;">
            <div>
                <div style="font-size: 0.7rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 4px; letter-spacing: 0.05em;">BORROWER BEHAVIORAL SUMMARY</div>
                <div style="font-size: 1.15rem; font-weight: 800; color: var(--text-primary);">${escapeCaHtml(data.summary)}</div>
            </div>
            <i class="fa-solid fa-clipboard-user" style="font-size: 2rem; color: var(--border-strong); opacity: 0.5;"></i>
        </div>
    `;
}

/* ---------------------------------------------------------------
   AFFORDABILITY ASSESSMENT (Phase 8)
--------------------------------------------------------------- */
async function checkAndLoadAffordability() {
    const errs = validateCreditForm();
    const body = document.getElementById('caAffordabilityBody');
    if (!body) return;

    if (errs.length > 0) {
        body.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-calculator" aria-hidden="true"></i>
                <p>Complete the applicant form to evaluate affordability.</p>
            </div>
        `;
        return;
    }

    const payload = gatherCreditFormData();
    const payloadStr = JSON.stringify(payload);

    if (payloadStr === lastAffordabilityPayloadStr || isAffordabilityLoading) {
        return;
    }

    isAffordabilityLoading = true;
    lastAffordabilityPayloadStr = payloadStr;

    body.innerHTML = `
        <div class="ca-loading" style="padding: var(--spacing-lg);">
            <i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i>
            <p>Calculating affordability based on Budgetly history...</p>
        </div>
    `;

    try {
        const res = await caRequest('/api/credit-risk/affordability', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ applicant: payload })
        });

        let data = null;
        try { data = await res.json(); } catch (e) { }

        if (!res.ok || !data || data.status !== 'success') {
            throw new Error('Server error');
        }
        renderAffordability(data, body);
    } catch (err) {
        lastAffordabilityPayloadStr = null; // enable retry
        body.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                <p>Failed to load affordability assessment.</p>
            </div>
        `;
    } finally {
        isAffordabilityLoading = false;
    }
}

function renderAffordability(data, container) {
    const valOr = (v, prefix = '', suffix = '') => v != null ? `${prefix}${v}${suffix}` : '<span style="color:var(--text-tertiary);">Insufficient Data</span>';

    let statusColor, statusBg, statusBorder, icon;
    const status = data.affordability.status;

    if (status === 'affordable') {
        statusColor = 'var(--success)';
        statusBg = 'var(--success-light)';
        statusBorder = 'var(--success-border)';
        icon = 'fa-check-circle';
    } else if (status === 'strained') {
        statusColor = 'var(--warning)';
        statusBg = 'var(--warning-light)';
        statusBorder = 'var(--warning-border)';
        icon = 'fa-circle-exclamation';
    } else if (status === 'unaffordable') {
        statusColor = 'var(--danger)';
        statusBg = 'var(--danger-light)';
        statusBorder = 'var(--danger-border)';
        icon = 'fa-triangle-exclamation';
    } else {
        statusColor = 'var(--text-secondary)';
        statusBg = 'var(--bg-elevated)';
        statusBorder = 'var(--border-subtle)';
        icon = 'fa-circle-question';
    }

    const statusLabel = status.replace('_', ' ').toUpperCase();

    container.innerHTML = `
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: var(--spacing-lg); margin-bottom: var(--spacing-xl);">
            <!-- Financial Capacity -->
            <div style="padding: 16px; background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md);">
                <div style="font-size: 0.75rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 12px; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-medium); padding-bottom: 6px;">Financial Capacity</div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span style="font-size: 0.85rem; color: var(--text-secondary);">Monthly income</span>
                    <span style="font-weight: 700; font-family: var(--font-mono);">${valOr(data.financial_capacity.monthly_income, '₹')}</span>
                </div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span style="font-size: 0.85rem; color: var(--text-secondary);">Monthly expenses</span>
                    <span style="font-weight: 700; font-family: var(--font-mono);">${valOr(data.financial_capacity.monthly_expenses, '₹')}</span>
                </div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span style="font-size: 0.85rem; color: var(--text-secondary);">Recurring burden</span>
                    <span style="font-weight: 700; font-family: var(--font-mono);">${valOr(data.financial_capacity.recurring_burden, '₹')}</span>
                </div>
                <div style="display: flex; justify-content: space-between; margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-medium);">
                    <span style="font-size: 0.85rem; font-weight: 700; color: var(--text-primary);">Available surplus</span>
                    <span style="font-weight: 800; font-family: var(--font-mono); color: var(--success);">${valOr(data.affordability.available_surplus, '₹')}</span>
                </div>
            </div>

            <!-- Proposed Loan -->
            <div style="padding: 16px; background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-md);">
                <div style="font-size: 0.75rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 12px; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-medium); padding-bottom: 6px;">Proposed Loan</div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span style="font-size: 0.85rem; color: var(--text-secondary);">Loan amount</span>
                    <span style="font-weight: 700; font-family: var(--font-mono);">${valOr(data.loan.credit_amount, '₹')}</span>
                </div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span style="font-size: 0.85rem; color: var(--text-secondary);">Duration</span>
                    <span style="font-weight: 700; font-family: var(--font-mono);">${valOr(data.loan.duration_months, '', ' months')}</span>
                </div>
                <div style="display: flex; justify-content: space-between; margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-medium);">
                    <span style="font-size: 0.85rem; font-weight: 700; color: var(--text-primary);">Estimated monthly payment</span>
                    <span style="font-weight: 800; font-family: var(--font-mono); color: var(--danger);">${valOr(data.loan.estimated_monthly_payment, '₹')}</span>
                </div>
                <div style="font-size: 0.65rem; color: var(--text-tertiary); margin-top: 6px; text-align: right;">${escapeCaHtml(data.loan.calculation_method)}</div>
            </div>
        </div>

        <!-- Affordability Status -->
        <div style="padding: 16px; background: ${statusBg}; border: 1px solid ${statusBorder}; border-left: 4px solid ${statusColor}; border-radius: var(--radius-md);">
            <div style="font-size: 0.75rem; font-weight: 800; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 8px; letter-spacing: 0.05em;">Repayment Capacity</div>
            
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
                <i class="fa-solid ${icon}" style="font-size: 1.5rem; color: ${statusColor};"></i>
                <span style="font-size: 1.25rem; font-weight: 800; color: ${statusColor};">${statusLabel}</span>
            </div>
            
            <div style="font-size: 0.9rem; font-weight: 600; color: var(--text-primary); margin-bottom: 12px;">
                ${escapeCaHtml(data.affordability.reason)}
            </div>
            
            ${data.affordability.status !== 'insufficient_data' ? `
                <div style="display: flex; flex-wrap: wrap; gap: 16px; font-size: 0.8rem; color: var(--text-secondary); border-top: 1px solid rgba(0,0,0,0.05); padding-top: 12px;">
                    <span>Payment / Income: <strong style="font-family: var(--font-mono);">${valOr(data.affordability.payment_to_income_ratio, '', '%')}</strong></span>
                    <span>Payment / Surplus: <strong style="font-family: var(--font-mono);">${valOr(data.affordability.payment_to_surplus_ratio, '', '%')}</strong></span>
                </div>
            ` : ''}
        </div>
    `;
}

/* ---------------------------------------------------------------
   RESPONSIBLE AI / FAIRNESS
--------------------------------------------------------------- */
async function loadResponsibleAiData() {
    let raiContainer = document.getElementById('caResponsibleAiSection');
    if (!raiContainer) return;

    raiContainer.innerHTML = `
        <div class="ca-loading" style="padding: var(--spacing-lg);">
            <i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i>
            <p>Loading Responsible AI metadata...</p>
        </div>
    `;

    try {
        const res = await caRequest('/api/credit-risk/responsible-ai', { method: 'GET' });
        let data = null;
        try { data = await res.json(); } catch (e) { }

        if (!res.ok || !data || data.status !== 'success') {
            throw new Error('Unavailable');
        }
        renderResponsibleAiData(data, raiContainer);
    } catch (err) {
        raiContainer.innerHTML = `
            <div class="ca-explain-empty" style="padding: var(--spacing-lg);">
                <i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                <p>Responsible AI data is currently unavailable.</p>
            </div>
        `;
    }
}

function renderResponsibleAiData(data, container) {
    const perf = data.model_performance || {};
    const fairness = data.fairness || {};

    const pct = (val) => val != null ? (val * 100).toFixed(1) + '%' : 'N/A';
    const dec = (val) => val != null ? val.toFixed(4) : 'N/A';

    let fairnessHtml = '';
    if (fairness.available && fairness.records && fairness.records.length > 0) {
        const groups = {};
        fairness.records.forEach(r => {
            if (!groups[r.attribute]) groups[r.attribute] = [];
            groups[r.attribute].push(r);
        });

        fairnessHtml = Object.keys(groups).map(attr => {
            const rows = groups[attr].map(r => {
                const apprDiff = r.approval_rate_difference != null ? (r.approval_rate_difference * 100).toFixed(1) + '%' : '-';
                const fprDiff = r.fpr_difference != null ? (r.fpr_difference * 100).toFixed(1) + '%' : '-';
                const tprDiff = r.tpr_difference != null ? (r.tpr_difference * 100).toFixed(1) + '%' : '-';

                // Identify reference group if values are explicitly baseline (zero)
                const isRef = r.is_reference === true || (r.approval_rate_difference === 0 && r.fpr_difference === 0 && r.tpr_difference === 0);

                return `
                    <tr style="border-bottom: 1px solid var(--border-subtle);">
                        <td style="padding: 10px 6px; font-weight: 600;">
                            ${escapeCaHtml(r.group)} 
                            ${isRef ? '<span style="font-size: 0.65rem; color: var(--text-tertiary); font-weight: normal; margin-left: 4px;">(Ref)</span>' : ''}
                        </td>
                        <td style="padding: 10px 6px; font-family: var(--font-mono); text-align: right;">${apprDiff}</td>
                        <td style="padding: 10px 6px; font-family: var(--font-mono); text-align: right;">${fprDiff}</td>
                        <td style="padding: 10px 6px; font-family: var(--font-mono); text-align: right;">${tprDiff}</td>
                    </tr>
                `;
            }).join('');

            return `
                <div style="margin-bottom: var(--spacing-md); background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: var(--spacing-md);">
                    <h4 style="font-size: 0.75rem; text-transform: uppercase; margin-bottom: 8px; color: var(--text-secondary); letter-spacing: 0.05em;">${escapeCaHtml(attr)}</h4>
                    <div style="overflow-x: auto;">
                        <table style="width: 100%; font-size: 0.8rem; text-align: left; border-collapse: collapse; white-space: nowrap;">
                            <thead>
                                <tr style="border-bottom: 2px solid var(--border-subtle); color: var(--text-tertiary); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em;">
                                    <th style="padding: 6px; font-weight: 700;">Group</th>
                                    <th style="padding: 6px; font-weight: 700; text-align: right;">Approval Rate Diff</th>
                                    <th style="padding: 6px; font-weight: 700; text-align: right;">FPR Diff</th>
                                    <th style="padding: 6px; font-weight: 700; text-align: right;">TPR Diff</th>
                                </tr>
                            </thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                </div>
            `;
        }).join('');
    } else {
        fairnessHtml = `<p style="font-size: 0.82rem; color: var(--text-tertiary);">Fairness monitoring data is not available.</p>`;
    }

    container.innerHTML = `
        <div class="card-header" style="margin-bottom: var(--spacing-lg);">
            <div>
                <h3 class="card-title" style="display: flex; align-items: center; gap: 8px;">
                    <i class="fa-solid fa-scale-balanced" style="color: var(--primary);"></i> 
                    Responsible AI
                </h3>
                <p class="card-subtitle">Dataset-level model transparency and fairness monitoring.</p>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: var(--spacing-xl);">
            
            <!-- Left Column: Perf & Calibration -->
            <div>
                <h4 style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: var(--spacing-md);">Model Performance</h4>
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: var(--spacing-xl);">
                    <div style="background: var(--bg-tertiary); padding: 12px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                        <div style="font-size: 0.65rem; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700; margin-bottom: 4px;">Accuracy</div>
                        <div style="font-size: 1.15rem; font-weight: 800; font-family: var(--font-mono); color: var(--text-primary);">${pct(perf.accuracy)}</div>
                    </div>
                    <div style="background: var(--bg-tertiary); padding: 12px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                        <div style="font-size: 0.65rem; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700; margin-bottom: 4px;">ROC-AUC</div>
                        <div style="font-size: 1.15rem; font-weight: 800; font-family: var(--font-mono); color: var(--text-primary);">${pct(perf.roc_auc)}</div>
                    </div>
                    <div style="background: var(--bg-tertiary); padding: 12px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                        <div style="font-size: 0.65rem; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700; margin-bottom: 4px;">PR-AUC</div>
                        <div style="font-size: 1.15rem; font-weight: 800; font-family: var(--font-mono); color: var(--text-primary);">${pct(perf.pr_auc)}</div>
                    </div>
                </div>

                <h4 style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: var(--spacing-sm);">Probability Calibration</h4>
                <p style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 12px;">Lower calibration error indicates probabilities are closer to observed outcomes.</p>
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: var(--spacing-lg);">
                    <div style="background: var(--bg-tertiary); padding: 12px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                        <div style="font-size: 0.65rem; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700; margin-bottom: 4px;">Brier Score</div>
                        <div style="font-size: 1.15rem; font-weight: 800; font-family: var(--font-mono); color: var(--text-primary);">${dec(perf.brier_score)}</div>
                    </div>
                    <div style="background: var(--bg-tertiary); padding: 12px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle);">
                        <div style="font-size: 0.65rem; color: var(--text-tertiary); text-transform: uppercase; font-weight: 700; margin-bottom: 4px;">Log Loss</div>
                        <div style="font-size: 1.15rem; font-weight: 800; font-family: var(--font-mono); color: var(--text-primary);">${dec(perf.log_loss)}</div>
                    </div>
                </div>
            </div>

            <!-- Right Column: Fairness Monitoring -->
            <div>
                <h4 style="font-size: 0.72rem; font-weight: 800; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: var(--spacing-sm);">Fairness Monitoring</h4>
                
                <div style="display: flex; gap: 12px; padding: 12px; background: var(--warning-light); border: 1px solid var(--warning-border); border-left: 3px solid var(--warning); border-radius: var(--radius-md); margin-bottom: var(--spacing-md);">
                    <i class="fa-solid fa-circle-info" style="color: var(--warning); font-size: 1.1rem; margin-top: 2px;"></i>
                    <div style="font-size: 0.8rem; color: var(--text-secondary); line-height: 1.5;">
                        <strong>Fairness metrics shown here are dataset-level monitoring results. They are not an individual applicant fairness score.</strong> 
                        Fairness disparities indicate areas for monitoring and review; they do not by themselves prove discriminatory intent or causation.
                    </div>
                </div>

                <p style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: var(--spacing-lg); line-height: 1.5;">
                    Attributes such as <strong>personal_status_sex</strong> and <strong>foreign_worker</strong> are included in the trained model and are monitored through this dataset-level analysis.
                </p>

                ${fairnessHtml}
            </div>
        </div>
    `;
}