"""Plugin reload must tolerate unrelated imports changing the module registry."""

from types import SimpleNamespace

from hermes_cli import plugins_loader


def test_reload_eviction_survives_an_unrelated_import(monkeypatch):
    modules = {}

    class ImportingKey(str):
        def startswith(self, prefix, *args):
            modules["unrelated.imported"] = imported
            return super().startswith(prefix, *args)

    imported = object()
    unrelated = object()
    modules.update({
        "hermes_plugins.reload": object(),
        ImportingKey("hermes_plugins.reload.adapter"): object(),
        "hermes_plugins.reload_other": unrelated,
    })
    monkeypatch.setattr(plugins_loader, "sys", SimpleNamespace(modules=modules))

    plugins_loader._evict_modules("hermes_plugins.reload")

    assert modules == {"hermes_plugins.reload_other": unrelated, "unrelated.imported": imported}
