const test = require('node:test');
const assert = require('node:assert/strict');

const {
    parseCreatedAtMs,
    shouldReplayMarkers,
    advancePhase,
    replayMarkers,
    estimateProgress,
} = require('../../app/static/js/progress_timeline.js');

const EXPECTED = { upload: 30, processing: 90, transcription: 300 };
const BOUNDARIES = { upload: 7, processing: 29, transcriptionStart: 29 };
const HOLD_AT = 95;

test('parseCreatedAtMs accepts ISO-8601 UTC with Z', () => {
    const ms = parseCreatedAtMs('2026-08-16T08:00:00Z');
    assert.equal(ms, Date.parse('2026-08-16T08:00:00Z'));
    assert.equal(Number.isFinite(ms), true);
});

test('parseCreatedAtMs rejects missing and invalid values', () => {
    assert.equal(Number.isFinite(parseCreatedAtMs(null)), false);
    assert.equal(Number.isFinite(parseCreatedAtMs(undefined)), false);
    assert.equal(Number.isFinite(parseCreatedAtMs('')), false);
    assert.equal(Number.isFinite(parseCreatedAtMs('not-a-date')), false);
});

test('shouldReplayMarkers is false while pending or cancelling', () => {
    assert.equal(shouldReplayMarkers('pending'), false);
    assert.equal(shouldReplayMarkers('cancelling'), false);
    assert.equal(shouldReplayMarkers('processing'), true);
    assert.equal(shouldReplayMarkers('finished'), true);
});

test('UPLOAD_COMPLETE advances the anchor by the expected upload duration', () => {
    const out = advancePhase({
        phaseStartTimeMs: 1_000_000,
        message: 'PHASE_MARKER:UPLOAD_COMPLETE',
        expectedTimes: EXPECTED,
    });
    assert.equal(out.phaseStartTimeMs, 1_000_000 + 30 * 1000);
    assert.equal(out.marker, 'uploadComplete');
});

test('TRANSCRIPTION_START advances the anchor by the expected processing duration', () => {
    const out = advancePhase({
        phaseStartTimeMs: 1_030_000,
        message: 'PHASE_MARKER:TRANSCRIPTION_START',
        expectedTimes: EXPECTED,
    });
    assert.equal(out.phaseStartTimeMs, 1_030_000 + 90 * 1000);
    assert.equal(out.marker, 'transcriptionStart');
});

test('non-marker messages leave the timeline untouched', () => {
    const out = advancePhase({
        phaseStartTimeMs: 5,
        message: 'Uploading audio for OpenAI GPT-4o Transcribe...',
        expectedTimes: EXPECTED,
    });
    assert.deepEqual(out, { phaseStartTimeMs: 5, marker: null });
});

test('small-file minimum durations still advance in milliseconds', () => {
    const out = advancePhase({
        phaseStartTimeMs: 100,
        message: 'PHASE_MARKER:UPLOAD_COMPLETE',
        expectedTimes: { upload: 0.2, processing: 0.2 },
    });
    assert.equal(out.phaseStartTimeMs, 100 + 200);
});

test('replay of a mid-transcription large-file log lands on the transcribing anchor', () => {
    const createdAtMs = 5_000_000;
    const out = replayMarkers({
        phaseStartTimeMs: createdAtMs,
        messages: [
            'Saving audio file...',
            'PHASE_MARKER:UPLOAD_COMPLETE',
            'Processing audio...',
            'PHASE_MARKER:TRANSCRIPTION_START',
            'Transcribing chunk 2 of 4...',
        ],
        expectedTimes: EXPECTED,
        fileSizeMb: 40,
        largeFileThresholdMb: 25,
    });
    assert.equal(out.phaseStartTimeMs, createdAtMs + (30 + 90) * 1000);
    assert.equal(out.phase, 'transcribing');
    assert.equal(out.lastProgressKey, 'processing');
});

test('replay of a small-file log (no TRANSCRIPTION_START) skips processing', () => {
    const createdAtMs = 1_000;
    const out = replayMarkers({
        phaseStartTimeMs: createdAtMs,
        messages: [
            'Permissions validated.',
            'PHASE_MARKER:UPLOAD_COMPLETE',
            'Transcribing...',
        ],
        expectedTimes: EXPECTED,
        fileSizeMb: 3,
        largeFileThresholdMb: 25,
    });
    assert.equal(out.phaseStartTimeMs, createdAtMs + 30 * 1000);
    assert.equal(out.phase, 'transcribing');
    assert.equal(out.lastProgressKey, 'upload');
});

test('pending-then-processing: markers are not consumed until replay runs', () => {
    const log = [
        'Job created.',
        'PHASE_MARKER:UPLOAD_COMPLETE',
    ];
    assert.equal(shouldReplayMarkers('pending'), false);
    const afterProcessing = replayMarkers({
        phaseStartTimeMs: 0,
        messages: log,
        expectedTimes: EXPECTED,
        fileSizeMb: 3,
        largeFileThresholdMb: 25,
    });
    assert.equal(afterProcessing.phase, 'transcribing');
    assert.equal(afterProcessing.phaseStartTimeMs, 30 * 1000);
});

test('estimateProgress reconstructs mid-transcription percent from created_at + now', () => {
    const createdAtMs = 0;
    const replayed = replayMarkers({
        phaseStartTimeMs: createdAtMs,
        messages: ['PHASE_MARKER:UPLOAD_COMPLETE', 'PHASE_MARKER:TRANSCRIPTION_START'],
        expectedTimes: EXPECTED,
        fileSizeMb: 40,
        largeFileThresholdMb: 25,
    });
    // 270s after enqueue = 150s into the 300s transcription phase
    const raw = estimateProgress({
        nowMs: createdAtMs + (30 + 90 + 150) * 1000,
        phaseStartTimeMs: replayed.phaseStartTimeMs,
        phase: replayed.phase,
        expectedTimes: EXPECTED,
        progressBoundaries: BOUNDARIES,
        holdAt: HOLD_AT,
    });
    assert.equal(Math.round(raw), 62);
});
