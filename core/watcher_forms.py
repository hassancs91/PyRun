"""
The watcher form (2.5 Stage 7, T31): one form for creating and editing a
watcher with a given adapter.

The adapter's settings are not fixed fields: they are generated from its
``config_schema`` (the same field builder as a script's Run now form,
``core.forms._input_form_field``), named ``cfg_<setting>`` so a setting can't
collide with the watcher's own fields, and checked with
``core.triggers.adapters.validate_config``.
"""

import json

from django import forms
from django.db.models import Q

from core.forms import CHECK_CLASS, INPUT_CLASS, _input_form_field
from core.models import Connection, Script, Watcher
from core.services.script_inputs import input_fields
from core.triggers.adapters import validate_config

CONFIG_PREFIX = "cfg_"
# Longest input map accepted (entries).
INPUT_MAP_MAX = 50


class _ScriptChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


class _ConnectionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.name} ({obj.provider})"


class WatcherForm(forms.Form):
    """Create or edit a watcher of ``workspace`` that uses ``adapter``.

    ``save()`` writes the watcher (``instance``, or a new one). Changing the
    adapter, its settings or the connection resets the watcher's state, so the
    next check only marks where the source is; turning a watcher back on
    clears its failure count and the auto-disable mark.
    """

    name = forms.CharField(max_length=120, widget=forms.TextInput(attrs={"class": INPUT_CLASS}))
    script = _ScriptChoice(
        queryset=Script.objects.none(), widget=forms.Select(attrs={"class": INPUT_CLASS}),
        help_text="It starts one run of this script per new item.",
    )
    interval_seconds = forms.IntegerField(
        label="Check every (seconds)",
        min_value=Watcher.INTERVAL_MIN_SECONDS, max_value=Watcher.INTERVAL_MAX_SECONDS,
        initial=Watcher.DEFAULT_INTERVAL_SECONDS,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "step": "1"}),
        help_text=f"At least {Watcher.INTERVAL_MIN_SECONDS} seconds.",
    )
    fire_cap_per_hour = forms.IntegerField(
        label="Most runs an hour",
        min_value=1, max_value=Watcher.FIRE_CAP_MAX, initial=Watcher.DEFAULT_FIRE_CAP,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "step": "1"}),
        help_text="New items past this wait for the next hour.",
    )
    input_map = forms.CharField(
        label="Script inputs from the item", required=False,
        widget=forms.Textarea(attrs={
            "class": INPUT_CLASS + " font-mono text-xs", "rows": 3, "spellcheck": "false",
            "placeholder": '{"title": "item.title"}',
        }),
        help_text='A JSON object: the script\'s input name, then a dotted path in the event '
                  '("item.title", "event_id"). Leave empty to read the event in the script instead.',
    )
    enabled = forms.BooleanField(
        label="On", required=False, initial=True, widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
    )

    def __init__(self, workspace, adapter, *args, instance=None, **kwargs):
        self.workspace = workspace
        self.adapter = adapter
        self.instance = instance
        if instance is not None and not args and "data" not in kwargs:
            kwargs["initial"] = {**self._initial_from(instance), **(kwargs.get("initial") or {})}
        super().__init__(*args, **kwargs)
        # Scripts of the workspace that aren't archived (plus the current one).
        current = instance.script_id if instance is not None else None
        self.fields["script"].queryset = Script.objects.filter(
            Q(archived_at__isnull=True) | Q(pk=current), workspace=workspace,
        ).order_by("name")
        if adapter.requires_connection:
            connections = Connection.objects.filter(workspace=workspace)
            if adapter.providers:
                connections = connections.filter(provider__in=adapter.providers)
            self.fields["connection"] = _ConnectionChoice(
                queryset=connections.order_by("name"), widget=forms.Select(attrs={"class": INPUT_CLASS}),
                help_text="The account the watcher checks as.",
            )
        self.config_specs = input_fields(adapter.config_schema)
        stored = (instance.config if instance is not None and instance.adapter == adapter.key else None) or {}
        for spec in self.config_specs:
            field = _input_form_field(spec)
            if spec.name in stored:
                field.initial = self._initial_value(spec, stored[spec.name])
            self.fields[CONFIG_PREFIX + spec.name] = field

    @staticmethod
    def _initial_from(watcher):
        return {
            "name": watcher.name,
            "script": watcher.script_id,
            "connection": watcher.connection_id,
            "interval_seconds": watcher.interval_seconds,
            "fire_cap_per_hour": watcher.fire_cap_per_hour,
            "input_map": json.dumps(watcher.input_map, indent=2) if watcher.input_map else "",
            "enabled": watcher.enabled,
        }

    @staticmethod
    def _initial_value(spec, value):
        if spec.kind == "select":
            for i, choice in enumerate(spec.choices):
                if type(choice) is type(value) and choice == value:
                    return str(i)
            return None
        return value

    def config_fields(self):
        """The bound fields of the adapter's settings, in the schema's order."""
        return [self[CONFIG_PREFIX + spec.name] for spec in self.config_specs]

    def clean_input_map(self):
        text = (self.cleaned_data.get("input_map") or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError:
            raise forms.ValidationError("Enter a JSON object.") from None
        if not isinstance(value, dict):
            raise forms.ValidationError("Enter a JSON object.")
        if len(value) > INPUT_MAP_MAX:
            raise forms.ValidationError(f"At most {INPUT_MAP_MAX} inputs.")
        for name, path in value.items():
            if not isinstance(path, str) or not path.strip():
                raise forms.ValidationError(f'"{name}": the path must be text, like "item.title".')
        return {name: path.strip() for name, path in value.items()}

    def clean(self):
        cleaned = super().clean()
        config = {}
        for spec in self.config_specs:
            key = CONFIG_PREFIX + spec.name
            if key not in cleaned:
                continue  # the field's own error is shown
            value = cleaned[key]
            if spec.kind == "select":
                value = spec.choices[int(value)] if value not in (None, "") else None
            config[spec.name] = value
        valid, errors = validate_config(self.adapter.config_schema, config)
        for name, reason in errors.items():
            key = CONFIG_PREFIX + name
            if key in self.fields and key not in self.errors:
                self.add_error(key, reason)
            elif not name:
                self.add_error(None, reason)
        cleaned["config"] = valid

        script = cleaned.get("script")
        input_map = cleaned.get("input_map")
        if script is not None and input_map:
            declared = set((script.inputs_schema or {}).get("properties", {}))
            unknown = sorted(name for name in input_map if name not in declared)
            if not declared:
                self.add_error("input_map", f'The script "{script.name}" declares no inputs.')
            elif unknown:
                self.add_error("input_map", "Not inputs of the script: " + ", ".join(unknown) + ".")
        return cleaned

    def save(self, created_by=None) -> Watcher:
        data = self.cleaned_data
        watcher = self.instance or Watcher(workspace=self.workspace, created_by=created_by)
        before = (watcher.adapter, watcher.config, watcher.connection_id) if self.instance else None
        was_enabled = watcher.enabled if self.instance else None
        watcher.name = data["name"]
        watcher.script = data["script"]
        watcher.adapter = self.adapter.key
        watcher.config = data["config"]
        watcher.connection = data.get("connection") if self.adapter.requires_connection else None
        watcher.interval_seconds = data["interval_seconds"]
        watcher.fire_cap_per_hour = data["fire_cap_per_hour"]
        watcher.input_map = data["input_map"]
        watcher.enabled = data["enabled"]
        if before is not None and before != (watcher.adapter, watcher.config, watcher.connection_id):
            # A different source: what was seen of the old one means nothing.
            watcher.state = None
            watcher.next_check_at = None
        if watcher.enabled and was_enabled is False:
            watcher.auto_disabled_at = None
            watcher.error_count = 0
            watcher.next_check_at = None
        watcher.save()
        return watcher
