'use strict';

/* ================================================================
   TRANSACTIONS.JS  — Full rewrite
   Aligned to: index.html · payment_modals.html · preview.py

   FinTrust does not hold user money. Every expense is settled
   externally (the user's own UPI app); this file only previews
   the financial impact and then records the outcome.

   ── ID MAP ──────────────────────────────────────────────────────
   Add form
     #transactionForm   submit  → submitTransaction
     #submitTxBtn       submit button
     #resetTxBtn        click   → resetTransactionForm
     #description       text input
     #amount            number input
     #transactionType   select  (expense | income)
     #category          select
     #notes             textarea

   Tx-preview modal  (#txPreviewModal)  — payment_modals.html §1
     #previewRiskBadge      risk level pill
     #previewWarningText    warning message
     #previewSurplusAfter   surplus stat
     #previewBudgetAfter    budget-used stat
     #previewSavingsAfter   savings-rate stat
     #previewGoalImpact     goal note paragraph

   UPI confirm modal  (#upiConfirmModal)  — payment_modals.html §2
     #upiConfirmAmount      amount display
     #upiConfirmDesc        description display
     #upiRefInput           optional UTR field

   Edit modal  (#editModal)  — index.html
     #editId  #editDescription  #editAmount  #editType
     #editCategory  #editDate
     #closeEditModalBtn  #cancelEditBtn  #saveEditBtn

   CSV/PDF preview modal  (#previewModal)  — index.html inline <script>
     showPreview() editField() removeRow() confirmImport()
     closePreview() previewData  → all defined in inline <script>,
     NOT redefined here.  getCategoryOptions() is defined here
     because the inline script calls it.
================================================================ */


/* ── MODULE STATE ───────────────────────────────────────────────── */
let _pendingTx = null;   // expense held between preview → payment steps
let _autoDetectTimer = null;

/* ================================================================
   LIVE CATEGORY AUTO-DETECT
   Fires 500ms after the user stops typing in #description.
   Calls GET /suggest-category?description=... and updates the
   #category select if it is still on "Auto-detect" (value "").
================================================================ */
function _initAutoDetect() {
    const descEl = document.getElementById('description');
    const catEl = document.getElementById('category');
    if (!descEl || !catEl) return;

    descEl.addEventListener('input', () => {
        clearTimeout(_autoDetectTimer);
        const val = descEl.value.trim();

        // Reset hint if field is cleared
        if (!val) {
            _setAutoDetectHint('');
            return;
        }

        // Only auto-detect when user hasn't manually picked a category
        if (catEl.value !== '') return;

        _autoDetectTimer = setTimeout(async () => {
            try {
                const res = await authFetch(
                    `/suggest-category?description=${encodeURIComponent(val)}`
                );
                if (!res || !res.ok) return;
                const data = await res.json();
                const suggested = data.category;
                if (!suggested || suggested === 'Misc') {
                    _setAutoDetectHint('');
                    return;
                }
                // Only apply if user still hasn't manually chosen
                if (catEl.value === '') {
                    _setAutoDetectHint(suggested);
                }
            } catch (_) { /* non-critical */ }
        }, 500);
    });

    // Reset hint when user manually picks a category
    catEl.addEventListener('change', () => {
        _setAutoDetectHint('');
    });
}

