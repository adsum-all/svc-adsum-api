# File-size exceptions (documented)

Per the CODE-THRESHOLDS-POLICY, a file may exceed the 500-line hard block, up to
the 750-line absolute maximum, when the exception is documented here with a
justification and a plan. The CI file-size gate skips the files listed below.

These are cohesive domain modules. Splitting them is tracked technical debt and
will be done in a dedicated refactor with full test coverage, not under a
deployment-unblock constraint.

## Exceptions

- app/schemas.py (833 lines): central Pydantic schema module for the member and
  admin surfaces. It currently exceeds the 750 absolute maximum and is therefore
  PRIORITY debt: it must be split by domain (member, admin, reference) with
  re-exports, mindful of forward-reference and import-cycle regressions.
- app/demandes.py (715 lines): member ticket and request domain (member and
  admin endpoints, unlock workflow, read receipts). The static catalogue and
  state machine already live in demandes_catalogue.py; next split: member
  endpoints versus admin endpoints.
- app/inscription.py (560 lines): account provisioning and onboarding. To be
  split by extracting the credential-delivery helpers into a submodule.
- app/notifications.py (518 lines): notification engine and daily job
  (birthday, reminders, closing windows, auto-close of overdue unlocks,
  notification retention purge). To be split by extracting the daily job.
- app/participation.py (603 lines): participation domain (window formula, member
  declaration, per-event statistics, global trends, per-member analytics). To be
  split into member endpoints and admin statistics, keeping FENETRE_FIN_SQL as
  the single shared window formula.
- app/admin.py (709 lines): central back-office admin surface (member governance,
  events, organisation, permissions). The recurring-activity series endpoint was
  already split out into evenements_series.py; next split: separate member
  governance from the events endpoints.
- app/membres.py (556 lines): member-facing endpoints (profile, card, agenda,
  participation). To be split by extracting the agenda/events endpoints.
- app/fichiers.py (510 lines): identity-document storage domain (upload, encrypted
  content read, admin access with audit). To be split by extracting the
  encryption and storage helpers into a submodule.
- app/collaboration_cartes.py (517 lines): rich collaboration card domain (the
  nested CarteProto builder plus card CRUD, move, duplicate, archive). Comments,
  reactions and checklists already live in collaboration_cartes_social.py; next
  split: extract the CarteProto builder helpers into collaboration_cartes_build.py.

## Rule

The absolute maximum remains 750 lines. A file over 750 lines is never accepted,
even if listed here. Remove an entry as soon as its file is split back under 500.
