import json
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'personal_agent.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def create_db_and_tables() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_local_schema()


def ensure_local_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    _migrate_integration_connection_uniqueness(inspector, table_names)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "agent_proposals" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_proposals")}
            if "telegram_outcome_fingerprint" not in columns:
                connection.execute(text("ALTER TABLE agent_proposals ADD COLUMN telegram_outcome_fingerprint VARCHAR(128)"))
        if "act_sessions" in table_names:
            columns = {column["name"] for column in inspector.get_columns("act_sessions")}
            if "agent_id" not in columns:
                connection.execute(text("ALTER TABLE act_sessions ADD COLUMN agent_id VARCHAR(32) NOT NULL DEFAULT 'act'"))
            if "proposal_count" not in columns:
                connection.execute(text("ALTER TABLE act_sessions ADD COLUMN proposal_count INTEGER NOT NULL DEFAULT 0"))
                if "agent_proposals" in table_names:
                    connection.execute(text(
                        "UPDATE act_sessions SET proposal_count = (SELECT COUNT(*) FROM agent_proposals "
                        "WHERE source_session_id = act_sessions.id AND replaces_proposal_id IS NULL)"
                    ))
        if "telegram_bot_connections" in table_names:
            columns = {column["name"] for column in inspector.get_columns("telegram_bot_connections")}
            for column, definition in {
                "topics_enabled": "BOOLEAN",
                "allows_users_to_create_topics": "BOOLEAN",
            }.items():
                if column not in columns:
                    connection.execute(
                        text(f"ALTER TABLE telegram_bot_connections ADD COLUMN {column} {definition}")
                    )
        if "mcp_audit_records" in table_names:
            columns = {column["name"] for column in inspector.get_columns("mcp_audit_records")}
            for column, definition in {"agent_id": "VARCHAR(32)", "agent_session_id": "INTEGER", "agent_turn_id": "INTEGER"}.items():
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE mcp_audit_records ADD COLUMN {column} {definition}"))
        if "approval_requests" in table_names:
            columns = {column["name"] for column in inspector.get_columns("approval_requests")}
            if "schedule_id" not in columns:
                connection.execute(text("ALTER TABLE approval_requests ADD COLUMN schedule_id INTEGER"))
        if "integration_connections" in table_names:
            columns = {column["name"] for column in inspector.get_columns("integration_connections")}
            added_default_column = "is_default" not in columns
            if "is_default" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT 1")
                )
            if "credential_kind" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN credential_kind VARCHAR(32) NOT NULL DEFAULT 'token'")
                )
            if "passphrase_secret_store_id" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN passphrase_secret_store_id VARCHAR(64)")
                )
            if "passphrase_secret_reference" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN passphrase_secret_reference VARCHAR(256)")
                )
            if "workspace_name" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN workspace_name VARCHAR(256)")
                )
            if "configured_resource_id" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN configured_resource_id VARCHAR(256)")
                )
            if "configured_report_resource_id" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE integration_connections "
                        "ADD COLUMN configured_report_resource_id VARCHAR(256)"
                    )
                )
            if "bot_id" not in columns:
                connection.execute(text("ALTER TABLE integration_connections ADD COLUMN bot_id VARCHAR(128)"))
            if added_default_column:
                connection.execute(
                    text(
                        "UPDATE integration_connections SET is_default = CASE WHEN id IN "
                        "(SELECT MIN(id) FROM integration_connections GROUP BY provider) THEN 1 ELSE 0 END"
                    )
                )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_connections_provider_default "
                    "ON integration_connections(provider) WHERE is_default = 1"
                )
            )
        if "invocation_approvals" in table_names:
            columns = {column["name"] for column in inspector.get_columns("invocation_approvals")}
            if "connection_id" not in columns:
                connection.execute(text("ALTER TABLE invocation_approvals ADD COLUMN connection_id INTEGER"))
        if "integration_audit_records" in table_names:
            columns = {column["name"] for column in inspector.get_columns("integration_audit_records")}
            additions = {
                "provider": "VARCHAR(32)",
                "connection_id": "INTEGER",
                "account_id": "VARCHAR(128)",
            }
            for column, definition in additions.items():
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE integration_audit_records ADD COLUMN {column} {definition}"))
        if "microsoft_oauth_client_configs" not in table_names:
            connection.execute(
                text(
                    "CREATE TABLE microsoft_oauth_client_configs ("
                    "id INTEGER PRIMARY KEY, client_id VARCHAR(1024) NOT NULL, "
                    "secret_store_id VARCHAR(64) NOT NULL, secret_reference VARCHAR(256) NOT NULL, "
                    "authority VARCHAR(256) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                )
            )
        if "quercus_courses" in table_names:
            columns = {column["name"] for column in inspector.get_columns("quercus_courses")}
            additions = {
                "last_processing_started_at": "DATETIME",
                "last_processing_completed_at": "DATETIME",
                "last_processing_status": "VARCHAR(32)",
                "last_processing_error_type": "VARCHAR(64)",
                "processed_file_count": "INTEGER NOT NULL DEFAULT 0",
                "failed_processing_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in additions.items():
                if column not in columns:
                    connection.execute(
                        text(f"ALTER TABLE quercus_courses ADD COLUMN {column} {definition}")
                    )
        if "quercus_sync_resources" in table_names:
            columns = {
                column["name"] for column in inspector.get_columns("quercus_sync_resources")
            }
            additions = {
                "processed_relative_path": "VARCHAR(1024)",
                "processed_source_fingerprint": "VARCHAR(64)",
                "processed_by": "VARCHAR(64)",
                "processing_state": "VARCHAR(32) NOT NULL DEFAULT 'not_processed'",
                "processing_error_type": "VARCHAR(64)",
                "processed_at": "DATETIME",
            }
            for column, definition in additions.items():
                if column not in columns:
                    connection.execute(
                        text(f"ALTER TABLE quercus_sync_resources ADD COLUMN {column} {definition}")
                    )
        if "quercus_processing_settings" in table_names:
            columns = {
                column["name"] for column in inspector.get_columns("quercus_processing_settings")
            }
            if "llama_cpp_directory" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE quercus_processing_settings "
                        "ADD COLUMN llama_cpp_directory VARCHAR(2048)"
                    )
                )
        if "skills" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skills")}
            if "runtime" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN runtime VARCHAR(32) NOT NULL DEFAULT 'function'")
                )
            if "input_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN input_schema_json JSON"))
            if "output_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN output_schema_json JSON"))
            if "function_requirements_json" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN function_requirements_json JSON NOT NULL DEFAULT '[]'")
                )
            if "integration_requirements_json" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN integration_requirements_json JSON NOT NULL DEFAULT '[]'")
                )
            if "active_version_id" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN active_version_id INTEGER"))
            connection.execute(text("UPDATE skills SET status = 'installed', enabled = 0 WHERE status = 'disabled'"))
            if "skill_type" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN skill_type"))
            if "interface_type" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN interface_type"))
            if "tool_ui_schema_json" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN tool_ui_schema_json"))
        if "skill_model_catalog" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_model_catalog")}
            if "reasoning_effort" not in columns:
                connection.execute(
                    text("ALTER TABLE skill_model_catalog ADD COLUMN reasoning_effort VARCHAR(32)")
                )
        if "skill_generation_requests" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_generation_requests")}
            if "product_manager_thread_id" not in columns:
                connection.execute(
                    text("ALTER TABLE skill_generation_requests ADD COLUMN product_manager_thread_id VARCHAR(128)")
                )
            if "proposed_skill_type" in columns:
                connection.execute(text("ALTER TABLE skill_generation_requests DROP COLUMN proposed_skill_type"))
        if "skill_schedules" in table_names:
            schedule_columns = {
                column["name"] for column in inspector.get_columns("skill_schedules")
            }
            if "availability_migrated" not in schedule_columns:
                connection.execute(
                    text(
                        "ALTER TABLE skill_schedules ADD COLUMN "
                        "availability_migrated BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "status" in schedule_columns:
                connection.execute(text("DELETE FROM skill_schedules WHERE status = 'deleted'"))
                connection.execute(
                    text(
                        "UPDATE skill_schedules SET status = 'paused' "
                        "WHERE status NOT IN ('active', 'paused')"
                    )
                )
            if "skill_id" in schedule_columns and "skills" in table_names:
                skill_columns = {column["name"] for column in inspector.get_columns("skills")}
                if "runtime" in skill_columns:
                    retired_schedule_ids = (
                        "SELECT skill_schedules.id FROM skill_schedules "
                        "JOIN skills ON skills.id = skill_schedules.skill_id "
                        "WHERE skills.runtime <> 'service'"
                    )
                    if "skill_runs" in table_names:
                        run_columns = {
                            column["name"] for column in inspector.get_columns("skill_runs")
                        }
                        if "source_schedule_id" in run_columns:
                            connection.execute(
                                text(
                                    "UPDATE skill_runs SET source_schedule_id = NULL "
                                    f"WHERE source_schedule_id IN ({retired_schedule_ids})"
                                )
                            )
                    if "approval_requests" in table_names:
                        approval_columns = {
                            column["name"] for column in inspector.get_columns("approval_requests")
                        }
                        if "schedule_id" in approval_columns:
                            connection.execute(
                                text(
                                    "DELETE FROM approval_requests "
                                    f"WHERE schedule_id IN ({retired_schedule_ids})"
                                )
                            )
                        if "request_type" in approval_columns:
                            connection.execute(
                                text("DELETE FROM approval_requests WHERE request_type = 'schedule'")
                            )
                    connection.execute(
                        text(
                            "DELETE FROM skill_schedules WHERE skill_id IN "
                            "(SELECT id FROM skills WHERE runtime <> 'service')"
                        )
                    )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_schedules_skill_id "
                        "ON skill_schedules (skill_id)"
                    )
                )
        if "schedule_runtime_states" in table_names:
            columns = {
                column["name"]
                for column in inspector.get_columns("schedule_runtime_states")
            }
            if "enabled" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE schedule_runtime_states ADD COLUMN "
                        "enabled BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
            if "configuration_json" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE schedule_runtime_states ADD COLUMN configuration_json JSON"
                    )
                )
        if "skill_versions" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_versions")}
            if "status" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'draft'"))
            if "folder_path" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN folder_path VARCHAR(512) NOT NULL DEFAULT ''"))
                connection.execute(text("UPDATE skill_versions SET folder_path = code_snapshot_path WHERE folder_path = ''"))
            if "activated_at" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN activated_at DATETIME"))
            if "created_by" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN created_by VARCHAR(32) NOT NULL DEFAULT 'system'"))
            if "parent_version_id" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN parent_version_id INTEGER"))
            if "permission_fingerprint" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN permission_fingerprint VARCHAR(128) NOT NULL DEFAULT ''"))
            if "test_status" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN test_status VARCHAR(32) NOT NULL DEFAULT 'not_run'"))
            if "validation_status" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN validation_status VARCHAR(32) NOT NULL DEFAULT 'not_run'"))
            if "changelog" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN changelog TEXT"))
        if "skill_runs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_runs")}
            if "codex_invocations_json" not in columns:
                connection.execute(text("ALTER TABLE skill_runs ADD COLUMN codex_invocations_json JSON NOT NULL DEFAULT '[]'"))
            for column in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            ):
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE skill_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"))
            skill_run_columns = {
                "version_id": "INTEGER",
                "invocation_source": "VARCHAR(32) NOT NULL DEFAULT 'internal'",
                "caller_skill_id": "INTEGER",
                "caller_version_id": "INTEGER",
                "parent_run_id": "INTEGER",
                "source_schedule_id": "INTEGER",
                "schedule_occurrence_key": "VARCHAR(64)",
                "scheduled_for_at": "DATETIME",
                "schedule_trigger": "VARCHAR(32)",
                "web_app_instance_id": "VARCHAR(64)",
                "initiating_action": "VARCHAR(128)",
                "function_capability_token_hash": "VARCHAR(128)",
            }
            for column, definition in skill_run_columns.items():
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE skill_runs ADD COLUMN {column} {definition}"))
            for column in (
                "version_id",
                "invocation_source",
                "caller_skill_id",
                "caller_version_id",
                "parent_run_id",
                "source_schedule_id",
                "schedule_occurrence_key",
                "web_app_instance_id",
                "function_capability_token_hash",
            ):
                connection.execute(
                    text(f"CREATE INDEX IF NOT EXISTS ix_skill_runs_{column} ON skill_runs ({column})")
                )
        if "invocation_approvals" in table_names:
            columns = {
                column["name"] for column in inspector.get_columns("invocation_approvals")
            }
            if "presentation_json" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE invocation_approvals "
                        "ADD COLUMN presentation_json JSON NOT NULL DEFAULT '{}'"
                    )
                )
            connection.execute(
                text(
                    "UPDATE invocation_approvals "
                    "SET execution_status = 'outcome_unknown', "
                    "error_type = COALESCE(error_type, 'interrupted_execution'), "
                    "error_message = COALESCE(error_message, "
                    "'Eidolon restarted while this action was executing; the external outcome is unknown.'), "
                    "execution_completed_at = COALESCE(execution_completed_at, CURRENT_TIMESTAMP) "
                    "WHERE execution_status = 'executing'"
                )
            )
        if "web_app_instances" in table_names:
            columns = {column["name"] for column in inspector.get_columns("web_app_instances")}
            if "relay_container_id" not in columns:
                connection.execute(text("ALTER TABLE web_app_instances ADD COLUMN relay_container_id VARCHAR(128)"))
        if "agent_runs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_runs")}
            if "current_task_id" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN current_task_id VARCHAR(128)"))
            if "current_milestone" in columns:
                connection.execute(
                    text(
                        "UPDATE agent_runs SET current_task_id = current_milestone "
                        "WHERE current_task_id IS NULL AND current_milestone IS NOT NULL"
                    )
                )
            if "failure_count_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN failure_count_json JSON NOT NULL DEFAULT '{}'"))
            if "build_workflow" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN build_workflow VARCHAR(32)"))
                connection.execute(text("UPDATE agent_runs SET build_workflow = 'task_dag' WHERE run_type = 'build_skill'"))
            if "blueprint_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN blueprint_json JSON"))
            if "final_summary_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN final_summary_json JSON"))
            for column in (
                "total_input_tokens",
                "total_cached_input_tokens",
                "total_output_tokens",
                "total_reasoning_output_tokens",
                "total_tokens",
            ):
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"))
            if "pause_reason" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN pause_reason TEXT"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'product_manager' WHERE current_step IN ('planner', 'reviewer')"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'product_manager' WHERE current_step IN ('permission_analyst', 'security_reviewer')"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'builder' WHERE current_step = 'repairer'"))
        if "agent_run_steps" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_run_steps")}
            if "action" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN action VARCHAR(96)"))
            if "task_node_id" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN task_node_id VARCHAR(128)"))
            if "approval_request_id" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN approval_request_id INTEGER"))
            if "agent_input_text" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN agent_input_text TEXT"))
            if "agent_output_text" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN agent_output_text TEXT"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_agent_run_steps_action ON agent_run_steps (action)")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_run_steps_approval_request_id "
                    "ON agent_run_steps (approval_request_id)"
                )
            )
            if "milestone_name" in columns:
                connection.execute(
                    text(
                        "UPDATE agent_run_steps SET task_node_id = milestone_name "
                        "WHERE task_node_id IS NULL AND milestone_name IS NOT NULL"
                    )
                )
            if "codex_invocations_json" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN codex_invocations_json JSON NOT NULL DEFAULT '[]'"))
            for column in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            ):
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE agent_run_steps ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'product_manager' WHERE step_name IN ('planner', 'reviewer')"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'product_manager' WHERE step_name IN ('permission_analyst', 'security_reviewer')"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'builder' WHERE step_name = 'repairer'"))
        if "act_turns" in table_names:
            columns = {column["name"] for column in inspector.get_columns("act_turns")}
            additions = {
                "cancel_requested_at": "DATETIME",
                "delivery_provider": "VARCHAR(32)",
                "delivery_connection_id": "INTEGER",
                "delivery_chat_id": "VARCHAR(64)",
                "delivery_message_thread_id": "INTEGER",
                "delivery_status": "VARCHAR(32)",
                "started_at": "DATETIME",
            }
            for column, definition in additions.items():
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE act_turns ADD COLUMN {column} {definition}"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_act_turns_delivery_connection_id ON act_turns (delivery_connection_id)")
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_act_turns_delivery_provider ON act_turns (delivery_provider)")
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_act_turns_delivery_status ON act_turns (delivery_status)")
            )
        if "wecom_observer_user_bindings" in table_names:
            user_columns = {
                column["name"] for column in inspector.get_columns("wecom_observer_user_bindings")
            }
            if "current_session_id" not in user_columns:
                if "active_session_id" in user_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE wecom_observer_user_bindings "
                            "RENAME COLUMN active_session_id TO current_session_id"
                        )
                    )
                else:
                    connection.execute(
                        text(
                            "ALTER TABLE wecom_observer_user_bindings "
                            "ADD COLUMN current_session_id INTEGER"
                        )
                    )
                user_columns.add("current_session_id")
            if "wecom_observer_bindings" in table_names:
                binding_columns = {
                    column["name"] for column in inspector.get_columns("wecom_observer_bindings")
                }
                if {"paired_user_id", "active_session_id"} <= binding_columns:
                    connection.execute(
                        text(
                            "INSERT OR IGNORE INTO wecom_observer_user_bindings "
                            "(connection_id, paired_user_id, current_session_id, created_at, updated_at) "
                            "SELECT connection_id, paired_user_id, active_session_id, updated_at, updated_at "
                            "FROM wecom_observer_bindings WHERE paired_user_id IS NOT NULL"
                        )
                    )
                if "paired_user_id" in binding_columns:
                    connection.execute(
                        text(
                            "UPDATE wecom_observer_bindings SET paired_user_id = NULL "
                            "WHERE paired_user_id IS NOT NULL"
                        )
                    )
                if "active_session_id" in binding_columns:
                    connection.execute(
                        text(
                            "UPDATE wecom_observer_bindings SET active_session_id = NULL "
                            "WHERE active_session_id IS NOT NULL"
                        )
                    )
        if "act_telegram_bindings" in table_names:
            # The old table held one mutable session pointer per bot.  Its
            # session rows remain historical, but the pointer itself cannot
            # be converted into a topic identity safely.
            connection.execute(text("DROP TABLE act_telegram_bindings"))
        _remove_retired_product_manager_routing(connection, table_names)
        _remove_retired_skill_contract_json(connection, table_names)
        _remove_retired_manifest_display_name(connection, table_names)


