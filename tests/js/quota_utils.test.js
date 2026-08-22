const test = require('node:test');
const assert = require('node:assert/strict');

const {
    USAGE_PERIODS,
    getUsageQuotaExceededReason,
} = require('../../app/static/js/quota_utils.js');

function readiness(overrides = {}) {
    return {
        limits: {
            limit_daily_cost: 0,
            limit_weekly_cost: 0,
            limit_monthly_cost: 0,
            limit_daily_minutes: 0,
            limit_weekly_minutes: 0,
            limit_monthly_minutes: 0,
            limit_daily_workflows: 0,
            limit_weekly_workflows: 0,
            limit_monthly_workflows: 0,
            limit_daily_live_minutes: 0,
            limit_weekly_live_minutes: 0,
            limit_monthly_live_minutes: 0,
            ...overrides.limits,
        },
        usage: {
            daily: { cost: 0, minutes: 0, workflows: 0, live_minutes: 0 },
            weekly: { cost: 0, minutes: 0, workflows: 0, live_minutes: 0 },
            monthly: { cost: 0, minutes: 0, workflows: 0, live_minutes: 0 },
            ...overrides.usage,
        },
    };
}

test('checks daily, weekly, and monthly periods with server-style projection', () => {
    for (const period of USAGE_PERIODS) {
        const reason = getUsageQuotaExceededReason(readiness({
            limits: { [`limit_${period}_minutes`]: 10 },
            usage: { [period]: { minutes: 9.5 } },
        }), { minutes: 0.6 }, { metrics: ['minutes'] });

        assert.equal(reason, `${period[0].toUpperCase() + period.slice(1)} audio time limit (10 minutes) reached.`);
    }
});

test('can use current-at-limit semantics for the upload gate', () => {
    const data = readiness({
        limits: { limit_weekly_cost: 5 },
        usage: { weekly: { cost: 5 } },
    });

    assert.equal(
        getUsageQuotaExceededReason(data, {}, {
            metrics: ['cost'],
            blockAtCurrentLimit: true,
        }),
        'Weekly cost limit ($5.00) reached.',
    );
});

test('checks workflow and live reservations through the same period loop', () => {
    const workflowData = readiness({
        limits: { limit_monthly_workflows: 3 },
        usage: { monthly: { workflows: 2 } },
    });
    const liveData = readiness({
        limits: { limit_daily_live_minutes: 10 },
        usage: { daily: { live_minutes: 9.5 } },
    });

    assert.equal(
        getUsageQuotaExceededReason(workflowData, { workflows: 2 }),
        'Monthly workflow limit (3) reached.',
    );
    assert.equal(
        getUsageQuotaExceededReason(liveData, { live_minutes: 1 }),
        'Daily live transcription time limit (10 minutes) reached.',
    );
});
