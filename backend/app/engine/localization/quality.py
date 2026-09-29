"""Deterministic translation QC for the localization pipeline (Work 07 Lane A).

Deterministic checks are ALWAYS authoritative:

- names / numbers / URLs / timestamps preserved (unless the glossary says
  otherwise),
- glossary violations (brand/product/terminology must survive translation),
- untranslated-text detection (script + ratio heuristics),
- CTA meaning via a keyword map (follow/comment/share/buy/... per language),
- banned / unsafe substitution list (risky claims must never be introduced),
- missing segments, speaker mismatch, subtitle timing and duration drift.

Semantic QA runs through the DecisionEngine — imported LAZILY inside a
try/except — strictly in SHADOW mode: it is recorded as an advisory check and
can never change a deterministic verdict.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

#: check severity → QC status rollup order (worst wins)
_FAIL, _REVIEW, _WARN, _PASS = "fail", "review", "warn", "pass"

#: CTA categories → per-language keyword lexicon (matched case-insensitively)
CTA_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "follow": {
        "en": ["follow for more", "follow me", "follow us", "follow"],
        "es": ["sigueme", "siguenos", "sigue para mas"],
        "fr": ["abonnez-vous", "suivez-moi", "suivez"],
        "de": ["folge mir", "folgen", "abonnieren"],
        "pt": ["siga-me", "siga", "seguir"],
        "it": ["seguimi", "seguici", "segui"],
        "hi": ["follow karein", "follow"],
        "ja": ["フォロー", "フォローして"],
    },
    "subscribe": {
        "en": ["subscribe", "hit the bell"],
        "es": ["suscribete", "suscribirse"],
        "fr": ["abonnez", "s'abonner"],
        "de": ["abonnieren", "abonniere"],
        "pt": ["inscreva-se", "inscrever"],
        "it": ["iscriviti", "iscriviti al canale"],
        "hi": ["subscribe karein", "subscribe"],
        "ja": ["チャンネル登録", "登録して"],
    },
    "comment": {
        "en": ["drop a comment", "comment below", "leave a comment", "comment"],
        "es": ["deja un comentario", "comenta", "comentario"],
        "fr": ["laisse un commentaire", "commente", "commentaire"],
        "de": ["kommentar hinterlassen", "kommentiere", "kommentar"],
        "pt": ["deixe um comentario", "comente", "comentario"],
        "it": ["lascia un commento", "commenta", "commento"],
        "hi": ["comment karein", "comment"],
        "ja": ["コメント", "コメントして"],
    },
    "share": {
        "en": ["share this", "share with", "share"],
        "es": ["comparte", "compartir"],
        "fr": ["partage", "partager"],
        "de": ["teile", "teilen"],
        "pt": ["compartilhe", "compartilhar"],
        "it": ["condividi", "condividere"],
        "hi": ["share karein", "share"],
        "ja": ["シェア", "シェアして"],
    },
    "buy": {
        "en": ["buy now", "get it today", "shop now", "buy"],
        "es": ["compra ahora", "compra", "consiguelo"],
        "fr": ["achetez", "acheter"],
        "de": ["kaufen", "jetzt kaufen"],
        "pt": ["compre agora", "compre"],
        "it": ["compra ora", "compra"],
        "hi": ["kharidein", "buy"],
        "ja": ["購入", "買って"],
    },
    "visit": {
        "en": ["link in bio", "check the link", "head to the site", "visit"],
        "es": ["enlace en la bio", "revisa el enlace", "visita"],
        "fr": ["lien dans la bio", "va sur le site", "visite"],
        "de": ["link in der bio", "besuche", "zur website"],
        "pt": ["link na bio", "visite"],
        "it": ["link in bio", "visita"],
        "hi": ["bio me link", "visit"],
        "ja": ["プロフィールのリンク", "リンク"],
    },
}

#: risky claims that must never appear as a substitution / in output
UNSAFE_TERMS: dict[str, list[str]] = {
    "en": ["get rich quick", "guaranteed returns", "guaranteed profit",
           "risk-free profit", "double your money", "free money",
           "no risk", "100% profit"],
    "es": ["enriquecerse rapido", "ganancias garantizadas", "dinero gratis",
           "sin riesgo", "duplica tu dinero"],
    "fr": ["enrichissement rapide", "gains garantis", "argent garanti",
           "sans risque", "doublez votre argent"],
    "de": ["schnell reich werden", "garantierte rendite", "risikofrei",
           "geld verdienen ohne risiko"],
    "pt": ["enriquecer rapido", "lucros garantidos", "dinheiro gratis",
           "sem risco"],
    "it": ["arricchirsi velocemente", "guadagni garantiti", "soldi gratis",
           "senza rischio"],
}

#: source term → forms that must never be used as its "translation"
BANNED_SUBSTITUTIONS: dict[str, tuple[str, ...]] = {
    "guaranteed": ("guaranteed returns", "guaranteed profit", "garantizado sin riesgo"),
    "risk-free": ("risk-free profit", "sin riesgo", "sans risque"),
    "investment": ("get rich quick", "double your money"),
}

#: script-bearing target languages → regex for "is this actually localized?"
SCRIPT_PATTERNS: dict[str, re.Pattern[str]] = {
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]"),
    "zh": re.compile(r"[\u4e00-\u9fff]"),
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"),
    "ar": re.compile(r"[\u0600-\u06ff]"),
    "ru": re.compile(r"[\u0400-\u04ff]"),
    "hi": re.compile(r"[\u0900-\u097f]"),
}

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*%?")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_TS_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_NAME_STOPWORDS = frozenset({
    "The", "This", "That", "These", "Those", "And", "But", "For", "You",
    "Your", "Are", "Was", "Were", "Have", "Has", "Had", "Not", "With",
    "What", "When", "Where", "Why", "How", "Who", "All", "Any", "Can",
    "Could", "Should", "Would", "Will", "Just", "Now", "New", "Here",
    "There", "They", "Them", "Then", "Than", "Also", "More", "Most",
    "First", "Second", "Third", "Next", "Step", "Steps", "Today", "Never",
    "Always", "Every", "One", "Two", "Three", "Four", "Five", "Get", "Got",
})

WORD_RATE = 2.6  # words per second — same budget the dubbing/TTS layer uses


# ---------------------------------------------------------------------------
# text helpers (pure, unit-tested)
# ---------------------------------------------------------------------------

def _fold(text: str) -> str:
    """Casefold + strip accents so 'Sígueme' matches 'sigueme'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).casefold()


