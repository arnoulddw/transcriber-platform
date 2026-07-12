// app/static/js/history/events.js
// DOMContentLoaded wiring for history interactions.

(function initializeHistoryEvents(window) {
    const History = window.History;
    const { historyLogPrefix } = History.logger;
    const idsToPollForTitle = History.state.idsToPollForTitle;
    const titlePollAttempts = History.state.titlePollAttempts;
    const { updateHistoryEmptyState } = History.ui;

async function getFullTranscript(transcriptionItem) {
    if (!transcriptionItem) return '';
    if (transcriptionItem.dataset.contentLoaded === 'true') {
        return transcriptionItem.dataset.fullText || '';
    }
    if (!transcriptionItem._contentPromise) {
        const transcriptionId = transcriptionItem.dataset.transcriptionId;
        transcriptionItem._contentPromise = fetch(`/api/transcriptions/${transcriptionId}/content`, {
            headers: { 'Accept': 'application/json', 'X-CSRFToken': window.csrfToken || '' }
        }).then(async response => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || 'Unable to load transcript.');
            const text = data.transcription_text || '';
            transcriptionItem.dataset.fullText = text;
            transcriptionItem.dataset.contentLoaded = 'true';
            transcriptionItem.dataset.hasMore = 'false';
            return text;
        }).finally(() => {
            transcriptionItem._contentPromise = null;
        });
    }
    return transcriptionItem._contentPromise;
}
History.getFullTranscript = getFullTranscript;

