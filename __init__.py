"""breadcrumbs: a Hermes memory provider with exact provenance (plugin entry point).

The plugin catalog installs this repository as the plugin directory; Hermes loads this file as a
package, so the import below is relative. See README.md.
"""
from .breadcrumbs.ace import Ace, wanted
from .breadcrumbs.hermes import BreadcrumbsProvider, register_plugin


def register(ctx):
    # Hermes finds a plugin's memory provider and context engine by these two calls in this file's text, so
    # they stay here; register_plugin adds the rest.
    provider = BreadcrumbsProvider()
    ctx.register_memory_provider(provider)
    register_plugin(ctx, provider)  # the handbook, the intercept and the islands
    if hasattr(ctx, "register_context_engine") and wanted(name=provider.name):  # ACE: where context.engine names it
        ctx.register_context_engine(Ace(name=provider.name))
