# tests/functional/services/test_user_service.py
# Contains functional tests for the user service.

import pytest
from app.services import user_service
from app.models.user import get_user_by_username
from app.models import user_prompt as user_prompt_model
from app.models import template_prompt as template_prompt_model

# --- Test API Key Management ---

def test_save_and_get_api_key(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user
    WHEN an API key is saved and then retrieved
    THEN check that the key is saved and can be decrypted.
    """
    user = get_user_by_username('testuser_permissions')
    service = 'openai'
    api_key = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

    # Save the API key
    user_service.save_user_api_key(user.id, service, api_key)

    # Retrieve the decrypted API key
    decrypted_key = user_service.get_decrypted_api_key(user.id, service)
    assert decrypted_key == api_key

def test_delete_api_key(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user with a saved API key
    WHEN the API key is deleted
    THEN check that the key is no longer available.
    """
    user = get_user_by_username('testuser_permissions')
    service = 'openai'
    api_key = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

    # Save the API key
    user_service.save_user_api_key(user.id, service, api_key)

    # Delete the API key
    user_service.delete_user_api_key(user.id, service)

    # Check that the key is no longer available
    decrypted_key = user_service.get_decrypted_api_key(user.id, service)
    assert decrypted_key is None

def test_get_api_key_status(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user
    WHEN the API key status is checked
    THEN check that the status is correct.
    """
    user = get_user_by_username('testuser_permissions')
    service = 'openai'
    api_key = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

    # Check initial status
    status = user_service.get_user_api_key_status(user.id)
    assert status[service] is False

    # Save the API key
    user_service.save_user_api_key(user.id, service, api_key)

    # Check status after saving
    status = user_service.get_user_api_key_status(user.id)
    assert status[service] is True

# --- Test Profile Update ---

def test_update_profile(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user
    WHEN the user's profile is updated
    THEN check that the profile is updated correctly.
    """
    user = get_user_by_username('testuser_permissions')
    new_profile_data = {
        'username': 'new_username',
        'email': 'new_email@example.com',
        'first_name': 'New',
        'last_name': 'User',
        'default_content_language': 'fr',
        'default_transcription_model': 'gpt-4o-transcribe',
        'enable_auto_title_generation': True,
        'language': 'fr'
    }

    user_service.update_profile(user.id, new_profile_data)

    updated_user = get_user_by_username('new_username')
    assert updated_user is not None
    assert updated_user.email == 'new_email@example.com'
    assert updated_user.first_name == 'New'
    assert updated_user.last_name == 'User'
    assert updated_user.default_content_language == 'fr'
    assert updated_user.default_transcription_model == 'openai:gpt-4o-transcribe'
    assert updated_user.enable_auto_title_generation is True
    assert updated_user.language == 'fr'

# --- Test Prompt Management ---

def test_save_and_get_user_prompt(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user
    WHEN a new prompt is saved
    THEN check that the prompt is saved and can be retrieved.
    """
    user = get_user_by_username('testuser_permissions')
    title = 'Test Prompt'
    prompt_text = 'This is a test prompt.'
    color = '#ff0000'

    # Save the prompt
    new_prompt = user_service.save_user_prompt(user.id, title, prompt_text, color)
    assert new_prompt is not None
    assert new_prompt.title == title

    # Get the prompts
    prompts = user_service.get_user_prompts(user.id)
    assert len(prompts) == 1
    assert prompts[0].title == title

def test_update_user_prompt(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user with a saved prompt
    WHEN the prompt is updated
    THEN check that the prompt is updated correctly.
    """
    user = get_user_by_username('testuser_permissions')
    title = 'Test Prompt'
    prompt_text = 'This is a test prompt.'
    color = '#ff0000'

    # Save the prompt
    new_prompt = user_service.save_user_prompt(user.id, title, prompt_text, color)

    # Update the prompt
    updated_title = 'Updated Test Prompt'
    updated_prompt_text = 'This is the updated test prompt.'
    updated_color = '#00ff00'
    user_service.update_user_prompt(new_prompt.id, user.id, updated_title, updated_prompt_text, updated_color)

    # Get the updated prompt
    updated_prompt = user_prompt_model.get_prompt_by_id(new_prompt.id)
    assert updated_prompt.title == updated_title
    assert updated_prompt.prompt_text == updated_prompt_text
    assert updated_prompt.color == updated_color

def test_delete_user_prompt(logged_in_client_with_permissions):
    """
    GIVEN a logged-in user with a saved prompt
    WHEN the prompt is deleted
    THEN check that the prompt is no longer available.
    """
    user = get_user_by_username('testuser_permissions')
    title = 'Test Prompt'
    prompt_text = 'This is a test prompt.'
    color = '#ff0000'

    # Save the prompt
    new_prompt = user_service.save_user_prompt(user.id, title, prompt_text, color)

    # Delete the prompt
    user_service.delete_user_prompt(new_prompt.id, user.id)

    # Check that the prompt is no longer available
    prompts = user_service.get_user_prompts(user.id)
    assert len(prompts) == 0

# --- Test Template Synchronization ---

def test_sync_templates_for_user(logged_in_client_with_permissions):
    """
    GIVEN a user and some admin templates
    WHEN the template synchronization is triggered
    THEN check that the user's prompts are updated correctly.
    """
    user = get_user_by_username('testuser_permissions')
    user_service.update_profile(user.id, {'username': user.username, 'email': user.email, 'language': 'en'})

    # Create some admin templates
    template_prompt_model.add_template('Template 1', 'Prompt 1', 'en', '#ff0000')
    template_prompt_model.add_template('Template 2', 'Prompt 2', 'en', '#00ff00')
    template_prompt_model.add_template('Template 3', 'Prompt 3', 'es', '#0000ff')

    # Sync templates for the user
    user_service.sync_templates_for_user(user.id)

    # Check that the user has the correct prompts
    prompts = user_service.get_user_prompts(user.id)
    assert len(prompts) == 2
    assert prompts[0].title == 'Template 1'
    assert prompts[1].title == 'Template 2'

    # Update a template
    templates = template_prompt_model.get_templates(language='en')
    template = templates[0]
    template_prompt_model.update_template(template.id, 'Updated Template 1', 'Updated Prompt 1', 'en', '#ffffff')

    # Sync templates again
    user_service.sync_templates_for_user(user.id)

    # Check that the user's prompt is updated
    prompts = user_service.get_user_prompts(user.id)
    assert len(prompts) == 2
    assert prompts[0].title == 'Updated Template 1'

    # Delete a template
    template_prompt_model.delete_template(template.id)

    # Sync templates again
    user_service.sync_templates_for_user(user.id)

    # Check that the user's prompt is deleted
    prompts = user_service.get_user_prompts(user.id)
    assert len(prompts) == 1
    assert prompts[0].title == 'Template 2'