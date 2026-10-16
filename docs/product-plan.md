> 雙模式版補充：本文件保留原版設計歷程。此獨立版本已授權本工具自己的被動小面板、點擊穿透、1080p/1440p ROI 對應；具體邊界與驗收以 [display-edition.md](display-edition.md) 為準。遊戲附掛、注入、控制與完整畫面持久化仍禁止。

# Wuthering Waves Personal Assistant V1 product plan

## 1. Outcome and scope

V1 turns the existing offline echo-recognition proof of concept into a Traditional Chinese,
Windows-first local desktop application that can be started every day. The application keeps
the proven OCR, parser, ROI and temporal-tracking modules and adds a GUI, persistent data,
passive monitor capture, explainable scoring, cultivation planning, tasks and gacha planning.

V1 is intentionally local and user-driven. Static game data can be imported from versioned
JSON or CSV files, but automatic network synchronization is out of scope and disabled by
default. Combat simulation, automatic game actions and an in-game overlay are not V1 goals.

## Delivery status

| Milestone | Status | Verified result |
|---|---|---|
| 2 - desktop and data foundation | Complete (2026-08-01) | Desktop smoke start, 27-table schema, persistent settings, offscreen GUI tests |
| 3 - passive live recognition | Complete (2026-08-01) | Fake/image-sequence/mss backends, ROI lease boundary, stable latest-only OCR, safety scan |
| 4 - live echo workspace | Complete (2026-08-01) | Manual start/stop, background persistence, correction audit, restart-safe history |
| 5 - scoring and decisions | Complete (2026-08-01) | Four editable templates, versioned Roll data, explainable scoring, bounded projections, persisted decisions |
| 6 - character cultivation | Complete (2026-08-01) | Atomic character/team CRUD, five independent echo slots, deterministic completion, Qt Model/View |
| 7 - daily planning | Complete (2026-08-01) | Breadth-first queue, transactional material/target CRUD, deterministic waveplate plan, Qt Today page |
| 8 - tasks and gacha | Complete (2026-08-08) | Offline recurring reminders, limited events, manual gacha resources and three explicit scenarios |
| 9 - onboarding and packaging | Complete (2026-08-08) | First-run wizard, verified backup/restore, diagnostic preview, scripts, one-folder package smoke test |

## 2. Permanent safety boundary

The application may only observe a user-selected monitor while the user has explicitly started
echo-recognition mode. It must never:

- simulate mouse, keyboard or controller input;
- inspect game processes, memory, handles, files or network traffic;
- inject code, hook rendering or create an in-game overlay;
- automate enhancement, selection, locking, discarding, collection, travel or combat;
- use cloud OCR or upload screenshots, character data or other user data;
- record video or write full monitor frames to disk;
- provide combat-time assistance.

Full captured frames have in-memory-only ownership. The capture pipeline immediately derives
the bounded ROIs it needs, releases the frame, and exposes no full-frame persistence API. Low
confidence debug output is opt-in and restricted to bounded ROIs. CI and local test scripts
scan dependencies and source for prohibited capabilities.

## 3. Architecture

The V1 architecture keeps UI, orchestration and domain logic replaceable:

```text
PySide6 views / Qt models
          |
application services and controllers
     /            |              \
SQLite repos   scoring/planner   live recognition coordinator
     |                           /          |          \
versioned migrations      capture backend  stability  existing OCR/parser/tracker
```

- **Presentation:** PySide6 widgets and Qt Model/View. Views receive immutable view data and do
  not run capture, OCR or database work on the GUI thread.
- **Application services:** use-case-oriented services coordinate transactions, persistence and
  domain engines without importing PySide6.
- **Persistence:** SQLite through SQLAlchemy 2 repositories, foreign keys enabled for every
  connection, explicit transactions and an application-owned migration runner. The migration
  mechanism is the lightweight equivalent of Alembic and records each version in
  `schema_version`.
- **Capture:** replaceable `FakeCaptureBackend`, `ImageSequenceBackend` and
  `MssMonitorCaptureBackend`. Only the mss backend accesses desktop pixels, and it does not
  search for or attach to a window or process.
- **Recognition:** the existing OpenCV/RapidOCR pipeline stays offline. A stability gate limits
  OCR to relevant, stable, changed ROIs.
