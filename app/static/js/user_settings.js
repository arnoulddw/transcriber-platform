// app/static/js/user_settings.js
/* Handles interactions within the 'Manage API Keys' modal. */

const userSettingsLogPrefix = "[UserSettingsJS]"; // Prefix for console logs

// --- Modal State & Elements ---
let apiKeyModal = null;
let apiKeyModalOverlay = null;
let apiKeyModalPanel = null;
let apiKeyModalTriggers = [];
let apiKeyModalCloseButtons = [];
let previouslyFocusedElement = null;
let publicApiKeyStatusEl = null;
let publicApiKeyForm = null;
let publicApiKeyNameInput = null;
let publicApiKeyCreatedPanel = null;
let publicApiKeyValueInput = null;
let publicApiCopyBtn = null;
let publicApiKeyListEl = null;
let lastGeneratedPublicApiKey = null;

function initializeApiKeyModalElements() {
    apiKeyModal = document.getElementById('apiKeyModal');
    apiKeyModalOverlay = document.getElementById('apiKeyModalOverlay');
    apiKeyModalPanel = document.getElementById('apiKeyModalPanel'); // Panel for focus trap
    apiKeyModalTriggers = Array.from(document.querySelectorAll('#apiKeysBtn, #apiKeysBtnMobile'));
    if (apiKeyModal) {
        apiKeyModalCloseButtons = Array.from(apiKeyModal.querySelectorAll('#apiKeyModalCloseButtonHeader, #apiKeyModalCloseButtonFooter'));
    }
    publicApiKeyStatusEl = document.getElementById('publicApiKeyStatus');
    publicApiKeyForm = document.getElementById('publicApiKeyForm');
    publicApiKeyNameInput = document.getElementById('publicApiKeyName');
    publicApiKeyCreatedPanel = document.getElementById('publicApiKeyCreatedPanel');
    publicApiKeyValueInput = document.getElementById('publicApiKeyValue');
    publicApiCopyBtn = document.getElementById('copyPublicApiKeyBtn');
    publicApiKeyListEl = document.getElementById('publicApiKeyList');

    if (!apiKeyModal || !apiKeyModalOverlay || !apiKeyModalPanel) {
        window.logger.warn(userSettingsLogPrefix, "One or more API Key modal core elements not found.");
        return false;
    }
    if (apiKeyModalTriggers.length === 0 && window.IS_MULTI_USER) { // Only warn if multi-user and triggers expected
        window.logger.warn(userSettingsLogPrefix, "API Key trigger buttons not found.");
    }
    return true;
}

function openApiKeyModalDialog() {
    if (!apiKeyModal || !apiKeyModalOverlay || !apiKeyModalPanel) {
        window.logger.error(userSettingsLogPrefix, "Cannot open API Key modal: core elements missing.");
        return;
    }
    previouslyFocusedElement = document.activeElement;

    apiKeyModal.classList.remove('hidden');
    apiKeyModalOverlay.classList.remove('hidden');
    apiKeyModalPanel.classList.remove('hidden'); // Ensure panel is also visible for transitions

    // Force reflow for transition
    void apiKeyModal.offsetWidth;

    apiKeyModal.classList.add('opacity-100');
    apiKeyModalOverlay.classList.add('opacity-100');
    apiKeyModalPanel.classList.add('opacity-100', 'scale-100');
    apiKeyModalPanel.classList.remove('opacity-0', 'scale-95');


    apiKeyModal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden'; // Prevent background scroll

    fetchApiKeyStatus(); // Fetch status when modal opens

    // Focus trap
    const focusableElements = Array.from(
        apiKeyModalPanel.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
    ).filter(el => !el.disabled && !el.closest('.hidden'));

    if (focusableElements.length > 0) {
        focusableElements[0].focus();
    } else {
        apiKeyModalPanel.focus(); // Fallback
    }
    window.logger.info(userSettingsLogPrefix, "API Key modal opened.");
}

