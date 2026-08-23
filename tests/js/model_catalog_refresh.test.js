const test = require('node:test');
const assert = require('node:assert/strict');

// Minimal browser shims so user_settings.js evaluates in Node.
global.window = {
    csrfToken: 'test-token',
    logger: {
        scoped: () => ({ debug() {}, info() {}, warn() {}, error() {} }),
        debug() {}, info() {}, warn() {}, error() {},
    },
};
global.document = {
    addEventListener() {},
};

const { refreshModelCatalogGlobals } = require('../../app/static/js/user_settings.js');

// Each test starts from the same pristine catalog snapshot because all tests
// share this process's window object.
function resetCatalogGlobals() {
    window.TRANSCRIPTION_MODELS = [{ code: 'old-transcription' }];
    window.LIVE_TRANSCRIPTION_MODELS = ['old-live'];
    window.LLM_MODEL_CATALOG = [{ code: 'old-llm' }];
}

test('refreshes the model catalog globals after saving a new model', async () => {
    resetCatalogGlobals();
    const payload = {
        transcription: [
            { code: 'old-transcription' },
            { code: 'brand-new-model', display_name: 'Brand New Model', required_api_key: 'openai' },
        ],
        live: ['old-live'],
        llm: [{ code: 'old-llm' }, { code: 'vendor/new-llm' }],
    };
    let requestedUrl = '';
    global.fetch = async (url) => {
        requestedUrl = url;
        return { ok: true, json: async () => payload };
    };

    await refreshModelCatalogGlobals();

    assert.equal(requestedUrl, '/api/user/model-catalog');
    assert.deepEqual(
        window.TRANSCRIPTION_MODELS.map(model => model.code),
        ['old-transcription', 'brand-new-model'],
    );
    assert.deepEqual(window.LLM_MODEL_CATALOG.map(model => model.code), ['old-llm', 'vendor/new-llm']);
    assert.deepEqual(window.LIVE_TRANSCRIPTION_MODELS, ['old-live']);
});

test('keeps existing catalog globals when the endpoint fails', async () => {
    resetCatalogGlobals();
    global.fetch = async () => ({ ok: false, status: 500 });

    await assert.doesNotReject(refreshModelCatalogGlobals());

    assert.deepEqual(window.TRANSCRIPTION_MODELS.map(model => model.code), ['old-transcription']);
    assert.deepEqual(window.LLM_MODEL_CATALOG.map(model => model.code), ['old-llm']);
    assert.deepEqual(window.LIVE_TRANSCRIPTION_MODELS, ['old-live']);

    global.fetch = async () => { throw new Error('network down'); };
    await assert.doesNotReject(refreshModelCatalogGlobals());
    assert.deepEqual(window.TRANSCRIPTION_MODELS.map(model => model.code), ['old-transcription']);
});

test('leaves catalogs absent from the response untouched', async () => {
    resetCatalogGlobals();
    global.fetch = async () => ({ ok: true, json: async () => ({ transcription: [{ code: 'only-t' }] }) });

    await refreshModelCatalogGlobals();

    assert.deepEqual(window.TRANSCRIPTION_MODELS.map(model => model.code), ['only-t']);
    assert.deepEqual(window.LIVE_TRANSCRIPTION_MODELS, ['old-live']);
    assert.deepEqual(window.LLM_MODEL_CATALOG.map(model => model.code), ['old-llm']);
});
