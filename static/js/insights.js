'use strict';

/* ================================================================
   INSIGHTS
================================================================ */
function loadInsights() {
    if (insightsLoadInFlight) return;
    insightsLoadInFlight = true;
    Promise.resolve(renderInsights('analysis'))
        .then(() => {
            loadTopCategories();
            loadSpendingInsights();
        })
        .finally(() => { insightsLoadInFlight = false; });
}

let insightsLoadInFlight = false;

/* Monotonically increasing token. Every renderInsights() call captures the
   token at its start; before writing any DOM (tab content or risk panel) it
   checks the token still matches the latest call. This makes overlapping/
   duplicate renderInsights() invocations (e.g. a double-fired tab click,
   or loadInsights() re-entering while a previous render is still in
   flight) safe — a stale, superseded call simply discards its results
   instead of writing them into the DOM after a newer call already has. */
let insightsRenderToken = 0;

/* ================================================================
   SPENDING INSIGHTS — "not enough data yet" state
   Used when /spending-insights returns { status: "insufficient_data" }
   instead of an array — i.e. the current month (or previous month) has
   no expense data, so a % comparison would be meaningless.
================================================================ */
function renderInsufficientDataState(message) {
    const noCurrentData = /no current-month/i.test(message || '');
    const body = noCurrentData
        ? 'No spending activity has been recorded this month yet. Add transactions to see meaningful month-over-month insights.'
        : 'You have spending this month, but not enough data from last month yet for a meaningful comparison.';

    return `
        <div style="text-align:center;padding:var(--spacing-lg);color:var(--text-tertiary);">
            <i class="fa-solid fa-chart-simple" style="font-size:2rem;display:block;margin-bottom:8px;opacity:.6;" aria-hidden="true"></i>
            <p style="font-weight:700;color:var(--text-secondary);">Current Month: Not Enough Data</p>
            <p style="color:var(--text-tertiary);font-size:.875rem;margin-top:4px;">${escapeHtml(body)}</p>
        </div>`;
}

/* ================================================================
   ANOMALIES — Compact spending-outlier UI
   (presentation only — /anomaly-transactions payload is untouched)
================================================================ */
let anomalyState = { items: [], filter: 'all', expanded: new Set(), showAll: false };

function formatINR(n) {
    return Number(n || 0).toLocaleString('en-IN');
}

function anomalySeverityRank(sev) {
    const order = { high: 0, medium: 1, low: 2 };
    return order[String(sev ?? '').toLowerCase()] ?? 3;
}

function anomalyDeviationValue(a) {
    const dev = typeof a.deviation === 'number' ? a.deviation : parseFloat(a.deviation);
    return Number.isFinite(dev) ? dev : 0;
}

function sortAnomalies(list) {
    return [...list].sort((a, b) => {
        const rankDiff = anomalySeverityRank(a.severity) - anomalySeverityRank(b.severity);
        if (rankDiff !== 0) return rankDiff;
        return anomalyDeviationValue(b) - anomalyDeviationValue(a);
    });
}

function anomalyCardKey(a, idx) {
    return String(a.id ?? a.transaction_id ?? idx);
}

