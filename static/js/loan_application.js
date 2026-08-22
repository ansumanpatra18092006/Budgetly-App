'use strict';

/* =================================================================
   BORROWER LOAN APPLICATION (Phase 1)

   This page is intentionally minimal: it collects the application
   and submits it to POST /api/loan-applications. It does NOT call,
   render, or reference any credit-risk assessment endpoint —
   risk probability, decision, SHAP, anomaly, scenario analysis, and
   Responsible AI all live only in the lender-side workspace.
================================================================== */

let laSubmitInFlight = false;

// Exact field schema expected by the credit-risk model (20 fields).
// Must stay in sync with the lender-side schema — do not add/remove.
const LA_FIELD_IDS = [
    'la_checking_account', 'la_duration_months', 'la_credit_history', 'la_purpose',
    'la_credit_amount', 'la_savings_account', 'la_employment_since', 'la_installment_rate',
    'la_personal_status_sex', 'la_other_debtors', 'la_residence_since', 'la_property',
    'la_age', 'la_other_installment_plans', 'la_housing', 'la_existing_credits',
    'la_job', 'la_dependents', 'la_telephone', 'la_foreign_worker'
];

const LA_NUMERIC_IDS = new Set([
    'la_duration_months', 'la_credit_amount', 'la_installment_rate', 'la_existing_credits',
    'la_residence_since', 'la_age', 'la_dependents'
]);

/* =================================================================
   BORROWER APPLICATION STATUS SYNC

   Reads the SAME loan_applications.status column the lender workspace
   writes to via GET /api/loan-applications (added alongside the
   existing POST on this route) — there is no separate status store.
   The list is fetched once on load and re-polled on a light interval
   so the borrower sees lender decisions without a manual refresh.
================================================================== */

// Mirrors the <option> labels in the Purpose <select> above — the
// backend only stores the raw code (e.g. "A46"), so this maps it back
// to a human-readable label for display here.
const LA_PURPOSE_LABELS = {
    A40: 'Car (new)', A41: 'Car (used)', A42: 'Furniture / equipment',
    A43: 'Radio / television', A44: 'Domestic appliances', A45: 'Repairs',
    A46: 'Education', A48: 'Retraining', A49: 'Business', A410: 'Other'
};

const LA_STATUS_META = {
    PENDING: {
        label: 'Under review',
        message: 'Your loan application is currently under review.',
        badgeStyle: 'color:#b45309;background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.4);'
    },
    APPROVED: {
        label: 'Approved',
        message: 'Your loan application has been approved by the lender.',
        badgeStyle: 'color:#15803d;background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.4);'
    },
    REJECTED: {
        label: 'Not approved',
        message: 'Your loan application was not approved by the lender.',
        badgeStyle: 'color:#b91c1c;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);'
    },
    WITHDRAWN: {
        label: 'Withdrawn',
        message: 'Your loan application has been withdrawn.',
        badgeStyle: 'color:#475569;background:rgba(100,116,139,0.12);border:1px solid rgba(100,116,139,0.4);'
    }
};

// application_id -> last-seen status. Only used to detect a CHANGE while
// the page is open; the initial load populates this without notifying
// (there's nothing to "change" from yet), and a brand-new application
// (from a fresh submit) is likewise never in this map, so it never
// fires a spurious notification either.
const laLastKnownStatuses = new Map();
let laApplicationsPollTimer = null;
let laStatusToastTimer = null;

function laFormatDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function laApplicationCardHtml(app) {
    const meta = LA_STATUS_META[app.status] || {
        label: app.status || 'Unknown', message: '', badgeStyle: 'color:#475569;background:rgba(100,116,139,0.12);border:1px solid rgba(100,116,139,0.4);'
    };
    const purposeLabel = LA_PURPOSE_LABELS[app.purpose] || app.purpose || '—';
    const amount = (app.loan_amount === null || app.loan_amount === undefined || app.loan_amount === '')
        ? '—' : `${Number(app.loan_amount).toLocaleString()} model units`;

    // Withdraw is only ever offered while the application is still
    // PENDING — never for APPROVED/REJECTED/WITHDRAWN. Delegated click
    // handling lives on the list container (see initLaApplicationsList),
    // so this button never needs its own listener re-attached on re-render.
    const withdrawHtml = app.status === 'PENDING'
        ? `
            <div style="margin-top:12px;">
                <button type="button" class="btn-secondary la-withdraw-btn"
                    data-application-id="${escapeLaHtml(app.application_id)}"
                    style="border-color:var(--danger, #ef4444); color:var(--danger, #ef4444);">
                    <i class="fa-solid fa-ban"></i> Withdraw Application
                </button>
            </div>`
        : '';

    return `
        <div class="health-score-card" style="margin-bottom:12px;">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;">
                <div>
                    <p class="card-subtitle" style="margin:0;">Application #${escapeLaHtml(app.application_id)} &middot; ${escapeLaHtml(app.lender_name)}</p>
                    <h3 class="card-title" style="margin:4px 0 0;">${escapeLaHtml(purposeLabel)}</h3>
                </div>
                <span style="display:inline-flex; align-items:center; gap:6px; font-size:0.72rem; font-weight:700; padding:4px 10px; border-radius:999px; text-transform:uppercase; letter-spacing:0.04em; ${meta.badgeStyle}">
                    ${escapeLaHtml(meta.label)}
                </span>
            </div>
            <div style="display:flex; gap:24px; flex-wrap:wrap; margin-top:10px;">
                <div><span class="ca-field-hint" style="display:block;">Loan Amount</span><strong>${escapeLaHtml(amount)}</strong></div>
                <div><span class="ca-field-hint" style="display:block;">Submitted</span><strong>${escapeLaHtml(laFormatDate(app.submitted_at))}</strong></div>
            </div>
            <p class="ca-field-hint" style="margin:10px 0 0;">${escapeLaHtml(meta.message)}</p>
            ${withdrawHtml}
        </div>
    `;
}

function laRenderApplications(applications) {
    const container = document.getElementById('laApplicationsList');
    if (!container) return;
    if (!applications || applications.length === 0) {
        container.innerHTML = '<p class="ca-field-hint" style="margin:0;">You haven\'t submitted any applications yet.</p>';
        return;
    }
    container.innerHTML = applications.map(laApplicationCardHtml).join('');
}

