'use strict';

/* ================================================================
   INSIGHTS — Financial Intelligence Dashboard
   ----------------------------------------------------------------
   This file now renders ONE dashboard sourced from ONE request:
   GET /api/insights/unified.

   The previous version independently called /spending-insights,
   /predict-expense, /anomaly-transactions, /recommendations,
   /subscriptions and /risk-analysis behind a five-tab UI, which is why
   the page could show numbers that disagreed with each other (e.g. an
   old-engine "recurring subscription" that the new recurring engine
   didn't consider a subscription at all). Those endpoints still exist
   server-side for backward compatibility, but this page no longer
   calls them.
================================================================ */

let insightsLoadInFlight = false;
let insightsRenderToken = 0;
let unifiedInsightsData = null;

/* ================================================================
   SECTION NAVIGATION
   ----------------------------------------------------------------
   The unified payload is rendered once into a set of section views;
   switching sections only toggles visibility (see switchFiSection).
   No additional network request is ever made here.
================================================================ */
const FI_SECTIONS = [
    { id: 'overview', label: 'Overview', icon: 'fa-gauge-high' },
    { id: 'forecast', label: 'Forecast', icon: 'fa-chart-line' },
    { id: 'recurring', label: 'Recurring', icon: 'fa-calendar-days' },
    { id: 'subscriptions', label: 'Subscriptions', icon: 'fa-rotate' },
    { id: 'spending', label: 'Spending', icon: 'fa-magnifying-glass-chart' },
    { id: 'goals', label: 'Goals', icon: 'fa-bullseye' },
    { id: 'anomalies', label: 'Anomalies', icon: 'fa-triangle-exclamation', secondary: true },
    { id: 'action-plan', label: 'Action Plan', icon: 'fa-lightbulb', secondary: true },
    { id: 'ai-summary', label: 'AI Summary', icon: 'fa-robot', secondary: true },
];

// Persisted only for the current page session (a plain module-level
// variable), not localStorage — resets on full page reload, survives
// switching away from and back to the Insights page within the SPA.
let fiActiveSection = 'overview';

function renderSectionNav() {
    let lastWasPrimary = true;
    const items = FI_SECTIONS.map(s => {
        const divider = (s.secondary && lastWasPrimary) ? `<span class="fi-nav-divider" aria-hidden="true"></span>` : '';
        lastWasPrimary = !s.secondary;
        const active = fiActiveSection === s.id;
        return `${divider}
            <button type="button"
                class="fi-nav-item ${active ? 'active' : ''}"
                data-section="${s.id}"
                role="tab"
                aria-selected="${active}"
                onclick="switchFiSection('${s.id}')">
                <i class="fa-solid ${s.icon}" aria-hidden="true"></i>
                <span>${s.label}</span>
            </button>`;
    }).join('');

    return `<nav class="fi-section-nav" role="tablist" aria-label="Insights sections">${items}</nav>`;
}

function switchFiSection(id) {
    if (!FI_SECTIONS.some(s => s.id === id)) return;
    fiActiveSection = id;

    document.querySelectorAll('.fi-nav-item').forEach(btn => {
        const isActive = btn.dataset.section === id;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', String(isActive));
    });

    document.querySelectorAll('.fi-section-view').forEach(view => {
        view.classList.toggle('active', view.dataset.section === id);
    });

    const activeBtn = document.querySelector(`.fi-nav-item[data-section="${id}"]`);
    if (activeBtn && typeof activeBtn.scrollIntoView === 'function') {
        activeBtn.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
    }
}

function loadInsights() {
    if (insightsLoadInFlight) return;
    insightsLoadInFlight = true;
    Promise.resolve(renderUnifiedDashboard())
        .finally(() => { insightsLoadInFlight = false; });
}

function renderInsights() {
    return renderUnifiedDashboard();
}

function formatINR(n) {
    return Number(n || 0).toLocaleString('en-IN');
}

function fmtPct(n, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    return `${Number(n).toFixed(digits ?? 1)}%`;
}

function trendArrow(trend) {
    if (trend === 'up' || trend === 'increasing') return '↑';
    if (trend === 'down' || trend === 'decreasing') return '↓';
    return '→';
}