def _contains(text: str, needle: str, case_sensitive: bool = False) -> bool:
    if not needle:
        return True
    if case_sensitive:
        return needle in (text or "")
    return _fold(needle) in _fold(text)


def extract_numbers(text: str) -> list[str]:
    return _NUM_RE.findall(text or "")


def extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def extract_timestamps(text: str) -> list[str]:
    return _TS_RE.findall(text or "")


def extract_names(text: str) -> list[str]:
    """Capitalized proper-noun candidates (sentence-initial words excluded)."""
    out: list[str] = []
    for i, tok in enumerate(_NAME_RE.findall(text or "")):
        if tok in _NAME_STOPWORDS:
            continue
        # first token of the string is usually a sentence start, not a name
        if i == 0 and (text or "")[:1].isalpha() and (text or "").find(tok) == 0:
            continue
        if tok not in out:
            out.append(tok)
    return out


def _digits(num: str) -> str:
    return re.sub(r"[^\d]", "", num)


def missing_numbers(source: str, target: str) -> list[str]:
    """Numbers present in the source but absent from the translation."""
    out: list[str] = []
    tgt_fold = (target or "")
    tgt_digits = _digits(tgt_fold)
    for num in extract_numbers(source):
        if num in tgt_fold:
            continue
        d = _digits(num)
        if d and d in tgt_digits:
            continue
        if num not in out:
            out.append(num)
    return out


