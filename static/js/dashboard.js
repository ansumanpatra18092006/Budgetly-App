/* ================================================================
   DASHBOARD
================================================================ */
async function loadDashboard() {
    updateDate();
    // fetchBudget() depends on currentSummary.expense (set by fetchSummary())
    // via checkBudgetWarning(), so it must not race against it.
    await fetchSummary();
    await fetchBudget();
    await Promise.all([
        loadCharts(),
        loadHealth(),
        loadTopCategories(),
        loadBalanceTrend(),
    ]);
    await loadAllAIFeatures();   // ← ADD THIS LINE
}

async function fetchSummary() {
    const res = await authFetch('/dashboard-summary');
    if (!res) return;

    try {
        const data = await res.json();
        const d = data.data ?? data;

        currentSummary = { income: d.income ?? 0, expense: d.expense ?? 0, balance: d.balance ?? 0 };
        animateValue('income', currentSummary.income);
        animateValue('expense', currentSummary.expense);
        animateValue('balance', currentSummary.balance);
    } catch (e) {
        console.error('fetchSummary parse error', e);
    }
}

async function fetchBudget() {
    const res = await authFetch('/get-budget');
    if (!res) return;

    try {
        const data = await res.json();
        const d = data.data ?? data;
        const budget = d.budget ?? 0;

        const input = document.getElementById('budgetInput');
        if (input) input.value = budget;

        checkBudgetWarning(budget, currentSummary.expense);
    } catch (e) {
        console.error('fetchBudget parse error', e);
    }
}

function setupBudgetListener() {
    const input = document.getElementById('budgetInput');
    if (!input) return;

    input.addEventListener('change', async () => {
        if (_isBudgetSaving) return;
        const amount = parseFloat(input.value);
        if (isNaN(amount) || amount < 0) return;

        _isBudgetSaving = true;
        const res = await authFetch('/set-budget', {
            method: 'POST',
            body: JSON.stringify({ amount }),
        });
        _isBudgetSaving = false;

        if (res && res.ok) {
            showNotification('Budget limit updated', 'success');
            checkBudgetWarning(amount, currentSummary.expense);
        }
    });
}

function checkBudgetWarning(budget, expense) {
    const warning = document.getElementById('budgetWarning');
    if (!warning) return;
    warning.classList.toggle('hidden', !(budget > 0 && expense > budget));
}

/*
 * OLD FLOW:  dashboard.js → GET /health-metrics → legacy locally-labeled score
 * NEW FLOW:  dashboard.js → GET /api/insights/unified → financial_health
 *            → authoritative score/savings/budget (same object Web Insights
 *            and Flutter already read). No score is computed here; the
 *            backend's calculate_financial_health() in ml/risk_model.py is
 *            the single source of truth.
 *
 * Field names below are taken from the actual financial_health payload as
 * already consumed by static/js/insights.js (renderFinancialHealthSection):
 *   financial_health.score
 *   financial_health.summary.current_savings_rate
 *   financial_health.budget.budget_usage_pct
 * financial_health has no income-stability figure — ml/risk_model.py's
 * calculate_financial_health() does not compute one — so that factor is
 * intentionally left on its previous /health-metrics-backed source
 * (loadIncomeStability) instead of being invented here.
 */
async function loadHealth() {
    const res = await authFetch('/api/insights/unified');
    if (!res) return;

    try {
        const data = await res.json();
        const health = data.financial_health || {};

        // health.score is null when status === 'insufficient_data'.
        const scoreKnown = typeof health.score === 'number';
        const score = scoreKnown ? health.score : 0;
        const sr = health.summary?.current_savings_rate ?? null;

        // Budget Adherence isn't a field risk_model.py returns directly;
        // it's mathematically derived from the authoritative
        // budget_usage_pct (how much of the budget has been used), not a
        // second independent calculation. No budget set → usage is null →
        // fall back to 0, same as the other factors above.
        const usage = health.budget?.budget_usage_pct;
        const ba = (usage === null || usage === undefined) ? 0 : Math.max(0, Math.min(100, 100 - usage));

        setEl('healthScore', scoreKnown ? score : '—');
        setEl('healthLabel', scoreKnown ? getHealthLabel(score) : 'Insufficient data');
        const circle = document.querySelector('.score-circle');
        if (circle) circle.style.setProperty('--score', Math.min(score, 100));
        setEl('savingsRate', sr === null ? '—' : sr + '%');
        setEl('budgetAdherence', Math.round(ba) + '%');

        setWidth('savingsBar', sr === null ? 0 : Math.min(sr, 100));
        setWidth('budgetBar', Math.min(ba, 100));
    } catch (e) {
        console.error('loadHealth parse error', e);
    }

    loadIncomeStability();
}

// Income Stability has no equivalent in the unified financial_health
// payload, so — per "leave the existing factor behavior untouched" — it
// keeps reading /health-metrics for this one value only. Nothing here
// feeds the health score or any other factor above.
async function loadIncomeStability() {
    const res = await authFetch('/health-metrics');
    if (!res) return;

    try {
        const data = await res.json();
        const d = data.data ?? data;
        const is_ = d.income_stability ?? 0;

        setEl('incomeStability', is_ + '%');
        setWidth('incomeBar', Math.min(is_, 100));
    } catch (e) {
        console.error('loadIncomeStability parse error', e);
    }
}

