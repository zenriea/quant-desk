import pytest


@pytest.fixture()
def client(env, monkeypatch):
    import importlib
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "correct horse")
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    appmod = importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_auth_and_csrf(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 200
    r = client.get("/api/state")
    assert r.status_code == 200 and r.json()["mode"]["paper"] is True
    assert client.post("/api/control/pause").status_code == 403            # missing X-QD header
    assert client.post("/api/control/pause", headers={"X-QD": "1"}).status_code == 200
    r = client.post("/api/settings/risk", json={"max_gross_pct": 3}, headers={"X-QD": "1"})
    assert r.status_code == 400                                              # above hard ceiling
    r = client.post("/api/settings/agent", json={"paused": False}, headers={"X-QD": "1"})
    assert r.status_code == 400
    from quantdesk import db
    text = client.get("/api/system").text + client.get("/api/state").text
    assert "correct horse" not in text                      # password never echoed
    assert db.q1("SELECT token_digest FROM web_sessions")["token_digest"] not in text


def test_stale_data_alert_ignores_symbols_dropped_from_universe(client):
    from quantdesk.marketdata import store
    store.set_status("bars:ROKU", False, "delisted")
    store.set_status("bars:BTC-USD", False, "stale")
    client.post("/api/login", json={"password": "correct horse"})
    keys = [d["key"] for d in client.get("/api/state").json()["data"]["problems"]]
    assert "bars:BTC-USD" in keys and "bars:ROKU" not in keys


def test_login_rate_limit(client):
    codes = [client.post("/api/login", json={"password": "x"}).status_code for _ in range(7)]
    assert 429 in codes


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "QuantDesk" in r.text
    assert "Content-Security-Policy" in r.headers


def test_refreshed_ui_assets_and_module_are_served(client):
    import re
    page = client.get("/")
    assert 'class="sidebar"' in page.text and 'id="equity-ranges"' in page.text and 'id="pipeline-body"' in page.text
    assets = dict(re.findall(r'"/static/(app\.(?:css|js))\?v=([\w-]+)"', page.text))
    assert assets.keys() == {"app.css", "app.js"} and assets["app.css"] == assets["app.js"]
    module = client.get(f"/static/app.js?v={assets['app.js']}")
    assert f"./ui-model.mjs?v={assets['app.js']}" in module.text
    for path in ("app.css", "app.js", "ui-model.mjs"):
        response = client.get(f"/static/{path}?v={assets['app.js']}")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-cache"
    assert "javascript" in client.get("/static/ui-model.mjs").headers["content-type"]


def test_equity_downsampling_has_strictly_ascending_unique_timestamps(client):
    from quantdesk import db
    client.post("/api/login", json={"password": "correct horse"})
    for i in range(50):
        db.ex("INSERT INTO equity_snapshots(ts, equity, cash, gross, crypto_gross, peak_equity) VALUES(?,?,?,?,?,?)",
              (1000 + i * 60, 100000 + i, 100000 + i, 0, 0, 100000 + i))
    rows = client.get("/api/equity?points=10").json()
    ts = [r["ts"] for r in rows]
    assert len(ts) == 10 and ts[0] == 1000 and ts[-1] == 1000 + 49 * 60
    assert all(a < b for a, b in zip(ts, ts[1:]))


def test_gate_presets_require_auth_csrf_and_write_an_audit_record(client):
    from quantdesk import db, settings
    from quantdesk.config import GATE_PRESETS
    path = "/api/gate-presets/strict"
    assert client.post(path, headers={"X-QD": "1"}).status_code == 401
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post(path).status_code == 403
    assert client.post("/api/gate-presets/unknown", headers={"X-QD": "1"}).status_code == 400
    assert client.post(path, headers={"X-QD": "1"}).json() == GATE_PRESETS["strict"]
    assert settings.get("gates") == GATE_PRESETS["strict"]
    assert db.q1("SELECT COUNT(*) n FROM audit_log WHERE action='settings.gates'")["n"] == 1
    system = client.get("/api/system").json()
    assert system["gate_profile"] == "strict" and system["gate_presets"] == GATE_PRESETS
    client.post("/api/settings/gates", json={"min_test_sharpe": 0.6}, headers={"X-QD": "1"})
    assert client.get("/api/system").json()["gate_profile"] == "custom"


def test_causal_dsl_requirement_must_be_boolean(client):
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post("/api/settings/gates", json={"require_causal_dsl": "false"},
                       headers={"X-QD": "1"}).status_code == 400


def test_legacy_gate_settings_inherit_defaults_and_keep_exploratory_label(client):
    from quantdesk import db
    from quantdesk.config import GATE_PRESETS
    legacy = dict(GATE_PRESETS["exploratory"])
    del legacy["require_causal_dsl"]
    db.ex("INSERT INTO settings(key, value) VALUES('gates', ?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (db.dumps(legacy),))
    client.post("/api/login", json={"password": "correct horse"})
    system = client.get("/api/system").json()
    assert system["settings"]["gates"] == GATE_PRESETS["exploratory"]
    assert system["gate_profile"] == "exploratory"
    # Read-time defaults must not rewrite the operator's saved settings.
    assert "require_causal_dsl" not in db.loads(db.q1("SELECT value FROM settings WHERE key='gates'")["value"])


@pytest.mark.parametrize("path", ["/api/equity?points=0", "/api/orders?limit=-1",
                                  "/api/fills?limit=1000000", "/api/candles?symbol=AAPL&limit=0",
                                  "/api/experiments?limit=-1", "/api/journal?limit=-1"])
def test_query_bounds(client, path):
    client.post("/api/login", json={"password": "correct horse"})
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("body", [[], None, "password", {"password": ["wrong type"]}])
def test_malformed_login_is_a_client_error(client, body):
    assert client.post("/api/login", json=body).status_code == 422


def test_cannot_clear_a_broken_ledger_halt(client):
    from quantdesk import db
    db.set_meta("system_halt", "1")
    db.ex("INSERT INTO cash_ledger(ts, delta, kind) VALUES(0, -100, 'fee')")
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post("/api/control/clear_system_halt", headers={"X-QD": "1"}).status_code == 409
    assert db.get_meta("system_halt") == "1"


def test_local_passwordless_api_rejects_foreign_host_and_origin(client, monkeypatch):
    import dataclasses
    import quantdesk.api.app as appmod
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, admin_password=""))
    assert client.get("/api/state", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.post("/api/control/pause", headers={"X-QD": "1", "Origin": "https://attacker.example"}).status_code == 403


def test_journal_catchup_reads_oldest_unseen_first(client):
    from quantdesk.agent import journal
    ids = [journal.write("system", f"entry {i}", "test") for i in range(125)]
    first = journal.since(0, limit=50)
    second = journal.since(first[-1]["id"], limit=50)
    third = journal.since(second[-1]["id"], limit=50)
    assert [r["id"] for r in first + second + third] == ids


def test_historical_detail_routes_are_authenticated_and_do_not_depend_on_list_window(client):
    from conftest import make_strategy
    from test_ledger import _order
    from quantdesk.trading import ledger
    sid = make_strategy()
    old = _order(sid, "AAPL", "buy", 1)
    fid = ledger.apply_fill(old, 1, 100, 0, {})
    newest = _order(sid, "AAPL", "buy", 1)
    assert client.get(f"/api/orders/{old['id']}").status_code == 401
    assert client.get(f"/api/strategies/{sid}").status_code == 401
    client.post("/api/login", json={"password": "correct horse"})
    assert client.get("/api/orders?limit=1").json()[0]["id"] == newest["id"]
    detail = client.get(f"/api/orders/{old['id']}").json()
    assert detail["id"] == old["id"] and detail["fills"][0]["id"] == fid
    assert client.get(f"/api/strategies/{sid}").json()["id"] == sid
    assert client.get("/api/orders/9999").status_code == 404
    assert client.get("/api/strategies/9999").status_code == 404


def test_logout_requires_csrf_and_revokes_session(client):
    assert client.post("/api/logout", headers={"X-QD": "1"}).status_code == 401
    client.post("/api/login", json={"password": "correct horse"})
    token = client.cookies.get("qd_session")
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/logout", headers={"X-QD": "1"}).status_code == 200
    assert client.get("/api/state").status_code == 401
    client.cookies.set("qd_session", token)
    assert client.get("/api/session").json()["authenticated"] is False


def test_public_bind_requires_admin_password(env, monkeypatch):
    from quantdesk.config import load_config
    monkeypatch.setenv("QD_BIND_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="QD_ADMIN_PASSWORD"):
        load_config(reload=True).check_api_bind()


@pytest.mark.parametrize("proxies", ["*", "127.0.0.1,*", "0.0.0.0/0", "::/0"])
def test_proxy_allowlist_rejects_wildcard(env, monkeypatch, proxies):
    from quantdesk.config import load_config
    monkeypatch.setenv("QD_TRUSTED_PROXIES", proxies)
    with pytest.raises(ValueError, match="QD_TRUSTED_PROXIES"):
        load_config(reload=True)


def test_configured_public_https_origin(client, monkeypatch):
    import dataclasses
    import quantdesk.api.app as appmod
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, public_origin="https://desk.example"))
    assert client.post("/api/login", json={"password": "correct horse"},
                       headers={"Origin": "https://elsewhere.example"}).status_code == 403
    assert client.post("/api/login", json={"password": "correct horse"},
                       headers={"Origin": "https://desk.example"}).status_code == 200
    assert client.post("/api/control/pause", headers={"X-QD": "1", "Origin": "https://desk.example"}).status_code == 200


