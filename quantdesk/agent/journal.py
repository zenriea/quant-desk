"""The analyst journal. Entries reference the records they are based on (``refs``), and
LLM-written entries link to the stored prompt/response (``llm_call_id``) for audit."""
from __future__ import annotations

import queue
import threading

from .. import clock, db

KINDS = {"observation", "research", "hypothesis", "test", "decision", "intent", "order", "fill",
         "risk", "review", "data", "system", "answer", "belief"}

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()


def write(kind: str, title: str, body: str, refs: dict | None = None, author: str = "rules",
          llm_call_id: int | None = None, dedupe_key: str | None = None) -> int | None:
    assert kind in KINDS, kind
    ts = clock.now()
    try:
        jid = db.ex("INSERT INTO journal(ts, kind, title, body, refs, author, llm_call_id, dedupe_key) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (ts, kind, title[:300], body[:8000], db.dumps(refs or {}), author, llm_call_id, dedupe_key))
    except Exception as e:  # duplicate dedupe_key -> already journaled (e.g. after restart)
        if "UNIQUE" in str(e):
            return None
        raise
    return jid


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_clause(query: str) -> tuple[str, list]:
    """Every word must match. ``#12`` matches an entry that references record 12 (any kind) or model call 12;
    other words match the title, body, kind, author or a referenced record type (``strategy``, ``experiment``)."""
    parts, params = [], []
    for word in query.lower().split()[:8]:
        if word.startswith("#") and word[1:].isdigit():
            parts.append("(EXISTS (SELECT 1 FROM json_each(journal.refs) WHERE json_each.type IN ('integer', 'text') "
                         "AND CAST(json_each.value AS TEXT) = ?) OR llm_call_id = ?)")
            params += [str(int(word[1:])), int(word[1:])]
            continue
        like = _like(word)
        key = like[1:]  # a record type: "strategy" matches the strategy_id key
        parts.append("(lower(title) LIKE ? ESCAPE '\\' OR lower(body) LIKE ? ESCAPE '\\' OR kind LIKE ? ESCAPE '\\' "
                     "OR replace(author, '_', ' ') LIKE ? ESCAPE '\\' "
                     "OR EXISTS (SELECT 1 FROM json_each(journal.refs) WHERE json_each.key LIKE ? ESCAPE '\\'))")
        params += [like, like, like, like, key]
    return " AND ".join(parts), params


def recent(limit: int = 100, after_id: int = 0, kinds: list[str] | None = None, query: str | None = None,
           before_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM journal WHERE id>?"
    params: list = [after_id]
    if before_id:
        sql += " AND id<?"
        params.append(before_id)
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if query and query.strip():
        clause, qp = search_clause(query)
        if clause:
            sql += " AND " + clause
            params += qp
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [db.row_dict(r, ["refs"]) for r in db.q(sql, params)]


def since(after_id: int, limit: int = 50) -> list[dict]:
    """Oldest unseen entries first: bounded stream batches must not skip a backlog."""
    return [db.row_dict(r, ["refs"]) for r in db.q(
        "SELECT * FROM journal WHERE id>? ORDER BY id ASC LIMIT ?", (after_id, limit))]


def set_status(activity: str, detail: str = "", next_actions: list | None = None) -> None:
    db.ex("INSERT INTO agent_status(id, activity, detail, updated_at, next_actions) VALUES(1,?,?,?,?) "
          "ON CONFLICT(id) DO UPDATE SET activity=excluded.activity, detail=excluded.detail, "
          "updated_at=excluded.updated_at, next_actions=COALESCE(excluded.next_actions, agent_status.next_actions)",
          (activity, detail, clock.now(), db.dumps(next_actions) if next_actions is not None else None))
