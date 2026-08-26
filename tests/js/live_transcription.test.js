const test = require('node:test');
const assert = require('node:assert/strict');

const {
    MAX_SESSION_DURATION_MS,
    OPENROUTER_CHUNK_SECONDS,
    AUDIO_STREAM_END_FRAME,
    LiveTranscriptReducer,
    buildGeminiRealtimeAudioFrame,
    buildGeminiSetupFrame,
    createCompleteOffer,
    downsampleTo16k,
    encodePcm16,
    encodePcm16Wav,
    providerLocalModelCode,
    remainingSessionMilliseconds,
    resolveLiveTransport,
    shouldAttemptReconnect,
    waitForDataChannelOpen,
} = require('../../app/static/js/live_transcription.js');

test('caps live sessions at 120 minutes', () => {
    assert.equal(MAX_SESSION_DURATION_MS, 120 * 60 * 1000);
    assert.equal(remainingSessionMilliseconds(1000, 1000), MAX_SESSION_DURATION_MS);
    assert.equal(
        remainingSessionMilliseconds(1000, 1000 + MAX_SESSION_DURATION_MS),
        0,
    );
});

test('encodes browser samples as a PCM WAV payload for OpenRouter', () => {
    assert.equal(OPENROUTER_CHUNK_SECONDS, 3);
    const wav = encodePcm16Wav(new Float32Array([0, 1, -1]), 16000);
    const view = new DataView(wav);
    assert.equal(String.fromCharCode(...new Uint8Array(wav.slice(0, 4))), 'RIFF');
    assert.equal(view.getUint16(34, true), 16);
    assert.equal(view.getUint32(40, true), 6);
});

test('reconciles interleaved deltas by item and preserves first-seen order', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.apply({
        type: 'conversation.item.input_audio_transcription.delta',
        item_id: 'first',
        delta: 'Hello',
    });
    reducer.apply({
        type: 'conversation.item.input_audio_transcription.delta',
        item_id: 'second',
        delta: 'Next',
    });
    reducer.apply({
        type: 'conversation.item.input_audio_transcription.delta',
        item_id: 'first',
        delta: ' world',
    });

    assert.equal(reducer.text(), 'Hello world Next');
});

test('completed events replace partial text without reordering turns', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.apply({
        type: 'conversation.item.input_audio_transcription.delta',
        item_id: 'first',
        delta: 'Helo',
    });
    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'second',
        transcript: 'Second turn.',
    });
    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'first',
        transcript: 'Hello.',
    });

    assert.deepEqual(
        reducer.entries().map((entry) => [entry.itemId, entry.text, entry.complete]),
        [
            ['first', 'Hello.', true],
            ['second', 'Second turn.', true],
        ],
    );
});

test('uses the local description updated by ICE gathering', async () => {
    class FakeConnection extends EventTarget {
        constructor() {
            super();
            this.iceGatheringState = 'new';
            this.localDescription = null;
        }

        async createOffer() {
            return { type: 'offer', sdp: 'initial SDP' };
        }

        async setLocalDescription(offer) {
            this.localDescription = offer;
            queueMicrotask(() => {
                this.localDescription = { type: 'offer', sdp: 'SDP with mobile ICE candidates' };
                this.iceGatheringState = 'complete';
                this.dispatchEvent(new Event('icegatheringstatechange'));
            });
        }
    }

    const offer = await createCompleteOffer(new FakeConnection());

    assert.equal(offer.sdp, 'SDP with mobile ICE candidates');
});

test('waits for the realtime data channel before reporting a live connection', async () => {
    class FakeDataChannel extends EventTarget {
        constructor() {
            super();
            this.readyState = 'connecting';
        }
    }

    const channel = new FakeDataChannel();
    const connection = new EventTarget();
    connection.connectionState = 'connecting';
    const opened = waitForDataChannelOpen(channel, connection, 100, 'Connection failed');

    channel.readyState = 'open';
    channel.dispatchEvent(new Event('open'));

    await opened;
});