function closeApiKeyModalDialog() {
    if (!apiKeyModal || !apiKeyModalOverlay || !apiKeyModalPanel) {
        window.logger.error(userSettingsLogPrefix, "Cannot close API Key modal: core elements missing.");
        return;
    }

    apiKeyModal.classList.remove('opacity-100');
    apiKeyModalOverlay.classList.remove('opacity-100');
    apiKeyModalPanel.classList.remove('opacity-100', 'scale-100');
    apiKeyModalPanel.classList.add('opacity-0', 'scale-95');


    apiKeyModal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';

    setTimeout(() => {
        apiKeyModal.classList.add('hidden');
        apiKeyModalOverlay.classList.add('hidden');
        // apiKeyModalPanel.classList.add('hidden'); // Panel visibility controlled by parent
    }, 300); // Match transition duration

    if (previouslyFocusedElement) {
        previouslyFocusedElement.focus();
        previouslyFocusedElement = null;
    }
    window.logger.info(userSettingsLogPrefix, "API Key modal closed.");
}


document.addEventListener('DOMContentLoaded', function() {
    if (!initializeApiKeyModalElements()) {
        window.logger.warn(userSettingsLogPrefix, "API Key modal setup skipped due to missing elements.");
        return;
    }

    apiKeyModalTriggers.forEach(trigger => {
        trigger.addEventListener('click', (event) => {
            event.preventDefault();
            openApiKeyModalDialog();
        });
    });

    apiKeyModalCloseButtons.forEach(button => {
        button.addEventListener('click', closeApiKeyModalDialog);
    });

    if (apiKeyModalOverlay) {
        apiKeyModalOverlay.addEventListener('click', closeApiKeyModalDialog);
    }

    if (publicApiKeyForm) {
        publicApiKeyForm.addEventListener('submit', handleGeneratePublicApiKey);
    }
    if (publicApiCopyBtn) {
        publicApiCopyBtn.addEventListener('click', copyPublicApiKeyToClipboard);
    }
    if (publicApiKeyListEl) {
        publicApiKeyListEl.addEventListener('click', function(event) {
            const revokeButton = event.target.closest('.revoke-public-key-btn');
            if (revokeButton) {
                event.preventDefault();
                handleRevokePublicApiKey(revokeButton);
            }
        });
    }

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && apiKeyModal && !apiKeyModal.classList.contains('hidden')) {
            closeApiKeyModalDialog();
        }
        // Basic focus trap for Tab key
        if (event.key === 'Tab' && apiKeyModal && !apiKeyModal.classList.contains('hidden')) {
            const focusableElements = Array.from(
                apiKeyModalPanel.querySelectorAll(
                    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
                )
            ).filter(el => !el.disabled && !el.closest('.hidden'));

            if (focusableElements.length === 0) return;

            const firstFocusable = focusableElements[0];
            const lastFocusable = focusableElements[focusableElements.length - 1];

            if (event.shiftKey) { // Shift + Tab
                if (document.activeElement === firstFocusable) {
                    lastFocusable.focus();
                    event.preventDefault();
                }
            } else { // Tab
                if (document.activeElement === lastFocusable) {
                    firstFocusable.focus();
                    event.preventDefault();
                }
            }
        }
    });

    // --- API Key Form Submission Listener ---
    const apiKeyForm = document.getElementById('apiKeyForm');
    if (apiKeyForm) {
        apiKeyForm.addEventListener('submit', handleApiKeySave);
        window.logger.debug(userSettingsLogPrefix, "API Key save form listener attached.");
    } else if (window.IS_MULTI_USER) {
         window.logger.warn(userSettingsLogPrefix, "API Key form element (#apiKeyForm) not found.");
    }

    // --- API Key Deletion Listener (Event Delegation) ---
    const keyStatusListContainer = apiKeyModalPanel.querySelector('ul.divide-y'); // Target the ul inside the panel
    if (keyStatusListContainer) {
        keyStatusListContainer.addEventListener('click', function(event) {
            const deleteButton = event.target.closest('.delete-key-btn');
            if (deleteButton) {
                window.logger.info(userSettingsLogPrefix, "Delete button clicked:", deleteButton);
                event.preventDefault();
                handleApiKeyDelete(deleteButton);
            }
        });
        window.logger.debug(userSettingsLogPrefix, "API Key delete listener (delegation) attached.");
    } else if (window.IS_MULTI_USER) {
         window.logger.warn(userSettingsLogPrefix, "API Key status list container not found for delete listener.");
    }

}); // End DOMContentLoaded


