"""ops/grafana_public.py: the plan for which dashboards get a public link, and the whole flow against a fake Grafana."""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("grafana_public", REPO / "ops" / "grafana_public.py")
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)

D1 = {"uid": "a1", "title": "Alpha"}
D2 = {"uid": "b2", "title": "Beta"}
D3 = {"uid": "c3", "title": "Gamma"}


def pub(dash: str, uid: str, **over) -> dict:
    return {"dashboardUid": dash, "uid": uid, "accessToken": f"tok-{uid}", **gp.WANTED, **over}


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
    assert code == 0 and out.count("ok  https://x.example/public-dashboards/tok-a1") == 1 and "created" not in out
    code, state, _ = run_main(tmp_path, dict(state["public"]), "--remove")
    assert code == 0 and state["public"] == {}


def test_main_list_changes_nothing_and_never_prints_the_password(tmp_path: Path, capsys) -> None:
    code, state, _ = run_main(tmp_path, {}, "--list")
    out = capsys.readouterr().out
    assert code == 0 and state["public"] == {} and "not public" in out and "pw" not in out.replace("public", "")
