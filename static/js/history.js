'use strict';

/* ================================================================
   HISTORY / TRANSACTION LIST
================================================================ */
async function loadHistory() {
    setListLoading('transactionsList');
    const res = await authFetch('/get-transactions');
    if (!res) { clearListLoading('transactionsList'); return; }

    try {
        const json = await res.json();
        const data = json.data ?? json;
        renderTransactionsList(data.transactions ?? []);
    } catch (e) {
        console.error('loadHistory parse error', e);
        renderTransactionsList([]);
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
    const start    = document.getElementById('filterStart').value;
    const end      = document.getElementById('filterEnd').value;
    const type     = document.getElementById('filterType').value;
    const category = document.getElementById('filterCategory').value;
    const search   = document.getElementById('filterSearch').value.trim();

    const params = new URLSearchParams();
    if (start)                          params.append('start',    start);
    if (end)                            params.append('end',      end);
    if (type     && type     !== 'All') params.append('type',     type);
    if (category && category !== 'All') params.append('category', category);
    if (search)                         params.append('search',   search);

    setListLoading('transactionsList');

    try {
        const res = await authFetch(`/get-transactions?${params.toString()}`);
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
    document.getElementById('filterStart').value    = '';
    document.getElementById('filterEnd').value      = '';
    document.getElementById('filterType').value     = 'All';
    document.getElementById('filterCategory').value = 'All';
    document.getElementById('filterSearch').value   = '';
    loadHistory();
    showNotification('Filters cleared', 'success');
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
            method:      'POST',
            credentials: 'include',
            body:        formData,
        });

        if (res && res.ok) {
            showNotification('CSV imported successfully', 'success');
            loadHistory();
            loadDashboard();
        } else {
            showNotification('Import failed. Check file format.', 'error');
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