function updatePublicApiKeySection(publicStatus) {
    if (!publicApiKeyStatusEl || !publicApiKeyValueInput || !publicApiCopyBtn) return;
    const status = publicStatus || {};
    const keys = Array.isArray(status.keys) ? status.keys : [];
    const enabled = Boolean(status && status.enabled);

    if (enabled) {
        publicApiKeyStatusEl.textContent = keys.length === 1 ? '1 Active' : `${keys.length} Active`;
        publicApiKeyStatusEl.className = 'text-sm text-green-600 font-medium';
    } else {
        publicApiKeyStatusEl.textContent = 'Not Configured';
        publicApiKeyStatusEl.className = 'text-sm text-orange-600 font-medium';
    }

    if (lastGeneratedPublicApiKey) {
        if (publicApiKeyCreatedPanel) publicApiKeyCreatedPanel.classList.remove('hidden');
        publicApiKeyValueInput.value = lastGeneratedPublicApiKey;
        publicApiCopyBtn.disabled = false;
    } else {
        if (publicApiKeyCreatedPanel) publicApiKeyCreatedPanel.classList.add('hidden');
        publicApiKeyValueInput.value = '';
        publicApiCopyBtn.disabled = true;
    }

    if (publicApiKeyListEl) {
        publicApiKeyListEl.innerHTML = '';
        if (keys.length === 0) {
            const emptyRow = document.createElement('li');
            emptyRow.className = 'py-3 text-sm text-text-muted';
            emptyRow.textContent = 'No public API keys configured.';
            publicApiKeyListEl.appendChild(emptyRow);
        } else {
            keys.forEach(key => {
                const row = document.createElement('li');
                row.className = 'py-3 flex items-center justify-between gap-3';

                const content = document.createElement('div');
                content.className = 'min-w-0';

                const name = document.createElement('p');
                name.className = 'text-sm font-medium text-text-strong truncate';
                name.textContent = key.name || 'Public API key';

                const meta = document.createElement('p');
                meta.className = 'text-xs text-text-muted';
                const createdAt = key.created_at ? formatPublicApiCreatedAt(key.created_at) : null;
                meta.textContent = createdAt ? `****${key.last_four || '----'} • Created ${createdAt}` : `****${key.last_four || '----'}`;

                content.appendChild(name);
                content.appendChild(meta);

                const actions = document.createElement('div');
                actions.className = 'flex items-center gap-2 flex-none';

                const active = document.createElement('span');
                active.className = 'text-sm text-green-600 mr-1';
                active.textContent = 'Configured';

                const revokeBtn = document.createElement('button');
                revokeBtn.type = 'button';
                revokeBtn.className = 'revoke-public-key-btn p-1.5 rounded-full text-gray-400 hover:text-red-600 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500';
                revokeBtn.dataset.keyId = key.id;
                revokeBtn.dataset.keyName = key.name || 'Public API key';
                revokeBtn.setAttribute('aria-label', `Revoke ${key.name || 'public API key'}`);
                revokeBtn.innerHTML = '<i class="material-icons text-base">delete</i>';

                actions.appendChild(active);
                actions.appendChild(revokeBtn);
                row.appendChild(content);
                row.appendChild(actions);
                publicApiKeyListEl.appendChild(row);
            });
        }
    }
}

function formatPublicApiCreatedAt(createdAt) {
    try {
        return new Date(createdAt).toLocaleString();
    } catch (e) {
        return createdAt;
    }
}

function copyPublicApiKeyToClipboard(event) {
    event.preventDefault();
    if (!lastGeneratedPublicApiKey) {
        window.showNotification('Nothing to copy. Generate a key first.', 'warning', 4000, false);
        return;
    }
    navigator.clipboard.writeText(lastGeneratedPublicApiKey)
        .then(() => {
            window.showNotification('API key copied to clipboard.', 'success', 3000, false);
        })
        .catch(() => {
            window.showNotification('Could not copy API key. Please copy it manually.', 'error', 4000, false);
        });
}