/* ================================================================
   MAIN ENTRY POINT — one request, one render pass
================================================================ */
async function renderUnifiedDashboard() {
    const myToken = ++insightsRenderToken;
    const container = document.getElementById('dynamicInsights');
    if (!container) return;

    container.innerHTML = `
        <div style="padding:var(--spacing-xl);text-align:center;color:var(--text-tertiary);">
            <i class="fa-solid fa-spinner fa-spin" style="font-size:1.5rem;margin-bottom:.5rem;display:block;" aria-hidden="true"></i>
            Loading your Financial Intelligence dashboard…
        </div>`;

    // A stray risk panel from the old architecture must never persist
    // alongside the new dashboard.
    const strayRiskPanel = document.getElementById('insightsRiskPanel');
    if (strayRiskPanel) strayRiskPanel.remove();

    try {
        const res = await authFetch('/api/insights/unified');
        if (!res) return;
        const json = await res.json();
        const data = json.data ?? json;

        if (myToken !== insightsRenderToken) return; // superseded by a newer render

        if (data.status !== 'success') {
            container.innerHTML = `<p style="color:var(--danger);">Unable to load Financial Intelligence dashboard right now.</p>`;
            return;
        }

        unifiedInsightsData = data;
        anomalyState = { items: data.anomalies || [], filter: 'all', expanded: new Set(), showAll: false };

        const sectionView = (id, innerHtml) =>
            `<div class="fi-section-view ${fiActiveSection === id ? 'active' : ''}" data-section="${id}">${innerHtml}</div>`;

        container.innerHTML = `
            <div class="fi-dashboard">
                ${renderSectionNav()}
                ${sectionView('overview', renderOverviewSection(data))}
                ${sectionView('forecast', renderForecastSection(data.forecast))}
                ${sectionView('recurring', renderUpcomingRecurringSection(data.recurring, (data.subscriptions?.possible || []).length))}
                ${sectionView('subscriptions', renderSubscriptionsSection(data.subscriptions))}
                ${sectionView('spending', renderSpendingIntelligenceSection(data.spending))}
                ${sectionView('goals', renderGoalIntelligenceSection(data.goals))}
                ${sectionView('anomalies', `<div id="anomalyRoot" class="anomaly-panel fi-section">${buildAnomalyBodyHTML()}</div>`)}
                ${sectionView('action-plan', renderActionPlanSection(data.recommendations))}
                ${sectionView('ai-summary', renderAiExplanationSection(data))}
            </div>`;

        renderAnomalySection();

    } catch (err) {
        if (myToken !== insightsRenderToken) return;
        container.innerHTML = `<p style="color:var(--danger);">Error loading insights. Please try again.</p>`;
        console.error('renderUnifiedDashboard error:', err);
    }
}

/* ================================================================
   0. OVERVIEW — condensed cross-section summary
   ----------------------------------------------------------------
   Reuses the same unified payload; nothing here is recalculated.
   Answers "How am I doing financially?" without repeating every
   detailed card — those live in their own section views.
================================================================ */
function severityIcon(sev) {
    return sev === 'high' ? 'fa-triangle-exclamation' : sev === 'medium' ? 'fa-circle-exclamation' : 'fa-circle-info';
}

