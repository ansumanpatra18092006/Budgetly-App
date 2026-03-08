'use strict';

/* ================================================================
   ROADMAP — AI-powered goal roadmap generator
   Integrates with existing goals system in Budgetly
================================================================ */

let _roadmapGoals   = [];
let _activeRoadmap  = null;

/* ── Entry point called from navigation ──────────────────────── */
async function loadRoadmap() {
    const page = document.getElementById('roadmap');
    if (!page) return;

    _renderRoadmapShell();
    await _fetchGoalsForRoadmap();
}

/* ── Render the static page shell ───────────────────────────── */
function _renderRoadmapShell() {
    const page = document.getElementById('roadmap');
    if (!page) return;

    page.innerHTML = `
    <div class="page-header">
        <div>
            <h1 class="page-title">Goal Roadmap</h1>
            <p class="page-subtitle">
                <i class="fa-solid fa-map-location-dot" aria-hidden="true"></i>
                AI-generated step-by-step plan to hit every goal
            </p>
        </div>
    </div>

    <div class="roadmap-goal-selector" id="roadmapGoalSelector">
        <div class="roadmap-selector-loading">
            <i class="fa-solid fa-spinner fa-spin"></i> Loading your goals…
        </div>
    </div>

    <div class="roadmap-viewport" id="roadmapViewport">
        <div class="roadmap-empty-state" id="roadmapEmptyState">
            <div class="roadmap-empty-icon">
                <i class="fa-solid fa-map"></i>
            </div>
            <h3>Select a Goal Above</h3>
            <p>Choose any goal to generate a personalised AI roadmap with monthly milestones, actionable steps, and savings checkpoints.</p>
        </div>
    </div>

    <div class="roadmap-modal hidden" id="roadmapModal" role="dialog" aria-modal="true" aria-labelledby="roadmapModalTitle">
        <div class="roadmap-modal-panel">
            <div class="roadmap-modal-header">
                <div>
                    <h2 class="roadmap-modal-title" id="roadmapModalTitle">Roadmap</h2>
                    <p class="roadmap-modal-subtitle" id="roadmapModalSubtitle"></p>
                </div>
                <button class="roadmap-modal-close" onclick="closeRoadmapModal()" aria-label="Close roadmap">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>
            <div class="roadmap-modal-body" id="roadmapModalBody"></div>
        </div>
    </div>`;
}

/* ── Fetch goals from existing API ──────────────────────────── */
async function _fetchGoalsForRoadmap() {
    const selector = document.getElementById('roadmapGoalSelector');
    if (!selector) return;

    try {
        const res = await authFetch('/get-goals-detailed');
        if (!res || !res.ok) throw new Error('fetch failed');

        const json        = await res.json();
        // Backend returns plain JSON — no wrapper object
        _roadmapGoals     = (json.goals ?? []).filter(g => g.status !== 'completed');

        _renderGoalSelector(_roadmapGoals);
    } catch (e) {
        selector.innerHTML = `
            <div class="roadmap-selector-error">
                <i class="fa-solid fa-circle-exclamation"></i>
                Failed to load goals. <button onclick="_fetchGoalsForRoadmap()">Retry</button>
            </div>`;
    }
}

/* ── Render pill-style goal selector ───────────────────────── */
function _renderGoalSelector(goals) {
    const selector = document.getElementById('roadmapGoalSelector');
    if (!selector) return;

    if (!goals || goals.length === 0) {
        selector.innerHTML = `
            <div class="roadmap-no-goals">
                <i class="fa-solid fa-circle-info"></i>
                No active goals found. <button onclick="showPage('goals')" class="roadmap-link-btn">Create a goal</button> first.
            </div>`;
        return;
    }

    const categoryIcons = {
        Savings:'fa-piggy-bank', Investment:'fa-chart-line', Emergency:'fa-shield-halved',
        Vacation:'fa-plane',     Education:'fa-graduation-cap', Home:'fa-house',
        Vehicle:'fa-car',        Retirement:'fa-umbrella-beach',
    };

    selector.innerHTML = `
        <div class="roadmap-selector-label">
            <i class="fa-solid fa-bullseye"></i> Select a goal to generate roadmap
        </div>
        <div class="roadmap-goal-pills">
            ${goals.map(g => {
                const icon        = categoryIcons[g.category] ?? 'fa-bullseye';
                const pct         = g.progress_percent ?? 0;
                const statusClass = g.status === 'at_risk' ? 'pill-risk' : g.status === 'on_track' ? 'pill-on-track' : '';
                return `
                <button class="roadmap-goal-pill ${statusClass}"
                        id="pill-${g.id}"
                        onclick="generateRoadmap(${g.id})"
                        title="${escapeHtml(g.name)}">
                    <span class="pill-icon"><i class="fa-solid ${icon}"></i></span>
                    <span class="pill-info">
                        <span class="pill-name">${escapeHtml(g.name)}</span>
                        <span class="pill-meta">${pct.toFixed(0)}% · ₹${Number(g.target_amount || 0).toLocaleString('en-IN',{maximumFractionDigits:0})}</span>
                    </span>
                    <span class="pill-arrow"><i class="fa-solid fa-arrow-right"></i></span>
                </button>`;
            }).join('')}
        </div>`;
}

