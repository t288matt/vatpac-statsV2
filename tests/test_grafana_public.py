"""ops/grafana_public.py: the plan for which dashboards get a public link, and the whole flow against a fake Grafana."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops"))  # as when the script is run: its folder is first on sys.path (it imports env_file)
import grafana_api as ga  # noqa: E402  (the shared client: its rules are tested here, through both scripts)
spec = importlib.util.spec_from_file_location("grafana_public", REPO / "ops" / "grafana_public.py")
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)

D1 = {"uid": "a1", "title": "Alpha"}
D2 = {"uid": "b2", "title": "Beta"}
D3 = {"uid": "c3", "title": "Gamma"}


def pub(dash: str, uid: str, **over) -> dict:
    return {"dashboardUid": dash, "uid": uid, "accessToken": gp.stable_token(dash), **gp.wanted(dash), **over}


def test_plan_creates_missing_fixes_wrong_and_keeps_right() -> None:
    steps = gp.plan([D3, D1, D2], [pub("a1", "p1"), pub("b2", "p2", timeSelectionEnabled=True)])
    assert [(a, d["uid"]) for a, d, _ in steps] == [("keep", "a1"), ("update", "b2"), ("create", "c3")]


def test_plan_treats_a_disabled_link_as_wrong() -> None:
    steps = gp.plan([D1], [pub("a1", "p1", isEnabled=False)])
    assert steps[0][0] == "update"


def test_the_time_picker_is_on_for_the_promotions_page_only() -> None:
    assert gp.wanted("vatpac-atc-promotions")["timeSelectionEnabled"] is True
    assert gp.wanted("a1")["timeSelectionEnabled"] is False
    uids = {json.loads(f.read_text(encoding="utf-8"))["uid"] for f in (REPO / "grafana" / "dashboards").rglob("*.json")}
    assert "vatpac-atc-promotions" in uids
    assert {u for u in uids if gp.wanted(u)["timeSelectionEnabled"]} == {"vatpac-atc-promotions"}
    assert all(gp.wanted(u)[k] == v for u in uids for k, v in
               {"isEnabled": True, "annotationsEnabled": False, "share": "public"}.items())


def test_a_link_with_the_wrong_picker_setting_is_updated_in_both_directions() -> None:
    old_picker = gp.TIME_PICKER
    try:
        gp.TIME_PICKER = {"a1"}
        steps = gp.plan([D1, D2], [pub("a1", "p1", timeSelectionEnabled=False), pub("b2", "p2", timeSelectionEnabled=True)])
    finally:
        gp.TIME_PICKER = old_picker
    assert [(a, d["uid"]) for a, d, _ in steps] == [("update", "a1"), ("update", "b2")]


def test_main_sends_the_picker_setting_on_create_and_on_repair(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gp, "TIME_PICKER", {"b2"})
    _, state, _ = run_main(tmp_path, {})
    assert state["public"]["b2"]["timeSelectionEnabled"] is True and state["public"]["a1"]["timeSelectionEnabled"] is False
    stale = {"a1": pub("a1", "p1", timeSelectionEnabled=True), "b2": pub("b2", "p2", timeSelectionEnabled=False)}
    _, state, _ = run_main(tmp_path, stale)
    assert state["public"]["b2"]["timeSelectionEnabled"] is True and state["public"]["a1"]["timeSelectionEnabled"] is False


def test_read_env_ignores_comments_and_strips_quotes(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text('# c\nGRAFANA_ADMIN_USER=admin\nGRAFANA_ADMIN_PASSWORD="s3cret=x"\n\nBLANK=\n', encoding="utf-8")
    assert gp.read_env(f) == {"GRAFANA_ADMIN_USER": "admin", "GRAFANA_ADMIN_PASSWORD": "s3cret=x", "BLANK": ""}


class FakeGrafana(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a) -> None:  # silence
        pass

    def _send(self, code: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self) -> None:
        s = self.state
        s["auth"] = self.headers.get("Authorization")
        if self.path.startswith("/api/search"):
            self._send(200, [D1, D2])
        elif self.path == "/api/dashboards/public-dashboards":  # like Grafana: the list carries no settings
            keys = ("dashboardUid", "uid", "accessToken", "isEnabled")
            self._send(200, {"publicDashboards": [{k: v[k] for k in keys} for v in s["public"].values()]})
        elif self.path.startswith("/api/dashboards/uid/") and self.path.endswith("/public-dashboards"):
            rec = s["public"].get(self.path.split("/")[4])
            self._send(200 if rec else 404, rec or {})
        else:
            self._send(404, {})

    def do_POST(self) -> None:
        dash = self.path.split("/")[4]
        rec = {"dashboardUid": dash, "uid": "new-" + dash, "accessToken": "tok-" + dash, **self._body()}
        self.state["public"][dash] = rec
        self._send(200, rec)

    def do_PATCH(self) -> None:
        dash = self.path.split("/")[4]
        self.state["public"][dash].update(self._body())
        self._send(200, self.state["public"][dash])

    def do_DELETE(self) -> None:
        self.state["public"].pop(self.path.split("/")[4], None)
        self._send(200, {})


def run_main(tmp_path: Path, public: dict, *extra: str) -> tuple[int, dict, str]:
    FakeGrafana.state = {"public": public}
    server = HTTPServer(("127.0.0.1", 0), FakeGrafana)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = tmp_path / ".env"
    env.write_text("GRAFANA_ADMIN_PASSWORD=pw\n", encoding="utf-8")
    try:
        code = gp.main(["--url", f"http://127.0.0.1:{server.server_port}", "--env", str(env), *extra])
    finally:
        server.shutdown()
    return code, FakeGrafana.state, ""


def test_main_creates_then_is_idempotent_then_removes(tmp_path: Path, capsys) -> None:
    code, state, _ = run_main(tmp_path, {})
    assert code == 0 and set(state["public"]) == {"a1", "b2"}
    assert all(p["timeSelectionEnabled"] is False and p["isEnabled"] and p["share"] == "public" for p in state["public"].values())
    assert state["auth"].startswith("Basic ")
    capsys.readouterr()
    code, state, _ = run_main(tmp_path, dict(state["public"]), "--public-url", "https://x.example")
    out = capsys.readouterr().out
    assert code == 0 and out.count(f"ok  https://x.example/public-dashboards/{gp.stable_token('a1')}") == 1 and "created" not in out
    code, state, _ = run_main(tmp_path, dict(state["public"]), "--remove")
    assert code == 0 and state["public"] == {}


def test_main_list_changes_nothing_and_never_prints_the_password(tmp_path: Path, capsys) -> None:
    code, state, _ = run_main(tmp_path, {}, "--list")
    out = capsys.readouterr().out
    assert code == 0 and state["public"] == {} and "not public" in out and "pw" not in out.replace("public", "")


def test_refuses_to_send_the_login_in_clear_to_another_host(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text("GRAFANA_ADMIN_PASSWORD=pw\n", encoding="utf-8")
    assert gp.main(["--url", "http://203.0.113.9:3000", "--env", str(env)]) == 2
    assert "refusing" in capsys.readouterr().err
    ga.check_url("https://vatpac.example")
    ga.check_url("http://127.0.0.1:3300")


def test_grafana_down_and_missing_env_give_a_message_not_a_traceback(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text("GRAFANA_ADMIN_PASSWORD=pw\n", encoding="utf-8")
    assert gp.main(["--url", "http://127.0.0.1:1", "--env", str(env)]) == 1  # nothing listens on port 1
    assert "cannot reach Grafana" in capsys.readouterr().err
    assert gp.main(["--env", str(tmp_path / "missing.env")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_list_shows_links_of_dashboards_that_are_no_longer_provisioned(tmp_path: Path, capsys) -> None:
    orphan = pub("zz", "p9")
    orphan["title"] = "Old dashboard"
    run_main(tmp_path, {"zz": orphan}, "--list")
    assert "Old dashboard" in capsys.readouterr().out


def test_default_url_follows_grafana_bind_and_port() -> None:
    assert ga.default_url({}) == "http://127.0.0.1:3300"
    assert ga.default_url({"GRAFANA_BIND": "192.0.2.10"}) == "http://192.0.2.10:3300"
    assert ga.default_url({"GRAFANA_BIND": "0.0.0.0", "GRAFANA_PORT": "3400"}) == "http://127.0.0.1:3400"


def test_http_is_allowed_only_to_addresses_of_this_machine() -> None:
    assert ga.is_this_machine("127.0.0.1") and ga.is_this_machine("localhost")
    assert not ga.is_this_machine("203.0.113.9") and not ga.is_this_machine(None)


def test_stable_token_is_32_hex_and_always_the_same() -> None:
    t = gp.stable_token("vatpac-pilots-now")
    assert len(t) == 32 and all(c in "0123456789abcdef" for c in t)
    assert t == gp.stable_token("vatpac-pilots-now") and t != gp.stable_token("vatpac-atc-overview")


def test_a_link_with_another_token_is_replaced(tmp_path: Path, capsys) -> None:
    random_token = pub("a1", "p1", accessToken="0" * 32)
    assert gp.plan([D1], [random_token])[0][0] == "recreate"
    code, state, _ = run_main(tmp_path, {"a1": random_token, "b2": pub("b2", "p2")})
    assert code == 0 and state["public"]["a1"]["accessToken"] == gp.stable_token("a1")
    assert state["public"]["b2"]["accessToken"] == gp.stable_token("b2")


def test_every_provisioned_dashboard_has_a_short_name() -> None:
    uids = {json.loads(f.read_text(encoding="utf-8"))["uid"] for f in (REPO / "grafana" / "dashboards").glob("*/*.json")}
    assert uids == set(gp.SHORT_NAMES), "add or remove the short name when a dashboard is added or removed"
    assert len(set(gp.SHORT_NAMES.values())) == len(gp.SHORT_NAMES)


def test_traefik_labels_redirect_each_short_name_to_its_stable_link(capsys) -> None:
    assert gp.main(["--traefik-labels", "--host", "vpac.example"]) == 0
    out = capsys.readouterr().out
    for uid, name in gp.SHORT_NAMES.items():
        assert f"Host(`vpac.example`) && Path(`/{name}`)" in out
        assert f"redirectregex.replacement=https://vpac.example/public-dashboards/{gp.stable_token(uid)}" in out
    assert out.count("redirectregex.regex=") == len(gp.SHORT_NAMES) and "/?$$" in out
    assert gp.main(["--traefik-labels"]) == 2


# ------------------------------------------------------------------ the security rules of the shared client (issue 210)

class _Hits(BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]] = []
    target = ""

    def log_message(self, *a) -> None:
        pass

    def do_GET(self) -> None:
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.target)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            raw = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)


def _serve(handler: type) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_is_never_followed_so_the_login_goes_nowhere_else() -> None:
    class Far(_Hits):
        hits: list[tuple[str, str | None]] = []

    class Near(_Hits):
        hits: list[tuple[str, str | None]] = []

    far, near = _serve(Far), _serve(Near)
    Near.target = f"http://127.0.0.1:{far.server_port}/stolen"
    try:
        status, _ = gp.Grafana(f"http://127.0.0.1:{near.server_port}", "admin", "pw").call("GET", "/redirect")
        assert status == 302  # reported as an error status, not followed
        assert Near.hits and Near.hits[0][1].startswith("Basic ")
        assert Far.hits == []  # the other address never saw a request, let alone the login
    finally:
        far.shutdown()
        near.shutdown()


def test_the_client_refuses_clear_text_to_another_machine_and_allows_this_one() -> None:
    import pytest

    for bad in ("http://203.0.113.9:3000", "http://grafana.example", "ftp://127.0.0.1"):
        with pytest.raises(ValueError, match="refusing to send the Grafana login"):
            gp.Grafana(bad, "admin", "pw")
    gp.Grafana("https://grafana.example", "admin", "pw")
    gp.Grafana("http://127.0.0.1:3300", "admin", "pw")
    gp.Grafana("http://localhost:3300", "admin", "pw")


# ------------------------------------------------------------------ ops/check_panels.py on the shared client (issue 210)

cp_spec = importlib.util.spec_from_file_location("check_panels", REPO / "ops" / "check_panels.py")
cp = importlib.util.module_from_spec(cp_spec)
cp_spec.loader.exec_module(cp)


class _PanelsGrafana(BaseHTTPRequestHandler):
    seen_auth: list[str | None] = []

    def log_message(self, *a) -> None:
        pass

    def _send(self, code: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        type(self).seen_auth.append(self.headers.get("Authorization"))
        self._send(200, {"database": "ok", "message": "Data source is working"})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        q = json.loads(self.rfile.read(n))["queries"][0]
        if "boom" in q["rawSql"]:
            self._send(200, {"results": {q["refId"]: {"error": "syntax error"}}})
        elif "nothing" in q["rawSql"]:
            self._send(200, {"results": {q["refId"]: {"frames": [{"data": {"values": []}}]}}})
        else:
            self._send(200, {"results": {q["refId"]: {"frames": [{"data": {"values": [[1, 2]]}}]}}})


def _panels_env(tmp_path: Path, port: int, bind: str = "127.0.0.1") -> Path:
    d = tmp_path / "dash" / "x"
    d.mkdir(parents=True)
    panels = [{"title": t, "targets": [{"refId": "A", "rawSql": sql}]} for t, sql in (("ok", "select 1"), ("empty", "select nothing"), ("bad", "select boom"))]
    (d / "d.json").write_text(json.dumps({"panels": panels}), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(f'GRAFANA_BIND={bind}\nexport GRAFANA_PORT="{port}"\nGRAFANA_ADMIN_PASSWORD=pw\n', encoding="utf-8")
    return env


def test_check_panels_counts_rows_empties_and_errors_through_the_shared_client(tmp_path: Path, capsys) -> None:
    _PanelsGrafana.seen_auth = []
    server = _serve(_PanelsGrafana)
    try:
        env = _panels_env(tmp_path, server.server_port)
        import sys as _sys
        old = _sys.argv
        _sys.argv = ["check_panels.py", "--env", str(env), "--dashboards", str(tmp_path / "dash")]
        try:
            code = cp.main()
        finally:
            _sys.argv = old
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 1 and "panel queries: 1 with rows, 1 empty, 1 errors" in out and "ERROR" in out and "syntax error" in out
    assert _PanelsGrafana.seen_auth and all(a and a.startswith("Basic ") for a in _PanelsGrafana.seen_auth)


def test_check_panels_refuses_to_send_the_login_in_clear_to_another_machine(tmp_path: Path, capsys) -> None:
    import sys as _sys
    env = _panels_env(tmp_path, 3300, bind="203.0.113.9")
    old = _sys.argv
    _sys.argv = ["check_panels.py", "--env", str(env), "--dashboards", str(tmp_path / "dash")]
    try:
        assert cp.main() == 2
    finally:
        _sys.argv = old
    assert "refusing to send the Grafana login" in capsys.readouterr().err


def test_both_scripts_use_the_one_client_module() -> None:
    for name in ("check_panels.py", "grafana_public.py"):
        text = (REPO / "ops" / name).read_text(encoding="utf-8")
        assert "from grafana_api import Grafana" in text and "urllib.request" not in text and "class Grafana" not in text, name
    import subprocess as sp
    import sys as _sys
    r = sp.run([_sys.executable, "ops/check_panels.py", "--help"], capture_output=True, text=True, cwd=REPO, timeout=60)  # run as the VM runs it
    assert r.returncode == 0 and "--dashboards" in r.stdout, r.stderr
