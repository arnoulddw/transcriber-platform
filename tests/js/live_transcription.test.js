const test = require('node:test');
const assert = require('node:assert/strict');

const {
    MAX_SESSION_DURATION_MS,
    OPENROUTER_CHUNK_SECONDS,
    LiveTranscriptReducer,
    encodePcm16Wav,
    createCompleteOffer,
    remainingSessionMilliseconds,
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
