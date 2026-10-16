# Character cultivation records

Milestone 6 adds local, user-maintained character cultivation records. It deliberately does not
import tier lists, inspect the game account, share equipment between characters, or infer missing
values. Every value shown in this area is either entered by the user or linked from a confirmed
local echo record.

## Record ownership

All character, weapon, team, skill and echo-slot records belong to one data set. Application
services reject links across data sets and commit each user action in a short transaction.
Archived characters remain available for history and can be restored; they are excluded from the
active roster by default.

One character record contains:

- name, element, role, active-roster flag and user priority;
- level, resonance chain, scoring profile, cultivation goal and notes;
- one independently assigned weapon, including level and resonance rank;
- five editable skill rows with per-character current and target levels;
- ten independently tracked small stat nodes;
- five permanent echo positions; and
- zero or more team memberships.

New characters start with five conventional skill rows. Core skills target level 10 and secondary
skills target level 6 by default, but every target is editable. The defaults are workflow aids, not
claims about a particular character.

## Permanent echo rule

An active echo may occupy at most one position from 1 through 5. Assigning it to a character is an
explicit user action. The database prevents two active echoes from occupying the same character
position, and the application service prevents an echo from being silently moved or replaced.
Replacement therefore requires an explicit replace operation. This implements the product rule
that characters keep independent permanent equipment rather than sharing one logical piece.

An empty slot, an echo with an unconfirmed score, or an echo below the character's passing
threshold remains visible as incomplete. A known set or main-stat label alone is not enough: the
user must mark it as matching the character's goal, or configure an explicit per-slot target. A
manual confirmation is stored together with that Echo's local identifier, so replacing the Echo
returns the new piece to "needs confirmation" instead of inheriting the old decision.
Scores only receive credit when their scoring-profile identifier and version match the character's
effective profile. OCR history and its correction audit remain intact when equipment links change.
Consumed echoes are archived when the material event is confirmed, and both candidate listing and
assignment reject older consumed rows defensively.

## Completion calculation

Completion is a transparent progress indicator, not a combat simulator or external tier rating.
It evaluates independently:

1. character level against its configured goal;
2. weapon level and resonance-rank goals;
3. each skill's current level against its own target;
4. completed small nodes out of the configured total; and
5. five permanent echo positions, including basic validity and the selected scoring profile's
   passing threshold.

The domain result exposes section percentages and typed blocking reasons. The character page shows separate level, weapon and skill progress instead of an overall completion percentage. Echo scores and replacement targets belong to the separate equipment workspace. Unknown values never receive completion credit. The calculation is pure and
deterministic so the breadth-first planner in Milestone 7 can reuse the richer result without
importing Qt or SQLAlchemy.

## Teams and seed data

Teams are user-owned labels with ordered members and optional roles. Any future demonstration
teams are stored in a separate demo data set and labelled non-authoritative. The application does
not fetch or overwrite teams from an online tier list.

## Desktop workflow (1.0.22)

The Character sidebar entry opens cultivation directly. A scrollable portrait rail is on the right;
its expansion shows exactly three columns. The default view summarizes identity, level and weapon,
while the five skill rows and ten nodes remain directly editable. Save persists these manual edits.
Rarely changed fields appear only after choosing Edit character data; hiding the editor keeps the
current draft. Character numeric and skill editors ignore wheel changes to prevent accidental edits.

Recognition emits character changes to the existing presenter. Its external refresh updates equipment
without replacing unsaved skill/weapon/character drafts. Strong recognized equipped-owner evidence
can register a new name; unknown character guides remain unscored until separately supplied. Catalog,
manual-add and refresh actions are no longer shown in the daily workspace.

The Echo sidebar entry contains the equipment overview and inspection. Complete equipment totals
are out of 500; individual scores are out of 100. Missing observations stay unknown. Character mode
supports total ordering and cultivation filters. Weakest-piece mode orders all reliably scored equipped
pieces ascending in a compact responsive grid and does not inherit hidden character filters. Score colors flag unmet character
requirements and exceptional scores; the display tier at 90 does not change the scoring engine.
Inspection shares priority stats and optional effective weights once per character. Each piece keeps
its replacement action and observed substats. No score formula, data model or raw OCR history changes.

Mouse button four and Alt+Left navigate back through the local workspace routes, including the chosen
equipment position after starting a replacement. Mouse back in an owned dialog closes that dialog
first. These controls only affect this application's UI and do not send input to the game.

## Skill order and recommended energy (1.0.26)

The skill table no longer asks for a core checkbox. Target levels express the intended investment;
before a custom order is saved, higher targets appear first. Select a row and use 提前升級 / 延後升級,
then 儲存角色. The existing skill display_order persists that order; 養成規劃 uses the same order
and omits completed skills. No schema migration or automatic rewriting of existing character data occurs.

Echo explanations and character details show the researched guide's recommended energy regeneration.
Routine editing no longer asks for measured panel energy. Existing manual values are preserved for
compatibility; an absent measurement stays unknown, never replaced with the recommendation as if
it had been measured. Recommended values do not claim the current equipment meets the target.

## Set-name recognition compatibility (1.0.23)

Lucy and Rebecca retain their existing unordered 1+2+2 set-count rules. Older local observations
sometimes contain 碎夢亡鬼之魔 where the source guide names 碎夢亡鬼之魘. An exact alias scoped to
these two guides maps that observed spelling to the canonical name before set-fit/count evaluation.
The original observations and confidence are unchanged. This does not accept arbitrary similar names,
missing equipment, or wrong set counts; slot ordering never changes set completion.

The global theme now uses a dark charcoal surface with pale purple accents and readable status colors.
