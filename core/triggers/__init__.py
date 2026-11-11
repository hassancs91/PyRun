"""
Triggers (2.5 Stage 7): watchers that poll an outside source and start a run
per new item.

- ``core.triggers.adapters``: the adapter base (``Adapter``, ``CheckContext``,
  ``validate_config``) and the built-in adapters (``http_poll``,
  ``rss_new_item``).
- ``core.triggers.registry``: every adapter a watcher can use, by key.
- ``core.triggers.runner``: one check of one watcher (claim, maintenance,
  adapter call, runs, state) and the minute tick over all due watchers.
"""
