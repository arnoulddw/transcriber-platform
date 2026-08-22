(function (global) {
    'use strict';

    const USAGE_PERIODS = Object.freeze(['daily', 'weekly', 'monthly']);
    const USAGE_METRICS = Object.freeze({
        cost: {
            label: 'cost',
            format: (value) => `$${value.toFixed(2)}`,
        },
        minutes: {
            label: 'audio time',
            format: (value) => (
                typeof global.formatMinutesSimple === 'function'
                    ? global.formatMinutesSimple(value)
                    : `${value} minutes`
            ),
        },
        workflows: {
            label: 'workflow',
            format: (value) => String(value),
        },
        live_minutes: {
            label: 'live transcription time',
            format: (value) => (
                typeof global.formatMinutesSimple === 'function'
                    ? global.formatMinutesSimple(value)
                    : `${value} minutes`
            ),
        },
    });

    function asNumber(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number : 0;
    }

    function getUsageQuotaExceededReason(readinessData, increments = {}, options = {}) {
        const limits = readinessData?.limits || {};
        const usage = readinessData?.usage || {};
        const metrics = Array.isArray(options.metrics)
            ? options.metrics
            : Object.keys(USAGE_METRICS);
        const blockAtCurrentLimit = options.blockAtCurrentLimit === true;

        for (const period of USAGE_PERIODS) {
            const periodUsage = usage[period] || {};
            for (const metric of metrics) {
                const metricDefinition = USAGE_METRICS[metric];
                if (!metricDefinition) continue;

                const limit = asNumber(limits[`limit_${period}_${metric}`]);
                if (limit <= 0) continue;

                const current = asNumber(periodUsage[metric]);
                const increment = asNumber(increments[metric]);
                const projected = current + increment;
                const exceeded = blockAtCurrentLimit
                    ? current >= limit
                    : projected > limit;
                if (!exceeded) continue;

                const periodLabel = period[0].toUpperCase() + period.slice(1);
                return `${periodLabel} ${metricDefinition.label} limit (${metricDefinition.format(limit)}) reached.`;
            }
        }

        return null;
    }

    global.getUsageQuotaExceededReason = getUsageQuotaExceededReason;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            USAGE_PERIODS,
            USAGE_METRICS,
            getUsageQuotaExceededReason,
        };
    }
}(typeof window !== 'undefined' ? window : globalThis));
