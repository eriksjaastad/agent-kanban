-- Maintained by scripts/db/schema_dump.py (make schema). Never edit by hand: add a migration.
-- Migrations: through 026
-- Body sha256: 2a8ef2da8cee0f678dbbbe99e15bf6af2d46df40fca238e8eb6c1b5127e2c4b8

CREATE TABLE _delete_permissions (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0
        );

CREATE TABLE _metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

CREATE TABLE "ai_agents" (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            role TEXT,
            notes TEXT
        );

CREATE TABLE "calendar_event_tasks" (
            event_id  INTEGER NOT NULL,
            task_id   INTEGER NOT NULL,
            link_type TEXT    NOT NULL DEFAULT 'related',
            PRIMARY KEY (event_id, task_id)
        );

CREATE TABLE "calendar_events" (
            id                    INTEGER PRIMARY KEY NOT NULL,
            title                 TEXT    NOT NULL DEFAULT '',
            description           TEXT,
            event_date            TEXT    NOT NULL DEFAULT '',
            event_time            TEXT,
            event_type            TEXT    NOT NULL DEFAULT 'reminder',
            recurrence            TEXT,
            project_id            TEXT,
            machine               TEXT,
            prompt                TEXT,
            notify_before_minutes INTEGER NOT NULL DEFAULT 60,
            notified_at           TEXT,
            status                TEXT    NOT NULL DEFAULT 'active',
            created_by            TEXT,
            metadata              TEXT    DEFAULT '{}',
            created_at            TEXT    NOT NULL DEFAULT '',
            updated_at            TEXT    NOT NULL DEFAULT ''
        , end_time TEXT);

CREATE TABLE codebase_size_snapshots (
            id            INTEGER PRIMARY KEY NOT NULL,
            run_id        TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            project       TEXT NOT NULL,
            code_lines    INTEGER NOT NULL,
            test_lines    INTEGER NOT NULL,
            doc_files     INTEGER NOT NULL,
            doc_lines     INTEGER NOT NULL,
            last_commit   TEXT,
            commits_90d   INTEGER,
            scanned_at    TEXT NOT NULL,
            UNIQUE(run_id, project)
        );

CREATE TABLE cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,  -- LOCAL_ONLY: AUTOINCREMENT ok
            project_id TEXT NOT NULL,
            schedule TEXT NOT NULL,
            command TEXT NOT NULL,
            description TEXT,
            last_run TEXT,
            is_active BOOLEAN DEFAULT 1, machine TEXT,
            -- LOCAL_ONLY: FK declaration retained for documentation only.
            -- SQLite FK enforcement is OFF by default and never enabled in this app,
            -- so CASCADE never fires. Application-level cleanup in delete_project()
            -- issues explicit DELETE FROM cron_jobs WHERE project_id = ?.
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

CREATE TABLE delete_attempt_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            reason TEXT NOT NULL
        );

CREATE TABLE delete_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            deleted_id TEXT NOT NULL,
            deleted_data TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            source TEXT DEFAULT 'unknown'
        );

CREATE TABLE handoffs (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id                  INTEGER NOT NULL,
            project                  TEXT,
            branch                   TEXT,
            file_list                TEXT NOT NULL DEFAULT '[]',
            intent                   TEXT NOT NULL,
            current_status           TEXT NOT NULL,
            next_command             TEXT NOT NULL,
            discard_or_preserve_guidance TEXT NOT NULL,
            record_type              TEXT NOT NULL DEFAULT 'unfinished'
                                         CHECK(record_type IN ('unfinished', 'pr_exempt')),
            pr_exempt_reason         TEXT,
            pr_exempt_disposition    TEXT
                                         CHECK(pr_exempt_disposition IN
                                               ('reverted', 'discarded',
                                                'left_as_artifact', 'merged_elsewhere',
                                                NULL)),
            pr_exempt_approver       TEXT,
            created_at               TEXT NOT NULL,
            created_by               TEXT,
            resolved_at              TEXT,
            resolved_note            TEXT
        );

