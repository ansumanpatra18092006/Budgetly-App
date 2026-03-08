'use strict';

/* ================================================================
   ADD TRANSACTION
================================================================ */
async function submitTransaction(e) {
    e.preventDefault();
    const btn = document.getElementById('submitTxBtn');
    if (btn.disabled) return;

    setButtonLoading(btn, 'Saving…');

    const payload = {
        description: document.getElementById('description').value.trim(),
        amount:      document.getElementById('amount').value,
        type:        document.getElementById('transactionType').value,
        category:    document.getElementById('category').value,
        notes:       document.getElementById('notes').value.trim(),
    };

    try {
        const res = await authFetch('/add-transaction', {
            method: 'POST',
            body:   JSON.stringify(payload),
        });

        if (res && res.ok) {
            e.target.reset();
            showNotification('Transaction added successfully', 'success');
            loadDashboard();
            setTimeout(() => showPage('history'), 400);
        } else {
            showNotification('Failed to add transaction', 'error');
        }
    } catch (err) {
        showNotification('Error saving transaction', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-plus" aria-hidden="true"></i> Add Transaction');
    }
}

function resetTransactionForm(event) {
    event.preventDefault();
    document.getElementById('description').value = '';
    document.getElementById('amount').value      = '';
    document.getElementById('notes').value       = '';
    const typeEl = document.getElementById('transactionType');
    const catEl  = document.getElementById('category');
    if (typeEl) typeEl.selectedIndex = 0;
    if (catEl)  catEl.selectedIndex  = 0;
}

/* ================================================================
   EDIT MODAL
================================================================ */
function openEditModal(id, description, amount, category, type, date) {
    document.getElementById('editId').value          = id;
    document.getElementById('editDescription').value = description;
    document.getElementById('editAmount').value      = amount;
    document.getElementById('editType').value        = type;
    document.getElementById('editCategory').value    = category;
    document.getElementById('editDate').value        = date;
    document.getElementById('editModal').classList.remove('hidden');
}

function closeEditModal() {
    document.getElementById('editModal').classList.add('hidden');
}

async function saveEdit() {
    const btn = document.getElementById('saveEditBtn');
    if (btn.disabled) return;

    const id = document.getElementById('editId').value;
    const payload = {
        description: document.getElementById('editDescription').value.trim(),
        amount:      parseFloat(document.getElementById('editAmount').value),
        type:        document.getElementById('editType').value,
        category:    document.getElementById('editCategory').value,
        date:        document.getElementById('editDate').value,
    };

    setButtonLoading(btn, 'Saving…');

    try {
        const res = await authFetch(`/update-transaction/${id}`, {
            method: 'PUT',
            body:   JSON.stringify(payload),
        });

        if (res && res.ok) {
            showNotification('Transaction updated', 'success');
            closeEditModal();
            loadHistory();
            loadDashboard();
        } else {
            showNotification('Update failed', 'error');
        }
    } catch (err) {
        showNotification('Update failed', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-floppy-disk" aria-hidden="true"></i> Save Changes');
    }
}