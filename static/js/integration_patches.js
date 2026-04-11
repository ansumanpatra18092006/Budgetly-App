/* ================================================================
   integration_patches.js
   
   These are SMALL ADDITIONS to existing files.
   Do NOT replace your existing files — copy each snippet into
   the indicated location.
================================================================ */


/* ──────────────────────────────────────────────────────────────
   PATCH 1: dashboard.js
   Add these two lines inside loadDashboard(), after the existing
   Promise.all([...]) call and before loadAllAIFeatures().
   ──────────────────────────────────────────────────────────────
   
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
       
       // ── ADD THESE TWO LINES ──
       if (typeof initWallet === 'function') await initWallet();
       checkLedgerIntegrity();                                   
       // ────────────────────────
       
       await loadAllAIFeatures();
   }
   
   ──────────────────────────────────────────────────────────────
   PATCH 2: navigation.js  showPage()
   Add 'wallet' case to the switch:
   ──────────────────────────────────────────────────────────────
   
   case 'wallet':
       initWallet();
       loadWalletHistory();
       break;
   
   ──────────────────────────────────────────────────────────────
   PATCH 3: index.html  — Add to <head>
   ──────────────────────────────────────────────────────────────
   
   <link rel="stylesheet" href="/static/css/payment.css">
   
   ──────────────────────────────────────────────────────────────
   PATCH 4: index.html  — Add before closing </body>
   ──────────────────────────────────────────────────────────────
   
   <!-- Payment modals (copy content of templates/payment_modals.html) -->
   <!-- Wallet widget (copy content of templates/wallet_widget.html)    -->
   
   <script src="/static/js/wallet.js"></script>
   
   ──────────────────────────────────────────────────────────────
   PATCH 5: index.html — Add wallet widget card to dashboard section
   ──────────────────────────────────────────────────────────────
   
   Inside <div id="dashboard" class="page active">, after .stats-grid div,
   paste the contents of templates/wallet_widget.html.
   
   ──────────────────────────────────────────────────────────────
   PATCH 6: index.html — Add date field to add-transaction form
   ──────────────────────────────────────────────────────────────
   
   <div class="form-group">
       <label class="form-label" for="txDate">Date</label>
       <input id="txDate" type="date" class="form-input"
              value="">  <!-- JS sets today on load -->
   </div>
   
   And in initApp() or a DOMContentLoaded:
       const txDate = document.getElementById('txDate');
       if (txDate) txDate.value = new Date().toISOString().slice(0,10);

================================================================ */


/* ================================================================
   STANDALONE HELPERS
   Add these to app.js or a new file loaded after wallet.js
================================================================ */

/* Wallet history modal open/close */
function openWalletHistoryPanel() {
    document.getElementById('walletHistoryModal')?.classList.remove('hidden');
    loadWalletHistory();
}

function closeWalletHistoryPanel() {
    document.getElementById('walletHistoryModal')?.classList.add('hidden');
}

/* Ledger integrity check — called on dashboard load */
async function checkLedgerIntegrity() {
    const badge = document.getElementById('ledgerBadge');
    try {
        const res  = await authFetch('/ledger/verify');
        if (!res) return;
        const data = await res.json();

        if (badge) {
            if (data.valid) {
                badge.innerHTML  = `<i class="fa-solid fa-shield-check"></i> Ledger OK (${data.total})`;
                badge.style.background = 'rgba(16,185,129,0.18)';
                badge.style.color      = '#10b981';
            } else {
                badge.innerHTML  = `<i class="fa-solid fa-triangle-exclamation"></i> Chain Broken`;
                badge.style.background = 'rgba(239,68,68,0.18)';
                badge.style.color      = '#ef4444';
            }
        }
    } catch (_) {
        if (badge) badge.textContent = '— Ledger';
    }
}