document.addEventListener('DOMContentLoaded', function() {
    const clearAllBtn = document.getElementById('clearAllBtn');
    if (clearAllBtn) { clearAllBtn.addEventListener('click', window.handleClearAll); window.logger.debug(historyLogPrefix, "Clear All button listener attached."); }
    else { window.logger.debug(historyLogPrefix, "Clear All button not found on page load."); }

    const historyList = document.getElementById('transcriptionHistory');
    updateHistoryEmptyState();
    if (historyList) {
        historyList.addEventListener('click', async function(event) {
            const copyBtn = event.target.closest('.transcript-panel .copy-btn');
            const downloadBtn = event.target.closest('.transcript-panel .download-btn');
            const pinBtn = event.target.closest('.transcript-panel .pin-btn');
            const deleteBtn = event.target.closest('.transcript-panel .delete-btn');
            const transcriptionItem = event.target.closest('li[data-transcription-id]');

            if (!transcriptionItem) return;

            const transcriptionId = transcriptionItem.dataset.transcriptionId;
            const filenameElement = transcriptionItem.querySelector('.transcript-panel b');
            const filename = filenameElement ? filenameElement.textContent.trim() : 'transcription';
            const baseFilename = filename.replace(/\.[^/.]+$/, "");

            if ((copyBtn || downloadBtn) && transcriptionId) {
                try {
                    const fullText = await getFullTranscript(transcriptionItem);
                    if (copyBtn) window.copyToClipboard(fullText);
                    else window.downloadTranscription(transcriptionId, fullText, `${baseFilename}_transcription`);
                } catch (error) {
                    window.showNotification(error.message || 'Unable to load transcript.', 'error', 4000, false);
                }
                return;
            }
            if (pinBtn && transcriptionId) {
                window.togglePin(transcriptionId, transcriptionItem);
                return;
            }
            if (deleteBtn && transcriptionId) {
                window.deleteTranscription(transcriptionId, transcriptionItem);
                return;
            }
        });
        window.logger.debug(historyLogPrefix, "Transcription action listeners attached via delegation.");

        historyList.addEventListener('click', async function(event) {
            const readMoreWorkflow = event.target.closest('.read-more-workflow');
            if (readMoreWorkflow) {
                event.preventDefault();
                const resultElement = readMoreWorkflow.previousElementSibling;
                if (resultElement && resultElement.classList.contains('workflow-result-text')) {
                    const currentState = resultElement.dataset.readMoreState;
                    const fullHtml = readMoreWorkflow.dataset.fullHtml;
                    const truncatedHtml = readMoreWorkflow.dataset.truncatedHtml;

                    if (currentState === 'truncated') {
                        resultElement.innerHTML = fullHtml;
                        resultElement.dataset.readMoreState = 'full';
                        readMoreWorkflow.textContent = window.i18n.readLess || ' Read Less';
                    } else {
                        resultElement.innerHTML = truncatedHtml;
                        resultElement.dataset.readMoreState = 'truncated';
                        readMoreWorkflow.textContent = window.i18n.readMore || ' Read More';
                    }
                }
                return; 
            }

            const readMoreTranscript = event.target.closest('.read-more');
            if (readMoreTranscript) {
                event.preventDefault();
                const transcriptElement = readMoreTranscript.previousElementSibling;
                if (transcriptElement && transcriptElement.classList.contains('transcription-text')) {
                    const transcriptionItem = transcriptElement.closest('li[data-transcription-id]');
                    try {
                        const fullText = await getFullTranscript(transcriptionItem);
                        if (!fullText) return;
                        const currentState = transcriptElement.dataset.readMoreState;
                        if (currentState === 'truncated') {
                            transcriptElement.textContent = fullText;
                            transcriptElement.dataset.readMoreState = 'full';
                            readMoreTranscript.textContent = window.i18n.readLess || ' Read Less';
                        } else {
                            const words = fullText.split(/\s+/).filter(Boolean);
                            const previewLength = 140;
                            const truncatedText = words.slice(0, previewLength).join(' ') + (words.length > previewLength ? '...' : '');
                            transcriptElement.textContent = truncatedText;
                            transcriptElement.dataset.readMoreState = 'truncated';
                            readMoreTranscript.textContent = window.i18n.readMore || ' Read More';
                        }
                    } catch (error) {
                        window.showNotification(error.message || 'Unable to load transcript.', 'error', 4000, false);
                    }
                }
                return; 
            }
        });
        window.logger.debug(historyLogPrefix, "Read more listeners attached via delegation.");

        historyList.querySelectorAll('.transcription-text').forEach(el => {
            const transcriptionItem = el.closest('li[data-transcription-id]'); 
            const fullText = transcriptionItem?.dataset.fullText;
            if (fullText) {
                const words = fullText.split(/\s+/).filter(Boolean);
                const previewLength = 140;
                if (words.length > previewLength || transcriptionItem?.dataset.hasMore === 'true') {
                    const truncatedText = words.slice(0, previewLength).join(' ') + '...';
                    el.textContent = truncatedText;
                    el.dataset.readMoreState = 'truncated'; 
                    let readMoreLink = el.nextElementSibling;
                    if (!readMoreLink || !readMoreLink.classList.contains('read-more')) {
                        readMoreLink = document.createElement('a');
                        readMoreLink.href = '#!';
                        readMoreLink.className = 'read-more text-primary hover:text-primary-dark text-sm';
                        readMoreLink.style.fontSize = '0.9em'; 
                        readMoreLink.style.marginLeft = '0px';
                        el.parentNode.insertBefore(readMoreLink, el.nextSibling);
                    }
                    readMoreLink.textContent = window.i18n.readMore || ' Read More';
                } else {
                    el.textContent = fullText;
                    el.dataset.readMoreState = 'full'; 
                    let existingLink = el.nextElementSibling;
                    if (existingLink && existingLink.classList.contains('read-more')) existingLink.remove();
                }
            }
        });

        if (typeof window.applyPillStyles === 'function') {
            window.applyPillStyles('#transcriptionHistory .prompt-label-pill');
        } else {
            window.logger.error("applyPillStyles function not found in history.js");
        }

        historyList.querySelectorAll('li[data-transcription-id]').forEach(item => {
            if (item.dataset.initialPollTitle === 'true') { // Use the stored initial flag
                idsToPollForTitle.add(item.dataset.transcriptionId);
                titlePollAttempts[item.dataset.transcriptionId] = 0;
            }
        });
        window.logger.debug(historyLogPrefix, `Initial title polling list size: ${idsToPollForTitle.size}`);
        if (typeof window.startTitlePolling === 'function') {
            window.startTitlePolling();
        } else {
            window.logger.error(historyLogPrefix, "startTitlePolling function not found.");
        }
    }
});
})(window);
