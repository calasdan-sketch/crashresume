"""The pytest plugin ships a working fixture - exercise it from a real test run."""
PLUGIN = """
STORES = {}


def store_for(ctx):
    return STORES.setdefault(ctx.run_id, {"done": set()})


def test_conform_fixture_passes(conform):
    class Agent:
        def __init__(self, ctx):
            self.ctx = ctx
            self.store = store_for(ctx)
            self.done = set(self.store["done"])

        def step(self, item):
            if item in self.done or item in self.ctx.effects.keys():
                self.done.add(item)
                self.store["done"] = set(self.done)
                return
            self.ctx.effect("e", item, key=item)
            self.done.add(item)
            self.store["done"] = set(self.done)

        def state(self):
            return {"done": sorted(self.store["done"])}

    conform(lambda ctx: Agent(ctx), ["a", "b"])


def test_conform_fixture_fails_on_replay(conform):
    class Agent:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            self.ctx.effect("e", item, key=item)

    import pytest
    with pytest.raises(AssertionError):
        conform(lambda ctx: Agent(ctx), ["a"])
"""


def test_plugin_fixture_registered_and_asserting(tmp_path, pytester):
    pytester.makepyfile(PLUGIN)
    # Expose the shipped fixture via conftest instead of -p: when the package is
    # pip-installed (CI), its pytest11 entry point already registers the plugin,
    # and an explicit -p load makes pluggy reject the double registration.
    # The conftest import works in both installed and PYTHONPATH-only runs.
    pytester.makeconftest("from crashresume.pytest_plugin import conform  # noqa: F401")
    result = pytester.runpytest()
    assert result.ret == 0