function buildAnomalyBodyHTML() {
    const items = anomalyState.items;

    if (!items.length) {
        return `
            <div class="anomaly-empty">
                <i class="fa-solid fa-circle-check" aria-hidden="true"></i>
                <h3>Spending looks normal</h3>
                <p>No transactions are significantly above your usual category spending.</p>
            </div>`;
    }

    const sorted = sortAnomalies(items);
    const categories = [...new Set(items.map(a => a.category).filter(Boolean))];
    const filtered = anomalyState.filter === 'all'
        ? sorted
        : sorted.filter(a => a.category === anomalyState.filter);

    const highest = Math.max(...items.map(a => Number(a.amount) || 0));

    const statsHtml = `
        <div class="anomaly-stats">
            <div class="anomaly-stat"><span class="anomaly-stat-value">${items.length}</span><span class="anomaly-stat-label">Outliers</span></div>
            <div class="anomaly-stat"><span class="anomaly-stat-value">₹${formatINR(highest)}</span><span class="anomaly-stat-label">Highest</span></div>
            <div class="anomaly-stat"><span class="anomaly-stat-value">${categories.length}</span><span class="anomaly-stat-label">Categories</span></div>
        </div>`;

    const filterHtml = categories.length > 1 ? `
        <div class="anomaly-filters" role="tablist">
            <button class="anomaly-filter-chip ${anomalyState.filter === 'all' ? 'active' : ''}" onclick="setAnomalyFilter('all')" role="tab" aria-selected="${anomalyState.filter === 'all'}">All</button>
            ${categories.map(c => `<button class="anomaly-filter-chip ${anomalyState.filter === c ? 'active' : ''}" onclick="setAnomalyFilter('${String(c).replace(/'/g, "\\'")}')" role="tab" aria-selected="${anomalyState.filter === c}">${escapeHtml(c)}</button>`).join('')}
        </div>` : '';

    if (!filtered.length) {
        return `
            ${statsHtml}
            ${filterHtml}
            <p class="anomaly-no-match">No outliers in this category.</p>`;
    }

    const visibleCount = anomalyState.showAll ? filtered.length : Math.min(5, filtered.length);
    const visible = filtered.slice(0, visibleCount);
    const remaining = filtered.length - visibleCount;

    const cardsHtml = visible.map((a, idx) => renderAnomalyCard(a, idx)).join('');
    const moreBtn = remaining > 0
        ? `<button class="anomaly-show-more" onclick="showMoreAnomalies()">Show ${remaining} more</button>`
        : '';

    return `
        <div class="anomaly-header">
            <h3 class="anomaly-title"><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> Spending Outliers</h3>
            <span class="anomaly-count-pill">${items.length} unusual transaction${items.length > 1 ? 's' : ''}</span>
        </div>
        <p class="anomaly-subtitle">Transactions significantly above your normal category spending.</p>
        ${statsHtml}
        ${filterHtml}
        <div class="anomaly-list">${cardsHtml}</div>
        ${moreBtn}`;
}

function renderAnomalyCard(a, idx) {
    const key = anomalyCardKey(a, idx);
    const severity = String(a.severity ?? 'low').toLowerCase();
    const severityLabel = severity.toUpperCase();
    const isExpanded = anomalyState.expanded.has(key);
    const deviation = anomalyDeviationValue(a);
    const expected = a.expected_amount ?? 0;
    const category = escapeHtml(a.category ?? 'Uncategorized');
    const deviationText = deviation
        ? `${deviation.toFixed(1)}× your usual ₹${formatINR(expected)}`
        : '';
    const confidence = a.confidence
        ? String(a.confidence).charAt(0).toUpperCase() + String(a.confidence).slice(1).toLowerCase()
        : '—';
    const reason = a.reason
        ? escapeHtml(a.reason)
        : `₹${formatINR(a.amount)} is ${deviation ? deviation.toFixed(1) + '×' : 'significantly above'} your usual ${category} spending of ₹${formatINR(expected)}.`;

    return `
        <div class="anomaly-card severity-${severity} ${isExpanded ? 'expanded' : ''}">
            <button class="anomaly-card-head" onclick="toggleAnomalyCard('${key}')" aria-expanded="${isExpanded}">
                <span class="anomaly-severity-badge">${severityLabel}</span>
                <span class="anomaly-card-main">
                    <span class="anomaly-card-category">${category}</span>
                    <span class="anomaly-card-txn">Transaction #${escapeHtml(String(a.transaction_id ?? a.id ?? ''))}</span>
                </span>
                <span class="anomaly-card-figures">
                    <span class="anomaly-card-amount">₹${formatINR(a.amount)}</span>
                    ${deviationText ? `<span class="anomaly-card-deviation">${deviationText}</span>` : ''}
                </span>
                <span class="anomaly-card-toggle">
                    View details <i class="fa-solid fa-chevron-down anomaly-card-chevron" aria-hidden="true"></i>
                </span>
            </button>
            <div class="anomaly-card-details">
                <div class="anomaly-card-details-inner">
                    <div class="anomaly-detail-reason">
                        <span class="anomaly-detail-label">Why flagged</span>
                        <p>${reason}</p>
                    </div>
                    <div class="anomaly-detail-grid">
                        <div><span>Expected</span><strong>₹${formatINR(expected)}</strong></div>
                        <div><span>Actual</span><strong>₹${formatINR(a.amount)}</strong></div>
                        <div><span>Confidence</span><strong>${confidence}</strong></div>
                    </div>
                </div>
            </div>
        </div>`;
}

function renderAnomalySection() {
    const root = document.getElementById('anomalyRoot');
    if (!root) return;
    root.innerHTML = buildAnomalyBodyHTML();
}

function toggleAnomalyCard(key) {
    if (anomalyState.expanded.has(key)) anomalyState.expanded.delete(key);
    else anomalyState.expanded.add(key);
    renderAnomalySection();
}

function setAnomalyFilter(cat) {
    anomalyState.filter = cat;
    anomalyState.showAll = false;
    renderAnomalySection();
}

function showMoreAnomalies() {
    anomalyState.showAll = true;
    renderAnomalySection();
}

async function renderInsights(type) {
    const myToken = ++insightsRenderToken;

    // Update active tab
    document.querySelectorAll('.insights-tab').forEach((tab, i) => {
        const types = ['analysis', 'trends', 'anomalies', 'recommendations', 'subscriptions'];
        tab.classList.toggle('active', types[i] === type);
    });

    const container = document.getElementById('dynamicInsights');
    if (!container) return;

    container.innerHTML = `
        <div style="padding:var(--spacing-xl);text-align:center;color:var(--text-tertiary);">
            <i class="fa-solid fa-spinner fa-spin" style="font-size:1.5rem;margin-bottom:.5rem;display:block;" aria-hidden="true"></i>
            Loading…
        </div>`;

    try {
        let html = '';

        if (type === 'analysis') {
            const res = await authFetch('/spending-insights');
            if (!res) return;
            const json = await res.json();
            const payload = json.data ?? json;

            if (payload && !Array.isArray(payload) && payload.status === 'insufficient_data') {
                html = renderInsufficientDataState(payload.message);
            } else {
                const insights = Array.isArray(payload) ? payload : [];
                if (insights.length === 0) {
                    html = `<p style="color:var(--text-tertiary);">No notable spending changes this month.</p>`;
                } else {
                    html = insights.map(i => `
                        <div style="margin-bottom:12px;padding:12px;background:var(--bg-tertiary);border-radius:var(--radius-md);">
                            <strong style="color:${i.type === 'warning' ? 'var(--warning)' : 'var(--success)'};">
                                <i class="fa-solid ${i.type === 'warning' ? 'fa-triangle-exclamation' : 'fa-check-circle'}" aria-hidden="true"></i>
                                ${i.type.toUpperCase()}
                            </strong>
                            <p style="margin-top:4px;font-size:.9rem;">${escapeHtml(i.message)}</p>
                        </div>`).join('');
                }
            }
        }

        else if (type === 'trends') {
            const res = await authFetch('/predict-expense');
            if (!res) return;
            const json = await res.json();
            const data = json.data ?? json;

            html = `
                <h3 style="font-size:1rem;font-weight:700;margin-bottom:12px;">
                    <i class="fa-solid fa-chart-line" style="color:var(--primary);margin-right:8px;" aria-hidden="true"></i>
                    Next Month Forecast
                </h3>
                <p style="font-size:1.75rem;font-weight:700;font-family:var(--font-mono);color:var(--primary);">
                    ₹${Number(data.predicted_expense ?? 0).toLocaleString('en-IN')}
                </p>
                <p style="color:var(--text-tertiary);margin-top:8px;font-size:.875rem;">
                    Predicted expense based on your historical spending pattern.
                </p>`;
        }

        else if (type === 'anomalies') {
            const res = await authFetch('/anomaly-transactions');
            if (!res) return;
            const json = await res.json();
            const data = json.data ?? json;

            anomalyState = { items: data.anomalies || [], filter: 'all', expanded: new Set(), showAll: false };
            html = `<div id="anomalyRoot" class="anomaly-panel">${buildAnomalyBodyHTML()}</div>`;
        }

        else if (type === 'recommendations') {
            const res = await authFetch('/recommendations');
            if (!res) return;
            const json = await res.json();
            const data = json.data ?? json;

            if (!data.recommendations || data.recommendations.length === 0) {
                html = `
                    <div style="text-align:center;padding:var(--spacing-lg);color:var(--success);">
                        <i class="fa-solid fa-star" style="font-size:2rem;display:block;margin-bottom:8px;" aria-hidden="true"></i>
                        <p style="font-weight:600;">Your finances look healthy!</p>
                        <p style="color:var(--text-tertiary);font-size:.875rem;margin-top:4px;">Keep up the great work.</p>
                    </div>`;
            } else {
                html = `
                    <h3 style="font-size:1rem;font-weight:700;margin-bottom:12px;">
                        <i class="fa-solid fa-lightbulb" style="color:var(--warning);margin-right:8px;" aria-hidden="true"></i>
                        Smart Recommendations
                    </h3>
                    ${data.recommendations.map(r => `
                        <div style="margin-bottom:10px;padding:12px 14px;background:var(--warning-light);border-radius:var(--radius-md);display:flex;gap:10px;align-items:flex-start;">
                            <i class="fa-solid fa-circle-info" style="color:var(--warning);margin-top:2px;flex-shrink:0;" aria-hidden="true"></i>
                            <span style="font-size:.9rem;">${escapeHtml(r)}</span>
                        </div>`).join('')}`;
            }
        }

        else if (type === 'subscriptions') {
            const res = await authFetch('/subscriptions');
            if (!res) return;
            const json = await res.json();
            const data = json.data ?? json;

            if (!data.subscriptions || data.subscriptions.length === 0) {
                html = `<p style="color:var(--text-tertiary);">No recurring expenses detected.</p>`;
            } else {
                html = `
                    <h3 style="font-size:1rem;font-weight:700;margin-bottom:12px;">
                        <i class="fa-solid fa-rotate" style="color:var(--primary);margin-right:8px;" aria-hidden="true"></i>
                        Recurring Subscriptions
                    </h3>
                    ${data.subscriptions.map(s => `
                        <div style="margin-bottom:8px;padding:10px 14px;background:var(--bg-tertiary);border-radius:var(--radius-md);display:flex;justify-content:space-between;align-items:center;">
                            <span style="font-weight:500;">${escapeHtml(s.name)}</span>
                            <span style="font-family:var(--font-mono);color:var(--text-secondary);">₹${Number(s.amount).toLocaleString('en-IN')}/mo</span>
                        </div>`).join('')}`;
            }
        }

        // A newer renderInsights() call may have started (and already written
        // its own tab content) while this call's fetch above was in flight.
        // If so, this call is stale — bail out before touching the DOM so it
        // can never overwrite/duplicate a newer render's content or panel.
        if (myToken !== insightsRenderToken) return;

        container.innerHTML = html;

        // Financial Risk panel: rendered into its own dedicated element
        // (#insightsRiskPanel) that is created at most once and thereafter
        // only ever updated in place via innerHTML assignment — never
        // appended with `+=`. Combined with the staleness check below, this
        // guarantees exactly one risk panel exists no matter how many times
        // or how rapidly renderInsights()/loadInsights() run.
        try {
            const riskRes = await authFetch('/risk-analysis');
            if (riskRes && myToken === insightsRenderToken) {
                const riskJson = await riskRes.json();
                const rd = riskJson.data ?? riskJson;
                renderInsightsRiskPanel(container, rd);
            }
        } catch (_) { /* risk panel is non-critical */ }

    } catch (err) {
        if (myToken !== insightsRenderToken) return;
        container.innerHTML = `<p style="color:var(--danger);">Error loading insights. Please try again.</p>`;
        console.error('renderInsights error:', err);
    }
}

/* Renders/updates the single Financial Risk panel.
   - Preserves the original markup, classes (`risk-panel <level>`) and all
     fields previously shown (risk, probability, projected_expense,
     days_left); `balance`, if present on the payload, is preserved on the
     data object untouched — nothing here alters or drops it.
   - Reuses the existing #insightsRiskPanel node when present; otherwise
     creates it exactly once and appends it as the last child of the
     insights container. Content is always replaced via `.innerHTML =`,
     never appended, so repeated calls update rather than duplicate it.
   - status === "insufficient_data" (no current-month income AND no
     current-month expense) shows a plain informational message instead of
     a fabricated probability/projection/runway. As soon as the API
     reports real current-month activity again, the normal branch below
     runs again on the very next render — no separate "restore" step
     needed. */
function renderInsightsRiskPanel(container, rd) {
    // Defensive: if a panel node exists anywhere outside the current
    // container (e.g. left behind by a prior DOM structure), drop it so we
    // never end up with two panels in the page at once.
    const stray = document.getElementById('insightsRiskPanel');
    if (stray && stray.parentElement !== container) {
        stray.remove();
    }

    let panel = document.getElementById('insightsRiskPanel');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'insightsRiskPanel';
        panel.style.marginTop = 'var(--spacing-xl)';
        container.appendChild(panel);
    } else if (panel.parentElement !== container) {
        container.appendChild(panel);
    }

    if (rd.status === 'insufficient_data') {
        // No level class (low/medium/high) — none of those verdicts apply
        // when there's no data to assess, so the panel falls back to the
        // base .risk-panel styling only (neutral border, no color claim).
        panel.className = 'risk-panel';
        panel.innerHTML = `
            <h3><i class="fa-solid fa-shield-halved" aria-hidden="true"></i> Financial Risk: Insufficient Data</h3>
            <p>${escapeHtml(rd.message || 'No current-month financial activity has been recorded, so Budgetly cannot reliably estimate your financial risk yet.')}</p>`;
        return;
    }

    const level = (rd.risk ?? 'LOW').toLowerCase();
    panel.className = `risk-panel ${level}`;
    panel.innerHTML = `
        <h3><i class="fa-solid fa-shield-halved" aria-hidden="true"></i> Financial Risk: ${rd.risk ?? 'Unknown'}</h3>
        <p>Budget breach probability: <strong>${rd.probability ?? 0}%</strong></p>
        <p>Projected monthly expense: <strong>₹${Number(rd.projected_expense ?? 0).toLocaleString('en-IN')}</strong></p>
        ${rd.days_left !== undefined ? `<p>Estimated days of runway: <strong>${rd.days_left}</strong></p>` : ''}`;
}

async function loadSpendingInsights() {
    const res = await authFetch('/spending-insights');
    if (!res) return;

    const container = document.getElementById('insightsContainer');
    if (!container) return;

    try {
        const json = await res.json();
        const insights = json.data ?? json;

        if (!Array.isArray(insights) || insights.length === 0) {
            container.innerHTML = '<p style="font-size:.875rem;">No month-over-month data yet.</p>';
            return;
        }

        container.innerHTML = insights.map(i => `
            <div style="margin-bottom:8px;">
                <strong>${i.type === 'warning' ? '⚠️' : '✅'}</strong> ${escapeHtml(i.message)}
            </div>`).join('');
    } catch (e) {
        console.error('loadSpendingInsights error', e);
    }
}