function renderOverviewSection(data) {
    const forecast = data.forecast || {};
    const recurring = data.recurring || {};
    const spending = data.spending || {};
    const goals = data.goals || {};
    const recommendations = data.recommendations || [];
    const topRec = recommendations[0];

    const forecastKnown = forecast.status !== 'insufficient_data' && forecast.forecast !== null && forecast.forecast !== undefined;
    const forecastValue = forecastKnown ? `₹${formatINR(forecast.forecast)}` : '—';
    const forecastSub = forecastKnown && forecast.trend && forecast.trend !== 'insufficient_data'
        ? `${trendArrow(forecast.trend)} ${escapeHtml(String(forecast.trend))}`
        : '';

    const overdueCount = (recurring.overdue || []).length;
    const upcomingCount = (recurring.upcoming || []).length;
    const recurringSub = overdueCount ? `${overdueCount} overdue` : (upcomingCount ? `${upcomingCount} upcoming` : 'Nothing due soon');

    const spendingTrendPct = spending.trend_pct;
    const spendingTrendDir = spendingTrendPct > 0 ? 'up' : spendingTrendPct < 0 ? 'down' : 'flat';
    const spendingValue = fmtPct(spendingTrendPct);
    const spendingSub = spending.top_category?.name ? `Top: ${escapeHtml(spending.top_category.name)}` : 'vs last month';

    const goalsKnown = (goals.details || []).length > 0;
    const goalsValue = goalsKnown && goals.pressure !== undefined && goals.pressure !== null ? `${Math.round(goals.pressure)}/100` : '—';
    const goalsSub = goalsKnown ? (goals.goals_at_risk ? `${goals.goals_at_risk} at risk` : 'On track') : 'No goals yet';

    const quickCard = (icon, label, value, sub, targetSection) => `
        <button type="button" class="fi-quick-card" onclick="switchFiSection('${targetSection}')">
            <span class="fi-quick-icon"><i class="fa-solid ${icon}" aria-hidden="true"></i></span>
            <span class="fi-quick-body">
                <span class="fi-quick-label">${label}</span>
                <span class="fi-quick-value">${value}</span>
                ${sub ? `<span class="fi-quick-sub">${sub}</span>` : ''}
            </span>
            <i class="fa-solid fa-chevron-right fi-quick-arrow" aria-hidden="true"></i>
        </button>`;

    const recHtml = topRec ? `
        <section class="fi-section fi-overview-rec">
            <h3 class="fi-section-title"><i class="fa-solid fa-lightbulb" aria-hidden="true"></i> Top Recommendation</h3>
            <div class="fi-action-card severity-${escapeHtml(topRec.severity || 'low')}">
                <span class="fi-action-icon"><i class="fa-solid ${severityIcon(topRec.severity)}" aria-hidden="true"></i></span>
                <div class="fi-action-body">
                    <p class="fi-action-title">${escapeHtml(topRec.title || '')}</p>
                    <p class="fi-action-message">${escapeHtml(topRec.message || '')}</p>
                    ${topRec.action ? `<span class="fi-action-next"><strong>Do this:</strong> ${escapeHtml(topRec.action)}</span>` : ''}
                </div>
            </div>
            <button type="button" class="fi-view-all-btn" onclick="switchFiSection('action-plan')">View full action plan <i class="fa-solid fa-arrow-right" aria-hidden="true"></i></button>
        </section>` : `
        <section class="fi-section fi-overview-rec">
            <h3 class="fi-section-title"><i class="fa-solid fa-lightbulb" aria-hidden="true"></i> Top Recommendation</h3>
            <div class="fi-empty-positive">
                <i class="fa-solid fa-star" aria-hidden="true"></i>
                <strong>Your finances look healthy!</strong>
                <span>No urgent recommendations right now.</span>
            </div>
        </section>`;

    return `
        ${renderFinancialHealthSection(data.financial_health)}
        <div class="fi-quick-grid">
            ${quickCard('fa-chart-line', 'Next Month Forecast', forecastValue, forecastSub, 'forecast')}
            ${quickCard('fa-calendar-days', 'Recurring Burden', `₹${formatINR(recurring.monthly_burden)}/mo`, recurringSub, 'recurring')}
            ${quickCard('fa-magnifying-glass-chart', 'Spending Trend', `${trendArrow(spendingTrendDir)} ${spendingValue}`, spendingSub, 'spending')}
            ${quickCard('fa-bullseye', 'Goal Pressure', goalsValue, goalsSub, 'goals')}
        </div>
        ${recHtml}`;
}

