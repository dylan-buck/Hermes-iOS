from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


MCP_SERVER_NAME = "hermes_mobile"
MCP_TOOL_NAMES = [
    "get_user_location",
    "get_location_history",
    "get_health_summary",
    "get_health_metric",
    "get_health_metrics_list",
    "get_user_activity",
    "get_sensor_schema",
    "query_sensor_data",
]


@dataclass(frozen=True)
class MCPRegistrationStatus:
    server_name: str
    hermes_home: Path
    config_path: Path
    command_path: str | None
    registered: bool
    included_tools: tuple[str, ...] = ()
    test_error: str | None = None


def resolve_hermes_home() -> Path:
    configured = os.getenv("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"


def resolve_mcp_command_path() -> Path:
    # Do NOT call .resolve() on sys.executable here: in a venv,
    # .venv/bin/python is a symlink to the base interpreter (e.g.
    # /opt/miniforge/bin/python3), so resolving it escapes the venv and
    # looks for the console script in the base environment, where it does
    # not exist. Keep the un-resolved path so the venv's own bin/ is used.
    interpreter = Path(sys.executable)
    if interpreter.is_symlink():
        interpreter = interpreter.absolute()

    candidates = [
        interpreter.with_name("hermes-mobile-mcp"),
    ]
    # Also consider the venv root that the interpreter lives in, which
    # covers invocations where sys.executable points at the base python.
    for parent in interpreter.parents:
        if (parent / "pyvenv.cfg").exists():
            candidates.append(parent / "bin" / "hermes-mobile-mcp")
            break

    which_match = shutil.which("hermes-mobile-mcp")
    if which_match:
        candidates.insert(0, Path(which_match))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise RuntimeError(
        "Could not find `hermes-mobile-mcp` in the connector environment. "
        "Reinstall the connector and try again."
    )


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _load_config(config_path: Path) -> CommentedMap:
    yaml = _yaml()
    if not config_path.exists():
        return CommentedMap()

    loaded = yaml.load(config_path.read_text(encoding="utf-8"))
    if isinstance(loaded, CommentedMap):
        return loaded
    if isinstance(loaded, dict):
        return CommentedMap(loaded)
    return CommentedMap()


def _save_config(config_path: Path, config: CommentedMap) -> None:
    yaml = _yaml()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.dump(config, handle)


def _extract_included_tools(entry: dict | None) -> tuple[str, ...]:
    if not isinstance(entry, dict):
        return ()
    tools = entry.get("tools")
    if not isinstance(tools, dict):
        return ()
    include = tools.get("include")
    if not isinstance(include, list):
        return ()
    return tuple(str(item) for item in include)


def register_native_mcp_server(*, state_dir: Path, server_name: str = MCP_SERVER_NAME) -> MCPRegistrationStatus:
    hermes_home = resolve_hermes_home()
    config_path = hermes_home / "config.yaml"
    config = _load_config(config_path)
    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, CommentedMap):
        mcp_servers = CommentedMap(mcp_servers or {})
        config["mcp_servers"] = mcp_servers

    entry = mcp_servers.get(server_name)
    if not isinstance(entry, CommentedMap):
        entry = CommentedMap(entry or {})
        mcp_servers[server_name] = entry

    command_path = str(resolve_mcp_command_path())
    entry["command"] = command_path
    entry["args"] = []
    entry["env"] = CommentedMap({"HERMES_MOBILE_CONNECTOR_HOME": str(state_dir)})
    entry["enabled"] = True
    entry["timeout"] = 60
    entry["connect_timeout"] = 30

    tools = entry.get("tools")
    if not isinstance(tools, CommentedMap):
        tools = CommentedMap(tools or {})
        entry["tools"] = tools
    tools["include"] = list(MCP_TOOL_NAMES)
    tools["resources"] = False
    tools["prompts"] = False
    tools.pop("exclude", None)

    _save_config(config_path, config)
    return MCPRegistrationStatus(
        server_name=server_name,
        hermes_home=hermes_home,
        config_path=config_path,
        command_path=command_path,
        registered=True,
        included_tools=tuple(MCP_TOOL_NAMES),
    )


