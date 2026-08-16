// app/static/js/main_poll.js
// Handles polling for transcription progress, updating the UI.

// --- Global State (Polling Specific) ---
let currentPollIntervalId = null;
let currentJobId = null;
let jobStartTime = null;
let lastMessageIndex = -1;
let jobIsFinishedOrErrored = false; // For transcription status ONLY
let currentPhase = 'upload';
let lastProgressValue = 0;
let phaseStartTime = null;
let uploadPhaseActualEndTime = null;
let processingPhaseActualEndTime = null;

const mainPollLogPrefix = "[MainPollJS]";

// --- Constants ---
const HOLD_PROGRESS_AT = 95;
const MIN_PHASE_DURATION_FOR_SMOOTHING = 0.5;

// Expose function to set the finished/errored flag (used by main_init.js on submission error)
function setJobFinishedOrErrored(value) {
    jobIsFinishedOrErrored = value;
}
window.setJobFinishedOrErrored = setJobFinishedOrErrored;

function setProgressBarWidth(progressBarElement, value) {
    if (!progressBarElement) {
        return;
    }
    const normalizedValue = typeof value === 'number' ? `${value}%` : value;
    progressBarElement.style.setProperty('--progress', normalizedValue);
}

function scheduleFinalUiReset(jobId, delayMs = 5000) {
    setTimeout(() => {
        const progressContainer = document.getElementById('progressContainer');
        if (progressContainer && currentJobId === jobId && jobIsFinishedOrErrored) {
            resetTranscribeUI();
        }
    }, delayMs);
}

/**
 * Utility to convert seconds into a compact display string (e.g., "23m 20s").
 */
function formatSecondsForDisplay(seconds) {
    const parsed = Number(seconds);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        return null;
    }
    const rounded = Math.round(parsed);
    let remaining = rounded;
    const hours = Math.floor(remaining / 3600);
    remaining -= hours * 3600;
    const minutes = Math.floor(remaining / 60);
    const secs = remaining - minutes * 60;
    const parts = [];
    if (hours) parts.push(`${hours}h`);
    if (minutes) parts.push(`${minutes}m`);
    if (secs || parts.length === 0) parts.push(`${secs}s`);
    return parts.join(' ');
}


/**
 * Updates the progress activity display (icon and message).
 * Allows HTML content in the message for links (e.g., API key modal link).
 * @param {string} icon - Material Icons name (e.g., 'hourglass_empty', 'check_circle').
 * @param {string} message - The message text (can include HTML).
 * @param {string} [iconColorClass=''] - Optional Tailwind color class for the icon (e.g., 'text-green-600').
 */
function updateProgressActivity(icon, message, iconColorClass = '') {
    const progressElement = document.getElementById('progressActivity');
    if (progressElement) {
        const isError = iconColorClass.includes('text-red');
        const progressContainer = document.getElementById('progressContainer');
        progressElement.classList.toggle('text-red-800', isError);
        progressElement.classList.toggle('text-blue-700', !isError);
        if (progressContainer) {
            progressContainer.classList.toggle('bg-red-50', isError);
            progressContainer.classList.toggle('border-red-200', isError);
            progressContainer.classList.toggle('bg-blue-50', !isError);
            progressContainer.classList.toggle('border-blue-200', !isError);
        }
        // Use innerHTML to allow the structured error content, but escape the icon.
        progressElement.innerHTML = `<i class="material-icons tiny ${iconColorClass} mr-3 mt-0.5 shrink-0">${escapeHtml(icon)}</i><div class="min-w-0 flex-1">${message}</div>`;
    }
}


