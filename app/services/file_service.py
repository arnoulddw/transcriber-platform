# app/services/file_service.py
# Handles file-related operations like validation, splitting, and cleanup.
# (No changes needed for MySQL migration)

import os
import time
import logging
import math
import subprocess
import json
import glob
import time
from typing import List, Callable, Optional, Tuple

# --- Configuration Constants ---
ALLOWED_EXTENSIONS = {'mp3', 'm4a', 'wav', 'ogg', 'webm', 'mpga', 'mpeg'}
DEFAULT_CHUNK_LENGTH_MS = 7 * 60 * 1000
OPENAI_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024
TARGET_CHUNK_SIZE_BYTES = 24 * 1024 * 1024
CHUNK_AUDIO_BITRATE = "64k"
CHUNK_AUDIO_SAMPLE_RATE = 16000
IGNORE_FILES = {'.DS_Store', '.gitkeep'}

# --- Helper Functions ---

def allowed_file(filename: str) -> bool:
    """Checks if the file extension is in the allowed set."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def ordinal(n: int) -> str:
    """Returns the ordinal string for a number (e.g., 1st, 2nd, 3rd)."""
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"

def validate_file_path(file_path: str, allowed_dir: str) -> bool:
    """
    Validates that a file path is securely located within an allowed directory.
    Prevents directory traversal attacks.
    """
    try:
        abs_allowed_dir = os.path.abspath(allowed_dir)
        abs_file_path = os.path.abspath(file_path)
        is_valid = os.path.commonpath([abs_allowed_dir, abs_file_path]) == abs_allowed_dir
        if not is_valid:
            logging.warning(f"[SERVICE:File:ValidatePath] Path validation failed: '{file_path}' is outside allowed directory '{allowed_dir}'.")
        return is_valid
    except ValueError:
        logging.warning(f"[SERVICE:File:ValidatePath] Path validation error (e.g., different drives) for '{file_path}' against '{allowed_dir}'.")
        return False
    except Exception as e:
        logging.error(f"[SERVICE:File:ValidatePath] Unexpected error validating path '{file_path}': {e}", exc_info=True)
        return False

def get_audio_duration(file_path: str) -> Tuple[float, float]:
    """
    Gets the duration of an audio/video file in seconds and minutes using ffprobe.
    This is memory-efficient as it doesn't load the whole file.

    Args:
        file_path: The absolute path to the media file.

    Returns:
        A tuple containing (duration_in_seconds, duration_in_minutes).
        Returns (0.0, 0.0) if duration cannot be determined.
    """
    log_prefix = f"[FileService:GetDuration]"
    if not os.path.exists(file_path):
        logging.error(f"{log_prefix} File not found at path: {file_path}")
        return 0.0, 0.0

    command = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        file_path
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        media_info = json.loads(result.stdout)

        duration_seconds = 0.0
        # Try to get duration from format first, then from streams
        if 'format' in media_info and 'duration' in media_info['format']:
            duration_seconds = float(media_info['format']['duration'])
        elif 'streams' in media_info and media_info['streams']:
            # Find the first stream with a duration
            for stream in media_info['streams']:
                if 'duration' in stream: # Ensure duration is present
                    duration_seconds = float(stream['duration'])
                    break
        
        if duration_seconds > 0:
            duration_minutes = round(duration_seconds / 60.0, 2)
            logging.debug(f"{log_prefix} Determined duration for {os.path.basename(file_path)}: {duration_seconds:.2f}s ({duration_minutes:.2f} min)")
            return duration_seconds, duration_minutes
        else:
            logging.warning(f"{log_prefix} Could not determine duration from ffprobe output for {os.path.basename(file_path)}.")
            return 0.0, 0.0

    except FileNotFoundError:
        logging.error(f"{log_prefix} 'ffprobe' command not found. Make sure FFmpeg is installed and in the system's PATH.")
        return 0.0, 0.0
    except subprocess.CalledProcessError as e:
        logging.error(f"{log_prefix} ffprobe failed for {os.path.basename(file_path)}. Error: {e.stderr}")
        return 0.0, 0.0
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logging.error(f"{log_prefix} Failed to parse ffprobe output for {os.path.basename(file_path)}. Error: {e}")
        return 0.0, 0.0
    except Exception as e:
        logging.error(f"{log_prefix} An unexpected error occurred while getting duration for {os.path.basename(file_path)}: {e}", exc_info=True)
        return 0.0, 0.0


def _parse_audio_bitrate_bytes_per_second(bitrate: str) -> int:
    """Parse ffmpeg-style bitrates such as '64k' into bytes per second."""
    normalized = bitrate.strip().lower()
    multiplier = 1
    if normalized.endswith("k"):
        multiplier = 1000
        normalized = normalized[:-1]
    elif normalized.endswith("m"):
        multiplier = 1000 * 1000
        normalized = normalized[:-1]

    bits_per_second = int(float(normalized) * multiplier)
    return max(1, bits_per_second // 8)


def _segment_audio_ffmpeg(
    source_path: str,
    output_pattern: str,
    segment_seconds: float,
    cancellation_check: Optional[Callable[[], bool]] = None,
) -> None:
    """Create every compressed audio chunk in one controlled FFmpeg pass."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", source_path,
        "-vn",
        "-ac", "1",
        "-ar", str(CHUNK_AUDIO_SAMPLE_RATE),
        "-b:a", CHUNK_AUDIO_BITRATE,
        "-f", "segment",
        "-segment_time", f"{segment_seconds:.3f}",
        "-segment_start_number", "1",
        "-reset_timestamps", "1",
        output_pattern,
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while process.poll() is None:
            if cancellation_check and cancellation_check():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise InterruptedError("Audio splitting cancelled.")
            time.sleep(0.25)
        stdout, stderr = process.communicate()
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, command, output=stdout, stderr=stderr
            )
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise


# --- Core File Operations ---

def split_audio_file(file_path: str, temp_dir: str,
                     progress_callback: Optional[Callable[[str, bool], None]] = None,
                     cancellation_check: Optional[Callable[[], bool]] = None,
                     chunk_length_ms: int = DEFAULT_CHUNK_LENGTH_MS,
                     target_chunk_size_bytes: int = TARGET_CHUNK_SIZE_BYTES
                     ) -> List[str]:
    """
    Splits a large audio file into smaller MP3 chunks suitable for transcription APIs.
    Adjusts chunk duration dynamically to attempt to stay under target_chunk_size_bytes,
    while also respecting the maximum chunk_length_ms.
    Checks for cancellation signal via the progress_callback.

    Args:
        file_path: Absolute path to the input audio file.
        temp_dir: Directory to save the output chunk files.
        progress_callback: Optional function to report progress updates. Can raise InterruptedError.
        chunk_length_ms: Maximum desired length of each chunk in milliseconds.
        target_chunk_size_bytes: Target maximum size in bytes for each output chunk.

    Returns:
        A list of absolute paths to the created chunk files, or an empty list on failure or cancellation.
    """
    base_name_orig = os.path.basename(file_path)
    log_prefix = f"[SERVICE:File:Split:{base_name_orig}]"

    def report_progress(message: str, is_error: bool = False):
        # This helper now only logs and calls the external callback.
        # It does NOT catch InterruptedError itself anymore.
        logging.log(logging.ERROR if is_error else logging.INFO, f"{log_prefix} {message}")
        if progress_callback:
            try:
                progress_callback(message, is_error) # Let InterruptedError propagate
            except InterruptedError:
                 raise # Re-raise immediately
            except Exception as cb_err:
                # Log other callback errors but don't stop the process for them
                logging.error(f"{log_prefix} Error executing progress callback (non-cancellation): {cb_err}", exc_info=True)

    if not validate_file_path(file_path, temp_dir):
         # Use try-except to report progress even on early failure
         try: report_progress(f"ERROR: Input file path validation failed: {file_path}", True)
         except InterruptedError: pass # Ignore cancellation during error reporting
         except Exception: pass
         return []
    if not os.path.exists(temp_dir):
         try: report_progress(f"ERROR: Temporary directory does not exist: {temp_dir}", True)
         except InterruptedError: pass
         except Exception: pass
         return []

    try:
        report_progress("Inspecting audio file for splitting...")
        duration_seconds, _duration_minutes = get_audio_duration(file_path)
        if duration_seconds <= 0:
            raise ValueError("Could not determine audio duration.")
        total_length_ms = int(math.ceil(duration_seconds * 1000))
        logging.debug(f"{log_prefix} Duration from ffprobe: {duration_seconds:.2f}s")
    except InterruptedError:
        logging.info(f"{log_prefix} Cancellation detected during audio inspection.")
        return []
    except Exception as e:
        msg = f"ERROR: Failed inspecting audio file: {e}"
        try: report_progress(msg, True)
        except InterruptedError: pass
        except Exception: pass
        logging.exception(f"{log_prefix} Unexpected error inspecting audio:")
        return []

    chunk_files = []
    base_name_no_ext = os.path.splitext(base_name_orig)[0]

    effective_chunk_length_ms = chunk_length_ms
    try:
        bytes_per_second = _parse_audio_bitrate_bytes_per_second(CHUNK_AUDIO_BITRATE)
        max_duration_seconds_for_size = target_chunk_size_bytes / bytes_per_second
        max_duration_ms_for_size = max(1000, int(max_duration_seconds_for_size * 1000))
        effective_chunk_length_ms = min(chunk_length_ms, max_duration_ms_for_size)

        logging.debug(f"{log_prefix} Calculated max duration for {target_chunk_size_bytes / (1024*1024):.1f}MB MP3 chunk: {max_duration_ms_for_size / 1000:.2f}s.")
        logging.debug(f"{log_prefix} Using effective chunk length: {effective_chunk_length_ms / 1000:.2f}s (min of default {chunk_length_ms / 1000}s and calculated max).")

    except Exception as e:
        msg = f"Warning: Could not calculate dynamic chunk size: {e}. Using default max length {chunk_length_ms / 1000}s."
        try: report_progress(msg, False)
        except InterruptedError: logging.info(f"{log_prefix} Cancellation detected during dynamic chunk size warning."); return []
        except Exception: pass
        logging.warning(f"{log_prefix} {msg}", exc_info=True)
        effective_chunk_length_ms = chunk_length_ms

    num_chunks = math.ceil(total_length_ms / effective_chunk_length_ms) if effective_chunk_length_ms > 0 else 0
    if num_chunks == 0 and total_length_ms > 0:
        logging.warning(f"{log_prefix} Calculated 0 chunks for non-zero audio length. Processing as 1 chunk.")
        num_chunks = 1
        effective_chunk_length_ms = total_length_ms

    try:
        report_progress(f"Splitting into {num_chunks} chunks (max duration {effective_chunk_length_ms / 1000:.1f}s each)...", False)
    except InterruptedError:
        logging.info(f"{log_prefix} Cancellation detected before chunk export loop.")
        return []
    except Exception as e:
         logging.error(f"{log_prefix} Error reporting split progress: {e}", exc_info=True)
         # Continue processing, but log the error

    output_pattern = os.path.join(temp_dir, f"{base_name_no_ext}_chunk_%03d.mp3")
    try:
        # The callback performs the existing cancellation check before FFmpeg starts.
        if progress_callback:
            progress_callback("Checking cancellation before audio splitting...", False)
        logging.debug(
            f"{log_prefix} Segmenting source into approximately {num_chunks} chunks in one FFmpeg pass."
        )
        _segment_audio_ffmpeg(
            file_path,
            output_pattern,
            effective_chunk_length_ms / 1000,
            cancellation_check,
        )
        chunk_files = sorted(glob.glob(os.path.join(temp_dir, f"{base_name_no_ext}_chunk_*.mp3")))
        if not chunk_files:
            raise RuntimeError("FFmpeg completed without creating audio chunks.")
        if len(chunk_files) != num_chunks:
            mismatch_message = (
                f"ERROR: Expected {num_chunks} audio chunks but FFmpeg created "
                f"{len(chunk_files)}."
            )
            report_progress(mismatch_message, True)
            remove_files(chunk_files)
            return []

        for chunk_index, chunk_filename_full in enumerate(chunk_files, start=1):
            actual_size = os.path.getsize(chunk_filename_full)
            logging.debug(
                f"{log_prefix} Actual size of chunk {chunk_index}: "
                f"{actual_size / (1024*1024):.2f}MB"
            )
            if actual_size > OPENAI_MAX_FILE_SIZE_BYTES:
                logging.warning(
                    f"{log_prefix} Chunk {chunk_index} size "
                    f"({actual_size / (1024*1024):.2f}MB) exceeds the API limit."
                )
                report_progress(f"Warning: Chunk {chunk_index} size may exceed API limit.", False)
            report_progress(f"Created {ordinal(chunk_index)} audio chunk of {len(chunk_files)}", False)
    except InterruptedError:
        logging.info(f"{log_prefix} Cancellation detected during audio splitting.")
        remove_files(chunk_files)
        remove_files(glob.glob(os.path.join(temp_dir, f"{base_name_no_ext}_chunk_*.mp3")))
        return []
    except Exception as e:
        msg = f"ERROR: Failed splitting audio: {e}"
        try:
            report_progress(msg, True)
        except InterruptedError:
            pass
        except Exception:
            pass
        logging.exception(f"{log_prefix} Error splitting audio:")
        remove_files(chunk_files)
        remove_files(glob.glob(os.path.join(temp_dir, f"{base_name_no_ext}_chunk_*.mp3")))
        return []

    try:
        report_progress(f"Finished splitting into {len(chunk_files)} chunks.", False)
    except InterruptedError:
        logging.info(f"{log_prefix} Cancellation detected after loop completion (during final report).")
        remove_files(chunk_files)
        return []
    except Exception as e:
         logging.error(f"{log_prefix} Error reporting final split progress: {e}", exc_info=True)

    return chunk_files


