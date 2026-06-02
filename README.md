# Transcriber Platform

**Self-hosted AI transcription platform for teams, SMBs and individuals who need control over their data, users, API keys and transcription costs.**

Transcriber Platform turns audio into organized text through a web UI, a public transcription API and reusable AI workflows. It supports **OpenAI GPT-4o Transcribe**, **OpenAI Whisper**, **AssemblyAI Universal** and LLM providers such as **OpenAI** and **Google Gemini** for titles, summaries and custom post-processing.

Use it as a simple personal transcription app in `single` mode, or run it as a team platform in `multi` mode with authentication, RBAC, per-user API keys, admin analytics, cost tracking and workflow templates.

[![Build Status](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/arnoulddw/transcriber-platform)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

![Screenshot of the Transcriber Platform App](transcriber-platform-screenshot.png)

## Table of Contents

-   [✨ Key Features](#-key-features)
-   [🚀 Quick Start (Docker)](#-quick-start-docker)
-   [🔧 Installation & Configuration](#-installation--configuration)
-   [💻 Usage Guide](#-usage-guide)
-   [🛠️ For Developers](#️-for-developers)
-   [🤔 Troubleshooting](#-troubleshooting)
-   [📜 License](#-license)

## ✨ Key Features

### Core Functionality
-   **Multiple Transcription APIs:** Choose from OpenAI GPT-4o Transcribe, OpenAI Whisper or AssemblyAI Universal.
-   **Speaker Diarization (AssemblyAI):** Toggle speaker labels to identify who said what on supported jobs.
-   **Large File Handling:** Automatically splits files over 25MB into chunks for seamless processing.
-   **AI-Powered Title Generation:** Automatically generates a concise title for each transcription.
-   **Custom AI Workflows:** Execute custom prompts (ex. summarize, extract action items) on transcribed text using LLMs like OpenAI models or Google Gemini; save reusable workflows from the UI.
-   **Public Transcription API:** Submit audio programmatically and track progress from external tools.
-   **Flexible Language Options:** Select the audio language manually or use automatic detection.
-   **Context Prompting:** Improve accuracy for jargon or specific names by providing context hints to OpenAI models.

### User Experience
-   **Intuitive Web Interface:** Clean and simple UI for uploading files, managing history and running workflows.
-   **Live Progress & Cancellation:** Track uploads/transcriptions with live updates and cancel long-running jobs without leaving the page.
-   **Comprehensive History:** View, copy, download (.txt) and delete past transcriptions.
-   **Internationalization (i18n):** Multi-language support (English, Spanish, French, Dutch).

### Multi-User & Admin Features
-   **Dual Deployment Modes:**
    -   `single`: Simple, no-login mode using global API keys. Perfect for personal use.
    -   `multi`: Full-featured user mode with registration, login and individual API key management.
-   **Secure User Authentication:** Supports username/password, Google Sign-In and password resets.
-   **Role-Based Access Control (RBAC):** Granularly control permissions for features, API usage and more.
-   **Smart API Key Handling:** If a user has permission to manage keys, their personal key is used. Otherwise, the system seamlessly falls back to the global API key, ensuring uninterrupted service.
-   **Comprehensive Admin Panel:**
    -   **User Management:** View and manage all users and their usage.
    -   **Cost & Usage Analytics:** Detailed dashboards to track transcription minutes, workflow costs and API expenses by user and role.
    -   **System-wide Templates:** Create and manage workflow templates available to all users.
    
    

## 🚀 Quick Start (Docker)

Get the platform running in under 5 minutes. This is the recommended method.

**Prerequisites:** [Docker](https://www.docker.com/get-started) and [Docker Compose](https://docs.docker.com/compose/install/).

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/arnoulddw/transcriber-platform.git
    cd transcriber-platform
    ```

2.  **Configure Your Environment**
    Copy the example environment file and edit it with your details.
    ```bash
    cp .env.example .env
    nano .env 
    ```
    -   **Crucially, you must set:** `SECRET_KEY`, your API keys (`OPENAI_API_KEY`, etc.) and `MYSQL_PASSWORD`, `MYSQL_USER`, `MYSQL_DB`.
    -   For multi-user mode, also set `ADMIN_USERNAME` and `ADMIN_PASSWORD` to create your admin account.

3.  **Build and Run**
    ```bash
    docker-compose up -d --build
    ```

4.  **Access the App**
    Open your browser and go to `http://localhost:5004` (or the `APP_PORT` you set in `.env`). The database will be initialized automatically on the first run.

## 🔧 Installation & Configuration

This section provides more detailed setup instructions.

### Prerequisites

-   **API Keys:** You need API keys for the services you plan to use:
    -   [OpenAI](https://platform.openai.com/) (for Whisper, GPT-4o Transcribe and LLM workflows)
    -   [AssemblyAI (Universal model)](https://www.assemblyai.com/)
    -   [Google Gemini](https://ai.google.dev/) (for title generation and LLM workflows)
-   **Docker & Docker Compose:** Required for the recommended installation method.
-   **Google Client ID (Optional):** Required for Google Sign-In in `multi` user mode.
-   **Python 3.9+:** Required for local development without Docker.

### Environment Variables

The application is configured using environment variables in a `.env` file. The table below lists all available options.

<details>
<summary><strong>Click to expand all environment variables</strong></summary>

| Variable | Description | Default |
|---|---|---|
| **Core Application** | | |
| `SECRET_KEY` | **CRITICAL:** A strong, random key for session security. **Must be set.** | (none) |
| `DEPLOYMENT_MODE` | `single` (no login) or `multi` (user accounts). | `multi` |
| `TZ` | Timezone for the application (ex. `UTC`, `Europe/Paris`). | `UTC` |
| `APP_PORT` | Port on which the app is accessible on the host machine. | `5004` |
| `LOG_LEVEL` | Application logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). | `INFO` |
| **API Keys (Global Fallback)** | | |
| `OPENAI_API_KEY` | Your API key for OpenAI (Whisper, GPT-4o Transcribe, LLMs). | (none) |
| `ASSEMBLYAI_API_KEY` | Your API key for AssemblyAI. | (none) |
| `GEMINI_API_KEY` | Your API key for Google Gemini (Title Generation, LLMs). | (none) |
| **Default Settings** | | |
| `DEFAULT_TRANSCRIPTION_PROVIDER` | Default transcription API on load (`gpt-4o-transcribe`, `whisper`, `assemblyai`). | `gpt-4o-transcribe` |
| `DEFAULT_LLM_PROVIDER` | Default LLM for tasks like title generation (`openai`, `gemini`). | `gemini` |
| `DEFAULT_LANGUAGE` | Default transcription language on load (`auto`, `en`, `es`, etc.). | `auto` |
| `SUPPORTED_LANGUAGE_CODES` | Comma-separated language codes to show in the UI (ex. `en,nl,fr,es`). | `en,nl,fr,es` |
| **Database (MySQL)** | | |
| `MYSQL_HOST` | Hostname for the MySQL server. Use `mysql` for Docker Compose. | `localhost` |
| `MYSQL_PORT` | Port for the MySQL server. | `3306` |
| `MYSQL_USER` | Username for MySQL connection. **Must be set.** | (none) |
| `MYSQL_PASSWORD` | Password for MySQL connection. **Must be set.** | (none) |
| `MYSQL_DB` | Name of the MySQL database. **Must be set.** | (none) |
| `MYSQL_ROOT_PASSWORD` | Root password for the MySQL service (used by Docker Compose). | (none) |
| `MYSQL_HOST_PORT` | Host port to map to MySQL's internal port (for external access). | `3307` |
| `MYSQL_POOL_SIZE` | Number of connections in the MySQL connection pool. | `10` |
| **Multi-User Mode** | | |
| `ADMIN_USERNAME` | Username for the initial admin account (created on first run). | `admin` |
| `ADMIN_PASSWORD` | Password for the initial admin account. **Must be set for admin creation.** | (none) |
| `ADMIN_EMAIL` | Email for the initial admin account. | (none) |
| `GOOGLE_CLIENT_ID` | Your Google OAuth 2.0 Client ID for Google Sign-In. | (none) |
| **Email (for Password Resets)** | | |
| `MAIL_SERVER` | SMTP server for sending emails. | (none) |
| `MAIL_PORT` | SMTP server port. | `587` |
| `MAIL_USE_TLS` | Whether to use TLS for SMTP (`true`, `false`). | `true` |
| `MAIL_USERNAME` | Username for SMTP authentication. | (none) |
| `MAIL_PASSWORD` | Password or App Password for SMTP authentication. | (none) |
| `MAIL_DEFAULT_SENDER` | Default sender email address (ex. `noreply@example.com`). | `noreply@example.com` |
| **Advanced Configuration** | | |
| `TRANSCRIPTION_WORKERS` | Number of parallel workers for chunked transcription. | `4` |
| `WORKFLOW_RATE_LIMIT` | Rate limit for workflow API calls per user (ex. `10 per hour`). | `10 per hour` |
| `PHYSICAL_DELETION_DAYS` | Days after soft-deletion before a transcription is permanently removed. | `120` |

</details>

### Other Installation Options

<details>
<summary><strong>Click to see alternative installation methods (Docker Hub, Local Development)</strong></summary>

#### Option 2: Using a Pre-built Docker Hub Image

1.  **Create a `.env` file** on your host machine with all necessary variables. Ensure `MYSQL_HOST` points to your accessible MySQL server.
2.  **Pull the Docker Image:**
    ```bash
    docker pull arnoulddw/transcriber-platform:latest
    ```
3.  **Run the Docker Container:**
    ```bash
    docker run -d -p 5004:5004 \
      --env-file ./.env \
      --name transcriber-platform-app \
      arnoulddw/transcriber-platform:latest
    ```

#### Option 3: Local Development (Without Docker)

1.  **Clone the repository** and `cd` into it.
2.  **Create and activate a Python virtual environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On macOS/Linux
    ```
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Set up MySQL:** Ensure you have a running MySQL server. Create a database and user.
5.  **Configure `.env`:** Create the file and add your `SECRET_KEY`, API keys and local MySQL connection details (`MYSQL_HOST=localhost`, etc.).
6.  **Initialize the Database:**
    ```bash
    export FLASK_APP=app
    flask init-db
    flask create-roles
    flask create-admin # If in multi-mode
    ```
7.  **Run the App:**
    ```bash
    flask run --host=0.0.0.0 --port=5004
    ```
</details>

## 💻 Usage Guide

1.  **Access the Application:** Open the application in your web browser.
2.  **Authentication (Multi-User Mode):**
    *   Register for an account or log in.
    *   Navigate to "Manage API Keys" to add your personal API keys for OpenAI, AssemblyAI, etc. This is required for most features.
3.  **Upload Audio:** Click the "File" button to select an audio file.
4.  **Configure Transcription:**
    *   Select your preferred API (GPT-4o Transcribe, Whisper, AssemblyAI, etc.).
    *   Choose the audio language or leave it on "Automatic Detection."
    *   (Optional) Provide a context prompt to improve accuracy.
    *   (Optional) Enable speaker diarization when using AssemblyAI to label speakers in the transcript.
5.  **Transcribe:** Click the "Transcribe" button.
6.  **Manage History:** Your completed transcriptions will appear in the history panel. From there you can:
    *   View, copy or download the text.
    *   Delete old transcriptions.
    *   Run an AI workflow (ex. summarize) on the text.

### Public Transcription API

You can trigger transcriptions programmatically using your personal API key (generate it in **Manage API Keys** → **Public API Access**). Requests run with your default transcription model and language and the results land in your normal history.

```bash
curl -X POST https://your-domain.example.com/api/v1/transcribe \
  -H "Authorization: Bearer <YOUR_USER_API_KEY>" \
  -F "audio_file=@/path/to/audio.wav"
```

Use your deployment’s base URL (or `http://localhost:5004` in local dev). The API responds with a `job_id` you can poll via `/api/progress/<job_id>` while signed in. Keep your API key secret; rotate it anytime from the same modal.

## 🛠️ For Developers

Database migrations are handled automatically by the application on startup; no manual migration commands are required.

## 🤔 Troubleshooting

-   **Port in use:** Change `APP_PORT` in `.env` and restart. If using Docker Compose, you can also change the host port in `docker-compose.yml` (ex. `"5005:5004"`).
-   **MySQL Connection Issues (Docker):** Ensure the `mysql` service is running (`docker-compose ps`). Check logs with `docker-compose logs mysql`. Verify `MYSQL_HOST` is set to `mysql` in your `.env` file.
-   **API Key Issues:** In `single` mode, double-check the global API keys in `.env`. In `multi` mode, ensure the logged-in user has added their keys correctly in the UI.
-   **Google Sign-In Errors:** Verify your `GOOGLE_CLIENT_ID` is correct and that your Google Cloud Project has the correct "Authorized JavaScript origins" (ex. `http://localhost:5004`) and "Redirect URIs".

## 📜 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