test('captures the first detected language from completed events', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'first',
        transcript: 'Bonjour',
        languages: [{ code: 'fr' }],
    });
    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'second',
        transcript: 'Hello',
        languages: [{ code: 'en' }],
    });

    assert.equal(reducer.detectedLanguage, 'fr');
});

test('keeps detectedLanguage null when the API reports no languages', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'first',
        transcript: 'Hello',
    });

    assert.equal(reducer.detectedLanguage, null);
});

test('clear resets the detected language', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'first',
        transcript: 'Hola',
        languages: [{ code: 'es' }],
    });
    reducer.clear();

    assert.equal(reducer.detectedLanguage, null);
});

test('resolves the Gemini transport from the data-provider attribute or value prefix', () => {
    assert.equal(
        resolveLiveTransport({ dataset: { provider: 'gemini' }, value: 'anything' }),
        'gemini-wss',
    );
    assert.equal(resolveLiveTransport({ value: 'gemini-2.5-flash' }), 'gemini-wss');
});

test('resolves the OpenRouter and WebRTC transports as before', () => {
    assert.equal(
        resolveLiveTransport({ dataset: { provider: 'openrouter' }, value: '' }),
        'openrouter-sse',
    );
    assert.equal(resolveLiveTransport({ value: 'openai/gpt-4o-mini-transcribe' }), 'openrouter-sse');
    assert.equal(resolveLiveTransport({ value: 'whisper-1' }), 'openai-webrtc');
});

test('resolves a missing model option defensively to the WebRTC default', () => {
    assert.equal(resolveLiveTransport(null), 'openai-webrtc');
    assert.equal(resolveLiveTransport(undefined), 'openai-webrtc');
});

test('strips the provider prefix from provider-qualified model keys', () => {
    assert.equal(
        providerLocalModelCode('gemini:gemini-3.5-transcribe-live'),
        'gemini-3.5-transcribe-live',
    );
});

test('strips only up to the first colon of a provider-qualified key', () => {
    assert.equal(providerLocalModelCode('openrouter:openai/whisper-large-v3'), 'openai/whisper-large-v3');
});

test('leaves bare model codes unchanged', () => {
    assert.equal(providerLocalModelCode('gpt-live-transcribe'), 'gpt-live-transcribe');
    assert.equal(providerLocalModelCode(' gemini-2.5-flash '), 'gemini-2.5-flash');
});

test('handles empty and missing model values defensively', () => {
    assert.equal(providerLocalModelCode(''), '');
    assert.equal(providerLocalModelCode(null), '');
    assert.equal(providerLocalModelCode(undefined), '');
});

test('encodes raw PCM16 little-endian samples without a WAV header', () => {
    const pcm = encodePcm16(new Float32Array([0, 0.999, -1, 1.5]));

    assert.ok(pcm instanceof ArrayBuffer);
    assert.equal(pcm.byteLength, 8);
    const view = new DataView(pcm);
    assert.deepEqual(
        [view.getInt16(0, true), view.getInt16(2, true), view.getInt16(4, true), view.getInt16(6, true)],
        [0, Math.round(0.999 * 0x7fff), -0x8000, 0x7fff],
    );
});

test('returns 16 kHz samples untouched when the context already samples at 16000 Hz', () => {
    const samples = new Float32Array([0.1, -0.2, 0.3]);
    const downsampled = downsampleTo16k(samples, 16000);

    assert.equal(downsampled.length, 3);
    [...downsampled].forEach((value, index) => {
        assert.ok(Math.abs(value - samples[index]) < Number.EPSILON);
    });
});

test('downsamples an exact 48000-to-16000 ratio along a ramp', () => {
    const samples = new Float32Array([0, 0.3, 0.6, 0.9, 1.2]);
    const downsampled = downsampleTo16k(samples, 48000);

    assert.equal(downsampled.length, Math.ceil(5 * 16000 / 48000));
    assert.ok(Math.abs(downsampled[0]) < Number.EPSILON);
    assert.ok(Math.abs(downsampled[1] - 0.9) < 1e-6);
});

