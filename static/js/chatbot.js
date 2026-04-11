document.addEventListener("DOMContentLoaded", () => {

    // ── ELEMENTS ─────────────────────────────
    const chatFab = document.getElementById("chatToggle");
    const chatPanel = document.getElementById("chatContainer");
    const chatCloseBtn = document.getElementById("chatClose");
    const chatInput = document.getElementById("chatInput");
    const chatSendBtn = document.getElementById("chatSendBtn");
    const chatMessages = document.getElementById("chatMessages");

    let controller = null;

    // ── TOGGLE ─────────────────────────────
    chatFab.onclick = () => {
        chatPanel.classList.toggle("hidden");
        chatInput.focus();
    };

    chatCloseBtn.onclick = () => chatPanel.classList.add("hidden");

    // ── INPUT ──────────────────────────────
    chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage(chatInput.value.trim());
        }
    });

    chatSendBtn.onclick = () => sendMessage(chatInput.value.trim());

    // ── 🔥 BUTTON FIX (CRITICAL) ─────────────
    document.addEventListener("click", (e) => {
        const btn = e.target.closest(".chat-suggestion-chip");
        if (!btn) return;

        const text = btn.innerText.trim();
        if (text) sendMessage(text);
    });

    // ── MAIN FUNCTION ──────────────────────
    async function sendMessage(text) {
        if (!text) return;

        if (controller) controller.abort();
        controller = new AbortController();

        chatInput.value = "";
        chatSendBtn.disabled = true;

        hideWelcome();
        addBubble("you", text);

        const typing = showTyping();

        try {
            const res = await fetch("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "include",
                body: JSON.stringify({ message: text }),
                signal: controller.signal
            });

            if (!res.ok) throw new Error("Server error");
            if (!res.body) throw new Error("No stream");

            removeTyping(typing);

            const reader = res.body.getReader();
            const decoder = new TextDecoder();

            const bubble = addBubble("ai", "");
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                const events = buffer.split("\n\n");

                for (let i = 0; i < events.length - 1; i++) {
                    const event = events[i];

                    if (event.includes("event:done")) continue;
                    if (event.includes("event:error")) {
                        bubble.textContent = "⚠️ Error generating response";
                        continue;
                    }

                    const match = event.match(/data:(.*)/);
                    if (!match) continue;

                    let token = match[1];
                    if (!token || token.trim() === "") continue;

                    token = sanitizeToken(token);

                    bubble.textContent += token;

                    // cursor animation
                    bubble.textContent =
                        bubble.textContent.replace("▋", "") + "▋";

                    scrollToBottom();
                }

                buffer = events[events.length - 1];
                await microDelay();
            }

            // FINAL CLEAN
            bubble.textContent = cleanFinalOutput(bubble.textContent);

        } catch (err) {
            if (err.name === "AbortError") return;

            console.error(err);
            removeTyping(typing);
            addBubble("ai", "⚠️ Failed to get response. Try again.");
        }

        chatSendBtn.disabled = false;
        chatInput.focus();
    }

    // ── TOKEN CLEANING ─────────────────────
    function sanitizeToken(token) {
        return token
            .replace(/\\n/g, "\n")
            .replace(/Observation.?—?/gi, "")
            .replace(/Action.?—?/gi, "")
            .replace(/Benefit.?—?/gi, "");
    }

    // ── ✅ FIXED FINAL CLEAN ───────────────
    function cleanFinalOutput(text) {
        return text
            .replace("▋", "")
            .replace(/\s+/g, " ")
            .replace(/ ([.,!?])/g, "$1")
            .trim();   // ❌ removed broken word-merging regex
    }

    // ── UI HELPERS ─────────────────────────
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
        return bubble;
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
        if (el?.parentNode) el.parentNode.removeChild(el);
    }

    function scrollToBottom() {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function hideWelcome() {
        const welcome = chatMessages.querySelector(".chat-welcome");
        if (welcome) welcome.style.display = "none";
    }

    function microDelay() {
        return new Promise(r => setTimeout(r, 0));
    }

});