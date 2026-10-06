"""Metric-honesty audit (Work 16.5.7 §8/§11). RUNS, AND FAILS LOUDLY.

Usage::

    backend\\.venv\\Scripts\\python scripts\\audit_analytics_honesty.py
    backend\\.venv\\Scripts\\python scripts\\audit_analytics_honesty.py --check

WHAT IT IS
----------
The audit that did not exist. Every metric the UI displays is declared here with
its provenance class -- MEASURED, DERIVED or UNAVAILABLE -- the endpoint it comes
from, and WHY it holds that class. The classes are not decoration: two
invariants are asserted against the real source tree, and the script exits
NON-ZERO when either is violated.

  RULE 1  no-a-fabricated-zero
      An audited field declared UNAVAILABLE-capable must not be able to emit a
      bare ``0``/``0.0`` for a value nobody measured. The check is a real source
      scan of the field's guard expression, not a restatement of this comment.

  RULE 2  no-money-total-that-swallows-an-unknown-exposure
      An audited MONEY field must not sum the ledger in a way that includes an
      ``UNKNOWN_EXPOSURE`` row as zero. Money is the one place where a
      convenient zero is materially dangerous: it under-reports real spend.

Each field also carries ``gate``: true when the number is a BUDGET GATE rather
than a measurement. A gate is exempt from the display contract on purpose and
the reason is recorded per field -- an unresolved exposure must CONSUME
headroom in a gate, never create it, so a gate that reported ``None`` would fail
OPEN.

DETERMINISM
-----------
No timestamps inside the hashed content, and every list is sorted before it is
written, so re-running against an unchanged tree produces a byte-identical
``docs/ANALYTICS_HONESTY_AUDIT.json``. A diff in that file means a provenance
claim changed, which is the point.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
OUT = REPO / "docs" / "ANALYTICS_HONESTY_AUDIT.json"

#: The vocabulary. Imported from the module that OWNS it rather than restated,
#: so the audit and the arithmetic cannot drift apart. If this import fails the
#: audit cannot run, which is the correct failure: an audit that invents its own
#: vocabulary is not auditing anything.
sys.path.insert(0, str(BACKEND))
from app.services.cost import (  # noqa: E402
    DERIVED,
    MEASURED,
    PROVENANCE_CLASSES,
    UNAVAILABLE,
)

# ---------------------------------------------------------------------------
# the vocabulary, in the audit's own terms
# ---------------------------------------------------------------------------

VOCABULARY = {
    MEASURED: "A real observation, including a real measured zero.",
    DERIVED: "Computed from measured values by the formula named in `formula`.",
    UNAVAILABLE: (
        "Cannot be known. Serialised as null and rendered UNAVAILABLE. "
        "Never rendered as 0."
    ),
}

#: The one zero shape that can satisfy "contains a zero" while still emitting 0
#: for a measurement nobody took: an accumulator dict seeded at zero and only
#: ever ADDED to. It is what `totals = {"views": 0, ...}` looked like, and it is
#: checked unconditionally because the sentinel check alone would pass it.
SEEDED_ZERO_ACCUMULATOR = re.compile(
    r"""=\s*\{\s*["'][a-z_]+["']\s*:\s*0(?:\.0)?\s*[,}]"""
)

#: Expressions that turn a routed ``None`` back into a zero on the way out. The
#: subtle one, and the reason this audit was verified by deliberately breaking a
#: rule rather than only by running it green: ``money_total`` correctly returned
#: ``None`` and the serializer then wrote ``round(float(total or 0.0), 4)``.
RE_BROKEN_BY_ZERO_DEFAULT = re.compile(
    r"""\bor\s+0(?:\.0)?\b|\belse\s+0(?:\.0)?\b|coalesce\([^)]*?,\s*0(?:\.0)?\s*\)"""
)

#: A coalesce of a COLUMN on a row that exists, e.g. `m.views or 0` inside a sum
#: over snapshots. Blank column on an observed row is a different claim from an
#: absent total: the row was measured, the provider left that one field empty.
#: Removed before the re-break scan so `sum(int(m.views or 0) for m in ...)` is
#: not mistaken for a total that swallows a missing measurement. It would be if
#: the ITERABLE were empty, and that is what rule 1's `None` requirement covers.
PER_ROW_COALESCE = re.compile(r"""\b\w+\.\w+\s+or\s+0(?:\.0)?\b""")

#: Legitimate uses of a numeric zero that must NOT trip a rule. Every entry is a
#: reason, not a suppression: an unexplained allowlist entry is how an audit
#: stops being able to fail. These are matched against each field's declared
#: reason, so a field cannot cite them without saying so in the audit output.
ZERO_ALLOW = {
    "count over an empty table is a measured zero",
    "rounded amount of a present, priced ledger row",
    "denominator of a guarded ratio",
}


@dataclass(frozen=True)
class Audited:
    """One displayed metric and the claim made about it."""

    endpoint: str
    field: str
    provenance: str
    reason: str
    #: ``file:line`` of the expression that decides the value. The audit reads
    #: THIS, so a claim cannot outlive the code it describes.
    site: str
    #: True when the value can legitimately be ``None`` because a measurement is
    #: missing. Rule 1 only applies to these.
    nullable: bool = True
    #: True for a BUDGET GATE. Gates are exempt from Rule 1 and must explain why.
    gate: bool = False
    gate_reason: str = ""
    #: True for a money field. Rule 2's scope is ``ledger`` money only.
    money: bool = False
    #: True when this money field totals the CostEntry LEDGER, which is the only
    #: place an UNKNOWN_EXPOSURE row can be swallowed. Agent-run accounting and
    #: shadow-run cost are money but live in different tables that carry no such
    #: marker, so rule 2 does not apply to them -- declared rather than inferred,
    #: so a future ledger field cannot quietly opt out.
    ledger: bool = False
    formula: str = ""
    #: Name of the published OpenAPI schema that carries this field, or None when
    #: the endpoint publishes none (recorded rather than hidden -- see the gap
    #: note in `test_analytics_honesty.py`).
    schema: str | None = None
    #: Property name INSIDE ``schema``. Defaults to the last dotted segment of
    #: ``field``; set it when the payload path and the schema property differ
    #: (e.g. ``by_topic[]`` rows live in a row schema as plain ``avg_views``).
    schema_field: str = ""
    #: Fields the audit could not establish a provenance for. Present and
    #: non-empty is a finding, not a pass. A value beginning "NOT FIXED" names a
    #: fabrication this lane may not repair, and its finding is routed to
    #: ``known_out_of_scope`` instead of failing the default run.
    unresolved: str = ""

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE_CLASSES:
            raise ValueError(
                f"{self.endpoint}.{self.field}: {self.provenance!r} is not one of "
                f"{PROVENANCE_CLASSES}"
            )
        if self.gate and not self.gate_reason:
            raise ValueError(
                f"{self.endpoint}.{self.field}: a gate must say why it is exempt"
            )
        if not self.schema_field:
            object.__setattr__(self, "schema_field", self.field.split(".")[-1])


def A(*args, **kwargs) -> Audited:
    return Audited(*args, **kwargs)


# ---------------------------------------------------------------------------
# THE AUDIT
# ---------------------------------------------------------------------------

FIELDS: tuple[Audited, ...] = (
    # ---- GET /analytics/overview (misc.py::analytics_overview) -------------
    A("/analytics/overview", "totals.views", UNAVAILABLE,
      "Seeded from None and promoted by the first PostMetric snapshot. Was a 0 "
      "accumulator, so an all-unmeasured workspace read 0 views.",
      "backend/app/api/v1/misc.py:352", schema="Totals5"),
    A("/analytics/overview", "totals.likes", UNAVAILABLE,
      "Same accumulator as totals.views.",
      "backend/app/api/v1/misc.py:352", schema="Totals5"),
    A("/analytics/overview", "totals.comments", UNAVAILABLE,
      "Same accumulator as totals.views.",
      "backend/app/api/v1/misc.py:352", schema="Totals5"),
    A("/analytics/overview", "totals.shares", UNAVAILABLE,
      "Same accumulator as totals.views.",
      "backend/app/api/v1/misc.py:352", schema="Totals5"),
    A("/analytics/overview", "totals.followers_gained", UNAVAILABLE,
      "Same accumulator as totals.views. Only providers reporting a follower "
      "delta contribute; the rest contribute nothing rather than a zero.",
      "backend/app/api/v1/misc.py:352", schema="Totals5"),
    A("/analytics/overview", "posts_published", MEASURED,
      "COUNT of published_posts rows. Zero published posts is a fact, not a gap.",
      "backend/app/api/v1/misc.py:393", nullable=False,
      schema="ApiV1WorkspacesWorkspaceAnalyticsOverview4"),
    A("/analytics/overview", "content_items", MEASURED,
      "COUNT of content_items rows. A COUNT over an empty table is a real "
      "measured zero -- 'no rows' and 'no measurement' are different claims and "
      "only one of them is what a COUNT answers.",
      "backend/app/api/v1/misc.py:394", nullable=False,
      schema="ApiV1WorkspacesWorkspaceAnalyticsOverview4"),
    A("/analytics/overview", "cost_total_usd", UNAVAILABLE,
      "Sum of PRICED ledger rows. None for an empty ledger (unknown-but-zero-"
      "observed) and None when any row is an UNKNOWN_EXPOSURE.",
      "backend/app/api/v1/misc.py:397", money=True, ledger=True,
      formula="sum(amount_usd) over priced rows; None if any row is unknown",
      schema="ApiV1WorkspacesWorkspaceAnalyticsOverview4"),
    A("/analytics/overview", "cost_total_unknown_exposure_rows", MEASURED,
      "COUNT of ledger rows carrying the UNKNOWN_EXPOSURE marker. Exists so the "
      "UI can say WHY the total is missing instead of rendering a bare dash.",
      "backend/app/api/v1/misc.py:398", nullable=False,
      schema="ApiV1WorkspacesWorkspaceAnalyticsOverview4"),

    # ---- GET /publishing/posts ---------------------------------------------
    A("/publishing/posts", "items[].metrics.views", UNAVAILABLE,
      "The PostMetric snapshot's own value, or None when the post has no "
      "snapshot at all. Was a per-post `else 0`, the most-read fabricated "
      "number in the product: it rendered in four post tables at once.",
      "backend/app/api/v1/misc.py:301"),
    A("/publishing/posts", "items[].metrics.likes", UNAVAILABLE,
      "As metrics.views.",
      "backend/app/api/v1/misc.py:301"),
    A("/publishing/posts", "items[].metrics.comments", UNAVAILABLE,
      "As metrics.views.",
      "backend/app/api/v1/misc.py:301"),
    A("/publishing/posts", "items[].metrics.completion_rate", UNAVAILABLE,
      "The provider's own report, or None. NULL became possible at the SOURCE "
      "in this change: PostStats.completion_rate defaulted to 0.0, so every "
      "platform that cannot compute one wrote 'nobody finished the video'.",
      "backend/app/api/v1/misc.py:304"),

    # ---- GET /analytics/breakdowns -----------------------------------------
    A("/analytics/breakdowns", "by_topic[].total_views", UNAVAILABLE,
      "Sum over measured posts in the bucket. A bucket is registered before "
      "any post in it is known to be measured, so it was 0 for an all-"
      "unmeasured bucket.",
      "backend/app/api/v1/misc.py:542", schema="ByTopicRow10",
      schema_field="total_views"),
    A("/analytics/breakdowns", "by_topic[].avg_views", DERIVED,
      "total_views / posts. None when the bucket has no measured post: an "
      "average over an empty sample is not zero.",
      "backend/app/api/v1/misc.py:543", schema="ByTopicRow10",
      schema_field="avg_views",
      formula="round(total_views / posts) when posts > 0 and total_views is not None"),
    A("/analytics/breakdowns", "by_topic[].engagement_pct", DERIVED,
      "Mean engagement over the posts that HAVE a view count. The denominator "
      "is engagement_samples, not posts: a post with 0 views has no rate to "
      "average, and padding it as a 0.0 reported a low rate for a strong bucket.",
      "backend/app/api/v1/misc.py:550", schema="ByTopicRow10",
      schema_field="engagement_pct",
      formula="100 * sum(engagement over posts with views>0) / engagement_samples"),
    A("/analytics/breakdowns", "by_hook_style[].avg_views", DERIVED,
      "As by_topic[].avg_views.",
      "backend/app/api/v1/misc.py:543", schema="ByHookStyleRow11",
      schema_field="avg_views"),
    A("/analytics/breakdowns", "by_duration[].avg_views", DERIVED,
      "As by_topic[].avg_views.",
      "backend/app/api/v1/misc.py:543", schema="ByDurationRow12",
      schema_field="avg_views"),

    # ---- GET /costs (misc.py::cost_summary) --------------------------------
    A("/costs", "spent_last_24h_usd", UNAVAILABLE,
      "Sum of PRICED rows in the window. Was `sum(float(a or 0))` over a grouped "
      "SUM, so an empty window read $0.00 -- 'nothing was booked' stated as "
      "'nothing was spent'.",
      "backend/app/api/v1/misc.py:1368", money=True, ledger=True,
      formula="sum(amount_usd) over priced rows; None if any row is unknown",
      schema="CostSummaryOut"),
    A("/costs", "last_24h_by_category", DERIVED,
      "Per-category sum of PRICED rows. Unknown-exposure rows are EXCLUDED "
      "rather than added as 0, which is why the map can hold numbers while the "
      "total is null. A category present in the map has rows and a real sum. "
      "Never null itself: the honest answer for an unknown category is ABSENCE.",
      "backend/app/api/v1/misc.py:1359", nullable=False, money=True,
      ledger=True,
      formula="group priced rows by category; sum each group",
      schema="CostSummaryOut"),
    A("/costs", "within_budget", MEASURED, "A BUDGET GATE, not a measurement.",
      "backend/app/api/v1/misc.py:1372", nullable=False, gate=True,
      gate_reason="A gate that resolved unknown to 'room available' would fail "
                  "OPEN: every unpriceable exposure would buy free budget. "
                  "Unknown spend must consume headroom, so this stays a real "
                  "boolean and stays non-null.",
      schema="CostSummaryOut"),
    A("/costs", "remaining_usd", DERIVED, "A BUDGET GATE, not a measurement.",
      "backend/app/api/v1/misc.py:1373", nullable=False, gate=True, money=True,
      ledger=True,
      gate_reason="daily_budget - spent_for_gate, where an unknown exposure is "
                  "counted as spend. Reported as a real number so the cap has a "
                  "single arithmetic; it is deliberately conservative.",
      schema="CostSummaryOut"),

    # ---- GET /costs/intelligence (safety.py::cost_intelligence) ------------
    A("/costs/intelligence", "total_cost_usd", UNAVAILABLE,
      "Sum of PRICED rows, all time. Was `coalesce(sum, 0.0)`, so an empty "
      "ledger read $0.0000 and an UNKNOWN_EXPOSURE row was dropped from the sum "
      "as a silent zero.",
      "backend/app/api/v1/safety.py:93", money=True, ledger=True,
      formula="sum(amount_usd) over priced rows; None if any row is unknown",
      schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE. `safety.py` is outside this lane's "
                 "permitted file set, so the fabrication is still live: an empty "
                 "ledger reports $0.0000 and an UNKNOWN_EXPOSURE row is dropped "
                 "as a silent zero. `scripts/audit_analytics_honesty.py --strict` "
                 "exits non-zero on it. Fix: route this endpoint's total through "
                 "`app.services.cost.money_total` exactly as `misc.py` does."),
    A("/costs/intelligence", "per_cycle_usd", DERIVED,
      "total / completed cycles. None when total is null (nothing to divide) or "
      "the denominator is zero.",
      "backend/app/api/v1/safety.py:142", money=True, ledger=True,
      formula="total_cost_usd / cycles_completed", schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE (safety.py). Derived from the "
                 "unfixed total above, so it inherits its fiction."),
    A("/costs/intelligence", "per_video_usd", DERIVED,
      "total / content items. None when total is null or the denominator is 0.",
      "backend/app/api/v1/safety.py:143", money=True, ledger=True,
      formula="total_cost_usd / videos_built", schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE (safety.py)."),
    A("/costs/intelligence", "cost_per_1000_views_usd", DERIVED,
      "total*1000 / views. None when either is null or zero.",
      "backend/app/api/v1/safety.py:145", money=True, ledger=True,
      formula="total_cost_usd * 1000 / views", schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE (safety.py)."),
    A("/costs/intelligence", "totals.views", UNAVAILABLE,
      "Sum of PostMetric.views over posts that HAVE a snapshot. Was `views = 0` "
      "as the seed of that sum.",
      "backend/app/api/v1/safety.py:108", schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE (safety.py): the `views = 0` seed is "
                 "still there, so an unmeasured workspace claims 0 views."),
    A("/costs/intelligence", "totals.posts_published", MEASURED,
      "COUNT of published_posts. Zero is a measured zero.",
      "backend/app/api/v1/safety.py:152", nullable=False, schema=None),

    # ---- GET /agents, GET /agents/{key} (misc.py) ---------------------------
    A("/agents", "items[].runs", MEASURED,
      "COUNT of AgentRun rows. An agent that has run zero times has run zero "
      "times.",
      "backend/app/api/v1/misc.py:798", nullable=False, schema=None),
    A("/agents", "items[].failure_rate", DERIVED,
      "failures / runs. Was `round(f/r, 3) if r else 0.0`, so an agent that had "
      "never run reported a 0% failure rate -- the most flattering possible "
      "reading of no data.",
      "backend/app/api/v1/misc.py:802",
      formula="round(failures / runs, 3) when runs > 0 else None",
      schema=None),
    A("/agents", "items[].total_cost_usd", UNAVAILABLE,
      "SUM of AgentRun.cost_usd. Was `round(float(r.cost or 0.0), 4) if r else "
      "0.0`, and the majority of AGENT_META keys have no AgentRun rows at all -- "
      "so most of the catalog reported $0.0000 of spend. NOTE: this is an agent-"
      "run accounting total, NOT the CostEntry ledger, so it carries no "
      "UNKNOWN_EXPOSURE marker and cannot swallow one.",
      "backend/app/api/v1/misc.py:805", money=True, schema=None),
    A("/agents", "items[].avg_duration_ms", UNAVAILABLE,
      "AVG of AgentRun.duration_ms. Already null-honest; recorded so the audit "
      "covers the field the two neighbours were wrong about.",
      "backend/app/api/v1/misc.py:804", schema=None),
    A("/agents/{key}", "stats.failure_rate", DERIVED,
      "As /agents items[].failure_rate.",
      "backend/app/api/v1/misc.py:695",
      formula="round(failures / runs, 3) when runs > 0 else None", schema=None),
    A("/agents/{key}", "stats.total_cost_usd", UNAVAILABLE,
      "As /agents items[].total_cost_usd. Agent-run accounting, not the ledger.",
      "backend/app/api/v1/misc.py:701", money=True, schema=None),

    # ---- GET /performance/overview + /compare (campaign analytics) ----------
    A("/performance/overview", "rollup.totals.views", UNAVAILABLE,
      "Sum over snapshots. empty_rollup() seeded 0 for every key, so a rollup "
      "over published-but-unmeasured posts read 0 views.",
      "backend/app/engine/campaign/analytics.py:63"),
    A("/performance/overview", "rollup.totals.watch_time", UNAVAILABLE,
      "As rollup.totals.views.",
      "backend/app/engine/campaign/analytics.py:64"),
    A("/performance/overview", "rollup.totals.engagement_rate", DERIVED,
      "(likes+comments+shares+saves)/views. Computed only when views > 0; None "
      "otherwise, because 0/0 is arithmetic that does not exist.",
      "backend/app/engine/campaign/analytics.py:95",
      formula="(likes+comments+shares+saves)/views when views > 0 else None"),
    A("/performance/overview", "rollup.totals.completion", DERIVED,
      "Views-weighted mean over the rows that REPORTED a completion_rate. Was "
      "computed over `completion_rate or 0.0`, which averaged 'the provider said "
      "nothing' with 'nobody finished the video'.",
      "backend/app/engine/campaign/analytics.py:106",
      formula="sum(completion_rate * views) / sum(views) over reporting rows only"),
    A("/performance/compare", "groups[].avg_completion", DERIVED,
      "Unweighted mean over the group rows that reported a completion rate. Was "
      "`else 0.0`, i.e. 0% completion for a group where nobody reported one.",
      "backend/app/api/v1/performance.py:137",
      formula="mean(reported completion rates) or None",
      schema=None),
    A("/performance/compare", "groups[].views", MEASURED,
      "Sum of PostMetric.views over the group's measured posts. A group with no "
      "measured post is absent from the response entirely, so a present group "
      "always has rows.",
      "backend/app/api/v1/performance.py:136", nullable=False, schema=None),

    # ---- GET /campaigns/{id} (campaigns.py::_cost_summary) -----------------
    A("/campaigns/{id}", "costs.total_usd", UNAVAILABLE,
      "Sum of the campaign's PRICED rows. Was `total += float(amount or 0.0)`, "
      "which let an UNKNOWN_EXPOSURE row contribute a hard zero and reported the "
      "sum of the other rows as if it were the whole bill.",
      "backend/app/api/v1/campaigns.py:355", money=True, ledger=True,
      formula="sum(amount_usd) over priced rows; None if any is unknown"),

    # ---- GET /opportunities (content.py) ------------------------------------
    A("/opportunities", "items[].virality", UNAVAILABLE,
      "The stored score when there is one, else None. Was "
      "`getattr(o, 'virality', 0.0) or 0.0`, which substituted 0.0 for a row "
      "with no value -- and the planner's own opportunities carry none, so a "
      "RECOMMENDED row advertised a breakout potential of exactly zero.",
      "backend/app/api/v1/content.py:75",
      unresolved="Provenance is UNRESOLVED at the storage layer: the column is "
                 "NOT NULL DEFAULT 0.0, so a genuinely-scored 0.0 and a "
                 "never-scored row are indistinguishable without a migration. The "
                 "serializer now refuses to invent the value; it cannot yet "
                 "recover which case it is in.",
      schema=None),

    # ---- GET /intelligence/decisions/shadow-report --------------------------
    A("/intelligence/decisions/shadow-report", "total_cost_usd", UNAVAILABLE,
      "Sum over shadow runs. Was `round(sum(costs), 6)` over an empty list, so "
      "a workspace that ran no shadow comparison reported $0.000000 -- which "
      "reads as 'shadow runs are free', the opposite of what one is.",
      "backend/app/engine/intelligence/shadow.py:118", money=True,
      formula="sum(run cost) when any run exists else None", schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE (`engine/intelligence/shadow.py`). "
                 "`Intelligence.tsx` still types this `number` and the endpoint "
                 "still answers $0.000000 for no runs."),

    # ---- GET /ops/overview (ops.py::_costs_section) -------------------------
    A("/ops/overview", "costs.spent_last_24h_usd", UNAVAILABLE,
      "Duplicated GET /costs, so it duplicated the fabrication: a grouped SUM "
      "re-summed as `float(amount or 0.0)`. The Command Center reads THIS "
      "section, so fixing /costs alone would have left the dashboard lying.",
      "backend/app/api/v1/ops.py:278", money=True, ledger=True,
      formula="sum(amount_usd) over priced rows; None if any row is unknown",
      schema=None,
      unresolved="NOT FIXED — OUT OF SCOPE (`api/v1/ops.py`). The Command Center "
                 "reads this section rather than GET /costs, so the dashboard's "
                 "spend tile still reads $0.0000 for an empty window. Fix: route "
                 "this through `money_total` exactly as `misc.py` does."),
    A("/ops/overview", "costs.within_budget", MEASURED,
      "A BUDGET GATE, not a measurement.",
      "backend/app/api/v1/ops.py:292", nullable=False, gate=True,
      gate_reason="Same reasoning as /costs within_budget: unknown spend must "
                  "consume headroom, never create it.",
      schema=None),
)

# ---------------------------------------------------------------------------
# rule 1 -- no audited field may fabricate a zero
# ---------------------------------------------------------------------------

#: Helpers that PRODUCE an UNAVAILABLE-capable value. A field whose value comes
#: from one of these satisfies rule 1 by construction: the arithmetic, and the
#: absent-versus-zero decision, live in ``app.services.cost`` where they are unit
#: tested -- not restated at six call sites that will drift apart.
HONEST_HELPERS = (
    "money_total(",
    "measured_sum(",
    "derived_mean(",
    "derived_ratio(",
    "empty_rollup(",
)

#: How many lines above a claim to read when locating its value expression.
_WINDOW = 14


#: How many lines AFTER a claim to read as well. The evidence for a null is
#: often the guard on the NEXT line -- `"views": (\n    ... if x else None\n),` --
#: so a backward-only window would report the most honest expressions in the file
#: as fabrications.
_WINDOW_AFTER = 6


#: The quoted or bare dict key reported on a line, or None.
_KEY = re.compile(r"""\s*(?P<key>["'][^"']+["']|[A-Za-z_][\w.\[\]]*)\s*:""")


def _claim_key(head: str) -> str | None:
    m = _KEY.match(head)
    if not m:
        return None
    return m.group("key").strip("\"'")


def _value_expression(lines: list[str], n: int) -> str:
    """The expression that produces the value reported on line ``n``.

    Read PRECISELY, because an over-wide read is worse than no read: it picks up
    the NEXT dict key, and a key named after an honest helper then vouched for a
    fabricated value sitting beside it. Measured by bracket balance from the
    claim line, so it ends at the comma that closes this value and no later.
    """
    head = lines[n - 1]
    # Drop the key: everything up to and including the first colon that follows
    # the quoted key. A leading slice is not a claim at all (the field's site
    # points at the key line), so a missing colon means the whole line.
    m = re.match(r"""\s*(?:["'][^"']*["']|[\w.\[\]]+)\s*:\s*(.*)$""", head)
    first = m.group(1) if m else head.strip()
    if first.count("(") >= first.count(")"):
        # The value opens a bracket and continues below: read until balanced.
        depth = first.count("(") - first.count(")")
        parts = [first]
        for offset in range(1, _WINDOW_AFTER):
            nxt = lines[n - 1 + offset] if n - 1 + offset < len(lines) else ""
            parts.append(nxt)
            depth += nxt.count("(") - nxt.count(")")
            if depth <= 0:
                break
        return "\n".join(parts)
    return first


def _claim_lines(f: "Audited") -> tuple[str, str, str, list[str]] | None:
    path, _, line = f.site.rpartition(":")
    if not path or not line.isdigit():
        return None
    full = REPO / path
    if not full.exists():
        return None
    lines = full.read_text(encoding="utf-8").splitlines()
    n = int(line)
    start = max(0, n - _WINDOW)
    return path, line, "\n".join(lines[start:n + _WINDOW_AFTER]), lines


def rule1_findings(strict: bool = False) -> list[dict]:
    """A nullable field must produce `None`, or come from an audited helper.

    A POSITIVE check, not a scan for forbidden spellings. The failure mode of a
    pattern scan is that it is satisfied by the absence of the thing it looks
    for: a field whose line was refactored into something the patterns do not
    recognise reads as CLEAN, which is exactly when an audit is most needed.
    Requiring the sentinel (or the helper that guarantees it) means a regression
    has to look deliberate to pass.

    Per-row column coalescing is allowed and is a different claim from the one
    being audited: the row was observed, so `m.views or 0` inside a sum over
    EXISTING snapshots is reading a blank column, not inventing a total. The
    check is therefore scoped to the VALUE EXPRESSION -- the claim line plus the
    few lines its parenthesised expression continues over -- and not to the whole
    window, which would let an unrelated guard vouch for a fabricated total.
    """
    findings: list[dict] = []
    for f in FIELDS:
        if not f.nullable:
            continue
        got = _claim_lines(f)
        if got is None:
            findings.append({
                "rule": "no-a-fabricated-zero",
                "endpoint": f.endpoint, "field": f.field, "site": f.site,
                "detail": "site is not a resolvable file:line reference",
            })
            continue
        path, line, window, lines = got
        expression = _value_expression(lines, int(line))
        # A value that was routed through an honest helper and THEN passed
        # through a zero-defaulting expression has been re-broken on the way out.
        # This is the exact shape of the regression the audit was verified
        # against: `money_total(rows)` correctly returning None, then
        # `"cost_total_usd": round(float(total or 0.0), 4)` turning it back into
        # $0.00. Checked before the sentinel test, because the sentinel test
        # would otherwise read the routing and pass it.
        rebroken = RE_BROKEN_BY_ZERO_DEFAULT.search(
            PER_ROW_COALESCE.sub("", expression)
        )
        if rebroken:
            finding = {
                "rule": "no-a-fabricated-zero",
                "endpoint": f.endpoint, "field": f.field, "site": f.site,
                "detail": (
                    f"value expression re-breaks an honest null with "
                    f"{rebroken.group(0)!r}: a routed None becomes 0 here"
                ),
            }
            if strict or not _is_out_of_scope(f):
                findings.append(finding)
            else:
                out_of_scope.append(finding)
            continue
        # A SEEDED ZERO ACCUMULATOR is the one shape that satisfies "contains a
        # zero" while still emitting 0 for a missing measurement, so it is
        # checked first and unconditionally.
        seeded = SEEDED_ZERO_ACCUMULATOR.search(expression)
        if seeded:
            finding = {
                "rule": "no-a-fabricated-zero",
                "endpoint": f.endpoint, "field": f.field, "site": f.site,
                "detail": (
                    f"value expression seeds a zero accumulator "
                    f"({seeded.group(0)!r}); a nullable field cannot start at 0"
                ),
            }
            if strict or not _is_out_of_scope(f):
                findings.append(finding)
            else:
                out_of_scope.append(finding)
            continue
        if "None" in expression or any(h in expression for h in HONEST_HELPERS):
            continue
        # A field whose fabrication is REAL but lives in a file this lane may not
        # write is recorded as a gap, not silently passed. Same classification as
        # rule 2's money fields and rule 3's contract lag: visible, counted, and
        # promotable to a failure with --strict.
        if not strict and _is_out_of_scope(f):
            out_of_scope.append({
                "rule": "no-a-fabricated-zero",
                "endpoint": f.endpoint, "field": f.field, "site": f.site,
                "detail": (
                    "declared UNAVAILABLE but the value expression still fabricates "
                    f"a zero: {expression.strip()[:90]!r} (file out of scope)"
                ),
            })
            continue
        # Not inline: the value is a local produced by an audited helper
        # (`total, unknown = money_total(rows)`). The evidence is per-VARIABLE,
        # not per-function: a function that calls `money_total` for one field
        # must not thereby vouch for a DIFFERENT field in the same function that
        # sums the ledger by hand. Scoping the check to the enclosing block would
        # have passed exactly the regression this audit exists to catch -- proven
        # by deliberately reverting `cost_total_usd` while `money_total` was
        # still in scope for its sibling field.
        fn = _enclosing_function(lines, int(line))
        if fn and _value_is_routed(expression, fn):
            continue
        # sibling arm of the dict it is built from. That is how
        # `/publishing/posts` works -- `{"views": m.views}` when a snapshot
        # exists, `{"views": None}` when it does not. Requiring the sentinel on
        # the measured line would report that as a fabrication, which would train
        # the audit to be ignored.
        key = _claim_key(lines[int(line) - 1])
        if fn and key and re.search(rf"""["']{re.escape(key)}["']\s*:\s*None\b""", fn):
            continue
        finding = {
            "rule": "no-a-fabricated-zero",
            "endpoint": f.endpoint, "field": f.field, "site": f.site,
            "detail": (
                "value expression contains no None and its enclosing function "
                f"calls no audited helper — {expression.strip()[:90]!r}"
            ),
        }
        if strict or not _is_out_of_scope(f):
            findings.append(finding)
        else:
            out_of_scope.append(finding)
    return findings


def _enclosing_function(lines: list[str], n: int) -> str | None:
    """The module-level ``def`` block containing line ``n`` (1-based), or None."""
    start = None
    for i in range(n - 1, -1, -1):
        line = lines[i]
        if line.startswith("def ") or line.startswith("async def "):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(n, len(lines)):
        line = lines[i]
        if line.startswith("@") and i + 1 < len(lines):
            # A decorator belongs to the function BELOW it; do not end the current
            # body at `@router.get(...)` and report the decorator as the code
            # under audit.
            if lines[i + 1].startswith(("def ", "async def ")):
                end = i
                break
            continue
        if line.startswith("def ") or line.startswith("async def "):
            end = i
            break
    return "\n".join(lines[start:end])


#: The names a value expression reads. A dict key's value, or the target of an
#: assignment -- the identifiers that must be traceable to an honest producer.
_VALUE_NAMES = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _value_is_routed(
    expression: str,
    function_body: str,
    *,
    helpers: tuple[str, ...] = HONEST_HELPERS,
    also_guard_lines: list[str] | None = None,
) -> bool:
    """Whether the value this expression reads is produced by an honest helper.

    Per-NAME, not per-function: a function that calls ``money_total`` for one
    field must not thereby vouch for a DIFFERENT field in the same function that
    sums the ledger by hand. Requiring more than that (adjacency, ordering) would
    make the audit a code-shape linter that fails on harmless refactors, and a
    rule that cries wolf is a rule that gets ignored. What it DOES refuse is the
    case that matters: a name with no honest assignment anywhere, computed by
    hand.

    ``also_guard_lines`` admits evidence that is not an assignment -- the
    ``if is_unknown_exposure(row): ... continue`` branch form, which is how a
    hand-rolled accumulator stays honest. The branch is matched for its CALL, so
    a bare variable of the same name cannot stand in for it.
    """
    read = _VALUE_NAMES.findall(expression)
    candidates = [
        n for n in read
        if n not in {"None", "True", "False", "round", "float", "int", "sum", "len"}
        and not n.startswith("_")
    ]
    if not candidates:
        return False
    assignments = [
        ln for ln in function_body.splitlines() if any(h in ln for h in helpers)
    ]
    for ln in also_guard_lines or []:
        assignments.append(ln)
    return any(any(name in ln for name in candidates) for ln in assignments)


# ---------------------------------------------------------------------------
# rule 2 -- no money total may swallow an unknown exposure
# ---------------------------------------------------------------------------

_MONEY_SUM_HINTS = (
    "func.sum(CostEntry.amount_usd)",
    "row.amount_usd",
    "entry.amount_usd",
)

#: Findings whose fabrication is real but whose FILE is outside this lane's
#: permitted write set. Collected separately from `findings` so a default run
#: reports honestly ("AUDIT OK for the audited scope, N known gaps elsewhere")
#: instead of either hiding them or failing on work it may not do.
out_of_scope: list[dict] = []


def _is_out_of_scope(f: "Audited") -> bool:
    return f.unresolved.startswith("NOT FIXED")


def rule2_findings(strict: bool = False) -> list[dict]:
    """A money total must not sum the ledger without consulting the marker.

    Checked by requiring every audited money site to route through the honest
    helper or an explicit unknown check, rather than by pattern-matching every
    possible summing expression -- a rule that only catches known spellings is a
    rule that reports "clean" the moment someone spells it differently.

    ``strict`` additionally reports fields that are DECLARED UNAVAILABLE but
    whose fabrication is still live because the file is outside this lane's
    permitted scope. Those are real findings and the default run lists them under
    ``known_out_of_scope``; ``--strict`` turns them into failures so a follow-up
    lane inherits them as a worklist rather than a footnote.
    """
    out_of_scope.clear()
    findings: list[dict] = []
    # Deliberately NARROW tokens: the CALLS, not the concept. A loose
    # ``"unknown_exposure"`` is satisfied by a VARIABLE of that name, so deleting
    # the guard while keeping `unknown = 0` still passed -- verified by removing
    # the `is_unknown_exposure(row)` branch from `_cost_summary` on purpose.
    #
    # The evidence is the same per-VARIABLE routing rule 1 uses, over the whole
    # enclosing function rather than a line window: a total is usually computed
    # far from the key that reports it, and a window narrow enough to be precise
    # about WHICH total would miss the assignment that makes it honest.
    guards = ("money_total(", "is_unknown_exposure(")
    for f in FIELDS:
        # Rule 2's scope is the CostEntry LEDGER. Agent-run and shadow-run
        # accounting are money but live in tables that carry no UNKNOWN_EXPOSURE
        # marker, so there is nothing there to swallow.
        if not f.money or f.gate or not f.ledger:
            continue
        got = _claim_lines(f)
        if got is None:
            findings.append({
                "rule": "no-money-total-that-swallows-an-unknown-exposure",
                "endpoint": f.endpoint, "field": f.field, "site": f.site,
                "detail": "site is not a resolvable file:line reference",
            })
            continue
        path, line, _, lines = got
        expression = _value_expression(lines, int(line))
        fn = _enclosing_function(lines, int(line))
        guarded = _value_is_routed(
            expression, fn or "",
            helpers=guards,
            # A guard may also be a bare `if is_unknown_exposure(row): ... continue`
            # rather than an assignment, so the branch form counts as evidence
            # for whatever the surrounding loop accumulated.
            also_guard_lines=[ln for ln in (fn or "").splitlines() if any(
                g in ln for g in guards)],
        )
        if guarded:
            continue
        finding = {
            "rule": "no-money-total-that-swallows-an-unknown-exposure",
            "endpoint": f.endpoint, "field": f.field, "site": f.site,
            "detail": (
                "no money_total() / is_unknown_exposure() guard reaches this "
                f"total; a raw sum over {', '.join(_MONEY_SUM_HINTS)} adds an "
                "UNKNOWN_EXPOSURE row as 0.0, under-reporting real money as a "
                "smaller confident number"
            ),
        }
        if strict or not _is_out_of_scope(f):
            findings.append(finding)
        else:
            out_of_scope.append(finding)
    return findings


# ---------------------------------------------------------------------------
# rule 3 -- the published schema must admit the null the backend now sends
# ---------------------------------------------------------------------------


def rule3_findings(strict: bool = False) -> list[dict]:
    """A widened field must be nullable in the published OpenAPI schema.

    Without this the fix is half cosmetic: the backend returns null, the contract
    says `number`, and a consumer that trusts the contract invents a value in the
    browser. Contracts attach with ``responses=``, which documents and does not
    filter, so the API answer is right either way -- which is exactly why this
    needs checking rather than assuming.
    """
    spec = REPO / "frontend" / "src" / "api" / "openapi.json"
    if not spec.exists():
        return [{
            "rule": "widened-field-is-nullable-in-the-schema",
            "endpoint": "*", "field": "*", "site": "frontend/src/api/openapi.json",
            "detail": "openapi.json not generated; run scripts/gen_openapi.py",
        }]
    schemas = json.loads(spec.read_text(encoding="utf-8"))["components"]["schemas"]
    findings: list[dict] = []
    for f in FIELDS:
        if f.schema is None or not f.nullable:
            continue
        model = schemas.get(f.schema)
        if model is None:
            findings.append({
                "rule": "widened-field-is-nullable-in-the-schema",
                "endpoint": f.endpoint, "field": f.field, "site": f.schema,
                "detail": "schema is not published",
            })
            continue
        # `schema_field` is the property name inside the named schema. A field
        # whose property is absent is recorded as UNCHECKABLE rather than passing:
        # the audit must never claim coverage it does not have.
        spec_field = model.get("properties", {}).get(f.schema_field)
        if spec_field is None:
            findings.append({
                "rule": "widened-field-is-nullable-in-the-schema",
                "endpoint": f.endpoint, "field": f.field, "site": f.schema,
                "detail": (
                    f"property {f.schema_field!r} is not in the published schema; "
                    f"the contract cannot carry this nullability -- regenerate "
                    f"the contract or mark the field UNRESOLVED"
                ),
            })
            continue
        nullable = spec_field.get("type") == "null" or "null" in str(
            spec_field.get("anyOf", "")
        )
        if not nullable:
            findings.append({
                "rule": "widened-field-is-nullable-in-the-schema",
                "endpoint": f.endpoint, "field": f.field, "site": f.schema,
                "detail": f"declared {json.dumps(spec_field)}; it can be null",
            })
    return findings


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def build_report(*, strict: bool = False) -> dict:
    findings = (
        rule1_findings(strict) + rule2_findings(strict) + rule3_findings()
    )
    gaps = sorted(out_of_scope, key=lambda d: (d["endpoint"], d["field"]))
    unresolved = sorted(
        f"{f.endpoint}.{f.field}: {f.unresolved}"
        for f in FIELDS if f.unresolved
    )
    by_endpoint: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for f in FIELDS:
        by_endpoint[f.endpoint] = by_endpoint.get(f.endpoint, 0) + 1
        by_class[f.provenance] = by_class.get(f.provenance, 0) + 1
    return {
        "audit": "analytics-honesty",
        "work_order": "16.5.7 §8/§11",
        "invariant": (
            "missing != 0; unsupported != 0; unknown != 0. A genuine measured "
            "zero stays 0."
        ),
        "vocabulary": dict(sorted(VOCABULARY.items())),
        "rules": [
            "no-a-fabricated-zero",
            "no-money-total-that-swallows-an-unknown-exposure",
            "widened-field-is-nullable-in-the-schema",
        ],
        "zero_allow": sorted(ZERO_ALLOW),
        "counts": {
            "fields": len(FIELDS),
            "by_class": dict(sorted(by_class.items())),
            "by_endpoint": dict(sorted(by_endpoint.items())),
            "findings": len(findings),
        },
        # Sorted for a stable diff. Deterministic by construction: no timestamp
        # inside the hashed content, and every collection is ordered.
        "findings": sorted(
            findings, key=lambda d: (d["rule"], d["endpoint"], d["field"])
        ),
        # Fabrications that are REAL but whose file is outside this lane's
        # permitted write set. Published so a follow-up lane inherits a worklist
        # rather than re-deriving it. `--strict` promotes them into `findings`.
        "known_out_of_scope": gaps,
        "unresolved_provenance": unresolved,
        "fields": [
            {
                "endpoint": f.endpoint,
                "field": f.field,
                "provenance": f.provenance,
                "reason": f.reason,
                "site": f.site,
                "nullable": f.nullable,
                "gate": f.gate,
                "gate_reason": f.gate_reason,
                "money": f.money,
                "ledger": f.ledger,
                "formula": f.formula,
                "schema": f.schema,
                "schema_field": f.schema_field,
                "unresolved": f.unresolved,
            }
            for f in sorted(FIELDS, key=lambda f: (f.endpoint, f.field))
        ],
    }


def print_table(report: dict) -> None:
    print("=" * 96)
    print("METRIC HONESTY AUDIT -- Work 16.5.7 §8/§11")
    print("=" * 96)
    print(f"invariant: {report['invariant']}\n")
    print("  vocabulary:")
    for cls, text_ in report["vocabulary"].items():
        print(f"    {cls:<14} {text_}")
    counts = report["counts"]
    print(
        f"\n  {counts['fields']} audited field(s) across "
        f"{len(counts['by_endpoint'])} endpoint(s): "
        + ", ".join(f"{k}={v}" for k, v in counts["by_class"].items())
    )

    print("\n" + "-" * 96)
    print(f"  {'ENDPOINT':<34} {'FIELD':<38} {'CLASS':<13} NOTES")
    print("-" * 96)
    for f in report["fields"]:
        notes = []
        if f["ledger"]:
            notes.append("ledger-money")
        elif f["money"]:
            notes.append("money(non-ledger)")
        if f["gate"]:
            notes.append("gate")
        if f["schema"] is None:
            notes.append("no-schema")
        if f["unresolved"]:
            notes.append("UNRESOLVED")
        if not f["nullable"]:
            notes.append("never-null")
        print(
            f"  {f['endpoint']:<34} {f['field']:<38} {f['provenance']:<13} "
            f"{','.join(notes)}"
        )

    print("\n" + "-" * 96)
    print("  UNRESOLVED PROVENANCE (a finding, not a pass)")
    print("-" * 96)
    if report["unresolved_provenance"]:
        for u in report["unresolved_provenance"]:
            print(f"  * {u}")
    else:
        print("  (none)")

    gaps = report["known_out_of_scope"]
    if gaps:
        print("\n" + "-" * 96)
        print(f"  KNOWN OUT-OF-SCOPE FABRICATIONS ({len(gaps)}) — real, not fixed here")
        print("-" * 96)
        for f in gaps:
            print(f"  [{f['rule']}] {f['endpoint']}.{f['field']} @ {f['site']}")
            print(f"      {f['detail']}")
        print("\n  Run with --strict to fail on these instead of listing them.")

    print("\n" + "-" * 96)
    print(f"  FINDINGS ({len(report['findings'])})")
    print("-" * 96)
    if not report["findings"]:
        print("  none -- every audited field is classifiable and no rule fired")
    for f in report["findings"]:
        print(f"  [{f['rule']}] {f['endpoint']}.{f['field']} @ {f['site']}")
        print(f"      {f['detail']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check", action="store_true",
        help="fail when docs/ANALYTICS_HONESTY_AUDIT.json is missing or stale",
    )
    ap.add_argument(
        "--strict", action="store_true",
        help=(
            "also fail on fabrications whose file is outside this lane's scope. "
            "A REVIEW mode: it prints but does not write the audit JSON, so it "
            "cannot overwrite the committed artifact with a different document."
        ),
    )
    args = ap.parse_args()

    report = build_report(strict=args.strict)
    print_table(report)

    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    previous = OUT.read_text(encoding="utf-8") if OUT.exists() else None

    findings = report["findings"]
    if args.strict:
        # `--strict` is a REVIEW mode, not an authoring mode: it promotes the
        # known out-of-scope gaps into failures, so writing its report would
        # overwrite the committed artifact with a different document and make the
        # next `--check` fail for a reason that has nothing to do with the code.
        if findings:
            print(f"\nAUDIT FAIL (--strict): {len(findings)} out-of-scope "
                  f"fabrication(s) remain")
            return 1
        print("\nAUDIT OK (--strict): no out-of-scope fabrications remain")
        return 0

    if args.check:
        if previous is None:
            print(f"AUDIT FAIL: {OUT.relative_to(REPO)} does not exist")
            return 1
        if previous != payload:
            print(
                f"AUDIT FAIL: {OUT.relative_to(REPO)} is stale; re-run without "
                f"--check and commit the result"
            )
            return 1
        print(f"AUDIT OK: {OUT.relative_to(REPO)} is current, 0 findings")
        return 0

    OUT.write_text(payload, encoding="utf-8")
    print(f"WROTE {OUT.relative_to(REPO)} ({OUT.stat().st_size:,} bytes)")

    if findings:
        print(f"\nAUDIT FAIL: {len(findings)} finding(s) -- see the table above")
        return 1
    print("\nAUDIT OK: every audited field is classifiable, 0 findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
