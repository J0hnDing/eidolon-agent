import ast
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

NETWORK_MODULES = {
    "aiohttp",
    "ftplib",
    "httpx",
    "imaplib",
    "poplib",
    "requests",
    "smtplib",
    "socket",
    "urllib",
    "urllib3",
    "websockets",
}
BROWSER_MODULES = {"playwright", "pyppeteer", "selenium"}
PROCESS_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.popen",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.system",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.run",
}
NETWORK_CALL_PREFIXES = ("aiohttp.", "httpx.", "requests.", "urllib.request.", "urllib3.")
DESTRUCTIVE_CALL_SUFFIXES = {".rmdir", ".rmtree", ".unlink"}
DESTRUCTIVE_CALLS = {"os.remove", "os.removedirs", "os.rmdir", "shutil.rmtree"}
SENSITIVE_NAME_PARTS = {"api_key", "credential", "password", "secret", "token"}
EXCLUDED_PARTS = {".deps", ".git", ".pytest_cache", "__pycache__", "cache", "tests"}
MAX_SOURCE_BYTES = 1_000_000


@dataclass(frozen=True)
class CapabilityFinding:
    capability: str
    status: str
    path: str
    line: int
    evidence: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.status != "allowed"


@dataclass(frozen=True)
class CapabilityScanResult:
    ok: bool
    scanned_files: list[str]
    findings: list[CapabilityFinding]
    limitations: list[str]

    def model_dump(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scanned_files": self.scanned_files,
            "findings": [{**asdict(finding), "blocking": finding.blocking} for finding in self.findings],
            "limitations": self.limitations,
        }