/* ================================================================
   1. FINANCIAL HEALTH
================================================================ */
function renderFinancialHealthSection(health) {
    if (!health || health.status === 'insufficient_data') {
        return `
        <section class="fi-section fi-hero">
            <h3 class="fi-section-title"><i class="fa-solid fa-heart-pulse" aria-hidden="true"></i> Financial Health</h3>
            <p class="fi-empty">Not enough financial activity yet to calculate a health score. Add some income and expense transactions to unlock this.</p>
        </section>`;
    }

    const scoreNum = typeof health.score === 'number' ? health.score : null;
    const score = scoreNum ?? '—';
    const level = (health.risk_level || 'unknown').replace(/_/g, ' ');
    const levelSlug = escapeHtml(level.replace(/\s+/g, '-'));
    const levelLabel = level.charAt(0).toUpperCase() + level.slice(1);
    const summary = health.summary || {};
    const cashFlow = health.cash_flow || {};
    const budget = health.budget || {};

    const stat = (label, value) => `
        <div class="fi-stat"><span class="fi-stat-label">${label}</span><span class="fi-stat-value">${value}</span></div>`;

    const factorsHtml = (title, icon, factors, cls) => (factors && factors.length) ? `
        <details class="fi-factors ${cls}">
            <summary><i class="fa-solid ${icon}" aria-hidden="true"></i> ${title} (${factors.length})</summary>
            <ul>${factors.map(f => `<li>${escapeHtml(typeof f === 'string' ? f : (f.message || JSON.stringify(f)))}</li>`).join('')}</ul>
        </details>` : '';

    return `
    <section class="fi-section fi-hero fi-health level-${levelSlug}">
        <div class="fi-hero-top">
            <div class="fi-score-ring" style="--score:${scoreNum ?? 0};">
                <span class="fi-score-num">${score}</span>
                <span class="fi-score-max">/100</span>
            </div>
            <div class="fi-hero-meta">
                <h3 class="fi-section-title"><i class="fa-solid fa-heart-pulse" aria-hidden="true"></i> Financial Health</h3>
                <p class="fi-hero-sub">Your overall financial wellbeing this cycle</p>
                <span class="fi-level-badge">${escapeHtml(levelLabel)} risk</span>
                ${cashFlow.projected_surplus !== null && cashFlow.projected_surplus !== undefined ? `
                    <span class="fi-cashflow-pill">Projected surplus <strong>₹${formatINR(cashFlow.projected_surplus)}</strong></span>` : ''}
            </div>
        </div>
        <div class="fi-stat-grid">
            ${stat('Savings rate', fmtPct(summary.current_savings_rate))}
            ${stat('Projected savings rate', fmtPct(summary.projected_savings_rate))}
            ${stat('Budget usage', fmtPct(budget.budget_usage_pct))}
            ${stat('Projected budget usage', fmtPct(budget.projected_budget_usage_pct))}
            ${stat('Recurring burden', fmtPct(summary.recurring_burden_pct))}
            ${stat('Goal pressure', summary.goal_pressure !== null && summary.goal_pressure !== undefined ? `${Math.round(summary.goal_pressure)}/100` : '—')}
        </div>
        <div class="fi-factors-row">
            ${factorsHtml('Risk factors', 'fa-triangle-exclamation', health.main_risk_factors, 'risk')}
            ${factorsHtml('Positive factors', 'fa-circle-check', health.positive_factors, 'positive')}
        </div>
    </section>`;
}

/* ================================================================
   2. EXPENSE FORECAST
================================================================ */
function renderForecastSection(forecast) {
    if (!forecast || forecast.status === 'insufficient_data' || forecast.forecast === null || forecast.forecast === undefined) {
        return `
        <section class="fi-section">
            <h3 class="fi-section-title"><i class="fa-solid fa-chart-line" aria-hidden="true"></i> Expense Forecast</h3>
            <p class="fi-empty">Not enough transaction history yet for a reliable forecast.</p>
        </section>`;
    }

    const confidenceLabel = forecast.confidence
        ? String(forecast.confidence).charAt(0).toUpperCase() + String(forecast.confidence).slice(1)
        : '—';

    const isInsufficientTrend = forecast.trend === 'insufficient_data';
    const trendClass = isInsufficientTrend ? 'unknown' : (forecast.trend === 'up' || forecast.trend === 'increasing') ? 'up'
        : (forecast.trend === 'down' || forecast.trend === 'decreasing') ? 'down' : 'flat';
    const trendLabel = isInsufficientTrend ? 'Insufficient history' : (forecast.trend || 'stable');

    return `
    <section class="fi-section">
        <h3 class="fi-section-title"><i class="fa-solid fa-chart-line" aria-hidden="true"></i> Expense Forecast</h3>
        <div class="fi-forecast-total">
            <span class="fi-stat-label">Next Month Forecast</span>
            <strong>₹${formatINR(forecast.forecast)}</strong>
            <span class="fi-trend-chip trend-${trendClass}">${isInsufficientTrend ? '' : trendArrow(forecast.trend) + ' '}${escapeHtml(trendLabel)}</span>
        </div>
        <div class="fi-stat-grid">
            <div class="fi-stat"><span class="fi-stat-label">Discretionary</span><span class="fi-stat-value">₹${formatINR(forecast.forecasted_discretionary)}</span></div>
            <div class="fi-stat"><span class="fi-stat-label">Recurring commitments</span><span class="fi-stat-value">₹${formatINR(forecast.expected_recurring)}</span></div>
            <div class="fi-stat"><span class="fi-stat-label">Expected range</span><span class="fi-stat-value">₹${formatINR(forecast.lower_bound)} – ₹${formatINR(forecast.upper_bound)}</span></div>
            <div class="fi-stat"><span class="fi-stat-label">Confidence</span><span class="fi-stat-value">${confidenceLabel}</span></div>
        </div>
        ${forecast.message ? `<p class="fi-note">${escapeHtml(forecast.message)}</p>` : ''}
    </section>`;
}