function _setAutoDetectHint(category) {
    const catEl = document.getElementById('category');
    let hintEl = document.getElementById('categoryAutoHint');

    if (!category) {
        if (hintEl) hintEl.remove();
        return;
    }

    if (!hintEl) {
        hintEl = document.createElement('p');
        hintEl.id = 'categoryAutoHint';
        hintEl.style.cssText = 'font-size:0.78rem;color:var(--primary);margin-top:4px;display:flex;align-items:center;gap:5px;';
        catEl?.parentElement?.appendChild(hintEl);
    }

    hintEl.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI detected: <strong>${escapeHtml(category)}</strong> — will be applied on save`;
}


/* ================================================================
   ADD TRANSACTION
   Wired to: #transactionForm  addEventListener('submit', submitTransaction)
================================================================ */
async function submitTransaction(e) {
    e.preventDefault();

    const btn = document.getElementById('submitTxBtn');
    if (btn.disabled) return;

    const catEl = document.getElementById('category');
    // If blank (Auto-detect option selected), send "auto-detect" so backend
    // explicitly runs get_smart_category instead of falling through to "Misc"
    const categoryValue = catEl.value || 'auto-detect';

    const payload = {
        description: document.getElementById('description').value.trim(),
        amount: parseFloat(document.getElementById('amount').value),
        type: document.getElementById('transactionType').value,
        category: categoryValue,
        notes: document.getElementById('notes')?.value?.trim() || '',
        date: new Date().toISOString().slice(0, 10),
    };

    /* Client-side validation */
    if (!payload.description)
        return showNotification('Enter a description', 'error');
    if (!payload.amount || payload.amount <= 0)
        return showNotification('Enter a valid amount', 'error');

    /* Income → no preview needed, save directly */
    if (payload.type === 'income') {
        return _directSave(payload);
    }

    /* Expense → call /preview-transaction, show impact modal */
    setButtonLoading(btn, 'Analysing…');

    try {
        const res = await authFetch('/preview-transaction', {
            method: 'POST',
            body: JSON.stringify(payload),
        });

        if (!res || !res.ok) throw new Error('Preview unavailable');

        const data = await res.json();
        _pendingTx = payload;
        _showPreviewModal(data);

    } catch (err) {
        console.warn('Preview failed — falling back to direct save:', err);
        await _directSave(payload);
    } finally {
        resetButton(btn, '<i class="fa-solid fa-plus" aria-hidden="true"></i> Add Transaction');
    }
}


/* ================================================================
   TRANSACTION PREVIEW MODAL  (#txPreviewModal)
   Populated from /preview-transaction response (preview.py).

   Response shape:
     { warning, level, new_surplus, budget_after,
       savings_rate_after, goal_impact,
       current_expense, current_surplus, budget }
================================================================ */
function _showPreviewModal(preview) {
    const modal = document.getElementById('txPreviewModal');

    /* Not in DOM yet → fall back silently */
    if (!modal) {
        console.warn('txPreviewModal missing — direct save fallback');
        return _directSave(_pendingTx);
    }

    /* =========================
       NORMALIZE OLD + NEW API
    ========================= */
    const level = preview.risk_level || preview.level || "low";
    const new_surplus = preview.impact?.new_surplus ?? preview.new_surplus ?? 0;
    const budget_after = preview.impact?.budget_after ?? preview.budget_after ?? 0;
    const savings_after = preview.impact?.savings_rate_after ?? preview.savings_rate_after ?? 0;

    /* =========================
       RISK BADGE (KEEP STYLE)
    ========================= */
    const RISK = {
        low: { label: 'Low Risk', bg: 'rgba(16,185,129,0.18)', color: '#10b981' },
        medium: { label: 'Medium Risk', bg: 'rgba(245,158,11,0.18)', color: '#f59e0b' },
        high: { label: 'High Risk', bg: 'rgba(239,68,68,0.18)', color: '#ef4444' },
    };

    const risk = RISK[level] || RISK.low;

    const badge = document.getElementById('previewRiskBadge');
    if (badge) {
        badge.textContent = risk.label;
        badge.style.background = risk.bg;
        badge.style.color = risk.color;
    }

    /* =========================
       SMART WARNING TEXT (UPGRADED)
    ========================= */
    const warningEl = document.getElementById('previewWarningText');

    if (warningEl) {
        let messages = [];

        if (preview.risk_reason?.length) {
            messages.push(...preview.risk_reason);
        }

        if (preview.warnings?.length) {
            messages.push(...preview.warnings);
        }

        if (preview.category_warning) {
            messages.push(preview.category_warning);
        }

        if (preview.forecast_warning) {
            messages.push(preview.forecast_warning);
        }

        if (messages.length === 0) {
            messages.push(preview.warning || "No major financial risks detected.");
        }

        warningEl.innerHTML = messages.map(m => `⚠️ ${m}`).join('<br>');
        warningEl.style.color = risk.color;
    }

    /* =========================
       IMPACT STATS (KEEP FORMAT)
    ========================= */
    _setText('previewSurplusAfter', `₹${_fmt(new_surplus)}`);
    _setText('previewBudgetAfter', `${budget_after}%`);
    _setText('previewSavingsAfter', `${savings_after}%`);

    /* Surplus color */
    const surplusEl = document.getElementById('previewSurplusAfter');
    if (surplusEl) {
        surplusEl.style.color = new_surplus < 0
            ? 'var(--danger)'
            : 'var(--text-primary)';
    }

    /* =========================
       GOAL IMPACT (UPGRADED)
    ========================= */
    const goalEl = document.getElementById('previewGoalImpact');

    if (goalEl) {
        if (Array.isArray(preview.goal_impact) && preview.goal_impact.length > 0) {
            goalEl.innerHTML = preview.goal_impact
                .map(g => `🎯 ${g}`)
                .join('<br>');
            goalEl.style.display = '';
        } else if (typeof preview.goal_impact === "string") {
            goalEl.textContent = preview.goal_impact;
            goalEl.style.display = '';
        } else {
            goalEl.style.display = 'none';
        }
    }

    /* =========================
       RECOMMENDATIONS (NEW, SAFE)
    ========================= */
    let recBox = document.getElementById('previewRecommendations');

    if (!recBox && warningEl) {
        recBox = document.createElement('div');
        recBox.id = 'previewRecommendations';
        recBox.style.marginTop = '8px';
        warningEl.parentNode.appendChild(recBox);
    }

    if (recBox) {
        if (preview.recommendations?.length) {
            recBox.innerHTML = preview.recommendations
                .map(r => `💡 ${r}`)
                .join('<br>');
            recBox.style.display = '';
        } else {
            recBox.style.display = 'none';
        }
    }

    /* =========================
       KEEP ANIMATION (IMPORTANT)
    ========================= */
    const sheet = modal.querySelector('.pay-sheet');
    if (sheet) {
        sheet.classList.remove('preview-enter');
        void sheet.offsetWidth;
        sheet.classList.add('preview-enter');
    }

    modal.classList.remove('hidden');
}

/** Close the tx-preview modal and discard pending tx */
function closePreviewModal() {
    document.getElementById('txPreviewModal')?.classList.add('hidden');
    _pendingTx = null;
}



/* ================================================================
   UPI PAYMENT FLOW
   Button: pay-btn-upi  onclick="proceedWithUPI()"
   Opens #upiConfirmModal instead of window.confirm.
   Backend: POST /confirm-upi-transaction  (preview.py)
            Body: { ...payload, upi_ref }
================================================================ */
function proceedWithUPI() {
    if (!_pendingTx) return;

    /* Close the preview modal first */
    document.getElementById('txPreviewModal')?.classList.add('hidden');

    /* Open UPI app via deep-link */
    const upiLink =
        `upi://pay?pa=FinTrust@upi&pn=FinTrust&am=${_pendingTx.amount}&cu=INR`;
    window.location.href = upiLink;

    /* Populate and open the UPI confirm modal */
    _setText('upiConfirmAmount', `₹${_fmt(_pendingTx.amount)}`);
    _setText('upiConfirmDesc', _pendingTx.description || '—');

    const refInput = document.getElementById('upiRefInput');
    if (refInput) refInput.value = '';

    document.getElementById('upiConfirmModal')?.classList.remove('hidden');
}

