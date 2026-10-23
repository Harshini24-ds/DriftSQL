# DriftSQL

DriftSQL is an Enterprise Query Intelligence and Validation System. It is being developed in stages according to the frozen Master and Production Implementation Blueprints, which remain the authority for project scope and implementation.

## Current project status

- **Month 1:** Completed.
- **Month 2:** Completed and merged into the `develop` branch.
- **Month 2 verification:** 116 tests passed.
- **Implemented Month 2 flow:** Query Intelligence Module (QIM) classifies supported and unsupported requests, creates required evidence obligations, retrieves and normalizes enterprise evidence, and builds an `EvidenceSnapshot` for supported requests.
- **Month 3:** Not started.

The Month 2 demonstration confirms that a supported request can be accepted and produce a complete evidence snapshot, while an unsupported operation is rejected by the QIM precheck. SQL generation is not part of this completed Month 2 flow.

## Git branch workflow

- `main` remains the stable baseline.
- `develop` contains the integrated Month 2 work.
- Month 3 work should use separate member branches based on `develop`, with pull requests targeting `develop`.

## Safety and preservation

- `.venv/` is machine-specific and should not be committed.
- Raw Olist CSV files are excluded from Git by `.gitignore`.
- No passwords, API keys, or database credentials should be committed.
- Credential examples must remain placeholders.
- Implementation should follow the approved project blueprints and be tested before integration.

## Data and implementation milestones

- DATA-001: Completed.
- DATA-002: Completed.
- DATA-003: Relational verification, adapter, registry, tests, and schema snapshot completed.
- Month 2: QIM, evidence retrieval and normalization, value facts, schema-drift checks, evidence snapshots, and the Month 2 evaluation dataset completed.

See the project blueprints in `docs/blueprints/` for the authoritative architecture and roadmap.
