# Changelog

## 0.1.1
- InjectedCrash now derives from BaseException so broad except Exception cannot hide it.
- Fixed substring match that could suppress duplicate-key findings.
- Report.warnings: flags runs that record no effects, and repeated keyless effects.
- Added python -m crashresume, py.typed, project URLs, ruff, Windows CI. Non-ASCII dashes removed from output and docs.
- CLI with no arguments now exits 2.
- README documents the crash model limits.