@pytest.mark.parametrize("pw", ["change-me-to-a-long-passphrase", "short"])
def test_rejects_published_or_short_admin_password(env, monkeypatch, pw):
    monkeypatch.setenv("QD_ADMIN_PASSWORD", pw)
    from quantdesk.config import load_config
    with pytest.raises(ValueError, match="QD_ADMIN_PASSWORD"):
        load_config(reload=True)


def test_env_example_password_is_blank_and_accepted(env, monkeypatch):
    from pathlib import Path
    from quantdesk.config import load_config
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "QD_ADMIN_PASSWORD=\n" in example
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "a perfectly fine passphrase")
    assert load_config(reload=True).admin_password == "a perfectly fine passphrase"


def test_batch_settings_save_is_all_or_nothing(client):
    from quantdesk import settings
    client.post("/api/login", json={"password": "correct horse"})
    before = settings.get("risk")["max_drawdown_pct"]
    r = client.post("/api/settings", json={"risk": {"max_drawdown_pct": 0.2}, "gates": {"min_test_trades": 0}},
                    headers={"X-QD": "1"})
    assert r.status_code == 400 and "gates.min_test_trades" in r.json()["detail"]
    assert settings.get("risk")["max_drawdown_pct"] == before          # the valid group was not written
    r = client.post("/api/settings", json={"risk": {"max_drawdown_pct": 0.2}, "gates": {"min_test_trades": 12}},
                    headers={"X-QD": "1"})
    assert r.status_code == 200
    assert settings.get("risk")["max_drawdown_pct"] == 0.2 and settings.get("gates")["min_test_trades"] == 12
    assert client.post("/api/settings", json={"agent": {"paused": False}}, headers={"X-QD": "1"}).status_code == 400
    assert client.post("/api/settings", json={"risk": {"max_drawdown_pct": 0.2}}).status_code == 403   # CSRF header


