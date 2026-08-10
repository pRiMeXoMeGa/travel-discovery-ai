"""Unit tests for the review-intelligence agent's sampler (WS0-B).

Self-contained on purpose: this file does NOT rely on `conftest.py`, a
`pyproject.toml`, a database, or any network. Heavy third-party imports and the
agent's sibling modules are stubbed in `sys.modules` before the module under
test is imported, so `pytest backend/tests/test_review_intel.py` works from a
bare interpreter with only pytest installed.

What is covered:
  * the pure query builders (SQL shape, ORDER BY determinism, parameterization —
    no user input is ever concatenated into the SQL string);
  * the aspect-polarity sampling semantics that replace the dead
    `ORDER BY rating` sample (`reviews.rating` is always NULL in this corpus);
  * snippet mapping + evidence signal (sentiment/aspects, never `rating`);
  * `_fallback_summary` on the [-1, 1] sentiment scale;
  * `_retrieve_review_snippets` against a fake pool: contract, dedup, cap,
    FTS-then-fallback ordering, and multilingual (null-aspect) survival.
"""
import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

# ── Import scaffolding ────────────────────────────────────────────────────────
# backend/ on sys.path so `app.agents.review_intel` resolves.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _stub(name: str, **attrs) -> types.ModuleType:
    """Register (or reuse) a stub module in sys.modules."""
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# Heavy third-party deps the real app modules would pull in.
for _heavy in ("fastembed", "qdrant_client", "redis", "asyncpg"):
    _stub(_heavy)
_stub("redis.asyncio")
sys.modules["redis"].asyncio = sys.modules["redis.asyncio"]


class _LLMError(Exception):
    """Stand-in for app.llm.LLMError."""


class _Citation:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


class _AgentStep:
    def __init__(self, *args, **kwargs):
        self.input_tokens = 0
        self.output_tokens = 0
        self.detail = None


async def _unused(*args, **kwargs):  # pragma: no cover - never called in tests
    raise AssertionError("network/db call escaped the stub")


# Sibling app modules — stubbed so importing the agent touches no I/O.
import app  # noqa: E402  (must follow the sys.path insert above)

for _name, _attrs in {
    "app.llm": {"LLMError": _LLMError, "complete_text_with_usage": _unused},
    "app.cache": {"cache_get": _unused, "cache_set": _unused},
    "app.config": {"settings": types.SimpleNamespace(cache_ttl_seconds=300)},
    "app.db": {"get_pool": _unused},
    "app.observability": {"AgentStep": _AgentStep},
    "app.schemas": {"Citation": _Citation},
    "app.embeddings": {"embed_query": _unused},
    "app.vectorstore": {"get_qdrant": _unused},
}.items():
    setattr(app, _name.split(".")[1], _stub(_name, **_attrs))

from app.agents import review_intel as ri  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _order_by_clauses(sql: str) -> list[str]:
    """Every ORDER BY clause in the query, each truncated at ')' or newline."""
    return [m.strip() for m in re.findall(r"ORDER BY ([^)\n]+)", sql)]


def _placeholders(sql: str) -> list[int]:
    return sorted({int(n) for n in re.findall(r"\$(\d+)", sql)})


def _row(
    rid: str,
    *,
    language: str = "en",
    text: str = "lovely place",
    sentiment: float | None = 0.5,
    aspects: dict | None = None,
) -> dict:
    return {
        "id": rid,
        "listing_id": "l1",
        "text": text,
        "language": language,
        "sentiment": sentiment,
        "aspects": aspects,
    }


def _snippet(
    rid: str,
    *,
    sentiment: float | None = None,
    praised: list[str] | None = None,
    criticised: list[str] | None = None,
    language: str = "en",
) -> dict:
    return {
        "id": rid,
        "language": language,
        "text": f"text of {rid}",
        "sentiment": sentiment,
        "praised": praised or [],
        "criticised": criticised or [],
    }


class _FakeConn:
    """Records every (sql, params) and returns queued result sets in order."""

    def __init__(self, results: list[list[dict]]):
        self._results = list(results)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self._results.pop(0) if self._results else []


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _patch_pool(monkeypatch, results: list[list[dict]]) -> _FakeConn:
    conn = _FakeConn(results)

    async def _get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(ri, "get_pool", _get_pool)
    return conn


# ── build_polarity_sample_query ───────────────────────────────────────────────

