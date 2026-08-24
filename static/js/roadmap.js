'use strict';

/* ================================================================
   ROADMAP — AI-powered goal roadmap generator
   Integrates with existing goals system in FinTrust
================================================================ */

let _roadmapGoals = [];
let _activeRoadmap = null;

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

        const json = await res.json();
        // Backend returns plain JSON — no wrapper object
        _roadmapGoals = (json.goals ?? []).filter(g => g.status !== 'completed');

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
        Savings: 'fa-piggy-bank', Investment: 'fa-chart-line', Emergency: 'fa-shield-halved',
        Vacation: 'fa-plane', Education: 'fa-graduation-cap', Home: 'fa-house',
        Vehicle: 'fa-car', Retirement: 'fa-umbrella-beach',
    };

    selector.innerHTML = `
        <div class="roadmap-selector-label">
            <i class="fa-solid fa-bullseye"></i> Select a goal to generate roadmap
        </div>
        <div class="roadmap-goal-pills">
            ${goals.map(g => {
        const icon = categoryIcons[g.category] ?? 'fa-bullseye';
        const pct = g.progress_percent ?? 0;
        const statusClass = g.status === 'at_risk' ? 'pill-risk' : g.status === 'on_track' ? 'pill-on-track' : '';
        return `
                <button class="roadmap-goal-pill ${statusClass}"
                        id="pill-${g.id}"
                        onclick="generateRoadmap(${g.id})"
                        title="${escapeHtml(g.name)}">
                    <span class="pill-icon"><i class="fa-solid ${icon}"></i></span>
                    <span class="pill-info">
                        <span class="pill-name">${escapeHtml(g.name)}</span>
                        <span class="pill-meta">${pct.toFixed(0)}% · ₹${Number(g.target_amount || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>
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
            try { const d = await res.json(); msg = d.error || msg; } catch (_) { }
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

    // NOTE: keys below match the ACTUAL backend contract (routes/goals.py
    // _compute_roadmap): difficulty is "Easy"/"Medium"/"Hard", phases use
    // title/target_savings/milestone/actions/tip. Older code here expected
    // a different (unused) schema — that mismatch is what silently blanked
    // out phase durations/steps/emoji; fixed to match the real response.
    const difficultyMeta = {
        Easy: { color: 'var(--success)', icon: 'fa-circle-check', label: 'Easy' },
        Medium: { color: 'var(--warning)', icon: 'fa-chart-line', label: 'Medium' },
        Hard: { color: 'var(--danger)', icon: 'fa-fire', label: 'Hard' },
    };
    const stratMeta = {
        conservative: { color: '#64748b', icon: 'fa-shield', label: 'Conservative' },
        balanced: { color: '#2563eb', icon: 'fa-scale-balanced', label: 'Balanced' },
        aggressive: { color: '#dc2626', icon: 'fa-rocket', label: 'Aggressive' },
    };
    const diff = difficultyMeta[roadmap.difficulty] ?? difficultyMeta.Medium;
    const strat = stratMeta[roadmap.strategy] ?? stratMeta.balanced;
    const phases = roadmap.phases ?? [];

    // Deadline vs. capacity — shown as two distinct, clearly labeled facts,
    // never merged into one ambiguous number.
    const planBadge = roadmap.plan_type === 'deadline'
        ? `<span class="roadmap-badge" style="color:var(--primary);border-color:var(--primary)20;background:var(--primary)12;">
               <i class="fa-solid fa-calendar-check"></i> Target: ${phases.length} mo
           </span>`
        : `<span class="roadmap-badge" style="color:var(--text-secondary);border-color:var(--border-subtle);">
               <i class="fa-solid fa-gauge"></i> Estimate: ${phases.length} mo at current pace
           </span>`;

    const riskBadge = roadmap.deadline_status === 'at_risk'
        ? `<span class="roadmap-badge" style="color:var(--danger);border-color:var(--danger)20;background:var(--danger)12;">
               <i class="fa-solid fa-triangle-exclamation"></i> At Risk — ₹${Math.round(roadmap.capacity_gap).toLocaleString('en-IN')}/mo short
           </span>`
        : roadmap.deadline_status === 'on_track'
            ? `<span class="roadmap-badge" style="color:var(--success);border-color:var(--success)20;background:var(--success)12;">
                   <i class="fa-solid fa-circle-check"></i> On Track
               </span>`
            : '';

    // Long timelines get a collapsible quarterly view; the backend never
    // truncates the actual phases — this is presentation only.
    const useGroups = roadmap.phase_view?.mode === 'quarterly' && roadmap.phase_view.groups?.length;

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
                ${planBadge}
                ${riskBadge}
            </div>
            <button class="roadmap-expand-btn" onclick="openRoadmapModal()" title="Expand full roadmap">
                <i class="fa-solid fa-expand"></i> Full View
            </button>
        </div>
        <p class="roadmap-summary">${escapeHtml(roadmap.summary ?? '')}</p>
        ${roadmap.current_monthly_capacity != null ? `
        <p class="roadmap-capacity-note" style="font-size:.85rem;color:var(--text-tertiary);margin-top:6px;">
            Required ₹${Math.round(roadmap.monthly_savings_needed).toLocaleString('en-IN')}/mo ·
            Current capacity ₹${Math.round(roadmap.current_monthly_capacity).toLocaleString('en-IN')}/mo
        </p>` : ''}
        ${roadmap.phases_truncated ? `
        <p style="font-size:.8rem;color:var(--warning);margin-top:4px;">
            <i class="fa-solid fa-circle-info"></i> Timeline is long — showing the first ${phases.length} months.
        </p>` : ''}
    </div>

    <div class="roadmap-timeline" id="roadmapTimeline">
        ${useGroups
            ? roadmap.phase_view.groups.map((g, gi) => _renderPhaseGroup(g, phases, gi)).join('')
            : phases.map((phase, i) => _renderPhaseCard(phase, i, phases.length)).join('')}
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

    <div class="roadmap-ai-section" id="roadmapAiSection" style="margin-top:var(--spacing-lg);">
        <button class="btn-secondary" onclick="explainRoadmapWithAI(${goal.id})" id="aiExplainBtn" style="gap:8px;">
            <i class="fa-solid fa-wand-magic-sparkles"></i> Explain with AI
        </button>
        <div id="aiExplanationBody"></div>
    </div>

    <div style="text-align:center;margin-top:var(--spacing-xl);">
        <button class="btn-secondary" onclick="generateRoadmap(${goal.id})" style="gap:8px;">
            <i class="fa-solid fa-rotate-right"></i> Regenerate Roadmap
        </button>
    </div>`;
}

function _renderPhaseCard(phase, index, total) {
    const isLast = index === total - 1;
    const actions = phase.actions ?? [];
    return `
    <div class="roadmap-phase" style="animation-delay:${Math.min(index, 20) * 0.1}s">
        ${!isLast ? '<div class="roadmap-connector"></div>' : ''}
        <div class="roadmap-phase-node">
            <span class="roadmap-phase-emoji">🎯</span>
        </div>
        <div class="roadmap-phase-card">
            <div class="roadmap-phase-header">
                <div class="roadmap-phase-title-row">
                    <span class="roadmap-phase-num">Phase ${index + 1}</span>
                    <h3 class="roadmap-phase-title">${escapeHtml(phase.title ?? '')}</h3>
                </div>
                <div class="roadmap-phase-meta">
                    ${phase.target_savings ? `<span><i class="fa-solid fa-arrow-trend-up"></i> Save ₹${Number(phase.target_savings).toLocaleString('en-IN')}/mo</span>` : ''}
                    ${phase.milestone != null ? `<span class="roadmap-milestone"><i class="fa-solid fa-flag"></i> Reach ₹${Number(phase.milestone).toLocaleString('en-IN')}</span>` : ''}
                </div>
            </div>
            <div class="roadmap-phase-body">
                <ol class="roadmap-steps">
                    ${actions.map(s => `<li>${escapeHtml(s)}</li>`).join('')}
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

/* ── Collapsible quarterly group (long-timeline UX; never alters
   the underlying monthly phases — just groups their display) ─── */
function _renderPhaseGroup(group, allPhases, groupIndex) {
    const groupId = `phaseGroup${groupIndex}`;
    const monthPhases = group.phase_indexes.map(i => allPhases[i]).filter(Boolean);
    return `
    <div class="roadmap-phase-group">
        <button class="roadmap-phase-group-toggle" onclick="_togglePhaseGroup('${groupId}')">
            <span><i class="fa-solid fa-layer-group"></i> ${escapeHtml(group.label)} — ${escapeHtml(group.months.join(', '))}</span>
            <span>Save ₹${Number(group.target_savings_total).toLocaleString('en-IN')} total · Milestone ₹${Number(group.milestone).toLocaleString('en-IN')}</span>
        </button>
        <div id="${groupId}" class="roadmap-phase-group-body hidden">
            ${monthPhases.map((phase, i) => _renderPhaseCard(phase, group.phase_indexes[i], allPhases.length)).join('')}
        </div>
    </div>`;
}

function _togglePhaseGroup(groupId) {
    document.getElementById(groupId)?.classList.toggle('hidden');
}

/* ── Optional, user-triggered AI explanation (never called automatically) ──
   NOTE: presentation-only. No API/schema/prompt changes — this only
   changes how the same `summary`/`why`/`priority`/`guidance`/`cached`/
   `available` fields already returned by /explain-roadmap-ai are rendered. */

function _injectAiGuidanceStyles() {
    if (document.getElementById('aiGuidanceStyles')) return;
    const style = document.createElement('style');
    style.id = 'aiGuidanceStyles';
    style.textContent = `
        .ai-guidance-panel {
            margin-top: 4px;
            border: 1px solid rgba(139, 92, 246, 0.22);
            border-radius: 12px;
            background: linear-gradient(180deg, rgba(139, 92, 246, 0.06) 0%, rgba(139, 92, 246, 0.02) 100%);
            box-shadow: 0 1px 3px rgba(0,0,0,0.16);
            overflow: hidden;
            opacity: 0;
            transform: translateY(4px);
            transition: opacity .25s ease, transform .25s ease;
        }
        .ai-guidance-panel.ai-guidance-visible { opacity: 1; transform: translateY(0); }
        .ai-guidance-header {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 14px 18px;
            border-bottom: 1px solid rgba(139, 92, 246, 0.16);
        }
        .ai-guidance-header-icon {
            width: 30px; height: 30px;
            border-radius: 8px;
            display: flex; align-items: center; justify-content: center;
            background: rgba(139, 92, 246, 0.16);
            color: #a78bfa;
            flex-shrink: 0;
            font-size: .85rem;
        }
        .ai-guidance-header-text h3 {
            margin: 0; font-size: .95rem; font-weight: 700;
            color: var(--text-primary, #e5e7eb);
        }
        .ai-guidance-header-text p {
            margin: 1px 0 0; font-size: .75rem;
            color: var(--text-tertiary, #9ca3af);
        }
        .ai-guidance-cached-tag {
            margin-left: auto;
            font-size: .68rem;
            color: var(--text-tertiary, #9ca3af);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 20px;
            padding: 2px 9px;
            white-space: nowrap;
        }
        .ai-guidance-body {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 0;
        }
        @media (max-width: 720px) {
            .ai-guidance-body { grid-template-columns: 1fr; }
        }
        .ai-guidance-col { padding: 18px; }
        .ai-guidance-col + .ai-guidance-col {
            border-left: 1px solid rgba(139, 92, 246, 0.14);
        }
        @media (max-width: 720px) {
            .ai-guidance-col + .ai-guidance-col {
                border-left: none;
                border-top: 1px solid rgba(139, 92, 246, 0.14);
            }
        }
        .ai-guidance-summary {
            font-size: 1.02rem;
            line-height: 1.55;
            color: var(--text-primary, #e5e7eb);
            font-weight: 500;
            margin: 0 0 18px;
        }
        .ai-guidance-subhead {
            font-size: .68rem;
            font-weight: 700;
            letter-spacing: .06em;
            text-transform: uppercase;
            color: #a78bfa;
            margin: 0 0 6px;
        }
        .ai-guidance-why {
            font-size: .88rem;
            line-height: 1.6;
            color: var(--text-secondary, #cbd5e1);
            margin: 0;
        }
        .ai-guidance-priority-box {
            border: 1px solid rgba(139, 92, 246, 0.30);
            background: rgba(139, 92, 246, 0.09);
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 18px;
        }
        .ai-guidance-priority-box p {
            margin: 4px 0 0;
            font-size: .9rem;
            font-weight: 600;
            color: var(--text-primary, #e5e7eb);
            line-height: 1.5;
        }
        .ai-guidance-actions { display: flex; flex-direction: column; gap: 2px; }
        .ai-guidance-action-row {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            padding: 9px 8px;
            border-radius: 7px;
            transition: background .15s ease;
        }
        .ai-guidance-action-row:hover,
        .ai-guidance-action-row:focus-within {
            background: rgba(139, 92, 246, 0.08);
        }
        .ai-guidance-action-num {
            font-size: .72rem;
            font-weight: 700;
            color: #a78bfa;
            width: 20px;
            flex-shrink: 0;
            padding-top: 1px;
        }
        .ai-guidance-action-text {
            font-size: .85rem;
            line-height: 1.5;
            color: var(--text-secondary, #cbd5e1);
        }
        .ai-guidance-error {
            padding: 16px 18px;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .ai-guidance-error-icon {
            font-size: 1rem;
            color: var(--text-tertiary, #9ca3af);
            flex-shrink: 0;
        }
        .ai-guidance-error-text strong {
            display: block;
            font-size: .88rem;
            color: var(--text-primary, #e5e7eb);
            margin-bottom: 2px;
        }
        .ai-guidance-error-text span {
            font-size: .78rem;
            color: var(--text-tertiary, #9ca3af);
        }
        .ai-guidance-retry-btn {
            margin-left: auto;
            font-size: .8rem;
            font-weight: 600;
            color: #a78bfa;
            background: none;
            border: 1px solid rgba(139, 92, 246, 0.35);
            border-radius: 7px;
            padding: 6px 12px;
            cursor: pointer;
            white-space: nowrap;
            transition: background .15s ease;
        }
        .ai-guidance-retry-btn:hover,
        .ai-guidance-retry-btn:focus-visible {
            background: rgba(139, 92, 246, 0.12);
            outline: none;
        }
    `;
    document.head.appendChild(style);
}

function _renderAiGuidancePanel(data) {
    const cachedTag = data.cached
        ? `<span class="ai-guidance-cached-tag">Previously generated</span>`
        : '';

    const priorityBlock = data.priority ? `
        <div class="ai-guidance-priority-box">
            <p class="ai-guidance-subhead" style="margin-bottom:0;">Priority</p>
            <p>${escapeHtml(data.priority)}</p>
        </div>` : '';

    const actionsBlock = data.guidance?.length ? `
        <p class="ai-guidance-subhead">Recommended Actions</p>
        <div class="ai-guidance-actions">
            ${data.guidance.map((g, i) => `
                <div class="ai-guidance-action-row" tabindex="0">
                    <span class="ai-guidance-action-num">${String(i + 1).padStart(2, '0')}</span>
                    <span class="ai-guidance-action-text">${escapeHtml(g)}</span>
                </div>`).join('')}
        </div>` : '';

    return `
    <div class="ai-guidance-panel" role="region" aria-label="AI Guidance">
        <div class="ai-guidance-header">
            <div class="ai-guidance-header-icon"><i class="fa-solid fa-wand-magic-sparkles" aria-hidden="true"></i></div>
            <div class="ai-guidance-header-text">
                <h3>AI Guidance</h3>
                <p>Personalized interpretation of your roadmap</p>
            </div>
            ${cachedTag}
        </div>
        <div class="ai-guidance-body">
            <div class="ai-guidance-col">
                ${data.summary ? `<p class="ai-guidance-summary">${escapeHtml(data.summary)}</p>` : ''}
                ${data.why ? `
                    <h4 class="ai-guidance-subhead">Why This Matters</h4>
                    <p class="ai-guidance-why">${escapeHtml(data.why)}</p>` : ''}
            </div>
            <div class="ai-guidance-col">
                ${priorityBlock}
                ${actionsBlock}
            </div>
        </div>
    </div>`;
}

function _renderAiGuidanceError(goalId, message) {
    return `
    <div class="ai-guidance-panel" role="region" aria-label="AI Guidance unavailable">
        <div class="ai-guidance-error">
            <i class="fa-solid fa-circle-info ai-guidance-error-icon" aria-hidden="true"></i>
            <div class="ai-guidance-error-text">
                <strong>AI guidance temporarily unavailable</strong>
                <span>The deterministic roadmap is still available above.</span>
            </div>
            <button type="button" class="ai-guidance-retry-btn" onclick="explainRoadmapWithAI(${goalId})">
                Try Again
            </button>
        </div>
    </div>`;
}

async function explainRoadmapWithAI(goalId) {
    const btn = document.getElementById('aiExplainBtn');
    const body = document.getElementById('aiExplanationBody');
    if (!body) return;

    _injectAiGuidanceStyles();

    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> Asking AI…'; }
    body.setAttribute('aria-live', 'polite');
    body.innerHTML = '';

    try {
        const res = await authFetch('/explain-roadmap-ai', {
            method: 'POST',
            body: JSON.stringify({ goal_id: goalId }),
        });
        const data = res ? await res.json() : null;

        if (!data || data.available === false) {
            body.innerHTML = _renderAiGuidanceError(goalId, data?.message);
        } else {
            body.innerHTML = _renderAiGuidancePanel(data);
        }

        // Subtle fade/slide-in once the panel is in the DOM.
        requestAnimationFrame(() => {
            body.querySelector('.ai-guidance-panel')?.classList.add('ai-guidance-visible');
        });
    } catch (e) {
        body.innerHTML = _renderAiGuidanceError(goalId);
        requestAnimationFrame(() => {
            body.querySelector('.ai-guidance-panel')?.classList.add('ai-guidance-visible');
        });
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles" aria-hidden="true"></i> Explain with AI'; }
    }
}

/* ── Full-view modal ─────────────────────────────────────────── */
function openRoadmapModal() {
    if (!_activeRoadmap || !_activeRoadmap.roadmap) return;
    const { goal, roadmap } = _activeRoadmap;

    const modal = document.getElementById('roadmapModal');
    const title = document.getElementById('roadmapModalTitle');
    const sub = document.getElementById('roadmapModalSubtitle');
    const body = document.getElementById('roadmapModalBody');
    if (!modal || !body) return;

    title.textContent = goal.name + ' — Full Roadmap';
    sub.textContent = `${roadmap.phases?.length ?? 0} phases · ${roadmap.strategy} strategy`;

    const phases = roadmap.phases ?? [];
    body.innerHTML = `
        <div class="roadmap-modal-summary">${escapeHtml(roadmap.summary ?? '')}</div>
        <div class="roadmap-modal-timeline">
            ${phases.map((phase, i) => `
            <div class="roadmap-modal-phase" style="animation-delay:${Math.min(i, 20) * 0.07}s">
                <div class="roadmap-modal-phase-left">
                    <div class="roadmap-modal-phase-circle">
                        <span>🎯</span>
                    </div>
                    ${i < phases.length - 1 ? '<div class="roadmap-modal-line"></div>' : ''}
                </div>
                <div class="roadmap-modal-phase-content">
                    <div class="roadmap-modal-phase-header">
                        <span class="roadmap-phase-num">Phase ${i + 1}</span>
                        <strong>${escapeHtml(phase.title ?? '')}</strong>
                    </div>
                    ${phase.milestone != null ? `
                    <div class="roadmap-modal-milestone">
                        <i class="fa-solid fa-flag"></i> Milestone: ₹${Number(phase.milestone).toLocaleString('en-IN')}
                        ${phase.target_savings ? ` · Save ₹${Number(phase.target_savings).toLocaleString('en-IN')}/mo` : ''}
                    </div>` : ''}
                    <ol class="roadmap-modal-steps">
                        ${(phase.actions ?? []).map(s => `<li>${escapeHtml(s)}</li>`).join('')}
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
    document.getElementById('roadmapModal')?.addEventListener('click', function (e) {
        if (e.target === this) closeRoadmapModal();
    });
});