def find_cta(texts: list[str], langs: tuple[str, ...] | None = None) -> str | None:
    """First CTA category detected across texts (any language, or a subset)."""
    blob = _fold(" ".join(texts or []))
    if not blob.strip():
        return None
    for category, by_lang in CTA_KEYWORDS.items():
        for lang, keywords in by_lang.items():
            if langs is not None and lang not in langs:
                continue
            for kw in keywords:
                if _fold(kw) in blob:
                    return category
    return None


def cta_category_present(texts: list[str], category: str, language: str) -> bool:
    blob = _fold(" ".join(texts or []))
    return any(_fold(kw) in blob
               for kw in CTA_KEYWORDS.get(category, {}).get(language, []))


def unsafe_hits(texts: list[str], language: str) -> list[str]:
    """Risky claims found in the output (target lexicon + English safety net)."""
    blob = _fold(" ".join(texts or []))
    langs = ["en"] + ([language] if language and language != "en" else [])
    hits: list[str] = []
    for lang in langs:
        for term in UNSAFE_TERMS.get(lang, []):
            if _fold(term) in blob and term not in hits:
                hits.append(term)
    return hits


def glossary_violations(source_text: str, target_text: str, entries: list[dict],
                        language: str) -> list[dict]:
    """Glossary terms in the source that did not survive the translation.

    Pronunciation entries are exempt: they only shape the TTS audio, the
    on-screen text keeps the real spelling.
    """
    out: list[dict] = []
    for entry in entries or []:
        term = str(entry.get("term") or "").strip()
        if not term:
            continue
        langs = [str(x) for x in (entry.get("target_languages") or [])]
        if langs and language not in langs:
            continue
        if str(entry.get("kind") or "") == "pronunciation":
            continue
        if not _contains(source_text, term, bool(entry.get("case_sensitive"))):
            continue
        expected = str(entry.get("replacement") or "").strip() or term
        if not _contains(target_text, expected, bool(entry.get("case_sensitive"))):
            out.append({"term": term, "expected": expected,
                        "kind": str(entry.get("kind") or "terminology"),
                        "source": source_text[:120], "target": target_text[:120]})
    return out


def banned_substitution_hits(source_text: str, target_text: str) -> list[dict]:
    hits: list[dict] = []
    for term, banned in BANNED_SUBSTITUTIONS.items():
        if not _contains(source_text, term):
            continue
        for form in banned:
            if _contains(target_text, form):
                hits.append({"source_term": term, "banned_form": form})
    return hits


def is_untranslated(source_texts: list[str], target_texts: list[str],
                    language: str, source_language: str = "en") -> tuple[bool, str]:
    """Script-family or same-text heuristics: is the output still the input?"""
    if not target_texts:
        return True, "no target text"
    if language and language == source_language:
        return False, "target equals source language"
    pattern = SCRIPT_PATTERNS.get(language)
    if pattern is not None:
        hits = sum(1 for t in target_texts if pattern.search(t or ""))
        ratio = hits / max(1, len(target_texts))
        if ratio < 0.5:
            return True, (f"only {hits}/{len(target_texts)} line(s) contain "
                          f"{language} script")
        return False, f"{hits}/{len(target_texts)} line(s) localized"
    pairs = list(zip(source_texts, target_texts))
    if not pairs:
        return True, "no paired text"
    same = sum(1 for s, t in pairs if (s or "").strip() == (t or "").strip()
               and (s or "").strip())
    ratio = same / len(pairs)
    if ratio > 0.8:
        return True, f"{same}/{len(pairs)} line(s) identical to the source"
    return False, f"{len(pairs) - same}/{len(pairs)} line(s) differ"


def estimate_seconds(text: str) -> float:
    words = len((text or "").split())
    return round(words / WORD_RATE, 3)


def _check(name: str, status: str, detail: str, **extra) -> dict:
    row = {"name": name, "status": status, "detail": detail}
    row.update(extra)
    return row


