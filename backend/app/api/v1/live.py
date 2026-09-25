"""Live operational monitoring: real-time metric series + agent pipeline graph.

Both endpoints are viewer-scoped, cheap (indexed queries over the last hour)
and consumed by the Live Monitor page which polls them every few seconds.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.db import get_db
from app.models import AgentConfig, AgentRun, EventLog, Workspace
from app.models.base import utcnow
from app.services.auth_service import require_workspace_role

router = APIRouter(prefix="/workspaces/{workspace_id}/live", tags=["live"])

from app.engine.agents.registry import AGENT_META

# The pipeline order used by the agent work-graph (FIND→…→LEARN).
PIPELINE_ORDER = [
    "trend_hunter",
    "trend_analyst",
    "research",
    "strategist",
    "script_writer",
    "hook_optimizer",
    "producer",
    "quality",
    "seo",
    "publisher",
    "analytics",
    "learning",
]

# Stage → agent(s) mapping shown in the DAG header
STAGE_OF_AGENT = {
    "trend_hunter": "FIND",
    "trend_analyst": "SCORE",
    "research": "RESEARCH",
    "strategist": "SELECT",
    "script_writer": "BUILD",
    "hook_optimizer": "BUILD",
    "producer": "BUILD",
    "quality": "VERIFY",
    "seo": "UPLOAD",
    "publisher": "UPLOAD",
    "analytics": "MEASURE",
    "live": "LEARN",
    "learning": "LEARN",
}


@router.get("/metrics", summary="Per-minute metric buckets for the last 60 minutes")
def live_metrics(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """Bucket agent runs, failures, events and cost into 1-minute slots.

    Empty buckets are returned as zeros so the chart can render a continuous
    axis (60 points) without client-side gap-filling.
    """
    now = utcnow().replace(second=0, microsecond=0)
    start = now - timedelta(minutes=59)

    buckets: dict[str, dict] = {}
    for i in range(60):
        t = start + timedelta(minutes=i)
        key = t.strftime("%H:%M")
        buckets[key] = {"minute": key, "runs": 0, "failures": 0, "cost_usd": 0.0, "events": 0}

    # Agent runs per minute
    rows = db.execute(
        select(
            func.strftime("%H:%M", AgentRun.created_at),
            func.count(),
            func.sum(AgentRun.status == "FAILED"),
            func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
        )
        .where(AgentRun.workspace_id == ws.id, AgentRun.created_at >= start)
        .group_by(func.strftime("%H:%M", AgentRun.created_at))
    ).all()
    for hm, runs, failures, cost in rows:
        slot = buckets.get(hm)
        if slot:
            slot["runs"] += int(runs or 0)
            slot["failures"] += int(failures or 0)
            slot["cost_usd"] += float(cost or 0.0)

    # Events per minute (error/warning only — info is too noisy for a live chart)
    ev_rows = db.execute(
        select(
            func.strftime("%H:%M", EventLog.created_at),
            func.count(),
        )
        .where(
            EventLog.workspace_id == ws.id,
            EventLog.created_at >= start,
            EventLog.level.in_(["error", "warning"]),
        )
        .group_by(func.strftime("%H:%M", EventLog.created_at))
        .limit(200)
    ).all()
    for hm, n in ev_rows:
        slot = buckets.get(hm)
        if slot:
            slot["events"] = int(n or 0)

    # Current totals snapshot
    running = db.scalar(
        select(func.count()).where(
            AgentRun.workspace_id == ws.id, AgentRun.status == "RUNNING"
        )
    )
    cycles_done = db.scalar(
        select(func.count())
        .where(AgentRun.workspace_id == ws.id, AgentRun.status != "RUNNING")
    )
    total_cost = db.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(
            AgentRun.workspace_id == ws.id
        )
    )

    return {
        "series": list(buckets.values()),
        "now": {
            "running_agents": int(running or 0),
            "runs_last_hour": sum(b["runs"] for b in buckets.values()),
            "failures_last_hour": sum(b["failures"] for b in buckets.values()),
            "cost_last_hour_usd": round(sum(b["cost_usd"] for b in buckets.values()), 4),
            "total_cost_usd": round(float(total_cost or 0.0), 4),
            "completed_runs": int(cycles_done or 0),
        },
    }


@router.get("/agents/graph", summary="Agent pipeline as a live DAG with per-node stats")
def agent_graph(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """Returns the agent pipeline in execution order with live state.

    The frontend renders this as a horizontal DAG; each node shows live status
    (busy/idle/disabled/error), run counts, failure rate and average duration.
    """

    stats_rows = db.execute(
        select(
            AgentRun.agent_key,
            func.count().label("runs"),
            func.sum(AgentRun.status == "FAILED").label("failures"),
            func.avg(AgentRun.duration_ms).label("avg_ms"),
            func.sum(AgentRun.cost_usd).label("cost"),
        )
        .where(AgentRun.workspace_id == ws.id)
        .group_by(AgentRun.agent_key)
    ).all()
    by_key = {r.agent_key: r for r in stats_rows}

    cfg_rows = {
        c.agent_key: c
        for c in db.scalars(select(AgentConfig).where(AgentConfig.workspace_id == ws.id)).all()
    }

    running = db.scalars(
        select(AgentRun)
        .where(AgentRun.workspace_id == ws.id, AgentRun.status == "RUNNING")
    ).all()
    running_by_key: dict[str, AgentRun] = {}
    for r in running:
        running_by_key.setdefault(r.agent_key, r)

    nodes = []
    for key in PIPELINE_ORDER:
        meta = AGENT_META.get(key)
        if not meta:
            continue
        r = by_key.get(key)
        runs = int(r.runs or 0) if r else 0
        failures = int(r.failures or 0) if r else 0
        cfg = cfg_rows.get(key)
        is_running = key in running_by_key
        nodes.append({
            "key": key,
            "title": meta.title,
            "stage": STAGE_OF_AGENT.get(key, ""),
            "status": "disabled" if cfg and not cfg.enabled else ("busy" if is_running else ("error" if runs and failures >= runs * 0.5 else "idle")),
            "current_task": running_by_key[key].task_type if is_running else None,
            "runs": runs,
            "failures": failures,
            "avg_ms": int(r.avg_ms or 0) if r and r.avg_ms is not None else None,
            "cost_usd": round(float(r.cost or 0.0), 4) if r else 0.0,
        })

    recent_runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.workspace_id == ws.id)
        .order_by(AgentRun.created_at.desc())
        .limit(12)
    ).all()

    return {
        "nodes": nodes,
        "recent_runs": [
            {
                "id": r.id,
                "agent_key": r.agent_key,
                "task_type": r.task_type,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "cost_usd": r.cost_usd,
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in recent_runs
        ],
    }