/* ── Main: generate AI roadmap for a goal ──────────────────── */
async function generateRoadmap(goalId) {
    const goal = _roadmapGoals.find(g => g.id === goalId);
    if (!goal) return;

    // Mark active pill
    document.querySelectorAll('.roadmap-goal-pill').forEach(p => p.classList.remove('active'));
    const activePill = document.getElementById(`pill-${goalId}`);
    if (activePill) activePill.classList.add('active');

    // Show loading state in viewport
    const viewport = document.getElementById('roadmapViewport');
    if (viewport) {
        viewport.innerHTML = `
            <div class="roadmap-generating">
                <div class="roadmap-gen-animation">
                    <div class="roadmap-gen-ring"></div>
                    <i class="fa-solid fa-map-location-dot roadmap-gen-icon"></i>
                </div>
                <h3>Building Your Roadmap</h3>
                <p>Analysing <strong>${escapeHtml(goal.name)}</strong> and creating personalised milestones…</p>
                <div class="roadmap-gen-steps">
                    <span class="gen-step active" id="genStep1"><i class="fa-solid fa-spinner fa-spin"></i> Analysing goal data</span>
                    <span class="gen-step" id="genStep2"><i class="fa-solid fa-clock"></i> Calculating milestones</span>
                    <span class="gen-step" id="genStep3"><i class="fa-solid fa-clock"></i> Writing action steps</span>
                </div>
            </div>`;

        setTimeout(() => {
            const s1 = document.getElementById('genStep1');
            const s2 = document.getElementById('genStep2');
            if (s1) { s1.innerHTML = '<i class="fa-solid fa-check"></i> Goal data analysed'; s1.classList.add('done'); }
            if (s2) { s2.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Calculating milestones'; s2.classList.add('active'); }
        }, 800);
        setTimeout(() => {
            const s2 = document.getElementById('genStep2');
            const s3 = document.getElementById('genStep3');
            if (s2) { s2.innerHTML = '<i class="fa-solid fa-check"></i> Milestones calculated'; s2.classList.add('done'); }
            if (s3) { s3.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Writing action steps'; s3.classList.add('active'); }
        }, 1600);
    }

    try {
        // ── Call YOUR Flask backend, not Anthropic directly ────────
        const res = await authFetch('/generate-roadmap', {
            method: 'POST',
            body: JSON.stringify({ goal_id: goalId }),
        });

        if (!res || !res.ok) {
            let msg = 'Failed to generate roadmap';
            try { const d = await res.json(); msg = d.error || msg; } catch (_) {}
            throw new Error(msg);
        }

        const roadmapData = await res.json();
        _activeRoadmap = { goal, roadmap: roadmapData };
        _renderRoadmapViewport(goal, roadmapData);

    } catch (e) {
        console.error('Roadmap generation error:', e);
        if (viewport) {
            viewport.innerHTML = `
                <div class="roadmap-error">
                    <i class="fa-solid fa-circle-exclamation"></i>
                    <p>${escapeHtml(e.message || 'Failed to generate roadmap. Please try again.')}</p>
                    <button class="btn-primary" onclick="generateRoadmap(${goalId})">
                        <i class="fa-solid fa-rotate-right"></i> Retry
                    </button>
                </div>`;
        }
    }
}

/* ── Render the roadmap in the viewport ─────────────────────── */
function _renderRoadmapViewport(goal, roadmap) {
    const viewport = document.getElementById('roadmapViewport');
    if (!viewport) return;

    const difficultyMeta = {
        easy:        { color: 'var(--success)', icon: 'fa-circle-check', label: 'Easy' },
        moderate:    { color: 'var(--warning)', icon: 'fa-chart-line',   label: 'Moderate' },
        challenging: { color: 'var(--danger)',  icon: 'fa-fire',         label: 'Challenging' },
    };
    const stratMeta = {
        conservative: { color: '#64748b', icon: 'fa-shield',        label: 'Conservative' },
        balanced:     { color: '#2563eb', icon: 'fa-scale-balanced', label: 'Balanced' },
        aggressive:   { color: '#dc2626', icon: 'fa-rocket',         label: 'Aggressive' },
    };
    const diff  = difficultyMeta[roadmap.difficulty]  ?? difficultyMeta.moderate;
    const strat = stratMeta[roadmap.strategy]          ?? stratMeta.balanced;
    const phases = roadmap.phases ?? [];

    viewport.innerHTML = `
    <div class="roadmap-header-card">
        <div class="roadmap-header-top">
            <div class="roadmap-header-meta">
                <span class="roadmap-badge" style="color:${diff.color};border-color:${diff.color}20;background:${diff.color}12;">
                    <i class="fa-solid ${diff.icon}"></i> ${diff.label}
                </span>
                <span class="roadmap-badge" style="color:${strat.color};border-color:${strat.color}20;background:${strat.color}12;">
                    <i class="fa-solid ${strat.icon}"></i> ${strat.label}
                </span>
                <span class="roadmap-badge roadmap-badge-phases">
                    <i class="fa-solid fa-layer-group"></i> ${phases.length} Phases
                </span>
            </div>
            <button class="roadmap-expand-btn" onclick="openRoadmapModal()" title="Expand full roadmap">
                <i class="fa-solid fa-expand"></i> Full View
            </button>
        </div>
        <p class="roadmap-summary">${escapeHtml(roadmap.summary ?? '')}</p>
    </div>

    <div class="roadmap-timeline" id="roadmapTimeline">
        ${phases.map((phase, i) => _renderPhaseCard(phase, i, phases.length)).join('')}
    </div>

    <div class="roadmap-bottom-grid">
        ${roadmap.quick_wins?.length ? `
        <div class="roadmap-quick-wins">
            <h3><i class="fa-solid fa-bolt"></i> Quick Wins</h3>
            <ul>${roadmap.quick_wins.map(w => `<li><i class="fa-solid fa-check"></i> ${escapeHtml(w)}</li>`).join('')}</ul>
        </div>` : ''}
        ${roadmap.risks?.length ? `
        <div class="roadmap-risks">
            <h3><i class="fa-solid fa-triangle-exclamation"></i> Watch Out For</h3>
            <ul>${roadmap.risks.map(r => `<li><i class="fa-solid fa-circle-dot"></i> ${escapeHtml(r)}</li>`).join('')}</ul>
        </div>` : ''}
    </div>

    ${roadmap.motivation ? `
    <div class="roadmap-motivation">
        <i class="fa-solid fa-star"></i>
        <p>${escapeHtml(roadmap.motivation)}</p>
    </div>` : ''}

    <div style="text-align:center;margin-top:var(--spacing-xl);">
        <button class="btn-secondary" onclick="generateRoadmap(${goal.id})" style="gap:8px;">
            <i class="fa-solid fa-rotate-right"></i> Regenerate Roadmap
        </button>
    </div>`;
}

function _renderPhaseCard(phase, index, total) {
    const isLast = index === total - 1;
    const steps  = phase.steps ?? [];
    return `
    <div class="roadmap-phase" style="animation-delay:${index * 0.1}s">
        ${!isLast ? '<div class="roadmap-connector"></div>' : ''}
        <div class="roadmap-phase-node">
            <span class="roadmap-phase-emoji">${phase.emoji || '🎯'}</span>
        </div>
        <div class="roadmap-phase-card">
            <div class="roadmap-phase-header">
                <div class="roadmap-phase-title-row">
                    <span class="roadmap-phase-num">Phase ${phase.phase}</span>
                    <h3 class="roadmap-phase-title">${escapeHtml(phase.title ?? '')}</h3>
                </div>
                <div class="roadmap-phase-meta">
                    <span><i class="fa-solid fa-calendar-days"></i> ${escapeHtml(phase.duration ?? '')}</span>
                    ${phase.target_saving ? `<span><i class="fa-solid fa-arrow-trend-up"></i> Save ₹${Number(phase.target_saving).toLocaleString('en-IN')}/mo</span>` : ''}
                    ${phase.milestone_amount ? `<span class="roadmap-milestone"><i class="fa-solid fa-flag"></i> Reach ₹${Number(phase.milestone_amount).toLocaleString('en-IN')}</span>` : ''}
                </div>
            </div>
            <div class="roadmap-phase-body">
                <ol class="roadmap-steps">
                    ${steps.map(s => `<li>${escapeHtml(s)}</li>`).join('')}
                </ol>
                ${phase.tip ? `
                <div class="roadmap-tip">
                    <i class="fa-solid fa-lightbulb"></i>
                    <span>${escapeHtml(phase.tip)}</span>
                </div>` : ''}
            </div>
        </div>
    </div>`;
}

/* ── Full-view modal ─────────────────────────────────────────── */
function openRoadmapModal() {
    if (!_activeRoadmap || !_activeRoadmap.roadmap) return;
    const { goal, roadmap } = _activeRoadmap;

    const modal = document.getElementById('roadmapModal');
    const title = document.getElementById('roadmapModalTitle');
    const sub   = document.getElementById('roadmapModalSubtitle');
    const body  = document.getElementById('roadmapModalBody');
    if (!modal || !body) return;

    title.textContent = goal.name + ' — Full Roadmap';
    sub.textContent   = `${roadmap.phases?.length ?? 0} phases · ${roadmap.strategy} strategy`;

    const phases = roadmap.phases ?? [];
    body.innerHTML = `
        <div class="roadmap-modal-summary">${escapeHtml(roadmap.summary ?? '')}</div>
        <div class="roadmap-modal-timeline">
            ${phases.map((phase, i) => `
            <div class="roadmap-modal-phase" style="animation-delay:${i * 0.07}s">
                <div class="roadmap-modal-phase-left">
                    <div class="roadmap-modal-phase-circle">
                        <span>${phase.emoji || '🎯'}</span>
                    </div>
                    ${i < phases.length - 1 ? '<div class="roadmap-modal-line"></div>' : ''}
                </div>
                <div class="roadmap-modal-phase-content">
                    <div class="roadmap-modal-phase-header">
                        <span class="roadmap-phase-num">Phase ${phase.phase}</span>
                        <strong>${escapeHtml(phase.title ?? '')}</strong>
                        <span class="roadmap-modal-duration">${escapeHtml(phase.duration ?? '')}</span>
                    </div>
                    ${phase.milestone_amount ? `
                    <div class="roadmap-modal-milestone">
                        <i class="fa-solid fa-flag"></i> Milestone: ₹${Number(phase.milestone_amount).toLocaleString('en-IN')}
                        ${phase.target_saving ? ` · Save ₹${Number(phase.target_saving).toLocaleString('en-IN')}/mo` : ''}
                    </div>` : ''}
                    <ol class="roadmap-modal-steps">
                        ${(phase.steps ?? []).map(s => `<li>${escapeHtml(s)}</li>`).join('')}
                    </ol>
                    ${phase.tip ? `<div class="roadmap-modal-tip"><i class="fa-solid fa-lightbulb"></i> ${escapeHtml(phase.tip)}</div>` : ''}
                </div>
            </div>`).join('')}
        </div>
        ${roadmap.motivation ? `<div class="roadmap-modal-motivation"><i class="fa-solid fa-star"></i> ${escapeHtml(roadmap.motivation)}</div>` : ''}`;

    modal.classList.remove('hidden');
}

function closeRoadmapModal() {
    document.getElementById('roadmapModal')?.classList.add('hidden');
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('roadmapModal')?.addEventListener('click', function(e) {
        if (e.target === this) closeRoadmapModal();
    });
});