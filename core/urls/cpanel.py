"""
URL patterns for control panel.
"""
from django.urls import path
from core.views import brains
from core.views.brain_sync import brain_sync_view
from core.views.connect import connect_view, url_tokens_toggle_view
from core.views.services import (
    embedding_provider_delete_view,
    embedding_provider_save_view,
    embedding_provider_test_view,
    vector_connection_default_view,
    vector_connection_delete_view,
    vector_connection_save_view,
    vector_connection_test_view,
)
from core.views.vectors import (
    vector_backup_toggle_view,
    vector_create_view,
    vector_delete_view,
    vector_detail_view,
    vector_list_view,
    vector_retry_view,
    vector_search_view,
)
from core.views.dashboard import dashboard_view, system_resources_api
from core.views.scripts import (
    script_list_view,
    script_create_view,
    script_detail_view,
    script_edit_view,
    script_run_view,
    run_rerun_view,
    script_toggle_view,
    script_archive_view,
    script_restore_view,
    script_fork_view,
    script_delete_view,
    schedule_toggle_view,
    schedule_history_view,
    cron_preview_view,
    webhook_enable_view,
    webhook_disable_view,
    webhook_regenerate_view,
    webhook_settings_view,
    scan_env_refs_view,
)
from core.views.runs import (
    run_cancel_view,
    run_detail_view,
    run_event_download_view,
    run_list_view,
    run_result_download_view,
    run_status_view,
)
from core.views.changelog import changelog_view
from core.views.environments import (
    environment_list_view,
    environment_detail_view,
    environment_create_view,
    environment_edit_view,
    environment_delete_view,
    environment_set_default_view,
    environment_recreate_view,
    environment_packages_view,
    package_install_view,
    package_uninstall_view,
    bulk_install_view,
    export_requirements_view,
    package_operation_status_view,
)
from core.views.settings import (
    settings_view,
    toggle_global_pause_view,
    notification_settings_view,
    test_email_view,
    general_settings_view,
    recaptcha_settings_view,
    retention_settings_view,
    worker_settings_view,
    execution_isolation_settings_view,
    api_writes_settings_view,
    sandbox_test_view,
    restart_workers_view,
    manual_cleanup_view,
    cleanup_preview_view,
    system_info_view,
)
from core.views.connections import (
    connection_create_view,
    connection_delete_view,
    connection_edit_view,
    connection_list_view,
    connection_revoke_view,
    connection_test_view,
    oauth_app_delete_view,
    oauth_app_form_view,
    oauth_app_list_view,
)
from core.views.plugin_connections import (
    plugin_connection_connect_view,
    plugin_connection_release_view,
    plugin_connection_use_view,
)
from core.views.secrets import (
    secret_list_view,
    secret_create_view,
    secret_edit_view,
    secret_delete_view,
    secret_picker_view,
    secret_test_resolve_view,
)
from core.views.tags import (
    tag_list_view,
    tag_create_view,
    tag_edit_view,
    tag_delete_view,
)
from core.views.databases import (
    database_list_view,
    database_create_view,
    database_detail_view,
    database_edit_view,
    database_grants_view,
    database_monitor_view,
    database_retry_view,
    database_reveal_view,
    database_delete_view,
    database_server_test_view,
    database_table_view,
    database_table_csv_view,
)
from core.views.libraries import (
    library_list_view,
    library_create_view,
    library_detail_view,
    library_edit_view,
    library_delete_view,
    library_revision_view,
    library_revision_restore_view,
)
from core.views.datastores import (
    datastore_list_view,
    datastore_create_view,
    datastore_detail_view,
    datastore_edit_view,
    datastore_delete_view,
    datastore_clear_view,
    datastore_entry_create_view,
    datastore_entry_edit_view,
    datastore_entry_delete_view,
    datastore_entry_cell_view,
    datastore_entry_download_view,
    datastore_export_view,
    datastore_import_view,
)
from core.views.tables import (
    table_create_view,
    table_delete_view,
    table_detail_view,
    table_edit_view,
    table_export_view,
    table_import_view,
    table_list_view,
    table_row_add_view,
    table_row_cell_view,
    table_row_delete_view,
)
from core.views.backup import (
    backup_create_view,
    backup_upload_view,
    backup_preview_view,
    backup_restore_view,
    backup_schedule_settings_view,
    backup_schedule_status_view,
    backup_run_now_view,
)
from core.views.users import (
    user_list_view,
    invite_user_view,
    revoke_invite_view,
    delete_user_view,
    user_reset_link_view,
    user_security_view,
    user_require_2fa_view,
)
from core.views.logs import (
    logs_view,
    logs_api_view,
    logs_clear_view,
)
from core.views.api_tokens import (
    api_token_list_view,
    api_token_create_view,
    api_token_created_view,
    api_token_revoke_view,
    api_token_toggle_view,
    api_token_activity_view,
    public_page_revoke_view,
)
from core.views.tasks import (
    tasks_view,
    tasks_api_view,
    task_cancel_view,
    task_force_stop_view,
    task_detail_view,
)
from core.views.pyai import (
    pyai_view,
    pyai_send_view,
    pyai_clear_view,
    pyai_settings_view,
)
from core.views.channels import (
    channel_list_view,
    channel_create_view,
    channel_edit_view,
    channel_delete_view,
    channel_test_view,
    channel_discover_chat_ids_view,
    channel_inbound_view,
    channel_member_action_view,
)
from core.views.services import (
    services_view,
    assets_storage_view,
    storage_connection_save_view,
    storage_connection_delete_view,
    storage_connection_default_view,
    s3_test_connection_view,
    claude_settings_view,
    claude_test_connection_view,
    claude_usage_view,
    ai_provider_save_view,
    ai_provider_delete_view,
    ai_provider_activate_view,
    secret_provider_save_view,
    secret_provider_delete_view,
    secret_provider_test_view,
)
from core.views.plugin_store import (
    plugin_install_url_view,
    plugin_store_detail_view,
    plugin_store_install_view,
    plugin_store_refresh_view,
    plugin_store_settings_view,
    plugin_store_view,
)
from core.views.plugins import (
    plugin_list_view,
    plugin_detail_view,
    plugin_icon_view,
    plugin_upload_view,
    plugin_activate_view,
    plugin_deactivate_view,
    plugin_delete_view,
    plugin_rollback_view,
    plugin_trust_policy_view,
    plugin_restart_view,
    plugin_restart_status_view,
    plugin_restarting_view,
)
from core.views.workspaces import (
    workspace_list_view,
    workspace_create_view,
    workspace_rename_view,
    workspace_delete_view,
    workspace_sandbox_policy_view,
    workspace_members_view,
    workspace_member_add_view,
    workspace_member_role_view,
    workspace_member_remove_view,
)
from core.views.maintenance import (
    maintenance_list_view,
    maintenance_edit_view,
    maintenance_toggle_view,
    maintenance_delete_view,
)
from core.views import watchers

