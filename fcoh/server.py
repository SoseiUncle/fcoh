"""Local stdio MCP entry point. No global SDK or network port required."""
import argparse
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from .core import FcohError, describe
from .jobs import build, capabilities, status

mcp = MCPServer('fcoh-mcp')


def expected_errors(fn, *args):
    try:
        return fn(*args)
    except (FcohError, FileNotFoundError) as error:
        raise ToolError(str(error)) from error


@mcp.tool()
def fcoh_capabilities() -> dict:
    """Report supported FCOH steps and actual CAD adapter readiness."""
    return capabilities()


@mcp.tool()
def fcoh_validate(file: str) -> dict:
    """Validate a project-relative .fcoh.yaml file without creating a CAD document."""
    return expected_errors(describe, file)


@mcp.tool()
def fcoh_build(file: str, cad: str = 'freecad') -> dict:
    """Create a NEW part under outputs/<job_id>. cad: freecad or fusion. Fusion is asynchronous; use fcoh_status."""
    return expected_errors(build, file, cad)


@mcp.tool()
def fcoh_status(job_id: str) -> dict:
    """Read CAD generation status, measured geometry, and exported artifact paths."""
    return expected_errors(status, job_id)


def main():
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
