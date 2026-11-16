"""The README architecture diagrams must match the code.

The container diagram names exactly the services in docker-compose.yml. Every component in the collector diagram is a
module of the collector. A service added, removed or renamed, or a module renamed, fails here until the README is updated."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text(encoding="utf-8")


def _block(heading: str) -> str:
    """The mermaid block under the README heading `### <heading>`."""
    section = README.split(f"### {heading}\n", 1)[1]
    return re.search(r"```mermaid\n(.*?)```", section, re.S).group(1)  # type: ignore[union-attr]


def _inside_subgraph(block: str) -> list[str]:
    body = re.search(r"subgraph [^\n]*\n(.*?)\n\s*end\b", block, re.S).group(1)  # type: ignore[union-attr]
    return [m.group(1) for m in re.finditer(r"^\s*\w+[\[\(]+([^\]\)]+)[\]\)]+\s*$", body, re.M)]


def test_container_diagram_names_exactly_the_compose_services() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    assert sorted(_inside_subgraph(_block("Containers"))) == sorted(compose["services"])


def test_every_container_has_a_row_in_the_table() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    section = README.split("### Containers\n", 1)[1].split("### Collector components", 1)[0]
    rows = set(re.findall(r"^\| `([\w-]+)` \|", section, re.M))
    assert rows == set(compose["services"])


def test_every_collector_component_is_a_module() -> None:
    modules = {p.stem for p in (REPO / "collector/src/vatpac").glob("*.py")}
    drawn = _inside_subgraph(_block("Collector components"))
    assert drawn, "no components found in the collector diagram"
    assert set(drawn) <= modules, f"not modules of the collector: {sorted(set(drawn) - modules)}"


def test_every_drawn_component_has_a_row_in_the_table() -> None:
    section = README.split("### Collector components\n", 1)[1].split("## Testing and development", 1)[0]
    rows = set(re.findall(r"^\| `([\w-]+)` \|", section, re.M))
    assert rows == set(_inside_subgraph(_block("Collector components")))
