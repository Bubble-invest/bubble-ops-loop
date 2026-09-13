from pathlib import Path
import subprocess

import yaml


DAY = "2026-09-13"


def _write_rnd(root: Path) -> Path:
    repo = root / "bubble-ops-rnd"
    (repo / "onboarding").mkdir(parents=True)
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "slug": "rnd",
                "display_name": "Rick",
                "owner": "joris",
                "created_at": "2026-04-13T16:13:50Z",
                "status": "Live",
                "host": "local",
                "validated_steps": ["mandate", "missions", "layers"],
                "last_updated_at": "2026-09-13T16:15:00Z",
                "commits": [],
            }
        )
    )
    (repo / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "rnd", "display_name": "Rick", "level": "ops"},
                "layers": {"subscribed": [1, 2, 3, 4]},
                "recurring_missions": [],
            }
        )
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:vdk888/bubble-rnd-workspace.git"],
        check=True,
    )
    (repo / "MANDATE.md").write_text("# Rick mandate\n")
    for layer in (1, 2, 3, 4):
        (repo / "layers" / str(layer)).mkdir(parents=True)
        (repo / "layers" / str(layer) / "PROMPT.md").write_text(f"# L{layer}\n")
        out = repo / "outputs" / DAY / str(layer)
        out.mkdir(parents=True)
        (out / ".last-run").write_text(f"{DAY}T12:0{layer}:00Z\n")
    export = {
        "dept": "rnd",
        "date": DAY,
        "status": "warning",
        "last_successful_layer": 4,
        "open_gates": 1,
        "open_exceptions": 0,
        "top_kpis": {"open_cards": 7, "build_control_health": 0.5},
        "needs_management_attention": ["one operator decision"],
        "links": {
            "operator_intent": "shared/operator-intents/rick-controlled-rnd-convergence.md"
        },
    }
    (repo / "outputs" / DAY / "4" / "management-export.yaml").write_text(
        yaml.safe_dump(export)
    )
    return repo


def test_rnd_disk_registration_graph_layers_and_report_are_visible(monkeypatch, tmp_path):
    from console import settings
    from console.services import dept_registry, github_reader, org_framework

    disk = tmp_path / "disk"
    disk.mkdir()
    repo = _write_rnd(disk)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "bubble-ops-rnd").symlink_to(repo, target_is_directory=True)
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(disk))
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(canonical))
    monkeypatch.setattr(org_framework, "_rail_status", lambda _: {"status": "unknown", "telemetry": False, "last_human": ""})

    rnd = dept_registry.get_department("rnd")
    assert rnd is not None
    assert (rnd.display_name, rnd.status, rnd.host) == ("Rick", "Live", "local")
    assert dept_registry.repo_path("rnd") == repo.resolve()
    assert dept_registry.runtime_repo_path("rnd") == (canonical / "bubble-ops-rnd").resolve()
    assert [github_reader.load_layer_prompt_md("rnd", n) for n in range(1, 5)] == [
        "# L1\n", "# L2\n", "# L3\n", "# L4\n"
    ]

    graph = org_framework.build_graph()
    node = next(item for item in graph["nodes"] if item["id"] == "dept:rnd")
    assert node["host"] == "local"
    assert [layer["num"] for layer in node["layers"]] == [1, 2, 3, 4]
    assert not any(item["id"] == "local:rick" for item in graph["nodes"])
    rnd_repo = next(item for item in graph["nodes"] if item["id"] == "repo:bubble-rnd-workspace")
    assert rnd_repo["href"] == "https://github.com/vdk888/bubble-rnd-workspace"

    report = github_reader._load_child_entry("rnd", repo)
    assert report["management_export"]["dept"] == "rnd"
    assert report["management_export"]["top_kpis"]["open_cards"] == 7
