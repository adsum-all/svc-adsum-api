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
- app/admin.py (575 lines): central back-office admin surface (member governance,
  events, organisation, permissions). The event create/update logic now lives in the
  shared activites engine (reused by collaboration); next split: separate member
  governance from the events endpoints.
- app/membres.py (556 lines): member-facing endpoints (profile, card, agenda,
  participation). To be split by extracting the agenda/events endpoints.
- app/groupes.py (513 lines): access-group domain (role sync, escalation guards,
  membership perimeters, application tagging). The read-only views already live
  in groupes_lecture.py; next split: extract the account-role synchronisation
  helpers (_sync_account_role and guards) into a submodule.
- app/fichiers.py (510 lines): identity-document storage domain (upload, encrypted
  content read, admin access with audit). To be split by extracting the
  encryption and storage helpers into a submodule.
- app/collaboration_transverse.py (550 lines): cross-cutting collaboration domain
  (notifications, profile, my-cards / calendar views, dashboards, search, and the
  shared-calendar activities: list of the whole evenement programme plus create /
  edit / cancel from collaboration). The my-cards and calendar views now assemble
  their cards through the shared batched reader (assemble_cartes). Next split: move
  the activities endpoints into collaboration_activites.py.
- app/collaboration_espaces.py (503 lines): collaboration space domain (space CRUD,
  membership and roles, labels, access requests, and the space-role guard reused by
  every card endpoint). The space payload now carries each member's display name and
  initials (single utilisateur+membre join) so the assignee picker shows every space
  member, not only staff. Next split: extract the membership/access-request endpoints
  into collaboration_espaces_membres.py.

- app/auth.py (696 lines): authentication and session domain (login, OTP/MFA, device
  trust, token issuance, current-user resolution). To be split by extracting the OTP/MFA
  and device-trust helpers into an auth_mfa.py submodule.
- app/collaboration_canal.py (802 lines): instruction-channel domain (channel notes,
  instruction workflow, moderation, emitters). PRIORITY debt: it exceeds the 750 absolute
  maximum and must be split first, by extracting the instruction workflow and moderation
  into dedicated submodules.
- app/collaboration_tableaux.py (605 lines): collaboration boards domain (board CRUD,
  columns, per-board instruction sync). To be split by extracting the column and
  instruction-sync logic into collaboration_tableaux_colonnes.py.
- app/formation.py (521 lines): questionnaire availability and member notification
  preferences (per-group channel matrix, master switches, week-start and questionnaire
  window parameters). To be split by extracting the admin parameter endpoints.

The rich collaboration card domain used to be listed here at 641 lines; it was split
under 500 (497) by extracting the nested-card assembly into collaboration_cartes_read.py,
so app/collaboration_cartes.py is no longer an exception.

## Rule

The absolute maximum remains 750 lines. A file over 750 lines is never accepted,
even if listed here. Remove an entry as soon as its file is split back under 500.
