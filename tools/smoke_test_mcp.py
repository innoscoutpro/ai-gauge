"""Exercise a packaged AI Gauge MCP helper over its real stdio transport."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "check_current_account_usage",
    "check_usage_guard",
    "get_ai_usage",
    "recommend_ai_account",
}


def _helper_path(value: str) -> Path:
    path = Path(value).resolve()
    if sys.platform == "win32" and path.suffix.lower() != ".exe":
        path = path.with_suffix(".exe")
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"MCP helper not found: {path}")
    return path


async def _smoke_test(helper: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-gauge-mcp-smoke-") as appdata:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            params = StdioServerParameters(
                command=str(helper),
                args=["--account-id", "codex"],
                # app_data_dir() reads a different variable per OS, and the MCP
                # client inherits the real HOME by default. Redirect all three
                # so this never reads (or fails against) a maintainer's own
                # AI Gauge configuration when run outside CI.
                env={
                    "APPDATA": appdata,
                    "HOME": appdata,
                    "XDG_CONFIG_HOME": appdata,
                },
            )
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    if initialized.serverInfo.name != "AI Gauge":
                        raise RuntimeError(
                            f"unexpected server name: {initialized.serverInfo.name!r}"
                        )
                    tools = await session.list_tools()
                    tool_names = {tool.name for tool in tools.tools}
                    if tool_names != EXPECTED_TOOLS:
                        raise RuntimeError(
                            f"unexpected MCP tools: {sorted(tool_names)!r}"
                        )
                    guard = await session.call_tool("check_current_account_usage", {})
                    content = guard.structuredContent or {}
                    if content.get("allowed") is not False:
                        raise RuntimeError(f"guard did not fail closed: {content!r}")

            errlog.seek(0)
            errors = errlog.read()
            if "Traceback (most recent call last)" in errors:
                raise RuntimeError(f"MCP helper emitted a traceback:\n{errors}")
            return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("helper", type=_helper_path)
    args = parser.parse_args()
    errors = asyncio.run(_smoke_test(args.helper))
    print("MCP packaged-helper handshake OK")
    if errors.strip():
        print("MCP helper stderr (non-fatal):", file=sys.stderr)
        print(errors.rstrip(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
