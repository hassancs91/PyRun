"""
Watcher adapters of the SDK Showcase plugin (``triggers.py``, PyRun 2.5).

Every ``TriggerAdapter`` subclass defined here is offered in the watcher
form's picker as ``"sdk_showcase:<key>"``, in every workspace, once the plugin
is loaded and the doctor's ``triggers`` rule passes. A watcher (made on a
script's Triggers page, or by this plugin with ``WatcherAPI``) calls
``check`` every interval and starts one run of its script per event.

This one watches the showcase's own counter (card 2), so it needs no network
and no account: press **Increment** and the next check starts a run.
"""

from core.plugins.api import DataStoreAPI, TriggerAdapter, TriggerError

OWNER = "sdk_showcase"
STORE_KEY = "state"


class CounterAdapter(TriggerAdapter):
    key = "counter_up"
    title = "Showcase counter went up"
    description = "Starts a run each time the SDK Showcase counter passes a multiple of a number."
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "every": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 1, "title": "Every",
                "description": "A run each time the counter reaches a multiple of this (1 = every increment).",
            },
        },
    }

    @staticmethod
    def _read(ctx):
        store = DataStoreAPI(OWNER, workspace=ctx.workspace).get(STORE_KEY)
        if store is None:
            raise TriggerError("The SDK Showcase demo isn't set up in this workspace yet.")
        # "Reset demo data" starts a new generation: event ids stay unique.
        return int(store.get("counter", 0) or 0), int(store.get("generation", 0) or 0)

    def check(self, ctx, config, state):
        counter, generation = self._read(ctx)
        mark = {"counter": counter, "generation": generation}
        if state is None or state.get("generation") != generation or counter < state.get("counter", 0):
            # First check, or the demo was reset: mark where it is, report nothing.
            return [], mark
        events = []
        every = config["every"]
        last = state["counter"]
        first = (last // every + 1) * every  # the next multiple past the mark
        for value in range(first, counter + 1, every):
            if ctx.limit is not None and len(events) >= ctx.limit:
                # Only move past what is returned: the rest comes next time.
                return events, {"counter": last, "generation": generation}
            events.append({
                "id": f"{generation}:{value}",
                "item": {"counter": value, "message": f"The showcase counter reached {value}."},
            })
            last = value
        return events, mark

    def test(self, ctx, config):
        counter, _ = self._read(ctx)
        every = config["every"]
        return f"The counter is at {counter}; a run starts at every multiple of {every}.", [{"counter": counter}]