function handleGeneratePublicApiKey(event) {
    event.preventDefault();
    if (!publicApiKeyForm) return;

    const submitButton = publicApiKeyForm.querySelector('button[type="submit"]');
    const keyName = publicApiKeyNameInput ? publicApiKeyNameInput.value.trim() : '';
    if (!keyName) {
        window.showNotification('Please name this API key.', 'warning', 4000, false);
        return;
    }

    const originalHtml = submitButton.innerHTML;
    submitButton.disabled = true;
    submitButton.innerHTML = 'Creating... <span class="spinner ml-2 inline-block" style="width: 1em; height: 1em; border-width: .15em;"></span>';

    const formData = new FormData(publicApiKeyForm);

    fetch('/api/user/public-api-key', {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': window.csrfToken,
            'Accept': 'application/json'
        }
    })
    .then(response => {
        if (!response.ok) {
            return response.json().catch(() => ({ error: `HTTP error! Status: ${response.status}` })).then(errData => {
                throw new Error(errData.error || `HTTP error! Status: ${response.status}`);
            });
        }
        return response.json();
    })
    .then(data => {
        lastGeneratedPublicApiKey = data.api_key;
        if (publicApiKeyNameInput) publicApiKeyNameInput.value = '';
        window.showNotification('New API key generated. Copy it now and keep it secure.', 'success', 5000, false);
        return fetchApiKeyStatus();
    })
    .catch(error => {
        window.logger.error(userSettingsLogPrefix, 'Error generating public API key:', error);
        window.showNotification(`Error generating API key: ${escapeHtml(error.message)}`, 'error', 6000, false);
    })
    .finally(() => {
        submitButton.disabled = false;
        submitButton.innerHTML = originalHtml;
    });
}

function handleRevokePublicApiKey(button) {
    const keyId = button.dataset.keyId;
    const keyName = button.dataset.keyName || 'this public API key';
    if (!keyId) {
        window.showNotification('Could not determine which public API key to revoke.', 'error', 4000, false);
        return;
    }
    if (!confirm(`Revoke ${keyName}? Existing scripts using this key will stop working.`)) {
        return;
    }
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner inline-block" style="width: 0.8em; height: 0.8em; border-width: .15em;"></span>';

    fetch(`/api/user/public-api-key/${encodeURIComponent(keyId)}`, {
        method: 'DELETE',
        headers: {
            'X-CSRFToken': window.csrfToken,
            'Accept': 'application/json'
        }
    })
    .then(response => {
        if (!response.ok) {
            return response.json().catch(() => ({ error: `HTTP error! Status: ${response.status}` })).then(errData => {
                throw new Error(errData.error || `HTTP error! Status: ${response.status}`);
            });
        }
        return response.json();
    })
    .then(data => {
        lastGeneratedPublicApiKey = null;
        window.showNotification(data.message || 'Public API key revoked.', 'success', 4000, false);
        return fetchApiKeyStatus();
    })
    .catch(error => {
        window.logger.error(userSettingsLogPrefix, 'Error revoking public API key:', error);
        window.showNotification(`Error revoking API key: ${escapeHtml(error.message)}`, 'error', 6000, false);
    })
    .finally(() => {
        button.disabled = false;
        button.innerHTML = originalHtml;
    });
}


/**
 * Fetches the current status of configured API keys from the backend API.
 * Updates the status display within the modal.
 * Returns a Promise that resolves with the key status data or rejects on error.
 */
