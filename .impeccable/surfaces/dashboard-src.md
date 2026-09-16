---
version: 1
slug: "dashboard-src"
primary_target: "dashboard/src"
related_targets: ["dashboard/src/pages/Overview.tsx","dashboard/src/pages/Datasets.tsx","dashboard/src/pages/Submit.tsx","dashboard/src/pages/JobDetail.tsx"]
---

Scope: the whole dashboard — app shell plus Overview, Datasets, Submit, Job
detail, Jobs list, Nodes. Visitor mode: **Operate**.

Audience: the dataset owner with no GPU is primary; the operator is served by
the same surfaces at greater depth. Task: hand over a dataset, watch strangers'
machines train on it, collect the model. Constraints: WCAG 2.1 AA is the target;
the anti-fabrication law governs pixels, so an unmeasured value never renders as
a number; the full audit trail (scheduling decisions, lease history, per-epoch
metrics, raw logs) may move behind disclosure but must never be removed.

## Direction contract

THESIS: A borrowed-compute control room that reads as a consumer product, not an
operator console. The surface owns one idea — *the primary path is upload, train,
collect, and everything else is available rather than present*. It refuses the
arrangement this category always ships: a dark left-sidebar shell with gradient
stat tiles, a glow accent, and equal visual weight given to fleet internals and
to the one action the visitor came for.

OWN-WORLD: Light ground with a cool tint (off-white drifting to pale blue-gray),
borderless panels on generous whitespace separated by a single soft shadow and
no visible card border. Top horizontal pill navigation, active item a solid
ink pill. The primary action is ink; the single accent is spent on focus, live
measurement and chart lines, never on the action; a four-role state palette (ok / caution / fault /
no-signal) where no-signal is visibly not a value. Large tabular numerals as the
hero element with tiny uppercase-tracked labels beneath, no chrome around them.
Type: Geist for UI with tabular figures, Geist Mono for identifiers, hashes and
log output. Radius 14–20px, borders reserved for tables and inputs.
Recognizable with all content removed by: pill nav, borderless soft-shadow
panels, oversized numerals, cool-tinted ground.

STORY: The visitor understands within one viewport that their dataset is being
trained on machines they do not own and that the run is real; believes it
because every figure shows what measured it and every gap admits itself; and
acts by uploading an archive or collecting a finished model.

FIRST VIEWPORT: Top pill nav across the full width, product name at left, account
at right. Below it a single line of oversized numerals — machines online, runs in
flight, models ready — inline, label under number, no cards. Beneath that a
two-thirds/one-third split: left, the visitor's most recent run as one wide panel
with its live accuracy curve and a primary "Collect model" action at the panel's
right edge; right, a stacked column of the fleet's machines as compact rows with
state dots. The primary action for a visitor with no runs yet is a single
"Upload a dataset" button in that left panel's place, not a sidebar link.

FORM: The standing exit — the category canon, played straight at Stripe/Resend
craft level — taken deliberately by the owner over the roll's assigned grounded
direction (Bench Instrument, index 4 of seven). Seed key 8c98ab03, scope
direction, mode operate. Evidence for the idiom is real and named: Nixtio's
Crextio HR dashboard (3.3k likes) and Phenomenon Studio's LoopAI CRM (1.5k),
which independently share tinted-light ground, top pill nav, borderless panels
and oversized numerals.

Signature interaction: a run panel that stays live — the accuracy curve extends
and the epoch counter advances in place, without a layout shift and without a
spinner, because the data is arriving rather than loading. Motion grammar: one
authored moment (the settle) plus state-change confirmations, exponential
ease-out from an already-visible default, reaching past transform and opacity
to clip-path where it stays smooth, honoring prefers-reduced-motion; motion
never decorates.

FINISH: unreviewed and undocumented is unfinished; this build ends with the
finish review, the verdict, DESIGN.md, and every shipping raster carrying its
provenance

## Unresolved

- The volunteer-facing surface (PRODUCT.md records it as a first-class audience
  with no surface) is out of this scope and still owed.
- Light-only for now; the token system is authored so a dark theme is possible
  later without restructuring, but no dark theme is shipped or promised.