def _migrate_integration_connection_uniqueness(inspector, table_names: set[str]) -> None:
    """Rebuild the legacy table so multiple non-default provider accounts fit safely."""

    if "integration_connections" not in table_names:
        return
    unique_constraints = inspector.get_unique_constraints("integration_connections")
    unique_indexes = inspector.get_indexes("integration_connections")
    provider_is_unique = any(
        item.get("column_names") == ["provider"] for item in unique_constraints
    ) or any(
        item.get("unique") and item.get("column_names") == ["provider"]
        for item in unique_indexes
    )
    if not provider_is_unique:
        return
    columns = {column["name"] for column in inspector.get_columns("integration_connections")}
    connection_columns = (
        "id",
        "provider",
        "is_default",
        "secret_store_id",
        "secret_reference",
        "credential_kind",
        "passphrase_secret_store_id",
        "passphrase_secret_reference",
        "status",
        "account_login",
        "account_id",
        "bot_id",
        "workspace_name",
        "configured_resource_id",
        "configured_report_resource_id",
        "error_type",
        "created_at",
        "updated_at",
        "last_validated_at",
    )
    defaults = {
        "is_default": "1",
        "credential_kind": "'token'",
        "passphrase_secret_store_id": "NULL",
        "passphrase_secret_reference": "NULL",
        "bot_id": "NULL",
        "workspace_name": "NULL",
        "configured_resource_id": "NULL",
        "configured_report_resource_id": "NULL",
        "error_type": "NULL",
    }
    select_columns = ", ".join(
        column if column in columns else f"{defaults.get(column, 'NULL')} AS {column}"
        for column in connection_columns
    )
    create_sql = (
        "CREATE TABLE integration_connections_new ("
        "id INTEGER PRIMARY KEY, provider VARCHAR(32) NOT NULL, "
        "is_default BOOLEAN NOT NULL DEFAULT 1, secret_store_id VARCHAR(64) NOT NULL, "
        "secret_reference VARCHAR(256) NOT NULL, credential_kind VARCHAR(32) NOT NULL DEFAULT 'token', "
        "passphrase_secret_store_id VARCHAR(64), passphrase_secret_reference VARCHAR(256), "
        "status VARCHAR(32) NOT NULL, account_login VARCHAR(128) NOT NULL, account_id VARCHAR(128) NOT NULL, "
        "bot_id VARCHAR(128), workspace_name VARCHAR(256), configured_resource_id VARCHAR(256), "
        "configured_report_resource_id VARCHAR(256), error_type VARCHAR(64), "
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, last_validated_at DATETIME NOT NULL)"
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            connection.exec_driver_sql("BEGIN")
            connection.exec_driver_sql(create_sql)
            connection.exec_driver_sql(
                f"INSERT INTO integration_connections_new ({', '.join(connection_columns)}) "
                f"SELECT {select_columns} FROM integration_connections"
            )
            connection.exec_driver_sql("UPDATE integration_connections_new SET is_default = 0")
            connection.exec_driver_sql(
                "UPDATE integration_connections_new SET is_default = 1 WHERE id IN "
                "(SELECT MIN(id) FROM integration_connections_new GROUP BY provider)"
            )
            connection.exec_driver_sql("DROP TABLE integration_connections")
            connection.exec_driver_sql("ALTER TABLE integration_connections_new RENAME TO integration_connections")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_integration_connections_provider ON integration_connections(provider)")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_integration_connections_is_default ON integration_connections(is_default)")
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_connections_provider_default "
                "ON integration_connections(provider) WHERE is_default = 1"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def _remove_retired_product_manager_routing(connection, table_names: set[str]) -> None:
    """Drop routing keys that no longer have a live ProductManager action."""

    if "codex_routing_settings" not in table_names:
        return
    rows = connection.execute(
        text("SELECT id, settings_json FROM codex_routing_settings WHERE settings_json IS NOT NULL")
    ).all()
    for row_id, raw_value in rows:
        try:
            value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        product_manager = value.get("product_manager")
        if not isinstance(product_manager, dict) or "plausibility_review" not in product_manager:
            continue
        cleaned = dict(value)
        cleaned_product_manager = dict(product_manager)
        cleaned_product_manager.pop("plausibility_review", None)
        cleaned["product_manager"] = cleaned_product_manager
        connection.execute(
            text("UPDATE codex_routing_settings SET settings_json = :value WHERE id = :row_id"),
            {"value": json.dumps(cleaned), "row_id": row_id},
        )


