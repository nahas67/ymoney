"""Portable JSON queries for the two backends this repository supports.

Why this module exists
----------------------
``services/activity.py`` filtered the ledger with::

    EventLog.data_json["project_id"].as_string() == str(project_id)

which reads like "the JSON string at this key equals this id". It does not
mean that on both backends, because ``as_string()`` compiles DIFFERENTLY:

===========================================  ==================================
PostgreSQL                                    SQLite
===========================================  ==================================
``CAST((col ->> 'k') AS VARCHAR) = '42'``    ``JSON_EXTRACT(col, '$."k"') = '42'``
===========================================  ==================================

The PostgreSQL cast stringifies a JSON number (``42`` -> ``'42'``), so it
matches a row written as ``{"k": 42}``. SQLite has no such cast: ``JSON_EXTRACT``
on a JSON number yields an INTEGER, and ``42 = '42'`` is FALSE in SQLite, so
the same stored row is invisible. Measured on both, same three rows::

    sqlite  as_string() == '42'   -> [1]        (only the string row)
    pg      as_string() == '42'   -> [1, 2]     (string AND number rows)

So the divergence is in which rows exist, not in whether an error is raised --
the failure mode is a missing row, which is the quiet kind.

A second, sharper trap: ``JSON.contains()`` is ONE Python name for TWO
different SQL operators. Measured, same ``JSON`` column, same argument::

    pg      col.contains('42')  -> 42883 operator does not exist: json ~~ text
    sqlite  col.contains('42')  -> LIKE '%42%'  -> a substring match over the
                                        serialized document

One is refused outright, the other answers a question about the document's
serialized TEXT. Nothing in the name says which. Callers who want "does this
document have this key?" and callers who want "does this text appear
anywhere in this document?" both reach for ``contains()`` and get different
meanings, or an error, depending on the deployment.

A third trap, the one that hides: comparing a JSON column to a SERIALIZED
STRING. SQLAlchemy only auto-casts dict/list on the right-hand side, never
``str``, so::

    Job.payload == '{"a":1}'

reaches the server as ``col = '{"a":1}'``. Measured on both::

    pg      -> 42883 operator does not exist: json = character varying
    sqlite  -> []            a correct-looking query that silently matches
                              nothing, forever

Silently returning zero rows is the worst of the three outcomes, because
nothing in the logs distinguishes "no rows matched" from "this comparison
cannot work here".

The three helpers below give each question its own honest name and its own
correct operator per backend, and every one of them is asserted to return the
SAME ROWS on SQLite and PostgreSQL in ``tests/test_work16_1_json.py``.

=========================================  ====================================
helper                                    the question it actually asks
=========================================  ====================================
``json_value_equals``                     Does the value at this key, read as
                                          text, equal this text? Type-insensitive
                                          on purpose: a JSON ``42`` and a JSON
                                          ``"42"`` are the same id.
``json_has_key``                          Does this document have this key at
                                          all -- a question about STRUCTURE.
``json_text_contains``                    Does this substring occur in the
                                          document's serialized TEXT -- a
                                          question about TEXT, and only ever
                                          meaningful once you accept that
                                          serialization is not canonical.
``json_document_equals``                  Is this document exactly this
                                          document? Replaces the
                                          column-vs-serialized-string trap.
=========================================  ====================================

Design rules these helpers follow
---------------------------------
1. **One question, one name.** ``json_has_key`` and ``json_text_contains``
   are separate functions because they answer separate questions. Neither is
   called ``contains``.
2. **Same stored value, same rows.** Every helper is verified against a real
   SQLite and a real PostgreSQL over identical canonical rows.
3. **No silent empty results.** ``json_document_equals`` normalizes both sides
   through the same server-side canonicalizer, so a shape mismatch is a
   mismatch rather than a dialect error or a permanent zero-row query.
4. **Fail loud, not quiet.** A malformed JSON document is a data problem;
   the helpers do not swallow it, and the tests pin the behaviour on each
   backend rather than assuming it is the same.

``json_type`` and ``json_each`` are SQLite built-ins with no PostgreSQL
equivalent of the same name, and ``json_exists``/``->>`` are PostgreSQL
constructs SQLite does not have at all. That asymmetry is why the helpers
branch on dialect at all -- and why the branching lives in ONE place instead
of being re-invented at every call site.
"""

