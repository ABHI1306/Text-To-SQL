"""Day 7: saved questions, dashboards, shares, result cache, scheduled questions, evaluation,
notifications, installation settings and saved chat history (summary, chart, trace per turn)."""

from alembic import op

revision = "0007_day7"
down_revision = "0006_day6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE saved_questions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question TEXT NOT NULL CHECK (length(btrim(question)) > 0 AND length(question) <= 2000),
            logical_sql TEXT NOT NULL CHECK (length(btrim(logical_sql)) > 0
                                             AND length(logical_sql) <= 20000),
            chart_spec JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX saved_questions_owner_idx ON saved_questions (project_id, owner_id);

        CREATE TABLE dashboards (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL CHECK (length(btrim(title)) > 0 AND length(title) <= 200),
            layout_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX dashboards_owner_idx ON dashboards (project_id, owner_id);

        CREATE TABLE dashboard_tiles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dashboard_id UUID NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
            saved_question_id UUID NOT NULL REFERENCES saved_questions(id) ON DELETE CASCADE,
            chart_spec JSONB,
            position_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            refresh_schedule TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX dashboard_tiles_dashboard_idx ON dashboard_tiles (dashboard_id, sort_order);

        -- Read-only grants. grantee_id is a user id, or a role name when grantee_type = 'role'.
        CREATE TABLE shares (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            resource_type TEXT NOT NULL CHECK (resource_type IN ('dashboard', 'saved_question', 'thread')),
            resource_id UUID NOT NULL,
            grantee_type TEXT NOT NULL CHECK (grantee_type IN ('user', 'role')),
            grantee_id TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'view' CHECK (permission = 'view'),
            granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (resource_type, resource_id, grantee_type, grantee_id)
        );
        CREATE INDEX shares_grantee_idx ON shares (grantee_type, grantee_id);

        -- A cached result is reusable only by a viewer whose compiled policy_context equals the
        -- one it was computed under, on the same model version.
        CREATE TABLE result_cache (
            resource_type TEXT NOT NULL,
            resource_id UUID NOT NULL,
            policy_context TEXT NOT NULL,
            mdl_version INTEGER NOT NULL,
            columns JSONB NOT NULL,
            rows JSONB NOT NULL,
            truncated BOOLEAN NOT NULL DEFAULT FALSE,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (resource_type, resource_id, policy_context, mdl_version)
        );
        CREATE INDEX result_cache_expiry_idx ON result_cache (expires_at);

        -- Scheduled questions run on the Day 6 job engine: one more job kind that points at a
        -- saved question. The job row is the schedule; question_runs is the history.
        ALTER TABLE scheduled_jobs DROP CONSTRAINT scheduled_jobs_kind_check;
        ALTER TABLE scheduled_jobs ADD CONSTRAINT scheduled_jobs_kind_check
            CHECK (kind IN ('profile_drift', 'saved_question'));
        ALTER TABLE scheduled_jobs DROP CONSTRAINT scheduled_jobs_project_id_kind_key;
        CREATE UNIQUE INDEX scheduled_jobs_project_kind_idx ON scheduled_jobs (project_id, kind)
            WHERE kind = 'profile_drift';
        ALTER TABLE scheduled_jobs
            ADD COLUMN saved_question_id UUID REFERENCES saved_questions(id) ON DELETE CASCADE;
        CREATE INDEX scheduled_jobs_question_idx ON scheduled_jobs (saved_question_id)
            WHERE saved_question_id IS NOT NULL;

        CREATE TABLE question_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id UUID NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
            slot TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
            policy_context TEXT,
            row_count INTEGER,
            error TEXT,
            trigger TEXT NOT NULL DEFAULT 'schedule' CHECK (trigger IN ('schedule', 'manual')),
            idempotency_key TEXT,
            UNIQUE (job_id, slot),
            UNIQUE (job_id, idempotency_key)
        );
        CREATE INDEX question_runs_job_idx ON question_runs (job_id, started_at DESC);

        CREATE TABLE notifications (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            read_at TIMESTAMPTZ
        );
        CREATE INDEX notifications_user_idx ON notifications (user_id, created_at DESC);

        CREATE TABLE eval_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            started_by UUID REFERENCES users(id) ON DELETE SET NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
            summary JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            UNIQUE (project_id, idempotency_key)
        );
        CREATE TABLE eval_results (
            run_id UUID NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
            case_id TEXT NOT NULL,
            suite TEXT NOT NULL,
            question TEXT NOT NULL,
            generated_sql TEXT,
            outcome TEXT NOT NULL,
            stage TEXT,
            exec_match BOOLEAN,
            failure_code TEXT,
            latency_ms INTEGER,
            gpu_ms INTEGER,
            PRIMARY KEY (run_id, suite, case_id)
        );

        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value_json JSONB NOT NULL,
            updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        GRANT SELECT, INSERT, UPDATE, DELETE ON
            saved_questions, dashboards, dashboard_tiles, shares, result_cache, question_runs,
            app_settings TO text2sql_app;
        GRANT SELECT, INSERT, UPDATE ON notifications, eval_runs, eval_results TO text2sql_app;

        -- Chat history: each turn keeps its summary, chart, timing, failure, step timeline and the
        -- model version it ran against. Result rows are deliberately not stored: they were computed
        -- under one account's row policies, so a reopened chat recomputes them under the viewer's
        -- own (see POST /messages/{id}/run).
        ALTER TABLE thread_turns
            ADD COLUMN summary TEXT,
            ADD COLUMN chart JSONB,
            ADD COLUMN suggestions JSONB,
            ADD COLUMN latency_ms INTEGER,
            ADD COLUMN failure_code TEXT,
            ADD COLUMN failure_message TEXT,
            ADD COLUMN trace JSONB,
            ADD COLUMN model_version INTEGER;
        CREATE INDEX threads_owner_idx ON threads (project_id, user_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX threads_owner_idx;
        ALTER TABLE thread_turns
            DROP COLUMN model_version, DROP COLUMN trace, DROP COLUMN failure_message,
            DROP COLUMN failure_code, DROP COLUMN latency_ms, DROP COLUMN suggestions,
            DROP COLUMN chart, DROP COLUMN summary;
        REVOKE ALL ON eval_results, eval_runs, notifications, app_settings, question_runs,
            result_cache, shares, dashboard_tiles, dashboards, saved_questions FROM text2sql_app;
        DROP TABLE app_settings;
        DROP TABLE eval_results;
        DROP TABLE eval_runs;
        DROP TABLE notifications;
        DROP TABLE question_runs;
        DROP INDEX scheduled_jobs_question_idx;
        DELETE FROM scheduled_jobs WHERE kind = 'saved_question';
        ALTER TABLE scheduled_jobs DROP COLUMN saved_question_id;
        DROP INDEX scheduled_jobs_project_kind_idx;
        ALTER TABLE scheduled_jobs ADD CONSTRAINT scheduled_jobs_project_id_kind_key
            UNIQUE (project_id, kind);
        ALTER TABLE scheduled_jobs DROP CONSTRAINT scheduled_jobs_kind_check;
        ALTER TABLE scheduled_jobs ADD CONSTRAINT scheduled_jobs_kind_check
            CHECK (kind IN ('profile_drift'));
        DROP TABLE result_cache;
        DROP TABLE shares;
        DROP TABLE dashboard_tiles;
        DROP TABLE dashboards;
        DROP TABLE saved_questions;
        """
    )