def rollup_status(checks: list[dict]) -> str:
    statuses = {c.get("status") for c in checks}
    if _FAIL in statuses:
        return "FAIL"
    if _REVIEW in statuses:
        return "REVIEW_REQUIRED"
    if _WARN in statuses:
        return "PASS_WITH_WARNINGS"
    return "PASS"


# ---------------------------------------------------------------------------
# semantic advisory (DecisionEngine, lazy import, SHADOW only)
# ---------------------------------------------------------------------------

def semantic_advisory(workspace_id: str, source_texts: list[str],
                      target_texts: list[str], language: str) -> dict:
    """SHADOW-only semantic cross-check. Never authoritative, never raises."""
    try:
        from app.engine.intelligence.decision import DecisionEngine
    except Exception as exc:  # pragma: no cover - engine always ships, be safe
        return _check("semantic_advisory", "pass",
                      f"skipped (DecisionEngine unavailable: {type(exc).__name__})",
                      authoritative=False, mode="SHADOW")
    try:
        engine = DecisionEngine(workspace_id, mode="SHADOW", persist=False)
        out, record = engine.verify({
            "claim": " ".join(source_texts or [])[:4000],
            "evidence": " ".join(target_texts or [])[:4000],
            "criterion": f"faithful {language} localization of the source meaning",
        })
        status = str((out or {}).get("status", "")) if isinstance(out, dict) else ""
        verdict = "warn" if status in ("UNVERIFIED", "PARTIAL") else "pass"
        return _check(
            "semantic_advisory", verdict,
            f"SHADOW advisory: {status or 'no verdict'} "
            f"(provider={record.actual_provider})",
            authoritative=False, mode="SHADOW", agreement=record.agreement,
        )
    except Exception as exc:  # advisory only - any failure degrades to skipped
        return _check("semantic_advisory", "pass",
                      f"skipped ({type(exc).__name__})",
                      authoritative=False, mode="SHADOW")


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