async function loadTopCategories() {
    const res = await authFetch('/top-categories');
    if (!res) return;

    const container = document.getElementById('topCategoriesContainer');
    if (!container) return;

    try {
        const data = await res.json();
        const list = data.data ?? data;

        if (!Array.isArray(list) || list.length === 0) {
            container.innerHTML = '<p style="color:var(--text-tertiary);font-size:.875rem;">No expense data yet.</p>';
            return;
        }

        container.innerHTML = list.map(cat => `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border-subtle);">
                <strong style="font-size:.875rem;">${escapeHtml(cat.category)}</strong>
                <span style="font-family:var(--font-mono);font-size:.875rem;color:var(--text-secondary);">
                    ₹${Number(cat.amount).toLocaleString('en-IN')} <span style="color:var(--text-tertiary);">(${cat.percent}%)</span>
                </span>
            </div>
        `).join('');
    } catch (e) {
        console.error('loadTopCategories parse error', e);
    }
}

/**
 * Render one stat-card trend footer.
 * @param {string} selector    CSS selector for the .stat-trend element
 * @param {object} trend       { status: 'ok'|'insufficient_data', change: number|null }
 * @param {string} emptyText   Text shown when there's no meaningful data
 * @param {boolean} invert     If true, an increase is shown as negative (used for expenses)
 */
function renderTrend(selector, trend, emptyText, invert = false) {
    const el = document.querySelector(selector);
    if (!el) return;

    if (!trend || trend.status !== 'ok' || trend.change === null || trend.change === undefined) {
        el.className = 'stat-trend neutral';
        el.innerHTML = `<i class="fa-solid fa-minus" aria-hidden="true"></i> ${emptyText}`;
        return;
    }

    const change = trend.change;
    const isIncrease = change >= 0;
    const isGood = invert ? !isIncrease : isIncrease;
    const arrow = isIncrease ? 'fa-arrow-up' : 'fa-arrow-down';

    el.className = `stat-trend ${isGood ? 'positive' : 'negative'}`;
    el.innerHTML = `<i class="fa-solid ${arrow}" aria-hidden="true"></i> ${Math.abs(change)}% vs last month`;
}

function renderTrendError(selector) {
    const el = document.querySelector(selector);
    if (!el) return;
    el.className = 'stat-trend neutral';
    el.innerHTML = `<i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> Unable to load trend`;
}

async function loadBalanceTrend() {
    const res = await authFetch('/balance-trend');
    if (!res) {
        renderTrendError('.stat-balance .stat-trend');
        renderTrendError('.stat-income .stat-trend');
        renderTrendError('.stat-expense .stat-trend');
        return;
    }

    try {
        const data = await res.json();
        const d = data.data ?? data;

        renderTrend('.stat-balance .stat-trend', d.balance, 'No activity this month');
        renderTrend('.stat-income .stat-trend', d.income, 'No income this month');
        renderTrend('.stat-expense .stat-trend', d.expense, 'No expenses this month', /* invert */ true);
    } catch (e) {
        console.error('loadBalanceTrend parse error', e);
        renderTrendError('.stat-balance .stat-trend');
        renderTrendError('.stat-income .stat-trend');
        renderTrendError('.stat-expense .stat-trend');
    }
}

/* ================================================================
   CHARTS
================================================================ */
async function loadCharts() {
    await Promise.all([loadCategoryChart(), renderTrendChart()]);
}

async function loadCategoryChart() {
    const res = await authFetch('/category-data');
    if (!res) return;

    try {
        const json = await res.json();
        console.log("Category API:", json);

        const labels = json.labels || [];
        const values = json.data || [];

        const canvas = document.getElementById('categoryChart');
        if (!canvas) return;

        const ctx = canvas.getContext('2d');

        if (categoryChartInstance) {
            categoryChartInstance.destroy();
        }

        if (labels.length === 0 || values.length === 0) {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.font = "14px Inter";
            ctx.fillStyle = "#888";
            ctx.textAlign = "center";
            ctx.fillText("No expense data yet", canvas.width / 2, canvas.height / 2);
            return;
        }

        categoryChartInstance = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: [
                        '#2563eb', '#059669', '#d97706', '#dc2626',
                        '#8b5cf6', '#64748b', '#0891b2', '#be185d',
                        '#f59e0b', '#10b981', '#ef4444', '#6366f1'
                    ],
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom' } },
                cutout: '60%'
            }
        });
    } catch (e) {
        console.error("Category chart error:", e);
    }
}

async function renderTrendChart() {
    const ctx = document.getElementById('trendChart');
    if (!ctx) return;

    const res = await authFetch('/monthly-trend');
    if (!res) return;

    try {
        const json = await res.json();
        const data = json.data ?? json;

        if (trendChartInstance) { trendChartInstance.destroy(); trendChartInstance = null; }

        trendChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.months ?? [],
                datasets: [
                    { label: 'Income', data: data.income ?? [], borderColor: '#059669', backgroundColor: 'rgba(5,150,105,0.1)', tension: 0.4, fill: true, pointRadius: 4, pointHoverRadius: 6 },
                    { label: 'Expense', data: data.expense ?? [], borderColor: '#dc2626', backgroundColor: 'rgba(220,38,38,0.1)', tension: 0.4, fill: true, pointRadius: 4, pointHoverRadius: 6 },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'top' } },
                scales: {
                    y: { ticks: { callback: v => '₹' + Number(v).toLocaleString('en-IN') } },
                },
            },
        });
    } catch (e) {
        console.error('renderTrendChart error', e);
    }
}

/* Recurring-expense suggestions are now handled exclusively by the
   single V2 popup system in ai_insights.js (#recurringPopupV2),
   triggered once per page load from loadAllAIFeatures() below. */