def test_emergency_stop_records_a_receipt_and_bounds_are_published(client):
    client.post("/api/login", json={"password": "correct horse"})
    r = client.post("/api/control/emergency_stop", headers={"X-QD": "1"})
    assert r.status_code == 200 and r.json()["cancelled"] == 0
    controls = client.get("/api/state").json()["controls"]
    assert controls["emergency_stop"] is True and controls["emergency_stop_at"] and controls["emergency_cancelled"] == 0
    bounds = client.get("/api/system").json()["bounds"]
    assert bounds["risk.max_gross_pct"][1] <= 1.0 and bounds["gates.min_test_trades"] == [1, 1000]


@pytest.fixture()
def viewer_client(env, monkeypatch):
    import importlib
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "correct horse")
    monkeypatch.setenv("QD_VIEWER_PASSWORD", "just looking around")
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    appmod = importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_viewer_password_gives_a_read_only_session(viewer_client):
    c = viewer_client
    r = c.post("/api/login", json={"password": "just looking around"})
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    assert c.get("/api/session").json()["role"] == "viewer"
    for path in ("/api/state", "/api/equity", "/api/journal", "/api/experiments", "/api/strategies",
                 "/api/orders", "/api/fills", "/api/universe"):
        assert c.get(path).status_code == 200, path
    # operator-only reads: limits, audit log, jobs, model prompts
    for path in ("/api/system", "/api/llm_calls", "/api/llm_calls/1"):
        assert c.get(path).status_code == 403, path
    w = {"X-QD": "1"}
    for path in ("/api/control/pause", "/api/control/emergency_stop", "/api/gate-presets/strict"):
        assert c.post(path, headers=w).status_code == 403, path
    assert c.post("/api/settings/risk", json={"max_gross_pct": 0.5}, headers=w).status_code == 403
    assert c.post("/api/ask", json={"question": "why?"}, headers=w).status_code == 403
    assert c.post("/api/ask/stream", json={"question": "why?"}, headers=w).status_code == 403
    assert c.get("/api/questions").status_code == 403
    from quantdesk import settings
    assert settings.get("agent")["paused"] is not True
    assert c.post("/api/logout").status_code == 403                  # logout still needs the CSRF header
    assert c.post("/api/logout", headers=w).status_code == 200
    assert c.get("/api/state").status_code == 401


