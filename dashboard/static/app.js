// Initialize Telegram WebApp SDK
const WebApp = window.Telegram?.WebApp;
if (WebApp) {
    WebApp.ready();
    WebApp.expand();
    // Configure header/theme colors to match the app
    if (WebApp.setHeaderColor) {
        WebApp.setHeaderColor('#111219');
    }
}

// Global Application State
const state = {
    userId: null,
    chatId: null,
    sessions: [],
    currentSessionId: null,
    currentSessionPhase: 'idle',
    currentSessionStatus: 'active',
    selectedFile: null
};

// Initialize User Credentials (Telegram vs Desktop testing)
function initCredentials() {
    if (WebApp && WebApp.initDataUnsafe && WebApp.initDataUnsafe.user) {
        state.userId = WebApp.initDataUnsafe.user.id;
        // In group threads there might be a chat, otherwise default chat_id to user_id
        state.chatId = WebApp.initDataUnsafe.chat?.id || WebApp.initDataUnsafe.user.id;
        console.log(`Initialized Telegram WebApp mode: user_id=${state.userId}, chat_id=${state.chatId}`);
    } else {
        // Fallback context for desktop browser development
        state.userId = 5226910060;
        state.chatId = 5226910060;
        console.log(`Initialized Mock Browser mode: user_id=${state.userId}, chat_id=${state.chatId}`);
    }
}

// ==========================================================================
// Formatting & Spec Extraction Helpers
// ==========================================================================

function formatPrice(val, currency = 'EUR') {
    if (val === null || val === undefined) return '';
    const symbol = currency === 'EUR' ? '€' : (currency === 'GBP' ? '£' : currency + ' ');
    return `${symbol}${Math.round(val).toLocaleString()}`;
}

function getStatusLabel(status, phase) {
    if (status === 'deal_agreed') return 'Deal Agreed';
    if (status === 'walk_away') return 'Walk Away';
    if (status === 'reset') return 'Reset';
    if (phase === 'awaiting_info') return 'Awaiting Specs';
    return 'Negotiating';
}

function getStatusClass(status, phase) {
    if (status === 'deal_agreed') return 'badge-agreed';
    if (status === 'walk_away') return 'badge-walkaway';
    if (status === 'reset') return 'badge-reset';
    if (phase === 'awaiting_info') return 'badge-awaiting';
    return 'badge-negotiating';
}

// Heuristic extraction of vehicle specs from text for badges
function extractSpecs(text) {
    if (!text) return [];
    const specs = [];
    const lower = text.toLowerCase();

    // Make detection
    const makes = ['bmw', 'mercedes', 'audi', 'porsche', 'lexus', 'tesla', 'fiat', 'ford', 'toyota', 'volkswagen', 'vw', 'peugeot', 'renault', 'opel', 'alfa'];
    for (const m of makes) {
        if (lower.includes(m)) {
            specs.push({ icon: 'fa-tag', text: m.toUpperCase() });
            break;
        }
    }

    // Year detection
    const yearMatch = text.match(/\b(19|20)\d{2}\b/);
    if (yearMatch) {
        specs.push({ icon: 'fa-calendar', text: yearMatch[0] });
    }

    // Mileage detection
    const kmMatch = text.match(/\b\d{1,3}(?:[,.]?\d{3})?\s*(?:k|km|kms|kilometers|miles|mi)\b/i);
    if (kmMatch) {
        specs.push({ icon: 'fa-gauge', text: kmMatch[0].replace(/\s+/g, '') });
    } else {
        const kMatch = text.match(/\b\d{2,3}k\b/i);
        if (kMatch) {
            specs.push({ icon: 'fa-gauge', text: kMatch[0] });
        }
    }

    // Transmission
    if (lower.includes('manual') || lower.includes('manuale')) {
        specs.push({ icon: 'fa-gears', text: 'Manual' });
    } else if (lower.includes('automatic') || lower.includes('automatico') || lower.includes('auto')) {
        specs.push({ icon: 'fa-gears', text: 'Automatic' });
    }

    // Fuel type
    const fuels = ['diesel', 'petrol', 'hybrid', 'electric', 'gpl', 'lpg', 'benzina', 'metano'];
    for (const f of fuels) {
        if (lower.includes(f)) {
            let label = f.charAt(0).toUpperCase() + f.slice(1);
            if (f === 'benzina') label = 'Petrol';
            specs.push({ icon: 'fa-gas-pump', text: label });
            break;
        }
    }

    return specs;
}

