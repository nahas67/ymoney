# YMONEY v2 Architecture

## Purpose

YMONEY is an autonomous content operating system, not a video generator. The
system discovers opportunities, makes explainable decisions, creates content,
validates it, distributes it through permitted providers, measures outcomes, and
learns from those outcomes.

The existing implementation remains the production baseline while v2 boundaries
are introduced incrementally. No v2 component may bypass workspace isolation,
safety gates, durable jobs, or provider interfaces.

## Team model

The codebase is organized around cooperating subsystems with explicit contracts:

| Team | Responsibility | Primary boundary |
|---|---|---|
| Supervisor | Durable workflow coordination and gates | `RunPlan` / stage transitions |
| Intelligence | Discovery, normalization, scoring, decisions | `Opportunity` + `Decision` |
| Research | Sources, claims, factual confidence | `ResearchBrief` + `Claim` |
| Creative | Strategy, hooks, scripts, storyboards | `CreativeBrief` |
| Media | Assets, audio, rendering, video analysis | `MediaArtifact` |
| Distribution | SEO, platform packages, publishing | `PublicationTarget` |
| Learning | Metrics, memory, patterns, feedback | `LearningObservation` |
| Governance | Permissions, budgets, risk, review | `PolicyDecision` |
| Platform | API, auth, events, storage, database | shared infrastructure |
| Experience | Command Center, Studio, Runs, Agents | read models over APIs/events |

These are logical teams, not separate processes by default. They can become
separate workers later without changing domain contracts.

## Runtime layers

```text
Experience
  ↓ REST + SSE
Application API
  ↓ commands / queries
Supervisor + Policy
  ↓ durable stage jobs
Domain teams
  ↓ skills + controlled tools
Provider ports
  ↓ adapters
Local, free, optional paid providers
```

### Domain layer

Pure, deterministic decisions belong here:

- lifecycle classification
- opportunity scoring
- content state transitions
- idempotency keys
- budget and rate gates
- workspace policy evaluation
- quality thresholds

Domain code must not import HTTP clients, environment secrets, or UI code.

### Application layer

Application services coordinate domain operations and persistence:

- create a run
- enqueue a stage
- invoke an agent
- record an event
- persist evidence
- resume or retry a stage

### Infrastructure layer

Infrastructure implements ports:

- SQLAlchemy repositories
- DB-backed job queue
- local object storage
- LLM/TTS/image/video adapters
- official publishing APIs
- analytics providers
- SSE event transport

## Agent runtime contract

Every agent declares:

```text
identity
objective
skills
controlled tools
memory scopes
permissions
budget policy
risk policy
model policy
retry policy
evaluator
execution policy
```

Agent execution must:

1. Load only targeted workspace context.
2. Check workspace capability permissions.
3. Check budget and safety policy.
4. Create an observable agent run.
5. Invoke tools through the controlled tool registry.
6. Validate structured output.
7. Persist output and evidence.
8. Emit success/failure events.
9. Be safe to retry or resume.

## Skills and tools

A Skill is reusable domain behavior. A Tool is a bounded operation.

Skills declare required tools and versions. Tools declare:

- input schema
- permissions
- provider
- timeout
- retryability
- estimated cost
- audit behavior

No agent may execute arbitrary Python, shell, network, or provider calls. All
external effects must pass through a registered tool and policy check.

The initial registry is in `backend/app/engine/capabilities.py`. The next step is
to migrate concrete agent calls behind tool handlers while preserving current
agent behavior.

## Provider ports

Provider-specific code stays behind interfaces:

- `LLMProvider`
- `ResearchProvider`
- `TTSProvider`
- `ImageProvider`
- `VideoEngine`
- `Publisher`
- `AnalyticsProvider`
- `MemoryProvider`
- `StorageProvider`

Provider selection is workspace-configurable. Defaults must work offline using
mock/local providers. Paid providers are explicit accelerators, never hidden
requirements.

## Durable workflow model

A workflow is a persisted Run containing ordered stage executions:

```text
DISCOVER
NORMALIZE
DEDUPE
SCORE
DECIDE
RESEARCH
FACT_CHECK
STRATEGIZE
SCRIPT
STORYBOARD
BUILD
QC
PACKAGE
PUBLISH
MEASURE
LEARN
```

Each stage records:

- status
- input/output references
- agent
- tool calls
- provider
- retries
- duration
- estimated/actual cost
- errors
- evidence

Stage transitions are explicit and idempotent. A retry must not duplicate a
render, publication, memory write, or analytics record.

## Explainability contract

Every decision returns:

```json
{
  "action": "PRODUCE",
  "confidence": 0.91,
  "factors": [],
  "evidence": [],
  "estimated_cost_usd": 0,
  "risk": 0,
  "expected_outcome": "high",
  "human_review_required": false
}
```

The UI renders this as a WHY panel. Explanations must be derived from persisted
signals, never generated after the fact from invented rationale.

## Governance

Workspace policy is evaluated before every externally consequential operation:

- allowed autonomy level
- allowed platforms
- forbidden topics
- max publications
- max concurrent jobs
- daily/monthly/per-item budget
- minimum QC score
- risk threshold
- required review categories

A policy violation creates a safety event and pauses the relevant run. It never
silently falls through to a mock success.

## Data ownership and tenancy

All tenant-owned records include `workspace_id` and are queried through
workspace-scoped dependencies. Secrets are encrypted and never serialized to
frontend responses. Audit records are append-oriented and searchable by
workspace, agent, tool, run, and cycle.

## Experience architecture

The UI is a read model over real backend state:

- Command Center: current run, next action, WHY, safety, cost
- Trends: source-backed opportunities and score decomposition
- Studio: content lifecycle and evidence
- Runs: stage timeline, retries, tool calls, errors
- Agents: skills, tools, permissions, health, run history
- Intelligence: patterns, confidence, sample sizes
- System Health: database, workers, providers, storage

Loading, empty, error, success, mock, and permission-denied states are explicit.

## Migration strategy

1. Keep existing `autopilot.py` and APIs as the compatibility path.
2. Introduce versioned contracts and registries first.
3. Add persistent capability policy and tool audits.
4. Add stage/run repositories and read models without changing behavior.
5. Move one stage at a time behind application services.
6. Add provider health and capability negotiation.
7. Replace direct agent/provider calls with controlled tool invocations.
8. Split workers only after durable contracts and tests are stable.

No migration step should require a paid provider or break simulation mode.

## Verification gates

Every slice must pass:

- workspace isolation tests
- permission and safety tests
- idempotency tests
- simulation end-to-end test
- backend test suite
- frontend typecheck/build
- API schema smoke checks

A slice is not complete if it merely creates files. It must be observable,
exercisable, and verified.