function fetchApiKeyStatus() {
    window.logger.debug(userSettingsLogPrefix, "Fetching API key status...");
    return new Promise((resolve, reject) => {
        const openaiStatusElem = document.getElementById('openaiKeyStatus');
        const assemblyaiStatusElem = document.getElementById('assemblyaiKeyStatus');
        const geminiStatusElem = document.getElementById('geminiKeyStatus');
        const openaiActionsElem = document.getElementById('openaiKeyActions');
        const assemblyaiActionsElem = document.getElementById('assemblyaiKeyActions'); // Corrected ID
        const geminiActionsElem = document.getElementById('geminiKeyActions');

        const permissions = window.USER_PERMISSIONS || {};
        const canUseOpenAI = permissions.use_api_openai_whisper || permissions.use_api_openai_gpt_4o_transcribe;
        const canUseAssemblyAI = permissions.use_api_assemblyai;
        const canUseGemini = permissions.use_api_google_gemini;

        if (canUseOpenAI && openaiStatusElem && openaiActionsElem) {
            updateStatusElement(openaiStatusElem, openaiActionsElem, null, 'openai', 'Checking...');
        } else if (canUseOpenAI) {
            window.logger.debug(userSettingsLogPrefix, "OpenAI status elements not found (but permission exists), skipping initial reset.");
        }

        if (canUseAssemblyAI && assemblyaiStatusElem && assemblyaiActionsElem) {
            updateStatusElement(assemblyaiStatusElem, assemblyaiActionsElem, null, 'assemblyai', 'Checking...');
        } else if (canUseAssemblyAI) {
            window.logger.debug(userSettingsLogPrefix, "AssemblyAI status elements not found (but permission exists), skipping initial reset.");
        }

        if (canUseGemini && geminiStatusElem && geminiActionsElem) {
            updateStatusElement(geminiStatusElem, geminiActionsElem, null, 'gemini', 'Checking...');
        } else if (canUseGemini) {
            window.logger.debug(userSettingsLogPrefix, "Gemini status elements not found (but permission exists), skipping initial reset.");
        }


        fetch('/api/user/keys', {
            method: 'GET',
            headers: { 'Accept': 'application/json', 'X-CSRFToken': window.csrfToken }
        })
        .then(response => {
            if (response.status === 401) throw new Error('Authentication required (401)');
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            return response.json();
        })
        .then(data => {
            window.logger.info(userSettingsLogPrefix, "API Key status received:", data);

            if (canUseOpenAI && openaiStatusElem && openaiActionsElem) {
                updateStatusElement(openaiStatusElem, openaiActionsElem, data.openai, 'openai');
            } else if (canUseOpenAI) {
                window.logger.debug(userSettingsLogPrefix, "OpenAI status elements not found (but permission exists), skipping update.");
            }

            if (canUseAssemblyAI && assemblyaiStatusElem && assemblyaiActionsElem) {
                updateStatusElement(assemblyaiStatusElem, assemblyaiActionsElem, data.assemblyai, 'assemblyai');
            } else if (canUseAssemblyAI) {
                window.logger.debug(userSettingsLogPrefix, "AssemblyAI status elements not found (but permission exists), skipping update.");
            }

            if (canUseGemini && geminiStatusElem && geminiActionsElem) {
                updateStatusElement(geminiStatusElem, geminiActionsElem, data.gemini, 'gemini');
            } else if (canUseGemini) {
                window.logger.debug(userSettingsLogPrefix, "Gemini status elements not found (but permission exists), skipping update.");
            }

            window.API_KEY_STATUS = data || {};
            updatePublicApiKeySection(data.public_api);

            if (typeof window.updateApiKeyNotificationVisibility === 'function') {
                window.updateApiKeyNotificationVisibility(data, permissions);
            } else {
                window.logger.error(userSettingsLogPrefix, "updateApiKeyNotificationVisibility function not found.");
            }

            resolve(data);
        })
        .catch(error => {
            window.logger.error(userSettingsLogPrefix, 'Error fetching API key status:', error);
            window.showNotification(`Error fetching key status: ${escapeHtml(error.message)}`, 'error', 6000, false);

            if (canUseOpenAI && openaiStatusElem && openaiActionsElem) {
                updateStatusElement(openaiStatusElem, openaiActionsElem, false, 'openai', 'Error');
            }
            if (canUseAssemblyAI && assemblyaiStatusElem && assemblyaiActionsElem) {
                updateStatusElement(assemblyaiStatusElem, assemblyaiActionsElem, false, 'assemblyai', 'Error');
            }
            if (canUseGemini && geminiStatusElem && geminiActionsElem) {
                updateStatusElement(geminiStatusElem, geminiActionsElem, false, 'gemini', 'Error');
            }
            updatePublicApiKeySection(null);

            if (error.message.includes('Authentication required')) {
                closeApiKeyModalDialog(); // Close the new modal
            }
            reject(error);
        });
    });
}