def remove_files(file_paths: List[str]) -> int:
    """
    Removes a list of files, logging actions and errors.

    Args:
        file_paths: A list of absolute paths to the files to be removed.

    Returns:
        The count of successfully removed files.
    """
    removed_count = 0
    log_prefix = "[SERVICE:File:Remove]"
    if not file_paths:
        return 0

    logging.debug(f"{log_prefix} Attempting to remove {len(file_paths)} file(s)...")
    for path in file_paths:
        file_basename = os.path.basename(path)
        try:
            if os.path.exists(path) and os.path.isfile(path):
                os.remove(path)
                logging.debug(f"{log_prefix} Removed file: {file_basename}")
                removed_count += 1
            elif not os.path.exists(path):
                logging.debug(f"{log_prefix} File already removed or does not exist: {file_basename}")
            else:
                 logging.warning(f"{log_prefix} Path exists but is not a file, skipping removal: {file_basename}")
        except OSError as e:
            logging.error(f"{log_prefix} OS error removing file '{file_basename}': {e}")
        except Exception as e:
            logging.exception(f"{log_prefix} Unexpected error removing file '{file_basename}':")
    if removed_count > 0:
        logging.info(f"{log_prefix} Finished removal attempt. Successfully removed {removed_count} file(s).")
    else:
        logging.debug(f"{log_prefix} Finished removal attempt. No files were removed.")
    return removed_count