def test_operator_session_unchanged_when_viewer_password_is_set(viewer_client):
    c = viewer_client
    assert c.post("/api/login", json={"password": "correct horse"}).json()["role"] == "operator"
    assert c.get("/api/session").json()["role"] == "operator"
    assert c.get("/api/system").status_code == 200
    assert c.post("/api/control/pause", headers={"X-QD": "1"}).status_code == 200


def test_viewer_session_role_is_stored_server_side(viewer_client):
    from quantdesk import db
    c = viewer_client
    c.post("/api/login", json={"password": "just looking around"})
    token = c.cookies.get("qd_session")
    row = db.q1("SELECT role, token_digest FROM web_sessions")
    assert row["role"] == "viewer" and row["token_digest"] != token
    assert c.get("/api/state").status_code == 200 and c.get("/api/system").status_code == 403


@pytest.fixture()
def public_client(env, monkeypatch):
    import importlib
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "correct horse")
    monkeypatch.setenv("QD_PUBLIC_VIEW", "1")
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    appmod = importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_public_view_is_read_only_without_a_password(public_client):
    c = public_client
    s = c.get("/api/session").json()
    assert s["authenticated"] and s["role"] == "viewer" and s["anonymous"] and s["public_view"]
    for path in ("/api/state", "/api/equity", "/api/journal", "/api/experiments", "/api/strategies",
                 "/api/orders", "/api/fills", "/api/universe"):
        assert c.get(path).status_code == 200, path
    for path in ("/api/system", "/api/llm_calls", "/api/llm_calls/1", "/api/questions"):
        assert c.get(path).status_code == 403, path
    w = {"X-QD": "1"}
    for path in ("/api/control/pause", "/api/control/emergency_stop", "/api/gate-presets/strict"):
        assert c.post(path, headers=w).status_code == 403, path
    assert c.post("/api/settings/risk", json={"max_gross_pct": 0.5}, headers=w).status_code == 403
    assert c.post("/api/ask", json={"question": "why?"}, headers=w).status_code == 403
    from quantdesk import db, settings
    assert settings.get("agent")["paused"] is not True
    assert db.q1("SELECT COUNT(*) n FROM web_sessions")["n"] == 0      # visitors get no session row


