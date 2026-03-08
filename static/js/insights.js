'use strict';

/* ================================================================
   INSIGHTS
================================================================ */
function loadInsights() {
    renderInsights('analysis');
    loadTopCategories();
    loadSpendingInsights();
}

async function renderInsights(type) {
    // Update active tab
    document.querySelectorAll('.insights-tab').forEach((tab, i) => {
        const types = ['analysis','trends','anomalies','recommendations','subscriptions'];
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
            const json     = await res.json();
            const insights = json.data ?? json;

            if (!Array.isArray(insights) || insights.length === 0) {
                html = `<p style="color:var(--text-tertiary);">No spending insights yet. Add more transactions to see patterns.</p>`;
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

            if (!data.anomalies || data.anomalies.length === 0) {
                html = `
                    <div style="text-align:center;padding:var(--spacing-lg);color:var(--success);">
                        <i class="fa-solid fa-shield-check" style="font-size:2rem;display:block;margin-bottom:8px;" aria-hidden="true"></i>
                        <p>No unusual transactions detected. Your spending looks normal!</p>
                    </div>`;
            } else {
                html = `
                    <h3 style="font-size:1rem;font-weight:700;margin-bottom:12px;color:var(--danger);">
                        <i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                        ${data.anomalies.length} Unusual Transaction${data.anomalies.length > 1 ? 's' : ''} Detected
                    </h3>
                    ${data.anomalies.map(a => `
                        <div style="margin-bottom:8px;padding:10px 14px;background:var(--danger-light);border-radius:var(--radius-md);color:var(--danger);display:flex;justify-content:space-between;">
                            <span>Transaction #${a.id}</span>
                            <strong style="font-family:var(--font-mono);">₹${Number(a.amount).toLocaleString('en-IN')}</strong>
                        </div>`).join('')}`;
            }
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

        container.innerHTML = html;

        // Append risk panel
        try {
            const riskRes = await authFetch('/risk-analysis');
            if (riskRes) {
                const riskJson = await riskRes.json();
                const rd       = riskJson.data ?? riskJson;
                const level    = (rd.risk ?? 'LOW').toLowerCase();
                container.innerHTML += `
                    <div class="risk-panel ${level}" style="margin-top:var(--spacing-xl);">
                        <h3><i class="fa-solid fa-shield-halved" aria-hidden="true"></i> Financial Risk: ${rd.risk ?? 'Unknown'}</h3>
                        <p>Budget breach probability: <strong>${rd.probability ?? 0}%</strong></p>
                        <p>Projected monthly expense: <strong>₹${Number(rd.projected_expense ?? 0).toLocaleString('en-IN')}</strong></p>
                        ${rd.days_left !== undefined ? `<p>Estimated days of runway: <strong>${rd.days_left}</strong></p>` : ''}
                    </div>`;
            }
        } catch (_) { /* risk panel is non-critical */ }

    } catch (err) {
        container.innerHTML = `<p style="color:var(--danger);">Error loading insights. Please try again.</p>`;
        console.error('renderInsights error:', err);
    }
}

async function loadSpendingInsights() {
    const res = await authFetch('/spending-insights');
    if (!res) return;

    const container = document.getElementById('insightsContainer');
    if (!container) return;

    try {
        const json     = await res.json();
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