def cleanup_old_files(directory: str, threshold_seconds: int) -> int:
    """
    Periodically cleans up files older than threshold_seconds in the specified directory.
    Intended to be run as a background task. Skips files in IGNORE_FILES.

    Args:
        directory: The absolute path to the directory to clean up.
        threshold_seconds: The age in seconds after which files are deleted.

    Returns:
        The number of files successfully deleted during this run.
    """
    deleted_count = 0
    log_prefix = "[SERVICE:File:CleanupTask]"
    if not os.path.isdir(directory):
        logging.error(f"{log_prefix} Cleanup directory not found or is not a directory: {directory}")
        return 0

    current_time = time.time()
    logging.debug(f"{log_prefix} Starting cleanup scan in directory: {directory} (Threshold: {threshold_seconds}s)")

    try:
        for filename in os.listdir(directory):
            if filename in IGNORE_FILES:
                logging.debug(f"{log_prefix} Skipping ignored file: {filename}")
                continue

            file_path = os.path.join(directory, filename)

            try:
                if os.path.isfile(file_path):
                    file_stat = os.stat(file_path)
                    file_age = current_time - file_stat.st_mtime

                    if file_age > threshold_seconds:
                        logging.info(f"{log_prefix} Deleting old file: {filename} (Age: {file_age:.0f}s)")
                        # Use the robust remove_files function
                        if remove_files([file_path]) > 0:
                            deleted_count += 1
                    # else:
                    #     logging.debug(f"{log_prefix} Keeping file: {filename} (Age: {file_age:.0f}s)")

            except FileNotFoundError:
                logging.warning(f"{log_prefix} File not found during cleanup scan (likely removed concurrently): {filename}")
            except OSError as e:
                logging.error(f"{log_prefix} OS error processing file '{filename}' during cleanup: {e}")
            except Exception as e:
                logging.exception(f"{log_prefix} Unexpected error processing file '{filename}' during cleanup:")

    except Exception as e:
        logging.exception(f"{log_prefix} Error listing directory '{directory}' during cleanup:")

    if deleted_count > 0:
        logging.info(f"{log_prefix} Cleanup scan finished for directory: {directory}. Deleted {deleted_count} file(s) in this run.")
    else:
        logging.debug(f"{log_prefix} Cleanup scan finished for directory: {directory}. No files were deleted.")
    return deleted_count