/* ================================================================
   3. UPCOMING RECURRING PAYMENTS
================================================================ */

// Classification must come from the backend's actual classification —
// never inferred/guessed here. This only maps the known enum values to
// display labels.
const RECURRING_CLASSIFICATION_LABELS = {
    subscription: 'Subscription',
    recurring_bill: 'Recurring bill',
    possible_subscription: 'Possible subscription',
    unknown_recurring: 'Unknown recurring',
    recurring_income: 'Recurring income',
};

function recurringClassificationLabel(classification) {
    return RECURRING_CLASSIFICATION_LABELS[classification] || 'Unknown recurring';
}

function paymentStatusLabel(status) {
    if (status === 'overdue') return 'Overdue';
    if (status === 'due_soon') return 'Due soon';
    if (status === 'upcoming') return 'Upcoming';
    if (status === 'possibly_missed') return 'Possibly missed';
    return status ? String(status).replace(/_/g, ' ') : '—';
}

function renderUpcomingRecurringSection(recurring, possibleCount) {
    const upcoming = recurring?.upcoming || [];
    const overdue = recurring?.overdue || [];
    const monthlyBurden = Number(recurring?.monthly_burden) || 0;

    const row = (item, tag) => {
        const classification = item.classification;
        const confidencePct = item.classification_confidence !== null && item.classification_confidence !== undefined
            ? Math.round(item.classification_confidence * 100)
            : null;

        return `
        <div class="fi-recurring-row ${tag ? 'is-' + tag : ''}">
            <div class="fi-recurring-main">
                <span class="fi-recurring-name">${escapeHtml(item.name)}</span>
                <span class="fi-recurring-meta-line">
                    <span class="fi-badge fi-badge-${escapeHtml(classification || 'unknown_recurring')}">${escapeHtml(recurringClassificationLabel(classification))}</span>
                    <span class="fi-recurring-lifecycle">${escapeHtml(item.lifecycle_status ? item.lifecycle_status.replace(/_/g, ' ') : '—')}</span>
                    ${confidencePct !== null ? `<span class="fi-recurring-lifecycle">Confidence ${confidencePct}%</span>` : ''}
                </span>
            </div>
            <div class="fi-recurring-side">
                <span class="fi-recurring-amount">₹${formatINR(item.expected_amount)}</span>
                <span class="fi-status-chip status-${escapeHtml(item.payment_status || '')}">${escapeHtml(paymentStatusLabel(item.payment_status))}</span>
            </div>
        </div>`;
    };

    // This section now only ever contains confirmed subscription/
    // recurring_bill commitments (unknown/possible/inactive items are
    // filtered out server-side), so a ₹0 confirmed burden can still
    // coexist with uncertain candidates that simply live elsewhere (the
    // Subscriptions section's "Possible" list) rather than here. Make
    // that explicit instead of letting ₹0 read as "no recurring
    // candidates exist at all".
    const burdenNote = (monthlyBurden === 0 && possibleCount > 0)
        ? `<p class="fi-note">₹0 confirmed monthly burden. Unconfirmed recurring candidates exist — see Possible Subscriptions below.</p>`
        : '';

    const body = (upcoming.length === 0 && overdue.length === 0)
        ? `<p class="fi-empty">No recurring payments due in the next 30 days.</p>`
        : `
            ${overdue.length ? `<h4 class="fi-subheading">Overdue</h4>${overdue.map(i => row(i, 'overdue')).join('')}` : ''}
            ${upcoming.length ? `<h4 class="fi-subheading">Upcoming (next 30 days)</h4>${upcoming.map(i => row(i, 'upcoming')).join('')}` : ''}`;

    return `
    <section class="fi-section">
        <h3 class="fi-section-title"><i class="fa-solid fa-calendar-days" aria-hidden="true"></i> Upcoming Recurring Payments</h3>
        <p class="fi-cashflow-line">Monthly burden: <strong>₹${formatINR(recurring?.monthly_burden)}</strong> · Annual burden: <strong>₹${formatINR(recurring?.annual_burden)}</strong></p>
        ${burdenNote}
        ${body}
    </section>`;
}

