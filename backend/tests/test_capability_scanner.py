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


def test_static_capability_scan_does_not_block_capability_imports_without_calls(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(skill_dir, "import requests\nimport subprocess\n")

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is True
    assert result.findings == []


def test_static_capability_scan_allows_urllib_parse_without_network_permission(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "from urllib.parse import unquote\nvalue = unquote('/notes%20manager')\n",
    )

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is True
    assert result.findings == []


def test_static_capability_scan_blocks_urllib_request_call_without_network_permission(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import urllib.request\nurllib.request.urlopen('https://example.com/data')\n",
    )

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is False
    assert any(
        finding.capability == "network" and finding.evidence == "calls urllib.request.urlopen with literal domain example.com"
        for finding in result.findings
    )


def test_static_capability_scan_resolves_aliased_urllib_request_call(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "from urllib.request import urlopen as fetch\nfetch('https://example.com/data')\n",
    )

    result = StaticCapabilityScanner().scan(skill_dir, {"network": [], "filesystem_write": [], "secrets": []})

    assert result.ok is False
    assert any(
        finding.capability == "network" and finding.evidence == "calls urllib.request.urlopen with literal domain example.com"
        for finding in result.findings
    )


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
    assert any("other.example" in finding.message and finding.blocking for finding in result.findings)
    assert any(
        finding.capability == "filesystem_write" and "result.json" in finding.evidence
        for finding in result.findings
    )
    assert not any("./cache/result.json" in finding.evidence for finding in result.findings)


def test_static_capability_scan_allows_dynamic_temporary_file_cleanup_in_approved_cache(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import os\nimport tempfile\n"
        "handle, temporary_name = tempfile.mkstemp(dir='./cache')\n"
        "os.close(handle)\nos.unlink(temporary_name)\n",
    )

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": [], "filesystem_write": ["./cache"], "secrets": []},
    )

    assert result.ok is True
    assert result.findings == []


def test_static_capability_scan_allows_literal_deletion_inside_approved_cache(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(skill_dir, "import os\nos.unlink('./cache/temporary.json')\n")

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": [], "filesystem_write": ["./cache"], "secrets": []},
    )

    assert result.ok is True
    assert result.findings == []


def test_static_capability_scan_blocks_literal_deletion_outside_approved_cache(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(skill_dir, "from pathlib import Path\nPath('outside.json').unlink()\n")

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": [], "filesystem_write": ["./cache"], "secrets": []},
    )

    assert result.ok is False
    assert any(
        finding.capability == "file_deletion" and finding.evidence == "deletes literal path outside.json"
        for finding in result.findings
    )


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


def test_web_app_scan_blocks_cache_beneath_read_only_package(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "from pathlib import Path\n"
        "PACKAGE_DIR = Path(__file__).resolve().parent\n"
        "STATIC_DIR = PACKAGE_DIR / 'static'\n"
        "CACHE_DIR = PACKAGE_DIR / 'cache'\n",
    )

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": [], "filesystem_write": ["./cache"], "secrets": []},
        runtime="web_app",
    )

    assert result.ok is False
    assert any(finding.capability == "web_app_cache_path" for finding in result.findings)


def test_web_app_scan_allows_platform_cache_environment_path(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import os\nfrom pathlib import Path\nCACHE_DIR = Path(os.environ['PERSONAL_AGENT_SKILL_CACHE_DIR'])\n",
    )

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": [], "filesystem_write": ["./cache"], "secrets": []},
        runtime="web_app",
    )

    assert result.ok is True


def test_static_capability_scan_blocks_direct_atlas_and_passphrase_access(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import os\nimport requests\n"
        "requests.post('http://127.0.0.1:4817/api/unlock')\n"
        "os.getenv('ATLAS_PASSPHRASE')\n",
    )

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": ["127.0.0.1"], "filesystem_write": [], "secrets": ["ATLAS_PASSPHRASE"]},
    )

    assert result.ok is False
    assert any(finding.capability == "direct_atlas_access" for finding in result.findings)
    assert any(finding.capability == "secrets" and finding.status != "allowed" for finding in result.findings)


def test_static_capability_scan_blocks_runtime_capability_serialization(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    write_skill_source(
        skill_dir,
        "import json\nimport os\n"
        "json.dumps(dict(os.environ))\n"
        "os.getenv('PERSONAL_AGENT_FUNCTION_CAPABILITY')\n",
    )

    result = StaticCapabilityScanner().scan(
        skill_dir,
        {"network": [], "filesystem_write": [], "secrets": []},
    )

    assert result.ok is False
    assert any(finding.capability == "integration_capability_material" for finding in result.findings)
    assert any(finding.capability == "secrets" and finding.status != "allowed" for finding in result.findings)
