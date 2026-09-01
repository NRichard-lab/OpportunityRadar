# OpportunityRadar repository instructions

- This repository is OpportunityRadar. Its canonical GitHub repository is `https://github.com/NRichard-lab/OpportunityRadar.git`, and the canonical branch is `main`.
- The authoritative local checkout is `C:\Users\dog10\Documents\Codex\FinancialJobsRadarOrig`. The older folder name is retained because Codex project configuration and linked worktrees reference it.
- `https://github.com/NRichard-lab/FinancialJobsRadar.git` is legacy/read-only. Never push new OpportunityRadar work there.
- Blue Ash Portal is a separate application and repository. Do not modify it unless a task explicitly requires portal integration, and never modify unrelated repositories.
- Requested OpportunityRadar work may proceed through implementation, testing, commit, push, production deployment, and verification without routine approval.
- Test the exact Git SHA that will be deployed. Production uses immutable source archives and SHA-tagged images under `/srv/opportunity-radar`; see `docs/REPOSITORY_AND_DEPLOYMENT.md`.
- Back up and validate the production SQLite database before a database migration or destructive data change. Do not replace or migrate production data for source-only deployments.
