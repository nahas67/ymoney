"""LLM provider: OpenAI-compatible chat completions with deterministic mock mode.

When MOCK_LLM=true or no API key is configured, callers must supply a
`mock_fn` producing the expected structured result; this keeps development
fully offline and clearly non-production.

**Work 15.7 — this is a billable call, and it is now guarded.** The old body
wrapped the metered POST in a ``models_to_try`` x ``attempts`` double loop
whose ``except Exception`` treated a read timeout exactly like a clean 4xx,
so one ``complete()`` could leave up to four billable requests on the wire and
``complete_json`` doubled that again. The loop now lives in
:mod:`app.engine.intelligence.llm_paid` and advances ONLY on a proven safe
retry:

    a billable POST that was delivered and lost
        ->  SUBMISSION_UNKNOWN, UNKNOWN_EXPOSURE, and the loop STOPS
            unless a caller explicitly opts into a second charge.

That last part is the honest limit of a chat completion. It is synchronous:
the generated text IS the response body, so the protocol hands back no request
id, no job id and no status endpoint. A lost response therefore cannot be
looked up later -- reconciliation is IMPOSSIBLE here, not pending, and
:func:`app.engine.intelligence.llm_paid.llm_reconciliation_capability` says so
rather than inventing a recovery handle. Only the safety rules moved; request
building, provider resolution, text sanitising and cost pricing all behave as
before.

**Work 15.9 §7: the re-ask is a SECOND PURCHASE, and it is now a named one.**
Work 15.7 was right that the re-ask is not an ambiguity retry -- the first reply
arrived, so its outcome is known -- and that made it easy to miss that it is
also *billable*. A parser helper that quietly calls the vendor again is a
purchase hidden inside a formatting function: no caller can budget it, no
ledger row names it, and ``reask_if_unparseable=True`` was the default, so every
unparseable reply across the product bought a second completion silently.

So the re-ask is now :class:`ReaskPolicy`, and the default refuses it. Three
things changed, in the order they are tried:

1. :func:`_local_json_repair` -- deterministic, free, and tried FIRST. It
   cannot be wrong about money and cannot fail, so every reply it rescues is a
   completion nobody had to buy.
2. :meth:`ReaskPolicy.decide` -- a refusal unless the caller named itself,
   declared additional budget, and that budget covers the worst-case estimate
   for the extra request.
3. the second :func:`complete` call -- which then reserves its OWN budget
   (never the caller's) and is recorded as its own submission.

Either way the outcome is durable: a ``paid.llm.reask`` activity-feed event
carries the first request's submission id and outcome, the repairs attempted,
the reason, the authority, and the second request's submission id and cost. A
second charge that cannot be explained three weeks later is the failure mode
this replaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.core.config import settings
from app.engine.intelligence import llm_paid
from app.services import cost


class LLMError(Exception):
    pass


class LLMCompletionError(LLMError, llm_paid.LLMCompletionPaidError):
    """A billable completion produced no usable text.

    Also an :class:`LLMError`, so every existing ``except LLMError`` handler
    keeps working unchanged, and also a
    :class:`~app.engine.intelligence.llm_paid.LLMCompletionPaidError`, so a
    caller that knows better can read ``.kind`` and the persisted
    ``.submission``:

    ``"AMBIGUOUS"``
        the request was sent and the outcome is not known. Do not re-send.
    ``"BILLED_BUT_UNUSABLE"``
        a 2xx arrived, so the completion WAS metered, and the body is not
        content. A re-send is a certain second charge, not a coin flip.
    ``"EXHAUSTED_ON_PROVEN_SAFE_FAILURES"``
        every candidate was refused with a 4xx or never reached a socket.
        Nothing was billed; retrying is safe.
    """

    def __init__(self, *, detail: str = "", kind: str = "",
                 submission=None, cause: BaseException | None = None) -> None:
        self.detail = detail
        self.kind = kind
        self.submission = submission
        # The cause already knows how to describe itself in its own words --
        # including what it is safe to do next -- so only the kind prefix is
        # added here rather than flattening the message to a bare detail.
        super().__init__(
            f"LLM completion failed ({kind or 'UNKNOWN'}): "
            f"{str(cause) if cause is not None else detail}")


class LLMResult:
    def __init__(self, text: str, model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                 attempts: int = 1, models_tried: tuple[str, ...] = (),
                 submission_id: str = "", is_estimate: bool = False):
        self.text = text
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        #: How many billable POSTs this answer actually cost. On a path where
        #: only proven-safe retries are followed this equals the number of
        #: candidate rejections, and it is always 1 when the first attempt
        #: succeeded.
        self.attempts = attempts
        self.models_tried = models_tried
        self.submission_id = submission_id
        self.is_estimate = is_estimate

    @property
    def cost_usd(self) -> float:
        return cost.estimate_llm_cost(self.model, self.prompt_tokens, self.completion_tokens)


def llm_available() -> bool:
    eff = _effective()
    if eff["mock"]:
        return False
    return bool(eff["api_key"])


def _effective() -> dict:
    from app.services.provider_settings import effective_llm

    return effective_llm()


def complete(
    system: str,
    user: str,
    *,
    workspace_id: str = "",
    model: str | None = None,
    tier: str | None = None,  # "cheap" | "reasoning" | "verification"
    temperature: float = 0.8,
    max_tokens: int = 1500,
    json_mode: bool = False,
    mock_fn=None,
    fallback_policy: llm_paid.FallbackPolicy | None = None,
    preauthorized_spend: object | None = None,
) -> LLMResult:
    """Synchronous completion. Raises LLMError when unavailable and no mock.

    ``preauthorized_spend`` lets a caller that has ALREADY reserved for this
    exact request (Work 15.8 §5: batch translation prices one combined request,
    not twenty segments) hand its reservation down instead of being charged a
    second time by the per-leg gate. Without it, a batch would write two
    reservation rows for one POST -- conservative, never a leak, but it
    over-counts the cap and hides real headroom.

    `tier` selects a routed model from provider settings when no explicit
    model is given (cheap/reasoning/verification), falling back to default.

    `fallback_policy` is how a caller says it accepts the risk of a SECOND
    billable completion after an ambiguous one. The default refuses; see
    :class:`app.engine.intelligence.llm_paid.FallbackPolicy` for why the
    decision is not the loop's to make.
    """
    if not llm_available():
        if mock_fn is not None:
            return LLMResult(text=mock_fn(), model="mock")
        raise LLMError(
            "LLM provider not configured — add an API key under Settings → Connections "
            "or set OPENAI_API_KEY and MOCK_LLM=false"
        )

    eff = _effective()
    chosen_model = model
    if not chosen_model and tier:
        chosen_model = (eff.get("tiers") or {}).get(tier) or eff["model"] or settings.llm_model
    if not chosen_model:
        chosen_model = eff["model"] or settings.llm_model
    base_url = (eff["base_url"] or settings.openai_base_url).rstrip("/")
    api_key = eff["api_key"]
    body = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    # The same ordered candidate list the old double loop walked -- but the
    # loop may only advance while the previous attempt is a PROVEN safe retry.
    candidates = llm_paid.build_candidates(
        chosen_model, body, fallback_model=settings.llm_fallback_model,
        json_mode=json_mode,
    )
    try:
        outcome = llm_paid.run_completion(
            transport=httpx.post,
            url=f"{base_url}/chat/completions",
            api_key=api_key,
            candidates=candidates,
            max_tokens=max_tokens,
            timeout=120,
            workspace_id=workspace_id,
            operation="complete",
            policy=fallback_policy,
            preauthorized_spend=preauthorized_spend,
            # An httpx error embeds the full request URL, so a configured
            # gateway credential would otherwise be logged and then raised to
            # the caller, the API response and the run record.
            secrets=(api_key,),
        )
    except llm_paid.LLMCompletionPaidError as exc:
        # One public error type for callers, three honest `kind`s inside it.
        raise LLMCompletionError(detail=exc.detail, kind=exc.kind,
                                 submission=exc.submission, cause=exc) from exc
    return LLMResult(
        text=outcome.text, model=outcome.model,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        attempts=outcome.attempts, models_tried=outcome.models_tried,
        submission_id=outcome.submission_id, is_estimate=outcome.is_estimate,
    )


def complete_json(system: str, user: str, *, workspace_id: str = "", mock_fn=None,
                  fallback_policy: llm_paid.FallbackPolicy | None = None,
                  reask_policy: ReaskPolicy | None = None,
                  reask_if_unparseable: bool | None = None,
                  preauthorized_spend: object | None = None,
                  report: dict | None = None, **kwargs) -> dict:
    """Completion constrained to JSON with tolerant parsing.

    Free models (OpenRouter) often prepend chatty meta-text before the JSON
    ("We need to produce a JSON with...\n{...}"). ``_extract_json`` already scans
    for the outermost braces, and :func:`_local_json_repair` then fixes the
    shapes that are free to fix. Only after BOTH fail is a second completion
    considered -- and that costs money.

    **The re-ask is a second purchase (Work 15.9 §7).** The default refuses it.
    To buy one, pass a :class:`ReaskPolicy` that names the approver, declares
    ``additional_budget_usd``, and has that budget cover the worst-case estimate
    for the extra request. ``fallback_policy`` is a DIFFERENT decision and is not
    consulted here: the first completion demonstrably arrived (we have its text),
    so nothing was ambiguous -- the question is only whether the answer is worth
    a second invoice.

    ``reask_if_unparseable`` is the old boolean and is kept only so a caller does
    not crash on it. It no longer enables a second charge on its own: ``True``
    without a ``reask_policy`` is an error, because a flag is exactly how this
    cost was hidden for two releases.

    An AMBIGUOUS first leg cannot reach the re-ask at all: ``complete()`` raises
    instead of returning unusable text, so the total is one POST.
    """
    if reask_if_unparseable is not None and reask_if_unparseable and \
            reask_policy is None:
        raise ValueError(
            "reask_if_unparseable=True would buy a SECOND billable completion "
            "and is no longer enough on its own. Pass reask_policy=ReaskPolicy("
            "approved_by=..., additional_budget_usd=..., reason=...) so the "
            "extra charge is attributed and budgeted, or drop the flag and let "
            "the local repair pass handle the reply.")
    policy = reask_policy or ReaskPolicy()

    res = complete(system, user, json_mode=True, workspace_id=workspace_id, mock_fn=mock_fn,
                   fallback_policy=fallback_policy,
                   preauthorized_spend=preauthorized_spend, **kwargs)
    first = _reask_record(res, phase="first")
    parsed, repairs = _extract_json_with_repair(res.text)
    if parsed is not None and repairs:
        _reask_event(
            "paid.llm.repair",
            f"complete_json rescued a reply locally instead of buying a second "
            f"completion (repairs: {', '.join(repairs) or 'none'})",
            level="info",
            data={**first, "repairs": list(repairs), "rescued": True,
                      "purchased": False},
        )

    if parsed is None:
        max_tokens = int(kwargs.get("max_tokens", 800) or 800)
        estimate = _reask_estimate(system, user, max_tokens=max_tokens)
        verdict = policy.decide(reason="the first reply was not valid JSON",
                                estimated_usd=estimate,
                                workspace_id=workspace_id)
        if not verdict.allowed:
            _reask_event(
                "paid.llm.reask",
                f"complete_json refused a second billable completion: "
                f"{verdict.reason}",
                level="warning",
                data={**first, **verdict.to_dict(), "estimated_usd": estimate,
                      "rescued": False},
            )
            if report is not None:
                report.update({**first, **verdict.to_dict(),
                               "estimated_usd": estimate, "purchased": False,
                               "repairs": list(repairs)})
            raise LLMError(
                f"model returned invalid JSON and no re-ask policy permits a "
                f"second completion: {verdict.reason}. First reply: "
                f"{res.text[:200]}")
        retry = complete(
            system + "\nCRITICAL: Your entire reply must be ONLY a valid JSON object. "
            "No preamble, no explanation, no markdown fences — start with { and end with }.",
            user,
            json_mode=False,
            workspace_id=workspace_id,
            temperature=0.3,
            max_tokens=max_tokens,
            # The re-ask is a deliberate SECOND billable generation, so it
            # reserves its own budget. Reusing the caller's reservation would
            # hide a real charge behind an already-settled row.
        )
        second = _reask_record(retry, phase="second")
        _reask_event(
            "paid.llm.reask",
            f"complete_json bought a SECOND billable completion "
            f"(${second['cost_usd']:.4f}) for the same question",
            level="warning",
            data={**first, **verdict.to_dict(), **second,
                  "repairs": list(repairs), "rescued": False,
                  "purchased": True, "estimated_usd": estimate},
        )
        if report is not None:
            report.update({**first, **verdict.to_dict(), **second,
                           "repairs": list(repairs), "purchased": True,
                           "estimated_usd": estimate})
        parsed = _extract_json(retry.text)
    elif report is not None:
        report.update({**first, "repairs": list(repairs), "purchased": False,
                       "rescued": bool(repairs)})
    if parsed is None:
        raise LLMError(f"model returned invalid JSON: {res.text[:200]}")
    return parsed


# ---------------------------------------------------------------------------
# §7: the re-ask policy, and the local repair that runs before it
# ---------------------------------------------------------------------------


class ReaskAuthority(StrEnum):
    """WHO authorised spending a second completion on the same question."""

    #: Nobody. The conservative default: an unparseable reply is a failure.
    NEVER = "NEVER"
    #: A named caller opted in, in review-visible code, with a budget.
    CALLER_EXPLICIT = "CALLER_EXPLICIT"


class ReaskDecision(StrEnum):
    ALLOW = "ALLOW_REASK"
    REFUSE = "REFUSE_REASK"


@dataclass(frozen=True)
class ReaskVerdict:
    decision: ReaskDecision
    reason: str
    estimated_usd: float = 0.0
    additional_budget_usd: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.decision is ReaskDecision.ALLOW

    def to_dict(self) -> dict:
        return {"reask_decision": str(self.decision),
                "reask_allowed": self.allowed,
                "reask_reason": self.reason,
                "additional_budget_usd": round(float(self.additional_budget_usd), 6)}


@dataclass(frozen=True)
class ReaskPolicy:
    """May this caller buy a SECOND completion for one question?

    Three requirements, all mandatory, because a second charge is money and a
    money decision needs a name, a ceiling and a reason:

    * :attr:`allow_reask` -- the opt-in, off by default;
    * :attr:`approved_by` -- who; an unattributed second charge is
      indistinguishable from a bug three weeks later;
    * :attr:`additional_budget_usd` -- how much MORE this question may cost,
      which must cover the worst-case estimate for the extra request. A ceiling
      of zero is not "free", it is "nobody authorised a second invoice", and a
      ceiling below the estimate is a budget that lies.

    The shape deliberately mirrors
    :class:`app.engine.intelligence.llm_paid.FallbackPolicy`, because they
    answer the same question about different risks: FallbackPolicy governs a
    second charge after an AMBIGUITY, this one governs a second charge after a
    DEFINITE but unusable answer.
    """

    allow_reask: bool = False
    approved_by: str = ""
    reason: str = ""
    additional_budget_usd: float = 0.0
    max_reasks: int = 1
    authority: ReaskAuthority = ReaskAuthority.NEVER

    def __post_init__(self) -> None:
        if self.allow_reask and not self.approved_by:
            raise ValueError(
                "allow_reask=True requires approved_by: a policy that permits a "
                "second billable completion must say who permitted it")
        if self.max_reasks < 0:
            raise ValueError(f"max_reasks cannot be negative: {self.max_reasks}")

    def decide(self, *, reason: str = "", estimated_usd: float = 0.0,
               workspace_id: str = "") -> ReaskVerdict:
        """Answer one question, WITHOUT issuing a request.

        Deliberately free-standing so an API layer, a UI or a CLI can render the
        risk before a human clicks.
        """
        budget = round(float(self.additional_budget_usd or 0.0), 6)
        need = round(float(estimated_usd or 0.0), 6)
        if not self.allow_reask:
            return ReaskVerdict(
                decision=ReaskDecision.REFUSE, estimated_usd=need,
                additional_budget_usd=budget,
                reason=(f"no re-ask policy permits a second completion "
                        f"({reason or 'the first reply was unusable'}). The "
                        f"default is conservative: an unparseable reply is a "
                        f"failure, not a licence to buy the same answer twice"))
        if not str(workspace_id or "").strip():
            return ReaskVerdict(
                decision=ReaskDecision.REFUSE, estimated_usd=need,
                additional_budget_usd=budget,
                reason=("a re-ask is a second billable operation and has no "
                        "workspace to charge it to, so there is nobody whose "
                        "cap would see it"))
        if budget <= 0:
            return ReaskVerdict(
                decision=ReaskDecision.REFUSE, estimated_usd=need,
                additional_budget_usd=budget,
                reason=("additional_budget_usd is 0.0: a re-ask is a second "
                        "purchase, and 'we did not budget for it' is a "
                        "refusal, not a free operation"))
        if need > budget:
            return ReaskVerdict(
                decision=ReaskDecision.REFUSE, estimated_usd=need,
                additional_budget_usd=budget,
                reason=(f"additional_budget_usd ${budget:.4f} does not cover "
                        f"the ${need:.4f} worst-case estimate for the re-ask, so "
                        f"authorising it would overspend the declared ceiling"))
        note = f"; {self.reason}" if self.reason else ""
        return ReaskVerdict(
            decision=ReaskDecision.ALLOW, estimated_usd=need,
            additional_budget_usd=budget,
            reason=(f"explicit re-ask by {self.approved_by}: a second completion "
                    f"is authorised for this question at up to ${budget:.4f} "
                    f"(estimated ${need:.4f}){note}"))


#: Repairs are tried in this order, cheapest-first, and each one is only applied
#: when the strict parse already failed. Every name is recorded when it succeeds
#: so an operator can see WHY no second charge was needed.
_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.+?)\s*```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_LEADING_LABEL = re.compile(r"^\s*(?:json|JSON)?\s*[:=]\s*")
#: Python literals, but only OUTSIDE string literals: rewriting ``None`` inside
#: a caption would silently change the content, so the scan tracks quoting.
_PY_LITERALS = re.compile(r"\b(True|False|None)\b")


def _py_literals_outside_strings(text: str) -> str:
    """``True``/``False``/``None`` -> JSON, never inside a quoted value."""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        match = _PY_LITERALS.match(text, index)
        if match:
            out.append({"True": "true", "False": "false",
                        "None": "null"}[match.group(1)])
            index = match.end()
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _local_json_repair(text) -> tuple[dict, list[str]] | None:
    """Deterministic repairs for a reply that is JSON with a fixable defect.

    Returns ``(value, repairs_applied)`` or ``None``.

    Every repair here is FREE and local, which is the entire point: a reply that
    arrives wrapped in a markdown fence or with a trailing comma did not need a
    second completion, and buying one is how a formatting bug became a line item
    on somebody's invoice.

    Deliberately absent: smart-quote normalisation, single-quote rewriting and
    unescaped-newline repair. Each can change a VALUE, and a parse that succeeds
    with a corrupted string is worse than a parse that fails honestly. The
    strict :func:`_extract_json` is unchanged for the same reason -- a tolerant
    parser is where a silent content change goes to hide.
    """
    import json

    if isinstance(text, dict):
        return text
    body = str(text or "").strip()
    if not body:
        return None
    raw_candidates: list[tuple[str, str]] = []
    fenced = _JSON_FENCE.search(body)
    if fenced:
        raw_candidates.append(("stripped-markdown-fence", fenced.group("body")))
    raw_candidates.append(("as-is", body))
    candidates: list[tuple[str, str]] = []
    for name, candidate in raw_candidates:
        candidates.append((name, candidate))
        # The strict parser already tolerates chatty prose by taking the
        # outermost brace span, so the repairs have to start from the same
        # place -- otherwise "Here you go:\n{...}" fails for the wrong reason
        # and a perfectly repairable reply looks unparseable.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidates.append((name, candidate[start:end + 1]))
    for name, candidate in candidates:
        # The mutations CHAIN. Each defect needs its own fix -- a reply that is
        # both fenced and comma-terminated only parses once both are gone -- so
        # every intermediate state is retried, cheapest fix first.
        working = candidate
        applied: list[str] = [] if name == "as-is" else [name]
        for repair, mutate in (
                ("stripped-leading-label",
                 lambda text: _LEADING_LABEL.sub("", text, count=1)),
                ("json-literals", _py_literals_outside_strings),
                ("stripped-trailing-comma",
                 lambda text: _TRAILING_COMMA.sub(r"\1", text)),
        ):
            mutated = mutate(working)
            if mutated == working:
                continue
            working = mutated
            try:
                parsed = json.loads(working)
            except (json.JSONDecodeError, ValueError):
                applied.append(repair)
                continue
            if isinstance(parsed, dict):
                return parsed, [*applied, repair]
            applied.append(repair)
    return None


def _extract_json_with_repair(text) -> tuple[dict | None, list[str]]:
    """Strict parse first, then the free local repairs. ``(value, repairs)``."""
    parsed = _extract_json(text)
    if parsed is not None:
        return parsed, []
    repaired = _local_json_repair(text)
    if repaired is None:
        return None, []
    value, applied = repaired
    return value, applied


def _reask_record(res, *, phase: str) -> dict:
    """One leg's outcome, shaped for the activity feed and the caller's report."""
    return {"phase": phase, "submission_id": str(getattr(res, "submission_id", "") or ""),
            "model": str(getattr(res, "model", "") or ""),
            "attempts": int(getattr(res, "attempts", 1) or 1),
            "prompt_tokens": int(getattr(res, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(res, "completion_tokens", 0) or 0),
            "cost_usd": round(float(getattr(res, "cost_usd", 0.0) or 0.0), 6)}


def _reask_estimate(system: str, user: str, *, max_tokens: int) -> float:
    """Worst-case price of the extra completion, for the budget check.

    A BOUND, not a measurement: the vendor owns the tokenizer and the model may
    route, so the input side is estimated per character. The same honest
    position ``llm_paid.estimate_request_cost`` takes.
    """
    try:
        eff = _effective()
    except Exception:  # noqa: BLE001 - a price hint must never fail a caller
        eff = {}
    model = str(eff.get("model") or settings.llm_model or "")
    prompt_tokens = -(-(len(system or "") + len(user or "")) // 4)
    return round(float(cost.estimate_llm_cost(model, prompt_tokens, max_tokens)), 6)


def _reask_event(kind: str, message: str, *, level: str = "info",
                 data: dict | None = None) -> None:
    """Persist the re-ask decision. Best-effort, like every paid telemetry write."""
    try:
        from app.services.events import record_event

        record_event(None, kind=kind, message=message, level=level,
                     source="llm.complete_json", data=dict(data or {}))
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a call
        from loguru import logger

        logger.warning("paid llm re-ask event dropped: %s", exc)


def _extract_json(text):
    import json

    if isinstance(text, dict):
        return text
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None
