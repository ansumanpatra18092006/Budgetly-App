'use strict';

/* ================================================================
   HISTORY / TRANSACTION LIST
================================================================ */
async function loadHistory() {
    setListLoading('transactionsList');
    const res = await authFetch(`/get-transactions?_ts=${Date.now()}`, {
        cache: 'no-store',
    });
    if (!res) { clearListLoading('transactionsList'); return; }

    try {
        const json = await res.json();

        if (!res.ok) {
            console.error('loadHistory server error:', json);
            showNotification(json.error || 'Failed to load transactions', 'error');
            renderTransactionsList([]);
            return;
        }

        // Support both current and wrapped API response shapes.
        const transactions =
            Array.isArray(json?.transactions) ? json.transactions :
            Array.isArray(json?.data?.transactions) ? json.data.transactions :
            Array.isArray(json) ? json : [];

        console.log(`[HISTORY] loaded ${transactions.length} transactions`);
        renderTransactionsList(transactions);
    } catch (e) {
        console.error('loadHistory parse error', e);
        renderTransactionsList([]);
        showNotification('Could not read transaction history', 'error');
    }
}

function renderTransactionsList(transactions) {
    const list = document.getElementById('transactionsList');
    if (!list) return;

    if (!transactions || transactions.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <i class="fa-solid fa-inbox" aria-hidden="true"></i>
                <p>No transactions found.</p>
            </div>`;
        return;
    }

    list.innerHTML = transactions.map(t => `
        <li class="transaction-item">
            <div class="tx-info">
                <div class="tx-desc">${escapeHtml(t.description)}</div>
                <div class="tx-meta">${escapeHtml(t.category ?? '')} &bull; ${t.date ?? ''}</div>
            </div>
            <div class="tx-actions">
                <span class="tx-amount ${t.type}">
                    ${t.type === 'income' ? '+' : '-'}₹${Number(t.amount).toFixed(2)}
                </span>
                <button class="btn-icon" title="Edit"
                    onclick="openEditModal(${t.id},'${escapeJs(t.description)}',${t.amount},'${escapeJs(t.category)}','${t.type}','${t.date}')">
                    <i class="fa-solid fa-pencil" aria-hidden="true"></i>
                </button>
                ${t.type === 'expense' ? `
                <button class="btn-icon ${t.fraudshield_reported ? 'fraudshield-reported' : ''}"
                    title="${t.fraudshield_reported ? 'Reported to FraudShield' : 'Report suspicious activity'}"
                    onclick="${t.fraudshield_reported ? '' : `reportTransactionToFraudShield(${t.id})`}"
                    ${t.fraudshield_reported ? 'disabled' : ''}>
                    <i class="fa-solid ${t.fraudshield_reported ? 'fa-shield-check' : 'fa-shield-halved'}" aria-hidden="true"></i>
                </button>` : ''}
                <button class="btn-icon danger" title="Delete" onclick="deleteTransaction(${t.id})">
                    <i class="fa-solid fa-trash" aria-hidden="true"></i>
                </button>
            </div>
        </li>
    `).join('');
}

/* ================================================================
   FILTERS
================================================================ */
function setupSearchDebounce() {
    const searchInput = document.getElementById('filterSearch');
    if (!searchInput) return;
    searchInput.addEventListener('input', () => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(applyFilters, 350);
    });
}

async function applyFilters() {
    const start = document.getElementById('filterStart').value;
    const end = document.getElementById('filterEnd').value;
    const type = document.getElementById('filterType').value;
    const category = document.getElementById('filterCategory').value;
    const search = document.getElementById('filterSearch').value.trim();

    const params = new URLSearchParams();
    if (start) params.append('start', start);
    if (end) params.append('end', end);
    if (type && type !== 'All') params.append('type', type);
    if (category && category !== 'All') params.append('category', category);
    if (search) params.append('search', search);

    setListLoading('transactionsList');

    try {
        const res = await authFetch(`/get-transactions?${params.toString()}${params.toString() ? '&' : ''}_ts=${Date.now()}`, { cache: 'no-store' });
        if (!res) return;
        const json = await res.json();
        const data = json.data ?? json;
        renderTransactionsList(data.transactions ?? []);
    } catch (err) {
        console.error('Filter error:', err);
        showNotification('Failed to apply filters', 'error');
    }
}

function resetFilters() {
    document.getElementById('filterStart').value = '';
    document.getElementById('filterEnd').value = '';
    document.getElementById('filterType').value = 'All';
    document.getElementById('filterCategory').value = 'All';
    document.getElementById('filterSearch').value = '';
    loadHistory();
    showNotification('Filters cleared', 'success');
}

async function reportTransactionToFraudShield(id) {
    if (!confirm('Report this transaction to FraudShield? Only this transaction’s relevant security details will be shared with your connected institution.')) return;
    try {
        const res = await authFetch(`/report-suspicious-to-fraudshield/${id}`, {
            method: 'POST',
            body: JSON.stringify({note: 'User reported this transaction as suspicious.'})
        });
        const data = await res?.json().catch(() => ({}));
        if (!res || !res.ok || !data.success) throw new Error(data.error || 'Report failed');
        showNotification(data.message || 'Reported to FraudShield', 'success');
        loadHistory();
    } catch (err) {
        console.error('FraudShield report failed:', err);
        showNotification(err.message || 'Could not report transaction', 'error');
    }
}

/* ================================================================
   DELETE / CLEAR
================================================================ */
async function deleteTransaction(id) {
    if (!confirm('Are you sure you want to delete this transaction?')) return;

    const res = await authFetch(`/delete-transaction/${id}`, { method: 'DELETE' });
    if (res && res.ok) {
        showNotification('Transaction deleted', 'success');
        if (document.getElementById('history').classList.contains('active')) {
            loadHistory();
        } else {
            loadDashboard();
        }
    } else {
        showNotification('Failed to delete transaction', 'error');
    }
}

async function clearAllTransactions() {
    if (!confirm('Delete ALL transactions? This cannot be undone.')) return;

    const res = await authFetch('/clear-all-transactions', { method: 'POST' });
    if (res && res.ok) {
        showNotification('All transactions cleared', 'success');
        loadHistory();
        loadDashboard();
    } else {
        showNotification('Failed to clear transactions', 'error');
    }
}

/* ================================================================
   CSV IMPORT / EXPORT
================================================================ */
async function importCSV(input) {
    if (!input.files || !input.files.length) {
        showNotification('Select a CSV file first', 'error');
        return;
    }

    const btn = document.getElementById('importCsvBtn');
    setButtonLoading(btn, 'Importing…');

    const formData = new FormData();
    formData.append('file', input.files[0]);

    try {
        const res = await fetch('/import-transactions', {
            method: 'POST',
            credentials: 'include',
            body: formData,
        });

        if (res && res.ok) {
            const data = await res.json().catch(() => ({}));
            const imported = Number(data.imported ?? 0);

            if (imported > 0) {
                showNotification(`${imported} transactions imported successfully`, 'success');
            } else if (Number(data.user_transaction_count ?? 0) > 0) {
                showNotification(
                    `CSV added 0 new rows; you already have ${data.user_transaction_count} transactions.`,
                    'warning'
                );
            } else {
                showNotification(
                    data.message || 'CSV was accepted, but no valid transactions were imported.',
                    'warning'
                );
            }

            await loadHistory();
            await loadDashboard();
        } else {
            let message = 'Import failed. Check file format.';
            try {
                const data = await res.json();
                if (data.error) message = data.error;
            } catch (_) {}
            showNotification(message, 'error');
        }
    } catch (err) {
        showNotification('Import failed', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-file-import" aria-hidden="true"></i> Import CSV');
        input.value = '';
    }
}

function exportCSV() {
    window.open('/export-transactions', '_blank');
}

async function uploadStatement(file) {
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch("/upload-statement", {
        method: "POST",
        body: formData,
        credentials: "include"
    });

    const data = await res.json();

    if (data.success) {
        showNotification(`${data.count} transactions imported`, "success");
        loadHistory();
    } else {
        showNotification("Import failed", "error");
    }
}