CREATE TABLE "ideas" (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );

CREATE TABLE job_submissions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id            INTEGER NOT NULL REFERENCES jobs(id),
            submitted_at      TEXT NOT NULL,
            resume_path       TEXT,
            cover_letter_path TEXT,
            notes             TEXT
        );

CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company     TEXT NOT NULL,
            title       TEXT NOT NULL,
            location    TEXT,
            url         TEXT NOT NULL UNIQUE,
            source      TEXT NOT NULL DEFAULT 'manual'
                        CHECK (source IN ('ats_sweep', 'hn', 'manual')),
            posted_date TEXT,
            first_seen  TEXT NOT NULL DEFAULT (date('now')),
            category    TEXT NOT NULL DEFAULT 'Other'
                        CHECK (category IN (
                            'Frontend/React', 'Full Stack',
                            'Forward Deployed / Solutions', 'SEO',
                            'Backend', 'Other'
                        )),
            raw         TEXT,
            deleted_at  TEXT,
            kind        TEXT NOT NULL DEFAULT 'job'
                        CHECK (kind IN ('job', 'volunteer')),
            volunteering_since TEXT
        );

CREATE TABLE loop_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loop_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed', 'timeout')),
            error_message TEXT,
            cards_created INTEGER DEFAULT 0,
            metadata TEXT
        );

CREATE TABLE migrations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            project_id      TEXT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT NOT NULL DEFAULT 'recording'
                                CHECK(status IN ('recording', 'finished',
                                                  'committed', 'reverted')),
            baseline_head   TEXT,
            manifest_path   TEXT,
            created_by      TEXT
        );

CREATE TABLE outreach_contacts (
            id           INTEGER PRIMARY KEY NOT NULL,
            name         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            contacted_at TEXT,
            replied_at   TEXT,
            deleted_at   TEXT,
            updated_at   TEXT NOT NULL
        );

CREATE TABLE project_checkouts (
        id             INTEGER PRIMARY KEY NOT NULL,
        project_id     TEXT NOT NULL,
        machine        TEXT NOT NULL,
        holder         TEXT NOT NULL,
        note           TEXT,
        checked_out_at TEXT NOT NULL,
        checked_in_at  TEXT,
        checked_in_by  TEXT
    , task_id INTEGER);

CREATE TABLE "project_info" (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT,
            key TEXT NOT NULL DEFAULT '',
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );

CREATE TABLE "projects" (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            description TEXT,
            phase TEXT,
            last_modified TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            completion_pct INTEGER DEFAULT 0,
            is_infrastructure BOOLEAN DEFAULT 0,
            has_index BOOLEAN DEFAULT 0,
            index_is_valid BOOLEAN DEFAULT 0,
            index_updated_at TEXT,
            project_type TEXT DEFAULT 'standard'
        , health_score INTEGER, health_grade TEXT, scaffolding_version TEXT, rules_version TEXT, scaffolding_applied_at TEXT, tier INTEGER, healthcheck_cmd TEXT, commands_to_run TEXT, cron_jobs_config TEXT, template_version_installed TEXT, template_version_expected TEXT, drift_status TEXT, autonomy_level TEXT DEFAULT 'report', machine TEXT);

CREATE TABLE reminders (
        id          INTEGER PRIMARY KEY NOT NULL,
        name        TEXT NOT NULL,
        purpose     TEXT,
        location    TEXT,
        created_at  TEXT NOT NULL,
        built_in_at TEXT,
        deleted_at  TEXT,
        updated_at  TEXT NOT NULL
    );

CREATE TABLE schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );

CREATE TABLE "service_dependencies" (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            service_name TEXT NOT NULL DEFAULT '',
            purpose TEXT,
            cost_monthly REAL
        );

CREATE TABLE site_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL,
            ran_at TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            sources TEXT
        );

