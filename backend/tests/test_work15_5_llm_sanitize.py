"""Think-scratchpad stripping and error-text redaction at the LLM boundary.

Two live defects, one choke point each:

* A reasoning model (DeepSeek-R1, QwQ, o-series) returns its private
  deliberation inside ``<think>…</think>``. Nothing stripped it, so the
  deliberation was read aloud in the script and printed in the subtitles. The
  case that actually bites is the *unclosed* block — a response truncated by the
  token cap, where a plain ``.*?</think>`` match matches nothing and leaves the
  entire scratchpad in place.
* Exception text from an OpenAI-compatible stack embeds the full request URL,
  so a gateway credential in userinfo or a query string was logged and raised
  to the caller.

The over-eagerness tests matter as much as the leak tests: a sanitizer that
mangles an ordinary URL or an ordinary sentence corrupts output silently, and
that is the failure mode nobody notices.
"""

from __future__ import annotations

import re
from urllib.parse import quote, quote_plus

import pytest

from app.engine.intelligence.sanitize import (
    REDACTED,
    assert_no_secrets,
    redact_error_text,
    redact_secrets,
    strip_think_tags,
)
from app.providers.llm import LLMError
from app.providers.llm import complete as llm_complete

# Credential-shaped literals are assembled at runtime: a hard-coded key-shaped
# string in a test file is a leak waiting for a grep-based secret scanner.
KEY_SHAPED = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "012345"


# ---------------------------------------------------------------------------
# <think> stripping
# ---------------------------------------------------------------------------


def test_closed_think_block_is_removed_and_answer_survives():
    out = strip_think_tags("<think>let me count the words first</think>Here is the script.")
    assert "let me count" not in out
    assert out == "Here is the script."


def test_multiple_closed_think_blocks_are_all_removed():
    out = strip_think_tags("<think>a</think>One.<think>b</think> Two.<think>c</think>")
    assert out == "One. Two."


def test_unclosed_think_block_leaves_nothing_behind():
    """The truncated-response case: no closing tag exists to match."""
    with pytest.raises(ValueError):
        strip_think_tags("<think>I should first consider whether the audience")


def test_unclosed_think_block_does_not_swallow_the_preceding_answer():
    out = strip_think_tags("Final answer line.<think>now I start doubting it")
    assert out == "Final answer line."


def test_unclosed_think_block_keeps_answer_that_preceded_it_on_its_own_line():
    out = strip_think_tags("Answer text here.\n<think>wait, let me reconsider")
    assert out == "Answer text here."


def test_think_block_with_attributes_is_matched():
    out = strip_think_tags('<think type="internal">hidden</think>Visible line.')
    assert out == "Visible line."


def test_think_matching_is_case_insensitive():
    out = strip_think_tags("<THINK>hidden</THINK>Visible line.")
    assert out == "Visible line."


def test_think_block_spanning_newlines_is_removed():
    out = strip_think_tags("<think>\nline one\nline two\n</think>\nAnswer.")
    assert "line one" not in out
    assert out == "Answer."


def test_paragraph_breaks_inside_the_answer_survive():
    """Scripts are split on blank lines; a strip that collapsed them broke
    every downstream paragraph split."""
    out = strip_think_tags("<think>x</think>First para.\n\nSecond para.\n\nThird.")
    assert out == "First para.\n\nSecond para.\n\nThird."


def test_single_newlines_inside_the_answer_survive():
    out = strip_think_tags("<think>x</think>Line one.\nLine two.")
    assert out == "Line one.\nLine two."


def test_leading_and_trailing_whitespace_is_trimmed():
    assert strip_think_tags("   <think>x</think>  Answer.  ") == "Answer."


def test_none_content_raises():
    with pytest.raises(ValueError):
        strip_think_tags(None)


def test_empty_string_raises():
    with pytest.raises(ValueError):
        strip_think_tags("")


def test_whitespace_only_after_strip_raises():
    with pytest.raises(ValueError):
        strip_think_tags("<think>only reasoning</think>   \n  ")


def test_non_string_content_raises_type_error():
    with pytest.raises(TypeError):
        strip_think_tags({"text": "not a string"})


def test_provider_label_appears_in_the_error():
    with pytest.raises(ValueError, match="deepseek"):
        strip_think_tags("<think>x</think>", provider="deepseek")


