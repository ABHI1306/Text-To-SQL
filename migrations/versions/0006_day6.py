"""Day 6: conversation threads, project knowledge, scheduler and drift, OIDC login state."""

from alembic import op

revision = "0006_day6"
down_revision = "0005_day5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE threads (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE thread_turns (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            standalone_question TEXT NOT NULL,
            logical_sql TEXT,
            answer TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX thread_turns_thread_idx ON thread_turns (thread_id, id);
        GRANT SELECT, INSERT ON threads, thread_turns TO text2sql_app;

        CREATE TABLE project_instructions (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            text TEXT NOT NULL CHECK (length(btrim(text)) > 0 AND length(text) <= 1000),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX project_instructions_project_idx ON project_instructions (project_id, active);

        CREATE TABLE sql_pairs (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            question TEXT NOT NULL CHECK (length(btrim(question)) > 0 AND length(question) <= 2000),
            logical_sql TEXT NOT NULL CHECK (length(btrim(logical_sql)) > 0
                                             AND length(logical_sql) <= 20000),
            status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
            source TEXT NOT NULL CHECK (source IN ('manual', 'feedback')),
            proposed_by UUID REFERENCES users(id),
            decided_by UUID REFERENCES users(id),
            decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX sql_pairs_project_status_idx ON sql_pairs (project_id, status, created_at);

        CREATE TABLE feedback (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            thread_id UUID,
            message_id TEXT,
            rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
            question TEXT NOT NULL,
            logical_sql TEXT NOT NULL,
            pair_id UUID REFERENCES sql_pairs(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX feedback_project_idx ON feedback (project_id, created_at);

        GRANT SELECT, INSERT, UPDATE, DELETE ON project_instructions, sql_pairs TO text2sql_app;
        GRANT SELECT, INSERT ON feedback TO text2sql_app;

        CREATE TABLE scheduled_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            -- the person the job runs as; SET NULL (not CASCADE) so a deleted owner pauses the
            -- job at its next slot instead of silently erasing it
            owner_id UUID REFERENCES users(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK (kind IN ('profile_drift')),
            schedule TEXT NOT NULL CHECK (length(btrim(schedule)) > 0),
            timezone TEXT NOT NULL DEFAULT 'UTC',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
            paused_reason TEXT,
            -- the next slot to run; advanced by whichever worker wins that slot's lease
            next_run_at TIMESTAMPTZ NOT NULL,
            last_run_at TIMESTAMPTZ,
            last_status TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (project_id, kind)
        );
        CREATE INDEX scheduled_jobs_due_idx ON scheduled_jobs (next_run_at) WHERE status = 'active';

        -- One row per (job, slot): the primary key is what keeps two workers off the same slot.
        CREATE TABLE job_leases (
            job_id UUID NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
            slot TIMESTAMPTZ NOT NULL,
            worker_id TEXT NOT NULL,
            leased_until TIMESTAMPTZ NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
            result JSONB,
            error TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            PRIMARY KEY (job_id, slot)
        );
        CREATE INDEX job_leases_running_idx ON job_leases (leased_until) WHERE status = 'running';

        CREATE TABLE drift_reports (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            mdl_version INTEGER NOT NULL,
            job_id UUID REFERENCES scheduled_jobs(id) ON DELETE SET NULL,
            slot TIMESTAMPTZ,
            has_drift BOOLEAN NOT NULL,
            summary JSONB NOT NULL,
            findings JSONB NOT NULL,
            draft_mdl_version INTEGER,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        -- a slot retaken after a crash must not store a second report
        CREATE UNIQUE INDEX drift_reports_slot_idx ON drift_reports (job_id, slot)
            WHERE job_id IS NOT NULL;
        CREATE INDEX drift_reports_project_idx ON drift_reports (project_id, created_at DESC);

        -- State for logins in flight: only the hash of the state, deleted on first use.
        CREATE TABLE oidc_login_states (
            state_hash TEXT PRIMARY KEY,
            nonce TEXT NOT NULL,
            code_verifier TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX oidc_login_states_expiry_idx ON oidc_login_states (expires_at);

        GRANT SELECT, INSERT, UPDATE, DELETE ON scheduled_jobs TO text2sql_app;
        GRANT SELECT, INSERT, UPDATE ON job_leases, drift_reports TO text2sql_app;
        GRANT SELECT, INSERT, DELETE ON oidc_login_states TO text2sql_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON oidc_login_states, drift_reports, job_leases, scheduled_jobs FROM text2sql_app;
        DROP TABLE oidc_login_states;
        DROP TABLE drift_reports;
        DROP TABLE job_leases;
        DROP TABLE scheduled_jobs;
        REVOKE ALL ON feedback, sql_pairs, project_instructions FROM text2sql_app;
        DROP TABLE feedback;
        DROP TABLE sql_pairs;
        DROP TABLE project_instructions;
        DROP TABLE thread_turns;
        DROP TABLE threads;
        """
    )