/* ================================================================
   4. SUBSCRIPTIONS — confirmed vs possible, never conflated
================================================================ */
function renderSubscriptionsSection(subs) {
    const active = subs?.active || [];
    const possible = subs?.possible || [];

    const confirmedRow = (s) => `
        <div class="fi-sub-card confirmed">
            <div class="fi-sub-head">
                <span class="fi-sub-name">${escapeHtml(s.name)}</span>
                <span class="fi-sub-amount">₹${formatINR(s.monthly_equivalent)}/mo</span>
            </div>
            <div class="fi-sub-meta">
                <span>${escapeHtml(s.frequency)}</span>
                <span>₹${formatINR(s.annualized_cost)}/yr</span>
                <span>Next: ${escapeHtml(s.next_expected_date)}</span>
                <span>${escapeHtml(s.lifecycle_status)} · ${escapeHtml(s.payment_status)}</span>
                <span>Confidence: ${Math.round((s.classification_confidence || 0) * 100)}%</span>
            </div>
            ${s.price_change ? `<div class="fi-price-change ${s.price_change.direction}">Price ${s.price_change.direction === 'increase' ? 'increased' : 'decreased'}: ₹${formatINR(s.price_change.previous)} → ₹${formatINR(s.price_change.current)} (${s.price_change.change_percent > 0 ? '+' : ''}${s.price_change.change_percent}%)</div>` : ''}
        </div>`;

    const possibleRow = (s) => `
        <div class="fi-sub-card possible">
            <div class="fi-sub-head">
                <span class="fi-sub-badge">Possible</span>
                <span class="fi-sub-name">${escapeHtml(s.name)}</span>
            </div>
            <div class="fi-sub-meta">
                <span>Confidence: ${Math.round((s.classification_confidence || 0) * 100)}%</span>
                <span>₹${formatINR(s.expected_amount)} · ${escapeHtml(s.frequency)}</span>
            </div>
        </div>`;

    return `
    <section class="fi-section fi-subs-section">
        <h3 class="fi-section-title"><i class="fa-solid fa-rotate" aria-hidden="true"></i> Subscriptions</h3>
        <p class="fi-cashflow-line">Confirmed: <strong>₹${formatINR(subs?.confirmed_monthly_cost)}/mo</strong> (₹${formatINR(subs?.confirmed_annual_cost)}/yr)</p>
        <h4 class="fi-subheading">Confirmed Subscriptions${active.length ? ` (${active.length})` : ''}</h4>
        ${active.length ? `<div class="fi-sub-grid">${active.map(confirmedRow).join('')}</div>` : `<p class="fi-empty">No confirmed active subscriptions detected.</p>`}
        <h4 class="fi-subheading">Possible Subscriptions${possible.length ? ` (${possible.length})` : ''}</h4>
        ${possible.length ? `<div class="fi-sub-grid">${possible.map(possibleRow).join('')}</div>` : `<p class="fi-empty">Nothing ambiguous right now.</p>`}
    </section>`;
}

