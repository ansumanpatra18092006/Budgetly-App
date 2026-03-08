// ================================================================
//  Budgetly AI Chat Widget
//  Connects to the same /chat Flask endpoint — no backend changes.
// ================================================================

document.addEventListener("DOMContentLoaded", () => {

const chatFab       = document.getElementById("chatToggle");
const chatPanel     = document.getElementById("chatContainer");
const chatCloseBtn  = document.getElementById("chatClose");
const chatInput     = document.getElementById("chatInput");
const chatSendBtn   = document.getElementById("chatSendBtn");
const chatMessages  = document.getElementById("chatMessages");

// ── Toggle open / close ──────────────────────────────────────────
function openChat() {
    chatPanel.classList.remove("hidden");
    chatFab.classList.add("open");
    chatInput.focus();
}

function closeChat() {
    chatPanel.classList.add("hidden");
    chatFab.classList.remove("open");
}

chatFab.addEventListener("click", () => {
    chatPanel.classList.contains("hidden") ? openChat() : closeChat();
});

chatCloseBtn.addEventListener("click", closeChat);

// ── Suggestion chips ─────────────────────────────────────────────
document.querySelectorAll(".chat-suggestion-chip").forEach(chip => {
    chip.addEventListener("click", () => {
        const msg = chip.dataset.msg;
        if (msg) sendMessage(msg);
    });
});

// ── Send on Enter or button click ────────────────────────────────
chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const text = chatInput.value.trim();
        if (text) sendMessage(text);
    }
});

chatSendBtn.addEventListener("click", () => {
    const text = chatInput.value.trim();
    if (text) sendMessage(text);
});

// ── Core send function ───────────────────────────────────────────
async function sendMessage(text) {
    chatInput.value = "";
    chatSendBtn.disabled = true;

    // Hide welcome card on first message
    const welcome = chatMessages.querySelector(".chat-welcome");
    if (welcome) welcome.style.display = "none";

    addBubble("you", text);
    const typingEl = showTyping();

    try {
        const res = await fetch("/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ message: text })
        });

        const data = await res.json();
        removeTyping(typingEl);
        addBubble("ai", data.reply || "Sorry, I couldn't understand that.");
    } catch (_) {
        removeTyping(typingEl);
        addBubble("ai", "Connection error. Please try again.");
    } finally {
        chatSendBtn.disabled = false;
        chatInput.focus();
    }
}

// ── DOM helpers ──────────────────────────────────────────────────
function addBubble(role, text) {
    const wrap = document.createElement("div");
    wrap.className = `chat-msg chat-msg-${role}`;

    const sender = document.createElement("span");
    sender.className = "chat-msg-sender";
    sender.textContent = role === "you" ? "You" : "Budgetly AI";

    const bubble = document.createElement("div");
    bubble.className = "chat-msg-bubble";
    bubble.textContent = text;

    wrap.appendChild(sender);
    wrap.appendChild(bubble);
    chatMessages.appendChild(wrap);
    scrollToBottom();
}

function showTyping() {
    const el = document.createElement("div");
    el.className = "chat-typing";
    el.innerHTML = `
        <div class="chat-typing-dot"></div>
        <div class="chat-typing-dot"></div>
        <div class="chat-typing-dot"></div>
    `;
    chatMessages.appendChild(el);
    scrollToBottom();
    return el;
}

function removeTyping(el) {
    if (el && el.parentNode) el.parentNode.removeChild(el);
}

function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

}); // end DOMContentLoaded