/**
* Translates backend error codes/messages into user-friendly text, icons, and colors.
* Handles specific error messages to provide actionable feedback (e.g., links to API key modal).
* NOTE: This function is now primarily for ERROR translation. Progress messages are handled separately.
* @param {string} backendMessage - The raw message from the backend progress log or error.
* @returns {object} - { message: string, icon: string, iconColorClass: string }
*/
function translateBackendErrorMessage(backendMessage) {
    const lowerMessage = backendMessage ? backendMessage.toLowerCase() : "";
    let message = backendMessage || "An unknown error occurred."; // Default message
    let icon = 'error'; // Default error icon
    let iconColorClass = 'text-red-600'; // Default error color (Tailwind)
    const keyAction = window.USER_PERMISSIONS?.allow_api_key_management
        ? '<a href="#!" data-transcription-error-action="manage-key" class="text-primary hover:text-primary-dark underline">Manage API Keys</a>'
        : 'contact your administrator';

    if (lowerMessage.startsWith('error:')) {
        const errorContent = backendMessage.substring(6).trim();
        const lowerErrorContent = errorContent.toLowerCase();

        if (lowerErrorContent.includes('no api keys configured')) {
            message = `No API Keys configured. Please ${keyAction}.`;
            icon = 'vpn_key_off';
        } else if (lowerErrorContent.includes('api key not configured')) {
            let serviceNameGuess = 'The required';
            if (lowerErrorContent.includes('openai')) serviceNameGuess = 'OpenAI';
            else if (lowerErrorContent.includes('assemblyai')) serviceNameGuess = 'AssemblyAI';
            else if (lowerErrorContent.includes('gemini')) serviceNameGuess = 'Gemini';
            message = `${serviceNameGuess} API key is not configured. Please ${keyAction}.`;
            icon = 'vpn_key_off';
        } else if (lowerErrorContent.includes('permission denied')) {
            const permissionMatch = errorContent.match(/Permission denied(?::\s*(.*))?/i);
            message = permissionMatch && permissionMatch[1] ? `Permission denied: ${escapeHtml(permissionMatch[1].trim())}` : "Permission denied to perform this action.";
            icon = 'lock_outline';
        } else if (lowerErrorContent.includes('usage limit exceeded')) {
            const limitMatch = errorContent.match(/Usage limit exceeded(?::\s*(.*))?/i);
            message = limitMatch && limitMatch[1] ? `Usage limit exceeded: ${escapeHtml(limitMatch[1].trim())}` : "Usage limit exceeded.";
            icon = 'block';
        } else if (lowerErrorContent.includes('api quota exceeded')) {
            const providerMatch = errorContent.match(/^(.*?)\s+API quota exceeded/i);
            const providerName = providerMatch ? escapeHtml(providerMatch[1]) : 'The API provider';
            message = `${providerName} quota exceeded. Please check your plan/billing with the provider.`;
            icon = 'account_balance_wallet';
            iconColorClass = 'text-orange-500'; // Tailwind orange
        } else if (lowerErrorContent.includes('authentication failed') || lowerErrorContent.includes('invalid api key') || lowerErrorContent.includes('incorrect api key')) {
            message = `The provider rejected your API key. Please ${keyAction}.`;
            icon = 'error';
        } else if (lowerErrorContent.includes('rate limit exceeded') || lowerErrorContent.includes('rate limit hit')) {
             message = "API rate limit hit. Please wait and try again later.";
             icon = 'history'; iconColorClass = 'text-orange-500';
        } else if (lowerErrorContent.includes('audio duration') && lowerErrorContent.includes('is longer than')) {
            const durationMatch = errorContent.match(/audio duration\s+(\d+(?:\.\d+)?)\s+seconds\s+is\s+longer\s+than\s+(\d+(?:\.\d+)?)/i);
            const maxDurationSeconds = durationMatch ? parseFloat(durationMatch[2]) : 1400;
            const currentDurationSeconds = durationMatch ? parseFloat(durationMatch[1]) : null;
            const providerLabel = lowerErrorContent.includes('gpt-4o') ? 'OpenAI GPT-4o Transcribe' : 'This model';
            const limitDisplay = formatSecondsForDisplay(maxDurationSeconds) || `${Math.floor(maxDurationSeconds / 60)} minutes`;
            const fileDisplay = formatSecondsForDisplay(currentDurationSeconds);
            const fileDetail = fileDisplay ? ` This upload is about ${fileDisplay}.` : '';
            message = `${providerLabel} only supports up to ${limitDisplay} per file.${fileDetail} Please trim the audio or switch to Whisper for longer recordings.`;
            icon = 'timer_off';
            iconColorClass = 'text-red-600';
        } else if (lowerErrorContent.includes('could not decode audio')
            || lowerErrorContent.includes('could not read this audio file')
            || lowerErrorContent.includes('invalid audio format')
            || lowerErrorContent.includes('audio splitting failed')
            || lowerErrorContent.includes('corrupted or unsupported')) {
             message = "This audio file could not be read. It may be damaged or use an unsupported codec. Export it as MP3 or WAV, then try again.";
             icon = 'broken_image';
        } else if (lowerErrorContent.includes('connection error') || lowerErrorContent.includes('network error') || lowerErrorContent.includes('could not connect')) {
             message = "Connection error communicating with the transcription service. Please check your internet connection.";
             icon = 'wifi_off'; iconColorClass = 'text-orange-500';
        } else if (lowerErrorContent.includes('service unavailable') || lowerErrorContent.includes('server error') || errorContent.includes('503')) {
             message = "The external transcription service is temporarily unavailable. Please try again later.";
             icon = 'cloud_off'; iconColorClass = 'text-orange-500';
        } else if (lowerErrorContent.includes('chunk transcription failed') || lowerErrorContent.includes('failed exporting audio chunk')) {
             message = "Part of the transcription failed. The result might be incomplete.";
             icon = 'warning'; iconColorClass = 'text-orange-500';
        } else if (lowerErrorContent.includes('transcription failed via api client')) {
             message = "Transcription failed. Please check the API service status or your API key.";
             icon = 'error_outline';
        } else if (lowerErrorContent.includes('context prompt exceeds 120 words')) {
            message = "Context Prompt is too long (max 120 words).";
            icon = 'warning'; iconColorClass = 'text-red-600';
        } else { 
            message = `An unexpected error occurred: ${escapeHtml(errorContent)}`;
            window.logger.warn(mainPollLogPrefix, "Unhandled backend error message:", backendMessage);
        }
    }
    else if (lowerMessage.includes("cancelled") || lowerMessage.includes("cancelling")) {
        message = "Transcription cancelled by user.";
        icon = 'cancel'; iconColorClass = 'text-orange-500';
    }
    else if (lowerMessage.includes("transcription completed") || lowerMessage.includes("finalized job") || lowerMessage.includes("transcription successful")) {
        message = "Transcription completed successfully!";
        icon = 'check_circle'; iconColorClass = 'text-green-600'; // Tailwind green
    }
    else {
        message = backendMessage;
        icon = 'info_outline'; iconColorClass = 'text-blue-600'; // Tailwind info blue
    }

    return { message, icon, iconColorClass };
}
window.translateBackendErrorMessage = translateBackendErrorMessage;

