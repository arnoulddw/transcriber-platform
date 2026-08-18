document.addEventListener('DOMContentLoaded', function() {
    const pricingForm = document.getElementById('pricing-form');
    if (!pricingForm) return;

    const selects = Array.from(pricingForm.querySelectorAll('.pricing-model-select'));
    const prices = Array.from(pricingForm.querySelectorAll('.pricing-value'));
    const priceByType = new Map();

    function priceInput(type) {
        return pricingForm.querySelector(`.pricing-value[data-type="${type}"]`) || prices[selects.findIndex(select => select.dataset.type === type)];
    }

    function populatePrices(data) {
        Object.entries(data || {}).forEach(([type, values]) => {
            if (!values || typeof values !== 'object') return;
            Object.entries(values).forEach(([key, value]) => priceByType.set(`${type}:${key}`, value));
        });
        selects.forEach(select => {
            const input = priceInput(select.dataset.type);
            if (input && select.value) input.value = priceByType.get(`${select.dataset.type}:${select.value}`) ?? '';
        });
    }

    fetch('/api/admin/pricing')
        .then(response => response.json())
        .then(populatePrices)
        .catch(error => window.logger.error('Error fetching prices:', error));

    selects.forEach(select => {
        select.addEventListener('change', () => {
            const input = priceInput(select.dataset.type);
            if (input) input.value = priceByType.get(`${select.dataset.type}:${select.value}`) ?? '';
        });
    });

    pricingForm.querySelectorAll('.save-price-btn').forEach(button => {
        button.addEventListener('click', () => {
            const type = button.dataset.type;
            const select = pricingForm.querySelector(`.pricing-model-select[data-type="${type}"]`);
            const input = priceInput(type);
            const itemKey = select?.value;
            const price = parseFloat(input?.value || '');
            if (!itemKey || Number.isNaN(price) || price < 0) {
                showNotification('Choose a model and enter a valid non-negative price.', 'warning', 4000, false);
                return;
            }
            button.disabled = true;
            fetch('/api/admin/pricing', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': pricingForm.querySelector('input[name="csrf_token"]').value,
                },
                body: JSON.stringify({ item_type: type, item_key: itemKey, price }),
            })
                .then(response => response.json().then(data => ({ ok: response.ok, data })))
                .then(({ ok, data }) => {
                    if (!ok || !data.success) throw new Error(data.error || 'Pricing update failed.');
                    priceByType.set(`${type}:${itemKey}`, price);
                    showNotification(data.message || 'Pricing updated successfully.', 'success', 3000, false);
                })
                .catch(error => {
                    window.logger.error('Error updating price:', error);
                    showNotification(`Error updating price: ${error.message}`, 'error', 5000, true);
                })
                .finally(() => { button.disabled = false; });
        });
    });
});

function parseLocaleNumber(stringNumber) {
    return parseFloat(String(stringNumber).replace(',', '.'));
}
