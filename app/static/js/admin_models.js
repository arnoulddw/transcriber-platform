document.addEventListener('DOMContentLoaded', function() {
    const modelsForm = document.getElementById('models-form');
    if (!modelsForm) return;

    const selects = Array.from(modelsForm.querySelectorAll('.model-rename-select'));

    // Pre-fill the display-name input when a model is selected. The input shows
    // the currently selected option's text (its display name) for editing.
    function populateInput(type) {
        const select = modelsForm.querySelector(`.model-rename-select[data-type="${type}"]`);
        const input = modelsForm.querySelector(`.model-rename-input[data-type="${type}"]`);
        if (!select || !input) return;
        const option = select.options[select.selectedIndex];
        input.value = option ? option.textContent : '';
    }

    // Initial paint: reflect the placeholder selection (empty).
    selects.forEach(select => populateInput(select.dataset.type));

    selects.forEach(select => {
        select.addEventListener('change', () => populateInput(select.dataset.type));
    });

    modelsForm.querySelectorAll('.save-model-btn').forEach(button => {
        button.addEventListener('click', () => {
            const type = button.dataset.type;
            const select = modelsForm.querySelector(`.model-rename-select[data-type="${type}"]`);
            const input = modelsForm.querySelector(`.model-rename-input[data-type="${type}"]`);
            const code = select?.value;
            const displayName = (input?.value || '').trim();
            if (!code || !displayName) {
                showNotification('Choose a model and enter a display name.', 'warning', 4000, false);
                return;
            }
            button.disabled = true;
            fetch('/api/admin/models/rename', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': modelsForm.querySelector('input[name="csrf_token"]').value,
                },
                body: JSON.stringify({ category: type, code, display_name: displayName }),
            })
                .then(response => response.json().then(data => ({ ok: response.ok, data })))
                .then(({ ok, data }) => {
                    if (!ok || !data.success) throw new Error(data.error || 'Rename failed.');
                    // Update the selected option's label in place.
                    const option = select.querySelector(`option[value="${CSS.escape(code)}"]`);
                    if (option) option.textContent = displayName;
                    showNotification(data.message || 'Model renamed successfully.', 'success', 3000, false);
                })
                .catch(error => {
                    window.logger.error('Error renaming model:', error);
                    showNotification(`Error renaming model: ${error.message}`, 'error', 5000, true);
                })
                .finally(() => { button.disabled = false; });
        });
    });
});
