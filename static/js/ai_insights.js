// ============================================================
// FILE: static/js/ai_insights.js
// Add <script src="/static/js/ai_insights.js"></script> to index.html
// Add `await loadAllAIFeatures();` at end of loadDashboard() in dashboard.js
// ============================================================
"use strict";

// ── 1. AI INSIGHTS ───────────────────────────────────────────────
async function loadAIInsights() {
    const c = document.getElementById("aiInsightsContainer");
    if (!c) return;
    c.innerHTML = "<div class=\"ai-insights-loading\"><i class=\"fa-solid fa-spinner fa-spin\"></i> Analyzing...</div>";
    try {
        const res = await authFetch("/ai-insights");
        if (!res) return;
        const data = await res.json();
        _renderAIInsights(data.insights || []);
    } catch (e) { c.innerHTML = "<p style=\"color:var(--text-tertiary)\">Could not load insights.</p>"; }
}

function _renderAIInsights(insights) {
    const c = document.getElementById("aiInsightsContainer");
    if (!c) return;
    const iconMap = { budget: "fa-wallet", trend: "fa-chart-line", category: "fa-tags", goal: "fa-bullseye" };
    if (!insights.length) {
        c.innerHTML = "<div class=\"ai-insight-card ai-insight-low\"><i class=\"fa-solid fa-circle-check ai-insight-icon\"></i><p>All metrics look healthy!</p></div>";
        return;
    }
    c.innerHTML = insights.map((ins, i) =>
        `<div class="ai-insight-card ai-insight-${ins.level}" style="animation-delay:${i * 0.09}s">
            <div class="ai-insight-header">
                <i class="fa-solid ${iconMap[ins.type] || "fa-lightbulb"} ai-insight-icon"></i>
                <span class="ai-insight-badge ai-badge-${ins.level}">${ins.level.toUpperCase()}</span>
            </div>
            <p class="ai-insight-msg">${escapeHtml(ins.message)}</p>
        </div>`
    ).join("");
}

// ── 2+3. RISK SCORE + NAV BADGE ──────────────────────────────────
async function loadRiskScore() {
    try {
        const res = await authFetch("/risk-score");
        if (!res) return;
        _renderRiskIndicator(await res.json());
    } catch (e) { }
}

function _renderRiskIndicator(data) {
    const el = document.getElementById("riskIndicator");
    if (!el) return;
    const icons = { low: "fa-shield-check", medium: "fa-shield-halved", high: "fa-shield-exclamation" };
    const lv = data.risk_level || "low";
    el.innerHTML = `<div class="risk-dot risk-dot-${lv}" title="${escapeHtml(data.tooltip || "")}">
        <i class="fa-solid ${icons[lv]}"></i><span>${data.health_score}</span></div>`;
}

async function loadNavBadge() {
    try {
        const res = await authFetch("/insight-badge");
        if (!res) return;
        _renderNavBadge(await res.json());
    } catch (e) { }
}

function _renderNavBadge(data) {
    document.querySelectorAll(".ai-nav-badge").forEach(el => el.remove());
    if (!data.count) return;
    const nav = document.querySelector("[data-target=\"insights\"]");
    if (!nav) return;
    const b = document.createElement("span");
    b.className = `ai-nav-badge ai-nav-badge-${data.color}`;
    b.textContent = data.count;
    nav.style.position = "relative";
    nav.appendChild(b);
}

// ── 4. SMART NUDGE ───────────────────────────────────────────────
async function loadSmartNudge() {
    try {
        const res = await authFetch("/smart-nudge");
        if (!res) return;
        const data = await res.json();
        document.getElementById("smartNudgeCard")?.remove();
        if (data.nudge) _renderNudge(data.nudge);
    } catch (e) { }
}

function _renderNudge(nudge) {
    const anchor = document.querySelector(".stats-grid");
    if (!anchor) return;
    const card = document.createElement("div");
    card.id = "smartNudgeCard"; card.className = "smart-nudge-card";
    card.innerHTML = `
        <div class="nudge-icon"><i class="fa-solid fa-bell-ring"></i></div>
        <div class="nudge-content"><strong>Month-End Alert</strong>
            <p>${escapeHtml(nudge.message)}</p></div>
        <button class="nudge-dismiss" onclick="document.getElementById('smartNudgeCard').remove()" title="Dismiss">
            <i class="fa-solid fa-xmark"></i></button>`;
    anchor.parentNode.insertBefore(card, anchor);
}

// ── 5. BEHAVIORAL PATTERNS ───────────────────────────────────────
async function loadBehavioralPatterns() {
    const c = document.getElementById("behavioralPatternsContainer");
    if (!c) return;
    try {
        const res = await authFetch("/behavioral-patterns");
        if (!res) return;
        _renderPatterns((await res.json()).patterns || []);
    } catch (e) { }
}