def test_angle_bracket_words_that_are_not_think_tags_survive():
    out = strip_think_tags("Use <thinking> mode carefully and <thinks> too.")
    assert "<thinking>" in out
    assert "<thinks>" in out


def test_ordinary_script_with_no_think_tags_is_returned_unchanged():
    script = "What if one small change to your routine reshaped your results?"
    assert strip_think_tags(script) == script


# ---------------------------------------------------------------------------
# redaction — leaks must be removed
# ---------------------------------------------------------------------------


def test_url_userinfo_is_redacted():
    out = redact_error_text("Connection to https://alice:hunter2@gw.example.com/v1 failed")
    assert "hunter2" not in out
    assert "alice" not in out
    assert REDACTED in out


def test_url_userinfo_with_empty_password_is_redacted():
    out = redact_error_text("https://alice:@gw.example.com/v1 timed out")
    assert "alice" not in out


def test_sensitive_query_parameter_is_redacted():
    for param in (
        "api_key", "apikey", "api-key", "token", "access_token",
        "access-token", "secret", "password", "authorization", "sig",
    ):
        out = redact_error_text(f"GET https://api.example.com/v1/x?{param}=SUPERSECRETVALUE")
        assert "SUPERSECRETVALUE" not in out, param
        assert REDACTED in out, param


def test_sensitive_query_parameter_mid_url_is_redacted():
    out = redact_error_text("https://api.example.com/v1?a=1&api_key=LEAKME&b=2 failed")
    assert "LEAKME" not in out
    # Non-sensitive siblings survive so the error stays diagnosable.
    assert "a=1" in out and "b=2" in out


def test_percent_encoded_query_value_is_redacted():
    out = redact_error_text("https://api.example.com/v1?api_key=abc%2Fdef%3D%3D&x=1")
    assert "abc%2Fdef%3D%3D" not in out


def test_known_secret_and_its_quote_plus_form_are_both_removed():
    """An upstream error reports the key the way the URL encoded it."""
    secret = "p@ss word/with+chars"
    encoded = quote_plus(secret)
    assert encoded != secret  # guard the test itself
    out = redact_error_text(f"failed with {secret} and {encoded}", secrets=[secret])
    assert secret not in out
    assert encoded not in out


def test_known_secret_percent_encoded_form_is_removed():
    secret = "cred-" + "value/with+chars"
    encoded = quote(secret, safe="")
    assert encoded != secret
    out = redact_error_text(f"boom {encoded}", secrets=[secret])
    assert encoded not in out


def test_blank_secret_is_ignored_not_crashing():
    out = redact_error_text("plain failure", secrets=["", None])
    assert out == "plain failure"


def test_redact_error_text_composes_with_known_key_shapes():
    out = redact_error_text(f"auth failed for {KEY_SHAPED}")
    assert KEY_SHAPED not in out


def test_bearer_token_in_error_is_redacted():
    out = redact_error_text("Authorization: Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in out


def test_redact_error_text_handles_none():
    assert redact_error_text(None) == ""


# ---------------------------------------------------------------------------
# redaction — ordinary content must SURVIVE (over-eagerness guards)
# ---------------------------------------------------------------------------


def test_normal_url_survives_redaction():
    url = "https://api.example.com/v1/chat/completions"
    assert redact_error_text(f"404 from {url}") == f"404 from {url}"


def test_url_with_ordinary_query_survives():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL1234"
    assert redact_error_text(url) == url


def test_url_with_non_sensitive_query_params_survives():
    url = "https://api.example.com/v1/search?q=keyword&limit=10&cursor=abc123"
    assert redact_error_text(url) == url


def test_port_in_url_is_not_mistaken_for_userinfo():
    """`https://host:8443/path` has a colon but no credentials."""
    url = "https://internal.example.com:8443/v1/health"
    assert redact_error_text(url) == url


def test_prose_mentioning_keys_survives():
    sentence = (
        "The script agent asked for the api_key but the workspace had none saved, "
        "so the provider fell back to mock mode."
    )
    assert redact_error_text(sentence) == sentence


def test_prose_mentioning_a_password_in_past_tense_survives():
    sentence = "The user changed their password last Tuesday and reconnected the provider."
    assert redact_error_text(sentence) == sentence


def test_email_address_survives_redaction():
    text = "contact ops@example.com for access"
    assert redact_error_text(text) == text


def test_url_fragment_with_at_sign_survives():
    url = "https://example.com/page#section@2"
    assert redact_error_text(url) == url


def test_monkey_and_keyword_query_params_are_not_redacted():
    """`?monkey=` / `?keyword=` must not match the `key=` parameter rule."""
    for url in (
        "https://example.com/x?monkey=banana",
        "https://example.com/x?keyword=ai",
        "https://example.com/x?donkey=zzz",
    ):
        assert redact_error_text(url) == url


def test_empty_text_survives():
    assert redact_error_text("") == ""


def test_redacted_output_passes_assert_no_secrets():
    redacted = redact_error_text(
        f"https://u:p@host/v1?api_key=abc123 and {KEY_SHAPED}"
    )
    assert_no_secrets(redacted)


def test_redact_error_text_does_not_mutate_existing_payload_scrubbing():
    """The existing dict scrubber must keep working — it is not replaced."""
    out = redact_secrets({"api_key": KEY_SHAPED, "model": "gpt"})
    assert out["model"] == "gpt"
    assert KEY_SHAPED not in out["api_key"]


# ---------------------------------------------------------------------------
# the two call sites
# ---------------------------------------------------------------------------


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }


