# Chunked Transcription Completeness Implementation Plan

> **For Hermes:** Follow the Karpathy guidelines: implement surgically, test the failure modes first, and do not broaden the scope.

**Goal:** Ensure chunked transcription fails when audio chunks are missing, visibly warns when a real audio chunk produces no text, and keeps that warning visible in transcription history.

**Architecture:** Keep chunk-splitting validation and provider-result validation as separate safeguards. Persist a boolean warning on the transcription record so the warning survives refreshes and can be rendered consistently by the server template and JavaScript history renderer. Do not add speech/volume detection; a non-trivial source chunk means an existing generated chunk with positive file size.

**Tech Stack:** Python/Flask, MySQL schema initialization, FFmpeg segmentation, vanilla JavaScript, Material Icons, existing pytest and Node test harnesses.

---

### Task 1: Add an exact chunk-count check during audio splitting

**Files:**
- Modify: `app/services/file_service.py`
- Test: `tests/unit/test_file_service_splitting.py`

**Steps:**
1. Keep the existing `num_chunks` calculation.
2. After FFmpeg completes and `chunk_files` is collected, compare `num_chunks` with `len(chunk_files)`.
3. On mismatch, log an error, report an error through the existing progress callback, remove generated chunk files, and return the existing splitter failure value so the caller raises a transcription-processing error.
4. Preserve the existing no-output failure path.
5. Add a test where three chunks are expected and two are created; assert failure, mismatch reporting, and cleanup.
6. Run the focused file-service tests and verify they pass.

### Task 2: Make chunk aggregation reject missing results

**Files:**
- Modify: `app/services/api_clients/transcription/base_transcription_client.py`
- Test: add focused coverage in `tests/unit/test_base_transcription_client.py` if no suitable existing fixture can be reused.

**Steps:**
1. Track how many expected chunk numbers have a completed result.
2. Before joining text, verify `successful_chunks == total_chunks` and that every expected chunk number has a result.
3. On mismatch, log details, report an error, and raise `TranscriptionProcessingError` before constructing the final transcript.
4. Add a test with a missing/`None` result for one chunk and assert that aggregation fails.
5. Add a test confirming all completed chunk results still aggregate in order.
6. Run the focused base-client tests and verify they pass.

### Task 3: Detect empty text from a non-trivial source chunk

**Files:**
- Modify: `app/services/api_clients/transcription/base_transcription_client.py`
- Test: `tests/unit/test_base_transcription_client.py`

**Steps:**
1. Reset a per-transcription warning flag at the beginning of each `transcribe` call.
2. Normalize returned chunk text with `.strip()`.
3. For a source chunk that exists and has positive file size, if the normalized text is empty, set the warning flag, write a warning to the application log, and send a stable warning message through the progress callback.
4. Continue joining non-empty chunk text; this warning remains non-fatal per the requested behavior.
5. Test that whitespace-only text from a non-trivial chunk sets the flag and emits the warning.
6. Test that an empty/invalid source chunk does not create the content warning.
7. Run the focused tests and verify they pass.

### Task 4: Persist the warning on the transcription record

**Files:**
- Modify: `app/models/transcription/schema.py`
- Modify: `app/models/transcription/serialization.py`
- Modify: `app/models/transcription/services.py`
- Modify: `app/services/transcription_service.py`
- Test: `tests/functional/services/test_transcription_completion.py` and any focused unit test needed for row mapping.

**Steps:**
1. Add `has_transcription_warning BOOLEAN NOT NULL DEFAULT FALSE` to the create-table schema.
2. Add an idempotent existing-table column check/alter, following the schema file’s existing pattern.
3. Normalize the field to a boolean in row serialization.
4. Extend `finalize_job_success` with an optional warning argument and save it with the transcript.
5. Pass the transcription client’s warning state from `process_transcription`.
6. Test the default false behavior and true persistence behavior.
7. Run the focused persistence tests and verify they pass.

### Task 5: Expose the warning to the progress and history APIs

**Files:**
- Modify: `app/api/transcriptions.py`
- Test: `tests/functional/api/test_transcriptions_api.py`

**Steps:**
1. Include `has_transcription_warning` in the private progress response.
2. Include it in the finished `result` payload used by the history renderer.
3. Default missing/legacy values to false.
4. Add API assertions for both flagged and unflagged finished jobs.
5. Run the focused API tests and verify they pass.

### Task 6: Show the warning in the progress activity area

**Files:**
- Modify: `app/static/js/main_poll.js`
- Test: add a small pure helper test in `tests/js/` if logic is extracted; otherwise cover the source contract in `tests/unit/test_ui_consistency.py` and run syntax checks.

**Steps:**
1. Recognize the stable backend warning message and/or API warning flag.
2. Use existing progress-activity styling to display a warning icon and text such as “Transcription completed with warnings. The transcript may be incomplete.”
3. Ensure the warning is not overwritten by the generic green success message when the job finishes.
4. Keep the normal success display unchanged for unflagged jobs.
5. Run JavaScript tests and `node --check` for all static JavaScript files.

### Task 7: Render a red exclamation icon at the end of history titles

**Files:**
- Modify: `app/templates/index.html`
- Modify: `app/static/js/history/render.js`
- Modify: `app/static/js/history/polling.js`
- Test: `tests/unit/test_ui_consistency.py` and JavaScript syntax/tests.

**Steps:**
1. Add a red Material Icon such as `error_outline` after all other title icons in the server-rendered history item.
2. Use the existing `tiny`, alignment, spacing, and red text classes; add an accessible label/tooltip explaining that the transcript may be incomplete.
3. Render the same icon for newly completed history items inserted by `addTranscriptionToHistory`.
4. Ensure title-generation polling preserves the warning icon when it replaces the title markup.
5. Add source-level UI assertions for the warning field and icon markup.
6. Run the JavaScript and UI consistency tests.

### Task 8: Run the complete verification gates

**Commands:**

```bash
env SECRET_KEY=unit-test-secret MYSQL_USER=test MYSQL_PASSWORD=test MYSQL_DB=test_db \
GOOGLE_CLIENT_ID=test-google-client-id DEPLOYMENT_MODE=multi \
/home/arnould/transcriber-platform/.venv/bin/pytest -q \
tests/unit/test_file_service_splitting.py \
tests/unit/test_base_transcription_client.py \
tests/unit/test_ui_consistency.py \
tests/functional/api/test_transcriptions_api.py \
tests/functional/services/test_transcription_completion.py
```

```bash
npm run test:js
for f in app/static/js/*.js; do node --check "$f"; done
git diff --check
python3 -m compileall -q app tests/unit
```

**Acceptance criteria:**

- A mismatch between expected and created chunk counts fails the job.
- Missing chunk results cannot be joined into a successful partial transcript.
- Empty text from a non-trivial chunk creates a log warning and progress warning.
- The finished history title shows a red exclamation icon and keeps it after title generation.
- Normal complete transcripts remain unchanged.
- No deployment or unrelated refactoring is performed.
