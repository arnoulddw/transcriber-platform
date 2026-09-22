# Database schema ownership

Model `init_db_command()` functions own the empty-database baseline: they use
`CREATE TABLE IF NOT EXISTS` so a new installation can start with the current
schema. Ordered files in this directory own changes to databases that already
have a baseline. Startup checks for the `roles` table as the baseline sentinel;
when it is present, startup skips every model initializer (including its
legacy `ALTER TABLE` repair code) and runs only the ordered migrations.

As a first step toward that boundary, removal of the legacy
`users.api_keys_encrypted` column is owned by
`V20251122_0005__migrate_user_api_keys.py`. The user schema initializer no
longer drops that column on every bootstrap. It remains compatible with a
legacy database long enough for the ordered migration to move the keys and
remove the column, while fresh tables never create it.

Future schema changes should follow the same pattern: add the current shape to
the baseline `CREATE TABLE`, then add one ordered migration for an existing
database rather than adding another unconditional repair to model startup. The
durable background queue follows this pattern: fresh databases get its table
from `app.models.background_job`, while existing databases get it from
`V20260922_0001__create_background_jobs.py`. The same rule applies to the live
session table and `V20260922_1__add_live_transcription_sessions.py`.

The sentinel assumes a supported installation has a complete baseline once
`roles` exists. A database left partially created by a failed first bootstrap
must be repaired or restored by an operator; startup deliberately does not
fall back to model-level repair code for that case.
