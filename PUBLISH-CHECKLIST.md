# Publish checklist (Dan, plain words)

Nothing here has been run for you. Claude never published, tagged or pushed anything. The upload step is yours.

**Version to release: 0.3.0.** PyPI currently has 0.1.1 (and 0.1.0), so 0.3.0 is the next number and nothing in this tree needs a new one. It is already 0.3.0 everywhere (`pyproject.toml`, `crashresume.__version__`, the CHANGELOG heading); a test checks they agree. Two behaviours differ from 0.1.1 and are written in the CHANGELOG: a run that tests nothing now fails in the command line and the pytest fixture, and crash points now include file operations.

## Author line (decided)

The package now says **Calas Automations Inc.** as author and maintainer, and the licence line says `Copyright (c) 2026 Calas Automations Inc.`. PyPI will show this forever once uploaded. No e-mail address is in the package (none was given; none was invented).

The web links in the package (`github.com/calasdan-sketch/crashresume`, issues, changelog) were already in the 0.1.1 you published. **Check that this GitHub repository is public.** If it is private, delete the whole `[project.urls]` block in `pyproject.toml` and the `informationUri` line in `src/crashresume/reports.py` before building, and the "uses: calasdan-sketch/crashresume" sentence in the README. Claude could not look (GitHub access was off).

## Check first

1. Open GitHub Actions for the branch. You want green on Linux **and Windows** (Claude has never seen Windows run) for Python 3.9 and 3.12. The new "Type check" step runs on 3.12 only.
2. Merge the `expert-2` branch into `main` the way you normally do. Then, from a clean copy of `main`:

```
python -m venv /tmp/rel && /tmp/rel/bin/pip install build twine
/tmp/rel/bin/python -m build
/tmp/rel/bin/twine check --strict dist/*
```

3. Look at who the package says wrote it (must print `Calas Automations Inc.` twice):

```
unzip -p dist/*.whl '*/METADATA' | grep -E "^(Author|Maintainer)"
```

4. Try the built file in a fresh environment before uploading:

```
python -m venv /tmp/try && /tmp/try/bin/pip install dist/*.whl
/tmp/try/bin/crashresume --version     # must say 0.3.0 and Calas Automations Inc.
/tmp/try/bin/crashresume --selftest    # must end with: selftest: PASS
```

## Release (only after the above)

```
git tag v0.3.0
git push origin main v0.3.0
/tmp/rel/bin/twine upload dist/*       # asks for your PyPI token; a PyPI upload cannot be undone or re-used
```

Change the CHANGELOG heading from "0.3.0 (unreleased)" to "0.3.0" with the date at the same time. If you pin GitHub Actions to commit hashes later, do it in `.github/workflows/ci.yml`.

## After

`pip install crashresume` in a new environment, then run the README quick start once.
