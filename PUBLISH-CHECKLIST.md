# Publish checklist (Dan, plain words)

Nothing here has been run. Claude never published, tagged or pushed anything.

**Version to release: 0.3.0.** PyPI currently has 0.1.1 (and 0.1.0). The repo is already 0.3.0 everywhere (`pyproject.toml`, `__version__`, CHANGELOG heading). It is not a "breaking" release in the strict sense, but two behaviours changed since 0.1.1: a run that tests nothing now fails in the CLI and the pytest fixture, and crash points now include file operations. Both are in the CHANGELOG.

## Check first

1. Decide the author line. The package says `calasdan-sketch`; PyPI will show it forever. Change `authors` in `pyproject.toml` and `LICENSE` if you want your name or Calas Automations Inc. there.
2. Look at GitHub Actions on the branch. You want green on Linux **and Windows** (never seen by Claude) for Python 3.9 and 3.12.
3. Merge the `expert-2` branch into `main` the way you normally do. Then, from a clean copy of `main`:

```
python -m venv /tmp/rel && /tmp/rel/bin/pip install build twine
/tmp/rel/bin/python -m build
/tmp/rel/bin/twine check --strict dist/*
```

4. Try the built file in a fresh environment before uploading:

```
python -m venv /tmp/try && /tmp/try/bin/pip install dist/*.whl
/tmp/try/bin/crashresume --version     # must say 0.3.0
/tmp/try/bin/crashresume --selftest    # must end with: selftest: PASS
```

## Release (only after the above)

```
git tag v0.3.0
git push origin main v0.3.0
/tmp/rel/bin/twine upload dist/*       # asks for your PyPI token; PyPI uploads cannot be undone or re-used
```

Change the CHANGELOG heading from "0.3.0 (unreleased)" to "0.3.0" with the date at the same time. If you pin GitHub Actions to commit hashes later, do it in `.github/workflows/ci.yml`.

## After

`pip install crashresume` in a new environment, then run the README quick start once.