CREATE TABLE task_attachments (
        id           INTEGER PRIMARY KEY NOT NULL,
        task_id      INTEGER NOT NULL DEFAULT 0,
        filename     TEXT NOT NULL DEFAULT '',
        stored_name  TEXT NOT NULL DEFAULT '',
        mime_type    TEXT,
        size_bytes   INTEGER,
        uploaded_at  TEXT DEFAULT (datetime('now')),
        uploaded_by  TEXT DEFAULT 'user'
    );

CREATE TABLE task_display_ids (
            task_id    INTEGER PRIMARY KEY NOT NULL,
            display_id INTEGER NOT NULL UNIQUE
        );

CREATE TABLE "task_history" (
            id INTEGER PRIMARY KEY NOT NULL,
            task_id INTEGER NOT NULL DEFAULT 0,
            project_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '' CHECK(event_type IN ('created', 'status_changed', 'completed', 'cancelled')),
            old_status TEXT,
            new_status TEXT,
            timestamp TEXT NOT NULL DEFAULT ''
        );

CREATE TABLE task_notes_history (
            id         INTEGER PRIMARY KEY NOT NULL,
            task_id    INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            old_notes  TEXT,
            new_notes  TEXT,
            source     TEXT NOT NULL DEFAULT 'unknown',
            timestamp  TEXT NOT NULL
        );

CREATE TABLE "tasks" (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog' CHECK(status IN ('Backlog', 'To Do', 'In Progress', 'Review', 'Done', 'Cancelled', 'TRIAGED', 'READY_FOR_PATCH', 'PR_READY')),
            project_id TEXT NOT NULL DEFAULT '',
            priority TEXT CHECK(priority IN ('Critical', 'High', 'Medium', 'Low', NULL)),
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT,
            prompt TEXT,
            task_type TEXT NOT NULL DEFAULT 'manual' CHECK(task_type IN ('manual', 'agent', 'proposal')),
            review_comment TEXT,
            archived_at TEXT
        , title TEXT, notes TEXT, commit_sha TEXT, category TEXT, parent_id INTEGER, blocked_by TEXT, sequence_order INTEGER, source TEXT, severity TEXT, detected_at TEXT, evidence TEXT, allowed_paths TEXT, definition_of_done TEXT, machine TEXT, created_by TEXT);

CREATE INDEX idx_ai_agents_project ON ai_agents(project_id);

CREATE INDEX idx_attachments_task_id ON task_attachments(task_id);

CREATE INDEX idx_cal_date    ON calendar_events(event_date);

CREATE INDEX idx_cal_machine ON calendar_events(machine);

CREATE INDEX idx_cal_project ON calendar_events(project_id);

CREATE INDEX idx_cal_status  ON calendar_events(status);

CREATE INDEX idx_cron_jobs_project ON cron_jobs(project_id);

CREATE INDEX idx_delete_audit_table ON delete_audit_log(table_name);

CREATE INDEX idx_delete_audit_time ON delete_audit_log(deleted_at);

CREATE INDEX idx_handoffs_card_id
        ON handoffs (card_id);

CREATE INDEX idx_handoffs_project
        ON handoffs (project);

CREATE INDEX idx_handoffs_resolved_at
        ON handoffs (resolved_at)
        WHERE resolved_at IS NULL;

CREATE INDEX idx_history_event ON task_history(event_type);

CREATE INDEX idx_history_project ON task_history(project_id);

CREATE INDEX idx_history_timestamp ON task_history(timestamp);

CREATE INDEX idx_job_submissions_job_submitted
        ON job_submissions(job_id, submitted_at);

CREATE INDEX idx_job_submissions_submitted_at
        ON job_submissions(submitted_at);

CREATE INDEX idx_jobs_category ON jobs(category);

CREATE INDEX idx_jobs_deleted_at ON jobs(deleted_at);

CREATE INDEX idx_jobs_first_seen ON jobs(first_seen);

CREATE INDEX idx_jobs_kind ON jobs(kind);

CREATE INDEX idx_loop_executions_name_time 
        ON loop_executions(loop_name, started_at DESC);