/* ================================================================
   5. SPENDING INTELLIGENCE
================================================================ */
function renderSpendingIntelligenceSection(spending) {
    const categories = spending?.categories || [];
    const up = categories.filter(c => c.trend === 'up');
    const down = categories.filter(c => c.trend === 'down');
    // "no_data" means the category simply has no current-month
    // transactions yet — that is NOT a spending decrease and must never
    // be shown as a falling/₹0 trend.
    const noData = categories.filter(c => c.trend === 'no_data');
    const topCat = spending?.top_category;

    const catRow = (c) => `
        <div class="fi-cat-row fi-cat-${escapeHtml(c.trend)}">
            <span class="fi-cat-name">${trendArrow(c.trend)} ${escapeHtml(c.category)}</span>
            <span class="fi-cat-value">₹${formatINR(c.forecast)} projected</span>
        </div>`;

    // Bug fix (duplication): previously rendered both the bare category
    // name AND the note (which already includes the category name),
    // producing "TransportTransport — no spending recorded this
    // month". The note is self-contained — render it alone.
    const noDataRow = (c) => `
        <div class="fi-cat-row fi-cat-nodata"><span>${escapeHtml(c.note || `${c.category} — no spending recorded this month`)}</span></div>`;

    return `
    <section class="fi-section">
        <h3 class="fi-section-title"><i class="fa-solid fa-magnifying-glass-chart" aria-hidden="true"></i> Spending Intelligence</h3>
        <p class="fi-cashflow-line">Overall spending trend: <strong>${trendArrow(spending?.trend_pct > 0 ? 'up' : spending?.trend_pct < 0 ? 'down' : 'flat')} ${fmtPct(spending?.trend_pct)}</strong> vs last month</p>
        ${topCat && topCat.name ? `<p class="fi-note">Largest contributor: ${escapeHtml(topCat.name)} (${fmtPct(topCat.percent, 0)} of spend)</p>` : ''}
        ${up.length ? `<h4 class="fi-subheading">Rising categories</h4><div class="fi-cat-grid">${up.map(catRow).join('')}</div>` : ''}
        ${down.length ? `<h4 class="fi-subheading">Falling categories</h4><div class="fi-cat-grid">${down.map(catRow).join('')}</div>` : ''}
        ${noData.length ? `<h4 class="fi-subheading">No activity this month</h4><div class="fi-cat-grid">${noData.map(noDataRow).join('')}</div>` : ''}
        ${(!up.length && !down.length && !noData.length) ? `<p class="fi-empty">No significant category shifts detected yet.</p>` : ''}
    </section>`;
}

/* ================================================================
   6. GOAL INTELLIGENCE
================================================================ */
function renderGoalIntelligenceSection(goals) {
    const details = goals?.details || [];
    if (!details.length) {
        return `
        <section class="fi-section">
            <h3 class="fi-section-title"><i class="fa-solid fa-bullseye" aria-hidden="true"></i> Goal Intelligence</h3>
            <div class="fi-empty">
                <p>No goals set up yet.</p>
                <p class="fi-note">Set up a goal on the Goals page to see progress, funding pace, and risk here.</p>
            </div>
        </section>`;
    }

    const goalCard = (g) => {
        const pct = Math.max(0, Math.min(100, Number(g.progress_percent) || 0));
        return `
        <div class="fi-goal-card risk-${escapeHtml(g.goal_risk || 'low')}">
            <div class="fi-goal-head">
                <span class="fi-goal-name">${escapeHtml(g.name)}</span>
                <span class="fi-goal-progress">${fmtPct(g.progress_percent, 0)}</span>
            </div>
            <div class="fi-goal-progress-bar"><div class="fi-goal-progress-fill" style="width:${pct}%;"></div></div>
            <div class="fi-sub-meta">
                <span>Required: ₹${formatINR(g.monthly_required)}/mo</span>
                ${g.target_date ? `<span>Target: ${escapeHtml(g.target_date)}</span>` : (g.months_left ? `<span>~${g.months_left} months left</span>` : '')}
                ${g.remaining ? `<span>Shortfall: ₹${formatINR(g.remaining)}</span>` : ''}
                <span>Risk: ${escapeHtml(g.goal_risk || 'low')}</span>
            </div>
        </div>`;
    };

    return `
    <section class="fi-section">
        <h3 class="fi-section-title"><i class="fa-solid fa-bullseye" aria-hidden="true"></i> Goal Intelligence</h3>
        <p class="fi-cashflow-line">Goal pressure: <strong>${Math.round(goals?.pressure || 0)}/100</strong> · Goals at risk: <strong>${goals?.goals_at_risk ?? 0}</strong></p>
        <div class="fi-goal-grid">${details.map(goalCard).join('')}</div>
    </section>`;
}