function showUPIQR(upiLink) {
    const qrUrl = `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(upiLink)}`;

    const modal = document.getElementById("upiQRModal");
    if (!modal) return;

    modal.querySelector("img").src = qrUrl;
    modal.classList.remove("hidden");
}

/** Called by "Yes, payment done!" button in #upiConfirmModal */
async function confirmUPISuccess() {
    if (!_pendingTx) return;

    const upiRef = (document.getElementById('upiRefInput')?.value || '').trim();

    /* Close UPI modal */
    document.getElementById('upiConfirmModal')?.classList.add('hidden');

    try {
        const res = await authFetch('/confirm-upi-transaction', {
            method: 'POST',
            body: JSON.stringify({ ..._pendingTx, upi_ref: upiRef }),
        });

        if (!res || !res.ok) throw new Error('UPI confirmation rejected');

        showNotification('UPI payment saved ✅', 'success');
        _afterSuccess();

    } catch (err) {
        console.warn('UPI save failed — falling back to direct save:', err);
        showNotification('UPI save failed — saving directly', 'warning');
        await _directSave(_pendingTx ?? {});
    }
}

/** Called by "No — I did not pay" in #upiConfirmModal */
function cancelUPIPayment() {
    document.getElementById('upiConfirmModal')?.classList.add('hidden');
    showNotification('UPI payment cancelled', 'warning');
    _pendingTx = null;
}