CREATE INDEX idx_migrations_name
        ON migrations (name);

CREATE INDEX idx_migrations_project
        ON migrations (project_id);

CREATE INDEX idx_migrations_status
        ON migrations (status)
        WHERE status = 'recording';

CREATE INDEX idx_outreach_contacts_deleted_replied
        ON outreach_contacts(deleted_at, replied_at);

CREATE UNIQUE INDEX idx_project_checkouts_open
        ON project_checkouts (project_id) WHERE checked_in_at IS NULL;

CREATE INDEX idx_project_info_project ON project_info(project_id);

CREATE INDEX idx_project_info_scope_key ON project_info(project_id, key);

CREATE INDEX idx_projects_last_modified ON projects(last_modified DESC);

CREATE INDEX idx_projects_name ON projects(name);

CREATE INDEX idx_projects_status ON projects(status);

CREATE INDEX idx_service_deps_project ON service_dependencies(project_id);

CREATE INDEX idx_site_stats_site_time ON site_stats(site, ran_at DESC);

CREATE INDEX idx_task_notes_history_task_timestamp
        ON task_notes_history(task_id, timestamp);

CREATE INDEX idx_tasks_blocked ON tasks(blocked_by) WHERE blocked_by IS NOT NULL;

CREATE INDEX idx_tasks_completed ON tasks(completed_at) WHERE completed_at IS NOT NULL;

CREATE INDEX idx_tasks_parent ON tasks(parent_id);

CREATE INDEX idx_tasks_project ON tasks(project_id);

CREATE INDEX idx_tasks_status ON tasks(status);

CREATE TRIGGER audit_project_delete
    BEFORE DELETE ON projects
    FOR EACH ROW
    BEGIN
        INSERT INTO delete_audit_log (table_name, deleted_id, deleted_data, deleted_at, source)
        VALUES (
            'projects',
            OLD.id,
            json_object(
                'id', OLD.id,
                'name', OLD.name,
                'path', OLD.path,
                'status', OLD.status,
                'description', OLD.description
            ),
            datetime('now'),
            'trigger'
        );
    END;

CREATE TRIGGER audit_task_delete
            BEFORE DELETE ON tasks
            WHEN (SELECT enabled FROM _delete_permissions WHERE id = 1) = 1
            BEGIN
                INSERT INTO delete_audit_log (table_name, deleted_id, deleted_data, deleted_at, source)
                VALUES (
                    'tasks',
                    OLD.id,
                    json_object(
                        'id', OLD.id,
                        'text', OLD.text,
                        'status', OLD.status,
                        'project_id', OLD.project_id,
                        'priority', OLD.priority,
                        'created_at', OLD.created_at,
                        'updated_at', OLD.updated_at,
                        'completed_at', OLD.completed_at,
                        'parent_id', OLD.parent_id,
                        'blocked_by', OLD.blocked_by,
                        'sequence_order', OLD.sequence_order
                    ),
                    datetime('now'),
                    'application'
                );
            END;

CREATE TRIGGER block_task_delete
            BEFORE DELETE ON tasks
            WHEN (SELECT enabled FROM _delete_permissions WHERE id = 1) = 0
            BEGIN
                INSERT INTO delete_attempt_log (table_name, attempted_at, reason)
                VALUES ('tasks', datetime('now'), 'blocked: _delete_permissions.enabled = 0');
                SELECT RAISE(FAIL, 'Task deletes are blocked. Set _delete_permissions.enabled=1 first (application only).');
            END;

CREATE TRIGGER codebase_size_snapshots_no_delete
        BEFORE DELETE ON codebase_size_snapshots
        BEGIN SELECT RAISE(ABORT, 'codebase_size_snapshots is append-only'); END;

CREATE TRIGGER codebase_size_snapshots_no_update
        BEFORE UPDATE ON codebase_size_snapshots
        BEGIN SELECT RAISE(ABORT, 'codebase_size_snapshots is append-only'); END;

INSERT INTO "_delete_permissions" VALUES (1, 0);