from __future__ import annotations

import json as _json
from typing import Any

from sqlalchemy import Text, cast, func, literal, or_
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "json_document_equals",
    "json_has_key",
    "json_key_text",
    "json_text_contains",
    "json_value_equals",
    "json_value_in",
]


def _normalize_path(path: str | tuple[str, ...]) -> tuple[str, ...]:
    """Accept ``"a.b"``, ``"a/b"`` or ``("a", "b")`` as a key path.

    Normalized to a tuple of keys so a key containing a dot is not silently
    split in two. Callers with a literal dotted key pass a 1-tuple.
    """
    if isinstance(path, tuple):
        keys = path
    elif isinstance(path, str):
        keys = tuple(path.replace("/", ".").split(".")) if path else ()
    else:  # pragma: no cover - defensive; misuse should be loud
        raise TypeError(f"json path must be str or tuple, got {type(path).__name__}")
    if not keys or any(k == "" for k in keys):
        raise ValueError(f"json path is empty or has an empty key: {path!r}")
    return keys


def _indexed(column: ColumnElement[Any], keys: tuple[str, ...]) -> ColumnElement[Any]:
    """``column[keys[0]][keys[1]]...`` -- SQLAlchemy's own JSON path index."""
    expr = column
    for key in keys:
        expr = expr[key]
    return expr


# ---------------------------------------------------------------------------
# 1. Value equality (the activity.py defect)
# ---------------------------------------------------------------------------


def json_value_equals(column: ColumnElement[Any], path: str | tuple[str, ...],
                      value: Any) -> ColumnElement[bool]:
    """Does the value at ``path``, read as TEXT, equal ``str(value)``?

    The fix for the ``as_string()`` divergence, and the reason it is a
    ``cast`` around ``as_string()`` rather than a bare ``as_string()``:

    =============================  ==================================  =========
    construct                      PostgreSQL                           SQLite
    =============================  ==================================  =========
    ``.as_string()``               ``CAST(col ->> 'k' AS VARCHAR)``     ``JSON_EXTRACT(col, '$."k"')``
    ``cast(.as_string(), Text)``   ``CAST(CAST(col->>'k' AS VARCHAR)     ``CAST(JSON_EXTRACT(...) AS TEXT)``
                                   AS TEXT)``
    =============================  ==================================  =========

    The outer ``CAST`` is a no-op on PostgreSQL, which already stringified,
    and is the whole fix on SQLite, where it turns INTEGER ``42`` into TEXT
    ``'42'``. Measured over rows ``{"k": "42"}``, ``{"k": 42}``, ``{"z": 9}``:

    ====================  ==========  ==========
    construct             SQLite      PostgreSQL
    ====================  ==========  ==========
    ``.as_string()``      ``[1]``     ``[1, 2]``
    ``cast(...)``         ``[1, 2]``  ``[1, 2]``
    ====================  ==========  ==========

    Type-insensitivity is deliberate and is the documented contract: a JSON
    number and a JSON string holding the same digits are the same identifier
    for filtering purposes. Callers that genuinely need type discrimination
    should read the value in Python via the loaded document.
    """
    return cast(_indexed(column, _normalize_path(path)).as_string(), Text) == str(value)


