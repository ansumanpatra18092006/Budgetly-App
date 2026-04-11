'use strict';
/* ================================================================
   wallet.js  — Budgetly Wallet Module
   Handles: balance display, top-up modal, send modal, history
================================================================ */

/* ── State ─────────────────────────────────────────────────────── */
let _walletBalance = 0;

/* ================================================================
   BALANCE
================================================================ */
async function loadWalletBalance() {
    try {
        const res = await authFetch('/wallet/balance');
        if (!res || !res.ok) return;
        const data = await res.json();
        _walletBalance = data.balance ?? 0;
        _renderWalletBalance();
    } catch (e) {
        console.error('loadWalletBalance', e);
    }
}

function getWalletBalance() { return _walletBalance; }

function _renderWalletBalance() {
    document.querySelectorAll('.wallet-balance-display').forEach(el => {
        el.textContent = `₹${_walletBalance.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    });
    // Color cue
    document.querySelectorAll('.wallet-balance-chip').forEach(el => {
        el.classList.toggle('wallet-low', _walletBalance < 500);
        el.classList.toggle('wallet-ok', _walletBalance >= 500);
    });
}

/* ================================================================
   TOP-UP MODAL
================================================================ */
function openTopupModal() {
    const modal = document.getElementById('walletTopupModal');
    if (!modal) return;
    document.getElementById('topupAmount').value = '';
    modal.classList.remove('hidden');
    setTimeout(() => document.getElementById('topupAmount').focus(), 80);
}

function closeTopupModal() {
    document.getElementById('walletTopupModal')?.classList.add('hidden');
}

async function submitTopup() {
    const input = document.getElementById('topupAmount');
    const amount = parseFloat(input?.value || 0);

    if (!amount || amount <= 0) {
        showNotification('Enter a valid amount', 'error');
        return;
    }
    if (amount > 100000) {
        showNotification('Maximum top-up is ₹1,00,000', 'error');
        return;
    }

    const btn = document.getElementById('topupSubmitBtn');
    setButtonLoading(btn, 'Adding…');

    try {
        const res = await authFetch('/wallet/topup', {
            method: 'POST',
            body: JSON.stringify({ amount }),
        });
        const data = await res.json();

        if (res.ok && data.success) {
            showNotification(data.message || 'Wallet topped up!', 'success');
            closeTopupModal();
            await loadWalletBalance();
            // Refresh dashboard totals since a top-up creates an income tx
            if (typeof loadDashboard === 'function') loadDashboard();
        } else {
            showNotification(data.message || 'Top-up failed', 'error');
        }
    } catch (e) {
        showNotification('Network error', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-plus"></i> Add Money');
    }
}

/* ================================================================
   SEND MODAL
================================================================ */
function openSendModal() {
    const modal = document.getElementById('walletSendModal');
    if (!modal) return;
    document.getElementById('sendReceiverEmail').value = '';
    document.getElementById('sendAmount').value = '';
    document.getElementById('sendNote').value = '';
    modal.classList.remove('hidden');
    setTimeout(() => document.getElementById('sendReceiverEmail').focus(), 80);
}

function closeSendModal() {
    document.getElementById('walletSendModal')?.classList.add('hidden');
}

async function submitSend() {
    const receiverEmail = (document.getElementById('sendReceiverEmail')?.value || '').trim();
    const amount = parseFloat(document.getElementById('sendAmount')?.value || 0);
    const note = (document.getElementById('sendNote')?.value || '').trim() || 'Wallet Transfer';

    if (!receiverEmail) { showNotification('Enter receiver email', 'error'); return; }
    if (!amount || amount <= 0) { showNotification('Enter a valid amount', 'error'); return; }
    if (amount > _walletBalance) {
        showNotification(`Insufficient balance (₹${_walletBalance.toLocaleString('en-IN')})`, 'error');
        return;
    }

    const btn = document.getElementById('sendSubmitBtn');
    setButtonLoading(btn, 'Sending…');

    try {
        const res = await authFetch('/wallet/send', {
            method: 'POST',
            body: JSON.stringify({ receiver_email: receiverEmail, amount, note }),
        });
        const data = await res.json();

        if (res.ok && data.success) {
            showNotification(data.message, 'success');
            closeSendModal();
            await loadWalletBalance();
            if (typeof loadHistory === 'function') loadHistory();
        } else {
            showNotification(data.message || 'Transfer failed', 'error');
        }
    } catch (e) {
        showNotification('Network error', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-paper-plane"></i> Send');
    }
}

/* ================================================================
   WALLET HISTORY PANEL  (inside wallet page / modal)
================================================================ */
async function loadWalletHistory() {
    const container = document.getElementById('walletHistoryList');
    if (!container) return;

    container.innerHTML = `<div class="wallet-history-loading">
        <i class="fa-solid fa-spinner fa-spin"></i> Loading…</div>`;

    try {
        const res = await authFetch('/wallet/history');
        const data = await res.json();
        _renderWalletHistory(data.history || []);
    } catch (e) {
        container.innerHTML = '<p style="color:var(--text-tertiary)">Could not load history.</p>';
    }
}

function _renderWalletHistory(items) {
    const container = document.getElementById('walletHistoryList');
    if (!container) return;

    if (!items.length) {
        container.innerHTML = `<div class="empty-state">
            <i class="fa-solid fa-wallet"></i>
            <p>No wallet transactions yet.</p></div>`;
        return;
    }

    const userId = parseInt(document.getElementById('profileId')?.value || 0);

    container.innerHTML = items.map(item => {
        const isSend = item.sender_id === userId;
        const label = isSend
            ? `Sent to ${escapeHtml(item.receiver_name || 'User')}`
            : item.sender_id === null
                ? 'Top-up'
                : `Received from ${escapeHtml(item.sender_name || 'User')}`;
        const sign = isSend ? '-' : '+';
        const colorCls = isSend ? 'tx-expense' : 'tx-income';
        const date = item.created_at ? item.created_at.slice(0, 10) : '';

        return `<li class="wallet-history-item">
            <div class="tx-info">
                <div class="tx-desc">${label}</div>
                <div class="tx-meta">${escapeHtml(item.note || '')} &bull; ${date}</div>
            </div>
            <span class="tx-amount ${colorCls}">
                ${sign}₹${Number(item.amount).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </span>
        </li>`;
    }).join('');
}

/* ================================================================
   BOOTSTRAP  — called from dashboard load or page switch
================================================================ */
async function initWallet() {
    await loadWalletBalance();
}

function openTopupModal() {
    document.getElementById("walletTopupModal")?.classList.remove("hidden");
}

function openSendModal() {
    document.getElementById("walletSendModal")?.classList.remove("hidden");
}

function openWalletHistoryPanel() {
    document.getElementById("walletHistoryModal")?.classList.remove("hidden");
}

function closeWalletHistoryPanel() {
    document.getElementById("walletHistoryModal")?.classList.add("hidden");
}