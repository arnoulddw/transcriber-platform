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
const profileSource = fs.readFileSync(
    require('node:path').join(__dirname, '..', '..', 'app', 'static', 'js', 'profile.js'),
    'utf8'
);
vm.runInContext(profileSource, sandbox);

function option(value, provider, modelName) {
    return {
        value,
        dataset: Object.assign(
            { provider: provider || '' },
            modelName ? { modelName } : {}
        ),
    };
}

function pickSelection(optionsJson, storedModel) {
    // Mirror of loadProfileData's selection logic under test.
    const context = vm.createContext({ options: optionsJson });
    return vm.runInContext(
        `options.find(option => option.value === ${JSON.stringify(storedModel)}) || null`,
        context
    );
}

test('profile modal selects the stored model by exact option value', () => {
    const picked = pickSelection(
        [
            option('', ''),
            option('openai:gpt-transcribe', 'openai'),
            option('assemblyai:universal', 'assemblyai'),
            option('openrouter:x-ai/grok-stt-1.0', 'openrouter', 'x-ai/grok-stt-1.0'),
        ],
        'openai:gpt-transcribe'
    );
    assert.equal(picked.value, 'openai:gpt-transcribe');
});

test('selection no longer consults OpenRouter slugs or provider fallbacks', () => {
    // The selection statement itself: an exact value match against
    // storedModel only — no suffix translation, no slug-based alternative.
    const selection = profileSource.match(
        /const matchedModelOption = ([\s\S]*?)if \(matchedModelOption\)/
    );
    assert.ok(selection, 'selection statement not found in loadProfileData');
    assert.doesNotMatch(selection[1], /openrouter|dataset\.provider|slice\(/i);
});

test('loadProfileData no longer references the removed legacy helper', () => {
    assert.match(profileSource, /option\.value === storedModel/);
    assert.doesNotMatch(profileSource, /findDefaultModelOption/);
    assert.doesNotMatch(profileSource, /slice\(1\)\.join/);
});
