"""Shared submission pipeline for web and public transcription uploads."""

from dataclasses import dataclass
import logging
import os
from typing import Any, Callable, Dict, Optional, Tuple

from werkzeug.utils import secure_filename

from app.database import get_db
from app.services.openrouter import resolve_openrouter_model


@dataclass(frozen=True)
class SubmissionDependencies:
    """Runtime collaborators supplied by the route to preserve test seams."""

    catalog: Any
    files: Any
    pricing: Any
    roles: Any
    transcription: Any
    queue_submitter: Callable[..., Any]
    transcription_processor: Callable[..., Any]
    model_resolver: Callable[..., Tuple[str, Optional[str]]]


@dataclass(frozen=True)
class CatalogContext:
    model_lookup: Dict[str, Dict[str, Any]]
    active_model_codes: set[str]
    default_model_code: Optional[str]
    active_language_codes: set[str]
    default_language_code: str


@dataclass(frozen=True)
class UploadMetadata:
    original_filename: str
    temp_filename: str
    file_size_mb: float
    audio_length_seconds: float
    audio_length_minutes: float


@dataclass(frozen=True)
class SubmissionPreparation:
    provider_code: str
    api_model: Optional[str]
    cost_to_add: float


class UploadTooLargeError(ValueError):
    def __init__(self, max_size_mb: float):
        self.max_size_mb = max_size_mb
        super().__init__(f"File exceeds the {max_size_mb}MB size limit.")


class UploadProcessingError(RuntimeError):
    """Raised when the shared upload/save/metadata stage cannot complete."""


class NoAssignedRoleError(PermissionError):
    """Raised when a user cannot reserve usage without a role."""