def _remove_retired_skill_contract_json(connection, table_names: set[str]) -> None:
    json_columns = {
        "skill_generation_requests": ("plan_json",),
        "skill_versions": ("manifest_json",),
        "agent_runs": ("blueprint_json", "final_summary_json"),
        "agent_run_steps": ("input_json", "output_json"),
        "approval_requests": ("reason_json",),
    }
    inspector = inspect(engine)
    for table_name, candidates in json_columns.items():
        if table_name not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in candidates:
            if column_name not in columns:
                continue
            rows = connection.execute(
                text(f"SELECT id, {column_name} FROM {table_name} WHERE {column_name} IS NOT NULL")
            ).all()
            for row_id, raw_value in rows:
                try:
                    value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
                except (TypeError, json.JSONDecodeError):
                    continue
                cleaned, changed = _without_retired_skill_fields(value)
                if changed:
                    connection.execute(
                        text(f"UPDATE {table_name} SET {column_name} = :value WHERE id = :row_id"),
                        {"value": json.dumps(cleaned), "row_id": row_id},
                    )


def _remove_retired_manifest_display_name(connection, table_names: set[str]) -> None:
    if "skill_versions" not in table_names:
        return
    columns = {column["name"] for column in inspect(engine).get_columns("skill_versions")}
    if "manifest_json" not in columns:
        return
    rows = connection.execute(
        text("SELECT id, manifest_json FROM skill_versions WHERE manifest_json IS NOT NULL")
    ).all()
    for row_id, raw_value in rows:
        try:
            value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or "display_name" not in value:
            continue
        cleaned = dict(value)
        cleaned.pop("display_name")
        connection.execute(
            text("UPDATE skill_versions SET manifest_json = :value WHERE id = :row_id"),
            {"value": json.dumps(cleaned), "row_id": row_id},
        )


def _without_retired_skill_fields(value):
    if isinstance(value, dict):
        retired_fields = {
            "interface_type",
            "proposed_skill_type",
            "skill_type",
            "tool_ui_schema",
            "tool_ui_schema_json",
        }
        changed = any(key in value for key in retired_fields)
        cleaned = {}
        for key, item in value.items():
            if key in retired_fields:
                continue
            cleaned_item, item_changed = _without_retired_skill_fields(item)
            cleaned[key] = cleaned_item
            changed = changed or item_changed
        return cleaned, changed
    if isinstance(value, list):
        cleaned = []
        changed = False
        for item in value:
            cleaned_item, item_changed = _without_retired_skill_fields(item)
            cleaned.append(cleaned_item)
            changed = changed or item_changed
        return cleaned, changed
    return value, False


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