def inspect_native_mcp_registration(*, server_name: str = MCP_SERVER_NAME) -> MCPRegistrationStatus:
    hermes_home = resolve_hermes_home()
    config_path = hermes_home / "config.yaml"
    config = _load_config(config_path)
    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return MCPRegistrationStatus(
            server_name=server_name,
            hermes_home=hermes_home,
            config_path=config_path,
            command_path=None,
            registered=False,
        )

    entry = mcp_servers.get(server_name)
    if not isinstance(entry, dict):
        return MCPRegistrationStatus(
            server_name=server_name,
            hermes_home=hermes_home,
            config_path=config_path,
            command_path=None,
            registered=False,
        )

    return MCPRegistrationStatus(
        server_name=server_name,
        hermes_home=hermes_home,
        config_path=config_path,
        command_path=entry.get("command"),
        registered=bool(entry.get("enabled", True)),
        included_tools=_extract_included_tools(entry),
    )


def validate_native_mcp_server(
    *,
    hermes_command: str,
    server_name: str = MCP_SERVER_NAME,
    timeout_seconds: float = 30.0,
) -> str | None:
    command_parts = shlex.split(hermes_command)
    if not command_parts:
        return "Hermes command is empty."

    env = os.environ.copy()
    env["HERMES_HOME"] = str(resolve_hermes_home())
    process = subprocess.run(
        [*command_parts, "mcp", "test", server_name],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=env,
        check=False,
    )
    if process.returncode == 0:
        return None

    output = (process.stderr or process.stdout or "").strip()
    return output or f"`{' '.join(command_parts)} mcp test {server_name}` failed with exit code {process.returncode}."


def validate_native_mcp_tools(*, server_name: str = MCP_SERVER_NAME) -> str | None:
    status = inspect_native_mcp_registration(server_name=server_name)
    if not status.registered:
        return f"`{server_name}` is not registered in {status.config_path}."

    missing_tools = [tool for tool in MCP_TOOL_NAMES if tool not in status.included_tools]
    if missing_tools:
        return f"MCP config is missing expected tools: {', '.join(missing_tools)}"

    if status.command_path is None or not Path(status.command_path).exists():
        return "Configured MCP command path does not exist."

    from .mcp_server import (
        get_health_metric,
        get_health_metrics_list,
        get_health_summary,
        get_location_history,
        get_user_location,
    )

    tool_checks = {
        "get_user_location": lambda: get_user_location(),
        "get_location_history": lambda: get_location_history(limit=1),
        "get_health_summary": lambda: get_health_summary(),
        "get_health_metric": lambda: get_health_metric("heart_rate", limit=1),
        "get_health_metrics_list": lambda: get_health_metrics_list(),
    }
    for tool_name, checker in tool_checks.items():
        try:
            checker()
        except Exception as error:  # noqa: BLE001
            return f"MCP tool `{tool_name}` is not callable: {error}"

    return None


def native_mcp_readiness_message(*, hermes_command: str) -> str:
    if _hermes_chat_running(hermes_command):
        return "Reload required — a Hermes chat is already running. Run `/reload-mcp` or restart chat."
    return "Ready now — fresh Hermes chats will load the Hermes Mobile MCP tools."


def _hermes_chat_running(hermes_command: str) -> bool:
    command_parts = shlex.split(hermes_command)
    executable = Path(command_parts[0]).name if command_parts else "hermes"
    process = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return False

    pattern = re.compile(rf"(^|/){re.escape(executable)}(\s|$)")
    current_pid = os.getpid()
    for raw_line in process.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        normalized = command.strip()
        if pattern.search(normalized) and " chat" in normalized:
            return True
    return False
