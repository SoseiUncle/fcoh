# FCOH prototype

- PowerShell commands must use PowerShell 7.
- Keep this project separate from sibling projects and ContextSync.
- Source files are authoritative here; generated CAD files go in outputs/.
- Create new CAD documents only. Never overwrite source CAD files.
- CAD APIs run in their own runtimes: FreeCAD's Python and Fusion's main thread.
- MCP is local stdio. No network listener is required.
- Unsupported steps and units must fail explicitly.
