/* ================================================================
   DASHBOARD
================================================================ */
async function loadDashboard() {
    updateDate();
    await Promise.all([
        fetchSummary(),
        fetchBudget(),
        loadCharts(),
        loadHealth(),
        loadTopCategories(),
        loadBalanceTrend(),
    ]);
    await loadAllAIFeatures();   // ← ADD THIS LINE
    print("GOALS RAW RESPONSE: $goalMaps");
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

async function loadHealth() {
    const res = await authFetch('/health-metrics');
    if (!res) return;

    try {
        const data = await res.json();
        const d = data.data ?? data;

        const score = d.health_score ?? 0;
        const sr = d.savings_rate ?? 0;
        const ba = d.budget_adherence ?? 0;
        const is_ = d.income_stability ?? 0;

        setEl('healthScore', score);
        setEl('healthLabel', getHealthLabel(score));
        const circle = document.querySelector('.score-circle');
        if (circle) circle.style.setProperty('--score', Math.min(score, 100));
        setEl('savingsRate', sr + '%');
        setEl('budgetAdherence', ba + '%');
        setEl('incomeStability', is_ + '%');

        setWidth('savingsBar', Math.min(sr, 100));
        setWidth('budgetBar', Math.min(ba, 100));
        setWidth('incomeBar', Math.min(is_, 100));
    } catch (e) {
        console.error('loadHealth parse error', e);
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

async function loadBalanceTrend() {
    const res = await authFetch('/balance-trend');
    if (!res) return;

    try {
        const data = await res.json();
        const d = data.data ?? data;
        const change = d.change ?? 0;
        const trendEl = document.querySelector('.stat-balance .stat-trend');
        if (!trendEl) return;

        if (change >= 0) {
            trendEl.className = 'stat-trend positive';
            trendEl.innerHTML = `<i class="fa-solid fa-arrow-up" aria-hidden="true"></i> +${change}% vs last month`;
        } else {
            trendEl.className = 'stat-trend negative';
            trendEl.innerHTML = `<i class="fa-solid fa-arrow-down" aria-hidden="true"></i> ${change}% vs last month`;
        }
    } catch (e) {
        console.error('loadBalanceTrend parse error', e);
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

let recurringData = [];
let currentRecurringIndex = 0;

async function checkRecurring() {
    const res = await fetch('/recurring-suggestions', {
        credentials: 'include'
    });

    recurringData = await res.json();

    if (!recurringData.length) return;

    currentRecurringIndex = 0;
    showRecurringPopup();
}

function showRecurringPopup() {
    const item = recurringData[currentRecurringIndex];

    document.getElementById('recurringText').innerText =
        `You usually pay ₹${item.amount} for ${item.description}. Add it now?`;

    document.getElementById('recurringModal').classList.remove('hidden');
}

document.getElementById('recurringYes').addEventListener('click', async () => {
    const item = recurringData[currentRecurringIndex];

    await fetch('/add-transaction', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
            description: item.description,
            amount: item.amount,
            type: 'expense'
        })
    });

    document.getElementById('recurringModal').classList.add('hidden');

    currentRecurringIndex++;

    if (currentRecurringIndex < recurringData.length) {
        showRecurringPopup();
    } else {
        location.reload();
    }
});

document.getElementById('recurringNo').addEventListener('click', () => {
    document.getElementById('recurringModal').classList.add('hidden');
});

window.addEventListener('load', () => {
    checkRecurring();
});