def test_polarity_halves_use_different_order_by():
    """The praise half and the complaint half must not sort identically.

    This is the WS0-B defect: with `ORDER BY rating DESC/ASC NULLS LAST` over an
    all-NULL column both halves returned the same arbitrary rows.
    """
    sql, _ = ri.build_polarity_sample_query(["l1"])
    clauses = _order_by_clauses(sql)

    pos = [c for c in clauses if "pos_peak" in c]
    neg = [c for c in clauses if "neg_peak" in c]
    assert pos and neg, clauses
    assert set(pos).isdisjoint(set(neg))
    assert "ORDER BY lang_rank, pos_peak DESC, id" in sql
    assert "ORDER BY lang_rank, neg_peak ASC, id" in sql


def test_polarity_selects_on_aspect_presence_not_rating():
    sql, _ = ri.build_polarity_sample_query(["l1"])
    assert "pos_peak > 0" in sql
    assert "neg_peak < 0" in sql
    # jsonb_each is guarded so NULL / non-object `aspects` can never raise.
    assert "jsonb_typeof(r.aspects) = 'object'" in sql
    assert "jsonb_typeof(e.value) = 'number'" in sql
    # rating is always NULL in this corpus — it must not drive anything.
    assert "rating" not in sql


def test_polarity_never_filters_on_sentiment():
    """sentiment is NULL for non-English rows; ordering on it would drop them."""
    sql, _ = ri.build_polarity_sample_query(["l1"])
    assert "NULLS LAST" not in sql
    assert "ORDER BY sentiment" not in sql
    for clause in _order_by_clauses(sql):
        assert "sentiment" not in clause


def test_every_order_by_is_deterministic_on_id():
    """`, id` on every ORDER BY => stable output across runs."""
    sql, _ = ri.build_polarity_sample_query(["l1"])
    clauses = _order_by_clauses(sql)
    assert clauses
    for clause in clauses:
        assert clause.split(",")[-1].strip() == "id", clause
    assert "ORDER BY bucket, ord, id" in sql


def test_language_diversity_is_the_tiebreak():
    sql, _ = ri.build_polarity_sample_query(["l1"])
    assert sql.count("PARTITION BY language") == 3   # pos, neg, filler
    # The per-language rank is the FIRST ordering key of each bucket.
    for clause in _order_by_clauses(sql):
        if "lang_rank" in clause:
            assert clause.startswith("lang_rank")


def test_polarity_params_are_numbered_and_complete():
    ids = ["a", "b", "c"]
    sql, params = ri.build_polarity_sample_query(ids, half=4, limit=8)
    assert params == ["a", "b", "c", 4, 8]
    # $1..$5 all used, none skipped, none dangling.
    assert _placeholders(sql) == [1, 2, 3, 4, 5]
    assert "$1, $2, $3" in sql
    assert "ord <= $4" in sql
    assert "ord <= $5" in sql


def test_polarity_never_inlines_user_input():
    hostile = "'; DROP TABLE reviews; --"
    sql, params = ri.build_polarity_sample_query([hostile])
    assert hostile not in sql
    assert "DROP TABLE" not in sql
    assert params[0] == hostile
    assert _placeholders(sql) == [1, 2, 3]


def test_polarity_builder_is_pure_and_repeatable():
    first = ri.build_polarity_sample_query(["l1", "l2"])
    second = ri.build_polarity_sample_query(["l1", "l2"])
    assert first == second


def test_polarity_empty_ids_yields_a_matchless_predicate():
    sql, params = ri.build_polarity_sample_query([])
    assert "listing_id IN (NULL)" in sql   # valid SQL, matches nothing
    assert params == [ri._HALF, ri._MAX_SNIPPETS]


def test_polarity_has_a_filler_bucket_for_null_aspect_rows():
    """Listings whose reviews are all non-English must still yield evidence."""
    sql, _ = ri.build_polarity_sample_query(["l1"])
    assert "rest_ranked AS" in sql
    assert "2 AS bucket" in sql
    # the filler bucket reads `base` unfiltered
    assert re.search(r"rest_ranked AS \(\s*SELECT \*.*FROM base\s*\)", sql, re.S)


# ── build_focus_query ─────────────────────────────────────────────────────────

def test_focus_query_parametrizes_focus_and_limit():
    sql, params = ri.build_focus_query(["l1", "l2"], "noisy street", limit=5)
    assert params == ["l1", "l2", "noisy street", 5]
    assert "noisy street" not in sql
    assert _placeholders(sql) == [1, 2, 3, 4]
    assert "LIMIT $4" in sql


def test_focus_query_is_fts_ranked_and_deterministic():
    sql, _ = ri.build_focus_query(["l1"], "clean")
    assert "to_tsvector('simple', text) @@ plainto_tsquery('simple', $2)" in sql
    assert "ORDER BY ts_rank(" in sql
    # `, id` terminates the sort so equal ts_ranks are broken deterministically.
    order_by = sql.split("ORDER BY", 1)[1].split("LIMIT")[0].strip()
    assert order_by.endswith(", id")
    assert "rating" not in sql


