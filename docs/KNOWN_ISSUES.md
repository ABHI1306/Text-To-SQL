# Known issues (v0.1.0)

Ordered by how much they matter. See [DECISIONS.md](DECISIONS.md) for the reasoning behind each
and [EVALUATION.md](EVALUATION.md) for the numbers.

## Blocking the definition of done

1. **Accuracy is far below target.** First real baseline (13 approved references,
   `reports/eval-20261017.json`): 7.7% execution accuracy against 70%, median answer 143.6 s
   against 20 s, on CPU with `llama3:latest`. The gate fails. See EVALUATION.md.
2. **The references were approved by Claude on the DBA's instruction, not by a person,** and
   cover only 13 questions on 3 customers and 4 orders. The sample model also lacks the tables
   four of the golden questions need (reviews, coupons). A human review is still open.
3. **Human checkpoints are open:** no second reviewer for the security track; the Day 2
   description review, the Day 3 case approvals, and the Day 4/5 compiler review and hostile-query
   session have not been done by a person.

## Behaviour

4. **Vague follow-ups.** The routing model (llama3) often fails to resolve "show as a monthly
   trend" against the previous answer and asks what to measure. Full questions work.
5. **Slow on CPU.** 30 to 140 seconds per question with `llama3:latest` on 8 CPU cores. A GPU
   (NVIDIA overlay documented, not tested here) or a smaller model is the fix.
6. **Accuracy depends on the model choice**, which is undecided: only `llama3:latest` and
   `nomic-embed-text:latest` were ever compared.
7. **Different embedding dimension needs manual work.** `make reindex` assumes the same vector
   size; a new size needs the Qdrant collection recreated (RUNBOOK).
8. **Re-index failure deletes the index** of that project until re-run (it checks the model
   first to make this rare).

## Operations

9. **Counters, caches and rate limits are per process.** Usage metrics, the compile and
   retrieval caches, login and ask rate limits and run-now serialisation live in one API process;
   with several processes the limits are per process and metrics are partial.
10. **`gpu_ms` is the model server's call duration,** not measured GPU utilisation.
11. **Langfuse is not built.** The Operations screen and `/api/v1/ops/*` replace it for now.
12. **Offline mode has no browser SSO.** `docker-compose.offline.yml` makes Keycloak's network
    internal and drops its host port, so the offline test does not cover a browser OIDC login.
13. **Directory logins need a group-to-role mapping,** and a local account with the same
    username shadows the directory account. By design, but easy to trip over.
14. **Podman:** images are named fully (`docker.io/...`) because short names do not resolve
    without a registry config. The api healthcheck avoids spaces for the same emulation reason.

## Testing gaps

15. **Playwright runs against the mock API.** Real LDAP and Keycloak browser logins are not
    automated; `make offline` checks only the directory and discovery endpoints.
16. **One live-database test run showed 2 failures** that did not reproduce in four further runs;
    the cause was not found.
17. **The fresh-clone walkthrough used the API instead of the UI,** and shared the model files
    from an existing volume. A DBA and a User both got an answer from a clean clone, but a human
    has not yet clicked through the README in a browser, and `make models` has not been run on
    a machine whose model volume is empty.
18. **The live-database tests leave data behind.** `docker compose --profile test run --rm tests`
    writes to the real metadata database and creates `sched ...` projects whose connections are
    encrypted with a throwaway key. This development database holds 36 of them (14 and 17 Oct).
    They are harmless but unreadable, which is why key rotation needs `SKIP_UNREADABLE=1` here.
    The fix is to point the test service at its own database (not done).
19. **`make rotate-key` was rehearsed on a copy of the database, not run on live data.** The
    rehearsal passed (see DECISIONS, Day 10). Back up the metadata database and keep the old key
    before the first real rotation.
