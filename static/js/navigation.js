'use strict';

/* ================================================================
   NAVIGATION
================================================================ */
function showPage(pageId) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

    const page   = document.getElementById(pageId);
    const navBtn = document.querySelector(`[data-target="${pageId}"]`);

    if (page)   page.classList.add('active');
    if (navBtn) navBtn.classList.add('active');

    switch (pageId) {
        case 'dashboard': loadDashboard();    break;
        case 'history':   loadHistory();      break;
        case 'goals':     loadGoals();        break;
        case 'roadmap': loadRoadmap(); break;
        case 'insights':  loadInsights();     break;
        case 'profile':   loadUserProfile();  break;
    }
}

/* Nav button wiring is done in the inline <script> in index.html
   after all JS files have been loaded. */

/* ================================================================
   KEYBOARD SHORTCUTS
================================================================ */
function setupKeyboardShortcuts() {
    const pageMap = {
    '1': 'dashboard',
    '2': 'add',
    '3': 'history',
    '4': 'goals',
    '5': 'insights',
    '6': 'roadmap',   // ← ADD THIS
};
    document.addEventListener('keydown', e => {
        if (!e.altKey) return;
        if (pageMap[e.key]) { e.preventDefault(); showPage(pageMap[e.key]); }
        if (e.key === 'd' || e.key === 'D') { e.preventDefault(); toggleTheme(); }
    });
}

/* ================================================================
   THEME
================================================================ */
function toggleTheme() {
    const html     = document.documentElement;
    const newTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme-preference', newTheme);
    const icon = document.querySelector('.theme-icon');
    if (icon) icon.className = newTheme === 'dark' ? 'fa-solid fa-sun theme-icon' : 'fa-solid fa-moon theme-icon';
}

function initTheme() {
    const saved = localStorage.getItem('theme-preference') || 'light';
    document.documentElement.setAttribute('data-theme', saved);
    const icon = document.querySelector('.theme-icon');
    if (icon && saved === 'dark') icon.className = 'fa-solid fa-sun theme-icon';
}