def test_focus_query_never_inlines_user_input():
    sql, params = ri.build_focus_query(["l1"], "'; DROP TABLE reviews; --")
    assert "DROP TABLE" not in sql
    assert params[-2] == "'; DROP TABLE reviews; --"


def test_both_builders_select_the_same_columns():
    focus_sql, _ = ri.build_focus_query(["l1"], "x")
    poly_sql, _ = ri.build_polarity_sample_query(["l1"])
    for col in ("id", "language", "text", "sentiment", "aspects"):
        assert col in focus_sql and col in poly_sql


# ── _split_aspects / _row_to_snippet / _evidence_signal ───────────────────────

@pytest.mark.parametrize(
    "aspects,expected",
    [
        (None, ([], [])),                                     # non-English row
        ({}, ([], [])),
        ({"cleanliness": None, "noise": None}, ([], [])),      # nothing mentioned
        ({"cleanliness": 0.5, "noise": -1.0}, (["cleanliness"], ["noise"])),
        ({"value": 0.0}, ([], [])),                            # 0 is neither
        ({"staff": True}, ([], [])),                           # bools are not scores
        ("not-a-dict", ([], [])),
    ],
)
def test_split_aspects(aspects, expected):
    assert ri._split_aspects(aspects) == expected


def test_row_to_snippet_keeps_the_contract_and_truncates():
    long_text = "x" * (ri._SNIPPET_CHARS + 50)
    snip = ri._row_to_snippet(
        _row("r1", text=long_text, sentiment=0.25, aspects={"noise": -0.5})
    )
    assert set(snip) >= {"id", "language", "text"}
    assert len(snip["text"]) == ri._SNIPPET_CHARS
    assert snip["sentiment"] == 0.25
    assert snip["criticised"] == ["noise"]
    assert "rating" not in snip


def test_row_to_snippet_tolerates_null_text_and_sentiment():
    snip = ri._row_to_snippet(_row("r1", text=None, sentiment=None, aspects=None))
    assert snip["text"] == ""
    assert snip["sentiment"] is None


def test_evidence_signal_surfaces_real_signal_not_na_rating():
    line = ri._evidence_signal(
        _snippet("r1", sentiment=0.33, praised=["cleanliness"], criticised=["noise"])
    )
    assert "sentiment +0.33" in line
    assert "praises cleanliness" in line
    assert "complaints noise" in line
    assert "n/a" not in line


def test_evidence_signal_for_unscored_row():
    line = ri._evidence_signal(_snippet("r1", language="fr"))
    assert line == "sentiment n/a, no aspect signal"


# ── _classify_snippet / _fallback_summary ─────────────────────────────────────

@pytest.mark.parametrize(
    "snippet,expected",
    [
        (_snippet("a", sentiment=0.6), "positive"),
        (_snippet("b", sentiment=-0.6), "negative"),
        (_snippet("c", sentiment=0.0), "mixed"),
        (_snippet("d", sentiment=ri._SENTIMENT_POS), "positive"),
        (_snippet("e", sentiment=ri._SENTIMENT_NEG), "negative"),
        (_snippet("f", praised=["staff"]), "positive"),
        (_snippet("g", criticised=["noise"]), "negative"),
        (_snippet("h", praised=["staff"], criticised=["noise"]), "mixed"),
        (_snippet("i"), "unscored"),
    ],
)
def test_classify_snippet_uses_the_minus_one_to_one_scale(snippet, expected):
    assert ri._classify_snippet(snippet) == expected


def test_fallback_summary_distinguishes_positive_negative_and_mixed():
    positive = [
        _snippet("p1", sentiment=0.8, praised=["cleanliness"]),
        _snippet("p2", sentiment=0.5, praised=["cleanliness", "staff"]),
    ]
    negative = [
        _snippet("n1", sentiment=-0.8, criticised=["noise"]),
        _snippet("n2", sentiment=-0.5, criticised=["noise", "value"]),
    ]
    mixed = positive[:1] + negative[:1]

    pos_text = ri._fallback_summary(positive, {})
    neg_text = ri._fallback_summary(negative, {})
    mix_text = ri._fallback_summary(mixed, {})

    assert len({pos_text, neg_text, mix_text}) == 3

    assert "2 lean positive" in pos_text and "raise concerns" not in pos_text
    assert "cleanliness" in pos_text

    assert "2 raise concerns" in neg_text and "lean positive" not in neg_text
    assert "noise" in neg_text

    assert "1 lean positive" in mix_text and "1 raise concerns" in mix_text

    # The old bug: every review fell through to this line, always.
    for text in (pos_text, neg_text, mix_text):
        assert "ratings are mixed or unavailable" not in text


