import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DirectChatAdapter(Protocol):
    def answer(self, prompt: str, message: str) -> str:
        pass


class FakeDirectChatAdapter:
    def answer(self, prompt: str, message: str) -> str:
        return "This is a normal chat response. Enable real Codex mode to answer through the Codex CLI."


class RealDirectChatAdapter:
    def __init__(
        self,
        command: str | None = None,
        timeout_seconds: int | None = None,
        workdir: Path | None = None,
        sandbox_mode: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
    ) -> None:
        self.command = command or os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
        self.timeout_seconds = timeout_seconds or _env_int("PERSONAL_AGENT_CODEX_CHAT_TIMEOUT_SECONDS", 120)
        self.workdir = workdir or Path.cwd()
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_CHAT_SANDBOX", "read-only")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")

    def answer(self, prompt: str, message: str) -> str:
        command = [
            self.command,
            "--ask-for-approval",
            self.approval_policy,
            "exec",
            "-C",
            str(self.workdir),
            "--ephemeral",
            "--color",
            "never",
            "--sandbox",
            self.sandbox_mode,
            "-",
        ]
        if self.model:
            command[-1:-1] = ["--model", self.model]
        result = subprocess.run(
            command,
            cwd=self.workdir,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            shell=False,
        )
        if result.returncode != 0:
            reason = result.stderr.strip() or "Codex chat request failed."
            return f"Codex could not answer this chat request: {reason}"
        return result.stdout.strip() or "Codex returned an empty response."


def default_direct_chat_adapter() -> DirectChatAdapter:
    if should_use_real_codex():
        return RealDirectChatAdapter(workdir=Path(__file__).resolve().parents[3])
    return FakeDirectChatAdapter()


def should_use_real_codex() -> bool:
    mode = os.getenv("PERSONAL_AGENT_CODEX_MODE", "auto").strip().lower()
    if mode == "real":
        return True
    if mode in {"fake", "dev", "stub", "local"}:
        return False
    command = os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
    return shutil.which(command) is not None


@dataclass
class DirectChatService:
    adapter: DirectChatAdapter | None = None

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = default_direct_chat_adapter()

    def answer(self, message: str) -> str:
        prompt = self.build_prompt(message)
        return self.adapter.answer(prompt, message)

    def build_prompt(self, message: str) -> str:
        return f"""
You are the chat assistant for the Local-First Self-Extending Personal AI Assistant.

Answer the user's message directly.

Important boundaries:
- This is Chat mode, not Project mode.
- Do not propose, create, modify, install, schedule, or run application skills.
- Do not write files.
- Do not execute commands.
- If the user asks to create a reusable skill, tell them to switch to Project mode.
- If the user asks what you are, say this app is using Codex CLI to generate the response; do not say the user is directly chatting with the Codex CLI itself.
- Keep the answer concise and useful.

User message:
{message}
""".strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
