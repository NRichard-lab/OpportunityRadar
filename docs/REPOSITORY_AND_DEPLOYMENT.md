# Repository and deployment source

OpportunityRadar's authoritative source chain is:

```text
C:\Users\dog10\Documents\Codex\FinancialJobsRadarOrig (main)
  -> https://github.com/NRichard-lab/OpportunityRadar.git (main)
  -> /srv/opportunity-radar/.full-feature/releases/<git-sha>
  -> SHA-tagged OpportunityRadar containers
  -> /srv/opportunity-radar/database/opportunity_radar.db
```

The local folder keeps its historical name because Codex project configuration and linked Git worktrees reference it. Its `origin` must point to `NRichard-lab/OpportunityRadar`. The local `financialjobsradar-legacy` remote exists only for read access to the former repository; its push URL is disabled. `NRichard-lab/FinancialJobsRadar` must not be used for new work or deployments.

Production is an artifact deployment, not a Git checkout. Build a release only from an explicitly tested commit that is reachable from `origin/main`. Create the source archive from that commit, transfer it into `/srv/opportunity-radar/.full-feature/incoming`, extract it into `/srv/opportunity-radar/.full-feature/releases/<git-sha>`, and build immutable images tagged with the same full SHA. The production source manifest is `/srv/opportunity-radar/.full-feature/source-repository.txt`.

Compose is `/srv/opportunity-radar/compose.production.yaml`. Persistent SQLite data is `/srv/opportunity-radar/database/opportunity_radar.db` and must remain outside release directories and images. Before any schema migration or destructive data change, create and validate a database backup. A repository-only change does not require a rebuild, restart, database backup, or data operation.

Blue Ash Portal is a separate repository and application. Do not treat its working tree as OpportunityRadar or alter its unrelated changes.