def test_fallback_summary_emits_resolvable_r_labels():
    """Labels must match `synthesize`'s 1-based [r#] scheme so citations resolve."""
    snippets = [
        _snippet("id-a", sentiment=0.9),
        _snippet("id-b", sentiment=-0.9),
        _snippet("id-c", sentiment=0.05),
    ]
    text = ri._fallback_summary(snippets, {})
    assert "[r1]" in text and "[r2]" in text and "[r3]" in text
    # citation extraction in synthesize() looks for exactly this form
    label_to_id = {f"r{i}": s["id"] for i, s in enumerate(snippets, start=1)}
    cited = [lab for lab in label_to_id if f"[{lab}]" in text]
    assert sorted(cited) == ["r1", "r2", "r3"]


def test_fallback_summary_is_honest_about_unscored_only_sets():
    snippets = [_snippet("x", language="nl"), _snippet("y", language="de")]
    text = ri._fallback_summary(snippets, {})
    assert "no usable sentiment signal" in text
    assert "non-English" in text
    assert "[r1]" in text and "[r2]" in text


def test_fallback_summary_handles_no_snippets():
    assert "nothing to summarize" in ri._fallback_summary([], {})


# ── _retrieve_review_snippets (fake pool) ─────────────────────────────────────

def test_retrieve_uses_focus_query_when_focus_given(monkeypatch):
    conn = _patch_pool(monkeypatch, [[_row("r1", aspects={"noise": -0.4})]])
    out = asyncio.run(ri._retrieve_review_snippets(["l1"], "noise"))

    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "ts_rank" in sql
    assert params == ("l1", "noise", ri._MAX_SNIPPETS)
    assert out[0]["criticised"] == ["noise"]


def test_retrieve_falls_back_to_polarity_sample_when_fts_misses(monkeypatch):
    conn = _patch_pool(monkeypatch, [[], [_row("r1")]])
    out = asyncio.run(ri._retrieve_review_snippets(["l1"], "unmatchable"))

    assert len(conn.calls) == 2
    assert "ts_rank" in conn.calls[0][0]
    assert "pos_peak" in conn.calls[1][0]
    assert [s["id"] for s in out] == ["r1"]


def test_retrieve_skips_fts_when_focus_is_blank(monkeypatch):
    conn = _patch_pool(monkeypatch, [[_row("r1")]])
    asyncio.run(ri._retrieve_review_snippets(["l1"], "   "))
    assert len(conn.calls) == 1
    assert "pos_peak" in conn.calls[0][0]


def test_retrieve_dedupes_overlapping_buckets_and_caps(monkeypatch):
    """A mixed review appears in both halves; it must be returned once."""
    rows = [_row("dup", aspects={"cleanliness": 0.5, "noise": -0.5})]
    rows += [_row(f"r{i}") for i in range(12)]
    rows += [_row("dup", aspects={"cleanliness": 0.5, "noise": -0.5})]
    conn = _patch_pool(monkeypatch, [rows])

    out = asyncio.run(ri._retrieve_review_snippets(["l1"], None))

    ids = [s["id"] for s in out]
    assert len(ids) == ri._MAX_SNIPPETS
    assert len(set(ids)) == len(ids)
    assert ids[0] == "dup"   # first occurrence wins, bucket order preserved
    assert conn.calls


def test_retrieve_preserves_multilingual_evidence(monkeypatch):
    """Non-English rows (null aspects AND null sentiment) must survive."""
    rows = [
        _row("en1", language="en", sentiment=0.6, aspects={"cleanliness": 0.6}),
        _row("fr1", language="fr", sentiment=None, aspects=None),
        _row("de1", language="de", sentiment=None, aspects=None),
    ]
    _patch_pool(monkeypatch, [rows])
    out = asyncio.run(ri._retrieve_review_snippets(["l1"], None))
    assert {s["language"] for s in out} == {"en", "fr", "de"}


def test_retrieve_returns_empty_without_listing_ids(monkeypatch):
    conn = _patch_pool(monkeypatch, [[_row("r1")]])
    assert asyncio.run(ri._retrieve_review_snippets([], None)) == []
    assert conn.calls == []


def test_retrieve_snippet_contract(monkeypatch):
    _patch_pool(monkeypatch, [[_row("r1", aspects={"staff": 0.7})]])
    out = asyncio.run(ri._retrieve_review_snippets(["l1"], None))
    assert len(out) == 1
    snip = out[0]
    assert snip["id"] == "r1"
    assert snip["language"] == "en"
    assert snip["text"] == "lovely place"
    assert snip["praised"] == ["staff"]
