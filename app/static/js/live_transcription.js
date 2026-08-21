(function (global) {
    'use strict';

    const MAX_SESSION_DURATION_MS = 120 * 60 * 1000;
    const OPENROUTER_CHUNK_SECONDS = 3;

    function encodePcm16Wav(samples, sampleRate) {
        const buffer = new ArrayBuffer(44 + samples.length * 2);
        const view = new DataView(buffer);
        const writeString = (offset, value) => {
            for (let index = 0; index < value.length; index += 1) {
                view.setUint8(offset + index, value.charCodeAt(index));
            }
        };
        writeString(0, 'RIFF');
        view.setUint32(4, 36 + samples.length * 2, true);
        writeString(8, 'WAVE');
        writeString(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);
        view.setUint16(22, 1, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * 2, true);
        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);
        writeString(36, 'data');
        view.setUint32(40, samples.length * 2, true);
        samples.forEach((sample, index) => {
            const clipped = Math.max(-1, Math.min(1, sample));
            view.setInt16(44 + index * 2, clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff, true);
        });
        return buffer;
    }

    function arrayBufferToBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = '';
        const chunkSize = 0x8000;
        for (let offset = 0; offset < bytes.length; offset += chunkSize) {
            binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
        }
        return global.btoa(binary);
    }

    class LiveTranscriptReducer {
        constructor() {
            this.order = [];
            this.turns = new Map();
            this.detectedLanguage = null;
        }

        apply(event) {
            const isDelta = event.type === 'conversation.item.input_audio_transcription.delta';
            const isComplete = event.type === 'conversation.item.input_audio_transcription.completed';
            if ((!isDelta && !isComplete) || !event.item_id) return false;

            if (isComplete && !this.detectedLanguage
                && Array.isArray(event.languages) && event.languages.length > 0) {
                const code = event.languages[0] && event.languages[0].code;
                if (typeof code === 'string' && code.trim()) {
                    this.detectedLanguage = code.trim().toLowerCase();
                }
            }

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
            this.detectedLanguage = null;
        }
    }

    async function createCompleteOffer(connection, timeoutMs = 2000) {
        const offer = await connection.createOffer();
        await connection.setLocalDescription(offer);
        if (connection.iceGatheringState !== 'complete') {
            await new Promise((resolve) => {
                const timeout = global.setTimeout(finish, timeoutMs);

                function finish() {
                    global.clearTimeout(timeout);
                    connection.removeEventListener('icegatheringstatechange', handleStateChange);
                    resolve();
                }

                function handleStateChange() {
                    if (connection.iceGatheringState === 'complete') finish();
                }

                connection.addEventListener('icegatheringstatechange', handleStateChange);
            });
        }
        return connection.localDescription || offer;
    }

    function waitForDataChannelOpen(channel, connection, timeoutMs, errorMessage) {
        if (channel.readyState === 'open') return Promise.resolve();
        return new Promise((resolve, reject) => {
            const timeout = global.setTimeout(() => finish(reject, new Error(errorMessage)), timeoutMs);

            function cleanup() {
                global.clearTimeout(timeout);
                channel.removeEventListener('open', handleOpen);
                channel.removeEventListener('close', handleFailure);
                channel.removeEventListener('error', handleFailure);
                connection.removeEventListener('connectionstatechange', handleConnectionState);
            }

            function finish(callback, value) {
                cleanup();
                callback(value);
            }

            function handleOpen() {
                finish(resolve);
            }

            function handleFailure() {
                finish(reject, new Error(errorMessage));
            }

            function handleConnectionState() {
                if (connection.connectionState === 'failed' || connection.connectionState === 'closed') {
                    handleFailure();
                }
            }

            channel.addEventListener('open', handleOpen);
            channel.addEventListener('close', handleFailure);
            channel.addEventListener('error', handleFailure);
            connection.addEventListener('connectionstatechange', handleConnectionState);
        });
    }

    function remainingSessionMilliseconds(startedAt, now = Date.now()) {
        return Math.max(0, MAX_SESSION_DURATION_MS - (now - startedAt));
    }

    global.LiveTranscriptReducer = LiveTranscriptReducer;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            MAX_SESSION_DURATION_MS,
            OPENROUTER_CHUNK_SECONDS,
            LiveTranscriptReducer,
            encodePcm16Wav,
            createCompleteOffer,
            remainingSessionMilliseconds,
            waitForDataChannelOpen,
        };
    }
    if (typeof document === 'undefined') return;

    document.addEventListener('DOMContentLoaded', () => {
        const root = document.getElementById('liveWorkspace');
        if (!root) return;

        const elements = {
            language: document.getElementById('liveLanguage'),
            microphone: document.getElementById('liveMicrophone'),
            model: document.getElementById('liveModel'),
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
            follow: document.getElementById('liveFollowButton'),
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
            maxDuration: root.dataset.i18nMaxDuration,
            noSpeech: root.dataset.i18nNoSpeech,
            copyError: root.dataset.i18nCopyError,
            requestFailed: root.dataset.i18nRequestFailed,
        };

        const reducer = new LiveTranscriptReducer();
        const turnElements = new Map();
        let state = 'idle';
        let stream = null;
        let peerConnection = null;
        let dataChannel = null;
        let audioContext = null;
        let analyser = null;
        let volumeFrame = null;
        let timerInterval = null;
        let sessionDeadlineTimeout = null;
        let startedAt = null;
        let sessionToken = null;
        let stopSignalSent = false;
        let unsavedTranscript = false;
        let stopping = false;
        let followingLive = true;
        let returningToLive = false;
        let liveTransport = 'openai-webrtc';
        let audioProcessor = null;
        let audioInputSource = null;
        let openrouterSamples = [];
        let openrouterSampleRate = 16000;
        let openrouterChunkTimer = null;
        let openrouterChunkSequence = 0;
        let openrouterChunkPromise = Promise.resolve();

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
            if (elements.model) elements.model.disabled = disabled;
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
            const updateTimer = () => {
                const remaining = remainingSessionMilliseconds(startedAt);
                const elapsed = Math.min(MAX_SESSION_DURATION_MS, Date.now() - startedAt);
                elements.timer.textContent = formatElapsed(elapsed);
                if (remaining === 0 && !stopping) {
                    setError(labels.maxDuration);
                    stopAndSave(true);
                }
            };
            timerInterval = global.setInterval(() => {
                updateTimer();
            }, 1000);
            sessionDeadlineTimeout = global.setTimeout(
                updateTimer,
                MAX_SESSION_DURATION_MS
            );
        }

        function stopTimer() {
            if (timerInterval) global.clearInterval(timerInterval);
            if (sessionDeadlineTimeout) global.clearTimeout(sessionDeadlineTimeout);
            timerInterval = null;
            sessionDeadlineTimeout = null;
        }

        function isAtBottom() {
            return elements.scroll.scrollHeight - elements.scroll.scrollTop - elements.scroll.clientHeight < 8;
        }

        function updateFollowButton() {
            const hasTranscript = Boolean(reducer.text());
            elements.follow.classList.toggle('hidden', !hasTranscript || followingLive);
        }

        function renderTranscript() {
            const entries = reducer.entries();
            if (!entries.some((turn) => turn.text.trim())) {
                const empty = document.createElement('p');
                empty.id = 'liveEmptyState';
                empty.className = 'live-transcript__empty';
                empty.textContent = labels.empty;
                elements.transcript.replaceChildren();
                elements.transcript.appendChild(empty);
                turnElements.clear();
                elements.copy.disabled = true;
                elements.follow.classList.add('hidden');
                followingLive = true;
                return;
            }
            const empty = document.getElementById('liveEmptyState');
            if (empty) empty.remove();
            entries.forEach((turn) => {
                if (!turn.text) return;
                let span = turnElements.get(turn.itemId);
                if (!span) {
                    span = document.createElement('span');
                    span.dataset.itemId = turn.itemId;
                    turnElements.set(turn.itemId, span);
                    elements.transcript.appendChild(span);
                }
                span.className = `live-transcript__turn${turn.complete ? '' : ' is-partial'}`;
                span.textContent = turn.text;
            });
            unsavedTranscript = true;
            elements.copy.disabled = false;
            if (followingLive) {
                global.requestAnimationFrame(() => {
                    elements.scroll.scrollTo({
                        top: elements.scroll.scrollHeight,
                        behavior: 'auto',
                    });
                    updateFollowButton();
                });
            } else {
                updateFollowButton();
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
            if (openrouterChunkTimer) global.clearInterval(openrouterChunkTimer);
            openrouterChunkTimer = null;
            if (audioProcessor) {
                audioProcessor.disconnect();
                audioProcessor.onaudioprocess = null;
            }
            audioProcessor = null;
            if (audioInputSource) audioInputSource.disconnect();
            audioInputSource = null;
            openrouterSamples = [];
            if (audioContext) await audioContext.close().catch(() => {});
            audioContext = null;
            analyser = null;
        }

        function appendOpenrouterTranscript(text, sequence) {
            if (!text) return;
            const itemId = `openrouter-${sequence}`;
            reducer.apply({
                type: 'conversation.item.input_audio_transcription.completed',
                item_id: itemId,
                transcript: text,
            });
            renderTranscript();
        }

        function flushOpenrouterChunk() {
            if (!sessionToken || !openrouterSamples.length) return;
            const samples = Float32Array.from(openrouterSamples);
            openrouterSamples = [];
            const audioData = arrayBufferToBase64(encodePcm16Wav(samples, openrouterSampleRate));
            const sequence = openrouterChunkSequence;
            openrouterChunkSequence += 1;
            openrouterChunkPromise = openrouterChunkPromise
                .then(() => postJson('/api/live/chunk', {
                    session_token: sessionToken,
                    audio_data: audioData,
                    audio_format: 'wav',
                    sequence,
                }))
                .then((result) => appendOpenrouterTranscript(result.transcript, result.sequence))
                .catch((error) => {
                    setError(error.message);
                });
        }

        function resetOpenrouterCapture() {
            openrouterChunkSequence = 0;
            openrouterChunkPromise = Promise.resolve();
            openrouterSamples = [];
        }

        function startOpenrouterCapture(activeStream) {
            if (!audioContext) throw new Error(labels.unsupported);
            audioInputSource = audioContext.createMediaStreamSource(activeStream);
            audioProcessor = audioContext.createScriptProcessor(4096, 1, 1);
            openrouterSampleRate = audioContext.sampleRate;
            audioProcessor.onaudioprocess = (event) => {
                const channel = event.inputBuffer.getChannelData(0);
                openrouterSamples.push(...channel);
                event.outputBuffer.getChannelData(0).fill(0);
            };
            audioInputSource.connect(audioProcessor);
            audioProcessor.connect(audioContext.destination);
            openrouterChunkTimer = global.setInterval(
                flushOpenrouterChunk,
                OPENROUTER_CHUNK_SECONDS * 1000,
            );
        }

        async function postJson(url, body, keepalive = false) {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-CSRFToken': global.csrfToken,
                },
                body: JSON.stringify(body),
                keepalive,
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.error || `${labels.requestFailed} (${response.status})`);
            }
            return data;
        }

        function stopAudioInput() {
            if (!stream) return;
            stream.getTracks().forEach((track) => {
                track.onended = null;
                track.stop();
            });
        }

        function requestRemoteStop(keepalive = false) {
            if (!sessionToken || stopSignalSent) return Promise.resolve();
            stopSignalSent = true;
            return postJson('/api/live/stop', {
                session_token: sessionToken,
            }, keepalive);
        }

        async function startListening() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
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

                const selectedModel = elements.model?.selectedOptions?.[0];
                const requestedTransport = selectedModel?.dataset.provider === 'openrouter'
                    || (elements.model?.value || '').includes('/');

                if (!requestedTransport && !global.RTCPeerConnection) {
                    throw new Error(labels.unsupported);
                }
                liveTransport = requestedTransport ? 'openrouter-sse' : 'openai-webrtc';
                peerConnection = requestedTransport ? null : new RTCPeerConnection();
                if (liveTransport === 'openrouter-sse') {
                    const session = await postJson('/api/live/session', {
                        sdp: '',
                        language_code: elements.language.value,
                        model: elements.model ? elements.model.value : '',
                        context_prompt: elements.contextPrompt ? elements.contextPrompt.value.trim() : '',
                    });
                    sessionToken = session.session_token;
                    stopSignalSent = false;
                    resetOpenrouterCapture();
                    startOpenrouterCapture(stream);
                    setStatus('live', labels.listening);
                    setAction(labels.stop, 'stop', false, true);
                    startTimer();
                    return;
                }
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
                    if (
                        peerConnection
                        && peerConnection.connectionState === 'failed'
                        && state === 'live'
                        && !stopping
                    ) {
                        setError(labels.interrupted);
                        stopAndSave();
                    }
                };

                const offer = await createCompleteOffer(peerConnection);
                const session = await postJson('/api/live/session', {
                    sdp: offer.sdp,
                    language_code: elements.language.value,
                    model: elements.model ? elements.model.value : '',
                    context_prompt: elements.contextPrompt ? elements.contextPrompt.value.trim() : '',
                });
                sessionToken = session.session_token;
                stopSignalSent = false;
                await peerConnection.setRemoteDescription({
                    type: 'answer',
                    sdp: session.answer_sdp,
                });
                await waitForDataChannelOpen(
                    dataChannel,
                    peerConnection,
                    10000,
                    labels.interrupted
                );

                setStatus('live', labels.listening);
                setAction(labels.stop, 'stop', false, true);
                startTimer();
            } catch (error) {
                await cleanupConnection();
                setControlsDisabled(false);
                setStatus('idle', labels.idle);
                setAction(labels.start, 'graphic_eq', false, false);
                setError(error.message);
            }
        }

        function wait(milliseconds) {
            return new Promise((resolve) => global.setTimeout(resolve, milliseconds));
        }

        async function stopAndSave(stopAudioImmediately = false) {
            if (stopping || !['live', 'connecting', 'save-error'].includes(state)) return;
            stopping = true;
            setStatus('saving', labels.saving);
            setAction(labels.saving, 'hourglass_top', true, false);
            stopTimer();
            if (stopAudioImmediately) stopAudioInput();

            if (liveTransport === 'openrouter-sse') {
                flushOpenrouterChunk();
                await openrouterChunkPromise;
            }

            if (dataChannel && dataChannel.readyState === 'open') {
                dataChannel.send(JSON.stringify({ type: 'input_audio_buffer.commit' }));
                await wait(800);
            }
            const transcriptText = reducer.text();
            stopAudioInput();
            await requestRemoteStop().catch((error) => {
                console.warn('Could not explicitly stop the realtime session.', error);
            });
            await cleanupConnection();

            try {
                if (!transcriptText) {
                    unsavedTranscript = false;
                    sessionToken = null;
                    setStatus('idle', labels.idle);
                    setAction(labels.start, 'graphic_eq', false, false);
                    setControlsDisabled(false);
                    setError(labels.noSpeech);
                    return;
                }
                const result = await postJson('/api/live/finalize', {
                    session_token: sessionToken,
                    transcript: transcriptText,
                    detected_language: reducer.detectedLanguage,
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
            stopSignalSent = false;
            liveTransport = 'openai-webrtc';
            resetOpenrouterCapture();
            unsavedTranscript = false;
            startedAt = null;
            elements.timer.textContent = '00:00';
            elements.history.classList.add('hidden');
            setError('');
            setControlsDisabled(false);
            setStatus('idle', labels.idle);
            setAction(labels.start, 'graphic_eq', false, false);
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

        elements.scroll.addEventListener('scroll', () => {
            if (returningToLive) return;
            followingLive = isAtBottom();
            updateFollowButton();
        }, { passive: true });
        elements.follow.addEventListener('click', () => {
            followingLive = true;
            returningToLive = true;
            elements.follow.classList.add('hidden');
            elements.scroll.scrollTo({
                top: elements.scroll.scrollHeight,
                behavior: 'smooth',
            });
            global.setTimeout(() => {
                returningToLive = false;
                followingLive = isAtBottom();
                updateFollowButton();
            }, 450);
        });

        global.addEventListener('beforeunload', (event) => {
            if (unsavedTranscript || state === 'live' || state === 'connecting' || state === 'saving') {
                event.preventDefault();
                event.returnValue = labels.unsaved;
            }
        });
        global.addEventListener('pagehide', () => {
            stopping = true;
            stopTimer();
            stopAudioInput();
            requestRemoteStop(true).catch(() => {});
            cleanupConnection();
        });
        global.addEventListener('pageshow', (event) => {
            if (event.persisted && stopping) global.location.reload();
        });
        if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
            navigator.mediaDevices.addEventListener('devicechange', () => {
                if (state === 'idle' || state === 'saved') listMicrophones();
            });
        }

        listMicrophones();
    });
})(typeof window !== 'undefined' ? window : globalThis);