def evaluate(*, workspace_id: str, language: str,
             source_cues: list[dict], target_cues: list[dict],
             glossary_entries: list[dict] | None = None,
             source_speakers: list[str] | None = None,
             target_speakers: list[str] | None = None,
             timeline_doc: dict | None = None,
             source_duration: float = 0.0,
             localized_duration: float = 0.0,
             source_language: str = "en",
             translation_version: int = 1) -> dict:
    """Run every deterministic check and roll up one QC status.

    ``semantic_advisory`` is appended AFTER the rollup: in SHADOW it can never
    change the verdict.
    """
    checks: list[dict] = []
    entries = list(glossary_entries or [])
    src_texts = [str(c.get("text") or "") for c in source_cues]
    tgt_texts = [str(c.get("text") or "") for c in target_cues]

    # 1. segment coverage -------------------------------------------------
    if len(target_cues) != len(source_cues):
        checks.append(_check(
            "segments_present", _FAIL,
            f"expected {len(source_cues)} segment(s), produced {len(target_cues)}"))
    else:
        checks.append(_check("segments_present", _PASS,
                             f"{len(target_cues)} segment(s) present"))
    empty = [i for i, t in enumerate(tgt_texts) if not t.strip()]
    if empty and len(target_cues) == len(source_cues):
        checks.append(_check("segments_non_empty", _FAIL,
                             f"empty translation at index {empty[:5]}"))

    # 2. glossary enforcement --------------------------------------------
    violations: list[dict] = []
    for src, tgt in zip(source_cues, target_cues):
        violations.extend(glossary_violations(
            str(src.get("text") or ""), str(tgt.get("text") or ""),
            entries, language))
    if violations:
        checks.append(_check(
            "glossary_preserved", _FAIL,
            f"{len(violations)} glossary term(s) mistranslated: "
            + ", ".join(v["term"] for v in violations[:5]),
            violations=violations))
    else:
        applied = sum(1 for e in entries
                      if not (e.get("target_languages")
                              and language not in e["target_languages"]))
        checks.append(_check("glossary_preserved", _PASS,
                             f"no violations across {applied} applicable term(s)"))

    # 3. numbers / URLs / timestamps / names ------------------------------
    missing_nums: list[str] = []
    missing_urls: list[str] = []
    missing_ts: list[str] = []
    for src, tgt in zip(source_cues, target_cues):
        s, t = str(src.get("text") or ""), str(tgt.get("text") or "")
        for n in missing_numbers(s, t):
            if n not in missing_nums:
                missing_nums.append(n)
        for u in extract_urls(s):
            if not _contains(t, u) and u not in missing_urls:
                missing_urls.append(u)
        for ts in extract_timestamps(s):
            if ts not in t and ts not in missing_ts:
                missing_ts.append(ts)
    if missing_nums or missing_urls or missing_ts:
        checks.append(_check(
            "literals_preserved", _REVIEW,
            f"missing numbers={missing_nums[:5]} urls={missing_urls[:3]} "
            f"timestamps={missing_ts[:3]}",
            numbers=missing_nums, urls=missing_urls, timestamps=missing_ts))
    else:
        checks.append(_check("literals_preserved", _PASS,
                             "numbers, URLs and timestamps preserved"))

    missing_names: list[str] = []
    for src, tgt in zip(source_cues, target_cues):
        s, t = str(src.get("text") or ""), str(tgt.get("text") or "")
        for name in extract_names(s):
            if name in missing_names:
                continue
            if _contains(t, name):
                continue
            if any(_contains(name, str(e.get("term") or "")) for e in entries):
                continue  # glossary owns it
            missing_names.append(name)
    if missing_names:
        checks.append(_check("names_preserved", _WARN,
                             f"name(s) not preserved: {missing_names[:5]}",
                             names=missing_names))
    else:
        checks.append(_check("names_preserved", _PASS, "names preserved"))

    # 4. untranslated-text detection --------------------------------------
    untranslated, detail = is_untranslated(src_texts, tgt_texts, language,
                                           source_language)
    checks.append(_check("translation_present", _REVIEW if untranslated else _PASS,
                         detail))

    # 5. CTA meaning -------------------------------------------------------
    source_cta = find_cta(src_texts)
    if source_cta and language in CTA_KEYWORDS[source_cta]:
        if cta_category_present(tgt_texts, source_cta, language):
            checks.append(_check("cta_meaning", _PASS,
                                 f"CTA '{source_cta}' preserved in {language}"))
        else:
            checks.append(_check(
                "cta_meaning", _WARN,
                f"CTA '{source_cta}' from the source is missing in {language}"))
    else:
        checks.append(_check("cta_meaning", _PASS,
                             "no CTA to preserve (or no lexicon for this language)"))

    # 6. banned / unsafe substitutions ------------------------------------
    unsafe = unsafe_hits(tgt_texts, language)
    banned: list[dict] = []
    for src, tgt in zip(source_cues, target_cues):
        banned.extend(banned_substitution_hits(str(src.get("text") or ""),
                                               str(tgt.get("text") or "")))
    if unsafe or banned:
        checks.append(_check(
            "unsafe_substitutions", _FAIL,
            f"unsafe terms={unsafe[:5]} banned substitutions={banned[:3]}",
            unsafe=unsafe, banned=banned))
    else:
        checks.append(_check("unsafe_substitutions", _PASS,
                             "no banned or unsafe substitution introduced"))

    # 7. speakers ----------------------------------------------------------
    src_spk, tgt_spk = list(source_speakers or []), list(target_speakers or [])
    if src_spk and sorted(src_spk) != sorted(tgt_spk):
        checks.append(_check(
            "speakers_mapped", _REVIEW,
            f"speaker mismatch: source={sorted(src_spk)} target={sorted(tgt_spk)}"))
    else:
        checks.append(_check("speakers_mapped", _PASS,
                             f"{len(tgt_spk)} speaker(s) mapped"))

    # 8. subtitle timing ---------------------------------------------------
    timing_problems: list[str] = []
    prev_end = -1.0
    for cue in target_cues:
        start = float(cue.get("start") or 0.0)
        end = float(cue.get("end") or 0.0)
        if end <= start:
            timing_problems.append(f"cue {cue.get('index')} has non-positive window")
        if start < prev_end - 1e-6:
            timing_problems.append(f"cue {cue.get('index')} overlaps the previous cue")
        prev_end = max(prev_end, end)
    if timeline_doc is not None:
        for tr in timeline_doc.get("tracks", []):
            if tr.get("kind") != "caption":
                continue
            cursor = 0.0
            for clip in sorted(tr.get("clips", []), key=lambda c: c.get("start", 0.0)):
                st = float(clip.get("start") or 0.0)
                if st < cursor - 1e-6:
                    timing_problems.append(
                        f"caption clip '{clip.get('id')}' overlaps in the timeline")
                cursor = max(cursor, st + float(clip.get("duration") or 0.0))
    if timing_problems:
        checks.append(_check("subtitle_timing", _FAIL,
                             "; ".join(timing_problems[:5]),
                             problems=timing_problems))
    else:
        checks.append(_check("subtitle_timing", _PASS,
                             "cue windows preserved and non-overlapping"))

    # 9. duration drift ----------------------------------------------------
    drift_ratio = 1.0
    if source_duration > 0:
        drift_ratio = localized_duration / source_duration
        if drift_ratio > 1.5 or drift_ratio < 0.5:
            checks.append(_check(
                "duration_drift", _REVIEW,
                f"localized runtime {localized_duration:.1f}s vs source "
                f"{source_duration:.1f}s ({drift_ratio:.2f}x)"))
        elif drift_ratio > 1.2 or drift_ratio < 0.8:
            checks.append(_check(
                "duration_drift", _WARN,
                f"runtime shifted to {drift_ratio:.2f}x of the source"))
        else:
            checks.append(_check("duration_drift", _PASS,
                                 f"runtime within tolerance ({drift_ratio:.2f}x)"))
    else:
        checks.append(_check("duration_drift", _PASS, "no source duration to compare"))

    status = rollup_status(checks)
    advisory = semantic_advisory(workspace_id, src_texts, tgt_texts, language)
    checks.append(advisory)
    if status == "PASS" and advisory.get("status") == "warn" and not \
            advisory.get("authoritative", True):
        # SHADOW advisory never downgrades below this, and never upgrades a
        # REVIEW/FAIL — at most it adds a warning to an otherwise clean run.
        status = "PASS_WITH_WARNINGS"

    return {
        "status": status,
        "checks": checks,
        "language": language,
        "source_language": source_language,
        "translation_version": translation_version,
        "counts": {
            "source_segments": len(source_cues),
            "target_segments": len(target_cues),
            "glossary_terms": len(entries),
            "warnings": sum(1 for c in checks if c["status"] == _WARN),
            "reviews": sum(1 for c in checks if c["status"] == _REVIEW),
            "failures": sum(1 for c in checks if c["status"] == _FAIL),
        },
    }


def save_report(db, *, workspace_id: str, localized_content_id: str,
                result: dict) -> object:
    """Persist a LocalizationQCReport row (workspace-scoped) and commit."""
    from app.models import LocalizationQCReport

    row = LocalizationQCReport(
        workspace_id=workspace_id,
        localized_content_id=localized_content_id,
        status=str(result.get("status") or "REVIEW_REQUIRED"),
        checks_json=dict(result or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


__all__ = [
    "BANNED_SUBSTITUTIONS",
    "CTA_KEYWORDS",
    "UNSAFE_TERMS",
    "banned_substitution_hits",
    "cta_category_present",
    "estimate_seconds",
    "evaluate",
    "extract_names",
    "extract_numbers",
    "extract_timestamps",
    "extract_urls",
    "find_cta",
    "glossary_violations",
    "is_untranslated",
    "missing_numbers",
    "rollup_status",
    "save_report",
    "semantic_advisory",
    "unsafe_hits",
]