class UsageLimitExceededError(PermissionError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class SubmissionJobCreationError(RuntimeError):
    """Raised when the transcription record cannot be created."""


class SubmissionQueueError(RuntimeError):
    """Raised when durable scheduling cannot enqueue a created submission."""


def _rollback_transaction() -> None:
    """Best-effort rollback for a submission transaction owned by this service."""
    try:
        get_db().rollback()
    except Exception:
        logging.exception("Failed to roll back transcription submission transaction.")


def build_model_lookup(models: list[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index selectable catalog rows by canonical key and unambiguous code."""
    lookup: Dict[str, Dict[str, Any]] = {}
    by_code: Dict[str, list[Dict[str, Any]]] = {}
    for model in models:
        code = str(model.get("code") or "").strip()
        model_key = str(model.get("model_key") or code).strip()
        if not code or not model_key:
            continue
        lookup[model_key] = model
        by_code.setdefault(code, []).append(model)
    for code, rows in by_code.items():
        if len(rows) == 1:
            lookup[code] = rows[0]
    return lookup


def resolve_catalog_model_parameters(
    api_choice: str,
    model_lookup: Dict[str, Dict[str, Any]],
    submitted_model: Optional[str] = None,
    *,
    catalog: Any,
) -> Tuple[str, Optional[str]]:
    """Resolve provider and provider-local model from one catalog selection."""
    model = model_lookup.get(api_choice) or catalog.get_model_by_code(api_choice) or {}
    provider = str(
        model.get("provider_code")
        or model.get("required_api_key")
        or ("openrouter" if "/" in str(api_choice or "") else "")
    ).strip().lower()
    local_model_code = str(model.get("code") or "").strip()
    if provider == "openrouter":
        if api_choice == "openrouter":
            model_name = resolve_openrouter_model(api_choice, submitted_model)
        else:
            model_name = str(
                model.get("model_slug")
                or model.get("model_name")
                or local_model_code
                or api_choice
            ).strip()
        return provider, model_name or None
    return provider, local_model_code or None


def load_catalog_context(catalog: Any, config: Any, log_prefix: str) -> CatalogContext:
    """Load model/language choices and apply the existing fallback rules."""
    try:
        catalog_models = catalog.get_active_models()
    except Exception as catalog_err:
        logging.error(
            f"{log_prefix} Failed to load transcription models from catalog: {catalog_err}",
            exc_info=True,
        )
        catalog_models = []

    model_lookup = build_model_lookup(catalog_models)
    default_model_code = next(
        (
            str(model.get("model_key") or model.get("code") or "").strip()
            for model in catalog_models
            if model.get("is_default")
        ),
        None,
    )
    if not default_model_code and catalog_models:
        default_model_code = str(
            catalog_models[0].get("model_key") or catalog_models[0].get("code") or ""
        ).strip()
    if not default_model_code:
        configured_default = config.get("DEFAULT_TRANSCRIPTION_PROVIDER")
        configured_row = catalog.get_model_by_code(configured_default)
        default_model_code = (configured_row or {}).get("model_key") or configured_default

    try:
        language_rows = catalog.get_active_languages()
    except Exception as lang_err:
        logging.error(
            f"{log_prefix} Failed to load transcription languages from catalog: {lang_err}",
            exc_info=True,
        )
        language_rows = []
    active_language_codes = {language["code"] for language in language_rows}
    default_language_code = next(
        (language["code"] for language in language_rows if language.get("is_default")),
        None,
    )
    if not default_language_code and language_rows:
        default_language_code = language_rows[0]["code"]
    if not default_language_code:
        default_language_code = config.get("DEFAULT_LANGUAGE", "auto")

    return CatalogContext(
        model_lookup=model_lookup,
        active_model_codes=set(model_lookup),
        default_model_code=default_model_code,
        active_language_codes=active_language_codes,
        default_language_code=default_language_code,
    )


def save_uploaded_audio(
    file: Any,
    *,
    upload_dir: str,
    job_id: str,
    max_size_mb: float,
    files: Any,
    log_prefix: str,
) -> UploadMetadata:
    """Persist an upload and extract duration using the shared file service."""
    original_filename = secure_filename(file.filename)
    temp_filename = os.path.join(upload_dir, f"{job_id}_{original_filename}")
    try:
        os.makedirs(upload_dir, exist_ok=True)
        if not files.validate_file_path(temp_filename, upload_dir):
            logging.error(f"{log_prefix} Invalid temporary file path generated: {temp_filename}")
            raise PermissionError("Invalid file path.")

        file.save(temp_filename)
        file_size_mb = round(os.path.getsize(temp_filename) / (1024 * 1024), 2)
        logging.info(
            f"{log_prefix} Saved temp upload: {os.path.basename(temp_filename)} "
            f"(Size: {file_size_mb:.2f} MB)"
        )
        if file_size_mb > max_size_mb:
            logging.warning(
                f"{log_prefix} File size {file_size_mb:.2f}MB exceeds limit {max_size_mb}MB."
            )
            files.remove_files([temp_filename])
            raise UploadTooLargeError(max_size_mb)

        try:
            audio_length_seconds, audio_length_minutes = files.get_audio_duration(temp_filename)
            if audio_length_seconds == 0.0:
                logging.warning(
                    f"{log_prefix} Could not determine audio duration for "
                    f"'{os.path.basename(temp_filename)}'. Assuming 0 minutes."
                )
        except Exception as audio_err:
            logging.error(
                f"{log_prefix} Error getting audio duration for "
                f"'{os.path.basename(temp_filename)}': {audio_err}",
                exc_info=True,
            )
            audio_length_seconds = 0.0
            audio_length_minutes = 0.0

        return UploadMetadata(
            original_filename=original_filename,
            temp_filename=temp_filename,
            file_size_mb=file_size_mb,
            audio_length_seconds=audio_length_seconds,
            audio_length_minutes=audio_length_minutes,
        )
    except UploadTooLargeError:
        raise
    except Exception as upload_err:
        logging.exception(f"{log_prefix} Failed during file save or metadata extraction: {upload_err}")
        if os.path.exists(temp_filename):
            files.remove_files([temp_filename])
        raise UploadProcessingError(str(upload_err)) from upload_err


def prepare_submission(
    dependencies: SubmissionDependencies,
    *,
    user: Any,
    user_id: int,
    api_choice: str,
    model_lookup: Dict[str, Dict[str, Any]],
    submitted_model: Optional[str],
    audio_length_seconds: float,
    audio_length_minutes: float,
    commit: bool = True,
) -> SubmissionPreparation:
    """Resolve provider identity, quote cost, and reserve usage atomically."""
    try:
        provider_code, api_model = dependencies.model_resolver(
            api_choice,
            model_lookup,
            submitted_model,
        )
        pricing_key = api_model if provider_code == "openrouter" else api_choice
        price = dependencies.pricing.get_price(
            item_type="transcription",
            item_key=pricing_key,
        )
        cost_to_add = 0.0
        if price is not None:
            cost_to_add = price * (
                audio_length_minutes if audio_length_minutes >= 1 else audio_length_seconds / 60
            )

        role = getattr(user, "role", None)
        if not role:
            raise NoAssignedRoleError("You do not have a role assigned.")
        reservation_kwargs = {
            "cost_to_add": cost_to_add,
            "minutes_to_add": audio_length_minutes,
        }
        if not commit:
            reservation_kwargs["commit"] = False
        allowed, reason = dependencies.roles.reserve_usage_if_allowed(
            user_id,
            role,
            **reservation_kwargs,
        )
        if not allowed:
            raise UsageLimitExceededError(reason)

        return SubmissionPreparation(
            provider_code=provider_code,
            api_model=api_model,
            cost_to_add=cost_to_add,
        )
    except Exception:
        if not commit:
            _rollback_transaction()
        raise


def create_and_schedule_job(
    dependencies: SubmissionDependencies,
    *,
    app: Any,
    job_id: str,
    user_id: int,
    upload: UploadMetadata,
    api_choice: str,
    preparation: SubmissionPreparation,
    context_prompt_used: bool,
    pending_workflow_prompt_text: Optional[str] = None,
    pending_workflow_prompt_title: Optional[str] = None,
    pending_workflow_prompt_color: Optional[str] = None,
    pending_workflow_origin_prompt_id: Optional[int] = None,
    public_api_invocation: bool = False,
    language_code: str = "auto",
    context_prompt: str = "",
    speaker_diarization_enabled: bool = False,
) -> None:
    """Create the durable job record and submit it to the bounded queue."""
    try:
        try:
            dependencies.transcription.create_transcription_job(
                job_id=job_id,
                user_id=user_id,
                filename=upload.original_filename,
                api_used=api_choice,
                file_size_mb=upload.file_size_mb,
                audio_length_minutes=upload.audio_length_minutes,
                context_prompt_used=context_prompt_used,
                pending_workflow_prompt_text=pending_workflow_prompt_text or None,
                pending_workflow_prompt_title=pending_workflow_prompt_title or None,
                pending_workflow_prompt_color=pending_workflow_prompt_color or None,
                pending_workflow_origin_prompt_id=pending_workflow_origin_prompt_id,
                public_api_invocation=public_api_invocation,
                api_model=preparation.api_model,
                commit=False,
            )
        except Exception as exc:
            raise SubmissionJobCreationError(str(exc)) from exc

        try:
            dependencies.queue_submitter(
                app.config,
                dependencies.transcription_processor,
                app,
                job_id,
                user_id,
                upload.temp_filename,
                language_code,
                api_choice,
                upload.original_filename,
                context_prompt,
                pending_workflow_prompt_text,
                pending_workflow_prompt_title,
                pending_workflow_prompt_color,
                pending_workflow_origin_prompt_id,
                speaker_diarization_enabled,
                preparation.api_model,
                commit=False,
            )
        except Exception as exc:
            raise SubmissionQueueError(str(exc)) from exc

        get_db().commit()
    except Exception:
        _rollback_transaction()
        raise