/* -----------------------------------------------------------------
   WITHDRAWAL CONFIRMATION (Phase 5)

   No modal framework exists elsewhere on this page, so this is a small
   self-contained confirm dialog (same "create the DOM node on demand"
   pattern already used by laShowStatusToast above) rather than a new
   framework. Resolves true only if the borrower clicks the destructive
   "Withdraw Application" button.
----------------------------------------------------------------- */
function laOpenWithdrawConfirm() {
    return new Promise(resolve => {
        const existing = document.getElementById('laConfirmOverlay');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = 'laConfirmOverlay';
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(15,23,42,0.45); display:flex; align-items:center; justify-content:center; z-index:2100; padding:16px;';
        overlay.innerHTML = `
            <div class="health-score-card" style="max-width:380px; width:100%; margin:0;">
                <h3 class="card-title" style="margin-top:0;">Withdraw this application?</h3>
                <p class="ca-field-hint" style="margin:8px 0 20px;">Once withdrawn, the lender will no longer be able to approve or reject this application.</p>
                <div class="form-actions" style="justify-content:flex-end; gap:10px; margin:0;">
                    <button type="button" class="btn-secondary" id="laConfirmCancelBtn">Cancel</button>
                    <button type="button" class="btn-primary" id="laConfirmWithdrawBtn" style="background:var(--danger, #ef4444); border-color:var(--danger, #ef4444);">Withdraw Application</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const cleanup = (result) => {
            overlay.remove();
            resolve(result);
        };
        document.getElementById('laConfirmCancelBtn').addEventListener('click', () => cleanup(false));
        document.getElementById('laConfirmWithdrawBtn').addEventListener('click', () => cleanup(true));
        overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(false); });
    });
}

// Submits the withdrawal. On success we simply re-fetch the borrower's
// applications from the same GET /api/loan-applications endpoint the
// polling loop already uses — that single refresh updates the card's
// status/badge/message, removes the Withdraw button (status is no
// longer PENDING), and runs through the existing status-change diff,
// which naturally raises the same "Your loan application has been
// withdrawn." notification used for lender decisions. No separate
// status store or render path is introduced.
async function laSubmitWithdraw(applicationId, btn) {
    if (btn) {
        btn.disabled = true;
        btn.dataset.originalHtml = btn.dataset.originalHtml || btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Withdrawing...';
    }

    try {
        const res = await fetch(`/api/loan-applications/${encodeURIComponent(applicationId)}/withdraw`, {
            method: 'POST',
            credentials: 'include'
        });
        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        if (!res.ok || !data || data.status !== 'success') {
            const message = (data && Array.isArray(data.errors) && data.errors[0])
                || 'Could not withdraw this application. Please try again.';
            // Reused purely for its red-accent styling — not an actual
            // status change.
            laShowStatusToast(message, 'REJECTED');
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = btn.dataset.originalHtml;
            }
            // The conflict may mean the lender just decided it — refresh
            // so the card reflects whatever the real current status is.
            laFetchApplications();
            return;
        }

        laFetchApplications();
    } catch (networkErr) {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = btn.dataset.originalHtml;
        }
    }
}

function laShowStatusToast(message, status) {
    let toast = document.getElementById('laStatusToast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'laStatusToast';
        toast.style.cssText = 'position:fixed; right:20px; bottom:20px; z-index:2000; max-width:340px; padding:12px 16px; border-radius:8px; font-size:13px; font-weight:600; background:#0f172a; color:#fff; box-shadow:0 12px 30px rgba(0,0,0,0.25); transition:opacity .2s ease;';
        document.body.appendChild(toast);
    }
    const accent = status === 'APPROVED' ? '#22c55e' : status === 'REJECTED' ? '#ef4444' : '#f59e0b';
    toast.style.borderLeft = `4px solid ${accent}`;
    toast.textContent = status === 'APPROVED' ? `✓ ${message}` : message;
    toast.style.opacity = '1';

    clearTimeout(laStatusToastTimer);
    laStatusToastTimer = setTimeout(() => { toast.style.opacity = '0'; }, 5000);
}

// Only notifies for applications we'd already seen with a DIFFERENT
// status — never on first sight of an id, so this can't spam the
// borrower on initial load or right after a fresh submission.
function laApplyStatusDiff(applications) {
    applications.forEach(app => {
        const prevStatus = laLastKnownStatuses.get(app.application_id);
        if (prevStatus !== undefined && prevStatus !== app.status) {
            const meta = LA_STATUS_META[app.status];
            if (meta) laShowStatusToast(meta.message, app.status);
        }
        laLastKnownStatuses.set(app.application_id, app.status);
    });
}

async function laFetchApplications() {
    try {
        const res = await fetch('/api/loan-applications', { method: 'GET', credentials: 'include' });
        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }
        if (!res.ok || !data || data.status !== 'success' || !Array.isArray(data.applications)) return;

        laApplyStatusDiff(data.applications);
        laRenderApplications(data.applications);
    } catch (networkErr) {
        // Silent — a transient failure to refresh the status list
        // shouldn't disrupt the rest of the page.
    }
}

function initLaApplicationsList() {
    laFetchApplications();
    if (laApplicationsPollTimer) clearInterval(laApplicationsPollTimer);
    laApplicationsPollTimer = setInterval(laFetchApplications, 20000);

    // Delegated on the list container (not per-card), so it survives
    // every laRenderApplications() re-render without ever double-binding.
    const container = document.getElementById('laApplicationsList');
    if (container && !container.dataset.laWithdrawBound) {
        container.dataset.laWithdrawBound = '1';
        container.addEventListener('click', async (e) => {
            const btn = e.target.closest('.la-withdraw-btn');
            if (!btn) return;
            const applicationId = btn.getAttribute('data-application-id');
            if (!applicationId) return;

            const confirmed = await laOpenWithdrawConfirm();
            if (!confirmed) return;
            laSubmitWithdraw(applicationId, btn);
        });
    }
}

function initLoanApplication() {
    const form = document.getElementById('loanApplicationForm');
    if (!form) return;
    form.addEventListener('submit', handleLaSubmit);

    const resetBtn = document.getElementById('laResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', () => {
        form.reset();
        renderLaErrors([]);
        document.getElementById('laFormSuccess').classList.add('hidden');
    });
}

function escapeLaHtml(str) {
    const div = document.createElement('div');
    div.textContent = str === null || str === undefined ? '' : String(str);
    return div.innerHTML;
}

function laApiFieldName(inputId) { return inputId.replace(/^la_/, ''); }

function laLabelFor(inputId) {
    const label = document.querySelector(`label[for="${inputId}"]`);
    return label ? label.textContent.replace(/\s+/g, ' ').trim() : inputId;
}

function gatherLaApplicant() {
    const applicant = {};
    LA_FIELD_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const raw = el.value;
        applicant[laApiFieldName(id)] = LA_NUMERIC_IDS.has(id) ? (raw === '' ? null : Number(raw)) : raw;
    });
    return applicant;
}

function validateLaForm() {
    const errors = [];

    const lenderEl = document.getElementById('la_lender_id');
    if (!lenderEl || lenderEl.value === '') {
        errors.push('Please select a lender.');
    }

    LA_FIELD_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.value === '' || el.value === null) {
            errors.push(`${laLabelFor(id)} is required.`);
            return;
        }
        if (LA_NUMERIC_IDS.has(id) && Number.isNaN(Number(el.value))) {
            errors.push(`${laLabelFor(id)} must be a number.`);
        }
    });

    return errors;
}

function renderLaErrors(errors) {
    const box = document.getElementById('laFormErrors');
    if (!box) return;
    if (!errors || errors.length === 0) {
        box.innerHTML = '';
        box.classList.add('hidden');
        return;
    }
    box.innerHTML = errors.map(m => `
        <div class="ca-error-item"><i class="fa-solid fa-circle-exclamation"></i><span>${escapeLaHtml(m)}</span></div>
    `).join('');
    box.classList.remove('hidden');
}

function renderLaSuccess(data) {
    const box = document.getElementById('laFormSuccess');
    box.innerHTML = `
        <div class="health-score-card" style="border-color: var(--success, #22c55e);">
            <h3 class="card-title" style="display:flex;align-items:center;gap:8px;">
                <i class="fa-solid fa-circle-check" style="color: var(--success, #22c55e);"></i>
                Application submitted
            </h3>
            <p class="card-subtitle">Reference: ${escapeLaHtml(data.application_id)} &middot; Status:
                ${escapeLaHtml(data.application_status)}</p>
            <p class="ca-field-hint" style="margin:0;">Your application has been sent to the selected lender
                for review. You can track its status in Your Applications above.</p>
        </div>
    `;
    box.classList.remove('hidden');
}

async function handleLaSubmit(e) {
    e.preventDefault();
    if (laSubmitInFlight) return;

    renderLaErrors([]);
    document.getElementById('laFormSuccess').classList.add('hidden');

    const clientErrors = validateLaForm();
    if (clientErrors.length > 0) {
        renderLaErrors(clientErrors);
        return;
    }

    const lenderId = document.getElementById('la_lender_id').value;
    const applicant = gatherLaApplicant();

    const btn = document.getElementById('laSubmitBtn');
    laSubmitInFlight = true;
    if (btn) {
        btn.disabled = true;
        btn.dataset.originalHtml = btn.dataset.originalHtml || btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Submitting...';
    }

    try {
        const res = await fetch('/api/loan-applications', {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lender_id: lenderId, applicant })
        });
        let data = null;
        try { data = await res.json(); } catch (parseErr) { data = null; }

        if (!res.ok || !data || data.status !== 'success') {
            const msgs = data && Array.isArray(data.errors) ? data.errors : ['We couldn\'t submit your application. Please try again.'];
            renderLaErrors(msgs);
            return;
        }

        document.getElementById('loanApplicationForm').reset();
        renderLaSuccess(data);
        laFetchApplications();
    } catch (networkErr) {
        renderLaErrors(['Couldn\'t reach the server. Check your connection and try again.']);
    } finally {
        laSubmitInFlight = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = btn.dataset.originalHtml;
        }
    }
}