class StaticCapabilityScanner:
    def scan(self, skill_dir: Path, approved_runtime: dict[str, Any]) -> CapabilityScanResult:
        skill_root = skill_dir.resolve()
        scanned_files: list[str] = []
        findings: list[CapabilityFinding] = []
        approved_domains = {str(domain).lower().rstrip(".") for domain in approved_runtime.get("network", []) or []}
        approved_secrets = {str(secret).lower() for secret in approved_runtime.get("secrets", []) or []}
        approved_writes = {
            self._normalize_relative_path(str(path))
            for path in approved_runtime.get("filesystem_write", []) or []
        }

        for source_path in sorted(skill_root.rglob("*.py")):
            relative_path = source_path.relative_to(skill_root)
            if any(part in EXCLUDED_PARTS or part.startswith(".") for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            scanned_files.append(relative)
            if source_path.stat().st_size > MAX_SOURCE_BYTES:
                findings.append(
                    CapabilityFinding(
                        capability="scan_error",
                        status="blocked",
                        path=relative,
                        line=1,
                        evidence=f"source file exceeds {MAX_SOURCE_BYTES} bytes",
                        message="Static capability scan cannot safely inspect an oversized Python source file.",
                    )
                )
                continue
            try:
                source = source_path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative)
            except (OSError, UnicodeError, SyntaxError) as exc:
                findings.append(
                    CapabilityFinding(
                        capability="scan_error",
                        status="blocked",
                        path=relative,
                        line=int(getattr(exc, "lineno", 1) or 1),
                        evidence=type(exc).__name__,
                        message="Static capability scan could not parse this Python source file.",
                    )
                )
                continue
            findings.extend(
                self._scan_tree(
                    tree,
                    relative,
                    approved_domains=approved_domains,
                    approved_secrets=approved_secrets,
                    approved_writes=approved_writes,
                )
            )

        findings = self._deduplicate(findings)
        return CapabilityScanResult(
            ok=not any(finding.blocking for finding in findings),
            scanned_files=scanned_files,
            findings=findings,
            limitations=[
                "The scan recognizes only direct Python syntax and selected standard library or common package patterns.",
                "It does not analyze dependency internals, dynamic imports, reflection, encoded source, runtime-built domains, or non-Python executables.",
                "An allowed finding is evidence that code references an approved capability, not proof that runtime use stays within the declaration.",
            ],
        )

    def _scan_tree(
        self,
        tree: ast.AST,
        relative_path: str,
        *,
        approved_domains: set[str],
        approved_secrets: set[str],
        approved_writes: set[str],
    ) -> list[CapabilityFinding]:
        findings: list[CapabilityFinding] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                findings.extend(self._import_findings(node, relative_path, approved_domains))
            if isinstance(node, ast.Call):
                finding = self._call_finding(
                    node,
                    relative_path,
                    approved_domains=approved_domains,
                    approved_secrets=approved_secrets,
                    approved_writes=approved_writes,
                )
                if finding is not None:
                    findings.append(finding)
            if isinstance(node, ast.Subscript):
                finding = self._environment_subscript_finding(node, relative_path, approved_secrets)
                if finding is not None:
                    findings.append(finding)
        return findings

    def _import_findings(
        self,
        node: ast.Import | ast.ImportFrom,
        relative_path: str,
        approved_domains: set[str],
    ) -> list[CapabilityFinding]:
        modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        findings: list[CapabilityFinding] = []
        for module in modules:
            root = module.split(".", 1)[0]
            if root in BROWSER_MODULES:
                findings.append(
                    CapabilityFinding(
                        capability="browser_automation",
                        status="blocked",
                        path=relative_path,
                        line=node.lineno,
                        evidence=f"imports {module}",
                        message="Browser automation is blocked by backend policy.",
                    )
                )
            elif root == "subprocess":
                findings.append(
                    CapabilityFinding(
                        capability="shell_process",
                        status="blocked",
                        path=relative_path,
                        line=node.lineno,
                        evidence=f"imports {module}",
                        message="Process execution is blocked by the current shell permission policy.",
                    )
                )
            elif root in NETWORK_MODULES:
                findings.append(
                    self._network_finding(
                        relative_path,
                        node.lineno,
                        approved_domains,
                        evidence=f"imports {module}",
                    )
                )
        return findings

    def _call_finding(
        self,
        node: ast.Call,
        relative_path: str,
        *,
        approved_domains: set[str],
        approved_secrets: set[str],
        approved_writes: set[str],
    ) -> CapabilityFinding | None:
        call_name = self._call_name(node.func)
        if call_name.startswith(NETWORK_CALL_PREFIXES) and node.args:
            url = self._literal_string(node.args[0])
            domain = self._url_domain(url or "")
            if domain:
                return self._network_finding(
                    relative_path,
                    node.lineno,
                    approved_domains,
                    evidence=f"calls {call_name} with literal domain {domain}",
                    domain=domain,
                )
        if call_name in PROCESS_CALLS:
            return CapabilityFinding(
                capability="shell_process",
                status="blocked",
                path=relative_path,
                line=node.lineno,
                evidence=f"calls {call_name}",
                message="Process execution is blocked by the current shell permission policy.",
            )
        if call_name in DESTRUCTIVE_CALLS or any(call_name.endswith(suffix) for suffix in DESTRUCTIVE_CALL_SUFFIXES):
            return CapabilityFinding(
                capability="file_deletion",
                status="blocked",
                path=relative_path,
                line=node.lineno,
                evidence=f"calls {call_name}",
                message="File deletion is blocked by backend policy.",
            )
        if call_name in {"os.getenv", "os.environ.get"}:
            secret_name = self._literal_string(node.args[0]) if node.args else None
            if secret_name and self._looks_sensitive(secret_name):
                return self._secret_finding(relative_path, node.lineno, secret_name, approved_secrets)

        path_and_mode = self._literal_file_access(node, call_name)
        if path_and_mode is None:
            return None
        literal_path, mode = path_and_mode
        if self._is_write_mode(mode) and not self._write_path_allowed(literal_path, approved_writes):
            return CapabilityFinding(
                capability="filesystem_write",
                status="undeclared",
                path=relative_path,
                line=node.lineno,
                evidence=f"writes literal path {literal_path}",
                message="Code appears to write outside the approved runtime filesystem paths.",
            )
        if self._unsafe_path(literal_path):
            return CapabilityFinding(
                capability="filesystem_read",
                status="undeclared",
                path=relative_path,
                line=node.lineno,
                evidence=f"accesses unsafe literal path {literal_path}",
                message="Code appears to access an absolute or parent-traversing filesystem path.",
            )
        return None

    def _environment_subscript_finding(
        self,
        node: ast.Subscript,
        relative_path: str,
        approved_secrets: set[str],
    ) -> CapabilityFinding | None:
        if self._call_name(node.value) != "os.environ":
            return None
        secret_name = self._literal_string(node.slice)
        if not secret_name or not self._looks_sensitive(secret_name):
            return None
        return self._secret_finding(relative_path, node.lineno, secret_name, approved_secrets)

    def _secret_finding(
        self,
        relative_path: str,
        line: int,
        secret_name: str,
        approved_secrets: set[str],
    ) -> CapabilityFinding:
        allowed = secret_name.lower() in approved_secrets
        return CapabilityFinding(
            capability="secrets",
            status="allowed" if allowed else "undeclared",
            path=relative_path,
            line=line,
            evidence=f"reads environment variable {secret_name}",
            message=(
                "Code references an approved secret name."
                if allowed
                else "Code appears to read a sensitive environment variable that was not approved."
            ),
        )

    def _network_finding(
        self,
        relative_path: str,
        line: int,
        approved_domains: set[str],
        *,
        evidence: str,
        domain: str | None = None,
    ) -> CapabilityFinding:
        allowed = bool(approved_domains) and (domain is None or domain in approved_domains)
        if allowed:
            message = "Code references runtime network access covered by the approved domain declaration."
        elif domain and approved_domains:
            message = f"Code references domain {domain}, which is not in the approved runtime network list."
        else:
            message = "Code references network functionality without approved runtime network domains."
        return CapabilityFinding(
            capability="network",
            status="allowed" if allowed else "undeclared",
            path=relative_path,
            line=line,
            evidence=evidence,
            message=message,
        )

    def _literal_file_access(self, node: ast.Call, call_name: str) -> tuple[str, str] | None:
        if call_name == "open":
            if not node.args:
                return None
            literal_path = self._literal_string(node.args[0])
            if literal_path is None:
                return None
            mode = self._literal_string(node.args[1]) if len(node.args) > 1 else None
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    mode = self._literal_string(keyword.value)
            return literal_path, mode or "r"
        if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Call):
            return None
        constructor = self._call_name(node.func.value.func)
        if constructor not in {"Path", "pathlib.Path"} or not node.func.value.args:
            return None
        literal_path = self._literal_string(node.func.value.args[0])
        if literal_path is None:
            return None
        if node.func.attr in {"write_bytes", "write_text"}:
            return literal_path, "w"
        if node.func.attr in {"read_bytes", "read_text"}:
            return literal_path, "r"
        if node.func.attr == "open":
            mode = self._literal_string(node.args[0]) if node.args else "r"
            return literal_path, mode or "r"
        return None

    def _write_path_allowed(self, path: str, approved_writes: set[str]) -> bool:
        normalized = self._normalize_relative_path(path)
        return any(normalized == root or normalized.startswith(f"{root}/") for root in approved_writes)

    def _unsafe_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        pure_path = PurePosixPath(normalized)
        return pure_path.is_absolute() or ".." in pure_path.parts or ":" in normalized

    def _normalize_relative_path(self, path: str) -> str:
        return path.replace("\\", "/").removeprefix("./").rstrip("/")

    def _is_write_mode(self, mode: str) -> bool:
        return any(flag in mode for flag in ("a", "w", "x", "+"))

    def _looks_sensitive(self, name: str) -> bool:
        normalized = name.lower()
        return any(part in normalized for part in SENSITIVE_NAME_PARTS)

    def _url_domain(self, value: str) -> str | None:
        if not value.lower().startswith(("http://", "https://")):
            return None
        return (urlsplit(value).hostname or "").lower().rstrip(".") or None

    def _literal_string(self, node: ast.AST) -> str | None:
        return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def _deduplicate(self, findings: list[CapabilityFinding]) -> list[CapabilityFinding]:
        unique: dict[tuple[str, str, int, str], CapabilityFinding] = {}
        for finding in findings:
            key = (finding.capability, finding.path, finding.line, finding.evidence)
            unique[key] = finding
        return sorted(unique.values(), key=lambda finding: (finding.path, finding.line, finding.capability, finding.evidence))