/**
 * Updates the display (text, color, actions) for a single API key's status in the modal.
 * @param {HTMLElement} statusElement - The span element for status text (e.g., #openaiKeyStatus).
 * @param {HTMLElement} actionsElement - The span element for action buttons (e.g., #openaiKeyActions).
 * @param {boolean|null} isSet - Whether the key is configured (true/false) or status is unknown (null).
 * @param {string} serviceName - The name of the service ('openai', 'assemblyai', 'gemini').
 * @param {string|null} [overrideText=null] - Optional text to display instead of default status (e.g., 'Checking...', 'Error').
 */
function updateStatusElement(statusElement, actionsElement, isSet, serviceName, overrideText = null) {
    if (!statusElement || !actionsElement) {
        window.logger.warn(userSettingsLogPrefix, `Attempted to update non-existent status elements for service: ${serviceName}`);
        return;
    }

    actionsElement.innerHTML = ''; // Clear previous buttons

    let statusText = '';
    let statusTextColorClass = 'text-gray-500'; // Default Tailwind color

    if (overrideText) {
        statusText = overrideText;
        if (overrideText.toLowerCase() === 'error') {
            statusTextColorClass = 'text-red-600';
        } else if (overrideText.toLowerCase() === 'checking...') {
            statusTextColorClass = 'text-gray-500 italic';
        }
    } else if (isSet === true) {
        statusText = 'Configured';
        statusTextColorClass = 'text-green-600';
        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'delete-key-btn p-1.5 rounded-full text-gray-400 hover:text-red-600 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500';
        deleteBtn.dataset.service = serviceName;
        deleteBtn.setAttribute('aria-label', `Delete ${serviceName} key`);
        deleteBtn.innerHTML = '<i class="material-icons text-base">delete</i>'; // Using text-base for smaller icon
        actionsElement.appendChild(deleteBtn);
    } else if (isSet === false) {
        statusText = 'Not Configured';
        statusTextColorClass = 'text-orange-500';
    } else {
         statusText = 'Unknown';
         statusTextColorClass = 'text-gray-500';
    }

    statusElement.textContent = statusText;
    statusElement.className = `text-sm mr-3 ${statusTextColorClass}`; // Apply Tailwind classes
}

/**
 * Handles the submission of the API key form (Save Key button).
 * Sends the data to the backend API.
 * @param {Event} event - The form submission event.
 */
function handleApiKeySave(event) {
    event.preventDefault();
    const logPrefix = "[UserSettingsJS:handleApiKeySave]";
    window.logger.info(logPrefix, "Save key form submitted.");

    const form = event.target;
    const serviceSelect = document.getElementById('apiKeyServiceSelect');
    const keyInput = document.getElementById('apiKeyInput');
    const submitButton = form.querySelector('button[type="submit"]');

    const service = serviceSelect.value;
    const apiKey = keyInput.value;

    if (!service) {
        window.showNotification('Please select an API service.', 'warning', 4000, false);
        return;
    }
    if (!apiKey) {
        window.showNotification('Please enter an API key.', 'warning', 4000, false);
        return;
    }
    if (apiKey.length < 10) {
         window.showNotification('API key seems too short.', 'warning', 4000, false);
         return;
    }

    const originalButtonHtml = submitButton.innerHTML;
    submitButton.disabled = true;
    submitButton.innerHTML = 'Saving... <span class="spinner ml-2 inline-block" style="width: 1em; height: 1em; border-width: .15em;"></span>';


    const formData = new FormData(form);

    fetch('/api/user/keys', {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': window.csrfToken,
            'Accept': 'application/json'
        }
    })
    .then(response => {
        if (!response.ok) {
            return response.json().catch(() => {
                return { error: `HTTP error! Status: ${response.status}` };
            }).then(errData => {
                throw new Error(errData.error || `Save failed: ${response.statusText}`);
            });
        }
        return response.json();
    })
    .then(data => {
        window.logger.info(logPrefix, "API Key save response:", data);
        window.showNotification(data.message || 'API Key saved successfully!', 'success', 4000, false);
        keyInput.value = '';
        serviceSelect.value = '';
        // No M.FormSelect.init needed for Tailwind select if it's a basic HTML select

        return fetchApiKeyStatus();
    })
    .then(() => {
        if (typeof window.invalidateReadinessCache === 'function') {
            window.invalidateReadinessCache(); // Invalidate cache
        }
        if (typeof checkTranscribeButtonState === 'function') {
            window.logger.info(logPrefix, "Triggering main page UI update after save.");
            checkTranscribeButtonState();
        } else {
            window.logger.warn(logPrefix, "checkTranscribeButtonState function not found in main.js");
        }
    })
    .catch(error => {
        window.logger.error(logPrefix, 'Error saving API key:', error);
        if (!error.message.includes("fetching key status")) {
            window.showNotification(`Error saving key: ${escapeHtml(error.message)}`, 'error', 6000, false);
        }
        if (error.message.includes('Authentication required')) {
            closeApiKeyModalDialog(); // Close the new modal
        }
    })
    .finally(() => {
        submitButton.disabled = false;
        submitButton.innerHTML = originalButtonHtml; // Restore original button text
    });
}