def json_value_in(column: ColumnElement[Any], path: str | tuple[str, ...],
                  values: Any) -> ColumnElement[bool]:
    """Any of ``values`` matches the value at ``path``, with the same typing
    rule as :func:`json_value_equals`.

    Written as an ``OR`` of equalities rather than ``.in_()``: ``in_()`` on the
    SQLite side would inherit the uncast ``JSON_EXTRACT`` and reintroduce the
    exact bug this module exists to remove. Measured on both backends,
    ``[1, 2, 3]`` for values ``("42", "7")`` -- the string row, the number row
    and the unrelated-but-matching row.
    """
    candidates = [str(v) for v in values]
    if not candidates:
        # An empty IN-list must match nothing, not everything. Returning a
        # false-literal keeps it a row-level predicate with no bind params.
        return literal(False)
    return or_(*[json_value_equals(column, path, v) for v in candidates])


def json_key_text(column: ColumnElement[Any], path: str | tuple[str, ...]) -> ColumnElement[str]:
    """The value at ``path`` as TEXT, portable -- the reusable core of
    :func:`json_value_equals`.

    Exposed so callers can compose (ordering, ``IS NULL``, ``LIKE``) without
    re-deriving the cast that makes SQLite agree with PostgreSQL.
    """
    return cast(_indexed(column, _normalize_path(path)).as_string(), Text)


# ---------------------------------------------------------------------------
# 2. Key containment (a STRUCTURE question)
# ---------------------------------------------------------------------------


def json_has_key(column: ColumnElement[Any], key: str,
                 dialect_name: str | None = None) -> ColumnElement[bool]:
    """Does this document have ``key`` at any depth?

    A question about document STRUCTURE, and named as such. It is not a text
    question: ``{"a": {"b": 1}}`` has key ``"b"``, and its serialized form
    contains the characters ``"b"``, but the substring and the key coincide
    often enough that using one operator for the two hides real bugs.

    ===========  ===================================  =============================
    backend      operator                             measured
    ===========  ===================================  =============================
    PostgreSQL   ``json_exists(col, '$."key"')``      ``{"k": "42"}`` -> True
    SQLite       ``json_type(col, '$."key"') IS NOT   same -> True
                NULL``
    ===========  ===================================  =============================

    ``json_exists`` is PostgreSQL 12+, and its second argument is a **jsonpath**,
    not a bare key name -- passing ``'project_id'`` directly is a server SYNTAX
    ERROR (``syntax error at end of jsonpath input``), because the text is
    parsed as a path expression. So the key is quoted and rooted with ``$``,
    which also makes a key containing a dot addressable.

    Note the asymmetry with :func:`json_value_equals`: key PRESENCE ignores
    depth, so use it to ask "is this shape known", not "is this exact field set".
    """
    if not key or not isinstance(key, str):
        raise ValueError(f"json key must be a non-empty str, got {key!r}")
    if dialect_name == "postgresql":
        return func.json_exists(
            column, literal(_json_path([], key))).is_(True)
    return func.json_type(
        column, literal(_json_path([], key))).is_not(None)


def _json_path(prefix: list[str], *keys: str) -> str:
    """A JSON path such as ``'$."target"."id"'``, valid on BOTH backends.

    Valid on both because SQLite's ``json_type`` and PostgreSQL's
    ``json_exists``/jsonpath share this syntax. Quoting each key individually
    is what makes a key containing a dot or a quote addressable, instead of
    being re-parsed as several path segments.
    """
    segments = [*prefix, *keys]
    return "$" + "".join(f".{_json.dumps(seg)}" for seg in segments)


# ---------------------------------------------------------------------------
# 3. Text containment (a TEXT question, and only that)
# ---------------------------------------------------------------------------


def json_text_contains(column: ColumnElement[Any], needle: str) -> ColumnElement[bool]:
    """Does ``needle`` occur in the document's serialized TEXT?

    A TEXT question, and deliberately not called ``contains``: the answer
    depends on the serialization (key order, whitespace), so it is only a
    meaningful predicate when the caller accepts that. Both backends render
    the document to text first and then apply ``LIKE``, which is what makes
    this one portable::

        pg      CAST(col AS TEXT) LIKE '%' || :needle || '%'
        sqlite  CAST(col AS TEXT) LIKE '%' || :needle || '%'

    Measured on both: needle ``'42'`` over ``{"k": "42"}`` and ``{"k": 42}``
    returns both rows. Use :func:`json_has_key` for "does this key exist" --
    the two are not interchangeable, and pretending otherwise is how the
    single ``contains()`` name became a portability bug.
    """
    if not isinstance(needle, str) or needle == "":
        raise ValueError("json_text_contains needs a non-empty str needle; an "
                         "empty substring matches every row and is almost "
                         "never what was meant")
    return cast(column, Text).like(f"%{needle}%")