function deriveDiagnosticCode(errorMessage) {
    const text = String(errorMessage || '').toUpperCase();
    if (text.includes('WORKER_INTERRUPTED')) return 'WORKER_INTERRUPTED';
    if (text.includes('API KEY NOT CONFIGURED') || text.includes('NO API KEYS CONFIGURED')) return 'MISSING_API_KEY';
    if (text.includes('AUTHENTICATION') || text.includes('INVALID API KEY') || text.includes('INCORRECT API KEY')) return 'PROVIDER_AUTH';
    if (text.includes('QUOTA')) return 'PROVIDER_QUOTA';
    if (text.includes('RATE LIMIT')) return 'PROVIDER_RATE_LIMIT';
    if (text.includes('DECODE')
        || text.includes('AUDIO FORMAT')
        || text.includes('COULD NOT READ THIS AUDIO FILE')
        || text.includes('AUDIO SPLITTING FAILED')
        || text.includes('CORRUPTED OR UNSUPPORTED')
        || (text.includes('INVALID_VALUE') && text.includes('AUDIO'))) return 'INVALID_AUDIO';
    if (text.includes('CONNECTION') || text.includes('NETWORK')) return 'PROVIDER_CONNECTION';
    return 'TRANSCRIPTION_FAILED';
}

function downloadTranscriptionDiagnostics() {
    const diagnostics = window.lastTranscriptionDiagnostics;
    if (!diagnostics) return;
    const blob = new Blob([JSON.stringify(diagnostics, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `transcription-diagnostic-${diagnostics.reference || 'unknown'}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

function focusModelPicker() {
    const picker = document.getElementById('apiSelect');
    if (picker) {
        picker.focus();
        picker.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
}

function retrySelectedFile() {
    const fileInput = document.getElementById('audioFile');
    if (!fileInput?.files?.length || typeof window.handleTranscribeSubmit !== 'function') return;
    resetPollingState();
    window.handleTranscribeSubmit();
}

function chooseAnotherFile() {
    document.getElementById('audioFile')?.click();
}

function dismissTranscriptionError() {
    resetTranscribeUI(false, false);
}

function renderActionableError(errorMessage, jobData = {}) {
    const technicalMessage = String(errorMessage || 'An unknown error occurred.').replace(/^ERROR:\s*/i, '');
    const translated = translateBackendErrorMessage(`ERROR: ${technicalMessage}`);
    const reference = String(jobData.job_id || '').slice(0, 8) || 'not-created';
    const diagnosticCode = deriveDiagnosticCode(technicalMessage);
    window.lastTranscriptionDiagnostics = {
        code: diagnosticCode,
        reference,
        job_id: jobData.job_id || null,
        status: jobData.status || 'error',
        provider: jobData.api_used || null,
        filename: jobData.filename || null,
        technical_message: technicalMessage,
        captured_at: new Date().toISOString(),
    };

    const actionButton = (action, label, primary = false) => (
        `<button type="button" data-transcription-error-action="${action}" class="inline-flex min-h-[40px] w-full sm:w-auto items-center justify-center rounded-md border px-3 py-2 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-offset-2 ${primary ? 'border-red-700 bg-red-700 text-white hover:bg-red-800 focus:ring-red-600' : 'border-red-300 bg-white text-red-800 hover:bg-red-100 focus:ring-red-500'}">${label}</button>`
    );
    const actions = [];
    if (diagnosticCode === 'INVALID_AUDIO') {
        actions.push(actionButton('select-file', 'Choose another file', true));
        actions.push(actionButton('provider', 'Change provider'));
    } else {
        if (document.getElementById('audioFile')?.files?.length) {
            actions.push(actionButton('retry', 'Retry', true));
        }
        actions.push(actionButton('provider', 'Change provider'));
    }
    if (['MISSING_API_KEY', 'PROVIDER_AUTH'].includes(diagnosticCode)
        && window.USER_PERMISSIONS?.allow_api_key_management
        && typeof window.openApiKeyModal === 'function') {
        actions.push(actionButton('manage-key', 'Update API key', actions.length === 0));
    }
    actions.push(actionButton('dismiss', 'Dismiss'));

    return {
        icon: translated.icon,
        iconColorClass: translated.iconColorClass,
        message: `<div class="w-full text-left"><div class="font-medium">${translated.message}</div><div class="mt-1 text-xs text-red-700">Code: ${escapeHtml(diagnosticCode)} · Reference: ${escapeHtml(reference)}</div><div class="mt-3 grid grid-cols-1 gap-2 sm:flex sm:flex-wrap">${actions.join('')}</div><details class="mt-3 text-xs"><summary class="cursor-pointer font-medium">Technical details</summary><code class="mt-2 block break-words rounded bg-white/70 p-2">${escapeHtml(technicalMessage)}</code><button type="button" data-transcription-error-action="diagnostics" class="mt-2 min-h-[36px] underline font-medium">Download diagnostics</button></details></div>`,
    };
}

window.downloadTranscriptionDiagnostics = downloadTranscriptionDiagnostics;
window.focusModelPicker = focusModelPicker;
window.retrySelectedFile = retrySelectedFile;
window.chooseAnotherFile = chooseAnotherFile;
window.dismissTranscriptionError = dismissTranscriptionError;
window.renderActionableError = renderActionableError;

if (!window.transcriptionErrorActionsBound) {
    window.transcriptionErrorActionsBound = true;
    document.addEventListener('click', event => {
        const actionElement = event.target.closest('[data-transcription-error-action]');
        if (!actionElement) return;
        event.preventDefault();
        const action = actionElement.dataset.transcriptionErrorAction;
        if (action === 'retry') retrySelectedFile();
        else if (action === 'select-file') chooseAnotherFile();
        else if (action === 'provider') focusModelPicker();
        else if (action === 'manage-key') openApiKeyModal(event);
        else if (action === 'diagnostics') downloadTranscriptionDiagnostics();
        else if (action === 'dismiss') dismissTranscriptionError();
    });
}


/**
* Polls the backend for transcription job progress and updates the UI accordingly.
*/
function pollProgress(jobId) {
    const progressBar = document.getElementById('progressBar');
    const progressPercentage = document.getElementById('progressPercentage');
    let pollIntervalMs = 1000;
    let errorCount = 0;
    const maxErrors = 5;

    if (currentPollIntervalId) clearTimeout(currentPollIntervalId);

    if (currentJobId !== jobId || !jobStartTime) {
        resetPollingState(); 
        currentJobId = jobId;
        jobStartTime = Date.now();
        phaseStartTime = jobStartTime;
        jobIsFinishedOrErrored = false;
        currentPhase = 'upload';
        lastProgressValue = 0;
        uploadPhaseActualEndTime = null;
        processingPhaseActualEndTime = null;
        window.logger.info(mainPollLogPrefix, `Polling started for Transcription Job ID: ${jobId} at ${new Date(jobStartTime).toLocaleTimeString()}`);
    }

    const scheduleNextPoll = delayMs => {
        if (!jobIsFinishedOrErrored && currentJobId === jobId) {
            currentPollIntervalId = setTimeout(pollOnce, delayMs);
        }
    };

    const pollOnce = async () => {
        currentPollIntervalId = null;
        if (document.hidden) {
            scheduleNextPoll(5000);
            return;
        }
        if (jobIsFinishedOrErrored) {
             clearTimeout(currentPollIntervalId);
             currentPollIntervalId = null;
             window.logger.info(mainPollLogPrefix, `Polling stopped for Job ID: ${jobId}. Reason: Transcription finished/errored/cancelled.`);
             setTimeout(() => {
                 const progressContainer = document.getElementById('progressContainer');
                 if (progressContainer && currentJobId === jobId && jobIsFinishedOrErrored) {
                     resetTranscribeUI();
                 }
             }, 5000);
             return;
        }

        if (currentJobId !== jobId) {
            clearTimeout(currentPollIntervalId);
            currentPollIntervalId = null;
            window.logger.info(mainPollLogPrefix, `Polling stopped for Job ID: ${jobId}. Reason: New Job Started.`);
            return;
        }

        try {
            const response = await fetch('/api/progress/' + jobId, {
                 headers: { 'Accept': 'application/json', 'X-CSRFToken': window.csrfToken }
            });

            if (response.status === 401) throw new Error('Authentication required (401)');
            if (response.status === 403) throw new Error('Access denied to job (403)');
            if (response.status === 404) throw new Error('Job not found (404)');
            if (!response.ok) throw new Error(`Polling failed: ${response.statusText} (${response.status})`);

            const jobData = await response.json();
            errorCount = 0;

            if (!jobData || jobData.job_id !== currentJobId) {
                window.logger.warn(mainPollLogPrefix, `Received invalid or mismatched progress data for job ID ${currentJobId}. Stopping poll.`);
                jobIsFinishedOrErrored = true;
                updateProgressActivity('error', 'Error receiving progress updates.', 'text-red-600');
                resetTranscribeUI(true, true);
                return;
            }

            const currentJobFileSizeMB = jobData.file_size_mb || 0.0;
            const currentJobApiName = formatApiLabel(jobData.api_used, jobData.api_model);
            const currentJobFilename = jobData.filename || 'unknown';

            const transcriptionStatus = jobData.status;
            const progressLog = jobData.progress || [];
            const now = Date.now();
            const elapsedTimeTotal = (now - jobStartTime) / 1000;
            const isTerminalStatus = ['finished', 'error', 'cancelled', 'interrupted'].includes(transcriptionStatus);
            const isCancellationPending = !isTerminalStatus && (
                window.cancellationRequestedForJobId === jobId || transcriptionStatus === 'cancelling'
            );

            if (currentPhase === 'waiting' && transcriptionStatus === 'processing') {
                currentPhase = 'upload';
                phaseStartTime = now;
            }

            if (!jobIsFinishedOrErrored && !isCancellationPending && progressLog.length > lastMessageIndex + 1) {
                const newMessages = progressLog.slice(lastMessageIndex + 1);
                newMessages.forEach(msg => {
                    const upperMsg = msg.toUpperCase();
                    if (currentPhase === 'upload' && upperMsg.includes("PHASE_MARKER:UPLOAD_COMPLETE")) {
                        window.logger.info(mainPollLogPrefix, "Phase transition: Upload -> Processing/Transcribing");
                        uploadPhaseActualEndTime = now;
                        const threshold = typeof LARGE_FILE_THRESHOLD_MB !== 'undefined' ? LARGE_FILE_THRESHOLD_MB : 25;
                        const needsProcessing = (currentJobFileSizeMB > threshold);
                        currentPhase = needsProcessing ? 'processing' : 'transcribing';
                        phaseStartTime = now;
                        lastProgressValue = progressBoundaries.upload;
                    } else if (currentPhase === 'processing' && upperMsg.includes("PHASE_MARKER:TRANSCRIPTION_START")) {
                        window.logger.info(mainPollLogPrefix, "Phase transition: Processing -> Transcribing");
                        processingPhaseActualEndTime = now;
                        currentPhase = 'transcribing';
                        phaseStartTime = now;
                        lastProgressValue = progressBoundaries.processing;
                    }
                });
                lastMessageIndex = progressLog.length - 1;
            }

            let progress = 0;
            const upBoundary = progressBoundaries.upload;
            const procBoundary = progressBoundaries.processing;
            const transStartBoundary = progressBoundaries.transcriptionStart;
            if (isCancellationPending) {
                progress = lastProgressValue; currentPhase = 'cancelling';
            } else if (transcriptionStatus === 'pending') {
                progress = 0; currentPhase = 'waiting';
            } else if (transcriptionStatus === 'finished') {
                progress = 100; currentPhase = 'finished';
            } else if (transcriptionStatus === 'error' || transcriptionStatus === 'cancelled' || transcriptionStatus === 'interrupted') {
                progress = lastProgressValue; currentPhase = transcriptionStatus;
            } else {
                const elapsedTimeInPhase = (now - phaseStartTime) / 1000;
                switch (currentPhase) {
                    case 'upload':
                        const expectedUpload = expectedTimes.upload;
                        progress = (expectedUpload < MIN_PHASE_DURATION_FOR_SMOOTHING || expectedUpload <= 0) ? upBoundary : Math.min((elapsedTimeInPhase / expectedUpload) * upBoundary, upBoundary);
                        break;
                    case 'processing':
                        const expectedProcessing = expectedTimes.processing;
                        const processingRange = procBoundary - upBoundary;
                        progress = (expectedProcessing < MIN_PHASE_DURATION_FOR_SMOOTHING || expectedProcessing <= 0) ? procBoundary : upBoundary + Math.min((elapsedTimeInPhase / expectedProcessing) * processingRange, processingRange);
                        break;
                    case 'transcribing':
                        const expectedTranscription = expectedTimes.transcription;
                        const transcriptionRange = HOLD_PROGRESS_AT - transStartBoundary;
                        progress = (expectedTranscription < MIN_PHASE_DURATION_FOR_SMOOTHING || expectedTranscription <= 0) ? HOLD_PROGRESS_AT : transStartBoundary + Math.min((elapsedTimeInPhase / expectedTranscription) * transcriptionRange, transcriptionRange);
                        break;
                    default: progress = lastProgressValue;
                }
            }
            progress = Math.max(0, Math.min(100, Math.round(progress)));
            jobIsFinishedOrErrored = isTerminalStatus;
            if (!jobIsFinishedOrErrored && !isCancellationPending) {
                progress = Math.max(progress, lastProgressValue);
            }
            lastProgressValue = progress;

            if (progressBar && progressPercentage) {
                setProgressBarWidth(progressBar, progress);
                progressPercentage.textContent = progress + '%';
            }

            let activityIcon = 'hourglass_empty';
            let activityMessage = 'Processing...';
            let activityColor = ''; // Tailwind color class
            if (isCancellationPending) {
                activityIcon = 'cancel'; activityMessage = 'Cancellation requested. Waiting for process to stop...'; activityColor = 'text-orange-500';
            } else if (currentPhase === 'waiting') {
                activityIcon = 'hourglass_empty'; activityMessage = 'Waiting for an available transcription slot...'; activityColor = 'text-blue-600';
            } else if (currentPhase === 'upload') {
                activityIcon = 'cloud_upload'; activityMessage = `Uploading audio for ${escapeHtml(currentJobApiName)}...`;
            } else if (currentPhase === 'processing') {
                activityIcon = 'sync'; activityMessage = `Processing audio for ${escapeHtml(currentJobApiName)}...`;
            } else if (currentPhase === 'transcribing') {
                activityIcon = 'record_voice_over'; activityMessage = `Transcribing with ${escapeHtml(currentJobApiName)}...`;
            } else if (currentPhase === 'finished') {
                activityIcon = 'check_circle'; activityMessage = 'Transcription completed successfully!'; activityColor = 'text-green-600';
            } else if (currentPhase === 'error' || currentPhase === 'interrupted') {
                const backendError = jobData.error_message || "An unknown error occurred.";
                const actionableError = renderActionableError(backendError, jobData);
                activityIcon = actionableError.icon; activityMessage = actionableError.message; activityColor = actionableError.iconColorClass;
            } else if (currentPhase === 'cancelled') {
                activityIcon = 'cancel'; activityMessage = 'Transcription cancelled by user.'; activityColor = 'text-orange-500';
            }
            updateProgressActivity(activityIcon, activityMessage, activityColor);

            if (transcriptionStatus === 'finished') {
                if (currentPollIntervalId) {
                    clearTimeout(currentPollIntervalId);
                    currentPollIntervalId = null;
                    window.logger.info(mainPollLogPrefix, `Polling stopped for Job ID: ${jobId}. Reason: Transcription finished.`);
                }

                if (typeof window.invalidateReadinessCache === 'function') {
                    window.invalidateReadinessCache();
                }

                const contextField = document.getElementById('contextPrompt');
                if (contextField && typeof validateContextPrompt === 'function') { contextField.value = ""; validateContextPrompt(); }
                else if (contextField) { contextField.value = ""; }

                let permissions = window.USER_PERMISSIONS || {};
                if (typeof window.fetchReadinessData === 'function') {
                    try {
                        const freshReadiness = await window.fetchReadinessData();
                        permissions = freshReadiness?.permissions || permissions;
                    } catch (readinessError) {
                        window.logger.warn(mainPollLogPrefix, "Readiness refresh failed after completed transcription; using current permissions for history controls.", readinessError);
                    }
                }
                const canDownload = window.IS_MULTI_USER ? (permissions.allow_download_transcript === true) : true;
                const canRunWorkflow = window.IS_MULTI_USER ? (permissions.allow_workflows === true) : true;

                try {
                    if (typeof window.addTranscriptionToHistory === 'function') {
                        window.logger.debug(mainPollLogPrefix, `Calling addTranscriptionToHistory for job ${jobId}`);
                        const hadPendingWorkflow = jobData.result && jobData.result.pending_workflow_prompt_text && jobData.result.pending_workflow_prompt_text.trim() !== '';
                        window.addTranscriptionToHistory(
                            jobData.result,
                            canDownload,
                            canRunWorkflow,
                            true,
                            jobData.should_poll_title,
                            hadPendingWorkflow
                        );
                    } else {
                        window.logger.error(mainPollLogPrefix, "addTranscriptionToHistory function is missing. Cannot update history item.");
                    }
                } catch (renderError) {
                    window.logger.error(mainPollLogPrefix, "Transcription finished, but updating the history UI failed.", renderError);
                }
                scheduleFinalUiReset(jobId);

            } else if (transcriptionStatus === 'error' || transcriptionStatus === 'interrupted') {
                if (currentPollIntervalId) {
                    clearTimeout(currentPollIntervalId);
                    currentPollIntervalId = null;
                }
                // M.toast({ html: 'Transcription failed. See status for details.', classes: 'red', displayLength: 8000 }); // Replaced
                window.showNotification('Transcription failed. See status for details.', 'error', 8000, false);
                resetTranscribeUI(true, true);

            } else if (transcriptionStatus === 'cancelled') {
                if (currentPollIntervalId) {
                    clearTimeout(currentPollIntervalId);
                    currentPollIntervalId = null;
                }
                if (window.cancellationRequestedForJobId === jobId) {
                    window.cancellationRequestedForJobId = null;
                    window.logger.debug(mainPollLogPrefix, `Backend confirmed cancellation for ${jobId}. Frontend flag cleared.`);
                }
                scheduleFinalUiReset(jobId);
            }

        } catch (error) {
            errorCount++;
            window.logger.error(mainPollLogPrefix, `Error polling progress (Attempt ${errorCount}/${maxErrors}):`, error);

            if (error.message.includes('Authentication required') || error.message.includes('Access denied') || error.message.includes('Job not found') || errorCount >= maxErrors) {
                jobIsFinishedOrErrored = true;

                let userMessage = `Error polling status: ${escapeHtml(error.message)}`;
                if (errorCount >= maxErrors) userMessage = "Connection lost while checking status. Please check history later.";
                const toastType = error.message.includes('Authentication required') ? 'warning' : 'error';

                const progressActivityElem = document.getElementById('progressActivity');
                const isAlreadyShowingFinalState = progressActivityElem && (progressActivityElem.textContent.includes('completed') || progressActivityElem.textContent.includes('Error:') || progressActivityElem.textContent.includes('cancelled'));

                if (!isAlreadyShowingFinalState) {
                    const translatedError = translateBackendErrorMessage(`ERROR: ${userMessage}`);
                    updateProgressActivity(translatedError.icon, `Error: ${translatedError.message}`, translatedError.iconColorClass);
                } else {
                    window.logger.warn(mainPollLogPrefix, "Polling failed, but job already reached final state. Not updating activity message.");
                }

                // M.toast({ html: userMessage, classes: toastClass, displayLength: 6000 }); // Replaced
                window.showNotification(userMessage, toastType, 6000, false);
                resetTranscribeUI(true, true);

                if (error.message.includes('Authentication required')) {
                    setTimeout(() => { window.location.href = '/login'; }, 2000);
                }
            } else {
                pollIntervalMs = Math.min(pollIntervalMs + 1000, 8000);
                window.logger.warn(mainPollLogPrefix, `Polling interval increased to ${pollIntervalMs}ms due to error.`);
                const progressActivityElem = document.getElementById('progressActivity');
                const isAlreadyShowingFinalState = progressActivityElem && (progressActivityElem.textContent.includes('completed') || progressActivityElem.textContent.includes('Error:') || progressActivityElem.textContent.includes('cancelled'));
                if (!isAlreadyShowingFinalState) {
                    updateProgressActivity('sync_problem', 'Connection issue checking status. Retrying...', 'text-orange-500');
                }
            }
        }

        if (!jobIsFinishedOrErrored && currentJobId === jobId) {
            if (errorCount === 0) {
                const elapsedSeconds = (Date.now() - jobStartTime) / 1000;
                pollIntervalMs = elapsedSeconds < 10 ? 1000 : (elapsedSeconds < 60 ? 2500 : 5000);
            }
            scheduleNextPoll(pollIntervalMs);
        }
    };

    scheduleNextPoll(0);
}
window.pollProgress = pollProgress;

async function resumeActiveTranscription() {
    if (currentJobId || jobIsFinishedOrErrored) return;
    try {
        const response = await fetch('/api/transcriptions/active', {
            headers: { 'Accept': 'application/json', 'X-CSRFToken': window.csrfToken }
        });
        if (!response.ok) return;
        const jobs = await response.json();
        if (!Array.isArray(jobs) || jobs.length === 0) return;

        const job = jobs[0];
        const progressContainer = document.getElementById('progressContainer');
        const transcribeBtn = document.getElementById('transcribeBtn');
        const stopBtn = document.getElementById('stopBtn');
        if (progressContainer) progressContainer.style.display = 'block';
        if (transcribeBtn) {
            transcribeBtn.disabled = true;
            transcribeBtn.textContent = 'PROCESSING...';
        }
        if (stopBtn) {
            stopBtn.classList.remove('hidden');
            stopBtn.disabled = job.status === 'cancelling';
            if (job.status === 'cancelling') {
                stopBtn.innerHTML = 'CANCELLING... <i class="material-icons right">hourglass_empty</i>';
                window.cancellationRequestedForJobId = job.job_id;
            }
        }

        jobFilename = job.filename || 'unknown';
        jobApiName = formatApiLabel(job.api_used, job.api_model);
        if (typeof calculateExpectedProgressData === 'function') {
            const scenario = (job.file_size_mb || 0) > 25 ? 'parallel' : 'no_split';
            calculateExpectedProgressData(job.api_used, job.file_size_mb || 0, job.audio_length_minutes || 0, scenario);
        }
        pollProgress(job.job_id);
        currentJobIdForStop = job.job_id;
        updateProgressActivity(
            job.status === 'cancelling' ? 'cancel' : (job.status === 'pending' ? 'hourglass_empty' : 'record_voice_over'),
            job.status === 'cancelling'
                ? 'Cancellation requested. Waiting for process to stop...'
                : job.status === 'pending'
                ? 'Waiting for an available transcription slot...'
                : `Reconnected to transcription of ${escapeHtml(job.filename || 'audio')}...`,
            job.status === 'cancelling' ? 'text-orange-500' : 'text-blue-600'
        );
        window.logger.info(mainPollLogPrefix, `Reconnected to active transcription ${job.job_id}.`);
    } catch (error) {
        window.logger.warn(mainPollLogPrefix, 'Could not reconnect to an active transcription.', error);
    }
}
window.resumeActiveTranscription = resumeActiveTranscription;
document.addEventListener('DOMContentLoaded', resumeActiveTranscription);


/**
 * Resets the transcription UI elements (progress bar, status messages)
 * to their initial state. Optionally keeps the progress box visible.
 * Also resets the transcribe and stop buttons state.
 * @param {boolean} [keepProgressBox=false] - If true, keeps the progress box visible but resets content.
 * @param {boolean} [isErrorState=false] - If true, indicates the reset is due to an error, affecting button reset logic.
 */
function resetTranscribeUI(keepProgressBox = false, isErrorState = false) {
    const progressContainer = document.getElementById('progressContainer');
    const progressBar = document.getElementById('progressBar');
    const progressPercentage = document.getElementById('progressPercentage');
    const progressActivity = document.getElementById('progressActivity');
    const transcribeBtn = document.getElementById('transcribeBtn');
    const stopBtn = document.getElementById('stopBtn');

    const shouldHideBox = !keepProgressBox && jobIsFinishedOrErrored;

    if (shouldHideBox && progressContainer) {
        progressContainer.style.display = 'none';
    } else if (progressContainer) {
        const isCancellationPending = window.cancellationRequestedForJobId === currentJobId;
        if (isCancellationPending) {
            if (progressActivity) updateProgressActivity('cancel', 'Cancellation requested. Waiting for process to stop...', 'text-orange-500');
            if (progressBar) setProgressBarWidth(progressBar, lastProgressValue);
            if (progressPercentage) progressPercentage.textContent = `${lastProgressValue}%`;
        } else if (!jobIsFinishedOrErrored) {
            if (progressBar) setProgressBarWidth(progressBar, 0);
            if (progressPercentage) progressPercentage.textContent = '0%';
            if (progressActivity) updateProgressActivity('info_outline', 'Ready for next job.', 'text-blue-600');
        } else if (isErrorState) {
            if (progressBar) setProgressBarWidth(progressBar, lastProgressValue);
            if (progressPercentage) progressPercentage.textContent = `${lastProgressValue}%`;
        } else if (jobIsFinishedOrErrored && !isErrorState && currentPhase === 'finished') {
             if (progressBar) setProgressBarWidth(progressBar, 100);
             if (progressPercentage) progressPercentage.textContent = '100%';
        } else if (jobIsFinishedOrErrored && !isErrorState && currentPhase === 'cancelled') {
             if (progressBar) setProgressBarWidth(progressBar, lastProgressValue);
             if (progressPercentage) progressPercentage.textContent = `${lastProgressValue}%`;
        }
    }

    if (transcribeBtn) {
         transcribeBtn.disabled = false;
         transcribeBtn.innerHTML = 'TRANSCRIBE <i class="material-icons text-base ml-2">send</i>';
         if (typeof checkTranscribeButtonState === 'function') {
             checkTranscribeButtonState();
         } else {
             window.logger.error(mainPollLogPrefix, "checkTranscribeButtonState function not found.");
         }
    }
    if (stopBtn) {
        stopBtn.disabled = false;
        stopBtn.innerHTML = 'STOP <i class="material-icons right">cancel</i>';
        stopBtn.classList.add('hidden');
    }

    if (shouldHideBox) {
        resetPollingState();
    }
}
window.resetTranscribeUI = resetTranscribeUI;


/**
 * Resets global polling state variables.
 */
function resetPollingState() {
    if (currentPollIntervalId) {
        clearTimeout(currentPollIntervalId);
        currentPollIntervalId = null;
    }
    currentJobId = null;
    jobStartTime = null;
    phaseStartTime = null;
    uploadPhaseActualEndTime = null;
    processingPhaseActualEndTime = null;
    lastMessageIndex = -1;
    jobIsFinishedOrErrored = false;
    currentPhase = 'upload';
    lastProgressValue = 0;
    if (typeof window.currentJobIdForStop !== 'undefined') {
        window.currentJobIdForStop = null;
    }
    window.cancellationRequestedForJobId = null;
    window.logger.debug(mainPollLogPrefix, "Polling state reset.");
}
window.resetPollingState = resetPollingState;


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

/**
 * Checks if a string value represents meaningful content, ignoring common placeholders.
 * @param {string|null|undefined} value - The string value to check.
 * @returns {boolean} - True if the value has meaningful content, false otherwise.
 */
function hasMeaningfulContent(value) {
    if (typeof value !== 'string' || !value.trim()) {
        return false;
    }
    const lowerValue = value.trim().toLowerCase();
    const placeholders = [
        'n/a', 'null', 'undefined', '[empty result]', 'none', '-',
        'no result', 'no error', 'no prompt'
    ];
    if (placeholders.includes(lowerValue)) {
        return false;
    }
    return true;
}
window.hasMeaningfulContent = hasMeaningfulContent;
