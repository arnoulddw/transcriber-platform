# ./app/services/api_clients/base_client.py
# Defines the Abstract Base Class for transcription API clients, handling common workflow logic.
import os
import logging
import time
import concurrent.futures
import threading
from abc import ABC, abstractmethod
from typing import Tuple, Optional, Callable, Dict, List, Any, Type

# Import project-specific services and config
from app.services import file_service
from app.config import Config # To access language codes if needed

# Define type hint for the progress callback function
ProgressCallback = Optional[Callable[[str, bool], None]] # Args: message, is_error

UNKNOWN_LANGUAGE_CODE = 'unknown'

class BaseTranscriptionClient(ABC):
    """
    Abstract Base Class for transcription API clients.
    Handles the common workflow: file size checks, splitting,
    parallel/sequential processing, retries, progress reporting,
    cancellation checks, and cleanup.
    """
    # Default max concurrent chunks, can be overridden by subclasses
    MAX_CONCURRENT_CHUNKS: int = 4

    def __init__(self, api_key: str) -> None:
        """
        Initializes the base client and calls the subclass's initializer.

        Args:
            api_key: The API key for the specific service.

        Raises:
            ValueError: If the API key is not provided or client initialization fails.
        """
        if not api_key:
            api_name = self._get_api_name() # Get name from subclass
            logging.error(f"[{api_name}] API key is required but not provided.")
            raise ValueError(f"{api_name} API key is required.")
        self.api_key = api_key
        self.client = None # Subclass initializer should set this
        self._initialize_client(api_key) # Call abstract method for subclass setup
        self.progress_callback: ProgressCallback = None # Store callback per transcribe call
        self.cancel_event: Optional[threading.Event] = None # Store cancel event per transcribe call

    # --- Abstract Methods (Must be implemented by subclasses) ---

    @abstractmethod
    def _initialize_client(self, api_key: str) -> None:
        """Initialize the specific SDK client (e.g., OpenAI(), aai.Transcriber())."""
        pass

    @abstractmethod
    def _prepare_api_params(self, language_code: str, context_prompt: str, response_format: str,
                            is_chunk: bool, extra_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Prepare the dictionary of parameters for the API call, excluding the file handle.
        Args:
            language_code: Requested language code ('auto' or specific).
            context_prompt: User-provided context.
            response_format: Desired response format ('text', 'json', 'verbose_json', etc.).
            is_chunk: Boolean indicating if this is for a chunk (True) or a single file (False).
            extra_options: Optional dictionary of provider-specific switches.
        Returns:
            Dictionary of API parameters.
        """
        pass

    @abstractmethod
    def _call_api(self, file_handle: Any, api_params: Dict[str, Any]) -> Any:
        """
        Make the actual transcription API call using the initialized client.
        Args:
            file_handle: The opened file handle (e.g., opened with 'rb').
            api_params: Dictionary of parameters prepared by _prepare_api_params.
        Returns:
            The raw response object from the API call.
        """
        pass

    @abstractmethod
    def _process_response(self, response: Any, response_format: str) -> Tuple[str, Optional[str]]:
        """
        Parse the raw API response to extract the transcription text and detected language.
        Args:
            response: The raw response object from _call_api.
            response_format: The response format requested (helps in parsing).
        Returns:
            Tuple (transcription_text: str, detected_language: Optional[str]).
            detected_language should be the language code string (e.g., 'en') or None.
        """
        pass

    @abstractmethod
    def _get_retryable_errors(self) -> Tuple[Type[Exception], ...]:
        """
        Return a tuple of API-specific exception classes that warrant a retry.
        (e.g., RateLimitError, APIConnectionError).
        """
        pass

    @abstractmethod
    def _get_api_name(self) -> str:
        """Return the display name of the API, resolved from the model catalog."""
        pass

    # --- Common Workflow Methods ---

    def _report_progress(self, message: str, is_error: bool = False) -> None:
        """
        Internal helper to report progress via logging and the callback.
        Checks for cancellation via the stored event and callback.
        """
        # Check internal cancel_event first
        if self.cancel_event and self.cancel_event.is_set():
            # <<< MODIFICATION: Raise exception if event is set >>>
            logging.info(f"[{self._get_api_name()}] Cancellation detected by internal event.")
            raise InterruptedError("Job cancelled (event set).")
            # <<< END MODIFICATION >>>

        log_prefix = f"[{self._get_api_name()}]" # Use API name from subclass
        logging.log(logging.ERROR if is_error else logging.INFO, f"{log_prefix} {message}")

        if self.progress_callback:
            try:
                # The callback itself might raise InterruptedError if it checks external signals
                self.progress_callback(message, is_error)
            except InterruptedError as ie: # <<< MODIFICATION: Catch specific error >>>
                logging.info(f"{log_prefix} Cancellation detected by progress callback.")
                if self.cancel_event: self.cancel_event.set() # Ensure event is set
                raise ie # <<< MODIFICATION: Re-raise the caught exception >>>
            except Exception as cb_err:
                logging.error(f"{log_prefix} Error executing progress callback: {cb_err}", exc_info=True)
                # Do not raise other callback errors, just log them

    def transcribe(self, audio_file_path: str, language_code: str,
                   progress_callback: ProgressCallback = None,
                   context_prompt: str = "",
                   original_filename: Optional[str] = None,
                   cancel_event: Optional[threading.Event] = None,
                   extra_options: Optional[Dict[str, Any]] = None
                   ) -> Tuple[Optional[str], Optional[str]]:
        """
        Transcribes the audio file using the specific API implementation. Handles splitting.

        Args:
            audio_file_path: Absolute path to the audio file.
            language_code: Language code ('auto' or specific like 'en', 'es').
            progress_callback: Optional function to report progress.
            context_prompt: Optional context/prompt string.
            original_filename: Original name of the file for logging.
            cancel_event: Optional threading.Event to signal cancellation.
            extra_options: Optional provider-specific flags (e.g., diarization).

        Returns:
            Tuple (transcription_text, detected_language).
            detected_language is the code used or detected.
        """
        # Store callback and cancel event for use by helper methods
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event or threading.Event() # Use provided or create new

        requested_language = language_code
        display_filename = original_filename or os.path.basename(audio_file_path)
        log_prefix = f"[{self._get_api_name()}:{display_filename}]" # Use API name from subclass

        self._report_progress("Starting transcription process...")
        transcription_text: Optional[str] = None
        final_detected_language: Optional[str] = None

        try:
            # Validate file existence
            if not os.path.exists(audio_file_path):
                self._report_progress(f"ERROR: Audio file not found: {audio_file_path}", True)
                return None, None

            file_size = os.path.getsize(audio_file_path)

            # Check if file needs splitting (using OpenAI's limit as a general threshold)
            if file_size > file_service.OPENAI_MAX_FILE_SIZE_BYTES:
                mode = "PARALLEL (no prompt)" if not context_prompt else "SEQUENTIAL (prompt provided)"
                self._report_progress(f"File size ({file_size / 1024 / 1024:.2f}MB) exceeds limit. Starting {mode} chunked transcription.")
                # Delegate to splitting method
                return self._split_and_transcribe(audio_file_path, requested_language, context_prompt, display_filename, extra_options=extra_options)
            else:
                # Process single file
                self._report_progress(f"File size ({file_size / 1024 / 1024:.2f}MB) within limit. Processing as single file.")

                # Validate file path is within allowed directory (security measure)
                abs_path = os.path.abspath(audio_file_path)
                temp_dir = os.path.dirname(abs_path) # Assuming file is in a temp dir
                if not file_service.validate_file_path(abs_path, temp_dir):
                    msg = f"ERROR: Audio file path is not allowed: {abs_path}"
                    self._report_progress(msg, True)
                    raise ValueError(msg) # Raise error to stop processing

                # --- Single File Transcription Attempt ---
                # Determine response format based on language request
                response_format = "verbose_json" if requested_language == 'auto' else "text" # Default logic, can be overridden by subclass _prepare_api_params

                # Prepare API parameters using abstract method
                api_params = self._prepare_api_params(
                    language_code=requested_language,
                    context_prompt=context_prompt,
                    response_format=response_format,
                    is_chunk=False,
                    extra_options=extra_options
                )
                # Update response_format based on what _prepare_api_params decided
                response_format = api_params.get("response_format", response_format)

                log_params = {k: v for k, v in api_params.items() if k != 'file'} # Exclude file from log
                logging.info(f"{log_prefix} Calling API for single file with parameters: {log_params}")

                # Open file and make API call using abstract methods
                with open(abs_path, "rb") as audio_file:
                    self._report_progress(f"Transcribing with {self._get_api_name()}...")
                    start_time = time.time()
                    # Check cancellation before API call (via _report_progress)
                    self._report_progress("Checking cancellation before API call...") # Implicit check
                    raw_response = self._call_api(audio_file, api_params)
                    duration = time.time() - start_time
                    logging.debug(f"{log_prefix} API call successful. Duration: {duration:.2f}s")

                # Process response using abstract method
                transcription_text, final_detected_language = self._process_response(raw_response, response_format)

                # If auto-detect was used but language wasn't returned by _process_response, log it
                if requested_language == 'auto':
                    if not final_detected_language:
                        logging.warning(f"{log_prefix} Language auto-detection requested, but final language not determined by _process_response.")
                        final_detected_language = UNKNOWN_LANGUAGE_CODE

                # If specific language was requested, ensure it's set as final
                elif requested_language != 'auto':
                    final_detected_language = requested_language

            # Final success reporting
            log_lang_msg = f"Transcription finished. Final language: {final_detected_language}"
            logging.info(f"{log_prefix} {log_lang_msg}")
            ui_finish_msg = f"{self._get_api_name()} transcription finished. Final language: {final_detected_language}"
            self._report_progress(ui_finish_msg, False)
            # --- ADDED: Explicit phase marker ---
            self._report_progress("PHASE_MARKER:TRANSCRIPTION_COMPLETE", False)
            # --- END ADDED ---
            self._report_progress("Transcription completed.", False)

            return transcription_text, final_detected_language

        # --- Exception Handling ---
        # Catch specific retryable errors first if needed at this level (less likely)
        except tuple(self._get_retryable_errors()) as retry_err:
            self._report_progress(f"ERROR: {self._get_api_name()} API connection/rate limit error: {retry_err}. Try again later.", True)
            logging.warning(f"{log_prefix} Retryable error at top level: {retry_err}")
            return None, None
        except FileNotFoundError as fnf_error:
            self._report_progress(f"ERROR: Audio file disappeared: {fnf_error}", True)
            return None, None
        except ValueError as ve: # Catch path validation errors etc.
            self._report_progress(f"ERROR: Input Error: {ve}", True)
            logging.error(f"{log_prefix} Value error: {ve}")
            return None, None
        except InterruptedError:
            logging.info(f"{log_prefix} Transcription interrupted by cancellation signal.")
            raise # Re-raise to be caught by the main service
        except Exception as e:
            self._report_progress(f"ERROR: Unexpected error during transcription: {e}", True)
            logging.exception(f"{log_prefix} Unexpected error detail:")
            return None, None
        finally:
            # Reset instance variables after processing
            self.progress_callback = None
            self.cancel_event = None


    def _split_and_transcribe(self, audio_file_path: str, language_code: str,
                             context_prompt: str = "",
                             display_filename: Optional[str] = None,
                             extra_options: Optional[Dict[str, Any]] = None
                             ) -> Tuple[Optional[str], Optional[str]]:
        """
        Handles splitting large files and transcribing chunks.
        Uses parallel execution if no context prompt is provided, otherwise sequential.
        Relies on the stored progress_callback and cancel_event. The extra_options dict
        is forwarded to provider-specific parameter preparation.
        """
        requested_language = language_code # Store the original request
        mode_log = "Parallel" if not context_prompt else "Sequential"
        log_prefix = f"[{self._get_api_name()}:{display_filename or os.path.basename(audio_file_path)}:{mode_log}]"

        temp_dir = os.path.dirname(audio_file_path)
        chunk_files: List[str] = []
        final_language_used: Optional[str] = None
        # Store results keyed by chunk number (1-based), value is tuple (text, detected_lang)
        results: Dict[int, Optional[Tuple[str, Optional[str]]]] = {}
        overall_success = True

        try:
            # --- ADDED: Explicit phase marker ---
            self._report_progress("PHASE_MARKER:PROCESSING_START", False)
            # --- END ADDED ---
            # Split the audio file using the file service
            # Pass the instance's _report_progress method as the callback for splitting
            chunk_files = file_service.split_audio_file(audio_file_path, temp_dir, self._report_progress)
            if not chunk_files:
                raise Exception("Audio splitting failed or resulted in no chunks.")

            total_chunks = len(chunk_files)
            # --- ADDED: Explicit phase marker ---
            self._report_progress("PHASE_MARKER:TRANSCRIPTION_START", False)
            # --- END ADDED ---

            if not context_prompt:
                # --- PARALLEL Processing (No Context Prompt) ---
                self._report_progress(f"Starting parallel transcription of {total_chunks} chunks (max workers: {self.MAX_CONCURRENT_CHUNKS})...")
                futures = []

                with concurrent.futures.ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_CHUNKS) as executor:
                    for idx, chunk_path in enumerate(chunk_files):
                        # Check cancellation before submitting new chunk
                        if self.cancel_event.is_set():
                            logging.info(f"{log_prefix} Cancellation detected, skipping submission of chunk {idx+1}.")
                            results[idx+1] = None # Mark as skipped/failed
                            overall_success = False
                            continue

                        chunk_num = idx + 1
                        chunk_log_prefix = f"{log_prefix}:Chunk{chunk_num}"
                        # Determine language param and response format for this chunk
                        current_chunk_lang_param = requested_language
                        response_format = "verbose_json" if requested_language == 'auto' else "text" # Default logic
                        logging.info(f"{chunk_log_prefix} Submitting chunk {chunk_num} with language '{current_chunk_lang_param}'.")

                        # Submit task using keyword arguments
                        future = executor.submit(
                            self._transcribe_single_chunk_with_retry,
                            chunk_path=chunk_path,
                            idx=chunk_num,
                            total_chunks=total_chunks,
                            language_code=current_chunk_lang_param,
                            # Pass base response format, _prepare_api_params will refine it
                            response_format=response_format,
                            context_prompt="", # No prompt for parallel
                            log_prefix=chunk_log_prefix,
                            extra_options=extra_options
                            # max_retries is handled by the method default
                            # cancel_event and progress_callback are accessed via self
                        )
                        futures.append((chunk_num, future))

                    self._report_progress(f"Waiting for {len(futures)} submitted chunk(s) to complete...", False)
                    # Collect results
                    for chunk_num, future in futures:
                        try:
                            chunk_result = future.result() # Returns tuple (text, lang) or raises exception
                            results[chunk_num] = chunk_result
                            logging.info(f"{log_prefix}:Chunk{chunk_num} completed successfully.")
                        except InterruptedError:
                             overall_success = False
                             results[chunk_num] = None
                             logging.info(f"{log_prefix}:Chunk{chunk_num} processing interrupted by cancellation.")
                             self._report_progress(f"Chunk {chunk_num} cancelled.", False)
                             self.cancel_event.set() # Ensure event is set
                        except Exception as exc:
                            overall_success = False
                            results[chunk_num] = None # Mark as failed
                            logging.error(f"{log_prefix}:Chunk{chunk_num} failed: {exc}")
                            self._report_progress(f"ERROR: Chunk {chunk_num} failed: {exc}", True)

                # Check for cancellation *after* parallel loop completes
                if self.cancel_event.is_set():
                    logging.info(f"{log_prefix} Cancellation detected after parallel chunk processing.")
                    raise InterruptedError("Job cancelled during parallel chunk processing.")

            else:
                # --- SEQUENTIAL Processing (Context Prompt Provided) ---
                self._report_progress(f"Starting sequential transcription of {total_chunks} chunks (prompt provided)...")
                for idx, chunk_path in enumerate(chunk_files):
                    # Cancellation is checked within _report_progress called by the retry method

                    chunk_num = idx + 1
                    chunk_log_prefix = f"{log_prefix}:Chunk{chunk_num}"
                    # Determine language param and response format for this chunk
                    current_chunk_lang_param = requested_language
                    response_format = "verbose_json" if requested_language == 'auto' else "text" # Default logic
                    logging.info(f"{chunk_log_prefix} Starting chunk {chunk_num} with language '{current_chunk_lang_param}'.")

                    try:
                        # Transcribe chunk sequentially
                        chunk_result = self._transcribe_single_chunk_with_retry(
                            chunk_path=chunk_path,
                            idx=chunk_num,
                            total_chunks=total_chunks,
                            language_code=current_chunk_lang_param,
                            response_format=response_format,
                            context_prompt=context_prompt,
                            log_prefix=chunk_log_prefix,
                            extra_options=extra_options
                            # cancel_event and progress_callback are accessed via self
                        )
                        results[chunk_num] = chunk_result
                        logging.info(f"{chunk_log_prefix} completed successfully.")

                        # If first chunk and auto-detect, report the detected language
                        if requested_language == 'auto' and idx == 0:
                            _, detected_lang = chunk_result
                            if detected_lang:
                                logging.info(f"{log_prefix} Detected language '{detected_lang}' from first chunk.")
                                self._report_progress(f"Detected language: {detected_lang}", False)
                            else:
                                logging.warning(f"{log_prefix} First chunk language detection failed or returned None.")
                                self._report_progress("First chunk language detection failed or returned None.", False)

                    except InterruptedError:
                         overall_success = False
                         results[chunk_num] = None
                         logging.info(f"{log_prefix}:Chunk{chunk_num} processing interrupted by cancellation.")
                         self._report_progress(f"Chunk {chunk_num} cancelled.", False)
                         raise # Re-raise to stop sequential processing immediately
                    except Exception as exc:
                        overall_success = False
                        results[chunk_num] = None # Mark as failed
                        logging.error(f"{chunk_log_prefix} failed: {exc}")
                        self._report_progress(f"ERROR: Chunk {chunk_num} failed: {exc}", True)
                        raise Exception(f"Sequential chunk {chunk_num} failed, aborting.") from exc

            # --- Aggregation ---
            if not overall_success:
                if self.cancel_event.is_set():
                    logging.info(f"{log_prefix} Aggregation skipped due to cancellation.")
                    raise InterruptedError("Job cancelled during chunk processing.")
                else:
                    logging.error(f"{log_prefix} One or more chunks failed transcription. Aggregation skipped.")
                    raise Exception("Chunk transcription failed. See logs for details.")

            transcription_texts = []
            detected_languages = []
            first_successful_lang = None
            successful_chunks = 0

            for i in range(total_chunks):
                chunk_num = i + 1
                result_tuple = results.get(chunk_num)
                if result_tuple:
                    text, lang = result_tuple
                    transcription_texts.append(text)
                    successful_chunks += 1
                    if lang:
                        detected_languages.append(lang)
                        if first_successful_lang is None:
                            first_successful_lang = lang

            full_transcription = " ".join(filter(None, transcription_texts))
            logging.info(f"{log_prefix} Successfully aggregated transcriptions from {successful_chunks} completed chunks.")

            # Determine final language used/detected
            if requested_language == 'auto':
                if first_successful_lang:
                    final_language_used = first_successful_lang
                else:
                    logging.warning(f"{log_prefix} Language auto-detection requested, but no chunk provided a detected language.")
                    final_language_used = UNKNOWN_LANGUAGE_CODE
                log_lang_msg = f"{mode_log} chunked transcription aggregated. Final language (detected/fallback): {final_language_used}"
                ui_lang_msg = f"Aggregated chunk transcriptions. Final language (detected/fallback): {final_language_used}"
            else:
                final_language_used = requested_language
                log_lang_msg = f"{mode_log} chunked transcription aggregated. Used requested language: {final_language_used}"
                ui_lang_msg = f"Aggregated chunk transcriptions. Used requested language: {final_language_used}"

            logging.info(f"{log_prefix} {log_lang_msg}")
            self._report_progress(ui_lang_msg, False)
            # --- ADDED: Explicit phase marker ---
            self._report_progress("PHASE_MARKER:TRANSCRIPTION_COMPLETE", False)
            # --- END ADDED ---
            self._report_progress("Transcription completed.", False)

            return full_transcription, final_language_used

        except InterruptedError:
             logging.info(f"{log_prefix} Split/transcribe process interrupted by cancellation.")
             raise # Re-raise to be caught by the main service
        except Exception as e:
            error_msg = f"ERROR: Error during {mode_log.lower()} split/transcribe process: {e}"
            if overall_success:
                 self._report_progress(error_msg, True)
            logging.exception(f"{log_prefix} Error detail in _split_and_transcribe:")
            return None, None # Indicate failure
        finally:
            # Cleanup chunk files
            if chunk_files:
                self._report_progress("Cleaning up temporary chunk files...", False)
                removed_count = file_service.remove_files(chunk_files)
                logging.info(f"{log_prefix} Cleaned up {removed_count} temporary chunk file(s).")


    def _transcribe_single_chunk_with_retry(self, chunk_path: str, idx: int, total_chunks: int,
                                            language_code: str, response_format: str,
                                            context_prompt: str = "", log_prefix: str = "", max_retries: int = 3,
                                            extra_options: Optional[Dict[str, Any]] = None
                                            ) -> Tuple[str, Optional[str]]:
        """
        Transcribes a single audio chunk with retry logic.
        Relies on the stored progress_callback and cancel_event from the instance.

        Args:
            chunk_path: Path to the audio chunk file.
            idx: The 1-based index of the chunk.
            total_chunks: Total number of chunks.
            language_code: Requested language code ('auto' or specific).
            response_format: Base response format ('text' or 'verbose_json').
            context_prompt: Context prompt string.
            log_prefix: Prefix for console logging.
            max_retries: Maximum number of retry attempts.
            extra_options: Optional provider-specific feature flags.

        Returns:
            Tuple (transcription_text, detected_language).

        Raises:
            Exception: If transcription fails after all retries.
            InterruptedError: If cancellation is detected.
        """
        last_error: Optional[Exception] = None
        chunk_base_name = os.path.basename(chunk_path)
        effective_log_prefix = log_prefix or f"[{self._get_api_name()}:Chunk{idx}]"

        # --- Retry Loop ---
        for attempt in range(max_retries + 1): # +1 to allow for initial try
            try:
                # Check cancellation before starting attempt (via _report_progress)
                self._report_progress(f"Starting chunk {idx}/{total_chunks} (Attempt {attempt+1})", False)

                # Validate chunk file path
                abs_chunk_path = os.path.abspath(chunk_path)
                temp_dir = os.path.dirname(abs_chunk_path)
                if not file_service.validate_file_path(abs_chunk_path, temp_dir):
                    raise ValueError(f"Chunk file path is not allowed: {abs_chunk_path}")

                # Prepare API parameters using abstract method
                api_params = self._prepare_api_params(
                    language_code=language_code,
                    context_prompt=context_prompt,
                    response_format=response_format,
                    is_chunk=True,
                    extra_options=extra_options
                )
                actual_response_format = api_params.get("response_format", response_format)

                log_params = {k: v for k, v in api_params.items() if k != 'file'}
                prompt_log = f", Prompt: '{context_prompt[:30]}...'" if context_prompt else ""
                logging.debug(f"{effective_log_prefix} Attempt {attempt+1}: Calling API with parameters: {log_params}{prompt_log}")

                # Open chunk file and make API call using abstract method
                with open(abs_chunk_path, "rb") as audio_file:
                    start_time = time.time()
                    # Check cancellation before API call (via _report_progress)
                    self._report_progress("Checking cancellation before API call...") # Implicit check
                    raw_response = self._call_api(audio_file, api_params)
                    duration = time.time() - start_time
                    logging.debug(f"{effective_log_prefix} Attempt {attempt+1}: API call successful. Duration: {duration:.2f}s")

                # Process response using abstract method
                text, detected_lang = self._process_response(raw_response, actual_response_format)

                self._report_progress(f"Finished chunk {idx}/{total_chunks}.", False)
                return (text.strip() if text else ""), detected_lang

            # --- Exception Handling within Retry Loop ---
            except InterruptedError:
                 logging.info(f"{effective_log_prefix} Chunk {idx} cancelled during attempt {attempt+1}.")
                 raise # Re-raise immediately to stop retries
            except tuple(self._get_retryable_errors()) as retry_err:
                last_error = retry_err
                if attempt < max_retries: # Check if more retries are allowed
                    wait_time = 2 ** attempt # Exponential backoff
                    error_detail = f"Retryable error on chunk {idx}, attempt {attempt+1}. Retrying in {wait_time}s... ({type(retry_err).__name__})"
                    self._report_progress(error_detail, False) # Report retry as info
                    logging.warning(f"{effective_log_prefix} {error_detail}: {retry_err}")
                    time.sleep(wait_time)
                else:
                    # Max retries reached, raise the last error
                    logging.error(f"{effective_log_prefix} Max retries ({max_retries}) reached for chunk {idx}. Last error: {retry_err}")
                    raise Exception(f"Chunk {idx} failed after {max_retries} retries: {retry_err}") from retry_err
            except FileNotFoundError as fnf_error:
                last_error = fnf_error
                error_detail = f"ERROR: Chunk file not found: {chunk_base_name}. Error: {fnf_error}"
                self._report_progress(error_detail, True)
                logging.error(f"{effective_log_prefix} Chunk file not found on attempt {attempt+1}: {chunk_base_name}. Error: {fnf_error}")
                raise Exception(f"Chunk file not found: {chunk_base_name}") from fnf_error # Fail fast
            except ValueError as ve: # Handle input/path errors
                last_error = ve
                error_detail = f"ERROR: Input error processing chunk {idx}: {ve}"
                self._report_progress(error_detail, True)
                logging.error(f"{effective_log_prefix} {error_detail}")
                raise Exception(f"Input error processing chunk {idx}: {ve}") from ve # Fail fast
            except Exception as e: # Catch any other unexpected errors (including non-retryable API errors)
                last_error = e
                error_detail = f"ERROR: Non-retryable error transcribing chunk {idx}: {e}"
                self._report_progress(error_detail, True)
                logging.exception(f"{effective_log_prefix} Non-retryable error detail on attempt {attempt+1}:")
                # Fail fast on non-retryable errors
                raise Exception(f"Non-retryable error transcribing chunk {idx}: {e}") from e

        # --- After Retry Loop (Should only be reached if retries exhausted) ---
        final_error_msg = f"ERROR: Chunk {idx} ('{chunk_base_name}') failed after {max_retries} attempts. Last error: {last_error}"
        self._report_progress(final_error_msg, True)
        logging.error(f"{effective_log_prefix} Chunk {idx} failed after {max_retries} attempts. Last error: {last_error}")
        raise Exception(f"Chunk {idx} failed after {max_retries} attempts: {last_error}")