/**
 * Handles the click on a delete API key button (using event delegation).
 * @param {HTMLElement} button - The delete button element that was clicked.
 */
function handleApiKeyDelete(button) {
    const service = button.dataset.service;
    const logPrefix = `[UserSettingsJS:handleApiKeyDelete:${service}]`;
    window.logger.info(logPrefix, `Delete key requested.`);

    if (!service) {
        window.logger.error(logPrefix, "Delete handler called without a service specified on the button.");
        window.showNotification('Could not determine which key to delete.', 'error', 4000, false);
        return;
    }

    let serviceDisplayName = service.charAt(0).toUpperCase() + service.slice(1);
    if (service === 'openai') serviceDisplayName = 'OpenAI';
    else if (service === 'assemblyai') serviceDisplayName = 'AssemblyAI (Universal)';
    else if (service === 'gemini') serviceDisplayName = 'Google Gemini';

    if (!confirm(`Are you sure you want to delete the API key for ${serviceDisplayName}?`)) {
        window.logger.info(logPrefix, "Delete cancelled by user.");
        return;
    }

    button.disabled = true;
    const originalIcon = button.innerHTML;
    button.innerHTML = '<span class="spinner inline-block" style="width: 0.8em; height: 0.8em; border-width: .15em;"></span>';


    fetch(`/api/user/keys/${service}`, {
        method: 'DELETE',
        headers: {
            'X-CSRFToken': window.csrfToken,
            'Accept': 'application/json'
        }
    })
    .then(response => {
        if (!response.ok) {
            return response.json().catch(() => {
                return { error: `HTTP error! Status: ${response.status}` };
            }).then(errData => {
                const message = response.status === 404 ? (errData.error || 'Key not found.') : (errData.error || `Delete failed: ${response.statusText}`);
                throw new Error(message);
            });
        }
        return response.json();
    })
    .then(data => {
        window.logger.info(logPrefix, `API Key delete response:`, data);
        window.showNotification(data.message || `API Key for ${serviceDisplayName} deleted.`, 'success', 4000, false);

        return fetchApiKeyStatus();
    })
    .then(() => {
        if (typeof window.invalidateReadinessCache === 'function') {
            window.invalidateReadinessCache(); // Invalidate cache
        }
        if (typeof checkTranscribeButtonState === 'function') {
            window.logger.info(logPrefix, "Triggering main page UI update after delete.");
            checkTranscribeButtonState();
        } else {
            window.logger.warn(logPrefix, "checkTranscribeButtonState function not found in main.js");
        }
    })
    .catch(error => {
        window.logger.error(logPrefix, `Error deleting API key:`, error);
        if (!error.message.includes("fetching key status")) {
             window.showNotification(`Error deleting key: ${escapeHtml(error.message)}`, 'error', 6000, false);
        }
        if (error.message.includes('Authentication required')) {
            closeApiKeyModalDialog(); // Close the new modal
        }

        if (document.body.contains(button)) {
            button.disabled = false;
            button.innerHTML = originalIcon;
        }
    });
}

/**
 * Simple HTML escaping.
 * @param {string} str The string to escape.
 * @returns {string} Escaped string.
 */
 function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
