# v0.1.0 validation

Initial hardware validation: 2026-09-13, Windows x64, FreeCAD 1.1.3,
Autodesk Fusion 2704.1.23, FcohBridge internal revision 0.1.1.
These results cover the supplied example and supported subset, not arbitrary CAD models.

| Check | Result |
|---|---|
| Input validation | 5 unittest methods passed, including 10 invalid-model cases |
| Local stdio MCP | Validation, build, status and artifact checks passed for both CAD adapters |
| Sample dimensions | 80 x 40 x 30 mm; 6 mm base; four 6 mm diameter holes |
| FreeCAD volume | 30041.415986824606 mm³; one solid |
| Fusion volume | 30041.41598682458 mm³; one solid |
| Native reopen | FCStd and F3D geometry, history and FCOH metadata checked |
| STEP difference | FreeCAD minus Fusion: 0 mm³; Fusion minus FreeCAD: 0 mm³ |
| Difference tolerance | 0.01 mm³ |
| Parameter variation | Length changed from 80 to 90 mm and regenerated |
| Invalid geometry | A pocket that removes no material was rejected |

## Reproduce

Run the commands in the README's validation section after setup.
Run both MCP adapter checks before the cross-CAD check. Fusion must be running
with the bridge enabled and no interactive modeling command in progress.
Raw evidence is generated into `evidence/`, with CAD artifacts in `outputs/`.
These local directories are not distributed because they contain machine-specific paths.

The release is experimental. No general import, history extraction, round-trip conversion,
assembly support, manufacturing certification or support guarantee is claimed.