/* ================================================================
   DIRECT SAVE  — income shortcut + all error fallback paths
   Backend: POST /add-transaction
================================================================ */
async function _directSave(payload) {
    const btn = document.getElementById('submitTxBtn');
    setButtonLoading(btn, 'Saving…');

    try {
        const res = await authFetch('/add-transaction', {
            method: 'POST',
            body: JSON.stringify(payload),
        });

        if (!res || !res.ok) throw new Error('Server rejected the transaction');

        showNotification('Transaction added successfully', 'success');
        _afterSuccess();

    } catch (err) {
        showNotification('Failed to save transaction', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-plus" aria-hidden="true"></i> Add Transaction');
    }
}


/* ================================================================
   POST-SUCCESS CLEANUP
   Runs after every successful save path (UPI / direct).
================================================================ */
function _afterSuccess() {
    _resetForm();
    _pendingTx = null;

    /* Refresh dashboard totals and charts */
    loadDashboard();

    /* Switch to history after a short delay */
    setTimeout(() => showPage('history'), 400);
}


/* ================================================================
   FORM RESET
================================================================ */
function _resetForm() {
    ['description', 'amount', 'notes'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });

    const typeEl = document.getElementById('transactionType');
    const catEl = document.getElementById('category');
    if (typeEl) typeEl.selectedIndex = 0;
    if (catEl) catEl.selectedIndex = 0;
}

/** Public handler — wired to #resetTxBtn click */
function resetTransactionForm(e) {
    e.preventDefault();
    _resetForm();
}


/* ================================================================
   EDIT TRANSACTION MODAL
   openEditModal() — called by history.js per-row edit icon
   closeEditModal()  — #closeEditModalBtn, #cancelEditBtn
   saveEdit()        — #saveEditBtn
   Backend: PUT /update-transaction/:id
================================================================ */
function openEditModal(id, description, amount, category, type, date) {
    document.getElementById('editId').value = id;
    document.getElementById('editDescription').value = description;
    document.getElementById('editAmount').value = amount;
    document.getElementById('editType').value = type;
    document.getElementById('editCategory').value = category;
    document.getElementById('editDate').value = date;
    document.getElementById('editModal').classList.remove('hidden');
}

function closeEditModal() {
    document.getElementById('editModal').classList.add('hidden');
}

async function saveEdit() {
    const btn = document.getElementById('saveEditBtn');
    if (btn.disabled) return;

    const id = document.getElementById('editId').value;
    const payload = {
        description: document.getElementById('editDescription').value.trim(),
        amount: parseFloat(document.getElementById('editAmount').value),
        type: document.getElementById('editType').value,
        category: document.getElementById('editCategory').value,
        date: document.getElementById('editDate').value,
    };

    setButtonLoading(btn, 'Saving…');

    try {
        const res = await authFetch(`/update-transaction/${id}`, {
            method: 'PUT',
            body: JSON.stringify(payload),
        });

        if (!res || !res.ok) throw new Error('Update rejected by server');

        showNotification('Transaction updated', 'success');
        closeEditModal();
        loadHistory();
        loadDashboard();

    } catch (err) {
        showNotification('Update failed', 'error');
    } finally {
        resetButton(
            btn,
            '<i class="fa-solid fa-floppy-disk" aria-hidden="true"></i> Save Changes'
        );
    }
}


/* ================================================================
   CSV / PDF IMPORT HELPER
   getCategoryOptions() is called from the inline <script> in
   index.html inside showPreview() to build per-row category
   dropdowns in #previewTableBody.

   previewData · showPreview() · editField() · removeRow()
   confirmImport() · closePreview()  all live in the inline
   <script> at the bottom of index.html — NOT redefined here.
================================================================ */

/** Category list matches <select id="category"> options in the add form */
const CATEGORIES = [
    'Food', 'Transport', 'Housing', 'Shopping',
    'Health', 'Education', 'Entertainment', 'Finance', 'Misc',
];

/**
 * Build <option> HTML for a category <select>.
 * @param {string} selected  value to pre-select
 * @returns {string}
 */
function getCategoryOptions(selected) {
    return CATEGORIES
        .map(c => `<option value="${c}"${c === selected ? ' selected' : ''}>${c}</option>`)
        .join('');
}


/* ================================================================
   PDF / CSV IMPORT — confirmImport()
   ----------------------------------------------------------------
   NOTE ON SCOPE: `previewData`, `showPreview()`, `editField()`,
   `removeRow()`, and `closePreview()` live in the inline <script>
   in index.html, not in this file (see header comment above). This
   file only has access to routes/transactions.py and
   static/js/transactions.js, so the fix below is implemented as a
   drop-in replacement for confirmImport(): remove the old
   confirmImport() function body from the inline <script> in
   index.html and either delete it there or leave it removed, since
   this file now defines window.confirmImport and will be the one
   that runs (as long as this <script src="transactions.js"> tag is
   loaded AFTER the inline script, which is already required today
   since the inline script calls getCategoryOptions() from this
   file). If your <script> tags are ordered the other way, move this
   block into the inline <script> instead — the logic is unchanged.

   OLD BEHAVIOR (being replaced): confirmImport() re-sent the PDF/CSV
   file (or re-triggered a parse) when importing, so the statement
   was parsed once for preview and again for import.

   NEW BEHAVIOR: confirmImport() sends the already-parsed,
   already-reviewed `previewData` array straight to the dedicated
   bulk-import endpoint. The backend never sees the PDF again and
   never re-runs categorization — whatever the user edited in the
   preview table is exactly what gets imported.
================================================================ */

