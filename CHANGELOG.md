# Changelog

## 0.1.0 — 2026-09-13

Initial experimental source release. Free, unsupported OSS under the MIT License.

- Human/LLM-readable FCOH 0.3 subset in YAML, with schema and semantic validation.
- Named dimensions, design intent (`meaning` / `why`) and ordered operations.
- New single-solid part generation using FreeCAD and Autodesk Fusion native APIs.
- Local stdio MCP: capabilities, validate, build and status.
- Isolated per-job outputs: native CAD, STEP, source YAML and result metadata.
- Mounting-bracket example, setup scripts and reproducible validation scripts.
- Real sample validation on both CADs, including native reopen and STEP shape comparison.

Supported: millimetres, XY planes with Z offsets, rectangles/circles, +Z pads and -Z pockets.
Not supported: existing CAD history extraction, STEP/STL input, complete round trips,
fillets, chamfers, general sketch constraints, assemblies or CAD edits written back to YAML.

Use a source checkout on Windows x64. This is not a standalone wheel/PyPI release.
Third-party CAD applications and dependencies retain their own licenses and requirements.
