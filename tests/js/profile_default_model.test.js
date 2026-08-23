const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// profile.js attaches a DOMContentLoaded listener at load time; stub the
// browser globals it touches at module scope before evaluating it.
const sandbox = {
    document: { addEventListener() {} },
    window: {},
};
vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(
        require('node:path').join(__dirname, '..', '..', 'app', 'static', 'js', 'profile.js'),
        'utf8'
    ),
    sandbox
);
const { findDefaultModelOption } = sandbox;

function option(value, provider, modelName) {
    return {
        value,
        dataset: Object.assign(
            { provider: provider || '' },
            modelName ? { modelName } : {}
        ),
    };
}

const OPTIONS = [
    option('', ''),
    option('openai:gpt-transcribe', 'openai'),
    option('assemblyai:universal', 'assemblyai'),
    option('openrouter:x-ai/grok-stt-1.0', 'openrouter', 'x-ai/grok-stt-1.0'),
];

test('saved transcription model wins over an OpenRouter slug match', () => {
    const picked = findDefaultModelOption(OPTIONS, 'openai:gpt-transcribe', 'x-ai/grok-stt-1.0');
    assert.equal(picked.value, 'openai:gpt-transcribe');
});

test('legacy bare model code resolves via the provider-keyed suffix', () => {
    const picked = findDefaultModelOption(OPTIONS, 'gpt-transcribe', '');
    assert.equal(picked.value, 'openai:gpt-transcribe');
});

test('legacy bare openrouter value falls back to the configured slug', () => {
    const picked = findDefaultModelOption(OPTIONS, 'openrouter', 'x-ai/grok-stt-1.0');
    assert.equal(picked.value, 'openrouter:x-ai/grok-stt-1.0');
});

test('unknown saved value keeps -- Use System Default -- selected', () => {
    assert.equal(findDefaultModelOption(OPTIONS, 'retired-model', 'x-ai/grok-stt-1.0'), null);
});

test('empty saved value keeps -- Use System Default -- selected', () => {
    assert.equal(findDefaultModelOption(OPTIONS, '', 'x-ai/grok-stt-1.0'), null);
});