test('downsampled output length follows the ceil ratio for odd source rates', () => {
    const samples = new Float32Array(5);

    assert.equal(downsampleTo16k(samples, 24000).length, Math.ceil(5 * 16000 / 24000));
});

test('builds the Gemini setup frame for automatic language detection', () => {
    assert.deepEqual(buildGeminiSetupFrame('gemini-2.5-flash', 'auto'), {
        setup: {
            model: 'models/gemini-2.5-flash',
            generationConfig: { responseModalities: ['TEXT'] },
            inputAudioTranscription: { languageCodes: [] },
            sessionResumption: {},
        },
    });
});

test('builds the Gemini setup frame with an explicit language and vocabulary', () => {
    assert.deepEqual(
        buildGeminiSetupFrame('gemini-live-2.5', 'es', ['Faro', 'SEPA'], 'resume-handle'),
        {
            setup: {
                model: 'models/gemini-live-2.5',
                generationConfig: { responseModalities: ['TEXT'] },
                inputAudioTranscription: {
                    languageCodes: ['es'],
                    customVocabulary: ['Faro', 'SEPA'],
                },
                sessionResumption: { handle: 'resume-handle' },
            },
        },
    );
});

test('omits customVocabulary when no vocabulary terms exist', () => {
    const frame = buildGeminiSetupFrame('gemini-2.5-flash', 'en', []);

    assert.equal(frame.setup.inputAudioTranscription.customVocabulary, undefined);
    assert.deepEqual(frame.setup.sessionResumption, {});
});

test('wraps base64 audio chunks in the Gemini realtime audio frame shape', () => {
    assert.deepEqual(buildGeminiRealtimeAudioFrame('aGVsbG8='), {
        realtimeInput: {
            audio: {
                data: 'aGVsbG8=',
                mimeType: 'audio/pcm;rate=16000',
            },
        },
    });
    assert.deepEqual(AUDIO_STREAM_END_FRAME, { realtimeInput: { audioStreamEnd: true } });
});

test('replace() updates interim text in place without growing the turn order', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.replace('gemini-interim', 'Hel');
    reducer.replace('gemini-interim', 'Hello wor');
    reducer.replace('gemini-interim', 'Hello world');

    assert.deepEqual(
        reducer.entries().map((entry) => [entry.itemId, entry.text, entry.complete]),
        [['gemini-interim', 'Hello world', false]],
    );
    assert.equal(reducer.text(), 'Hello world');
});

test('replace() appends unknown item ids and preserves completed turns from apply()', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'gemini-final-1',
        transcript: 'First.',
    });
    reducer.replace('gemini-interim', 'Second');
    reducer.apply({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'gemini-final-2',
        transcript: 'Third.',
    });

    assert.deepEqual(
        reducer.entries().map((entry) => entry.itemId),
        ['gemini-final-1', 'gemini-interim', 'gemini-final-2'],
    );
    assert.equal(reducer.text(), 'First. Second Third.');
});

test('replace() clears the interim slot without leaving stray text', () => {
    const reducer = new LiveTranscriptReducer();

    reducer.replace('gemini-interim', 'partial');
    reducer.replace('gemini-interim', '');

    assert.equal(reducer.text(), '');
});

test('reconnects only within duration limits and below three consecutive failures', () => {
    const cap = MAX_SESSION_DURATION_MS;

    assert.equal(shouldAttemptReconnect(0, 0), true);
    assert.equal(shouldAttemptReconnect(cap - 1000, 2), true);
    assert.equal(shouldAttemptReconnect(cap, 0), false);
    assert.equal(shouldAttemptReconnect(-1, 0), false);
    assert.equal(shouldAttemptReconnect(60000, 3), false);
});
