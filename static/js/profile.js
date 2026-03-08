'use strict';

/* ================================================================
   PROFILE — LOAD
================================================================ */
async function loadUserProfile() {
    const res = await authFetch('/user-profile');
    if (!res || !res.ok) return;

    try {
        const json = await res.json();
        const data = json.data ?? json;
        setInput('profileName',  data.name  ?? '');
        setInput('profileEmail', data.email ?? '');
        setInput('profileId',    data.id    ?? '');
    } catch (e) {
        console.error('loadUserProfile parse error', e);
    }
}

/* ================================================================
   CHANGE PASSWORD MODAL
================================================================ */
function openChangePasswordModal() {
    document.getElementById('changePasswordModal').classList.remove('hidden');
    setTimeout(() => document.getElementById('currentPassword').focus(), 80);
}

function closeChangePasswordModal() {
    document.getElementById('changePasswordModal').classList.add('hidden');
    ['currentPassword','newPassword','confirmPassword'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
}

async function changePassword() {
    const current = (document.getElementById('currentPassword').value || '').trim();
    const newPass = (document.getElementById('newPassword').value     || '').trim();
    const confirm = (document.getElementById('confirmPassword').value || '').trim();

    if (!current || !newPass || !confirm) {
        showNotification('Please fill in all fields', 'error');
        return;
    }
    if (newPass.length < 6) {
        showNotification('New password must be at least 6 characters', 'error');
        return;
    }
    if (newPass !== confirm) {
        showNotification('Passwords do not match', 'error');
        return;
    }

    const btn = document.getElementById('changePwBtn');
    setButtonLoading(btn, 'Updating\u2026');

    try {
        const res = await authFetch('/change-password', {
            method: 'POST',
            body: JSON.stringify({ current_password: current, new_password: newPass }),
        });

        if (res && res.ok) {
            showNotification('Password updated successfully', 'success');
            closeChangePasswordModal();
        } else {
            let msg = 'Password update failed';
            try { const d = await res.json(); msg = d.message || msg; } catch (_) {}
            showNotification(msg, 'error');
        }
    } catch (err) {
        showNotification('Error updating password', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-lock" aria-hidden="true"></i> Update Password');
    }
}

/* ================================================================
   EDIT PROFILE MODAL
================================================================ */
function openEditProfileModal() {
    document.getElementById('editProfileName').value =
        document.getElementById('profileName').value;

    document.getElementById('editProfileEmail').value =
        document.getElementById('profileEmail').value;

    document.getElementById('editProfileModal').classList.remove('hidden');
}

function closeEditProfileModal() {
    document.getElementById('editProfileModal').classList.add('hidden');
}

async function saveProfileChanges() {
    const name  = document.getElementById('editProfileName').value.trim();
    const email = document.getElementById('editProfileEmail').value.trim();

    if (!name || !email) {
        showNotification('All fields are required', 'error');
        return;
    }

    const btn = document.getElementById('saveProfileBtn');
    setButtonLoading(btn, 'Saving...');

    try {
        const res = await authFetch('/update-profile', {
            method: 'PUT',
            body: JSON.stringify({ name, email })
        });

        if (res && res.ok) {
            showNotification('Profile updated successfully', 'success');
            closeEditProfileModal();
            loadUserProfile();
        } else {
            showNotification('Update failed', 'error');
        }
    } catch {
        showNotification('Error updating profile', 'error');
    } finally {
        resetButton(btn, '<i class="fa-solid fa-floppy-disk"></i> Save Changes');
    }
}