def test_provider_strips_think_block_before_returning(monkeypatch, billable_workspace):
    """`complete()` is the single choke point; the scratchpad must not escape."""
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _Resp("<think>private reasoning about hooks</think>Real answer."),
    )
    res = llm_complete("sys", "user", workspace_id=billable_workspace)
    assert res.text == "Real answer."
    assert "private reasoning" not in res.text


def test_provider_raises_when_only_a_think_block_came_back(monkeypatch, billable_workspace):
    """An answer that exists only inside the scratchpad has not answered.

    Returning an empty script would let a blank voice-over reach the timeline
    looking like finished work, so this fails loudly instead.
    """
    monkeypatch.setattr("httpx.post", lambda url, **kw: _Resp("<think>still weighing"))
    with pytest.raises(LLMError):
        llm_complete("sys", "user", workspace_id=billable_workspace)


def test_provider_still_returns_script_when_no_think_tag_present(monkeypatch, billable_workspace):
    monkeypatch.setattr(
        "httpx.post", lambda url, **kw: _Resp("Line one.\n\nLine two.")
    )
    assert llm_complete("sys", "user", workspace_id=billable_workspace).text == "Line one.\n\nLine two."


class _Boom(Exception):
    pass


def test_llm_error_does_not_leak_a_key_from_the_exception(monkeypatch, billable_workspace):
    def _post(url, **kw):
        raise _Boom("POST https://gateway.example.com/v1?key=SUPERSECRET123 failed")

    monkeypatch.setattr("httpx.post", _post)
    with pytest.raises(LLMError) as excinfo:
        llm_complete("sys", "user", workspace_id=billable_workspace)
    assert "SUPERSECRET123" not in str(excinfo.value)


def test_llm_error_does_not_leak_the_configured_key_verbatim(monkeypatch, billable_workspace):
    """The resolver-backed key is removed even when it is not URL-shaped.

    ``test-key`` is what the test bootstrap resolves as the workspace key, so
    it is what the provider passes to the sanitizer. A bare ``key=`` with no
    ``?``/``&`` prefix is deliberately *not* a query parameter, so this case can
    only be covered by the literal-secret path.
    """
    def _post(url, **kw):
        raise _Boom("gateway rejected credential test-key for this workspace")

    monkeypatch.setattr("httpx.post", _post)
    with pytest.raises(LLMError) as excinfo:
        llm_complete("sys", "user", workspace_id=billable_workspace)
    assert "test-key" not in str(excinfo.value)


def test_llm_error_does_not_leak_gateway_userinfo(monkeypatch, billable_workspace):
    def _post(url, **kw):
        raise _Boom("connect to https://alice:hunter2@gw.example.com/v1 refused")

    monkeypatch.setattr("httpx.post", _post)
    with pytest.raises(LLMError) as excinfo:
        llm_complete("sys", "user", workspace_id=billable_workspace)
    assert "hunter2" not in str(excinfo.value)


