# 检查待发布文件与历史配置中的常见隐私风险且不回显密钥内容

from __future__ import annotations

import re
import subprocess
from pathlib import Path


SECRET_PATTERNS = {
    "OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "Bearer token": re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~-]{24,}"),
}

CONFIG_SECRET_FIELDS = {
    ("server", "password"): "server.password",
    ("llm", "api_key"): "llm.api_key",
    ("service", "api_token"): "service.api_token",
}


def _git(*args: str) -> bytes:
    """读取 Git 命令输出"""

    return subprocess.check_output(("git", *args), stderr=subprocess.DEVNULL)


def _find_nonempty_config_secrets(content: bytes) -> list[str]:
    """从简单 YAML 配置中识别非空敏感字段"""

    section = ""
    findings: list[str] = []
    for raw_line in content.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        top_level = re.match(r"^([A-Za-z_][\w-]*):\s*(?:#.*)?$", raw_line)
        if top_level:
            section = top_level.group(1)
            continue
        nested = re.match(r"^\s+([A-Za-z_][\w-]*):\s*(.*?)\s*$", raw_line)
        if not nested:
            continue
        field = CONFIG_SECRET_FIELDS.get((section, nested.group(1)))
        value = nested.group(2).split(" #", 1)[0].strip()
        if field and value not in {"", "''", '""', "null", "~"}:
            findings.append(field)
    return findings


def audit_current_files() -> list[tuple[str, str]]:
    """扫描当前待提交文本中的常见密钥格式"""

    names = _git("ls-files", "-co", "--exclude-standard").decode("utf-8").splitlines()
    findings: list[tuple[str, str]] = []
    for name in names:
        path = Path(name)
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"\0" in content[:4096]:
            continue
        for risk_type, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append((name, risk_type))
    return findings


def audit_config_history() -> list[tuple[str, str]]:
    """检查历史配置中的非空敏感字段"""

    commits = _git("rev-list", "--all", "--", "config.yaml").decode("ascii").splitlines()
    findings: list[tuple[str, str]] = []
    for commit in commits:
        try:
            content = _git("show", f"{commit}:config.yaml")
        except subprocess.CalledProcessError:
            continue
        for field in _find_nonempty_config_secrets(content):
            findings.append((commit[:12], field))
    return findings


def main() -> int:
    """运行隐私审计并用退出码表示是否发现风险"""

    current = audit_current_files()
    history = audit_config_history()
    print(f"CURRENT_SECRET_PATTERNS: {current or 'none'}")
    print(f"HISTORY_SECRET_FIELDS: {history or 'none'}")
    return 1 if current or history else 0


if __name__ == "__main__":
    raise SystemExit(main())
