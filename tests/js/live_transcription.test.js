const test = require('node:test');
const assert = require('node:assert/strict');

const {
    LiveTranscriptReducer,
} = require('../../app/static/js/live_transcription.js');

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
