'use strict';

/* ================================================================
   GOALS — INTERNAL STATE
================================================================ */
let _progressAction = 'add';

/* ================================================================
   LOAD GOALS
================================================================ */
async function loadGoals() {
    const listContainer = document.getElementById('goalsList');
    const emptyState    = document.getElementById('goalsEmpty');
    const summaryBar    = document.getElementById('goalsSummary');

    if (listContainer) {
        listContainer.innerHTML = `
            <div style="padding:var(--spacing-xl);display:flex;flex-direction:column;gap:var(--spacing-sm);">
                ${Array(3).fill('<div class="skeleton skeleton-line" style="height:120px;border-radius:var(--radius-lg);"></div>').join('')}
            </div>`;
    }

    const res = await authFetch('/get-goals-detailed');
    if (!res) return;

    let goals = [];
    try {
        const json = await res.json();
        const data = json.data ?? json;
        goals = data.goals ?? [];
    } catch (e) {
        console.error('loadGoals parse error', e);
        if (listContainer) listContainer.innerHTML = '';
        return;
    }

    _renderGoalsSummary(goals);
    _renderGoalCards(goals);
}

function _renderGoalsSummary(goals) {
    const summaryBar = document.getElementById('goalsSummary');
    if (!summaryBar) return;

    if (goals.length === 0) { summaryBar.style.display = 'none'; return; }

    summaryBar.style.display = '';
    const completed  = goals.filter(g => g.status === 'completed').length;
    const totalSaved = goals.reduce((s, g) => s + (g.saved_amount  || 0), 0);
    const totalTarget= goals.reduce((s, g) => s + (g.target_amount || 0), 0);

    setEl('summaryTotal',     goals.length);
    setEl('summaryCompleted', completed);
    setEl('summarySaved',  '₹' + Number(totalSaved).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
    setEl('summaryTarget', '₹' + Number(totalTarget).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
}

function _renderGoalCards(goals) {
    const listContainer = document.getElementById('goalsList');
    const emptyState    = document.getElementById('goalsEmpty');

    if (!listContainer) return;

    if (!goals || goals.length === 0) {
        listContainer.innerHTML = '';
        if (emptyState) emptyState.classList.remove('hidden');
        return;
    }

    if (emptyState) emptyState.classList.add('hidden');

    const categoryIcons = {
        Savings:    'fa-piggy-bank',
        Investment: 'fa-chart-line',
        Emergency:  'fa-shield-halved',
        Vacation:   'fa-plane',
        Education:  'fa-graduation-cap',
        Home:       'fa-house',
        Vehicle:    'fa-car',
        Retirement: 'fa-umbrella-beach',
    };

    listContainer.innerHTML = goals.map(g => {
        const saved    = Number(g.saved_amount  || 0);
        const target   = Number(g.target_amount || 0);
        const pct      = g.progress_percent ?? (target > 0 ? Math.min((saved / target) * 100, 100) : 0);
        const status   = g.status ?? 'in_progress';
        const icon     = categoryIcons[g.category] ?? 'fa-bullseye';

        const statusMeta = {
            completed:   { label: 'Completed',   icon: 'fa-circle-check' },
            on_track:    { label: 'On Track',    icon: 'fa-circle-check' },
            at_risk:     { label: 'At Risk',     icon: 'fa-triangle-exclamation' },
            in_progress: { label: 'In Progress', icon: 'fa-clock' },
            no_savings:  { label: 'Not Started', icon: 'fa-circle-pause' },
        };
        const sm = statusMeta[status] ?? statusMeta.in_progress;

        const chips = [];

        if (g.months_to_goal !== null && g.months_to_goal !== undefined && status !== 'completed') {
            const monthLabel = g.months_to_goal <= 0 ? 'Goal near!' : `~${g.months_to_goal} mo left`;
            chips.push(`<span class="goal-chip"><i class="fa-solid fa-clock"></i>${escapeHtml(monthLabel)}</span>`);
        }

        if (g.monthly_saving > 0) {
            chips.push(`<span class="goal-chip"><i class="fa-solid fa-arrow-trend-up"></i>Saving ₹${Number(g.monthly_saving).toLocaleString('en-IN', {maximumFractionDigits:0})}/mo</span>`);
        }

        if (g.required_per_month !== null && g.required_per_month !== undefined && status !== 'completed') {
            chips.push(`<span class="goal-chip"><i class="fa-solid fa-flag"></i>Need ₹${Number(g.required_per_month).toLocaleString('en-IN', {maximumFractionDigits:0})}/mo</span>`);
        }

        if (g.target_date) {
            const fmt = new Date(g.target_date).toLocaleDateString('en-IN', { month:'short', year:'numeric' });
            chips.push(`<span class="goal-chip"><i class="fa-solid fa-calendar"></i>${escapeHtml(fmt)}</span>`);
        }

        const predictionRow = chips.length
            ? `<div class="goal-prediction-row">${chips.join('')}</div>`
            : '';

        const isCompleted = status === 'completed';

        return `
        <div class="goal-card status-${escapeHtml(status)}" data-goal-id="${g.id}">
            <div class="goal-card-header">
                <div class="goal-card-left">
                    <div class="goal-icon" aria-hidden="true">
                        <i class="fa-solid ${icon}"></i>
                    </div>
                    <div class="goal-card-info">
                        <div class="goal-card-name" title="${escapeHtml(g.name)}">${escapeHtml(g.name)}</div>
                        <div class="goal-card-category">${escapeHtml(g.category ?? '')}</div>
                    </div>
                </div>
                <span class="goal-status-badge ${escapeHtml(status)}">
                    <i class="fa-solid ${sm.icon}" aria-hidden="true"></i>
                    ${sm.label}
                </span>
            </div>

            <div class="goal-progress-section">
                <div class="goal-amounts">
                    <span class="goal-saved">₹${Number(saved).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}</span>
                    <span class="goal-target">of ₹${Number(target).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}</span>
                </div>
                <div class="goal-progress-bar" role="progressbar" aria-valuenow="${pct.toFixed(1)}" aria-valuemin="0" aria-valuemax="100">
                    <div class="goal-progress-fill" style="width:${pct.toFixed(1)}%"></div>
                </div>
                <div class="goal-progress-pct">${pct.toFixed(1)}%</div>
            </div>

            ${predictionRow}

            ${!isCompleted ? `
            <div class="goal-card-actions">
                <button class="goal-btn add-funds"
                        onclick="openProgressModal(${g.id},'${escapeJs(g.name)}',${saved},'add')"
                        aria-label="Add funds to ${escapeHtml(g.name)}">
                    <i class="fa-solid fa-plus" aria-hidden="true"></i> Add Funds
                </button>
                <button class="goal-btn withdraw"
                        onclick="openProgressModal(${g.id},'${escapeJs(g.name)}',${saved},'withdraw')"
                        aria-label="Withdraw from ${escapeHtml(g.name)}">
                    <i class="fa-solid fa-minus" aria-hidden="true"></i> Withdraw
                </button>
                <button class="goal-btn view-pred"
                        onclick="openPredictionModal(${g.id},'${escapeJs(g.name)}')"
                        aria-label="View forecast for ${escapeHtml(g.name)}" title="View Forecast">
                    <i class="fa-solid fa-chart-line" aria-hidden="true"></i>
                </button>
                <button class="goal-btn delete-goal"
                        onclick="deleteGoal(${g.id},'${escapeJs(g.name)}')"
                        aria-label="Delete ${escapeHtml(g.name)}" title="Delete Goal">
                    <i class="fa-solid fa-trash" aria-hidden="true"></i>
                </button>
            </div>` : `
            <div style="display:flex;gap:var(--spacing-sm);">
                <div style="flex:1;text-align:center;padding:var(--spacing-sm);background:var(--success-light);border-radius:var(--radius-md);color:var(--success);font-size:.875rem;font-weight:600;">
                    <i class="fa-solid fa-trophy" aria-hidden="true"></i> Goal achieved!
                </div>
                <button class="goal-btn delete-goal" style="flex:0 0 auto;"
                        onclick="deleteGoal(${g.id},'${escapeJs(g.name)}')"
                        aria-label="Delete ${escapeHtml(g.name)}" title="Delete Goal">
                    <i class="fa-solid fa-trash" aria-hidden="true"></i>
                </button>
            </div>`}
        </div>`;
    }).join('');
}

/* ================================================================
   GOAL FORM
================================================================ */
function showGoalForm() {
    const c = document.getElementById('goalFormContainer');
    if (c) {
        c.classList.remove('hidden');
        setTimeout(() => document.getElementById('goalName')?.focus(), 80);
    }
}

function hideGoalForm() {
    const c = document.getElementById('goalFormContainer');
    if (c) c.classList.add('hidden');
}

async function addGoal(e) {
    e.preventDefault();
    const btn = document.getElementById('addGoalBtn');
    setButtonLoading(btn, 'Saving…');

    const payload = {
        name:        document.getElementById('goalName').value.trim(),
        target:      document.getElementById('goalTarget').value,
        category:    document.getElementById('goalCategory').value,
        target_date: document.getElementById('goalTargetDate').value || null,
    };

    try {
        const res = await authFetch('/add-goal', {
            method: 'POST',
            body:   JSON.stringify(payload),
        });

        if (res && (res.ok || res.status === 201)) {
            showNotification('Goal created!', 'success');
            hideGoalForm();
            e.target.reset();
            loadGoals();
        } else {
            let msg = 'Failed to create goal';
            try { const d = await res.json(); msg = d.error || msg; } catch (_) {}
            showNotification(msg, 'error');
        }
    } catch (err) {
        showNotification('Error creating goal', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-floppy-disk" aria-hidden="true"></i> Save Goal');
    }
}

/* ================================================================
   DELETE GOAL
================================================================ */
async function deleteGoal(goalId, goalName) {
    const confirmed = confirm(`Delete goal "${goalName}"?\n\nThis action cannot be undone.`);
    if (!confirmed) return;

    const card = document.querySelector(`.goal-card[data-goal-id="${goalId}"]`);
    if (card) {
        card.style.transition = 'opacity 0.25s, transform 0.25s';
        card.style.opacity    = '0';
        card.style.transform  = 'scale(0.96)';
    }

    try {
        const res = await authFetch(`/delete-goal/${goalId}`, { method: 'DELETE' });

        if (res && res.ok) {
            showNotification(`"${goalName}" deleted`, 'success');
            loadGoals();
        } else {
            if (card) { card.style.opacity = '1'; card.style.transform = ''; }
            let msg = 'Failed to delete goal';
            try { const d = await res.json(); msg = d.error || msg; } catch (_) {}
            showNotification(msg, 'error');
        }
    } catch (err) {
        if (card) { card.style.opacity = '1'; card.style.transform = ''; }
        showNotification('Error deleting goal', 'error');
    }
}

/* ================================================================
   PROGRESS MODAL (Add Funds / Withdraw)
================================================================ */
function openProgressModal(goalId, goalName, currentSaved, action = 'add') {
    document.getElementById('progressGoalId').value  = goalId;
    document.getElementById('progressGoalName').textContent =
        `Goal: ${goalName}`;
    document.getElementById('progressGoalSaved').textContent =
        `Currently saved: ₹${Number(currentSaved).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}`;
    document.getElementById('progressAmount').value = '';
    setProgressAction(action);
    document.getElementById('goalProgressModal').classList.remove('hidden');
    setTimeout(() => document.getElementById('progressAmount').focus(), 80);
}

function closeProgressModal() {
    document.getElementById('goalProgressModal').classList.add('hidden');
    document.getElementById('progressAmount').value = '';
}

function setProgressAction(action) {
    _progressAction = action;
    const addBtn  = document.getElementById('progressToggleAdd');
    const wdwBtn  = document.getElementById('progressToggleWithdraw');
    const submit  = document.getElementById('progressSubmitBtn');

    addBtn.classList.toggle('active', action === 'add');
    wdwBtn.classList.toggle('active', action === 'withdraw');

    if (action === 'add') {
        submit.innerHTML = '<i class="fa-solid fa-arrow-up-right-dots" aria-hidden="true"></i> Add Funds';
    } else {
        submit.innerHTML = '<i class="fa-solid fa-minus" aria-hidden="true"></i> Withdraw';
    }
}

async function submitProgress() {
    const goalId = document.getElementById('progressGoalId').value;
    const amount = parseFloat(document.getElementById('progressAmount').value);

    if (!goalId || isNaN(amount) || amount <= 0) {
        showNotification('Enter a valid amount greater than zero', 'error');
        return;
    }

    const btn = document.getElementById('progressSubmitBtn');
    setButtonLoading(btn, 'Updating…');

    try {
        const res = await authFetch('/update-goal-progress', {
            method: 'POST',
            body: JSON.stringify({
                goal_id: parseInt(goalId, 10),
                amount:  amount,
                action:  _progressAction,
            }),
        });

        if (res && res.ok) {
            const verb = _progressAction === 'add' ? 'added to' : 'withdrawn from';
            showNotification(`₹${amount.toLocaleString('en-IN')} ${verb} goal`, 'success');
            closeProgressModal();
            loadGoals();
        } else {
            let msg = 'Failed to update goal';
            try { const d = await res.json(); msg = d.error || msg; } catch (_) {}
            showNotification(msg, 'error');
        }
    } catch (err) {
        showNotification('Error updating goal', 'error');
    } finally {
        setProgressAction(_progressAction);
        const btn2 = document.getElementById('progressSubmitBtn');
        if (btn2) btn2.disabled = false;
    }
}

/* ================================================================
   PREDICTION MODAL
================================================================ */
async function openPredictionModal(goalId, goalName) {
    const modal = document.getElementById('goalPredictionModal');
    const body  = document.getElementById('predictionModalBody');
    const sub   = document.getElementById('predictionModalSubtitle');

    if (!modal || !body) return;

    document.getElementById('predictionModalTitle').textContent = 'Goal Forecast';
    sub.textContent = goalName;

    body.innerHTML = `
        <div style="text-align:center;padding:var(--spacing-xl);color:var(--text-tertiary);">
            <i class="fa-solid fa-spinner fa-spin" style="font-size:1.5rem;display:block;margin-bottom:.75rem;" aria-hidden="true"></i>
            Loading forecast…
        </div>`;
    modal.classList.remove('hidden');

    try {
        const res = await authFetch(`/goal-prediction/${goalId}`);

        if (!res || !res.ok) {
            body.innerHTML = `<p style="color:var(--danger);text-align:center;">Unable to load forecast. Please try again.</p>`;
            return;
        }

        const data = await res.json();
        const d    = data.data ?? data;

        const status          = d.status ?? 'unknown';
        const monthsToGoal    = d.months_to_goal;
        const monthlySaving   = Number(d.monthly_saving  ?? 0);
        const remainingAmount = Number(d.remaining_amount ?? 0);
        const requiredPerMonth= d.required_per_month != null ? Number(d.required_per_month) : null;

        const statusMeta = {
            completed:   { color: 'var(--success)', icon: 'fa-circle-check',         label: 'Completed' },
            on_track:    { color: 'var(--primary)', icon: 'fa-circle-check',          label: 'On Track' },
            at_risk:     { color: 'var(--danger)',  icon: 'fa-triangle-exclamation',  label: 'At Risk' },
            in_progress: { color: 'var(--warning)', icon: 'fa-clock',                 label: 'In Progress' },
        };
        const sm = statusMeta[status] ?? { color: 'var(--text-secondary)', icon: 'fa-circle-info', label: status };

        const metrics = [];

        metrics.push({
            icon:  'fa-chart-pie',
            label: 'Status',
            value: `<span style="display:inline-flex;align-items:center;gap:6px;color:${sm.color};font-weight:700;">
                        <i class="fa-solid ${sm.icon}" aria-hidden="true"></i> ${sm.label}
                    </span>`,
        });

        metrics.push({
            icon:  'fa-coins',
            label: 'Remaining',
            value: `₹${remainingAmount.toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}`,
        });

        metrics.push({
            icon:  'fa-arrow-trend-up',
            label: 'Current Monthly Saving',
            value: monthlySaving > 0
                ? `₹${monthlySaving.toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}/mo`
                : '<span style="color:var(--text-tertiary);">No savings this month</span>',
        });

        metrics.push({
            icon:  'fa-clock',
            label: 'Estimated Time to Goal',
            value: monthsToGoal === 0
                ? '<span style="color:var(--success);font-weight:700;">Goal reached! 🎉</span>'
                : monthsToGoal != null
                    ? `<strong>~${monthsToGoal} month${monthsToGoal !== 1 ? 's' : ''}</strong>`
                    : '<span style="color:var(--text-tertiary);">Cannot estimate — no savings</span>',
        });

        if (requiredPerMonth !== null) {
            const gap = requiredPerMonth - monthlySaving;
            const gapHtml = gap > 0
                ? `<span style="color:var(--danger);font-size:.8rem;margin-left:8px;">↑ ₹${gap.toLocaleString('en-IN', {maximumFractionDigits:0})} shortfall/mo</span>`
                : `<span style="color:var(--success);font-size:.8rem;margin-left:8px;">✓ Covered</span>`;
            metrics.push({
                icon:  'fa-flag',
                label: 'Required to Hit Deadline',
                value: `₹${requiredPerMonth.toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}/mo ${gapHtml}`,
            });
        }

        body.innerHTML = `
            <div style="display:flex;flex-direction:column;gap:var(--spacing-md);">
                ${metrics.map(m => `
                    <div style="display:flex;align-items:flex-start;gap:var(--spacing-md);padding:var(--spacing-md);background:var(--bg-tertiary);border-radius:var(--radius-md);">
                        <div style="width:36px;height:36px;border-radius:var(--radius-sm);background:var(--bg-elevated);border:1px solid var(--border-subtle);display:flex;align-items:center;justify-content:center;flex-shrink:0;color:var(--text-tertiary);">
                            <i class="fa-solid ${m.icon}" aria-hidden="true"></i>
                        </div>
                        <div style="flex:1;min-width:0;">
                            <div style="font-size:.75rem;font-weight:600;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px;">${m.label}</div>
                            <div style="font-size:.9375rem;font-family:var(--font-mono);color:var(--text-primary);">${m.value}</div>
                        </div>
                    </div>`).join('')}

                ${status === 'at_risk' ? `
                <div style="padding:var(--spacing-md);background:rgba(220,38,38,0.08);border:1px solid rgba(220,38,38,0.20);border-radius:var(--radius-md);color:var(--danger);">
                    <i class="fa-solid fa-triangle-exclamation" style="margin-right:6px;" aria-hidden="true"></i>
                    <strong>Action needed:</strong> Your current savings rate won't meet the deadline.
                    Consider increasing contributions or extending the target date.
                </div>` : ''}

                ${status === 'on_track' ? `
                <div style="padding:var(--spacing-md);background:rgba(37,99,235,0.08);border:1px solid rgba(37,99,235,0.20);border-radius:var(--radius-md);color:var(--primary);">
                    <i class="fa-solid fa-circle-check" style="margin-right:6px;" aria-hidden="true"></i>
                    You're on track! Keep up the current saving pace.
                </div>` : ''}
            </div>`;

    } catch (err) {
        body.innerHTML = `<p style="color:var(--danger);text-align:center;">Error loading forecast. Please try again.</p>`;
        console.error('openPredictionModal error:', err);
    }
}

function closePredictionModal() {
    const modal = document.getElementById('goalPredictionModal');
    if (modal) modal.classList.add('hidden');
}