def test_operator_signs_in_over_the_public_view(public_client):
    c = public_client
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"password": "correct horse"}).json()["role"] == "operator"
    s = c.get("/api/session").json()
    assert s["role"] == "operator" and not s["anonymous"]
    assert c.get("/api/system").status_code == 200
    assert c.post("/api/control/pause", headers={"X-QD": "1"}).status_code == 200
    assert c.post("/api/logout", headers={"X-QD": "1"}).status_code == 200
    assert c.get("/api/session").json()["role"] == "viewer"
    assert c.get("/api/system").status_code == 403


def test_public_view_needs_an_operator_password(env, monkeypatch):
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "")
    monkeypatch.setenv("QD_PUBLIC_VIEW", "1")
    from quantdesk.config import load_config
    with pytest.raises(ValueError, match="QD_PUBLIC_VIEW"):
        load_config(reload=True)


@pytest.mark.parametrize("admin,viewer", [("correct horse", "correct horse"), ("correct horse", "short"), ("", "just looking around")])
def test_rejects_unsafe_viewer_password(env, monkeypatch, admin, viewer):
    monkeypatch.setenv("QD_ADMIN_PASSWORD", admin)
    monkeypatch.setenv("QD_VIEWER_PASSWORD", viewer)
    from quantdesk.config import load_config
    with pytest.raises(ValueError, match="QD_VIEWER_PASSWORD"):
        load_config(reload=True)


def test_ask_stream_reports_progress_and_lands_in_history(client):
    import json
    client.post("/api/login", json={"password": "correct horse"})
    w = {"X-QD": "1"}
    assert client.post("/api/ask/stream", json={"question": "why?"}).status_code == 403   # CSRF header required
    r = client.post("/api/ask/stream", json={"question": "What are you researching right now?"}, headers=w)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    steps = [e["step"] for e in events if e["type"] == "step"]
    assert steps[:2] == ["symbols", "account"] and "gathered" in steps and steps[-1] == "verify"
    assert all(e["t"] >= 0 for e in events if e["type"] == "step")
    answer = events[-1]
    assert answer["type"] == "answer" and answer["author"] == "rules" and answer["answer"] and answer["ts"]
    hist = client.get("/api/questions").json()
    assert hist[0]["id"] == answer["id"] and hist[0]["question"] == "What are you researching right now?"
    assert hist[0]["author"] == "rules" and hist[0]["answer"] == answer["answer"] and hist[0]["ts"] == answer["ts"]
    client.post("/api/ask", json={"question": "Why cash?"}, headers=w)
    hist = client.get("/api/questions").json()
    assert [h["question"] for h in hist[:2]] == ["Why cash?", "What are you researching right now?"]
    assert [h["question"] for h in client.get(f"/api/questions?before_id={hist[0]['id']}").json()][:1] == \
        ["What are you researching right now?"]


def test_journal_search_covers_the_full_history(client):
    from quantdesk.agent import journal
    old = journal.write("decision", "Rejected: crypto breakout", "spread 50% wider", refs={"experiment_id": 12})
    journal.write("review", "Review: keeping gap_fade", "fine", refs={"strategy_id": 5, "probe": True})
    for i in range(30):
        journal.write("system", f"noise {i}", "nothing")
    client.post("/api/login", json={"password": "correct horse"})
    get = lambda **p: [j["id"] for j in client.get("/api/journal", params={"limit": 5, **p}).json()]
    assert old not in get()                                  # outside the loaded window
    assert get(q="crypto breakout") == [old]                 # every word must match
    assert get(q="#12") == [old] and get(q="experiment #12") == [old]
    assert get(q="#1") == []                                 # a boolean ref is not record 1
    assert get(q="strategy gap_fade") and get(q="50%") == [old] and get(q="%") == [old] and get(q="5_%") == []
    assert get(q="crypto", kind="review") == []
    assert get(before_id=old + 3, limit=50) == [old + 2, old + 1, old]
