"""Work 15 autonomous planning: signals, opportunities, dedupe, capacity, calendar.

One planning layer built ON the existing stack, not beside it:

    signals.py          TrendSignal — an observation, with its evidence
    opportunities.py    ContentOpportunity — scored, with measured/inferred split
    dedup.py            NEW / RELATED / DUPLICATE / SATURATED, with a series exception
    capacity.py         ProductionCapacity — refuse impossible workloads
    calendar.py         placement that writes the EXISTING ScheduleEntry store
    autonomy.py         DISABLED / RECOMMEND / APPROVAL / AUTONOMOUS
    engine.py           ContentPlanningEngine — the orchestrator
    orchestration.py    trend -> campaign, idempotent and resumable
    feedback.py         measured outcomes, and lessons that need a sample

Three invariants the whole package is built to hold, each with tests that fail
if it is broken:

1. **No fabricated metrics.** A signal with one observation has no velocity; an
   opportunity factor with no data contributes 0 and says so; a lesson needs
   MIN_SAMPLE measured items and a real effect size. Nothing claims virality,
   revenue, or a success probability.
2. **One scheduler.** The planner decides *when*; the existing Scheduler agent
   owns dispatch, and ``ScheduleEntry`` remains the only schedule store.
3. **Planning autonomy is not publishing autonomy.** ``assert_may_advance``
   refuses ``PUBLISH`` at every mode, unconditionally.

A note on units, because it is the easiest thing to get wrong here: the
capacity columns are RATES (``shorts_per_day``, ``longform_per_week``) and are
scaled to the planning horizon before being compared against a committed COUNT.
A pool left unset is UNBOUNDED, not zero.

Reused, never rebuilt: GlobalMemory (Work 10), Opportunity/Campaign/ScheduleEntry
(Work 05/09), the research agent, the publication approval path, Work 11.5
budget enforcement, and the Work 14 platform profiles.
"""
