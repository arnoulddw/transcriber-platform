// app/static/js/main_init.js
/* Handles initial page setup, readiness checks, and event listener attachment. */

const mainInitLogPrefix = "[MainInitJS]";
const initLogger = window.logger.scoped("MainInitJS");
const LARGE_FILE_THRESHOLD_MB = 25; 
const CONTEXT_PROMPT_SUPPORTED_APIS = ['gpt-4o-transcribe', 'assemblyai'];
const SPEAKER_DIARIZATION_SUPPORTED_APIS = ['assemblyai'];
const SPEAKER_BTN_DEFAULT_CLASSES = ['bg-white', 'text-gray-700', 'hover:bg-gray-50', 'border-gray-300'];
const SPEAKER_BTN_ACTIVE_CLASSES = ['bg-green-600', 'text-white', 'hover:bg-green-700', 'border-green-600'];

let speakerDiarizationBtnRef = null;
let speakerDiarizationInputRef = null;
let isSpeakerDiarizationActive = false;

// --- Readiness Cache ---
let readinessCache = null;
let readinessCacheTimestamp = 0;
const READINESS_CACHE_DURATION_MS = 30000;
let isFetchingReadiness = false;
let pendingReadinessPromise = null;
let hasLoggedReadinessCacheHit = false;
let hasLoggedReadinessFetchInProgress = false;
let READINESS_DIAGNOSTICS_ENABLED = false;
let lastApiDropdownSignature = null;
let lastContextPromptVisibility = null;
let lastTranscribeButtonState = { enabled: null, reason: null };
let lastTranscribeButtonMeta = {};
let hasLoggedReadinessAttempt = false;

function logTranscribeButtonState(enabled, reason, meta = {}) {
    const normalizedReason = reason || null;
    if (lastTranscribeButtonState.enabled === enabled && lastTranscribeButtonState.reason === normalizedReason) {
        return;
    }
    lastTranscribeButtonState = { enabled, reason: normalizedReason };
    lastTranscribeButtonMeta = meta;
    if (enabled) {
        initLogger.info("Transcribe button enabled.", meta);
    } else {
        initLogger.info("Transcribe button disabled.", { reason: normalizedReason, ...meta });
    }
}

try {
    const storedReadinessDiagnosticsFlag = window.localStorage
        ? window.localStorage.getItem('enable_readiness_diagnostics')
        : null;
    READINESS_DIAGNOSTICS_ENABLED = Boolean(
        window.APP_DEBUG_MODE &&
        (window.ENABLE_READINESS_DIAGNOSTICS === true || storedReadinessDiagnosticsFlag === 'true')
    );
} catch (readinessDiagnosticsError) {
    READINESS_DIAGNOSTICS_ENABLED = false;
}
window.READINESS_DIAGNOSTICS_ENABLED = READINESS_DIAGNOSTICS_ENABLED;

function invalidateReadinessCache() {
    readinessCache = null;
    readinessCacheTimestamp = 0;
    hasLoggedReadinessCacheHit = false;
    hasLoggedReadinessFetchInProgress = false;
    hasLoggedReadinessAttempt = false;
    initLogger.debug("Readiness cache invalidated.");
}
window.invalidateReadinessCache = invalidateReadinessCache; 

function updateApiDropdownState(apiKeyStatus) {
    const apiSelect = document.getElementById('apiSelect');
    if (!apiSelect || !window.IS_MULTI_USER) {
        initLogger.debug("Skipping API dropdown update (not multi-user or element missing).");
        return;
    }
    const currentKeyStatus = (typeof apiKeyStatus === 'object' && apiKeyStatus !== null) ? apiKeyStatus : {};
    const options = apiSelect.options;
    const disabledMarker = " (API Key Missing)";
    const changes = [];

    for (let i = 0; i < options.length; i++) {
        const option = options[i];
        const keyRequired = option.dataset.keyRequired;
        if (keyRequired) {
            const isKeySet = currentKeyStatus[keyRequired];
            const isDisabled = option.disabled;
            const currentText = option.textContent;

            if (!isKeySet && !isDisabled) {
                option.disabled = true;
                if (!currentText.includes(disabledMarker)) {
                    option.textContent += disabledMarker;
                }
                changes.push({ value: option.value, action: 'disabled' });
            } else if (isKeySet && isDisabled) {
                option.disabled = false;
                option.textContent = currentText.replace(disabledMarker, "").trim();
                changes.push({ value: option.value, action: 'enabled' });
            }
        }
    }
    const signature = JSON.stringify({
        keyStatus: currentKeyStatus,
        disabledState: Array.from(options).map(opt => ({ value: opt.value, disabled: opt.disabled }))
    });

    if (changes.length) {
        initLogger.info("API dropdown state updated.", { changes, keyStatus: currentKeyStatus });
    } else if (initLogger.isDebugEnabled && initLogger.isDebugEnabled() && signature !== lastApiDropdownSignature) {
        initLogger.debug("API dropdown evaluated with no state changes.", { keyStatus: currentKeyStatus });
    }
    lastApiDropdownSignature = signature;
}
window.updateApiDropdownState = updateApiDropdownState;

