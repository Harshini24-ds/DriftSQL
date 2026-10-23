# DriftSQL Month 2: Member 1 Handoff

## Purpose

This document summarizes Member 1's Month 2 evidence work and gives Member 2
the information needed to review it and connect it to QIM and retrieval.

The frozen DriftSQL Master and Production blueprints remain the project
authority. This work targets the Olist database:

- `database_id`: `olist_primary`
- PostgreSQL database: `driftsql`
- Olist schema: `olist_raw`

## Status at handoff

| Task | What is implemented | Current status |
|---|---|---|
| EVID-003: Metadata | Olist metadata catalog, schema/hash validation, evidence loader, unit tests | Catalog is `DRAFT_FOR_TEAM_REVIEW`; team review is pending |
| EVID-004: Glossary | Versioned glossary format, approval/effective-date validation, evidence loader, unit tests | Definitions are drafts; team review is pending |
| EVID-006: Permissions | PostgreSQL role grants, policy fixture, integration tests | Member 1 reports all 4 permission tests passed; teammate review of the sample policy is pending |

The focused unit test run reported **19 passed**. The EVID-006 integration
test run reported **4 passed**. These tests do not represent team approval of
the business definitions or permission policy.

EVID-003 was previously pushed to the `month2-member1` branch. Check Git status
before sharing the later EVID-004 and EVID-006 files; they may still be local.

## EVID-003: Metadata catalog

Files:

- `evidence/metadata.json`
- `src/evidence/metadata.py`
- `tests/unit/test_metadata_evidence.py`

The catalog describes Olist tables and selected columns with descriptions,
aliases, business terms, units, schema identity, and provenance.

The loader checks that the catalog matches the schema snapshot's
`database_id`, schema, and schema hash. It also checks that catalog targets
exist in the snapshot. Draft metadata is validated, but it produces no runtime
evidence until the catalog is approved.

### Member 2 review

- Check that metadata evidence uses the same `database_id` and schema hash as
  the request and schema snapshot.
- Treat the current descriptions as draft suggestions until the team approves
  them.
- Do not interpret `price` or `payment_value` as “revenue” without an approved
  business definition.

## EVID-004: Business glossary

Files:

- `evidence/glossary.json`
- `src/evidence/glossary.py`
- `tests/unit/test_glossary_evidence.py`

The glossary stores business terms, definitions, aliases, target fields,
version, status, effective dates, and provenance. The loader checks glossary
targets against the metadata catalog and checks effective periods. It emits
evidence only for approved definitions whose effective period applies. The
latest applicable approved version is selected; same-date competing
definitions are rejected rather than silently chosen.

The current Olist glossary contains draft entries for:

- `order`
- `order item`
- `payment value`

It intentionally does not define “revenue.” The team needs to agree on its
meaning before adding an approved definition.

The approved current glossary is the highest authority for business meaning.
A user may clarify their question or choose among approved interpretations.
A user clarification cannot create a new authoritative definition or override
the approved glossary, schema, or permissions.

### Member 2 review

- Review the wording and aliases with Member 1.
- Flag definitions that are unclear or unsupported by verified Olist facts.
- Keep glossary definitions separate from document descriptions and metadata.
- Use `build_glossary_evidence()` only after the catalog and metadata are
  approved.

## EVID-006: Permission policies

Files:

- `migrations/002_permissions.sql`
- `evidence/policies.json`
- `tests/integration/test_permissions.py`

The PostgreSQL migration creates non-login fixture roles:

- `driftsql_olist_reader`: can read the configured Olist tables.
- `driftsql_orders_limited`: can read `order_id` and `order_status` from
  `olist_raw.orders`; the sample policy denies `customer_id`.

The integration tests use `SET ROLE` and ask PostgreSQL to enforce allowed and
denied operations. The successful four-test run checks the local database
configuration used by those tests. PostgreSQL remains the authority for
whether a query is actually permitted; `policies.json` documents the fixture
policy and does not replace PostgreSQL enforcement.

### Member 2 review

- Confirm the sample `customer_id` restriction is appropriate for the demo.
- Check that retrieval and query execution use the active database role.
- Make permission denial a hard stop; metadata or user clarification must not
  grant access.

## Member 2 integration point

Member 2 owns QIM, `QueryRequest` / `QueryIntent`, required evidence
obligations `R(q)`, alias and ambiguity handling, retrieval, normalization,
and schema drift detection.

Expected connection:

1. QIM receives a request with `database_id = olist_primary` and determines
   the required evidence obligations for the question.
2. Retrieval gathers relevant schema, metadata, glossary, and documentation
   evidence while respecting PostgreSQL permissions.
3. Normalization builds one `EvidenceSnapshot` for the request. Every item in
   the snapshot must have the same `database_id`; the snapshot records the
   matching schema hash.
4. If evidence is ambiguous, stale, unapproved, or inaccessible, DriftSQL
   should clarify, refresh, or reject according to the approved recovery
   rules. It should not guess or bypass PostgreSQL permissions.

Agree on the QIM-to-retrieval function signatures and evidence input/output
shapes together before wiring the modules. EVID-005 documentation remains
secondary to an approved glossary definition.

## Viva explanation

**EVID-003:** “We created a versioned metadata catalog for Olist and validate
it against the schema snapshot. That prevents metadata from pointing to
missing fields or a different schema. It stays draft until reviewed.”

**EVID-004:** “We store team-approved definitions separately from technical
metadata. Each definition has a version, effective period, and provenance.
Only an approved definition that applies to the relevant date becomes
runtime evidence. Ambiguous business meaning is not guessed.”

**EVID-006:** “We use PostgreSQL roles to enforce access. The tests switch to
restricted roles and verify that allowed reads work and denied reads or
writes fail. The JSON policy records the fixture; PostgreSQL decides access.”

**How the three connect:** “Metadata describes the available data, the
glossary explains approved business meanings, and PostgreSQL permissions
control what the current role may access. QIM and retrieval can combine
their evidence in a database-specific snapshot.”

## Remaining review and coordination

- Member 1 and Member 2 review the metadata and glossary definitions together.
- Confirm the sample permission policy with Member 2.
- Keep draft catalogs marked as drafts until the team approves them.
- Connect the evidence loaders to Member 2's retrieval and normalization work.
- Check Git status and ensure no passwords, DSNs, `.idea` files, or cache
  archives are included before sharing the branch.

## Scope note

The Month 2 work uses Olist as the primary dataset. Supporting an arbitrary
new dataset with no preparation is a later extension. A new dataset needs a
connection and schema discovery, approved business meanings, permission
checks, and tests before DriftSQL can safely answer business questions about
it.