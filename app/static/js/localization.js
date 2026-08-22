// app/static/js/localization.js
// This file provides localization utilities, focusing on date and time formatting.

/**
 * Gets the user's preferred locale from the browser.
 * Falls back to 'en-US' if the locale cannot be determined.
 * @returns {string} The determined locale.
 */
function getUserLocale() {
    return (navigator.languages && navigator.languages.length) ? navigator.languages[0] : (navigator.language || 'en-US');
}

/**
 * Formats an ISO 8601 datetime string into a locale-aware date and time string.
 * @param {string} isoString - The ISO 8601 datetime string.
 * @returns {string} A formatted date and time string (e.g., "8/18/2025, 2:30 PM" or "18/8/2025, 14:30").
 */
function formatDateTime(isoString) {
    if (!isoString) return 'N/A';
    try {
        const date = new Date(isoString);
        if (isNaN(date.getTime())) return isoString;
        const locale = getUserLocale();
        const options = {
            year: 'numeric',
            month: 'numeric',
            day: 'numeric',
            hour: 'numeric',
            minute: 'numeric'
        };
        return new Intl.DateTimeFormat(locale, options).format(date);
    } catch (e) {
        window.logger.error("Error formatting datetime:", e);
        return isoString; // Fallback to original string on error
    }
}

// Expose functions to the global window object to be accessible from other scripts.
// Server-side number/currency formatting goes through Babel with the
// formatting_locale computed in app/__init__.py; native <input type="number">
// fields handle their own separator display and parsing in the browser.
window.getUserLocale = getUserLocale;
window.formatDateTime = formatDateTime;