async function fetchReadinessData() {
    if (!window.IS_MULTI_USER) {
        initLogger.debug("Single-user mode: Assuming readiness.");
        return {
            api_keys: { openai: true, assemblyai: true, gemini: true },
            permissions: {
                use_api_assemblyai: true, use_api_openai_whisper: true, use_api_openai_gpt_4o_transcribe: true,
                use_api_google_gemini: true,
                allow_large_files: true, allow_context_prompt: true, allow_download_transcript: true,
                allow_workflows: true,
                manage_workflow_templates: true,
                allow_auto_title_generation: true,
                allow_speaker_diarization: true,
            },
            limits: {},
            usage: {}
        };
    }

    const now = Date.now();
    if (readinessCache && (now - readinessCacheTimestamp < READINESS_CACHE_DURATION_MS)) {
        if (!hasLoggedReadinessCacheHit) {
            initLogger.debug("Returning cached readiness data.");
            hasLoggedReadinessCacheHit = true;
        }
        return Promise.resolve(readinessCache);
    }

    if (isFetchingReadiness && pendingReadinessPromise) {
        if (!hasLoggedReadinessFetchInProgress) {
            initLogger.debug("Readiness data fetch already in progress. Returning existing promise.");
            hasLoggedReadinessFetchInProgress = true;
        }
        return pendingReadinessPromise;
    }

    initLogger.debug("Fetching fresh readiness data (cache expired or not set).");
    isFetchingReadiness = true;
    hasLoggedReadinessCacheHit = false;
    hasLoggedReadinessFetchInProgress = false;

    pendingReadinessPromise = fetch('/api/user/readiness', {
        method: 'GET',
        headers: { 'Accept': 'application/json', 'X-CSRFToken': window.csrfToken }
    })
    .then(async response => {
        if (response.status === 401) {
            initLogger.warn("Readiness check failed (401 Unauthorized). User likely logged out.");
            window.showNotification('Session expired. Please log in.', 'warning', 4000, false);
            setTimeout(() => { window.location.href = '/login'; }, 2000);
            throw new Error('Unauthorized (401)');
        }
        if (!response.ok) {
            initLogger.error(`Error fetching readiness data: ${response.status} ${response.statusText}`);
            throw new Error(`HTTP error ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        readinessCache = data;
        readinessCacheTimestamp = Date.now();
        window.API_KEY_STATUS = data.api_keys || {};
        window.USER_PERMISSIONS = data.permissions || {};
        initLogger.debug("Fresh readiness data fetched and cached.", data);
        if (READINESS_DIAGNOSTICS_ENABLED) {
            initLogger.debug("DIAGNOSTIC_LOG: Received readiness data from backend:", JSON.stringify(data, null, 2));
        }
        isFetchingReadiness = false;
        pendingReadinessPromise = null;
        hasLoggedReadinessFetchInProgress = false;
        hasLoggedReadinessCacheHit = false;
        hasLoggedReadinessAttempt = false;
        return data;
    })
    .catch(error => {
        initLogger.error('Error fetching readiness data:', error.message);
        isFetchingReadiness = false;
        pendingReadinessPromise = null;
        hasLoggedReadinessFetchInProgress = false;
        throw error;
    });

    return pendingReadinessPromise;
}
window.fetchReadinessData = fetchReadinessData;

async function checkTranscribeButtonState() {
    const apiSelect = document.getElementById('apiSelect');
    const fileInput = document.getElementById('audioFile');
    const contextPromptInput = document.getElementById('contextPrompt');
    const contextPromptSection = document.getElementById('contextPromptSection');
    const transcribeBtn = document.getElementById('transcribeBtn');
    const statusSpan = document.getElementById('transcribeBtnStatus');
    const toggleContextPromptBtn = document.getElementById('toggleContextPromptBtn');

    const requiredElements = { transcribeBtn, apiSelect, fileInput };
    const presentRequiredCount = Object.values(requiredElements).filter(Boolean).length;
    if (presentRequiredCount === 0) {
        return false;
    }
    if (presentRequiredCount !== Object.keys(requiredElements).length) {
        const missingElements = Object.entries(requiredElements)
            .filter(([, element]) => !element)
            .map(([name]) => name)
            .join(', ');
        initLogger.warn(`Required elements for transcribe button check not found: ${missingElements}.`);
        return false;
    }

    transcribeBtn.disabled = true;
    if (statusSpan) { 
        statusSpan.innerHTML = '';
        statusSpan.className = 'mt-2 text-xs text-red-600 text-center'; 
    }


    if (initLogger.isDebugEnabled && initLogger.isDebugEnabled() && !hasLoggedReadinessAttempt) {
        initLogger.debug("checkTranscribeButtonState: Attempting to get readiness data...");
        hasLoggedReadinessAttempt = true;
    }
    let readinessData;
    try {
        readinessData = await fetchReadinessData();
    } catch (error) {
        initLogger.error("Failed to get readiness data in checkTranscribeButtonState:", error.message);
        if (statusSpan) statusSpan.textContent = 'Could not verify user status.';
        if (contextPromptSection) contextPromptSection.classList.add('hidden');
        if (toggleContextPromptBtn) toggleContextPromptBtn.classList.add('hidden');
        return false;
    }

    if (!readinessData) {
        if (statusSpan) statusSpan.textContent = 'Could not verify user status.';
        if (contextPromptSection) contextPromptSection.classList.add('hidden');
        if (toggleContextPromptBtn) toggleContextPromptBtn.classList.add('hidden');
        return false;
    }

    const apiKeys = readinessData.api_keys || {};
    const permissions = readinessData.permissions || {};
    const limits = readinessData.limits || {};
    const usage = readinessData.usage || {};

    if (window.IS_MULTI_USER) {
        updateApiDropdownState(apiKeys); 
    }

    const selectedApiValue = apiSelect.value;
    const selectedApiOption = apiSelect.selectedOptions[0];
    const apiKeyRequired = selectedApiOption ? selectedApiOption.dataset.keyRequired : null;
    const isFileSelected = fileInput.files.length > 0;
    updateSpeakerDiarizationVisibility(selectedApiValue, permissions);

    if (toggleContextPromptBtn) {
        const currentPermissions = readinessData.permissions || {};
        const hasContextPermission = currentPermissions.allow_context_prompt === true;
        const supportsContextPrompt = CONTEXT_PROMPT_SUPPORTED_APIS.includes(selectedApiValue);
        const canShowContextPromptButton = hasContextPermission && supportsContextPrompt;

        if (lastContextPromptVisibility !== canShowContextPromptButton) {
            lastContextPromptVisibility = canShowContextPromptButton;
            const meta = { api: selectedApiValue, hasContextPermission, supportsContextPrompt };
            if (canShowContextPromptButton) {
                initLogger.info("Context Prompt controls shown.", meta);
            } else {
                initLogger.info("Context Prompt controls hidden.", meta);
            }
        }

        if (canShowContextPromptButton) {
            toggleContextPromptBtn.classList.remove('hidden');
            toggleContextPromptBtn.style.display = 'inline-flex'; 
        } else {
            toggleContextPromptBtn.classList.add('hidden');
            toggleContextPromptBtn.style.display = 'none';
            if (contextPromptSection) {
                contextPromptSection.classList.add('hidden');
                if (contextPromptInput) {
                    contextPromptInput.value = '';
                }
            }
            toggleContextPromptBtn.innerHTML = '<i class="material-icons left tiny -ml-0.5 mr-1.5">add_circle_outline</i>Context Prompt';
        }
    }

    let disableReason = '';
    let isPermissionError = false;

    if (!selectedApiValue) {
        disableReason = "No transcription models available or selected.";
        isPermissionError = true;
    }

    if (!disableReason && window.IS_MULTI_USER && apiKeyRequired && !(apiKeys[apiKeyRequired])) {
        const apiName = window.API_NAME_MAP_FRONTEND[selectedApiValue] || selectedApiValue;
        disableReason = `ERROR: ${apiName} API key not configured.`;
    }

    if (!disableReason) {
        let canUseSelectedApi = false;
        if (selectedApiValue === 'gpt-4o-transcribe') canUseSelectedApi = permissions.use_api_openai_gpt_4o_transcribe;
        else if (selectedApiValue === 'whisper') canUseSelectedApi = permissions.use_api_openai_whisper;
        else if (selectedApiValue === 'assemblyai') canUseSelectedApi = permissions.use_api_assemblyai;

        if (!canUseSelectedApi || (selectedApiOption && selectedApiOption.disabled)) {
            const apiName = window.API_NAME_MAP_FRONTEND[selectedApiValue] || selectedApiValue;
            disableReason = `Permission denied for ${apiName} API.`;
            isPermissionError = true;
        }
    }

    if (contextPromptSection && !contextPromptSection.classList.contains('hidden') && contextPromptInput) {
        if (!disableReason && contextPromptInput.value.trim() !== '') {
            const words = contextPromptInput.value.match(/\S+/g) || [];
            if (words.length > 120) {
                disableReason = 'ERROR: Context Prompt exceeds 120 words.';
            }
        }
    }

    if (!disableReason && isFileSelected) {
        const file = fileInput.files[0];
        const fileSizeMB = file.size / (1024 * 1024);
        if (fileSizeMB > LARGE_FILE_THRESHOLD_MB && !permissions.allow_large_files) {
            disableReason = `File exceeds ${LARGE_FILE_THRESHOLD_MB}MB limit. Permission denied.`;
            isPermissionError = true;
        }
    }

    if (!disableReason && window.IS_MULTI_USER) {
        if (limits.max_transcriptions_total > 0 && usage.total_transcriptions >= limits.max_transcriptions_total) {
            disableReason = `Total transcription limit (${limits.max_transcriptions_total}) reached.`;
        }
        if (!disableReason && limits.max_minutes_total > 0 && usage.total_minutes >= limits.max_minutes_total) {
            disableReason = `Total audio time limit (${window.formatMinutesSimple(limits.max_minutes_total)}) reached.`;
        }
        if (!disableReason && limits.max_transcriptions_monthly > 0 && usage.monthly_transcriptions >= limits.max_transcriptions_monthly) {
            disableReason = `Monthly transcription limit (${limits.max_transcriptions_monthly}) reached.`;
        }
        if (!disableReason && limits.max_minutes_monthly > 0 && usage.monthly_minutes >= limits.max_minutes_monthly) {
            disableReason = `Monthly audio time limit (${window.formatMinutesSimple(limits.max_minutes_monthly)}) reached.`;
        }
    }

    if (!disableReason && !isFileSelected) {
        transcribeBtn.disabled = true;
        if (statusSpan) statusSpan.innerHTML = '';
        logTranscribeButtonState(false, "no-file-selected", { api: selectedApiValue, contextVisible: Boolean(contextPromptSection && !contextPromptSection.classList.contains('hidden')) });
        return false;
    }

    if (disableReason) {
        transcribeBtn.disabled = true;
        if (statusSpan) {
            const isApiKeyError = disableReason.toLowerCase().includes('api key not configured');
            if (!isPermissionError && !isApiKeyError) {
                let translatedReason = { message: window.escapeHtml(disableReason), iconColorClass: 'text-red-600' };
                if (typeof window.translateBackendErrorMessage === 'function') {
                    translatedReason = window.translateBackendErrorMessage(disableReason, 0, 0, '', '');
                } else {
                    initLogger.warn("translateBackendErrorMessage function not found.");
                }
                statusSpan.innerHTML = translatedReason.message;
                statusSpan.className = `mt-2 text-xs ${translatedReason.iconColorClass || 'text-red-600'} text-center`;
            } else {
                statusSpan.innerHTML = '';
            }
        }
        logTranscribeButtonState(false, disableReason, { api: selectedApiValue, isPermissionError });
        return false;
    } else {
        transcribeBtn.disabled = false;
        if (statusSpan) statusSpan.innerHTML = '';
        logTranscribeButtonState(true, null, { api: selectedApiValue });
        return true;
    }
}
window.checkTranscribeButtonState = checkTranscribeButtonState;

function validateContextPrompt() {
    const contextField = document.getElementById('contextPrompt');
    const errorSpan = document.getElementById('contextPromptError');
    if (!contextField || !errorSpan) return;
    const words = contextField.value.match(/\S+/g) || [];
    const wordCount = words.length;
    const maxWords = 120;
    errorSpan.textContent = `${wordCount}/${maxWords} words`;
    if (wordCount > maxWords) {
        errorSpan.classList.add("text-red-600"); 
        errorSpan.classList.remove("text-gray-500");
        contextField.classList.add("border-red-500", "focus:border-red-500", "focus:ring-red-500"); 
        contextField.classList.remove("border-gray-300", "focus:border-primary", "focus:ring-primary");
    } else {
        errorSpan.classList.remove("text-red-600");
        errorSpan.classList.add("text-gray-500"); 
        contextField.classList.remove("border-red-500", "focus:border-red-500", "focus:ring-red-500");
        contextField.classList.add("border-gray-300", "focus:border-primary", "focus:ring-primary"); 
    }
    checkTranscribeButtonState();
}
window.validateContextPrompt = validateContextPrompt;

function updateApiKeyNotificationVisibility(keyStatus, permissions) {
    if (!window.IS_MULTI_USER) return;
    const notificationElement = document.getElementById('api-key-notification');
    const normalizedKeyStatus = (typeof keyStatus === 'object' && keyStatus !== null) ? keyStatus : {};
    const normalizedPermissions = (typeof permissions === 'object' && permissions !== null) ? permissions : {};

    const servicePermissions = [
        { key: 'openai', permitted: normalizedPermissions.use_api_openai_whisper || normalizedPermissions.use_api_openai_gpt_4o_transcribe },
        { key: 'assemblyai', permitted: normalizedPermissions.use_api_assemblyai },
        { key: 'gemini', permitted: normalizedPermissions.use_api_google_gemini }
    ];

    const permittedServices = servicePermissions.filter(service => service.permitted);
    const hasAnyPermission = permittedServices.length > 0;
    const hasKeyForPermittedService = permittedServices.some(service => Boolean(normalizedKeyStatus[service.key]));
    const shouldShow = hasAnyPermission && !hasKeyForPermittedService;
    if (shouldShow) {
        if (!notificationElement) {
            initLogger.info("Showing API key needed notification.");
            window.showNotification(
                'API Key needed. Please go to  <a href="#" onclick="openApiKeyModal(event)" class="underline text-blue-600 hover:text-blue-800">Manage API Keys</a>  to use all features.',
                'warning', 0, true, 'api-key-notification'
            );
        } else {
            initLogger.debug("API key notification should be shown, but it already exists.");
        }
    } else {
        if (notificationElement) {
            initLogger.info("Hiding API key needed notification.");
            notificationElement.style.opacity = '0';
            setTimeout(() => {
                notificationElement.remove();
            }, 500);
        } else {
            initLogger.debug("API key notification is correctly not shown as it's not needed.");
        }
    }
}
window.updateApiKeyNotificationVisibility = updateApiKeyNotificationVisibility;

function applySpeakerButtonStyles(isActive) {
    if (!speakerDiarizationBtnRef) return;
    const classesToRemove = isActive ? SPEAKER_BTN_DEFAULT_CLASSES : SPEAKER_BTN_ACTIVE_CLASSES;
    const classesToAdd = isActive ? SPEAKER_BTN_ACTIVE_CLASSES : SPEAKER_BTN_DEFAULT_CLASSES;
    speakerDiarizationBtnRef.classList.remove(...classesToRemove);
    speakerDiarizationBtnRef.classList.add(...classesToAdd);
    speakerDiarizationBtnRef.setAttribute('aria-pressed', isActive ? 'true' : 'false');
}

function setSpeakerDiarizationState(isActive) {
    isSpeakerDiarizationActive = Boolean(isActive);
    if (speakerDiarizationInputRef) {
        speakerDiarizationInputRef.value = isSpeakerDiarizationActive ? '1' : '';
    }
    applySpeakerButtonStyles(isSpeakerDiarizationActive);
}

function updateSpeakerDiarizationVisibility(selectedApi, permissions = {}) {
    if (!speakerDiarizationBtnRef) return;
    const supportsApi = selectedApi && SPEAKER_DIARIZATION_SUPPORTED_APIS.includes(selectedApi);
    const hasAssemblyPermission = permissions.use_api_assemblyai !== false;
    const hasDiarizationPermission = permissions.allow_speaker_diarization === true || (!window.IS_MULTI_USER && permissions.allow_speaker_diarization !== false);
    const shouldShow = supportsApi && hasAssemblyPermission && hasDiarizationPermission;

    if (shouldShow) {
        speakerDiarizationBtnRef.classList.remove('hidden');
    } else {
        if (!speakerDiarizationBtnRef.classList.contains('hidden')) {
            initLogger.info("Speaker diarization button hidden.", {
                api: selectedApi,
                hasAssemblyPermission,
                hasDiarizationPermission,
                supportsApi
            });
        }
        speakerDiarizationBtnRef.classList.add('hidden');
        if (isSpeakerDiarizationActive) {
            setSpeakerDiarizationState(false);
        }
    }
}
window.updateSpeakerDiarizationVisibility = updateSpeakerDiarizationVisibility;

document.addEventListener('DOMContentLoaded', function() {
    const apiSelect = document.getElementById('apiSelect');
    const contextPromptInput = document.getElementById('contextPrompt');
    const fileInput = document.getElementById('audioFile');
    const transcribeBtn = document.getElementById('transcribeBtn');
    const stopBtn = document.getElementById('stopBtn');
    const toggleContextPromptBtn = document.getElementById('toggleContextPromptBtn');
    const contextPromptSection = document.getElementById('contextPromptSection');
    const applyWorkflowBtn = document.getElementById('applyWorkflowBtn');
    const workflowModal = document.getElementById('workflowModal'); 
    const removeWorkflowBtn = document.getElementById('removeWorkflowBtn');
    speakerDiarizationBtnRef = document.getElementById('speakerDiarizationBtn');
    speakerDiarizationInputRef = document.getElementById('speakerDiarizationInput');


    if (apiSelect) {
        apiSelect.addEventListener('change', checkTranscribeButtonState);
    }
    if (contextPromptInput) {
        contextPromptInput.addEventListener('input', validateContextPrompt);
    }
    if (fileInput) {
        fileInput.addEventListener('change', function() {
            const filePathSpan = document.getElementById('audioFilePath');
            if (this.files && this.files.length > 0) {
                filePathSpan.textContent = this.files[0].name;
            } else {
                filePathSpan.textContent = filePathSpan.dataset.placeholderLong || 'Select an audio file (mp3, wav, m4a...)';
            }
            checkTranscribeButtonState();
        });
    }
    if (transcribeBtn) {
        if (typeof window.handleTranscribeSubmit === 'function') {
            transcribeBtn.addEventListener('click', window.handleTranscribeSubmit);
        } else {
            initLogger.error("handleTranscribeSubmit function not found.");
        }
    }
    if (stopBtn) {
        if (typeof window.handleStopTranscription === 'function') {
            stopBtn.addEventListener('click', window.handleStopTranscription);
        } else {
            initLogger.error("handleStopTranscription function not found.");
        }
    }

    if (toggleContextPromptBtn && contextPromptSection) {
        toggleContextPromptBtn.addEventListener('click', function() {
            const isHidden = contextPromptSection.classList.contains('hidden');
            contextPromptSection.classList.toggle('hidden', !isHidden);

            const icon = toggleContextPromptBtn.querySelector('i.material-icons');
            const textNode = Array.from(toggleContextPromptBtn.childNodes).find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim().length > 0);

            if (isHidden) { 
                if (icon) icon.textContent = 'remove_circle_outline';
                if (textNode) textNode.textContent = ' Context Prompt';
            } else { 
                if (icon) icon.textContent = 'add_circle_outline';
                if (textNode) textNode.textContent = ' Context Prompt';
                if (contextPromptInput) {
                    contextPromptInput.value = '';
                    validateContextPrompt();
                }
            }
            checkTranscribeButtonState(); 
        });
    }

    if (applyWorkflowBtn && workflowModal) {
        applyWorkflowBtn.addEventListener('click', function() {
            initLogger.info("Apply Workflow button clicked.");
            if (typeof window.Workflow !== 'undefined' && typeof window.Workflow.openWorkflowModal === 'function') {
                workflowModal.dataset.mode = 'pre-apply';
                window.Workflow.openWorkflowModal(null);
            } else {
                initLogger.error("Workflow.openWorkflowModal function not found.");
                window.showNotification('Error opening workflow selection.', 'error', 4000, false);
            }
        });
    }

    if (removeWorkflowBtn) {
        removeWorkflowBtn.addEventListener('click', function(event) {
            event.stopPropagation(); // Prevent the main button click

            const applyWorkflowBtnEl = document.getElementById('applyWorkflowBtn');
            // const applyWorkflowBtnIcon = document.getElementById('applyWorkflowBtnIcon'); // Icon doesn't change

            // Clear pending workflow hidden inputs
            const pendingTextElem = document.getElementById("pendingWorkflowPromptText");
            const pendingTitleElem = document.getElementById("pendingWorkflowPromptTitle");
            const pendingColorElem = document.getElementById("pendingWorkflowPromptColor");
            const pendingOriginPromptIdElem = document.getElementById("pendingWorkflowOriginPromptId");

            if (pendingTextElem) pendingTextElem.value = "";
            if (pendingTitleElem) pendingTitleElem.value = "";
            if (pendingColorElem) pendingColorElem.value = "";
            if (pendingOriginPromptIdElem) pendingOriginPromptIdElem.value = "";

            // Hide selected workflow info span
            const selectedInfoElem = document.getElementById("selectedWorkflowInfo");
            if (selectedInfoElem) {
                selectedInfoElem.textContent = '';
                selectedInfoElem.classList.add('hidden');
                selectedInfoElem.style.backgroundColor = '';
                selectedInfoElem.style.color = '';
            }

            // Revert button appearance
            if (applyWorkflowBtnEl) {
                applyWorkflowBtnEl.classList.remove('bg-green-600', 'text-white', 'hover:bg-green-700', 'border-green-600');
                applyWorkflowBtnEl.classList.add('bg-white', 'text-gray-700', 'hover:bg-gray-50', 'border-gray-300');
                // applyWorkflowBtnIcon.textContent = 'add_circle_outline'; // Icon does not change
                this.classList.add('hidden'); // Hide the cross
            }
            
            // Update transcribe button state if needed
            if (typeof window.checkTranscribeButtonState === 'function') {
                window.checkTranscribeButtonState();
            }
            initLogger.info("Applied workflow removed.");
            // Clear pre-apply mode from modal if it was set
            if (workflowModal && workflowModal.dataset.mode === 'pre-apply') {
                delete workflowModal.dataset.mode;
                initLogger.debug("Cleared pre-apply mode from workflow modal after removal.");
            }
        });
    }


    if (speakerDiarizationBtnRef && speakerDiarizationInputRef) {
        setSpeakerDiarizationState(false);
        speakerDiarizationBtnRef.addEventListener('click', function() {
            if (speakerDiarizationBtnRef.classList.contains('hidden')) {
                return;
            }
            const nextState = !isSpeakerDiarizationActive;
            setSpeakerDiarizationState(nextState);
            initLogger.info("Speaker diarization toggled.", { enabled: nextState });
        });
    }

    if (window.IS_MULTI_USER) {
        const initialKeys = window.API_KEY_STATUS || {};
        const initialPermissions = window.USER_PERMISSIONS || {};
        updateApiKeyNotificationVisibility(initialKeys, initialPermissions);
        checkTranscribeButtonState();
    } else {
        checkTranscribeButtonState();
    }

    initLogger.info("Event listeners attached.");
});

initLogger.info("Initialization script loaded.");