# ---------------------------------------------------------------------------
# 4. Document equality (the column-vs-string trap)
# ---------------------------------------------------------------------------


def json_document_equals(column: ColumnElement[Any], document: dict[str, Any],
                         dialect_name: str | None = None) -> ColumnElement[bool]:
    """Is this document exactly ``document``?

    Replaces ``column == '{"a":1}'``, which is refused on PostgreSQL (42883)
    and silently matches nothing on SQLite. Both sides are normalized through
    the same server-side canonicalizer, so this compares DOCUMENT CONTENT
    rather than serialized text:

    ===========  ==========================================  ==============
    backend      expression                                 same content,
                                                            different
                                                            formatting
    ===========  ==========================================  ==============
    PostgreSQL   ``CAST(col AS JSONB) = CAST(:doc AS JSONB)`` yes
    SQLite       ``json(col) = json(:doc)``                  yes
    ===========  ==========================================  ==============

    ``jsonb`` is used on the PostgreSQL side because ``json`` has NO equality
    operator at all (that is the 42883 in the defect above), and because
    ``jsonb`` normalizes key order -- which is what lets ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` compare equal, as they do in Python.

    Key order matters on SQLite, where ``json()`` preserves insertion order.
    That is a real limitation of the backend's canonicalizer, stated here
    rather than papered over: pass a dict built the same way on both sides, or
    compare with :func:`json_value_equals` on the keys that carry identity.
    """
    if not isinstance(document, dict):
        raise TypeError(
            "json_document_equals compares against a dict. Passing a "
            "serialized string is the defect this function exists to remove: "
            "on PostgreSQL it is a 42883 and on SQLite it silently matches "
            "nothing."
        )
    payload = _json.dumps(document, sort_keys=True)
    if dialect_name == "postgresql":
        return cast(column, postgresql.JSONB) == cast(
            literal(payload), postgresql.JSONB)
    return func.json(column) == func.json(literal(payload))


def _dialect_name(obj: Any) -> str | None:
    """``.dialect.name`` of a Connection/Engine, or ``None`` when unknown."""
    dialect = getattr(obj, "dialect", None)
    return getattr(dialect, "name", None)


#: Backends this module claims portability for. Used by the tests to prove a
#: third backend is never silently accepted.
SUPPORTED_DIALECTS: frozenset[str] = frozenset({"sqlite", "postgresql"})


def dialect_of(bind: Any) -> str:
    """The dialect name for a Session, Connection, Engine or ``db.get_bind()``.

    Accepts a Session directly, so callers do not have to remember to unwrap
    it. Raises on an unrecognised backend rather than assuming SQLite, because
    assuming SQLite is how a PostgreSQL-only deployment ends up running
    SQLite-only SQL.
    """
    candidate: Any = bind
    get_bind = getattr(bind, "get_bind", None)
    if callable(get_bind):
        try:
            candidate = get_bind()
        except Exception:  # noqa: BLE001 - unbound Session; fall back to the
            candidate = bind  # Session's own dialect, may be None
    name = _dialect_name(candidate) or _dialect_name(bind)
    if name not in SUPPORTED_DIALECTS:
        raise RuntimeError(
            f"json_portability supports {sorted(SUPPORTED_DIALECTS)}, not "
            f"{name!r}. Refusing to guess: emitting SQLite-only SQL against "
            f"another backend would fail silently or not at all."
        )
    return name


__all__.append("dialect_of")
__all__.append("SUPPORTED_DIALECTS")
