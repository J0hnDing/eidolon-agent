import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from app.services.skill_package_files import iter_skill_files

BROWSER_MODULES = {"playwright", "pyppeteer", "selenium"}
CREDENTIAL_STORE_MODULES = {"keyring", "win32cred"}
TEST_ONLY_MODULES = {"integration_test_adapter"}
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
DESTRUCTIVE_METHODS = {"rmdir", "rmtree", "unlink"}
DESTRUCTIVE_CALLS = {"os.remove", "os.removedirs", "os.rmdir", "os.unlink", "shutil.rmtree"}
SENSITIVE_NAME_PARTS = {"api_key", "credential", "password", "secret", "token"}
EXCLUDED_PARTS = {".build-deps", ".deps", ".git", ".pytest_cache", "__pycache__", "cache", "tests"}
MAX_SOURCE_BYTES = 1_000_000
WEB_ASSET_SUFFIXES = {".css", ".html", ".htm", ".js", ".mjs"}
ABSOLUTE_BROWSER_URL = re.compile(r"https?://[^\s\"'<>)}]+", re.IGNORECASE)
GITHUB_HOSTS = {"api.github.com", "github.com"}
INTEGRATION_HELPERS = {
    "integration_runtime_capabilities.call",
    "web_runtime_capabilities.call_integration",
}


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
    def scan(
        self,
        skill_dir: Path,
        approved_runtime: dict[str, Any],
        *,
        runtime: str = "function",
        declared_integration_operations: set[str] | None = None,
        selected_integration_operations: set[str] | None = None,
        include_paths: set[str] | None = None,
    ) -> CapabilityScanResult:
        skill_root = skill_dir.resolve()
        scanned_files: list[str] = []
        findings: list[CapabilityFinding] = []
        approved_domains = {str(domain).lower().rstrip(".") for domain in approved_runtime.get("network", []) or []}
        approved_secrets = {str(secret).lower() for secret in approved_runtime.get("secrets", []) or []}
        approved_writes = {
            self._normalize_relative_path(str(path))
            for path in approved_runtime.get("filesystem_write", []) or []
        }

        package_files = iter_skill_files(skill_root)
        for source_path in (path for path in package_files if path.suffix.lower() == ".py"):
            relative_path = source_path.relative_to(skill_root)
            if any(part in EXCLUDED_PARTS or part.startswith(".") for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            if include_paths is not None and relative not in include_paths:
                continue
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
                    runtime=runtime,
                    declared_integration_operations=declared_integration_operations or set(),
                    selected_integration_operations=selected_integration_operations or set(),
                )
            )

        for asset_path in (path for path in package_files if path.suffix.lower() in WEB_ASSET_SUFFIXES):
            relative_path = asset_path.relative_to(skill_root)
            if any(part in EXCLUDED_PARTS or part.startswith(".") for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            if include_paths is not None and relative not in include_paths:
                continue
            scanned_files.append(relative)
            if asset_path.stat().st_size > MAX_SOURCE_BYTES:
                findings.append(
                    CapabilityFinding(
                        capability="scan_error",
                        status="blocked",
                        path=relative,
                        line=1,
                        evidence=f"web asset exceeds {MAX_SOURCE_BYTES} bytes",
                        message="Static capability scan cannot safely inspect an oversized browser asset.",
                    )
                )
                continue
            try:
                source = asset_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                findings.append(
                    CapabilityFinding(
                        capability="scan_error",
                        status="blocked",
                        path=relative,
                        line=1,
                        evidence=type(exc).__name__,
                        message="Static capability scan could not read this browser asset.",
                    )
                )
                continue
            for match in ABSOLUTE_BROWSER_URL.finditer(source):
                domain = self._url_domain(match.group(0))
                findings.append(
                    CapabilityFinding(
                        capability="direct_github_access" if domain in GITHUB_HOSTS else "browser_network",
                        status="blocked",
                        path=relative,
                        line=source.count("\n", 0, match.start()) + 1,
                        evidence=f"references absolute browser URL {match.group(0)[:200]}",
                        message=(
                            "Browser-side external network references are blocked; web applications must use "
                            "same-origin application routes backed by approved server-side permissions."
                        ),
                    )
                )
            for marker in (
                "call_integration",
                "/integrations/capabilities",
                "/capabilities/integrations",
                "PERSONAL_AGENT_",
            ):
                if marker in source:
                    findings.append(
                        CapabilityFinding(
                            capability="browser_integration",
                            status="blocked",
                            path=relative,
                            line=source.count("\n", 0, source.index(marker)) + 1,
                            evidence=f"browser asset contains {marker}",
                            message="Browser code cannot invoke integrations or receive runtime capability material.",
                        )
                    )

        findings = self._deduplicate(findings)
        return CapabilityScanResult(
            ok=not any(finding.blocking for finding in findings),
            scanned_files=scanned_files,
            findings=findings,
            limitations=[
                "The scan blocks only selected direct Python evidence plus literal absolute URLs in browser assets.",
                "Unknown dynamic paths, imports without recognized calls, and runtime-built domains are intentionally not findings.",
                "The runtime sandbox remains authoritative when static evidence is ambiguous or absent.",
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
        runtime: str = "function",
        declared_integration_operations: set[str],
        selected_integration_operations: set[str],
    ) -> list[CapabilityFinding]:
        findings: list[CapabilityFinding] = []
        import_aliases = self._import_aliases(tree)
        if runtime == "web_app":
            findings.extend(self._web_app_cache_findings(tree, relative_path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                findings.extend(self._import_findings(node, relative_path))
            if isinstance(node, ast.Call):
                finding = self._call_finding(
                    node,
                    relative_path,
                    approved_domains=approved_domains,
                    approved_secrets=approved_secrets,
                    approved_writes=approved_writes,
                    import_aliases=import_aliases,
                )
                if finding is not None:
                    findings.append(finding)
                integration_finding = self._integration_call_finding(
                    node,
                    relative_path,
                    import_aliases,
                    declared_integration_operations,
                    selected_integration_operations,
                )
                if integration_finding is not None:
                    findings.append(integration_finding)
            if isinstance(node, ast.Subscript):
                finding = self._environment_subscript_finding(node, relative_path, approved_secrets)
                if finding is not None:
                    findings.append(finding)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                if "/integrations/capabilities/" in lowered:
                    findings.append(
                        CapabilityFinding(
                            capability="integration_internal_path",
                            status="blocked",
                            path=relative_path,
                            line=getattr(node, "lineno", 1),
                            evidence="references the internal integration HTTP path",
                            message="Generated code must use the trusted integration helper, not the internal HTTP path.",
                        )
                    )
                if "api.github.com" in lowered or "https://github.com" in lowered:
                    findings.append(
                        CapabilityFinding(
                            capability="direct_github_access",
                            status="blocked",
                            path=relative_path,
                            line=getattr(node, "lineno", 1),
                            evidence="contains a direct GitHub host",
                            message="Direct GitHub access is blocked; use the trusted integration helper.",
                        )
                    )
                if lowered == "authorization" or lowered.startswith("bearer "):
                    findings.append(
                        CapabilityFinding(
                            capability="authentication_header",
                            status="blocked",
                            path=relative_path,
                            line=getattr(node, "lineno", 1),
                            evidence="constructs authentication header material",
                            message="Generated code cannot construct provider authentication or bearer headers.",
                        )
                    )
        return findings

    def _web_app_cache_findings(self, tree: ast.AST, relative_path: str) -> list[CapabilityFinding]:
        package_path_names: set[str] = set()
        findings: list[CapabilityFinding] = []
        assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
        for node in assignments:
            value = node.value
            if value is None or not self._contains_name(value, "__file__"):
                continue
            package_path_names.update(self._assignment_names(node))
            if self._joins_cache(value, package_path_names):
                findings.append(self._package_cache_finding(relative_path, node.lineno))
        for node in assignments:
            value = node.value
            if value is None or not self._joins_cache(value, package_path_names):
                continue
            findings.append(self._package_cache_finding(relative_path, node.lineno))
        return findings

    @staticmethod
    def _assignment_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {target.id for target in targets if isinstance(target, ast.Name)}

    def _joins_cache(self, node: ast.AST, package_path_names: set[str]) -> bool:
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            return False
        if self._literal_string(node.right) != "cache":
            return False
        return self._contains_name(node.left, "__file__") or any(
            self._contains_name(node.left, name) for name in package_path_names
        )

    @staticmethod
    def _contains_name(node: ast.AST, name: str) -> bool:
        return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))

    @staticmethod
    def _package_cache_finding(relative_path: str, line: int) -> CapabilityFinding:
        return CapabilityFinding(
            capability="web_app_cache_path",
            status="blocked",
            path=relative_path,
            line=line,
            evidence="resolves cache beneath the read-only package directory",
            message=(
                "Web applications must resolve persistent state from PERSONAL_AGENT_SKILL_CACHE_DIR or ./cache, "
                "not relative to __file__ or another package path."
            ),
        )

    def _import_findings(
        self,
        node: ast.Import | ast.ImportFrom,
        relative_path: str,
    ) -> list[CapabilityFinding]:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        else:
            parent = node.module or ""
            modules = [f"{parent}.{alias.name}".strip(".") for alias in node.names]
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
            if root in CREDENTIAL_STORE_MODULES:
                findings.append(
                    CapabilityFinding(
                        capability="credential_store",
                        status="blocked",
                        path=relative_path,
                        line=node.lineno,
                        evidence=f"imports {module}",
                        message="Generated code cannot import operating-system credential-store APIs.",
                    )
                )
            if root in TEST_ONLY_MODULES:
                findings.append(
                    CapabilityFinding(
                        capability="test_adapter_in_runtime",
                        status="blocked",
                        path=relative_path,
                        line=node.lineno,
                        evidence=f"imports test-only module {module}",
                        message="The deterministic integration adapter is available only to generated tests.",
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
        import_aliases: dict[str, str],
    ) -> CapabilityFinding | None:
        call_name = self._resolve_import_alias(self._call_name(node.func), import_aliases)
        if (
            call_name in {"ctypes.WinDLL", "ctypes.windll.LoadLibrary"}
            or "advapi32.Cred" in call_name
            or any(name in call_name for name in ("CredRead", "CredWrite", "CredDelete"))
        ):
            return CapabilityFinding(
                capability="credential_store",
                status="blocked",
                path=relative_path,
                line=node.lineno,
                evidence=f"calls operating-system credential API {call_name}",
                message="Generated code cannot call operating-system credential-store APIs.",
            )
        if call_name.startswith(NETWORK_CALL_PREFIXES) and node.args:
            url = self._literal_string(node.args[0])
            domain = self._url_domain(url or "")
            if domain:
                if domain in GITHUB_HOSTS:
                    return CapabilityFinding(
                        capability="direct_github_access",
                        status="blocked",
                        path=relative_path,
                        line=node.lineno,
                        evidence=f"calls {call_name} with GitHub domain {domain}",
                        message="Direct GitHub access is blocked; use the trusted integration helper.",
                    )
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
        deletion_path = self._literal_deletion_path(node, call_name)
        if deletion_path is not None and (
            self._unsafe_path(deletion_path) or not self._write_path_allowed(deletion_path, approved_writes)
        ):
            return CapabilityFinding(
                capability="file_deletion",
                status="blocked",
                path=relative_path,
                line=node.lineno,
                evidence=f"deletes literal path {deletion_path}",
                message="Code explicitly deletes a path outside approved runtime filesystem write roots.",
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

    def _integration_call_finding(
        self,
        node: ast.Call,
        relative_path: str,
        import_aliases: dict[str, str],
        declared_operations: set[str],
        selected_operations: set[str],
    ) -> CapabilityFinding | None:
        call_name = self._resolve_import_alias(self._call_name(node.func), import_aliases)
        if call_name not in INTEGRATION_HELPERS:
            return None
        operation_node = next((keyword.value for keyword in node.keywords if keyword.arg == "operation"), None)
        operation_id = self._literal_string(operation_node) if operation_node is not None else None
        if operation_id is None:
            return CapabilityFinding(
                capability="integration_operation",
                status="blocked",
                path=relative_path,
                line=node.lineno,
                evidence="dynamically constructs integration operation identifier",
                message="Integration operation identifiers must be literal and selected by the approved build context.",
            )
        if operation_id not in declared_operations:
            return CapabilityFinding(
                capability="integration_operation",
                status="undeclared",
                path=relative_path,
                line=node.lineno,
                evidence=f"calls undeclared operation {operation_id}",
                message="Generated code invokes an operation absent from the active manifest.",
            )
        if operation_id not in selected_operations:
            return CapabilityFinding(
                capability="integration_operation",
                status="blocked",
                path=relative_path,
                line=node.lineno,
                evidence=f"calls unselected operation {operation_id}",
                message="Generated code invokes an operation outside its approved Builder context.",
            )
        return None

    def _literal_deletion_path(self, node: ast.Call, call_name: str) -> str | None:
        if call_name in DESTRUCTIVE_CALLS:
            return self._literal_string(node.args[0]) if node.args else None
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in DESTRUCTIVE_METHODS:
            return None
        receiver = node.func.value
        if not isinstance(receiver, ast.Call) or self._call_name(receiver.func) not in {"Path", "pathlib.Path"}:
            return None
        return self._literal_string(receiver.args[0]) if receiver.args else None

    def _import_aliases(self, tree: ast.AST) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".", 1)[0]
                    aliases[local_name] = alias.name if alias.asname else local_name
            elif isinstance(node, ast.ImportFrom):
                parent = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    aliases[local_name] = f"{parent}.{alias.name}".strip(".")
        return aliases

    @staticmethod
    def _resolve_import_alias(call_name: str, aliases: dict[str, str]) -> str:
        root, separator, remainder = call_name.partition(".")
        resolved_root = aliases.get(root)
        if resolved_root is None:
            return call_name
        return f"{resolved_root}.{remainder}" if separator else resolved_root

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
        if any(part in secret_name.lower() for part in ("github", "token", "credential", "secret")):
            allowed = False
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
