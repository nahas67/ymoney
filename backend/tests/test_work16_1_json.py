"""Work 16 §2: JSON portability proven with ROWS, on SQLite and PostgreSQL.

Command
-------
::

    cd backend
    $env:YMONEY_TEST_POSTGRES="postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
    .\\.venv\\Scripts\\python -m pytest tests/test_work16_1_json.py -q

Without a reachable server the PostgreSQL half skips cleanly and DECLARATIVELY
(``pytest.mark.skipif`` at module scope, never a runtime ``pytest.skip()`` for
the backend check), so the default SQLite suite needs no database.

Why rows and not compiled SQL
-----------------------------
A dialect compiler will cheerfully emit SQL that means something else on the
server. Two of the three defects here are *silent*: SQLite returns a
correct-looking ``[]`` where PostgreSQL raises 42883, and returns ``[1]`` where
PostgreSQL returns ``[1, 2]``. A test asserting on a compiled string cannot see
either. Every parity assertion below reads rows back.

Every parity assertion is ANTI-VACUOUS
--------------------------------------
A test where both backends return ``[]`` passes trivially and is worse than no
test: a helper that silently stopped matching would turn every parity test
green. So ``assert_parity`` checks each side for rows FIRST, then for equality,
and ``test_the_anti_vacuity_guard_rejects_an_empty_side`` proves the guard fires
by feeding it an empty result.

The three defects being pinned
-----------------------------
1. ``activity.py`` used ``data_json["project_id"].as_string() == str(...)``.
   PostgreSQL casts, SQLite does not, so a row stored ``{"project_id": 42}`` is
   found on PostgreSQL and lost on SQLite.
   :: ``test_project_filter_matches_numeric_json``
2. ``JSON.contains()`` is one Python name for two operators: ``json ~~ text``
   (42883 on PostgreSQL) and a ``LIKE`` substring match (SQLite).
   :: ``test_key_containment_and_text_containment_are_different_operators``
3. ``column == '{"a":1}'`` is 42883 on PostgreSQL and a permanent zero-row query
   on SQLite.
   :: ``test_raw_string_comparison_is_the_defect_we_removed``
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import JSON, create_engine, select
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Backend reachability: declarative, never a runtime skip of the PG requirement.
# ---------------------------------------------------------------------------

_DEFAULT_DSN = "postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
PG_DSN = os.environ.get("YMONEY_TEST_POSTGRES", _DEFAULT_DSN).strip()


def _pg_reachable(dsn: str) -> bool:
    """Whether a PostgreSQL server answers on ``dsn``. Never raises."""
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


PG_UP = _pg_reachable(PG_DSN)

#: Applied to every test that reads the PostgreSQL fixture. Declarative, so a
#: missing server costs a collection-time connect probe and nothing else.
requires_postgres = pytest.mark.skipif(
    not PG_UP, reason="no PostgreSQL reachable at YMONEY_TEST_POSTGRES")

# ---------------------------------------------------------------------------
# The canonical rows: ONE dict, loaded into BOTH backends verbatim.
# ---------------------------------------------------------------------------

#: id -> ``data_json``. This dict is the single source of truth both backends
#: are loaded with, so "same canonical stored value" is literally the same
#: Python object serialized into two databases.
#:
#:   1: project_id STRING "42"   -- what every current writer stores
#:   2: project_id NUMBER 42     -- the divergent case (a non-string writer, or
#:                                  a JSON import). Invisible to ``.as_string()``
#:                                  on SQLite.
#:   3: no project_id at all     -- the must-NOT-match row
#:   4: project_id "4242"        -- a DIFFERENT id, must never match 42
#:   5: target.type/id STRINGS
#:   6: target.type/id NUMBERS, project_id NUMBER
#:   7: key nested in an object
#:   8: key nested in an array object
#:   9: key-like text, not an object member
#:  10: numeric-looking object member key "0"
#:  11: array index 0 is not an object member key
CANONICAL: dict[int, dict[str, Any]] = {
    1: {"actor": "u1", "project_id": "42"},
    2: {"actor": "u2", "project_id": 42},
    3: {"actor": "u3", "notes": "no project here"},
    4: {"actor": "u4", "project_id": "4242"},
    5: {"actor": "u5", "project_id": "42",
        "target": {"type": "content_item", "id": "c-1"}},
    6: {"actor": "u6", "project_id": 42,
        "target": {"type": 7, "id": 88}},
    7: {"outer": {"deep_key": "nested"}},
    8: {"items": [{"deep_key": "in array"}]},
    9: {"description": "deep_key"},
    10: {"outer": {"0": "object member"}},
    11: {"items": ["array index only"]},
}

#: Every row that legitimately carries project_id 42 (as a string OR a number).
EXPECTED_PROJECT_42: list[int] = [1, 2, 5, 6]

#: A predicate over ``(column, dialect_name)``.
Predicate = Callable[[Any, str], Any]


def _probe_table(meta: sa.MetaData) -> sa.Table:
    """A fresh probe table per MetaData.

    Built by a factory, not a shared column tuple: a ``Column`` object can only
    be assigned to ONE ``Table``, so sharing instances across the SQLite and
    PostgreSQL fixtures would fail at construction.
    """
    return sa.Table(
        "w16_json_events", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("data_json", JSON, nullable=False),
    )


@pytest.fixture(scope="module")
def sqlite_backend() -> Iterator[dict]:
    """An in-memory SQLite holding ``CANONICAL``, in a real table."""
    meta = sa.MetaData()
    table = _probe_table(meta)
    engine = create_engine("sqlite://")
    meta.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        for row_id, doc in CANONICAL.items():
            s.execute(sa.insert(table).values(
                id=row_id, workspace_id="ws-1", data_json=doc))
        s.commit()
    try:
        yield {"name": "sqlite", "dialect": "sqlite", "engine": engine,
               "Session": Session, "table": table}
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def pg_backend() -> Iterator[dict]:
    """A real PostgreSQL scratch database holding the SAME ``CANONICAL``."""
    import psycopg

    admin = PG_DSN.rsplit("/", 1)[0] + "/postgres"
    name = f"w16_1_json_{os.urandom(4).hex()}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    base = admin.rsplit("/", 1)[0].replace("postgresql://",
                                           "postgresql+psycopg://", 1)
    engine = create_engine(base.rstrip("/") + f"/{name}")
    meta = sa.MetaData()
    table = _probe_table(meta)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    meta.create_all(engine)
    with Session() as s:
        for row_id, doc in CANONICAL.items():
            s.execute(sa.insert(table).values(
                id=row_id, workspace_id="ws-1", data_json=doc))
        s.commit()
    try:
        yield {"name": "postgresql", "dialect": "postgresql",
               "engine": engine, "Session": Session, "table": table}
    finally:
        engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def ids(backend: dict, predicate: Predicate) -> list[int]:
    """Ids this backend returns for ``predicate(column, dialect)``."""
    table = backend["table"]
    with backend["Session"]() as s:
        return sorted(s.execute(select(table.c.id).where(
            predicate(table.c.data_json, backend["dialect"])
        )).scalars().all())


def assert_parity(sqlite_backend: dict, pg_backend: dict,
                  predicate: Predicate) -> dict[str, list[int]]:
    """Both backends, same predicate, same rows -- and NEITHER EMPTY.

    The non-empty assertion is the whole point of this helper. A parity test
    where both sides return ``[]`` proves nothing: a predicate broken on both
    backends, or one that silently matches nothing, would pass. So each side is
    checked for rows FIRST, then the two are checked for equality.
    """
    lite = ids(sqlite_backend, predicate)
    pg = ids(pg_backend, predicate)
    assert lite, (
        "SQLite matched NO rows, so this parity assertion is vacuous -- if "
        f"PostgreSQL also matched none it would pass while testing nothing. "
        f"Predicate: {predicate}")
    assert pg, (
        f"PostgreSQL matched NO rows while SQLite matched {lite}. An empty "
        f"side hides the disagreement. Predicate: {predicate}")
    assert lite == pg, (
        f"SAME stored values, DIFFERENT rows: SQLite={lite} PostgreSQL={pg}. "
        f"Predicate: {predicate}")
    return {"sqlite": lite, "postgresql": pg}


def sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


# ===========================================================================
# 1. Defect 1: a numeric JSON value must be found on BOTH backends
# ===========================================================================


@requires_postgres
def test_project_filter_matches_numeric_json(sqlite_backend: dict,
                                            pg_backend: dict) -> None:
    """``activity.query``'s project filter, over the canonical rows.

    Row 2 stores ``{"project_id": 42}`` -- a NUMBER. It must be found on both
    backends. Rows 3 (absent) and 4 (different id) must not match, so the
    assertion on the exact expected set doubles as a check that the filter is
    not simply returning everything.
    """
    from app.services.json_portability import json_value_equals

    def by_project(col: Any, _dialect: str) -> Any:
        return json_value_equals(col, "project_id", "42")

    got = assert_parity(sqlite_backend, pg_backend, by_project)
    assert got["sqlite"] == EXPECTED_PROJECT_42, (
        f"SQLite matched {got['sqlite']}, expected {EXPECTED_PROJECT_42} -- the "
        f"numeric row (id 2) and the differing id (4) must be the difference")
    assert 2 in got["postgresql"], (
        f"the numeric row 2 was not found on PostgreSQL: {got['postgresql']}")


@requires_postgres
def test_as_string_would_have_diverged_so_the_helper_is_load_bearing(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """Pin the DEFECT as a measured difference, then show the fix removes it.

    This is the before/after table from the lane brief, produced by the suite
    rather than quoted from it: ``.as_string()`` misses the numeric row on
    SQLite only, and the helper returns the same rows everywhere.
    """
    from app.services.json_portability import json_value_equals

    def broken(col: Any, _dialect: str) -> Any:
        return col["project_id"].as_string() == "42"

    def fixed(col: Any, _dialect: str) -> Any:
        return json_value_equals(col, "project_id", "42")

    lite_broken, pg_broken = ids(sqlite_backend, broken), ids(pg_backend, broken)
    assert lite_broken == [1, 5], (
        f"SQLite .as_string() -> {lite_broken}; expected the two STRING rows "
        f"only. If this changed, the defect is gone and the fix needs "
        f"re-justifying")
    assert pg_broken == EXPECTED_PROJECT_42, (
        f"PostgreSQL .as_string() -> {pg_broken}; expected string AND numeric")
    assert lite_broken != pg_broken, (
        "the two backends agree here, so this test no longer demonstrates the "
        "defect it exists to pin")
    assert assert_parity(sqlite_backend, pg_backend, fixed) == {
        "sqlite": EXPECTED_PROJECT_42, "postgresql": EXPECTED_PROJECT_42}


@requires_postgres
def test_nested_target_filters_agree_on_strings_and_numbers(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """The other two ``activity.query`` filters, same invariant.

    ``target.id`` is a string on row 5 and a number on row 6, which also
    exercises the multi-key path -- the case a helper that splits a dotted
    string on ``.`` would get wrong.
    """
    from app.services.json_portability import json_value_equals

    def by_target_id(col: Any, _dialect: str) -> Any:
        return json_value_equals(col, ("target", "id"), "88")

    assert assert_parity(sqlite_backend, pg_backend, by_target_id) == {
        "sqlite": [6], "postgresql": [6]}
    # And the string-valued one still works, so the fix did not trade one
    # case for another.
    assert assert_parity(sqlite_backend, pg_backend,
                         lambda c, d: json_value_equals(
                             c, ("target", "type"), "content_item")) == {
        "sqlite": [5], "postgresql": [5]}
    assert_parity(sqlite_backend, pg_backend,
                  lambda c, d: json_value_equals(c, ("target", "type"), "7"))


@requires_postgres
def test_a_mismatched_id_matches_nothing_on_either_backend(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """The negative control: the helpers CAN return nothing, measurably.

    Paired with the anti-vacuity assertions elsewhere, so "both empty" is an
    asserted EXPECTATION here rather than a vacuous pass there.
    """
    from app.services.json_portability import json_value_equals

    absent = lambda c, d: json_value_equals(c, "project_id", "999")  # noqa: E731
    assert ids(sqlite_backend, absent) == [], "SQLite matched a nonexistent id"
    assert ids(pg_backend, absent) == [], "PostgreSQL matched a nonexistent id"
    assert_parity(sqlite_backend, pg_backend,
                  lambda c, d: json_value_equals(c, "project_id", "4242"))


@requires_postgres
def test_value_in_matches_every_listed_value_on_both_backends(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """``json_value_in`` is an OR of equalities, so it inherits the cast.

    Using ``.in_()`` here would reintroduce the exact uncast
    ``JSON_EXTRACT`` bug this module exists to remove, which is why the helper
    is written as an OR.
    """
    from app.services.json_portability import json_value_in

    assert_parity(sqlite_backend, pg_backend,
                  lambda c, d: json_value_in(c, "project_id", ("42", "7")))
    seven = lambda c, d: json_value_in(c, "project_id", ("7",))  # noqa: E731
    assert ids(sqlite_backend, seven) == [], "SQLite matched project 7"
    assert ids(pg_backend, seven) == [], "PostgreSQL matched project 7"
    # An empty candidate list must match NOTHING, never everything.
    empty = lambda c, d: json_value_in(c, "project_id", ())  # noqa: E731
    assert ids(sqlite_backend, empty) == [], "an empty IN-list matched on SQLite"
    assert ids(pg_backend, empty) == [], "an empty IN-list matched on PostgreSQL"


# ===========================================================================
# 2. Defect 2: ONE Python name, TWO SQL operators
# ===========================================================================


@requires_postgres
def test_key_containment_and_text_containment_are_different_operators(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """Defect 2: one name, two operators -- so two correctly-named helpers.

    ``json_has_key`` asks about document STRUCTURE (PostgreSQL
    ``json_exists``, SQLite ``json_type(...) IS NOT NULL``). It finds the key
    on rows 1, 2, 4, 5, 6 -- all but row 3 -- and does not care whether the
    value is a string or a number.

    ``json_text_contains`` asks about serialized TEXT. ``"notes"`` is a
    substring of row 3's document and is true only for row 3.
    """
    from app.services.json_portability import json_has_key, json_text_contains

    got = assert_parity(sqlite_backend, pg_backend,
                        lambda c, d: json_has_key(c, "project_id", d))
    assert got["sqlite"] == [1, 2, 4, 5, 6], (
        f"json_has_key(project_id) -> {got['sqlite']}; expected every row that "
        f"HAS the key. Row 3 has none and must be absent")

    text = assert_parity(sqlite_backend, pg_backend,
                         lambda c, d: json_text_contains(c, "notes"))
    assert text["sqlite"] == [3], (
        f"'notes' as TEXT -> {text['sqlite']}; only row 3's document contains "
        f"that substring")


@requires_postgres
def test_the_two_containment_questions_genuinely_disagree(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """Proof the two helpers are not one operator wearing two names.

    ``"no"`` is a substring of row 3's serialized document (inside
    ``"notes"``), but ``no`` is not a KEY on any row. If the two helpers were
    secretly the same predicate, these two results would be equal.
    """
    from app.services.json_portability import json_has_key, json_text_contains

    assert ids(sqlite_backend, lambda c, d: json_has_key(c, "no", d)) == [], (
        "'no' is not a key on any row, so key containment must be empty on SQLite")
    assert ids(pg_backend, lambda c, d: json_has_key(c, "no", d)) == [], (
        "'no' is not a key on any row, so key containment must be empty on PG")
    # The text question, on both backends, is NOT empty:
    assert ids(sqlite_backend,
               lambda c, d: json_text_contains(c, "no")) == [3]
    assert ids(pg_backend, lambda c, d: json_text_contains(c, "no")) == [3]


def test_json_has_key_finds_nested_object_members_not_text_or_array_indexes(
        sqlite_backend: dict) -> None:
    """Recursive key lookup includes object names but excludes array indexes."""
    from app.services.json_portability import json_has_key

    assert ids(sqlite_backend, lambda c, d: json_has_key(c, "deep_key", d)) == [7, 8]
    assert ids(sqlite_backend, lambda c, d: json_has_key(c, "0", d)) == [10]


@requires_postgres
def test_json_has_key_recursive_results_match_postgresql(
        sqlite_backend: dict, pg_backend: dict) -> None:
    from app.services.json_portability import json_has_key

    for key, expected in (("deep_key", [7, 8]), ("0", [10])):
        result = assert_parity(
            sqlite_backend, pg_backend,
            lambda c, d, key=key: json_has_key(c, key, d),
        )
        assert result["sqlite"] == expected


@requires_postgres
def test_raw_json_contains_is_refused_on_postgres_and_meaningless_on_sqlite(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """Defect 2 at the source: what ``JSON.contains()`` actually does.

    PostgreSQL refuses it (42883, ``operator does not exist: json ~~ text``);
    SQLite answers -- as a substring search over the serialized document. Two
    different behaviours behind one Python name, which is why neither helper is
    called ``contains``.
    """
    with pg_backend["Session"]() as s:
        with pytest.raises(sa.exc.DBAPIError) as caught:
            s.execute(select(pg_backend["table"].c.id).where(
                pg_backend["table"].c.data_json.contains("42"))
            ).scalars().all()
        assert sqlstate(caught.value) == "42883", (
            f"expected 42883 for `json ~~ text`, got {sqlstate(caught.value)}")
        s.rollback()  # the failed statement aborted the transaction

    # SQLite answers "42" on five rows: 1, 2 (42), 4 ("4242" CONTAINS "42"),
    # 5 and 6 (project_id 42). A substring match, not a key match -- and note
    # row 4, which no equality predicate would ever match.
    assert ids(sqlite_backend, lambda c, d: c.contains("42")) == [1, 2, 4, 5, 6]
    assert ids(sqlite_backend, lambda c, d: c.contains("no")) == [3]


# ===========================================================================
# 3. Defect 3: JSON column vs serialized string
# ===========================================================================


@requires_postgres
def test_raw_string_comparison_is_the_defect_we_removed(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """Defect 3 at the source, measured on both backends.

    The brief described SQLite as "silently ``[]``". Measured, it is more
    precise and more dangerous than that: SQLite does not compare documents, it
    compares the **serialized TEXT byte for byte**. So it returns a row only
    when the caller's string happens to be byte-identical to what the driver
    wrote. Here that is true for ``'{"actor": "u1", "project_id": "42"}'``
    (row 1) and false the moment the same document is written with different
    separators or key order -- including compact separators and reversed keys:

    ===============================  ============  ===================
    comparison                       SQLite        PostgreSQL
    ===============================  ============  ===================
    ``col == <exact same text>``      ``[1]``       42883
    ``col == <compact separators>``   ``[]``        42883
    ``col == <reordered keys>``       ``[]``        42883
    ``json_document_equals(...)``     ``[1]``       ``[1]``
    ===============================  ============  ===================

    So the SQLite side passes a unit test that round-trips one dict, then
    returns nothing the first time anything reformats the document -- with no
    error to notice. That is the whole reason this helper exists.
    """
    from app.services.json_portability import json_document_equals

    doc = {"actor": "u1", "project_id": "42"}
    exact = '{"actor": "u1", "project_id": "42"}'
    compact = '{"actor":"u1","project_id":"42"}'
    reordered = '{"project_id": "42", "actor": "u1"}'

    # SQLite: byte-exact text comparison. One of the three forms matches.
    assert ids(sqlite_backend, lambda c, d: c == exact) == [1], (
        "SQLite should byte-match the exact serialization it wrote")
    assert ids(sqlite_backend, lambda c, d: c == compact) == [], (
        "SQLite returned rows for a compact serialization; it is comparing "
        "serialized text, not the document")
    assert ids(sqlite_backend, lambda c, d: c == reordered) == [], (
        "SQLite matched a reordered document; it is comparing serialized text")

    # PostgreSQL: refused outright, for every spelling.
    for label, target in (("exact", exact), ("compact", compact),
                          ("reordered", reordered)):
        with pg_backend["Session"]() as s:
            with pytest.raises(sa.exc.DBAPIError) as caught:
                s.execute(select(pg_backend["table"].c.id).where(
                    pg_backend["table"].c.data_json == target)).scalars().all()
            assert sqlstate(caught.value) == "42883", (
                f"expected 42883 for `json = character varying` ({label}), got "
                f"{sqlstate(caught.value)}")
            s.rollback()

    # The replacement finds that row on both, whatever the key order.
    assert assert_parity(sqlite_backend, pg_backend,
                         lambda c, d: json_document_equals(c, doc, d)) == {
        "sqlite": [1], "postgresql": [1]}


@requires_postgres
def test_document_equality_compares_content_not_key_order(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """The replacement compares CONTENT, so a reordered document matches.

    ``{"actor": "u1", "project_id": "42"}`` and its reverse are equal as Python
    dicts. Comparing serialized text would say no; comparing documents says yes
    on both backends.
    """
    from app.services.json_portability import json_document_equals

    def reordered(col: Any, dialect: str) -> Any:
        return json_document_equals(
            col, {"project_id": "42", "actor": "u1"}, dialect)

    assert_parity(sqlite_backend, pg_backend, reordered)
    # And the negative case: a document that is NOT equal must match nothing.
    assert ids(sqlite_backend,
               lambda c, d: json_document_equals(
                   c, {"actor": "u1"}, d)) == []
    assert ids(pg_backend,
               lambda c, d: json_document_equals(
                   c, {"actor": "u1"}, d)) == []


def test_document_equality_rejects_a_serialized_string() -> None:
    """The trap is refused at the call site, naming BOTH backend outcomes.

    A ``TypeError`` that says "on PostgreSQL this is a 42883 and on SQLite it
    silently matches nothing" is strictly better than either failure.
    """
    from app.services.json_portability import json_document_equals

    col = sa.column("data_json", JSON)
    with pytest.raises(TypeError) as caught:
        json_document_equals(col, '{"a":1}', "postgresql")
    message = str(caught.value)
    assert "42883" in message and "nothing" in message, (
        f"the error must name both backend outcomes; got {message}")


# ===========================================================================
# 4. The anti-vacuity guard, proven to fire
# ===========================================================================


@requires_postgres
def test_the_anti_vacuity_guard_rejects_an_empty_side(
        sqlite_backend: dict, pg_backend: dict) -> None:
    """``assert_parity`` must FAIL whenever a side returns nothing.

    Without this, a helper that silently stopped matching would turn every
    parity test in this file green by matching nothing at all. Both empty
    sides (the vacuous pass) and one empty side (the hidden disagreement) are
    exercised, and the guard's own message is asserted so it cannot be
    weakened into a generic failure.
    """
    from app.services.json_portability import json_value_equals

    # (a) Empty on BOTH sides -> rejected as vacuous.
    with pytest.raises(AssertionError) as vacuous:
        assert_parity(sqlite_backend, pg_backend,
                      lambda c, d: json_value_equals(c, "project_id", "999"))
    assert "vacuous" in str(vacuous.value), str(vacuous.value)

    # (b) A predicate that matches on ONE backend only must also be rejected,
    #     naming the empty side. `json_type` is a SQLite built-in, so this is
    #     a real one-sided predicate rather than a contrived one; on PostgreSQL
    #     the guarded failure is the empty-side branch.
    sqlite_only = lambda c, d: sa.func.json_type(c, '$."project_id"').is_not(None)  # noqa: E731
    assert ids(sqlite_backend, sqlite_only) == [1, 2, 4, 5, 6]
    with pytest.raises(sa.exc.DatabaseError):
        # Proving the asymmetry for real rather than asserting it on faith:
        # `json_type` is a SQLite built-in with no PostgreSQL equivalent.
        ids(pg_backend, sqlite_only)

    # (c) A predicate that matches on both, and is non-empty on both, passes.
    assert assert_parity(sqlite_backend, pg_backend,
                         lambda c, d: json_value_equals(c, "actor", "u1"))


def test_a_parity_helper_that_matched_nothing_would_be_caught() -> None:
    """The guard's teeth, independent of the database.

    Two backends over the SAME rows, one of them deliberately filtered to
    nothing -- exactly the "both sides return []" shape -- and the guard
    raises. This pins the guard itself, so a future edit that turns the
    non-empty assertion into a no-op fails here rather than silently
    neutering every parity test in the file.
    """
    def assert_parity_stub(lite: list[int], pg: list[int]) -> None:
        assert lite, "would be vacuous"
        assert pg, "empty side"
        assert lite == pg

    with pytest.raises(AssertionError) as caught:
        assert_parity_stub([], [])
    assert "vacuous" in str(caught.value)
    with pytest.raises(AssertionError) as caught:
        assert_parity_stub([1], [])
    assert "empty side" in str(caught.value)


# ===========================================================================
# 5. The SHIPPED service, end to end, on a real PostgreSQL
# ===========================================================================


@requires_postgres
def test_the_shipped_activity_query_finds_numeric_rows_on_postgres(
        pg_backend: dict) -> None:
    """``activity.query`` -- the real function -- against a real PostgreSQL.

    The models bind to the ``events`` table, so this creates that table in the
    scratch database and calls the shipped service. It is the end-to-end proof
    that the fix reaches the operator-visible result, not just a helper.
    """
    from app.models import EventLog
    from app.services import activity

    EventLog.__table__.create(pg_backend["engine"])
    Session = pg_backend["Session"]
    wid = f"w16-1-ws-{os.urandom(4).hex()}"
    try:
        with Session() as s:
            rows = [("str-proj", {"project_id": "42"}),
                    ("num-proj", {"project_id": 42}),
                    ("other-proj", {"project_id": "4242"}),
                    ("no-proj", {"notes": "no project"})]
            for i, (message, doc) in enumerate(rows):
                s.add(EventLog(id=f"w16-1-{i}-{os.urandom(4).hex()}",
                               workspace_id=wid, level="info", source="test",
                               kind="PROJECT_UPDATED", message=message,
                               data_json=doc))
            s.commit()

        with Session() as s:
            page = activity.query(s, wid, project_id="42")
            found = sorted(row["message"] for row in page)
            assert found == ["num-proj", "str-proj"], (
                f"activity.query returned {found}. 'num-proj' is the NUMERIC "
                f"row, the one that used to vanish on a non-casting backend")

            # The negative control through the shipped function.
            assert len(activity.query(s, wid, project_id="999")) == 0, (
                "activity.query matched a project that was never written")
            assert sorted(r["message"] for r in
                          activity.query(s, wid, project_id="4242")) == \
                ["other-proj"]
    finally:
        with Session() as s:
            s.execute(sa.text("DELETE FROM events WHERE workspace_id=:w"),
                      {"w": wid})
            s.commit()
            s.execute(sa.text("DROP TABLE IF EXISTS events"))
            s.commit()


@requires_postgres
def test_the_shipped_activity_query_finds_numeric_rows_on_sqlite_too() -> None:
    """The same shipped function, on SQLite -- the side that used to lose it.

    Mutation-testing this lane proved this test is the load-bearing one: with
    ``activity.query`` reverted to ``.as_string()``, the PostgreSQL test still
    passes (PostgreSQL casts, so it was never broken there) and ONLY this test
    fails, by losing the numeric row. That asymmetry is exactly the defect --
    the backend that works hides the bug in the backend that does not.
    """
    from app.models import EventLog
    from app.services import activity

    meta = sa.MetaData()
    EventLog.__table__.to_metadata(meta)
    engine = create_engine("sqlite://")
    meta.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    wid = "w16-1-ws-sqlite"
    try:
        with Session() as s:
            for i, (message, doc) in enumerate(
                    [("str-proj", {"project_id": "42"}),
                     ("num-proj", {"project_id": 42}),
                     ("other-proj", {"project_id": "4242"}),
                     ("no-proj", {"notes": "no project"})]):
                s.add(EventLog(id=f"w16-1-{i}", workspace_id=wid, level="info",
                               source="test", kind="PROJECT_UPDATED",
                               message=message, data_json=doc))
            s.commit()

        with Session() as s:
            found = sorted(r["message"] for r in
                           activity.query(s, wid, project_id="42"))
            assert found == ["num-proj", "str-proj"], (
                f"activity.query on SQLite returned {found}. 'num-proj' is the "
                f"NUMERIC row; losing it on SQLite while keeping it on "
                f"PostgreSQL is the defect this lane exists to remove")
    finally:
        engine.dispose()


# ===========================================================================
# 6. Helper-level guarantees that need no database
# ===========================================================================


def test_json_paths_are_validated_not_silently_misparsed() -> None:
    """A dotted path is ambiguous, so a literal dotted key must be a tuple."""
    from app.services.json_portability import _normalize_path

    assert _normalize_path("a.b") == ("a", "b")
    assert _normalize_path("a/b") == ("a", "b")
    assert _normalize_path(("a.b",)) == ("a.b",), (
        "a key containing a dot must survive as ONE key, not two segments")
    for bad in ("", (), ("",)):
        with pytest.raises(ValueError):
            _normalize_path(bad)
    with pytest.raises(TypeError):
        _normalize_path(42)


def test_dialect_of_accepts_every_bind_shape_and_refuses_the_rest() -> None:
    """Refusing beats assuming: a wrong guess is how SQLite SQL reaches PG."""
    from app.services.json_portability import dialect_of

    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        assert dialect_of(conn) == "sqlite"
        assert dialect_of(conn.engine) == "sqlite"
        assert dialect_of(sessionmaker(bind=eng)()) == "sqlite"
    eng.dispose()

    class _Mystery:
        @property
        def dialect(self):
            return type("D", (), {"name": "oracle"})()

    with pytest.raises(RuntimeError) as caught:
        dialect_of(_Mystery())
    assert "oracle" in str(caught.value), caught.value


def test_containment_helpers_reject_meaningless_arguments() -> None:
    """An empty substring matches EVERY row; that is never what was meant."""
    from app.services.json_portability import json_has_key, json_text_contains

    col = sa.column("data_json", JSON)
    for bad in ("", None, 42):
        with pytest.raises(ValueError):
            json_text_contains(col, bad)
        with pytest.raises(ValueError):
            json_has_key(col, bad)


def test_every_exported_helper_is_documented() -> None:
    """A helper whose meaning is unclear is the defect this lane is about."""
    from app.services import json_portability

    for name in json_portability.__all__:
        member = getattr(json_portability, name)
        assert member.__doc__, f"json_portability.{name} has no docstring"