def test_llm_error_still_explains_the_failure(monkeypatch, billable_workspace):
    """Redaction must not destroy diagnosability."""
    def _post(url, **kw):
        raise _Boom("Connection refused on port 443")

    monkeypatch.setattr("httpx.post", _post)
    with pytest.raises(LLMError) as excinfo:
        llm_complete("sys", "user", workspace_id=billable_workspace)
    assert "Connection refused" in str(excinfo.value)


class _RenderResp:
    headers: dict = {}

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _stub_renderer(monkeypatch, status_code: int, text: str) -> None:
    from app.providers import broll as broll_mod

    monkeypatch.setattr(broll_mod, "broll_ai_base_url", lambda: "http://render.test")
    monkeypatch.setattr("httpx.post", lambda url, **kw: _RenderResp(status_code, text))


def test_broll_renderer_error_body_is_redacted(monkeypatch):
    """The one unredacted call site: `resp.text[:200]` from the AI renderer."""
    from app.providers import broll as broll_mod

    # Both credentials appear the way a real gateway reports them: one in the
    # URL userinfo, one as a query parameter.
    _stub_renderer(
        monkeypatch,
        500,
        '{"error":"upstream https://svc:hunter2@render.internal/v1 failed: '
        'https://render.internal/v1/job?api_key=SUPERSECRET123"}',
    )
    with pytest.raises(broll_mod.BrollError) as excinfo:
        broll_mod._server_generate("a prompt", "ws-test", 4.0, "9:16", "")
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "SUPERSECRET123" not in message


def test_broll_error_body_still_reports_status_and_body(monkeypatch):
    from app.providers import broll as broll_mod

    _stub_renderer(monkeypatch, 503, "renderer queue is full, retry later")
    with pytest.raises(broll_mod.BrollError) as excinfo:
        broll_mod._server_generate("a prompt", "ws-test", 4.0, "9:16", "")
    assert "503" in str(excinfo.value)
    assert "queue is full" in str(excinfo.value)


# ---------------------------------------------------------------------------
# structural guards
# ---------------------------------------------------------------------------


def test_think_regexes_are_distinct_and_unclosed_is_anchored():
    """The two shapes must be separate patterns, or the fix silently collapses."""
    from app.engine.intelligence.sanitize import (
        _THINK_BLOCK_RE,
        _UNCLOSED_THINK_BLOCK_RE,
    )

    assert _THINK_BLOCK_RE is not _UNCLOSED_THINK_BLOCK_RE
    assert _THINK_BLOCK_RE.search("<think>x</think>") is not None
    # The unclosed pattern must match with no closing tag present at all.
    assert _UNCLOSED_THINK_BLOCK_RE.search("<think>x and keeps going") is not None


def test_redaction_never_emits_a_bare_secret_in_a_url():
    """Property check across several credential shapes."""
    for s in (
        "https://user:pw12345@host/v1",
        "https://host/v1?api_key=abcdef123456",
        "https://host/v1?token=xyz789",
    ):
        out = redact_error_text(f"error while calling {s} for reason 42")
        assert "pw12345" not in out
        assert "abcdef123456" not in out
        assert "xyz789" not in out
        assert "reason 42" in out


def test_redacted_marker_is_not_double_applied():
    once = redact_error_text("https://u:p@h/v1?api_key=abcdef")
    twice = redact_error_text(once)
    assert once == twice


def test_url_in_an_exception_repr_is_redacted():
    text = "HTTPStatusError: 401 for url https://u:p@h/v1?token=abc123"
    out = redact_error_text(text)
    assert "abc123" not in out
    assert "401" in out


def test_redaction_is_idempotent_under_repetition():
    text = "https://u:p@h/v1?api_key=abcdef"
    assert redact_error_text(redact_error_text(text)) == redact_error_text(text)


def test_regex_source_does_not_reveal_a_literal_secret():
    """Guard against a future edit hard-coding one into the module."""
    from app.engine.intelligence.sanitize import _SENSITIVE_QUERY_RE

    assert not re.search(r"=\s*[\"'][A-Za-z0-9]{16,}", _SENSITIVE_QUERY_RE.pattern)

# ---------------------------------------------------------------------------
# Work 15.9 1: these tests complete a BILLABLE LLM request.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _owner_for_billable_lanes(billable_workspace):
    """A billable completion with no budget owner is refused (15.9 1).

    Correct behaviour, so these tests opt into a real workspace explicitly.
    """
    yield billable_workspace
