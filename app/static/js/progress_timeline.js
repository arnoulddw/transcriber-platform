// app/static/js/progress_timeline.js
// Pure timeline helpers for transcription job progress. Anchors completed
// phases to the job's server-side creation time so a page that connects
// mid-job (another device, or a refresh) resumes at the same position
// instead of restarting from 0. No DOM access — unit-testable in Node.
(function (global) {
    'use strict';

    const UPLOAD_COMPLETE = 'PHASE_MARKER:UPLOAD_COMPLETE';
    const TRANSCRIPTION_START = 'PHASE_MARKER:TRANSCRIPTION_START';
    const MIN_PHASE_DURATION_FOR_SMOOTHING = 0.5;
    const HOLD_PROGRESS_AT = 95;

    function parseCreatedAtMs(value) {
        if (value === null || value === undefined || value === '') {
            return NaN;
        }
        const ms = Date.parse(value);
        return Number.isFinite(ms) ? ms : NaN;
    }

    function shouldReplayMarkers(status) {
        return status !== 'pending' && status !== 'cancelling';
    }

    /**
     * Advance the phase timeline by one progress message.
     * Phase anchors accumulate expected durations from the current phase start;
     * markers are written once by the worker, in order.
     * @returns {{phaseStartTimeMs:number, marker:string|null}}
     */
    function advancePhase({ phaseStartTimeMs, message, expectedTimes }) {
        const upper = String(message || '').toUpperCase();
        const times = expectedTimes || {};
        if (upper.includes(UPLOAD_COMPLETE)) {
            return {
                phaseStartTimeMs: phaseStartTimeMs + Math.max(0, times.upload || 0) * 1000,
                marker: 'uploadComplete',
            };
        }
        if (upper.includes(TRANSCRIPTION_START)) {
            return {
                phaseStartTimeMs: phaseStartTimeMs + Math.max(0, times.processing || 0) * 1000,
                marker: 'transcriptionStart',
            };
        }
        return { phaseStartTimeMs: phaseStartTimeMs, marker: null };
    }

    function replayMarkers({
        phaseStartTimeMs,
        messages,
        expectedTimes,
        fileSizeMb,
        largeFileThresholdMb,
    }) {
        const threshold = typeof largeFileThresholdMb === 'number' ? largeFileThresholdMb : 25;
        const size = typeof fileSizeMb === 'number' ? fileSizeMb : 0;
        let start = phaseStartTimeMs;
        let phase = 'upload';
        let lastProgressKey = null;
        const list = Array.isArray(messages) ? messages : [];
        for (let i = 0; i < list.length; i += 1) {
            const advanced = advancePhase({
                phaseStartTimeMs: start,
                message: list[i],
                expectedTimes: expectedTimes,
            });
            if (advanced.marker === 'uploadComplete' && phase === 'upload') {
                start = advanced.phaseStartTimeMs;
                phase = size > threshold ? 'processing' : 'transcribing';
                lastProgressKey = 'upload';
            } else if (advanced.marker === 'transcriptionStart' && phase === 'processing') {
                start = advanced.phaseStartTimeMs;
                phase = 'transcribing';
                lastProgressKey = 'processing';
            }
        }
        return { phaseStartTimeMs: start, phase: phase, lastProgressKey: lastProgressKey };
    }

    function estimateProgress({
        nowMs,
        phaseStartTimeMs,
        phase,
        expectedTimes,
        progressBoundaries,
        holdAt,
        minPhaseDuration,
    }) {
        const hold = typeof holdAt === 'number' ? holdAt : HOLD_PROGRESS_AT;
        const minDuration = typeof minPhaseDuration === 'number' ? minPhaseDuration : MIN_PHASE_DURATION_FOR_SMOOTHING;
        const times = expectedTimes || {};
        const bounds = progressBoundaries || {};
        const elapsedTimeInPhase = (nowMs - phaseStartTimeMs) / 1000;
        const upBoundary = bounds.upload || 0;
        const procBoundary = bounds.processing || 0;
        const transStartBoundary = bounds.transcriptionStart || 0;
        switch (phase) {
            case 'upload': {
                const expectedUpload = times.upload;
                return (expectedUpload < minDuration || expectedUpload <= 0)
                    ? upBoundary
                    : Math.min((elapsedTimeInPhase / expectedUpload) * upBoundary, upBoundary);
            }
            case 'processing': {
                const expectedProcessing = times.processing;
                const processingRange = procBoundary - upBoundary;
                return (expectedProcessing < minDuration || expectedProcessing <= 0)
                    ? procBoundary
                    : upBoundary + Math.min((elapsedTimeInPhase / expectedProcessing) * processingRange, processingRange);
            }
            case 'transcribing': {
                const expectedTranscription = times.transcription;
                const transcriptionRange = hold - transStartBoundary;
                return (expectedTranscription < minDuration || expectedTranscription <= 0)
                    ? hold
                    : transStartBoundary + Math.min((elapsedTimeInPhase / expectedTranscription) * transcriptionRange, transcriptionRange);
            }
            default:
                return 0;
        }
    }

    const api = {
        parseCreatedAtMs: parseCreatedAtMs,
        shouldReplayMarkers: shouldReplayMarkers,
        advancePhase: advancePhase,
        replayMarkers: replayMarkers,
        estimateProgress: estimateProgress,
    };
    global.ProgressTimeline = api;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