app_name = "cpanel"

urlpatterns = [
    path("connect/", connect_view, name="connect"),
    path("brains/", brains.brain_list_view, name="brain_list"),
    path("brains/create/", brains.brain_create_view, name="brain_create"),
    path("brains/<uuid:brain_id>/", brains.brain_detail_view, name="brain_detail"),
    path("brains/<uuid:brain_id>/edit/", brains.brain_edit_view, name="brain_edit"),
    path("brains/<uuid:brain_id>/revisions/", brains.brain_revisions_view, name="brain_revisions"),
    path("brains/<uuid:brain_id>/revisions/<uuid:revision_id>/", brains.brain_diff_view, name="brain_diff"),
    path("brains/<uuid:brain_id>/settings/", brains.brain_settings_view, name="brain_settings"),
    path("brains/<uuid:brain_id>/delete/", brains.brain_delete_view, name="brain_delete"),
    path("brains/<uuid:brain_id>/sync/", brain_sync_view, name="brain_sync"),
    path("brains/<uuid:brain_id>/grants/", brains.brain_grants_view, name="brain_grants"),
    path("brains/<uuid:brain_id>/grants/<uuid:grant_id>/delete/", brains.brain_grant_delete_view, name="brain_grant_delete"),
    path("brains/<uuid:brain_id>/export/", brains.brain_export_view, name="brain_export"),
    path("brains/<uuid:brain_id>/inbox/", brains.brain_inbox_view, name="brain_inbox"),
    path("brains/<uuid:brain_id>/reader/", brains.brain_reader_view, name="brain_reader"),
    path("brains/<uuid:brain_id>/reader/<uuid:job_id>/", brains.brain_reader_status_view, name="brain_reader_status"),
    path("brains/<uuid:brain_id>/inbox/add/", brains.brain_feed_create_view, name="brain_feed_create"),
    path("brains/<uuid:brain_id>/inbox/<uuid:feed_id>/", brains.brain_feed_view, name="brain_feed"),
    path("connect/url-tokens/", url_tokens_toggle_view, name="mcp_url_tokens_toggle"),
    # Dashboard
    path("", dashboard_view, name="dashboard"),
    path("api/system-resources/", system_resources_api, name="system_resources_api"),

    # Changelog / What's new
    path("changelog/", changelog_view, name="changelog"),

    # Scripts
    path("scripts/", script_list_view, name="script_list"),
    path("scripts/create/", script_create_view, name="script_create"),
    path("api/scan-env-refs/", scan_env_refs_view, name="scan_env_refs"),
    path("scripts/<uuid:pk>/", script_detail_view, name="script_detail"),
    path("scripts/<uuid:pk>/edit/", script_edit_view, name="script_edit"),
    path("scripts/<uuid:pk>/run/", script_run_view, name="script_run"),
    path("scripts/<uuid:pk>/toggle/", script_toggle_view, name="script_toggle"),
    path("scripts/<uuid:pk>/schedule/toggle/", schedule_toggle_view, name="schedule_toggle"),
    path("scripts/<uuid:pk>/schedule/history/", schedule_history_view, name="schedule_history"),
    path("scripts/cron/preview/", cron_preview_view, name="cron_preview"),
    # Script archive/restore/delete
    path("scripts/<uuid:pk>/archive/", script_archive_view, name="script_archive"),
    path("scripts/<uuid:pk>/restore/", script_restore_view, name="script_restore"),
    path("scripts/<uuid:pk>/fork/", script_fork_view, name="script_fork"),
    path("scripts/<uuid:pk>/delete/", script_delete_view, name="script_delete"),
    # Webhooks
    path("scripts/<uuid:pk>/webhook/enable/", webhook_enable_view, name="webhook_enable"),
    path("scripts/<uuid:pk>/webhook/disable/", webhook_disable_view, name="webhook_disable"),
    path("scripts/<uuid:pk>/webhook/regenerate/", webhook_regenerate_view, name="webhook_regenerate"),
    path("scripts/<uuid:pk>/webhook/settings/", webhook_settings_view, name="webhook_settings"),
    # Triggers: a script's triggers and the workspace's watchers
    path("scripts/<uuid:pk>/triggers/", watchers.script_triggers_view, name="script_triggers"),
    path("triggers/", watchers.watcher_list_view, name="watcher_list"),
    path("triggers/new/", watchers.watcher_edit_view, name="watcher_create"),
    path("triggers/<uuid:watcher_id>/", watchers.watcher_detail_view, name="watcher_detail"),
    path("triggers/<uuid:watcher_id>/edit/", watchers.watcher_edit_view, name="watcher_edit"),
    path("triggers/<uuid:watcher_id>/check/", watchers.watcher_check_view, name="watcher_check"),
    path("triggers/<uuid:watcher_id>/toggle/", watchers.watcher_toggle_view, name="watcher_toggle"),
    path("triggers/<uuid:watcher_id>/delete/", watchers.watcher_delete_view, name="watcher_delete"),

    # Runs
    path("runs/", run_list_view, name="run_list"),
    path("runs/<uuid:pk>/", run_detail_view, name="run_detail"),
    path("runs/<uuid:pk>/rerun/", run_rerun_view, name="run_rerun"),
    path("runs/<uuid:pk>/cancel/", run_cancel_view, name="run_cancel"),
    path("runs/<uuid:pk>/status/", run_status_view, name="run_status"),
    path("runs/<uuid:pk>/result.json", run_result_download_view, name="run_result_download"),
    path("runs/<uuid:pk>/event.json", run_event_download_view, name="run_event_download"),

    # Tasks
    path("tasks/", tasks_view, name="tasks"),
    path("api/tasks/", tasks_api_view, name="tasks_api"),
    path("tasks/<str:task_id>/cancel/", task_cancel_view, name="task_cancel"),
    path("tasks/<str:task_id>/force-stop/", task_force_stop_view, name="task_force_stop"),
    # NOTE: keep this AFTER cancel/force-stop so those specific routes win.
    path("tasks/<str:task_id>/", task_detail_view, name="task_detail"),

    # Environments
    path("environments/", environment_list_view, name="environment_list"),
    path("environments/create/", environment_create_view, name="environment_create"),
    path("environments/<uuid:pk>/", environment_detail_view, name="environment_detail"),
    path("environments/<uuid:pk>/edit/", environment_edit_view, name="environment_edit"),
    path("environments/<uuid:pk>/delete/", environment_delete_view, name="environment_delete"),
    path("environments/<uuid:pk>/set-default/", environment_set_default_view, name="environment_set_default"),
    path("environments/<uuid:pk>/recreate/", environment_recreate_view, name="environment_recreate"),
    # Package Management
    path("environments/<uuid:pk>/packages/", environment_packages_view, name="environment_packages"),
    path("environments/<uuid:pk>/packages/install/", package_install_view, name="package_install"),
    path("environments/<uuid:pk>/packages/uninstall/", package_uninstall_view, name="package_uninstall"),
    path("environments/<uuid:pk>/packages/bulk-install/", bulk_install_view, name="bulk_install"),
    path("environments/<uuid:pk>/packages/export/", export_requirements_view, name="export_requirements"),
    # AJAX endpoint
    path("api/package-operation/<uuid:operation_id>/status/", package_operation_status_view, name="package_operation_status"),

    # Secrets
    path("secrets/", secret_list_view, name="secret_list"),
    path("secrets/create/", secret_create_view, name="secret_create"),
    path("api/secret-picker/", secret_picker_view, name="secret_picker"),
    path("api/secret-test-resolve/", secret_test_resolve_view, name="secret_test_resolve"),
    path("secrets/<uuid:pk>/edit/", secret_edit_view, name="secret_edit"),
    path("secrets/<uuid:pk>/delete/", secret_delete_view, name="secret_delete"),
    path("connections/", connection_list_view, name="connection_list"),
    path("connections/new/", connection_create_view, name="connection_create"),
    path("connections/<uuid:pk>/", connection_edit_view, name="connection_edit"),
    path("connections/<uuid:pk>/test/", connection_test_view, name="connection_test"),
    path("connections/<uuid:pk>/revoke/", connection_revoke_view, name="connection_revoke"),
    path("connections/<uuid:pk>/delete/", connection_delete_view, name="connection_delete"),
    path("connections/apps/", oauth_app_list_view, name="oauth_app_list"),
    path("connections/apps/new/", oauth_app_form_view, name="oauth_app_create"),
    path("connections/apps/<uuid:pk>/", oauth_app_form_view, name="oauth_app_edit"),
    path("connections/apps/<uuid:pk>/delete/", oauth_app_delete_view, name="oauth_app_delete"),
    # A plugin's requires_connections (T25): its page's Connect / Use / Stop using.
    path("connections/plugin/<slug:slug>/<slug:key>/connect/", plugin_connection_connect_view,
         name="plugin_connection_connect"),
    path("connections/plugin/<slug:slug>/<slug:key>/use/", plugin_connection_use_view,
         name="plugin_connection_use"),
    path("connections/plugin/<slug:slug>/<slug:key>/release/", plugin_connection_release_view,
         name="plugin_connection_release"),

    # Channels
    path("channels/", channel_list_view, name="channel_list"),
    path("channels/create/", channel_create_view, name="channel_create"),
    path("channels/<uuid:pk>/edit/", channel_edit_view, name="channel_edit"),
    path("channels/<uuid:pk>/delete/", channel_delete_view, name="channel_delete"),
    path("channels/<uuid:pk>/test/", channel_test_view, name="channel_test"),
    path("channels/<uuid:pk>/discover/", channel_discover_chat_ids_view, name="channel_discover"),
    path("channels/<uuid:pk>/inbound/", channel_inbound_view, name="channel_inbound"),
    path("channels/<uuid:pk>/members/<uuid:member_pk>/<str:action>/", channel_member_action_view, name="channel_member_action"),

    # Py AI
    path("pyai/", pyai_view, name="pyai"),
    path("pyai/send/", pyai_send_view, name="pyai_send"),
    path("pyai/clear/", pyai_clear_view, name="pyai_clear"),
    path("pyai/settings/", pyai_settings_view, name="pyai_settings"),

    # Tags
    path("tags/", tag_list_view, name="tag_list"),
    path("tags/create/", tag_create_view, name="tag_create"),
    path("tags/<uuid:pk>/edit/", tag_edit_view, name="tag_edit"),
    path("tags/<uuid:pk>/delete/", tag_delete_view, name="tag_delete"),

    # Databases (managed Postgres for scripts & plugins; Owner/Admin only)
    path("databases/", database_list_view, name="database_list"),
    path("databases/create/", database_create_view, name="database_create"),
    path("databases/server-test/", database_server_test_view, name="database_server_test"),
    path("databases/monitor/", database_monitor_view, name="database_monitor"),
    path("databases/<uuid:pk>/", database_detail_view, name="database_detail"),
    path("databases/<uuid:pk>/tables/<str:table>/", database_table_view, name="database_table"),
    path("databases/<uuid:pk>/tables/<str:table>/csv/", database_table_csv_view, name="database_table_csv"),
    path("databases/<uuid:pk>/edit/", database_edit_view, name="database_edit"),
    path("databases/<uuid:pk>/grants/", database_grants_view, name="database_grants"),
    path("databases/<uuid:pk>/retry/", database_retry_view, name="database_retry"),
    path("databases/<uuid:pk>/reveal/", database_reveal_view, name="database_reveal"),
    path("databases/<uuid:pk>/delete/", database_delete_view, name="database_delete"),

    # Vectors (vector collections on the Local / Qdrant stores; Owner/Admin only)
    path("vectors/", vector_list_view, name="vector_list"),
    path("vectors/create/", vector_create_view, name="vector_create"),
    path("vectors/<uuid:pk>/", vector_detail_view, name="vector_detail"),
    path("vectors/<uuid:pk>/search/", vector_search_view, name="vector_search"),
    path("vectors/<uuid:pk>/backup/", vector_backup_toggle_view, name="vector_backup_toggle"),
    path("vectors/<uuid:pk>/retry/", vector_retry_view, name="vector_retry"),
    path("vectors/<uuid:pk>/delete/", vector_delete_view, name="vector_delete"),

    # Libraries (shared multi-module code attached to scripts)
    path("libraries/", library_list_view, name="library_list"),
    path("libraries/create/", library_create_view, name="library_create"),
    path("libraries/<uuid:pk>/", library_detail_view, name="library_detail"),
    path("libraries/<uuid:pk>/edit/", library_edit_view, name="library_edit"),
    path("libraries/<uuid:pk>/delete/", library_delete_view, name="library_delete"),
    path("libraries/<uuid:pk>/v<int:version>/", library_revision_view, name="library_revision"),
    path("libraries/<uuid:pk>/v<int:version>/restore/", library_revision_restore_view, name="library_revision_restore"),

    # Data Stores
    path("datastores/", datastore_list_view, name="datastore_list"),
    path("datastores/create/", datastore_create_view, name="datastore_create"),
    path("datastores/<uuid:pk>/", datastore_detail_view, name="datastore_detail"),
    path("datastores/<uuid:pk>/edit/", datastore_edit_view, name="datastore_edit"),
    path("datastores/<uuid:pk>/delete/", datastore_delete_view, name="datastore_delete"),
    path("datastores/<uuid:pk>/clear/", datastore_clear_view, name="datastore_clear"),
    path("datastores/<uuid:pk>/entries/create/", datastore_entry_create_view, name="datastore_entry_create"),
    path("datastores/<uuid:pk>/entries/<uuid:entry_pk>/edit/", datastore_entry_edit_view, name="datastore_entry_edit"),
    path("datastores/<uuid:pk>/entries/<uuid:entry_pk>/delete/", datastore_entry_delete_view, name="datastore_entry_delete"),
    path("datastores/<uuid:pk>/entries/<uuid:entry_pk>/cell/", datastore_entry_cell_view, name="datastore_entry_cell"),
    path("datastores/<uuid:pk>/entries/<uuid:entry_pk>/download/", datastore_entry_download_view,
         name="datastore_entry_download"),
    path("datastores/<uuid:pk>/export/", datastore_export_view, name="datastore_export"),
    path("datastores/<uuid:pk>/import/", datastore_import_view, name="datastore_import"),
    # Tables (2.5): typed columns, JSON rows, the shared grid
    path("tables/", table_list_view, name="table_list"),
    path("tables/create/", table_create_view, name="table_create"),
    path("tables/<uuid:pk>/", table_detail_view, name="table_detail"),
    path("tables/<uuid:pk>/edit/", table_edit_view, name="table_edit"),
    path("tables/<uuid:pk>/delete/", table_delete_view, name="table_delete"),
    path("tables/<uuid:pk>/export.csv", table_export_view, name="table_export"),
    path("tables/<uuid:pk>/import/", table_import_view, name="table_import"),
    path("tables/<uuid:pk>/rows/add/", table_row_add_view, name="table_row_add"),
    path("tables/<uuid:pk>/rows/<int:row_id>/cell/", table_row_cell_view, name="table_row_cell"),
    path("tables/<uuid:pk>/rows/<int:row_id>/delete/", table_row_delete_view, name="table_row_delete"),

    # Settings
    path("settings/", settings_view, name="settings"),
    path("settings/toggle-pause/", toggle_global_pause_view, name="toggle_global_pause"),
    path("settings/notifications/", notification_settings_view, name="notification_settings"),
    path("settings/test-email/", test_email_view, name="test_email"),
    path("settings/general/", general_settings_view, name="general_settings"),
    path("settings/recaptcha/", recaptcha_settings_view, name="recaptcha_settings"),
    path("settings/retention/", retention_settings_view, name="retention_settings"),
    path("settings/workers/", worker_settings_view, name="worker_settings"),
    path("settings/isolation/", execution_isolation_settings_view, name="execution_isolation_settings"),
    path("settings/isolation/test/", sandbox_test_view, name="sandbox_test"),
    path("settings/api-writes/", api_writes_settings_view, name="api_writes_settings"),
    path("settings/restart-workers/", restart_workers_view, name="restart_workers"),
    path("settings/cleanup/", manual_cleanup_view, name="manual_cleanup"),
    path("settings/cleanup-preview/", cleanup_preview_view, name="cleanup_preview"),
    path("settings/system-info/", system_info_view, name="system_info"),

    # Backup & Restore
    path("settings/backup/create/", backup_create_view, name="backup_create"),
    path("settings/backup/upload/", backup_upload_view, name="backup_upload"),
    path("settings/backup/preview/", backup_preview_view, name="backup_preview"),
    path("settings/backup/restore/", backup_restore_view, name="backup_restore"),
    path("settings/backup/schedule/", backup_schedule_settings_view, name="backup_schedule_settings"),
    path("settings/backup/schedule/status/", backup_schedule_status_view, name="backup_schedule_status"),
    path("settings/backup/run-now/", backup_run_now_view, name="backup_run_now"),

    # Maintenance windows: global (superuser) here, per workspace below.
    path("settings/maintenance/", maintenance_list_view, name="maintenance_list"),
    path("settings/maintenance/new/", maintenance_edit_view, name="maintenance_create"),
    path("settings/maintenance/<uuid:window_id>/", maintenance_edit_view, name="maintenance_edit"),
    path("settings/maintenance/<uuid:window_id>/toggle/", maintenance_toggle_view, name="maintenance_toggle"),
    path("settings/maintenance/<uuid:window_id>/delete/", maintenance_delete_view, name="maintenance_delete"),

    # Workspaces (tenancy management — role-based; see core/views/workspaces.py)
    path("workspaces/", workspace_list_view, name="workspace_list"),
    path("workspaces/create/", workspace_create_view, name="workspace_create"),
    path("workspaces/<uuid:pk>/rename/", workspace_rename_view, name="workspace_rename"),
    path("workspaces/<uuid:pk>/delete/", workspace_delete_view, name="workspace_delete"),
    path("workspaces/<uuid:pk>/sandbox-policy/", workspace_sandbox_policy_view, name="workspace_sandbox_policy"),
    path("workspaces/<uuid:pk>/members/", workspace_members_view, name="workspace_members"),
    path("workspaces/<uuid:pk>/members/add/", workspace_member_add_view, name="workspace_member_add"),
    path("workspaces/<uuid:pk>/members/<uuid:membership_id>/role/", workspace_member_role_view, name="workspace_member_role"),
    path("workspaces/<uuid:pk>/members/<uuid:membership_id>/remove/", workspace_member_remove_view, name="workspace_member_remove"),
    path("workspaces/<uuid:pk>/maintenance/", maintenance_list_view, name="workspace_maintenance_list"),
    path("workspaces/<uuid:pk>/maintenance/new/", maintenance_edit_view, name="workspace_maintenance_create"),
    path("workspaces/<uuid:pk>/maintenance/<uuid:window_id>/", maintenance_edit_view, name="workspace_maintenance_edit"),
    path("workspaces/<uuid:pk>/maintenance/<uuid:window_id>/toggle/", maintenance_toggle_view, name="workspace_maintenance_toggle"),
    path("workspaces/<uuid:pk>/maintenance/<uuid:window_id>/delete/", maintenance_delete_view, name="workspace_maintenance_delete"),

    # User Management
    path("users/", user_list_view, name="user_list"),
    path("users/invite/", invite_user_view, name="invite_user"),
    path("users/invite/<int:pk>/revoke/", revoke_invite_view, name="revoke_invite"),
    path("users/<int:pk>/delete/", delete_user_view, name="delete_user"),
    path("users/<int:pk>/reset-link/", user_reset_link_view, name="user_reset_link"),
    path("users/security/", user_security_view, name="user_security"),
    path("users/security/require-2fa/", user_require_2fa_view, name="user_require_2fa"),

    # Logs
    path("logs/", logs_view, name="logs"),
    path("api/logs/", logs_api_view, name="logs_api"),
    path("api/logs/clear/", logs_clear_view, name="logs_clear"),

    # API Tokens
    path("settings/api-tokens/", api_token_list_view, name="api_token_list"),
    path("settings/api-tokens/create/", api_token_create_view, name="api_token_create"),
    path("settings/api-tokens/<uuid:pk>/created/", api_token_created_view, name="api_token_created"),
    path("settings/api-tokens/<uuid:pk>/revoke/", api_token_revoke_view, name="api_token_revoke"),
    path("settings/api-tokens/<uuid:pk>/toggle/", api_token_toggle_view, name="api_token_toggle"),
    path("settings/api-tokens/<uuid:pk>/activity/", api_token_activity_view, name="api_token_activity"),
    path("settings/api-tokens/public-pages/<uuid:pk>/revoke/", public_page_revoke_view, name="public_page_revoke"),

    # Services
    path("services/", services_view, name="services"),
    path("services/s3/test/", s3_test_connection_view, name="s3_test_connection"),
    path("services/storage/save/", storage_connection_save_view, name="storage_connection_save"),
    path("services/storage/<uuid:connection_id>/delete/", storage_connection_delete_view, name="storage_connection_delete"),
    path("services/storage/<uuid:connection_id>/default/", storage_connection_default_view, name="storage_connection_default"),
    path("services/storage/assets/", assets_storage_view, name="assets_storage"),
    path("services/claude/", claude_settings_view, name="claude_settings"),
    path("services/claude/test/", claude_test_connection_view, name="claude_test_connection"),
    path("services/claude/usage/", claude_usage_view, name="claude_usage"),
    path("services/ai/providers/save/", ai_provider_save_view, name="ai_provider_save"),
    path("services/ai/providers/<uuid:provider_id>/delete/", ai_provider_delete_view, name="ai_provider_delete"),
    path("services/ai/providers/<uuid:provider_id>/activate/", ai_provider_activate_view, name="ai_provider_activate"),
    path("services/secret-providers/save/", secret_provider_save_view, name="secret_provider_save"),
    path("services/secret-providers/test/", secret_provider_test_view, name="secret_provider_test"),
    path("services/secret-providers/<uuid:provider_id>/delete/", secret_provider_delete_view, name="secret_provider_delete"),
    path("services/embeddings/save/", embedding_provider_save_view, name="embedding_provider_save"),
    path("services/embeddings/test/", embedding_provider_test_view, name="embedding_provider_test"),
    path("services/embeddings/<uuid:provider_id>/delete/", embedding_provider_delete_view, name="embedding_provider_delete"),
    path("services/vectors/save/", vector_connection_save_view, name="vector_connection_save"),
    path("services/vectors/test/", vector_connection_test_view, name="vector_connection_test"),
    path("services/vectors/<uuid:connection_id>/delete/", vector_connection_delete_view, name="vector_connection_delete"),
    path("services/vectors/<uuid:connection_id>/default/", vector_connection_default_view, name="vector_connection_default"),

    # Plugins (superuser)
    path("plugins/", plugin_list_view, name="plugin_list"),
    path("plugins/upload/", plugin_upload_view, name="plugin_upload"),
    path("plugins/restart/", plugin_restart_view, name="plugin_restart"),
    path("plugins/trust-policy/", plugin_trust_policy_view, name="plugin_trust_policy"),
    # Plugin store (2.5 T44). Hyphenated, so no plugin slug can shadow them.
    path("plugins/plugin-store/", plugin_store_view, name="plugin_store"),
    path("plugins/store-refresh/", plugin_store_refresh_view, name="plugin_store_refresh"),
    path("plugins/store-settings/", plugin_store_settings_view, name="plugin_store_settings"),
    path("plugins/plugin-store/<slug:slug>/", plugin_store_detail_view, name="plugin_store_detail"),
    path("plugins/plugin-store/<slug:slug>/install/", plugin_store_install_view, name="plugin_store_install"),
    path("plugins/install-from-url/", plugin_install_url_view, name="plugin_install_url"),
    path("plugins/restarting/", plugin_restarting_view, name="plugin_restarting"),
    path("plugins/restart/status/", plugin_restart_status_view, name="plugin_restart_status"),
    path("plugins/<uuid:pk>/activate/", plugin_activate_view, name="plugin_activate"),
    path("plugins/<uuid:pk>/deactivate/", plugin_deactivate_view, name="plugin_deactivate"),
    path("plugins/<uuid:pk>/delete/", plugin_delete_view, name="plugin_delete"),
    path("plugins/<uuid:pk>/rollback/", plugin_rollback_view, name="plugin_rollback"),
    # Slug routes LAST so the literal/uuid plugin routes above always win.
    path("plugins/<slug:slug>/icon/", plugin_icon_view, name="plugin_icon"),
    path("plugins/<slug:slug>/", plugin_detail_view, name="plugin_detail"),
]