/* ================================================================
   8. ACTION PLAN (recommendations)
================================================================ */
function renderActionPlanSection(recommendations) {
    if (!recommendations || recommendations.length === 0) {
        return `
        <section class="fi-section fi-action-section">
            <h3 class="fi-section-title"><i class="fa-solid fa-lightbulb" aria-hidden="true"></i> Action Plan</h3>
            <div class="fi-empty-positive">
                <i class="fa-solid fa-star" aria-hidden="true"></i>
                <strong>Your finances look healthy!</strong>
                <span>Keep up the great work.</span>
            </div>
        </section>`;
    }

    const item = (r, idx) => `
        <div class="fi-action-card severity-${escapeHtml(r.severity || 'low')}">
            <span class="fi-action-icon"><i class="fa-solid ${severityIcon(r.severity)}" aria-hidden="true"></i></span>
            <div class="fi-action-body">
                <p class="fi-action-title">${idx + 1}. ${escapeHtml(r.title || '')}</p>
                <p class="fi-action-message">${escapeHtml(r.message || '')}</p>
                ${r.action ? `<span class="fi-action-next"><strong>Do this:</strong> ${escapeHtml(r.action)}</span>` : ''}
                ${r.estimated_impact ? `<p class="fi-action-impact">Estimated impact: ${escapeHtml(String(r.estimated_impact))}</p>` : ''}
            </div>
        </div>`;

    return `
    <section class="fi-section fi-action-section">
        <h3 class="fi-section-title"><i class="fa-solid fa-lightbulb" aria-hidden="true"></i> Action Plan</h3>
        <div class="fi-action-list">${recommendations.map(item).join('')}</div>
    </section>`;
}

/* ================================================================
   9. AI EXPLANATION — a short synthesis, built only from the numbers
   already present in the unified payload (no independent calculation).
================================================================ */
function renderAiExplanationSection(data) {
    const health = data.financial_health || {};
    const forecast = data.forecast || {};
    const subs = data.subscriptions || {};

    if (health.status === 'insufficient_data') {
        return `
        <section class="fi-section fi-explanation">
            <div class="fi-ai-card">
                <span class="fi-ai-icon"><i class="fa-solid fa-robot" aria-hidden="true"></i></span>
                <div class="fi-ai-body">
                    <h3 class="fi-section-title">AI Financial Summary</h3>
                    <p>There isn't enough transaction history yet to summarize your finances meaningfully. This will fill in as you add income and expenses.</p>
                </div>
            </div>
        </section>`;
    }

    const level = (health.risk_level || 'unknown').replace(/_/g, ' ');
    const parts = [];
    parts.push(`Your financial health score is ${health.score ?? '—'}/100 (${level}).`);
    if (forecast.forecast !== undefined && forecast.forecast !== null) {
        parts.push(`Next month is projected to cost around ₹${formatINR(forecast.forecast)}, including ₹${formatINR(forecast.expected_recurring)} in recurring commitments.`);
    }
    if (subs.confirmed_monthly_cost) {
        parts.push(`Confirmed subscriptions currently cost ₹${formatINR(subs.confirmed_monthly_cost)}/month.`);
    }
    if (data.goals && data.goals.goals_at_risk) {
        parts.push(`${data.goals.goals_at_risk} goal(s) may be at risk given your current surplus.`);
    }

    return `
    <section class="fi-section fi-explanation">
        <div class="fi-ai-card">
            <span class="fi-ai-icon"><i class="fa-solid fa-robot" aria-hidden="true"></i></span>
            <div class="fi-ai-body">
                <h3 class="fi-section-title">AI Financial Summary</h3>
                <p>${parts.map(escapeHtml).join(' ')}</p>
            </div>
        </div>
    </section>`;
}

/* ================================================================
   ANOMALIES — Compact spending-outlier UI
   (presentation only — data now comes from the unified payload's
   `anomalies` array, sourced from the same anomaly engine as before)
================================================================ */
let anomalyState = { items: [], filter: 'all', expanded: new Set(), showAll: false };

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
            <h3 class="anomaly-title"><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> Spending Anomalies</h3>
            <span class="anomaly-count-pill">${items.length} unusual transaction${items.length > 1 ? 's' : ''}</span>
        </div>
        <p class="anomaly-subtitle">Transactions significantly above your normal category spending. Review to make sure each was expected.</p>
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
        : `₹${formatINR(a.amount)} is ${deviation ? deviation.toFixed(1) + '×' : 'significantly above'} your usual ${category} spending of ₹${formatINR(expected)}. Review to make sure it was expected.`;

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