- **Data files:** scoring rules, roll tables, probability assumptions and optional static content
  are versioned package resources with provenance status. User overrides live in SQLite.

## 4. Local data layout

Production data is outside the repository under
`%LOCALAPPDATA%\WuwaPersonalAssistant\`:

```text
data/wuwa-personal-assistant.sqlite3
backups/database/
backups/restore-staging/
backups/restore-rollbacks/
debug/low-confidence-rois/
logs/
exports/
```

The application creates these directories on startup. Tests inject a temporary root. Demo rows
are flagged and can be removed without touching real rows. Logs contain state and error metadata,
never images or OCR ROI contents.

## 5. Milestones and commit gates

Each milestone updates documentation, implements a coherent usable slice, runs the entire local
quality gate and ends in its own Git commit. Later migrations are additive and tested from an
empty database and from the previous schema version.

### Milestone 2 - desktop shell and persistent data foundation

Deliverables:

- PySide6 application bootstrap, resizable dark Traditional Chinese main window and eight-item
  navigation;
- DPI-aware startup, saved geometry, monitor setting and application version/about information;
- local data-path service, SQLite engine, full initial schema, migration runner, transaction and
  repository helpers;
- settings and dashboard services, initial summary cards and useful empty states;
- friendly startup failure for an unavailable or invalid data directory/database;
- offscreen GUI smoke/model tests, migration/foreign-key/transaction/Windows-path tests.

Exit criteria: a user can launch the shell, navigate all pages, change persistent settings, close
and reopen without losing them, and see an empty but functional dashboard.

Expected commit: `feat: add desktop shell and persistent data foundation`

### Milestone 3 - passive live capture and recognition runtime

Deliverables:

- fake, image-sequence and mss monitor capture backends behind one contract;
- monitor enumeration without window/process lookup and configurable 2-8 Hz sampling;
- worker-thread live coordinator, capture-state machine and explicit start/stop lifecycle;
- relevant-page fast classification, 0.5-0.75 second stability gate, duplicate suppression and
  ROI-only OCR dispatch;
- safe ownership/release of full frames, cancellation, backpressure and recoverable errors;
- fake-capture integration tests, no-full-frame-write instrumentation, source/dependency safety
  scanner and CI/local-script enforcement.

Exit criteria: the fake/image sequence pipeline produces stable observations without blocking Qt,
and mss can passively sample a selected monitor without any full-frame write path.

Expected commit: `feat: add passive live recognition pipeline`

### Milestone 4 - live echo workspace and auditable corrections

Deliverables:

- functional echo-enhancement page with capture controls, state, confidence and current
  observation;
- session timeline, level/substat changes, previous/next history and manual final disposition;
- durable sessions, observations and events across page switches, stop and application restart;
- editor for uncertain OCR fields, correction audit rows and deterministic reparse/rescore hooks;
- low-confidence confirmation behavior and optional bounded ROI diagnostics;
- live-session persistence, correction and history view tests.

Exit criteria: a user can observe a sequence, correct it, stop capture, restart and review the same
confirmed session.

Expected commit: `feat: add live echo workspace and correction history`

### Milestone 5 - data-driven scoring and upgrade decisions

Deliverables:

- `CharacterScoringProfile`, weights, conditional/threshold/crit rules, roll tiers, score
  breakdown and upgrade decision domain models;
- versioned generic damage, healing/HP, defence and energy-requirement templates;
- editable profile weights and configurable 0-100 category thresholds;
- actual-roll normalization, crit-balance and threshold-aware scoring;
- tested enumeration model for remaining slots using explicitly labelled assumptions and uniform
  fallback;
- continue/stop/qualified/max/manual-confirmation decisions with concise reasons and detailed
  expandable breakdown.

Exit criteria: known observations receive reproducible scores and explainable decisions; changing
a profile or correction recomputes both.

Expected commit: `feat: add explainable echo scoring and upgrade decisions`

### Milestone 6 - character cultivation records

Deliverables:

- character, weapon, team, skill, target and five permanent-echo CRUD services and views;
- active-roster and priority controls, per-character skill target overrides and small-node status;
- explicit echo-to-character/slot association with no equipment-sharing assumption;
- completion percentage, qualified-echo count, weakest slot and next-action summary;
- optional demo teams clearly labelled as non-authoritative seed data;
- CRUD, relationship, validation and completion tests.

Exit criteria: a user can maintain the characters they may use and see persistent, meaningful
completion status for each one.

Expected commit: `feat: add character and permanent echo management`

### Milestone 7 - breadth-first cultivation and daily resource planning

Deliverables:

- inventory/material/target management and versioned manual static-data import contract;
- breadth-first queue using incorrect set/main stat, incomplete five slots, functional thresholds,
  weakest echo and unfinished skill/weapon priorities;
- daily waveplate plan with primary/secondary actions, run counts, reasons, shortages and estimated
  finish date;
- weekly limits, open-day validation and manual overrides;
- deterministic priority, material-gap, schedule and date-estimation tests.

Exit criteria: the dashboard and Today page explain the next account-wide action from locally
entered data.

Expected commit: `feat: add cultivation queue and daily planner`

### Milestone 8 - tasks, events and gacha planning

Deliverables:

- daily/weekly/custom/limited task CRUD, recurrence, snooze, complete, hide and priority behavior;
- today, next-three-days, this-week and overdue dashboard groups;
- manual astrite/pull/pity/guarantee state and target version/date planning;
- custom future income and conservative/typical/optimistic scenario totals;
- recurrence/date-boundary, task editing and gacha scenario tests.

Exit criteria: tasks and pull plans can be managed fully offline and survive restart.

Expected commit: `feat: add task event and gacha planners`

### Milestone 9 - onboarding, recovery, packaging and V1 acceptance

Deliverables:

- first-run onboarding for safety, monitor capture test, data path, first character/demo data and
  scoring template;
- database backup plus validated JSON export/import with preview, transactions and rollback;
- corrupt-database detection and friendly recovery/restore workflow;
- diagnostic-package manifest and explicit user preview, with no images by default;
- `scripts/setup.ps1`, `run.ps1`, `test.ps1`, `build.ps1`, PyInstaller configuration and bundled
  offline OCR resources;
- packaged application directory, clean-machine instructions, installer follow-up document and
  complete V1 acceptance report.

Exit criteria: development scripts work from a clean checkout, a packaged Windows build starts
without downloading models, and all automated and manual safety/acceptance checks pass.

Expected commit: `feat: deliver installable local desktop V1`

## 6. Quality gate for every milestone

The milestone is not committed until all applicable commands pass:

```powershell
python -B -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pip check
python -m wuwa_assistant.safety_scan
```

Qt tests use `QT_QPA_PLATFORM=offscreen`. Package smoke tests run from the built directory rather
than the source tree. Private screenshots, databases, logs, backups, exports and debug images are
ignored and checked before every commit.

## 7. Migration and compatibility policy

- Migration 1 establishes the complete V1 table namespace so later features can add columns,
  indexes and constraints incrementally without renaming the proven OCR domain.
- Migration 2 adds reproducible scoring-decision fingerprints, decision-input/context snapshots
  and Echo score profile/version/observation provenance; legacy decision rows receive a stable
  local hash during migration.
- Migration 4 makes event deletion retain linked tasks, adds an explicit gacha-income pull type,
  backfills legacy rows as unknown and indexes the date fields used by the daily views.
- Migrations run inside an exclusive transaction where SQLite supports it. A failed migration
  leaves the previously committed version usable.
- Repositories own conversion between persistence rows and domain models. The existing PoC
  dataclasses and CLI remain supported while persistent variants are added.
- JSON exports include application version, schema version, export timestamp and data sections.
  Import validates everything before opening a write transaction.

## 8. Current blockers and deferred calibration

There are no blockers for Milestones 2-9. The existing nine private screenshots are sufficient to
preserve and regression-test the currently known 2560x1440 Traditional Chinese echo UI.

The following inputs improve later calibration but do not block V1 implementation:

- screenshots of future/new UI layouts, echo names or DPI arrangements;
- the user's final character/team roster and desired scoring weights;
- verified roll-value sets and probability evidence beyond the explicitly labelled uniform
  fallback;
- code-signing identity and installer branding for a future signed installer.
