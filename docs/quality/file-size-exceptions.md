# File-size exceptions (documented)

Per the CODE-THRESHOLDS-POLICY, a file may exceed the 500-line hard block, up to
the 750-line absolute maximum, when the exception is documented here with a
justification and a plan. The CI file-size gate skips the files listed below.

These are cohesive domain modules. Splitting them is tracked technical debt and
will be done in a dedicated refactor with full test coverage, not under a
deployment-unblock constraint.

## Exceptions

- app/schemas.py (620 lines): central Pydantic schema module for the member and
  admin surfaces. Splitting risks forward-reference and import-cycle regressions;
  to be split by domain (member, admin, reference) with re-exports.
- app/demandes.py (716 lines): member ticket and request domain (catalogue, state
  machine, step tracking, member and admin endpoints). To be split into member
  endpoints, admin endpoints, and the static catalogue.
- app/inscription.py (560 lines): account provisioning and onboarding. To be
  split by extracting the credential-delivery helpers into a submodule.

## Rule

The absolute maximum remains 750 lines. A file over 750 lines is never accepted,
even if listed here. Remove an entry as soon as its file is split back under 500.