function _renderPatterns(patterns) {
    const c = document.getElementById("behavioralPatternsContainer");
    if (!c) return;
    if (!patterns.length) {
        c.innerHTML = "<p style=\"color:var(--text-tertiary);font-size:.875rem;padding:8px 0\">No behavioral patterns detected yet.</p>";
        return;
    }
    c.innerHTML = patterns.map((p, i) =>
        `<div class="pattern-item pattern-${p.severity}" style="animation-delay:${i * 0.07}s">
            <button class="pattern-toggle" onclick="togglePattern(this)" aria-expanded="false">
                <span class="pattern-severity-dot pattern-dot-${p.severity}"></span>
                <strong>${escapeHtml(p.title)}</strong>
                <i class="fa-solid fa-chevron-down pattern-chevron"></i>
            </button>
            <div class="pattern-body hidden"><p>${escapeHtml(p.description)}</p></div>
        </div>`
    ).join("");
}

function togglePattern(btn) {
    const body = btn.nextElementSibling;
    const chev = btn.querySelector(".pattern-chevron");
    const isOpen = btn.getAttribute("aria-expanded") === "true";
    body.classList.toggle("hidden", isOpen);
    chev.style.transform = isOpen ? "" : "rotate(180deg)";
    btn.setAttribute("aria-expanded", String(!isOpen));
}

// ── 6. RECURRING POPUP V2 (single recurring-expense UI system) ──
// This is the ONLY recurring-suggestion trigger in the app. It is
// guarded to run once per page load (session), regardless of how many
// times loadDashboard()/loadAllAIFeatures() re-run (dashboard revisits,
// section switches). Cross-reload suppression of handled occurrences is
// persisted server-side (recurring_suggestion_state), not in this flag.
let _rqQueue = [], _rqIdx = 0;
let _rqSessionStarted = false;

async function loadRecurringV2() {
    if (_rqSessionStarted) return;   // single trigger per page session
    _rqSessionStarted = true;
    try {
        const res = await authFetch("/recurring-suggestions-v2");
        if (!res) return;
        const data = await res.json();
        // Defensive client-side dedupe by merchant/description key, in
        // case the endpoint ever returns duplicates for the same
        // merchant/occurrence.
        const seen = new Set();
        const deduped = [];
        for (const item of data) {
            const key = (item.description || "").toLowerCase().trim();
            if (seen.has(key)) continue;
            seen.add(key);
            deduped.push(item);
        }
        if (deduped.length) { _rqQueue = deduped; _rqIdx = 0; _showRQ(); }
    } catch (e) { }
}

function _showRQ() {
    document.getElementById("recurringPopupV2")?.remove();
    if (_rqIdx >= _rqQueue.length) return;
    const item = _rqQueue[_rqIdx];
    const popup = document.createElement("div");
    popup.id = "recurringPopupV2"; popup.className = "recurring-popup-v2";
    popup.innerHTML = `
        <div class="recurring-popup-inner">
            <div class="recurring-popup-icon"><i class="fa-solid fa-rotate"></i></div>
            <div class="recurring-popup-body">
                <strong>Recurring Expense Detected</strong>
                <p>You usually pay <strong>Rs.${item.amount}</strong> for
                <em>${escapeHtml(item.description)}</em> around this date.</p>
            </div>
            <div class="recurring-popup-actions">
                <button class="btn-primary btn-sm" id="rqAddBtn">Add Now</button>
                <button class="btn-secondary btn-sm" id="rqDismissBtn">Dismiss</button>
            </div>
        </div>`;
    document.body.appendChild(popup);
    requestAnimationFrame(() => popup.classList.add("visible"));
    document.getElementById("rqAddBtn").onclick = () => _addRQ(item);
    document.getElementById("rqDismissBtn").onclick = _dismissRQ;
}

async function _addRQ(item) {
    let added = false;
    try {
        const res = await authFetch("/add-transaction", {
            method: "POST",
            body: JSON.stringify({ description: item.description, amount: item.amount, type: "expense", category: item.category })
        });
        if (res && res.ok) {
            added = true;
            showNotification(`Added Rs.${item.amount} for ${item.description}`, "success");
            // Refresh dashboard numbers in the background; does not
            // touch the recurring queue (guarded by _rqSessionStarted).
            loadDashboard();
        }
    } catch (e) { }
    await _advanceRQ(item, added ? "added" : "dismissed");
}

async function _markRQ(item, status) {
    try {
        await authFetch("/recurring-suggestions-v2/mark", {
            method: "POST",
            body: JSON.stringify({
                description: item.description,
                occurrence_period: item.occurrence_period,
                status
            })
        });
    } catch (e) { }
}

function _dismissRQ() {
    _advanceRQ(_rqQueue[_rqIdx], "dismissed");
}

async function _advanceRQ(item, status) {
    const p = document.getElementById("recurringPopupV2");
    if (p) { p.classList.remove("visible"); setTimeout(() => p.remove(), 300); }
    if (item) await _markRQ(item, status);
    _rqIdx++;
    if (_rqIdx < _rqQueue.length) setTimeout(_showRQ, 450);
}

// ── BOOTSTRAP ────────────────────────────────────────────────────
async function loadAllAIFeatures() {
    await Promise.all([loadAIInsights(), loadRiskScore(), loadNavBadge(), loadSmartNudge(), loadBehavioralPatterns()]);
    setTimeout(loadRecurringV2, 1200);
}