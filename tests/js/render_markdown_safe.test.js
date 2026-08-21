const test = require('node:test');
const assert = require('node:assert/strict');

// Minimal browser shims so main_utils.js evaluates in Node.
global.window = {
    APP_DEBUG_MODE: false,
    logger: {
        scoped: () => ({ debug() {}, info() {}, warn() {}, error() {} }),
    },
};
global.document = {
    addEventListener() {},
};

const { renderMarkdownSafe } = require('../../app/static/js/main_utils.js');

test('renderMarkdownSafe fails closed to escaped text when marked/DOMPurify are missing', () => {
    assert.equal(typeof window.marked, 'undefined');
    assert.equal(typeof window.DOMPurify, 'undefined');
    const html = renderMarkdownSafe('<script>alert(1)</script>');
    assert.match(html, /^<pre/);
    assert.equal(html.includes('<script>'), false);
});

test('renderMarkdownSafe sanitizes dangerous markdown when libraries are present', () => {
    window.marked = {
        setOptions() {},
        parse: (text) => `<p>hi</p><script>alert(1)</script><img src=x onerror="alert(1)">`,
    };
    window.DOMPurify = {
        sanitize: (html) => html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/\sonerror="[^"]*"/g, ''),
    };
    const html = renderMarkdownSafe('hi');
    assert.match(html, /<p>hi<\/p>/);
    assert.equal(html.includes('<script>'), false);
    assert.equal(html.includes('onerror'), false);
    delete window.marked;
    delete window.DOMPurify;
});
