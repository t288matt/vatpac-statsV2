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
spec = importlib.util.spec_from_file_location("grafana_public", REPO / "ops" / "grafana_public.py")
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)

D1 = {"uid": "a1", "title": "Alpha"}
D2 = {"uid": "b2", "title": "Beta"}
D3 = {"uid": "c3", "title": "Gamma"}


def pub(dash: str, uid: str, **over) -> dict:
    return {"dashboardUid": dash, "uid": uid, "accessToken": gp.stable_token(dash), **gp.WANTED, **over}


def test_plan_creates_missing_fixes_wrong_and_keeps_right() -> None:
    steps = gp.plan([D3, D1, D2], [pub("a1", "p1"), pub("b2", "p2", timeSelectionEnabled=True)])
    assert [(a, d["uid"]) for a, d, _ in steps] == [("keep", "a1"), ("update", "b2"), ("create", "c3")]


def test_plan_treats_a_disabled_link_as_wrong() -> None:
    steps = gp.plan([D1], [pub("a1", "p1", isEnabled=False)])
    assert steps[0][0] == "update"


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
    gp.check_url("https://vatpac.example")
    gp.check_url("http://127.0.0.1:3300")


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
    assert gp.default_url({}) == "http://127.0.0.1:3300"
    assert gp.default_url({"GRAFANA_BIND": "192.0.2.10"}) == "http://192.0.2.10:3300"
    assert gp.default_url({"GRAFANA_BIND": "0.0.0.0", "GRAFANA_PORT": "3400"}) == "http://127.0.0.1:3400"


def test_http_is_allowed_only_to_addresses_of_this_machine() -> None:
    assert gp.is_this_machine("127.0.0.1") and gp.is_this_machine("localhost")
    assert not gp.is_this_machine("203.0.113.9") and not gp.is_this_machine(None)


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
