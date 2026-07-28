(function (global) {
    'use strict';

    class LiveTranscriptReducer {
        constructor() {
            this.order = [];
            this.turns = new Map();
        }

        apply(event) {
            const isDelta = event.type === 'conversation.item.input_audio_transcription.delta';
            const isComplete = event.type === 'conversation.item.input_audio_transcription.completed';
            if ((!isDelta && !isComplete) || !event.item_id) return false;

            if (!this.turns.has(event.item_id)) {
                this.order.push(event.item_id);
                this.turns.set(event.item_id, { text: '', complete: false });
            }
            const turn = this.turns.get(event.item_id);
            if (isDelta && !turn.complete) {
                turn.text += event.delta || '';
            } else if (isComplete) {
                turn.text = event.transcript || turn.text;
                turn.complete = true;
            }
            return true;
        }

        entries() {
            return this.order.map((itemId) => ({
                itemId,
                ...this.turns.get(itemId),
            }));
        }

        text() {
            return this.entries()
                .map((turn) => turn.text.trim())
                .filter(Boolean)
                .join(' ');
        }

        clear() {
            this.order = [];
            this.turns.clear();
        }
    }

    global.LiveTranscriptReducer = LiveTranscriptReducer;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { LiveTranscriptReducer };
    }
    if (typeof document === 'undefined') return;

    document.addEventListener('DOMContentLoaded', () => {
        const root = document.getElementById('liveWorkspace');
        if (!root) return;

        const elements = {
            language: document.getElementById('liveLanguage'),
            microphone: document.getElementById('liveMicrophone'),
            contextToggle: document.getElementById('liveContextToggle'),
            contextPanel: document.getElementById('liveContextPanel'),
            contextPrompt: document.getElementById('liveContextPrompt'),
            contextCount: document.getElementById('liveContextCount'),
            action: document.getElementById('liveActionButton'),
            actionIcon: document.getElementById('liveActionIcon'),
            actionText: document.getElementById('liveActionText'),
            status: document.getElementById('liveStatus'),
            statusText: document.getElementById('liveStatusText'),
            timer: document.getElementById('liveTimer'),
            volume: document.getElementById('liveVolumeLevel'),
            error: document.getElementById('liveError'),
            scroll: document.getElementById('liveTranscriptScroll'),
            transcript: document.getElementById('liveTranscript'),
            empty: document.getElementById('liveEmptyState'),
            copy: document.getElementById('liveCopyButton'),
            copyText: document.getElementById('liveCopyText'),
            history: document.getElementById('liveHistoryLink'),
        };
        const labels = {
            start: root.dataset.i18nStart,
            stop: root.dataset.i18nStop,
            newSession: root.dataset.i18nNew,
            connecting: root.dataset.i18nConnecting,
            listening: root.dataset.i18nListening,
            saving: root.dataset.i18nSaving,
            saved: root.dataset.i18nSaved,
            idle: root.dataset.i18nIdle,
            empty: root.dataset.i18nEmpty,
            copy: root.dataset.i18nCopy,
            copied: root.dataset.i18nCopied,
            unsaved: root.dataset.i18nUnsaved,
            defaultMicrophone: root.dataset.i18nDefaultMicrophone,
            microphone: root.dataset.i18nMicrophone,
            words: root.dataset.i18nWords,
            unsupported: root.dataset.i18nUnsupported,
            realtimeError: root.dataset.i18nRealtimeError,
            interrupted: root.dataset.i18nInterrupted,
            noSpeech: root.dataset.i18nNoSpeech,
            copyError: root.dataset.i18nCopyError,
            requestFailed: root.dataset.i18nRequestFailed,
        };

        const reducer = new LiveTranscriptReducer();
        let state = 'idle';
        let stream = null;
        let peerConnection = null;
        let dataChannel = null;
        let audioContext = null;
        let analyser = null;
        let volumeFrame = null;
        let timerInterval = null;
        let startedAt = null;
        let sessionToken = null;
        let unsavedTranscript = false;
        let stopping = false;

        function setError(message) {
            elements.error.textContent = message || '';
            elements.error.classList.toggle('hidden', !message);
        }

        function setStatus(nextState, text) {
            state = nextState;
            elements.statusText.textContent = text;
            elements.status.classList.toggle('is-live', nextState === 'live');
        }

        function setControlsDisabled(disabled) {
            elements.language.disabled = disabled;
            elements.microphone.disabled = disabled;
            if (elements.contextPrompt) elements.contextPrompt.disabled = disabled;
            if (elements.contextToggle) elements.contextToggle.disabled = disabled;
        }

        function setAction(label, icon, disabled, isLive) {
            elements.actionText.textContent = label;
            elements.actionIcon.textContent = icon;
            elements.action.disabled = disabled;
            elements.action.classList.toggle('is-live', Boolean(isLive));
        }

        function formatElapsed(milliseconds) {
            const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
            const hours = Math.floor(totalSeconds / 3600);
            const minutes = Math.floor((totalSeconds % 3600) / 60);
            const seconds = totalSeconds % 60;
            const base = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
            return hours ? `${String(hours).padStart(2, '0')}:${base}` : base;
        }

        function startTimer() {
            startedAt = Date.now();
            elements.timer.textContent = '00:00';
            timerInterval = global.setInterval(() => {
                elements.timer.textContent = formatElapsed(Date.now() - startedAt);
            }, 1000);
        }

        function stopTimer() {
            if (timerInterval) global.clearInterval(timerInterval);
            timerInterval = null;
        }

        function shouldStickToBottom() {
            return elements.scroll.scrollHeight - elements.scroll.scrollTop - elements.scroll.clientHeight < 160;
        }

        function renderTranscript() {
            const stickToBottom = shouldStickToBottom();
            const entries = reducer.entries();
            elements.transcript.replaceChildren();
            if (!entries.some((turn) => turn.text.trim())) {
                const empty = document.createElement('p');
                empty.id = 'liveEmptyState';
                empty.className = 'live-transcript__empty';
                empty.textContent = labels.empty;
                elements.transcript.appendChild(empty);
                elements.copy.disabled = true;
                return;
            }
            entries.forEach((turn) => {
                if (!turn.text) return;
                const span = document.createElement('span');
                span.className = `live-transcript__turn${turn.complete ? '' : ' is-partial'}`;
                span.dataset.itemId = turn.itemId;
                span.textContent = turn.text;
                elements.transcript.appendChild(span);
            });
            unsavedTranscript = true;
            elements.copy.disabled = false;
            if (stickToBottom) {
                global.requestAnimationFrame(() => {
                    elements.scroll.scrollTop = elements.scroll.scrollHeight;
                });
            }
        }

        function handleRealtimeEvent(event) {
            if (reducer.apply(event)) {
                renderTranscript();
                return;
            }
            if (event.type === 'error') {
                const message = event.error && event.error.message
                    ? event.error.message
                    : labels.realtimeError;
                setError(message);
            }
        }

        async function listMicrophones() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
            const currentValue = elements.microphone.value;
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const microphones = devices.filter((device) => device.kind === 'audioinput');
                elements.microphone.replaceChildren();
                const defaultOption = new Option(labels.defaultMicrophone, '');
                elements.microphone.appendChild(defaultOption);
                microphones.forEach((device, index) => {
                    if (device.deviceId === 'default') return;
                    elements.microphone.appendChild(
                        new Option(device.label || `${labels.microphone} ${index + 1}`, device.deviceId)
                    );
                });
                if ([...elements.microphone.options].some((option) => option.value === currentValue)) {
                    elements.microphone.value = currentValue;
                }
            } catch (error) {
                console.warn('Could not enumerate microphones.', error);
            }
        }

        function startVolumeMeter(activeStream) {
            const AudioContextClass = global.AudioContext || global.webkitAudioContext;
            if (!AudioContextClass) return;
            audioContext = new AudioContextClass();
            analyser = audioContext.createAnalyser();
            analyser.fftSize = 256;
            analyser.smoothingTimeConstant = 0.75;
            audioContext.createMediaStreamSource(activeStream).connect(analyser);
            const samples = new Uint8Array(analyser.frequencyBinCount);

            const draw = () => {
                analyser.getByteFrequencyData(samples);
                const average = samples.reduce((sum, value) => sum + value, 0) / samples.length;
                const level = Math.min(1, Math.max(0, average / 90));
                elements.volume.style.transform = `scaleX(${level.toFixed(3)})`;
                volumeFrame = global.requestAnimationFrame(draw);
            };
            draw();
        }

        async function cleanupConnection() {
            if (volumeFrame) global.cancelAnimationFrame(volumeFrame);
            volumeFrame = null;
            elements.volume.style.transform = 'scaleX(0)';
            if (dataChannel) {
                dataChannel.onmessage = null;
                dataChannel.onclose = null;
                if (dataChannel.readyState !== 'closed') dataChannel.close();
            }
            dataChannel = null;
            if (peerConnection) {
                peerConnection.onconnectionstatechange = null;
                peerConnection.close();
            }
            peerConnection = null;
            if (stream) {
                stream.getTracks().forEach((track) => {
                    track.onended = null;
                    track.stop();
                });
            }
            stream = null;
            if (audioContext) await audioContext.close().catch(() => {});
            audioContext = null;
            analyser = null;
        }

        async function postJson(url, body) {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-CSRFToken': global.csrfToken,
                },
                body: JSON.stringify(body),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.error || `${labels.requestFailed} (${response.status})`);
            }
            return data;
        }

        async function startListening() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !global.RTCPeerConnection) {
                setError(labels.unsupported);
                return;
            }
            setError('');
            setStatus('connecting', labels.connecting);
            setAction(labels.connecting, 'sync', true, false);
            setControlsDisabled(true);

            try {
                const selectedDevice = elements.microphone.value;
                stream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        deviceId: selectedDevice ? { exact: selectedDevice } : undefined,
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true,
                    },
                });
                stream.getAudioTracks().forEach((track) => {
                    track.onended = () => {
                        if (!stopping && (state === 'connecting' || state === 'live')) {
                            setError(labels.interrupted);
                            stopAndSave();
                        }
                    };
                });
                await listMicrophones();
                startVolumeMeter(stream);

                peerConnection = new RTCPeerConnection();
                stream.getTracks().forEach((track) => peerConnection.addTrack(track, stream));
                dataChannel = peerConnection.createDataChannel('oai-events');
                dataChannel.onmessage = (message) => {
                    try {
                        handleRealtimeEvent(JSON.parse(message.data));
                    } catch (error) {
                        console.warn('Ignored an unreadable realtime event.', error);
                    }
                };
                dataChannel.onclose = () => {
                    if (state === 'live' && !stopping) stopAndSave();
                };
                peerConnection.onconnectionstatechange = () => {
                    if (peerConnection && peerConnection.connectionState === 'failed' && !stopping) {
                        setError(labels.interrupted);
                        stopAndSave();
                    }
                };

                const offer = await peerConnection.createOffer();
                await peerConnection.setLocalDescription(offer);
                const session = await postJson('/api/live/session', {
                    sdp: offer.sdp,
                    language_code: elements.language.value,
                    context_prompt: elements.contextPrompt ? elements.contextPrompt.value.trim() : '',
                });
                sessionToken = session.session_token;
                await peerConnection.setRemoteDescription({
                    type: 'answer',
                    sdp: session.answer_sdp,
                });

                setStatus('live', labels.listening);
                setAction(labels.stop, 'stop', false, true);
                startTimer();
            } catch (error) {
                await cleanupConnection();
                setControlsDisabled(false);
                setStatus('idle', labels.idle);
                setAction(labels.start, 'mic', false, false);
                setError(error.message);
            }
        }

        function wait(milliseconds) {
            return new Promise((resolve) => global.setTimeout(resolve, milliseconds));
        }

        async function stopAndSave() {
            if (stopping || !['live', 'connecting', 'save-error'].includes(state)) return;
            stopping = true;
            setStatus('saving', labels.saving);
            setAction(labels.saving, 'hourglass_top', true, false);
            stopTimer();

            if (dataChannel && dataChannel.readyState === 'open') {
                dataChannel.send(JSON.stringify({ type: 'input_audio_buffer.commit' }));
                await wait(800);
            }
            const transcriptText = reducer.text();
            await cleanupConnection();

            try {
                if (!transcriptText) {
                    unsavedTranscript = false;
                    sessionToken = null;
                    setStatus('idle', labels.idle);
                    setAction(labels.start, 'mic', false, false);
                    setControlsDisabled(false);
                    setError(labels.noSpeech);
                    return;
                }
                const result = await postJson('/api/live/finalize', {
                    session_token: sessionToken,
                    transcript: transcriptText,
                });
                unsavedTranscript = false;
                setStatus('saved', labels.saved);
                setAction(labels.newSession, 'add', false, false);
                elements.history.href = result.history_url || '/';
                elements.history.classList.remove('hidden');
            } catch (error) {
                setStatus('save-error', labels.idle);
                setAction(labels.stop, 'save', false, false);
                setError(error.message);
                unsavedTranscript = true;
            } finally {
                stopping = false;
            }
        }

        function resetSession() {
            reducer.clear();
            renderTranscript();
            sessionToken = null;
            unsavedTranscript = false;
            startedAt = null;
            elements.timer.textContent = '00:00';
            elements.history.classList.add('hidden');
            setError('');
            setControlsDisabled(false);
            setStatus('idle', labels.idle);
            setAction(labels.start, 'mic', false, false);
        }

        elements.action.addEventListener('click', () => {
            if (state === 'live' || state === 'connecting' || unsavedTranscript) {
                stopAndSave();
            } else if (state === 'saved') {
                resetSession();
            } else {
                startListening();
            }
        });

        if (elements.contextToggle) {
            elements.contextToggle.addEventListener('click', () => {
                const expanded = elements.contextToggle.getAttribute('aria-expanded') === 'true';
                elements.contextToggle.setAttribute('aria-expanded', String(!expanded));
                elements.contextPanel.classList.toggle('hidden', expanded);
                if (!expanded) elements.contextPrompt.focus();
            });
        }

        if (elements.contextPrompt) {
            elements.contextPrompt.addEventListener('input', () => {
                const words = elements.contextPrompt.value.trim()
                    ? elements.contextPrompt.value.trim().split(/\s+/).length
                    : 0;
                elements.contextCount.textContent = `${words}/120 ${labels.words}`;
                const overLimit = words > 120;
                elements.contextCount.classList.toggle('text-red-600', overLimit);
                elements.action.disabled = overLimit;
            });
        }

        elements.copy.addEventListener('click', async () => {
            const text = reducer.text();
            if (!text) return;
            try {
                await navigator.clipboard.writeText(text);
                elements.copyText.textContent = labels.copied;
                global.setTimeout(() => {
                    elements.copyText.textContent = labels.copy;
                }, 1600);
            } catch (error) {
                setError(labels.copyError);
            }
        });

        global.addEventListener('beforeunload', (event) => {
            if (unsavedTranscript || state === 'live' || state === 'connecting' || state === 'saving') {
                event.preventDefault();
                event.returnValue = labels.unsaved;
            }
        });
        if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
            navigator.mediaDevices.addEventListener('devicechange', () => {
                if (state === 'idle' || state === 'saved') listMicrophones();
            });
        }

        listMicrophones();
    });
})(typeof window !== 'undefined' ? window : globalThis);
