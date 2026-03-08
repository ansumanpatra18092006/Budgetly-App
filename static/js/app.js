/* ================================================================
   GLOBALS & STATE
================================================================ */
let categoryChartInstance = null;
let trendChartInstance    = null;
let currentSummary        = { income: 0, expense: 0, balance: 0 };
let searchDebounceTimer   = null;
let _isBudgetSaving       = false;

/* ================================================================
   INITIALIZATION
   Called from index.html after all scripts are loaded.
================================================================ */
function initApp() {
    initTheme();
    updateDate();
    loadDashboard();
    setupBudgetListener();
    setupSearchDebounce();
    setupKeyboardShortcuts();
    setupModalEscapeClose();
    syncProfileHeroOnLoad();
}

/* ================================================================
   AUTH FETCH
================================================================ */
async function authFetch(url, options = {}) {
    try {
        const res = await fetch(url, {
            credentials: 'include',
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        if (res.status === 401) {
            window.location.href = '/login';
            return null;
        }
        return res;
    } catch (err) {
        console.error('Network error for', url, err);
        showNotification('Network error. Please check your connection.', 'error');
        return null;
    }
}

async function handleLogout() {
    await fetch('/logout', { method: 'POST', credentials: 'include' });
    window.location.href = '/login';
}

/* ================================================================
   NOTIFICATIONS
================================================================ */
function showNotification(msg, type = 'success') {
    const icons = { success: 'fa-check-circle', error: 'fa-circle-exclamation', warning: 'fa-triangle-exclamation' };
    const div   = document.createElement('div');
    div.className = `notification ${type}`;
    div.innerHTML = `<i class="fa-solid ${icons[type] ?? icons.success}" aria-hidden="true"></i> ${escapeHtml(msg)}`;
    document.body.appendChild(div);
    setTimeout(() => {
        div.classList.add('hide');
        setTimeout(() => div.remove(), 350);
    }, 3000);
}

/* ================================================================
   HELPERS
================================================================ */
function animateValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = `₹${Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function updateDate() {
    const el = document.getElementById('currentDate');
    if (el) el.textContent = new Date().toLocaleDateString('en-US', { year:'numeric', month:'long', day:'numeric' });
}

function setEl(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function setWidth(id, pct) {
    const el = document.getElementById(id);
    if (el) el.style.width = Math.max(0, Math.min(100, pct)) + '%';
}

function setInput(id, val) {
    const el = document.getElementById(id);
    if (el) el.value = val;
}

function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g,'&amp;')
        .replace(/</g,'&lt;')
        .replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;')
        .replace(/'/g,'&#039;');
}

function escapeJs(str) {
    return String(str ?? '').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"');
}

function getHealthLabel(score) {
    if (score >= 80) return 'Excellent';
    if (score >= 60) return 'Good';
    if (score >= 40) return 'Average';
    return 'Poor';
}

function setButtonLoading(btn, label) {
    if (!btn) return;
    btn.disabled         = true;
    btn._originalHtml    = btn.innerHTML;
    btn.innerHTML        = `<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> ${label}`;
}

function resetButton(btn, html) {
    if (!btn) return;
    btn.disabled  = false;
    btn.innerHTML = html ?? btn._originalHtml ?? 'Submit';
}

function setListLoading(listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    list.innerHTML = `
        <div style="padding:var(--spacing-xl);display:flex;flex-direction:column;gap:var(--spacing-sm);">
            ${Array(4).fill('<div class="skeleton skeleton-line"></div>').join('')}
        </div>`;
}

function clearListLoading(listId) {
    const list = document.getElementById(listId);
    if (list) list.innerHTML = '';
}

/* ================================================================
   MODAL ESCAPE / BACKDROP CLOSE
================================================================ */
function setupModalEscapeClose() {
    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        if (!document.getElementById('goalProgressModal').classList.contains('hidden'))   closeProgressModal();
        if (!document.getElementById('goalPredictionModal').classList.contains('hidden')) closePredictionModal();
    });

    document.getElementById('goalProgressModal').addEventListener('click', function(e) {
        if (e.target === this) closeProgressModal();
    });
    document.getElementById('goalPredictionModal').addEventListener('click', function(e) {
        if (e.target === this) closePredictionModal();
    });
}

/* ================================================================
   PROFILE HERO SYNC (bootstrapped from app.js)
================================================================ */
function syncProfileHeroOnLoad() {
    const profileBtn = document.querySelector('[data-target="profile"]');
    if (profileBtn) profileBtn.addEventListener('click', () => setTimeout(() => pollUntilLoaded(10), 200));
    pollUntilLoaded(5);
}

function getInitials(name) {
    if (!name) return '?';
    return name.trim().split(/\s+/).slice(0, 2).map(w => w[0].toUpperCase()).join('');
}

function updateProfileHero() {
    const name  = (document.getElementById('profileName')  || {}).value || '';
    const email = (document.getElementById('profileEmail') || {}).value || '';
    const uid   = (document.getElementById('profileId')    || {}).value || '';

    const avatar    = document.getElementById('profileAvatar');
    const heroName  = document.getElementById('profileHeroName');
    const heroEmail = document.getElementById('profileHeroEmail');
    const idMeta    = document.getElementById('profileIdMeta');

    if (avatar)    avatar.textContent    = getInitials(name) || '?';
    if (heroName)  heroName.textContent  = name  || '\u2014';
    if (heroEmail) heroEmail.textContent = email || '\u2014';
    if (idMeta)    idMeta.textContent    = uid   || '\u2014';
}

function pollUntilLoaded(attempts) {
    updateProfileHero();
    const name = (document.getElementById('profileName') || {}).value;
    if (name || attempts <= 0) return;
    setTimeout(() => pollUntilLoaded(attempts - 1), 400);
}