let _importInFlight = false;

// Set true locally to log a step-by-step timing breakdown of the
// Import All flow (click → request → response → preview closed →
// history/dashboard refresh). Leave false in production.
const IMPORT_PROFILE = false;

/**
 * Called by the "Import All" button in the CSV/PDF preview modal.
 * Sends window.previewData (as reviewed/edited by the user via
 * editField()/removeRow()) to POST /import-preview-transactions.
 */
async function confirmImport() {
    if (_importInFlight) return;

    const _t0 = performance.now();
    const _tlog = (label) => {
        if (IMPORT_PROFILE) {
            console.log(`[IMPORT UI] ${label}: ${((performance.now() - _t0) / 1000).toFixed(3)}s`);
        }
    };

    // previewData is maintained by showPreview()/editField()/removeRow()
    // in the inline <script>. It's read here via window.previewData so
    // this function works regardless of how it was declared there.
    const rows = window.previewData || (typeof previewData !== 'undefined' ? previewData : null);

    if (!rows || !rows.length) {
        showNotification('No transactions to import', 'error');
        return;
    }

    const importBtn = document.getElementById('confirmImportBtn')
        || document.getElementById('importAllBtn');

    _importInFlight = true;
    if (importBtn) {
        setButtonLoading(importBtn, 'Importing…');
        importBtn.disabled = true;
    }

    _tlog('import clicked');

    try {
        const res = await authFetch('/import-preview-transactions', {
            method: 'POST',
            body: JSON.stringify({ transactions: rows }),
        });
        _tlog('response received');

        if (!res || !res.ok) {
            const errData = await res?.json().catch(() => null);
            throw new Error(errData?.error || 'Import failed');
        }

        const data = await res.json();
        const count = data.imported ?? data.count ?? rows.length;

        showNotification(`Imported ${count} transaction${count === 1 ? '' : 's'} ✅`, 'success');

        // Close the preview modal as soon as the server confirms success —
        // do NOT wait on history/dashboard refresh first.
        if (typeof closePreview === 'function') {
            closePreview();
        } else {
            document.getElementById('previewModal')?.classList.add('hidden');
        }
        _tlog('preview closed');

        // Refresh transaction history / dashboard ASYNCHRONOUSLY —
        // intentionally not awaited, so a slow dashboard/history fetch
        // never delays the user seeing "Imported X transactions" or
        // blocks the modal from closing. Errors here are non-fatal to
        // the import, which already succeeded server-side.
        if (typeof loadHistory === 'function') {
            Promise.resolve(loadHistory())
                .then(() => _tlog('history refresh completed'))
                .catch(err => console.warn('loadHistory failed after import:', err));
        }
        if (typeof loadDashboard === 'function') {
            Promise.resolve(loadDashboard())
                .then(() => _tlog('dashboard refresh completed'))
                .catch(err => console.warn('loadDashboard failed after import:', err));
        }

    } catch (err) {
        console.error('confirmImport failed:', err);
        showNotification(err.message || 'Import failed — please try again', 'error');
        // Keep the preview modal open on failure so the user can retry.
    } finally {
        _importInFlight = false;
        if (importBtn) {
            resetButton(importBtn, 'Import All');
            importBtn.disabled = false;
        }
    }
}

window.confirmImport = confirmImport;


/* ================================================================
   PRIVATE UTILITIES
================================================================ */

/** Safe textContent setter — no-ops if element absent */
function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

/**
 * Format a number as Indian locale with 2 decimal places.
 * e.g. 12345.6 → "12,345.60"
 */
function _fmt(n) {
    return Number(n).toLocaleString('en-IN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
}


window.proceedWithUPI = proceedWithUPI;
window.confirmUPISuccess = confirmUPISuccess;
window.cancelUPIPayment = cancelUPIPayment;
window.closePreviewModal = closePreviewModal;

// Initialise live auto-detect when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initAutoDetect);
} else {
    _initAutoDetect();
}