// Clean text description to show as a short title in sidebar
function getSessionTitle(description) {
    if (!description) return 'New Car Listing';
    // Look for first line or first 30 chars
    const clean = description.split('\n')[0].trim();
    if (clean.length > 35) {
        return clean.substring(0, 32) + '...';
    }
    return clean || 'Car Negotiation';
}

// Helper to extract timestamp format
function formatTime(isoString) {
    try {
        const date = new Date(isoString);
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (e) {
        return '';
    }
}

// ==========================================================================
// API Handlers & UI Builders
// ==========================================================================

async function loadSessions(autoSelectFirst = false) {
    const listContainer = document.getElementById('sessions-list');
    try {
        const response = await fetch(`/api/sessions?user_id=${state.userId}`);
        if (!response.ok) throw new Error('Failed to load sessions');
        
        state.sessions = await response.ok ? await response.json() : [];
        
        if (state.sessions.length === 0) {
            listContainer.innerHTML = `
                <div class="empty-list-loader">
                    <i class="fa-solid fa-car-side" style="font-size: 30px; opacity: 0.5;"></i>
                    <p>No negotiations started yet.</p>
                </div>
            `;
            return;
        }

        listContainer.innerHTML = '';
        state.sessions.forEach(sess => {
            const specs = extractSpecs(sess.description);
            const statusLabel = getStatusLabel(sess.status, sess.phase);
            const statusClass = getStatusClass(sess.status, sess.phase);
            
            // Guess latest price from description or estimate
            let priceDisplay = 'Price pending';
            if (sess.low_price) {
                // If negotiation has an estimate, show mid-point or low
                priceDisplay = formatPrice(sess.low_price, sess.currency);
            }

            const activeClass = state.currentSessionId === sess.id ? 'active' : '';

            const item = document.createElement('div');
            item.className = `session-item ${activeClass}`;
            item.setAttribute('data-id', sess.id);
            item.innerHTML = `
                <div class="session-item-header">
                    <h4 class="session-item-title">${getSessionTitle(sess.description)}</h4>
                    <span class="session-item-time">${formatTime(sess.last_activity_at)}</span>
                </div>
                <p class="session-item-desc">${sess.description || 'Image uploaded'}</p>
                <div class="session-item-footer">
                    <span class="badge ${statusClass}">${statusLabel}</span>
                    <span class="session-item-price">${priceDisplay}</span>
                </div>
            `;

            item.addEventListener('click', () => selectSession(sess.id));
            listContainer.appendChild(item);
        });

        if (autoSelectFirst && state.sessions.length > 0 && !state.currentSessionId) {
            selectSession(state.sessions[0].id);
        }
    } catch (err) {
        console.error(err);
        listContainer.innerHTML = `
            <div class="empty-list-loader">
                <i class="fa-solid fa-triangle-exclamation" style="color: var(--status-walkaway);"></i>
                <p>Error loading negotiations.</p>
            </div>
        `;
    }
}

async function selectSession(sessionId) {
    state.currentSessionId = sessionId;
    
    // Highlight sidebar active item
    document.querySelectorAll('.session-item').forEach(item => {
        if (parseInt(item.getAttribute('data-id')) === sessionId) {
            item.classList.add('active');
        } else {
            item.classList.remove('active');
        }
    });

    const activePane = document.getElementById('active-negotiation-pane');
    const newPane = document.getElementById('new-negotiation-pane');
    const container = document.getElementById('app-container');

    // Switch view panes
    activePane.style.display = 'flex';
    newPane.style.display = 'none';
    
    // Change layout classes for mobile responsiveness
    container.classList.remove('show-sidebar', 'show-new');
    container.classList.add('show-chat');

    // Get session metadata
    const sess = state.sessions.find(s => s.id === sessionId);
    if (!sess) return;

    state.currentSessionPhase = sess.phase;
    state.currentSessionStatus = sess.status;

    // Render Title & Badges
    document.getElementById('chat-car-title').innerText = getSessionTitle(sess.description);
    const badgesContainer = document.getElementById('chat-car-badges');
    badgesContainer.innerHTML = '';
    const specs = extractSpecs(sess.description);
    specs.forEach(sp => {
        badgesContainer.innerHTML += `
            <span class="meta-badge"><i class="fa-solid ${sp.icon}"></i> ${sp.text}</span>
        `;
    });

    // Render Status Badge
    const statusBadge = document.getElementById('session-status-badge');
    statusBadge.innerText = getStatusLabel(sess.status, sess.phase);
    statusBadge.className = `badge ${getStatusClass(sess.status, sess.phase)}`;

    // Set Input Fields configuration
    const chatInput = document.getElementById('chat-input');
    const chatForm = document.getElementById('chat-form');
    
    if (sess.status !== 'active') {
        chatInput.disabled = true;
        chatInput.placeholder = "Negotiation concluded.";
        chatForm.querySelector('button').disabled = true;
    } else {
        chatInput.disabled = false;
        chatForm.querySelector('button').disabled = false;
        if (sess.phase === 'awaiting_info') {
            chatInput.placeholder = "Provide clarifying details (e.g. year, mileage)...";
        } else {
            chatInput.placeholder = "Type your response / counter-offer...";
        }
    }

    // Load Turns history
    await loadTurns(sessionId, sess);
}

async function loadTurns(sessionId, sess) {
    const messagesContainer = document.getElementById('chat-messages');
    messagesContainer.innerHTML = `
        <div class="empty-list-loader">
            <i class="fa-solid fa-circle-notch fa-spin"></i>
        </div>
    `;

    try {
        const response = await fetch(`/api/sessions/${sessionId}/turns`);
        if (!response.ok) throw new Error('Failed to load message history');
        const turns = await response.json();
        
        messagesContainer.innerHTML = '';
        let lastOfferPrice = null;
        let lastCurrency = sess.currency || 'EUR';

        turns.forEach(t => {
            if (t.offered_price) {
                lastOfferPrice = t.offered_price;
                if (t.currency) lastCurrency = t.currency;
            }

            if (t.model_status === 'summary') {
                // Render summary box
                const div = document.createElement('div');
                div.className = `system-event ${sess.status === 'deal_agreed' ? 'deal-agreed' : 'walk-away'}`;
                div.innerHTML = `<strong>${sess.status === 'deal_agreed' ? 'Deal Agreed' : 'Closed'}</strong><br>${t.text}`;
                messagesContainer.appendChild(div);
                return;
            }

            const speakerClass = t.speaker === 'buyer' ? 'buyer' : 'seller';
            const row = document.createElement('div');
            row.className = `message-row ${speakerClass}`;
            
            row.innerHTML = `
                <div class="message-bubble">
                    <div class="message-text">${t.text}</div>
                    <div class="message-meta">
                        ${t.speaker === 'buyer' ? 'CarBot' : 'Owner'} • ${formatTime(t.created_at)}
                    </div>
                </div>
            `;
            messagesContainer.appendChild(row);
        });

        // Update Estimator Gauge Widget
        updateEstimatorGauge(sess, lastOfferPrice, lastCurrency);
        
        // Scroll to bottom
        scrollToBottom();

    } catch (err) {
        console.error(err);
        messagesContainer.innerHTML = '<p style="padding: 20px; color: var(--status-walkaway); text-align: center;">Error loading messages.</p>';
    }
}

function updateEstimatorGauge(sess, currentOffer, currency) {
    const panel = document.getElementById('estimator-panel');
    const rangeText = document.getElementById('estimator-range-text');
    const reasoningText = document.getElementById('estimator-reasoning');
    
    // Hide panel if no estimate is available yet
    if (!sess.low_price || !sess.high_price) {
        panel.style.display = 'none';
        return;
    }

    panel.style.display = 'block';
    
    const low = sess.low_price;
    const high = sess.high_price;
    
    rangeText.innerText = `${formatPrice(low, currency)} - ${formatPrice(high, currency)}`;
    reasoningText.innerText = sess.reasoning || '';
    
    document.getElementById('gauge-label-low').innerText = formatPrice(low, currency);
    document.getElementById('gauge-label-high').innerText = formatPrice(high, currency);

    const pin = document.getElementById('gauge-pin');
    const pinVal = document.getElementById('gauge-pin-value');
    const fill = document.querySelector('.gauge-fill');

    if (currentOffer) {
        pin.style.display = 'block';
        pinVal.innerText = formatPrice(currentOffer, currency);

        // Calculate percentage along the gauge line
        // We spread the gauge visually from (low - 20% span) to (high + 20% span) to give breathing room
        const span = high - low;
        const minBoundary = Math.max(0, low - span * 0.25);
        const maxBoundary = high + span * 0.25;
        
        let pct = ((currentOffer - minBoundary) / (maxBoundary - minBoundary)) * 100;
        pct = Math.max(5, Math.min(95, pct)); // clamp visually
        
        pin.style.left = `${pct}%`;

        // Position fill between low and high
        const fillLeft = ((low - minBoundary) / (maxBoundary - minBoundary)) * 100;
        const fillWidth = ((high - low) / (maxBoundary - minBoundary)) * 100;
        fill.style.left = `${fillLeft}%`;
        fill.style.width = `${fillWidth}%`;

        // Change color based on offer quality (relative to low/high estimate)
        if (currentOffer < low) {
            pin.style.borderColor = 'var(--status-agreed)'; // Great buyer price
            pinVal.style.backgroundColor = 'var(--status-agreed)';
        } else if (currentOffer > high) {
            pin.style.borderColor = 'var(--status-walkaway)'; // Too expensive
            pinVal.style.backgroundColor = 'var(--status-walkaway)';
        } else {
            pin.style.borderColor = 'var(--accent-blue)'; // Fair range
            pinVal.style.backgroundColor = 'var(--accent-blue)';
        }
    } else {
        pin.style.display = 'none';
        // Put fill spanning full middle
        fill.style.left = '25%';
        fill.style.width = '50%';
    }
}

function scrollToBottom() {
    const chatContainer = document.querySelector('.chat-log-container');
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// ==========================================================================
// Form & Document Submissions
// ==========================================================================

async function handleSendMessage(e) {
    e.preventDefault();
    if (state.currentSessionStatus !== 'active') return;

    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    // Render User message instantly for immediate responsiveness
    const messagesContainer = document.getElementById('chat-messages');
    const userRow = document.createElement('div');
    userRow.className = 'message-row seller';
    const now = new Date();
    userRow.innerHTML = `
        <div class="message-bubble">
            <div class="message-text">${text}</div>
            <div class="message-meta">
                Owner • ${now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </div>
        </div>
    `;
    messagesContainer.appendChild(userRow);
    
    // Clear input & scroll to bottom
    input.value = '';
    scrollToBottom();

    // Show bot typing indicator
    const typingIndicator = document.getElementById('typing-indicator');
    typingIndicator.style.display = 'block';
    scrollToBottom();

    try {
        const formData = new FormData();
        formData.append('text', text);

        let endpoint = `/api/sessions/${state.currentSessionId}/message`;
        if (state.currentSessionPhase === 'awaiting_info') {
            endpoint = `/api/sessions/${state.currentSessionId}/clarify`;
        }

        const response = await fetch(endpoint, {
            method: 'POST',
            body: formData
        });

        if (!response.ok) throw new Error('Network error sending message');
        const data = await response.json();
        
        // Hide typing indicator
        typingIndicator.style.display = 'none';

        // Load turns and reload sidebar list
        await loadSessions(false);
        await selectSession(state.currentSessionId);

    } catch (err) {
        console.error(err);
        typingIndicator.style.display = 'none';
        const errorRow = document.createElement('div');
        errorRow.className = 'system-event walk-away';
        errorRow.innerText = 'Failed to get counter-offer. Check connection.';
        messagesContainer.appendChild(errorRow);
        scrollToBottom();
    }
}

async function handleCreateNewSession(e) {
    e.preventDefault();
    const descText = document.getElementById('desc-input').value.trim();
    
    if (!descText && !state.selectedFile) {
        alert('Please enter a vehicle description or upload a photo.');
        return;
    }

    // Show processing overlay
    const overlay = document.getElementById('loading-overlay');
    overlay.style.display = 'flex';

    try {
        const formData = new FormData();
        if (descText) formData.append('description', descText);
        if (state.selectedFile) formData.append('photo', state.selectedFile);
        
        // Pass Telegram tracking context
        if (state.userId) formData.append('user_id', state.userId);
        if (state.chatId) formData.append('chat_id', state.chatId);

        const response = await fetch('/api/sessions/new', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) throw new Error('Failed to parse vehicle listing.');
        const result = await response.json();
        
        // Clear form
        document.getElementById('new-negotiation-form').reset();
        removeSelectedFile();
        
        // Hide overlay
        overlay.style.display = 'none';
        
        // Refresh sidebar and auto select new session
        await loadSessions(false);
        await selectSession(result.session_id);

    } catch (err) {
        console.error(err);
        overlay.style.display = 'none';
        alert('Error processing listing. Please clarify details or check if the photo is readable.');
    }
}

async function handleResetSession() {
    if (!state.currentSessionId) return;
    if (!confirm('Are you sure you want to cancel and reset this negotiation?')) return;

    try {
        const response = await fetch(`/api/sessions/${state.currentSessionId}/reset`, {
            method: 'POST'
        });
        if (!response.ok) throw new Error('Reset failed');
        
        // Refresh
        await loadSessions(false);
        await selectSession(state.currentSessionId);
    } catch (err) {
        console.error(err);
        alert('Failed to reset session.');
    }
}

// ==========================================================================
// Drag & Drop Upload Handlers
// ==========================================================================

function handleFileSelection(file) {
    if (!file || !file.type.startsWith('image/')) {
        alert('Please upload an image file (PNG/JPG).');
        return;
    }
    state.selectedFile = file;
    
    // Render file preview card
    document.getElementById('preview-filename').innerText = file.name;
    document.getElementById('preview-filesize').innerText = `${Math.round(file.size / 1024)} KB`;
    
    document.getElementById('zone-prompt').style.display = 'none';
    document.getElementById('zone-preview').style.display = 'flex';
}

function removeSelectedFile() {
    state.selectedFile = null;
    document.getElementById('file-input').value = '';
    
    document.getElementById('zone-prompt').style.display = 'block';
    document.getElementById('zone-preview').style.display = 'none';
}

// ==========================================================================
// SPA Navigation & Event Listeners setup
// ==========================================================================

function setupEventListeners() {
    // New negotiation button in sidebar
    document.getElementById('btn-new-neg-sidebar').addEventListener('click', () => {
        document.getElementById('active-negotiation-pane').style.display = 'none';
        document.getElementById('new-negotiation-pane').style.display = 'flex';
        
        const container = document.getElementById('app-container');
        container.classList.remove('show-sidebar', 'show-chat');
        container.classList.add('show-new');
        
        // Clear sidebar selection visually
        document.querySelectorAll('.session-item').forEach(item => item.classList.remove('active'));
        state.currentSessionId = null;
    });

    // Mobile Navigation: Back to list
    document.getElementById('btn-back-to-sidebar').addEventListener('click', () => {
        const container = document.getElementById('app-container');
        container.classList.remove('show-chat', 'show-new');
        container.classList.add('show-sidebar');
    });
    
    document.getElementById('btn-cancel-new').addEventListener('click', () => {
        const container = document.getElementById('app-container');
        container.classList.remove('show-chat', 'show-new');
        container.classList.add('show-sidebar');
    });

    // Form Submissions
    document.getElementById('chat-form').addEventListener('submit', handleSendMessage);
    document.getElementById('new-negotiation-form').addEventListener('submit', handleCreateNewSession);
    document.getElementById('btn-reset-session').addEventListener('click', handleResetSession);

    // File Drag & Drop
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    
    dropZone.addEventListener('click', () => fileInput.click());
    
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleFileSelection(e.target.files[0]);
        }
    });

    // Prevent default drag behaviors
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
        }, false);
    });

    // Add drag highlight classes
    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, () => dropZone.classList.add('dragover'), false);
    });
    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, () => dropZone.classList.remove('dragover'), false);
    });

    // Handle dropped file
    dropZone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        if (dt && dt.files.length > 0) {
            handleFileSelection(dt.files[0]);
        }
    });

    // Clear file selection button
    document.getElementById('btn-remove-file').addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeSelectedFile();
    });
}

// App Entry Point
document.addEventListener('DOMContentLoaded', async () => {
    initCredentials();
    setupEventListeners();
    // Load sessions, and auto-select the first session in desktop pane view
    await loadSessions(window.innerWidth > 768);
});
