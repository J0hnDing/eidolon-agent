from pathlib import Path

from app.services.capability_scanner import StaticCapabilityScanner


def write_skill_source(skill_dir: Path, source: str) -> None:
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.py").write_text(source, encoding="utf-8")


def test_static_capability_scan_allows_simple_standard_library_code(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import json\nprint(json.dumps({'ok': True, 'link': 'https://example.com/read-only-link'}))\n",
    )

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is True
    assert result.scanned_files == ["skill.py"]
    assert result.findings == []


def test_static_capability_scan_blocks_undeclared_network_and_process_usage(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import requests\nimport subprocess\nrequests.get('https://example.com/data')\nsubprocess.run(['tool'])\n",
    )

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is False
    assert {(finding.capability, finding.status) for finding in result.findings} >= {
        ("network", "undeclared"),
        ("shell_process", "blocked"),
    }


def test_static_capability_scan_checks_domains_and_cache_writes(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import requests\n"
        "from pathlib import Path\n"
        "requests.get('https://other.example/data')\n"
        "Path('./cache/result.json').write_text('ok')\n"
        "Path('result.json').write_text('not allowed')\n",
    )

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": ["approved.example"], "filesystem_write": ["./cache"], "secrets": []},
    )

    assert result.ok is False
    assert any(finding.status == "allowed" and finding.evidence == "imports requests" for finding in result.findings)
    assert any("other.example" in finding.message and finding.blocking for finding in result.findings)
    assert any(
        finding.capability == "filesystem_write" and "result.json" in finding.evidence
        for finding in result.findings
    )
    assert not any("./cache/result.json" in finding.evidence for finding in result.findings)


def test_static_capability_scan_ignores_tests_and_dependency_folders(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(skill_dir, "print('safe')\n")
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_skill.py").write_text("import subprocess\n", encoding="utf-8")
    deps_dir = skill_dir / ".deps" / "package"
    deps_dir.mkdir(parents=True)
    (deps_dir / "client.py").write_text("import requests\n", encoding="utf-8")

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is True
    assert result.scanned_files == ["skill.py"]
