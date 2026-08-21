# app/cli.py
# Custom Flask CLI commands for database setup and migrations.

import logging

import click
from flask import current_app
from flask.cli import with_appcontext

from app.initialization import (
    initialize_database_schema,
    create_default_roles,
    create_initial_admin,
    create_initialization_marker,
)
from migrations.runner import run_migrations
from app.models.llm_operation import mark_stale_operations_interrupted
from app.tasks.transcription_queue import recover_abandoned_jobs


@click.command("init-db")
@with_appcontext
def init_db_command_cli():
    """Initialise or patch the database schema."""
    log_prefix = "[CLI:init-db]"
    logging.info(f"{log_prefix} Requested manual schema initialisation.")
    click.echo("Initialising database schema...")
    try:
        initialize_database_schema()
    except Exception as exc:
        click.echo(click.style(f"Error initialising database: {exc}", fg="red"), err=True)
        logging.error(f"{log_prefix} Failed: {exc}", exc_info=True)
        raise SystemExit(1) from exc
    click.echo(click.style("Database schema initialised successfully.", fg="green"))
    logging.info(f"{log_prefix} Completed successfully.")


@click.command("create-roles")
@with_appcontext
def create_roles_command_cli():
    """Ensure default application roles exist."""
    log_prefix = "[CLI:create-roles]"
    logging.info(f"{log_prefix} Requested manual role setup.")
    click.echo("Checking default roles...")
    try:
        create_default_roles()
    except Exception as exc:
        click.echo(click.style(f"Error updating roles: {exc}", fg="red"), err=True)
        logging.error(f"{log_prefix} Failed: {exc}", exc_info=True)
        raise SystemExit(1) from exc
    click.echo(click.style("Role setup complete. See logs for details.", fg="blue"))
    logging.info(f"{log_prefix} Completed successfully.")


@click.command("create-admin")
@with_appcontext
def create_admin_command_cli():
    """Create the initial admin user if missing."""
    log_prefix = "[CLI:create-admin]"
    logging.info(f"{log_prefix} Requested manual admin creation.")
    click.echo("Checking initial admin user...")
    try:
        create_initial_admin()
    except Exception as exc:
        click.echo(click.style(f"Error ensuring admin user: {exc}", fg="red"), err=True)
        logging.error(f"{log_prefix} Failed: {exc}", exc_info=True)
        raise SystemExit(1) from exc
    click.echo(click.style("Admin user check complete. See logs for details.", fg="blue"))
    logging.info(f"{log_prefix} Completed successfully.")


@click.command("db-migrate")
@with_appcontext
def db_migrate_command_cli():
    """Apply pending schema migrations."""
    log_prefix = "[CLI:db-migrate]"
    logging.info(f"{log_prefix} Requested database migration run.")
    click.echo("Running database migrations...")
    try:
        run_migrations()
    except Exception as exc:
        click.echo(click.style(f"Migration error: {exc}", fg="red"), err=True)
        logging.error(f"{log_prefix} Failed: {exc}", exc_info=True)
        raise SystemExit(1) from exc
    click.echo(click.style("Database migrations applied successfully.", fg="green"))
    logging.info(f"{log_prefix} Completed successfully.")


@click.command("bootstrap")
@with_appcontext
def bootstrap_command_cli():
    """Run all bootstrap steps (schema, migrations, marker creation)."""
    log_prefix = "[CLI:bootstrap]"
    logging.info(f"{log_prefix} Requested bootstrap run.")
    click.echo("Running bootstrap tasks (schema + migrations)...")
    try:
        initialize_database_schema()
        run_migrations()
        interrupted_count = recover_abandoned_jobs(current_app._get_current_object())
        if interrupted_count:
            click.echo(f"Marked {interrupted_count} abandoned transcription job(s) as interrupted.")
        # No background thread survives a restart, so every pending/processing
        # LLM operation at bootstrap time is abandoned; sweep them all.
        llm_interrupted_count = mark_stale_operations_interrupted(stale_seconds=None)
        if llm_interrupted_count:
            click.echo(f"Marked {llm_interrupted_count} stuck LLM operation(s) as interrupted.")
        create_initialization_marker(current_app.config)
    except Exception as exc:
        click.echo(click.style(f"Bootstrap error: {exc}", fg="red"), err=True)
        logging.error(f"{log_prefix} Failed: {exc}", exc_info=True)
        raise SystemExit(1) from exc
    click.echo(click.style("Bootstrap completed successfully.", fg="green"))
    logging.info(f"{log_prefix} Completed successfully.")


def register_cli_commands(app):
    """Register custom CLI commands on the Flask application."""
    app.cli.add_command(init_db_command_cli)
    app.cli.add_command(create_roles_command_cli)
    app.cli.add_command(create_admin_command_cli)
    app.cli.add_command(db_migrate_command_cli)
    app.cli.add_command(bootstrap_command_cli)
    logging.info(
        "[SYSTEM] Registered CLI commands: init-db, create-roles, create-admin, db-migrate, bootstrap."
    )
