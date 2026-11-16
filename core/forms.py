"""
Forms for the core app.
"""
import calendar
import codecs
import json
import math
import re
from zoneinfo import available_timezones

from django import forms
from django.utils.text import slugify

from core.api.exceptions import ApiError
# JSON parsing that refuses NaN/Infinity, which neither database stores as JSON.
from core.api.schema import loads as strict_json_loads
from core.models import Script, Environment, ScriptSchedule, Tag, DataStore, DataStoreEntry, APIToken, Database, Library, MaintenanceWindow, Secret, SecretProvider
from core.services import EnvironmentService
from core.services.script_env import RESERVED_NAMES, EnvVarsError, check_env_vars
from core.services.script_inputs import (
    ScriptInputsError,
    check_inputs_schema,
    input_fields,
    prepare_inputs,
)
from core.api.script_tools import (
    default_name as default_tool_name,
    name_holder as tool_name_holder,
)


# Regex pattern for secret key validation (uppercase, numbers, underscores, must start with letter)
SECRET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


# Shared form-control classes (console design tokens — theme-aware via CSS variables)
INPUT_CLASS = (
    "w-full px-3.5 py-2.5 bg-ink border border-line rounded-lg text-text text-sm "
    "placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 "
    "focus:border-ok/60 transition-colors"
)
CHECK_CLASS = "w-4 h-4 rounded text-ok bg-ink border-line focus:ring-ok/40 focus:ring-2"
FILE_CLASS = (
    "block w-full text-sm text-muted file:mr-3 file:py-2 file:px-3.5 file:rounded-lg "
    "file:border-0 file:text-sm file:font-medium file:bg-ok file:text-ink "
    "hover:file:opacity-90 cursor-pointer"
)


class PluginUploadForm(forms.Form):
    """Upload a plugin packaged as a ``.zip`` (superuser only)."""

    plugin_file = forms.FileField(
        label="Plugin .zip",
        help_text="A .zip whose single top-level folder is the plugin and contains "
        "plugin.json (slug, name, version).",
        widget=forms.ClearableFileInput(attrs={"class": FILE_CLASS, "accept": ".zip"}),
    )

    def clean_plugin_file(self):
        f = self.cleaned_data["plugin_file"]
        if not (f.name or "").lower().endswith(".zip"):
            raise forms.ValidationError("Plugin must be a .zip file.")
        return f


# Common timezone choices (sorted, common ones first)
COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Australia/Sydney",
]


def get_timezone_choices():
    """Timezone choices grouped Common/All.

    Optgroups (not a fake separator row) so the visual divider is not a
    submittable value — a plain separator choice used to validate and could be
    stored as timezone="---".
    """
    all_tz = sorted(available_timezones())
    common = [(tz, tz) for tz in COMMON_TIMEZONES if tz in all_tz]
    others = [(tz, tz) for tz in all_tz if tz not in COMMON_TIMEZONES]
    return [("Common", common), ("All timezones", others)]


class InputsSchemaField(forms.CharField):
    """The script's inputs schema as JSON text (the Inputs tab writes it).

    Blank means the script declares no inputs (stored as NULL), not ``null``.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault(
            "widget",
            forms.Textarea(
                attrs={
                    "class": INPUT_CLASS + " font-mono text-xs",
                    "rows": 14,
                    "spellcheck": "false",
                    "placeholder": '{"type": "object", "properties": {"city": {"type": "string"}}}',
                }
            ),
        )
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, dict):
            return json.dumps(value, indent=2)
        return "" if value is None else value

    def to_python(self, value):
        text = super().to_python(value)
        if not text:
            return None
        try:
            return strict_json_loads(text)
        except ApiError:
            raise forms.ValidationError("The inputs schema isn't valid JSON.") from None


class EnvVarsWidget(forms.HiddenInput):
    """The editor's Env vars rows: inputs named ``<name>_name`` / ``<name>_value``.

    The template draws the rows itself (``ScriptForm.env_var_rows``); the widget
    renders only the hidden ``<name>`` marker. A form posted without the marker
    (no Env vars tab: an older page, a script) leaves the saved variables alone.
    """

    # A visible field all the same, so the error summary links to the tab.
    is_hidden = False

    def format_value(self, value):
        return "1"

    @staticmethod
    def _list(data, key):
        getlist = getattr(data, "getlist", None)
        if getlist is not None:  # a QueryDict: a real POST
            return list(getlist(key))
        value = data.get(key, [])
        return list(value) if isinstance(value, (list, tuple)) else [value]

    def value_from_datadict(self, data, files, name):
        if name not in data:
            return None
        names = self._list(data, name + "_name")
        values = self._list(data, name + "_value")
        values += [""] * (len(names) - len(values))
        return list(zip(names, values))

    def value_omitted_from_data(self, data, files, name):
        return name not in data


class EnvVarsField(forms.Field):
    """``(name, value)`` rows in, the ``{NAME: value}`` dict to store out."""

    widget = EnvVarsWidget

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def to_python(self, value):
        if value is None or isinstance(value, dict):
            return value
        try:
            return check_env_vars(value)
        except EnvVarsError as e:
            raise forms.ValidationError(e.messages) from None


class ScriptForm(forms.ModelForm):
    """Form for creating and editing scripts."""

    inputs_schema = InputsSchemaField(label="Inputs")
    env_vars = EnvVarsField(label="Env vars")

    class Meta:
        model = Script
        fields = [
            "name",
            "description",
            "code",
            "inputs_schema",
            "env_vars",
            "environment",
            "tags",
            "timeout_seconds",
            "stop_grace_seconds",
            "concurrency_policy",
            "retry_max_attempts",
            "retry_delay_seconds",
            "retry_backoff_multiplier",
            "retry_on_timeout",
            "is_enabled",
            "expose_as_tool",
            "tool_name",
            "isolation_mode",
            "injection_mode",
            "notify_on",
            "notify_email",
            "notify_webhook_url",
            "notify_webhook_enabled",
            "notify_channels",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "My script name"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What does this script do?",
                }
            ),
            "code": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS + " font-mono",
                    "rows": 15,
                    "placeholder": '# Your Python code here\nprint("Hello, World!")',
                }
            ),
            "environment": forms.Select(attrs={"class": INPUT_CLASS}),
            "tags": forms.CheckboxSelectMultiple(attrs={"class": "tag-checkbox"}),
            "timeout_seconds": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 1, "max": 86400}
            ),
            "stop_grace_seconds": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 0, "max": Script.MAX_STOP_GRACE_SECONDS}
            ),
            "concurrency_policy": forms.Select(attrs={"class": INPUT_CLASS}),
            "retry_max_attempts": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 0, "max": Script.MAX_RETRY_ATTEMPTS}
            ),
            "retry_delay_seconds": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 0, "max": Script.MAX_RETRY_DELAY_SECONDS}
            ),
            "retry_backoff_multiplier": forms.NumberInput(
                attrs={
                    "class": INPUT_CLASS,
                    "min": 1,
                    "max": Script.MAX_RETRY_BACKOFF_MULTIPLIER,
                    "step": "0.1",
                }
            ),
            "retry_on_timeout": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            "is_enabled": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            "expose_as_tool": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            "tool_name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS + " font-mono",
                    "placeholder": "weekly_report",
                    "autocomplete": "off",
                    "spellcheck": "false",
                }
            ),
            "isolation_mode": forms.Select(attrs={"class": INPUT_CLASS}),
            "injection_mode": forms.Select(attrs={"class": INPUT_CLASS}),
            "notify_on": forms.Select(attrs={"class": INPUT_CLASS}),
            "notify_email": forms.EmailInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "Override default email (optional)",
                }
            ),
            "notify_webhook_url": forms.URLInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "https://your-service.com/webhook",
                }
            ),
            "notify_webhook_enabled": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            "notify_channels": forms.CheckboxSelectMultiple(attrs={"class": CHECK_CLASS}),
        }
        labels = {
            "name": "Script Name",
            "description": "Description",
            "code": "Python Code",
            "environment": "Environment",
            "tags": "Tags",
            "timeout_seconds": "Timeout (seconds)",
            "stop_grace_seconds": "Stop grace period (seconds)",
            "concurrency_policy": "If it's already running",
            "retry_max_attempts": "Retries on failure",
            "retry_delay_seconds": "Delay before a retry (seconds)",
            "retry_backoff_multiplier": "Backoff multiplier",
            "retry_on_timeout": "Retry timeouts too",
            "is_enabled": "Enabled",
            "expose_as_tool": "Expose as MCP tool",
            "tool_name": "Tool name",
            "isolation_mode": "Execution Isolation",
            "injection_mode": "Secret injection",
            "notify_on": "Notify On",
            "notify_email": "Notification Email",
            "notify_webhook_url": "Webhook URL",
            "notify_webhook_enabled": "Enable Webhook",
            "notify_channels": "Notify channels",
        }
        help_texts = {
            "timeout_seconds": "Maximum execution time (1 second to 24 hours)",
            "stop_grace_seconds": "When the run is stopped or times out, the script "
            "gets SIGTERM and this long to shut down before it is killed "
            "(0 = kill at once, up to 60). See pyrunner_run.on_stop().",
            "concurrency_policy": "What a new trigger does while a run is still going. "
            "Allow starts another run alongside it. Skip doesn't start it and lists "
            "it as skipped. Queue starts it when the current run ends, one at a time, "
            "in order. Applies to Run now, schedules, webhooks, chat and plugins.",
            "retry_max_attempts": "How many more times a failed run is tried, "
            "with the same inputs (0 = never, up to 10). Only the last attempt notifies.",
            "retry_delay_seconds": "Wait before the first retry (up to a day). PyRun "
            "checks for due retries once a minute, so shorter delays act like about a minute.",
            "retry_backoff_multiplier": "Each retry waits this many times longer than "
            "the one before: 1 keeps the same delay, 2 gives 1x, 2x, 4x.",
            "isolation_mode": "Run sandboxed. Effective only when the workspace policy is 'optional' (a 'required' workspace always sandboxes).",
            "injection_mode": "All = every workspace secret (default). Selected = only the secrets you attach below.",
            "notify_email": "Leave empty to use global default",
            "notify_webhook_url": "URL to POST notifications to when script completes",
            "tool_name": "Lowercase letters, digits and underscores, up to "
            f"{Script.TOOL_NAME_MAX_LENGTH} characters, unique in this workspace. "
            "Leave it blank to use the script's name.",
        }

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._workspace = workspace
        # Only show active environments
        self.fields["environment"].queryset = Environment.objects.filter(is_active=True)
        # Preselect the default environment on the create form (the field is
        # required with no model default, so it otherwise renders on the empty
        # "---------" choice even though a default environment exists).
        # (instance pk can't distinguish create from edit here — Script's UUID
        # pk is filled at instantiation — so use _state.adding.)
        if (
            not self.is_bound
            and self.instance._state.adding
            and "environment" not in self.initial
        ):
            default_env = Environment.objects.filter(
                is_active=True, is_default=True
            ).first()
            if default_env:
                self.initial["environment"] = default_env.pk
        # Optional: a form submitted without it (or legacy callers) defaults to
        # 'all' in clean_injection_mode, matching the model default.
        self.fields["injection_mode"].required = False
        # Same for the concurrency policy, which then keeps its saved value.
        self.fields["concurrency_policy"].required = False
        # And for the retry numbers (the checkbox posts nothing when off) and
        # the stop grace period.
        for name in (
            "retry_max_attempts",
            "retry_delay_seconds",
            "retry_backoff_multiplier",
            "stop_grace_seconds",
        ):
            self.fields[name].required = False
        # Scope the notify-channels picker to the active workspace (tenancy);
        # None ⇒ empty so a channel never leaks across workspaces in the picker.
        from core.models import Channel

        self.fields["notify_channels"].required = False
        self.fields["notify_channels"].queryset = (
            Channel.objects.for_workspace(workspace)
            if workspace is not None
            else Channel.objects.none()
        )

    def clean_injection_mode(self):
        return self.cleaned_data.get("injection_mode") or Script.InjectionMode.ALL

    def clean_concurrency_policy(self):
        return (
            self.cleaned_data.get("concurrency_policy")
            or self.instance.concurrency_policy
            or Script.ConcurrencyPolicy.ALLOW
        )

    def _saved_if_blank(self, name):
        """A retry number posted blank (or not at all) keeps its saved value."""
        value = self.cleaned_data.get(name)
        if value is None:
            value = getattr(self.instance, name)
        return value

    def clean_retry_max_attempts(self):
        return self._saved_if_blank("retry_max_attempts")

    def clean_retry_delay_seconds(self):
        return self._saved_if_blank("retry_delay_seconds")

    def clean_retry_backoff_multiplier(self):
        return self._saved_if_blank("retry_backoff_multiplier")

    def clean_stop_grace_seconds(self):
        return self._saved_if_blank("stop_grace_seconds")

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip()
        if not code:
            raise forms.ValidationError("Script code cannot be empty.")
        return code

    def clean_inputs_schema(self):
        try:
            return check_inputs_schema(self.cleaned_data.get("inputs_schema"))
        except ValueError as e:
            raise forms.ValidationError(str(e)) from None

    def clean_env_vars(self):
        value = self.cleaned_data.get("env_vars")
        if value is None:  # the Env vars rows weren't posted: keep what's saved
            value = self.instance.env_vars or {}
        return value

    def env_var_rows(self):
        """The Env vars tab's rows: as posted (so a refused save keeps what was
        typed), else as saved, sorted by name (PostgreSQL doesn't keep the
        order they were typed in)."""
        if self.is_bound:
            posted = self["env_vars"].data
            if posted is not None:
                return [{"name": n, "value": v} for n, v in posted if n or v]
        saved = self.initial.get("env_vars") or self.instance.env_vars or {}
        return [{"name": n, "value": saved[n]} for n in sorted(saved)]

    @property
    def opening_tab(self):
        """The editor tab shown first: the one holding an error, else Code."""
        if self.errors.get("inputs_schema"):
            return "inputs"
        if self.errors.get("env_vars"):
            return "env"
        return "code"

    def clean_timeout_seconds(self):
        timeout = self.cleaned_data.get("timeout_seconds")
        if timeout is not None and (timeout < 1 or timeout > 86400):
            raise forms.ValidationError("Timeout must be between 1 and 86400 seconds (24 hours).")
        return timeout

    def clean_tool_name(self):
        return (self.cleaned_data.get("tool_name") or "").strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("expose_as_tool") and "tool_name" in cleaned:
            self._clean_exposed_tool_name(cleaned)
        return cleaned

    def _clean_exposed_tool_name(self, cleaned):
        """An exposed script's tool name: typed, or made from the script's
        name when left blank, and not used by another exposed script in the
        workspace (the unique constraint names the workspace, which isn't a
        form field, so the form checks it here)."""
        name, typed = cleaned["tool_name"], bool(cleaned["tool_name"])
        if not typed:
            name = default_tool_name(cleaned.get("name") or self.instance.name)
            if not name:
                self.add_error(
                    "tool_name",
                    "Give the tool a name: lowercase letters, digits and underscores, "
                    "starting with a letter.",
                )
                return
            cleaned["tool_name"] = name
        workspace = self.instance.workspace_id or getattr(self._workspace, "pk", None)
        holder = tool_name_holder(workspace, name, exclude_pk=self.instance.pk)
        if holder is not None:
            archived = " (archived)" if holder.archived_at else ""
            which = "this tool name" if typed else f'the tool name "{name}"'
            self.add_error(
                "tool_name",
                f'The script "{holder.name}"{archived} already uses {which}. Pick another.',
            )


class _NumberInputField(forms.Field):
    """A JSON number. Whole-number text stays an int, so 3 reaches the script as "3"."""

    widget = forms.NumberInput

    def to_python(self, value):
        text = value.strip() if isinstance(value, str) else value
        if text in (None, ""):
            return None
        try:
            number = int(text) if re.fullmatch(r"[+-]?\d+", str(text)) else float(text)
        except (TypeError, ValueError):
            raise forms.ValidationError("Enter a number.") from None
        if isinstance(number, float) and not math.isfinite(number):
            raise forms.ValidationError("Enter a finite number.")
        return number


class _JSONInputField(forms.CharField):
    """Any JSON value typed as text. Blank = not given."""

    def to_python(self, value):
        text = super().to_python(value)
        if not text:
            return None
        try:
            return strict_json_loads(text)
        except ApiError:
            raise forms.ValidationError("Enter valid JSON.") from None


def _input_form_field(spec):
    """The form field for one declared input (a core.services.script_inputs.InputField)."""
    common = {"label": spec.label, "help_text": spec.help, "required": spec.required}
    initial = spec.default if spec.has_default else None
    if spec.kind == "boolean":
        common["required"] = False  # an unticked box is a value: false
        return forms.BooleanField(
            initial=bool(initial),
            widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            **common,
        )
    if spec.kind == "select":
        choices = [
            (str(i), v if isinstance(v, str) else json.dumps(v))
            for i, v in enumerate(spec.choices)
        ]
        if not spec.required:
            choices.insert(0, ("", "—"))
        picked = next(
            (
                str(i)
                for i, v in enumerate(spec.choices)
                if spec.has_default and type(v) is type(initial) and v == initial
            ),
            None,
        )
        return forms.ChoiceField(
            choices=choices,
            initial=picked,
            widget=forms.Select(attrs={"class": INPUT_CLASS}),
            **common,
        )
    if spec.kind in ("number", "integer"):
        attrs = {"class": INPUT_CLASS, "step": "1" if spec.kind == "integer" else "any"}
        if spec.minimum is not None:
            attrs["min"] = spec.minimum
        if spec.maximum is not None:
            attrs["max"] = spec.maximum
        field_class = forms.IntegerField if spec.kind == "integer" else _NumberInputField
        return field_class(initial=initial, widget=forms.NumberInput(attrs=attrs), **common)
    if spec.kind == "text":
        attrs = {"class": INPUT_CLASS}
        if spec.max_length is not None:
            attrs["maxlength"] = spec.max_length
        return forms.CharField(
            initial=initial,
            empty_value=None,
            widget=forms.TextInput(attrs=attrs),
            **common,
        )
    return _JSONInputField(
        initial=json.dumps(initial, indent=2) if spec.has_default else None,
        empty_value=None,
        widget=forms.Textarea(
            attrs={"class": INPUT_CLASS + " font-mono text-xs", "rows": 4, "spellcheck": "false"}
        ),
        **common,
    )


class ScriptInputsForm(forms.Form):
    """Run now's form for a script's declared inputs.

    One field per declared input. Once the form is valid, ``inputs`` holds the
    values validated by ``prepare_inputs`` (defaults filled in), ready to stamp
    on the Run. A blank optional field is left out, so its default applies.
    """

    def __init__(self, script, *args, **kwargs):
        # Prefixed so an input can't collide with csrfmiddlewaretoken & co.
        kwargs.setdefault("prefix", "input")
        super().__init__(*args, **kwargs)
        self.script = script
        self.inputs = None
        self.input_specs = input_fields(script.inputs_schema)
        for spec in self.input_specs:
            self.fields[spec.name] = _input_form_field(spec)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        values = {}
        for spec in self.input_specs:
            value = cleaned.get(spec.name)
            if spec.kind == "select":
                if not value:
                    continue
                value = spec.choices[int(value)]
            elif value is None:
                continue
            values[spec.name] = value
        try:
            self.inputs = prepare_inputs(self.script, values)
        except ScriptInputsError as e:
            if not e.field_errors:
                self.add_error(None, e.message)
            for name, reason in e.field_errors.items():
                if name in self.fields:
                    self.add_error(name, reason)
                else:
                    self.add_error(None, f"{name}: {reason}")
        return cleaned


class TagForm(forms.ModelForm):
    """Form for creating and editing tags."""

    class Meta:
        model = Tag
        fields = ["name", "color"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "Tag name",
                }
            ),
            "color": forms.Select(
                attrs={
                    "class": INPUT_CLASS,
                }
            ),
        }
        labels = {
            "name": "Tag Name",
            "color": "Color",
        }

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Tag name is required.")
        # Check uniqueness (excluding current instance for edits)
        qs = Tag.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A tag with this name already exists.")
        return name


class ScheduleForm(forms.ModelForm):
    """Form for configuring script schedules."""

    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    MONTHDAY_CHOICES = [(i, str(i)) for i in range(1, 32)]
    MONTH_CHOICES = [(i, calendar.month_name[i]) for i in range(1, 13)]

    # Declared explicitly so min/max come from the model's bounds rather than
    # PositiveIntegerField's default min=0; the form field's min_value/max_value
    # also render as the <input> min/max attributes.
    interval_minutes = forms.IntegerField(
        required=False,
        min_value=ScriptSchedule.INTERVAL_MIN_MINUTES,
        max_value=ScriptSchedule.INTERVAL_MAX_MINUTES,
        label="Every N minutes",
        help_text=(
            f"Any whole number from {ScriptSchedule.INTERVAL_MIN_MINUTES} to "
            f"{ScriptSchedule.INTERVAL_MAX_MINUTES} (about 22 days)."
        ),
        widget=forms.NumberInput(
            attrs={"class": INPUT_CLASS, "step": 1, "placeholder": "e.g. 7"}
        ),
    )
    # Quick-pick buttons rendered next to the interval input (template reads
    # this off the form so the views don't need to pass it through).
    interval_presets = ScriptSchedule.INTERVAL_PRESETS

    # Custom field for daily times (comma-separated input)
    daily_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "09:00, 18:00"}
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    # Weekly mode fields
    weekly_days_input = forms.MultipleChoiceField(
        required=False,
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": "sr-only peer",
            }
        ),
        label="Days of Week",
    )

    weekly_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "09:00, 18:00"}
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    # Monthly mode fields
    monthly_days_input = forms.MultipleChoiceField(
        required=False,
        choices=MONTHDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": "sr-only peer",
            }
        ),
        label="Days of Month",
    )

    monthly_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "09:00, 18:00"}
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    # Cron mode field (raw 5-field expression)
    cron_expression = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "0 9 * * 1-5",
            }
        ),
        label="Cron Expression",
        help_text="5 fields: minute hour day-of-month month day-of-week, in the selected timezone.",
    )

    yearly_month = forms.TypedChoiceField(
        required=False, choices=MONTH_CHOICES, coerce=int,
        widget=forms.Select(attrs={"class": INPUT_CLASS}), label="Month",
    )
    yearly_day = forms.TypedChoiceField(
        required=False, choices=MONTHDAY_CHOICES, coerce=int,
        widget=forms.Select(attrs={"class": INPUT_CLASS}), label="Day",
    )
    yearly_time = forms.CharField(
        required=False,
        widget=forms.TimeInput(
            attrs={"class": INPUT_CLASS, "type": "time"}, format="%H:%M"
        ),
        label="Run time", help_text="24-hour format (HH:MM).",
    )

    timezone = forms.ChoiceField(
        choices=get_timezone_choices,
        initial="UTC",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )

    class Meta:
        model = ScriptSchedule
        fields = ["run_mode", "interval_minutes", "cron_expression", "timezone", "is_active"]
        widgets = {
            "run_mode": forms.RadioSelect(
                attrs={
                    "class": "sr-only peer",
                }
            ),
            "is_active": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        }
        labels = {
            "run_mode": "Run Mode",
            "is_active": "Schedule Active",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Populate daily_times_input from instance
        if self.instance and self.instance.pk and self.instance.daily_times:
            self.fields["daily_times_input"].initial = ", ".join(
                self.instance.daily_times
            )

        # Populate weekly fields from instance
        if self.instance and self.instance.pk:
            if self.instance.weekly_days:
                self.fields["weekly_days_input"].initial = [
                    str(d) for d in self.instance.weekly_days
                ]
            if self.instance.weekly_times:
                self.fields["weekly_times_input"].initial = ", ".join(
                    self.instance.weekly_times
                )

        # Populate monthly fields from instance
        if self.instance and self.instance.pk:
            if self.instance.monthly_days:
                self.fields["monthly_days_input"].initial = [
                    str(d) for d in self.instance.monthly_days
                ]
            if self.instance.monthly_times:
                self.fields["monthly_times_input"].initial = ", ".join(
                    self.instance.monthly_times
                )
            self.fields["yearly_month"].initial = self.instance.yearly_month
            self.fields["yearly_day"].initial = self.instance.yearly_day
            self.fields["yearly_time"].initial = self.instance.yearly_time

    def _parse_times(self, value, *, refuse_repeats=True):
        """Parse and validate comma-separated times input: each one HH:MM,
        none listed twice (a repeat is dropped instead when refuse_repeats is
        False)."""
        if not value:
            return []

        value = value.strip()
        if not value:
            return []

        times = []
        for time_str in value.split(","):
            time_str = time_str.strip()
            if not time_str:
                continue

            # Validate HH:MM format
            try:
                parts = time_str.split(":")
                if len(parts) != 2:
                    raise ValueError
                hour, minute = int(parts[0]), int(parts[1])
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except ValueError:
                raise forms.ValidationError(
                    f"Invalid time format: '{time_str}'. Use HH:MM (e.g., 09:00)"
                )

            # Compared after normalizing, so "09:00, 9:00" is a repeat. Each
            # time becomes its own cron, so a repeat would run the script twice
            # at that time. ScheduleAPI.sync refuses repeats the same way.
            normalized = f"{hour:02d}:{minute:02d}"
            if normalized in times:
                if refuse_repeats:
                    raise forms.ValidationError(f"{normalized} is listed twice.")
                continue
            times.append(normalized)

        return times

    def _repeats_refused(self, mode):
        """Refuse a repeated time only in the chosen mode's times field.

        The form keeps every mode's times, but the editor shows only the chosen
        mode's field. A repeat stored in another mode's times before repeats were
        refused would otherwise block every save of the script with an error
        under a hidden field, so those times drop the repeat instead. run_mode is
        a model field, so it is cleaned before the times fields.
        """
        return self.cleaned_data.get("run_mode") == mode

    def clean_daily_times_input(self):
        """Parse and validate daily times input."""
        return self._parse_times(
            self.cleaned_data.get("daily_times_input", ""),
            refuse_repeats=self._repeats_refused(ScriptSchedule.RunMode.DAILY),
        )

    def clean_weekly_times_input(self):
        """Parse and validate weekly times input."""
        return self._parse_times(
            self.cleaned_data.get("weekly_times_input", ""),
            refuse_repeats=self._repeats_refused(ScriptSchedule.RunMode.WEEKLY),
        )

    def clean_monthly_times_input(self):
        """Parse and validate monthly times input."""
        return self._parse_times(
            self.cleaned_data.get("monthly_times_input", ""),
            refuse_repeats=self._repeats_refused(ScriptSchedule.RunMode.MONTHLY),
        )

    def clean_yearly_time(self):
        value = self.cleaned_data.get("yearly_time")
        if not value:
            return ""
        times = self._parse_times(value)
        if len(times) != 1:
            raise forms.ValidationError("Enter one time in HH:MM format.")
        return times[0]

    def clean(self):
        cleaned_data = super().clean()
        run_mode = cleaned_data.get("run_mode")

        if run_mode == ScriptSchedule.RunMode.INTERVAL:
            # Range errors are already attached by the model field's validators;
            # only the missing case needs a message here.
            if "interval_minutes" not in self.errors and not cleaned_data.get(
                "interval_minutes"
            ):
                self.add_error(
                    "interval_minutes", "Interval is required for interval mode."
                )

        # A times field _parse_times refused (a bad or repeated time) already
        # carries that error; "at least one time" is only for an empty field.
        elif run_mode == ScriptSchedule.RunMode.DAILY:
            daily_times = cleaned_data.get("daily_times_input", [])
            if not daily_times and "daily_times_input" not in self.errors:
                self.add_error(
                    "daily_times_input",
                    "At least one time is required for daily mode.",
                )

        elif run_mode == ScriptSchedule.RunMode.WEEKLY:
            weekly_days = cleaned_data.get("weekly_days_input", [])
            weekly_times = cleaned_data.get("weekly_times_input", [])
            if not weekly_days:
                self.add_error(
                    "weekly_days_input",
                    "At least one day is required for weekly mode.",
                )
            if not weekly_times and "weekly_times_input" not in self.errors:
                self.add_error(
                    "weekly_times_input",
                    "At least one time is required for weekly mode.",
                )

        elif run_mode == ScriptSchedule.RunMode.MONTHLY:
            monthly_days = cleaned_data.get("monthly_days_input", [])
            monthly_times = cleaned_data.get("monthly_times_input", [])
            if not monthly_days:
                self.add_error(
                    "monthly_days_input",
                    "At least one day is required for monthly mode.",
                )
            if not monthly_times and "monthly_times_input" not in self.errors:
                self.add_error(
                    "monthly_times_input",
                    "At least one time is required for monthly mode.",
                )

        elif run_mode == ScriptSchedule.RunMode.CRON:
            from core.services.schedule_service import ScheduleService

            expression = (cleaned_data.get("cron_expression") or "").strip()
            is_valid, error = ScheduleService.validate_cron_expression(
                expression, cleaned_data.get("timezone") or "UTC"
            )
            if not is_valid:
                self.add_error("cron_expression", error)
            else:
                cleaned_data["cron_expression"] = expression
        elif run_mode == ScriptSchedule.RunMode.YEARLY:
            month = cleaned_data.get("yearly_month")
            day = cleaned_data.get("yearly_day")
            if not month:
                self.add_error("yearly_month", "Month is required for yearly mode.")
            if not day:
                self.add_error("yearly_day", "Day is required for yearly mode.")
            elif month and day > calendar.monthrange(2024, month)[1]:
                self.add_error("yearly_day", "That day does not exist in the selected month.")
            if not cleaned_data.get("yearly_time"):
                self.add_error("yearly_time", "Time is required for yearly mode.")

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Set daily_times from parsed input
        instance.daily_times = self.cleaned_data.get("daily_times_input", [])

        # Set weekly fields from parsed input
        weekly_days = self.cleaned_data.get("weekly_days_input", [])
        instance.weekly_days = [int(d) for d in weekly_days] if weekly_days else []
        instance.weekly_times = self.cleaned_data.get("weekly_times_input", [])

        # Set monthly fields from parsed input
        monthly_days = self.cleaned_data.get("monthly_days_input", [])
        instance.monthly_days = [int(d) for d in monthly_days] if monthly_days else []
        instance.monthly_times = self.cleaned_data.get("monthly_times_input", [])
        instance.yearly_month = self.cleaned_data.get("yearly_month") or None
        instance.yearly_day = self.cleaned_data.get("yearly_day") or None
        instance.yearly_time = self.cleaned_data.get("yearly_time", "")

        if commit:
            instance.save()

        return instance


class EnvironmentCreateForm(forms.ModelForm):
    """Form for creating a new environment."""

    python_path = forms.ChoiceField(
        choices=[],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Python Version",
        help_text="Select Python installation to use for this environment",
    )

    class Meta:
        model = Environment
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "My environment"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "Environment description (optional)",
                }
            ),
        }
        labels = {
            "name": "Environment Name",
            "description": "Description",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate python_path choices from discovered Python installations
        pythons = EnvironmentService.discover_python_versions()
        choices = [(p["path"], p["display"]) for p in pythons]
        if not choices:
            choices = [("", "No Python installations found")]
        self.fields["python_path"].choices = choices

    def clean_name(self):
        """Validate name and check for path uniqueness."""
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Environment name is required.")

        # Generate path from slugified name
        base_path = slugify(name)
        if not base_path:
            base_path = "environment"

        # Ensure path is unique
        path = base_path
        counter = 1
        while Environment.objects.filter(path=path).exists():
            path = f"{base_path}-{counter}"
            counter += 1

        # Store generated path for use in view
        self._generated_path = path
        return name

    def get_generated_path(self) -> str:
        """Return the generated path after validation."""
        return getattr(self, "_generated_path", slugify(self.cleaned_data.get("name", "env")))


class EnvironmentEditForm(forms.ModelForm):
    """Form for editing environment details (name/description only)."""

    class Meta:
        model = Environment
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "My environment"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "Environment description (optional)",
                }
            ),
        }
        labels = {
            "name": "Environment Name",
            "description": "Description",
        }


class PackageInstallForm(forms.Form):
    """Form for installing a single package."""

    package_spec = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS + " font-mono", "placeholder": "requests==2.31.0"}
        ),
        label="Package",
        help_text="Package name with optional version (e.g., requests, django>=4.0)",
    )

    def clean_package_spec(self):
        spec = self.cleaned_data.get("package_spec", "").strip()
        if not spec:
            raise forms.ValidationError("Package specification is required.")
        if not EnvironmentService.validate_package_spec(spec):
            raise forms.ValidationError(
                "Invalid package specification. Use format: package or package==version"
            )
        return spec


class BulkInstallForm(forms.Form):
    """Form for bulk package installation from requirements."""

    requirements = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 10,
                "placeholder": "requests==2.31.0\ndjango>=4.0\nnumpy",
            }
        ),
        label="Requirements",
        help_text="Paste requirements.txt content (one package per line)",
        required=False,
    )

    requirements_file = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": FILE_CLASS, "accept": ".txt"}),
        label="Or upload requirements.txt",
    )

    def clean(self):
        cleaned_data = super().clean()
        text = cleaned_data.get("requirements", "").strip()
        file = cleaned_data.get("requirements_file")

        if not text and not file:
            raise forms.ValidationError(
                "Provide requirements text or upload a file."
            )

        # If file provided, read its content. Decode by BOM the way pip does:
        # Windows PowerShell 5.1 writes `pip freeze > requirements.txt` as
        # UTF-16, and Notepad can prefix UTF-8 with a BOM.
        if file:
            raw = file.read()
            utf16 = raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
            try:
                cleaned_data["requirements"] = raw.decode("utf-16" if utf16 else "utf-8-sig")
            except UnicodeDecodeError:
                raise forms.ValidationError(
                    "Could not read file. Ensure it's a valid text file."
                )

        error = EnvironmentService.requirements_error(cleaned_data.get("requirements", ""))
        if error:
            raise forms.ValidationError(error)

        return cleaned_data


class SecretCreateForm(forms.Form):
    """Form for creating a new secret.

    Value source (External Secret Providers): ``source="local"`` stores an
    encrypted ``value`` (today's path); ``source="external"`` stores a reference
    (``provider`` + ``external_ref``) resolved live at run time. The value/provider
    fields are conditionally required in ``clean()`` on the chosen source.
    """

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the key-uniqueness check (tenancy Stage 3:
        # secret keys are unique per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)
        # Set the provider queryset here (not at class scope) to avoid a DB hit at
        # import time, and to keep the dropdown fresh per request.
        self.fields["provider"].queryset = SecretProvider.objects.all().order_by("name")

    key = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono uppercase",
                "placeholder": "API_KEY",
                "autocomplete": "off",
            }
        ),
        label="Key Name",
        help_text="Uppercase letters, numbers, and underscores only. Must start with a letter.",
    )

    # required=False so a POST that omits it means today's local secret — keeps
    # every existing caller (and existing test) byte-for-byte; clean() normalizes.
    source = forms.ChoiceField(
        choices=Secret.Source.choices,
        initial=Secret.Source.LOCAL,
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Value source",
    )

    value = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 3,
                "placeholder": "sk-your-secret-value-here",
                "autocomplete": "off",
                # Masked until "Show" (static/js/secret_value_toggle.js).
                "data-masked": "1",
                "style": "-webkit-text-security: disc",
                "spellcheck": "false",
            }
        ),
        label="Secret Value",
        help_text="The secret value (will be encrypted at rest)",
    )

    provider = forms.ModelChoiceField(
        queryset=SecretProvider.objects.none(),
        required=False,
        empty_label="— select a provider —",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    external_ref = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "path/to/secret#key",
                "autocomplete": "off",
            }
        ),
        label="Reference",
        help_text="Reference to the value within the provider (e.g. a Vault path#key).",
    )

    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS,
                "rows": 2,
                "placeholder": "What is this secret used for?",
            }
        ),
        label="Description",
        help_text="Optional description to help remember what this secret is for",
    )

    def clean_external_ref(self):
        return (self.cleaned_data.get("external_ref") or "").strip()

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("source") or Secret.Source.LOCAL
        cleaned["source"] = source  # normalize blank → local for the view
        if source == Secret.Source.EXTERNAL:
            if not cleaned.get("provider"):
                self.add_error("provider", "Select a provider for an external secret.")
            if not cleaned.get("external_ref"):
                self.add_error(
                    "external_ref", "A reference is required for an external secret."
                )
        else:  # local
            if not cleaned.get("value"):
                self.add_error("value", "Secret value is required.")
        return cleaned

    def clean_key(self):
        """Validate and normalize the key."""
        key = self.cleaned_data.get("key", "").strip().upper()

        if not key:
            raise forms.ValidationError("Key name is required.")

        if not SECRET_KEY_PATTERN.match(key):
            raise forms.ValidationError(
                "Key must start with a letter and contain only uppercase letters, numbers, and underscores."
            )

        # Check for reserved environment variable names (shared with env vars)
        if key in RESERVED_NAMES:
            raise forms.ValidationError(
                f"'{key}' is a reserved environment variable name."
            )

        # Check if key already exists within the active workspace (keys are
        # per-workspace, so two workspaces can each own an API_KEY).
        from core.models import Secret

        qs = Secret.objects.filter(key=key)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if qs.exists():
            raise forms.ValidationError(
                f"A secret with key '{key}' already exists in this workspace."
            )

        return key

    def clean_value(self):
        """Length-check only; the required-ness of the value depends on the chosen
        source and is enforced in ``clean()`` (local needs a value; external does not)."""
        value = self.cleaned_data.get("value", "")

        # Reasonable max length for secrets
        if len(value) > 10000:
            raise forms.ValidationError(
                "Secret value is too long (max 10,000 characters)."
            )

        return value


class SecretEditForm(forms.Form):
    """Form for editing an existing secret (value/source and description).

    Supports switching a secret's value source (External Secret Providers). A
    local edit may leave ``value`` blank to keep the stored value; switching to
    (or staying) external requires ``provider`` + ``external_ref``.
    """

    def __init__(self, *args, instance=None, **kwargs):
        self._instance = instance
        super().__init__(*args, **kwargs)
        self.fields["provider"].queryset = SecretProvider.objects.all().order_by("name")
        if instance is not None and not self.is_bound:
            self.fields["source"].initial = instance.source
            self.fields["provider"].initial = instance.provider_id
            self.fields["external_ref"].initial = instance.external_ref
            self.fields["description"].initial = instance.description

    # required=False so a POST that omits it means today's local secret — keeps
    # every existing caller (and existing test) byte-for-byte; clean() normalizes.
    source = forms.ChoiceField(
        choices=Secret.Source.choices,
        initial=Secret.Source.LOCAL,
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Value source",
    )

    value = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 3,
                "placeholder": "Leave blank to keep current value",
                "autocomplete": "off",
                # Masked until "Show" (static/js/secret_value_toggle.js).
                "data-masked": "1",
                "style": "-webkit-text-security: disc",
                "spellcheck": "false",
            }
        ),
        label="New Secret Value",
        help_text="Leave blank to keep the current value",
    )

    provider = forms.ModelChoiceField(
        queryset=SecretProvider.objects.none(),
        required=False,
        empty_label="— select a provider —",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    external_ref = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "path/to/secret#key",
                "autocomplete": "off",
            }
        ),
        label="Reference",
        help_text="Reference to the value within the provider (e.g. a Vault path#key).",
    )

    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS,
                "rows": 2,
                "placeholder": "What is this secret used for?",
            }
        ),
        label="Description",
    )

    def clean_external_ref(self):
        return (self.cleaned_data.get("external_ref") or "").strip()

    def clean_value(self):
        """Validate the secret value if provided."""
        value = self.cleaned_data.get("value", "")

        if value and len(value) > 10000:
            raise forms.ValidationError(
                "Secret value is too long (max 10,000 characters)."
            )

        return value

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("source") or Secret.Source.LOCAL
        cleaned["source"] = source  # normalize blank → local for the view
        if source == Secret.Source.EXTERNAL:
            if not cleaned.get("provider"):
                self.add_error("provider", "Select a provider for an external secret.")
            if not cleaned.get("external_ref"):
                self.add_error(
                    "external_ref", "A reference is required for an external secret."
                )
        else:  # local
            # A value may be omitted only when the row already holds a stored local
            # value to keep; switching from external → local must supply one.
            has_stored_local = bool(
                self._instance
                and self._instance.source == Secret.Source.LOCAL
                and self._instance.encrypted_value
            )
            if not cleaned.get("value") and not has_stored_local:
                self.add_error("value", "Secret value is required.")
        return cleaned


class NotificationSettingsForm(forms.Form):
    """Form for global notification settings."""

    from core.models import GlobalSettings

    EMAIL_BACKEND_CHOICES = [
        (GlobalSettings.EmailBackend.DISABLED, "Disabled"),
        (GlobalSettings.EmailBackend.SMTP, "SMTP"),
        (GlobalSettings.EmailBackend.RESEND, "Resend API"),
    ]

    email_backend = forms.ChoiceField(
        choices=EMAIL_BACKEND_CHOICES,
        initial=GlobalSettings.EmailBackend.DISABLED,
        widget=forms.RadioSelect(
            attrs={
                "class": "sr-only peer",
            }
        ),
        label="Email Backend",
    )

    # SMTP Configuration
    smtp_host = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "smtp.example.com",
            }
        ),
        label="SMTP Host",
    )

    smtp_port = forms.IntegerField(
        required=False,
        initial=587,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 1,
                "max": 65535,
            }
        ),
        label="SMTP Port",
    )

    smtp_username = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "username@example.com",
            }
        ),
        label="SMTP Username",
    )

    smtp_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="SMTP Password",
    )

    smtp_use_tls = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Use TLS",
    )

    smtp_from_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "noreply@example.com",
            }
        ),
        label="From Email",
    )

    # Resend Configuration
    resend_api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Resend API Key",
    )

    resend_from_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "noreply@yourdomain.com",
            }
        ),
        label="From Email",
    )

    # Default notification email
    default_notification_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "notifications@example.com",
            }
        ),
        label="Default Notification Email",
        help_text="All script notifications will be sent here unless overridden per-script.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["email_backend"].initial = instance.email_backend
            self.fields["smtp_host"].initial = instance.smtp_host
            self.fields["smtp_port"].initial = instance.smtp_port
            self.fields["smtp_username"].initial = instance.smtp_username
            self.fields["smtp_use_tls"].initial = instance.smtp_use_tls
            self.fields["smtp_from_email"].initial = instance.smtp_from_email
            self.fields["resend_from_email"].initial = instance.resend_from_email
            self.fields["default_notification_email"].initial = instance.default_notification_email

    def clean(self):
        """Validate configuration based on selected backend."""
        cleaned_data = super().clean()
        backend = cleaned_data.get("email_backend")

        from core.models import GlobalSettings

        if backend == GlobalSettings.EmailBackend.SMTP:
            if not cleaned_data.get("smtp_host"):
                self.add_error("smtp_host", "SMTP host is required for SMTP backend.")
            if not cleaned_data.get("smtp_from_email"):
                self.add_error("smtp_from_email", "From email is required for SMTP backend.")

        elif backend == GlobalSettings.EmailBackend.RESEND:
            if not cleaned_data.get("resend_from_email"):
                self.add_error("resend_from_email", "From email is required for Resend backend.")

        return cleaned_data

    def save(self, instance):
        """Save the notification settings to the GlobalSettings instance."""
        from core.services import EncryptionService

        instance.email_backend = self.cleaned_data["email_backend"]
        instance.smtp_host = self.cleaned_data.get("smtp_host") or ""
        instance.smtp_port = self.cleaned_data.get("smtp_port") or 587
        instance.smtp_username = self.cleaned_data.get("smtp_username") or ""
        instance.smtp_use_tls = self.cleaned_data.get("smtp_use_tls", True)
        instance.smtp_from_email = self.cleaned_data.get("smtp_from_email") or ""
        instance.resend_from_email = self.cleaned_data.get("resend_from_email") or ""
        instance.default_notification_email = self.cleaned_data.get("default_notification_email") or ""

        # Encrypt and save SMTP password if provided
        smtp_password = self.cleaned_data.get("smtp_password")
        if smtp_password:
            instance.smtp_password_encrypted = EncryptionService.encrypt(smtp_password)

        # Encrypt and save Resend API key if provided
        resend_api_key = self.cleaned_data.get("resend_api_key")
        if resend_api_key:
            instance.resend_api_key_encrypted = EncryptionService.encrypt(resend_api_key)

        instance.save()
        return instance



def _reserved_top_level_paths():
    """First path segments of every root route except the admin itself."""
    from importlib import import_module

    from django.conf import settings
    from django.urls.resolvers import URLResolver

    # Plugin mounts exist only while a plugin is loaded, so reserve their root too.
    reserved = {"static", "media", "plugins"}
    for entry in import_module(settings.ROOT_URLCONF).urlpatterns:
        if isinstance(entry, URLResolver) and entry.app_name == "admin":
            continue
        segment = str(entry.pattern).lstrip("^").split("/", 1)[0]
        if segment and "<" not in segment:
            reserved.add(segment)
    return reserved

class GeneralSettingsForm(forms.Form):
    """Form for general instance settings."""

    instance_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "PyRun",
            }
        ),
        label="Instance Name",
        help_text="Displayed in the header and email notifications",
    )

    timezone = forms.ChoiceField(
        choices=get_timezone_choices,
        initial="UTC",
        widget=forms.Select(
            attrs={
                "class": INPUT_CLASS,
            }
        ),
        label="Timezone",
        help_text="Times in the console and scheduled backup times use this timezone",
    )

    admin_url_slug = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "django-admin",
            }
        ),
        label="Django Admin URL",
        help_text="URL path for Django admin (e.g., 'django-admin' → /django-admin/). Requires app restart.",
    )

    def clean_admin_url_slug(self):
        """Validate admin URL slug format."""
        import re
        slug = self.cleaned_data.get("admin_url_slug", "django-admin").strip().lower()
        if not slug:
            slug = "django-admin"
        # Remove leading/trailing slashes
        slug = slug.strip("/")
        # Validate: alphanumeric, hyphens, underscores only
        if not re.match(r"^[a-z0-9_-]+$", slug):
            raise forms.ValidationError(
                "Admin URL can only contain lowercase letters, numbers, hyphens, and underscores."
            )
        # The admin is mounted first (pyrunner/urls.py), so a slug equal to any
        # other top-level route would shadow it: "api" would hide /api/v1/.
        if slug in _reserved_top_level_paths():
            raise forms.ValidationError(
                f"'{slug}' is a reserved URL path. Please choose a different name."
            )
        return slug

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["instance_name"].initial = instance.instance_name
            self.fields["timezone"].initial = instance.timezone
            self.fields["admin_url_slug"].initial = instance.admin_url_slug

    def save(self, instance):
        """Save the general settings to the GlobalSettings instance."""
        instance.instance_name = self.cleaned_data.get("instance_name") or "PyRun"
        instance.timezone = self.cleaned_data.get("timezone") or "UTC"
        instance.admin_url_slug = self.cleaned_data.get("admin_url_slug") or "django-admin"
        instance.save(update_fields=[
            "instance_name", "timezone", "admin_url_slug", "updated_at"
        ])
        return instance


class LogRetentionForm(forms.Form):
    """Form for log retention settings."""

    retention_days = forms.IntegerField(
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 0,
            }
        ),
        label="Retention Days",
        help_text="Runs newer than this many days are always kept (0 = no age rule)",
    )

    retention_count = forms.IntegerField(
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 0,
            }
        ),
        label="Retention Count",
        help_text="Each script's last N runs are always kept (0 = no count rule)",
    )

    auto_cleanup_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Auto Cleanup",
        help_text="Automatically clean up old runs daily at 2 AM",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["retention_days"].initial = instance.retention_days
            self.fields["retention_count"].initial = instance.retention_count
            self.fields["auto_cleanup_enabled"].initial = instance.auto_cleanup_enabled

    def save(self, instance):
        """Save the retention settings to the GlobalSettings instance."""
        from core.services import RetentionService

        instance.retention_days = self.cleaned_data.get("retention_days") or 0
        instance.retention_count = self.cleaned_data.get("retention_count") or 0

        # Handle auto cleanup schedule
        new_auto_cleanup = self.cleaned_data.get("auto_cleanup_enabled", False)
        old_auto_cleanup = instance.auto_cleanup_enabled

        instance.auto_cleanup_enabled = new_auto_cleanup
        instance.save(update_fields=[
            "retention_days", "retention_count", "auto_cleanup_enabled", "updated_at"
        ])

        # Manage the django-q2 schedule
        if new_auto_cleanup and not old_auto_cleanup:
            RetentionService.enable_auto_cleanup()
        elif not new_auto_cleanup and old_auto_cleanup:
            RetentionService.disable_auto_cleanup()

        return instance


class WorkerSettingsForm(forms.Form):
    """Form for Django-Q2 worker configuration."""

    q_workers = forms.IntegerField(
        min_value=1,
        max_value=16,
        initial=2,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 1,
                "max": 16,
            }
        ),
        label="Worker Count",
        help_text="Number of worker processes (1-16). More workers can process more tasks simultaneously.",
    )

    q_timeout = forms.IntegerField(
        min_value=0,
        max_value=86400,
        initial=600,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 0,
                "max": 86400,
            }
        ),
        label="Task Timeout (seconds)",
        help_text="Maximum time a task can run before worker timeout. Use 0 for no timeout (required on Windows). For long-running scripts, also increase the script's own timeout.",
    )

    q_retry = forms.IntegerField(
        min_value=60,
        max_value=86400,
        initial=660,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 60,
                "max": 86400,
            }
        ),
        label="Retry Delay (seconds)",
        help_text="Time before a task is re-delivered to another worker. Should be greater than timeout. At worker start the effective value is automatically raised above the largest script timeout so long runs are never re-delivered mid-run.",
    )

    q_queue_limit = forms.IntegerField(
        min_value=5,
        max_value=100,
        initial=20,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 5,
                "max": 100,
            }
        ),
        label="Queue Limit",
        help_text="Maximum number of tasks that can be queued at once.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["q_workers"].initial = instance.q_workers
            self.fields["q_timeout"].initial = instance.q_timeout
            self.fields["q_retry"].initial = instance.q_retry
            self.fields["q_queue_limit"].initial = instance.q_queue_limit

    def clean(self):
        """Validate that retry > timeout."""
        cleaned_data = super().clean()
        timeout = cleaned_data.get("q_timeout", 0)
        retry = cleaned_data.get("q_retry", 660)

        if timeout > 0 and retry <= timeout:
            self.add_error(
                "q_retry",
                f"Retry delay ({retry}s) must be greater than timeout ({timeout}s).",
            )

        return cleaned_data

    def save(self, instance):
        """Save the worker settings to the GlobalSettings instance."""
        from django.utils import timezone

        # Check if any values actually changed
        changed = (
            instance.q_workers != self.cleaned_data["q_workers"]
            or instance.q_timeout != self.cleaned_data["q_timeout"]
            or instance.q_retry != self.cleaned_data["q_retry"]
            or instance.q_queue_limit != self.cleaned_data["q_queue_limit"]
        )

        instance.q_workers = self.cleaned_data["q_workers"]
        instance.q_timeout = self.cleaned_data["q_timeout"]
        instance.q_retry = self.cleaned_data["q_retry"]
        instance.q_queue_limit = self.cleaned_data["q_queue_limit"]

        if changed:
            instance.worker_settings_updated_at = timezone.now()

        instance.save(
            update_fields=[
                "q_workers",
                "q_timeout",
                "q_retry",
                "q_queue_limit",
                "worker_settings_updated_at",
                "updated_at",
            ]
        )
        return instance


class ExecutionIsolationForm(forms.Form):
    """Form for the script-execution sandbox (FOUNDATIONS Seam 2).

    Dashboard-managed, stored on ``GlobalSettings`` and resolved per-run at
    execution time (no restart). Mirrors ``WorkerSettingsForm``. The resource
    limits are active immediately; the isolation *mode* selects the filesystem/
    network sandbox (``SandboxedSubprocessBackend``), resolved per run against the
    workspace/script policy.
    """

    from core.models import GlobalSettings

    SANDBOX_MODE_CHOICES = GlobalSettings.SandboxMode.choices

    sandbox_default = forms.ChoiceField(
        choices=SANDBOX_MODE_CHOICES,
        initial=GlobalSettings.SandboxMode.OFF,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Isolation default",
        help_text="Instance-wide default. Gates the filesystem/network sandbox; "
        "the resource limits below apply regardless.",
    )

    sandbox_fail_closed = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Fail closed",
        help_text="When a required sandbox is unavailable on the host, fail the "
        "run instead of degrading to a lower tier with a warning.",
    )

    sandbox_rlimit_memory_mb = forms.IntegerField(
        min_value=0,
        max_value=1048576,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Memory limit (MB)",
        help_text="Per-run address-space cap (RLIMIT_AS). 0 = unlimited. POSIX only.",
    )

    sandbox_rlimit_cpu_seconds = forms.IntegerField(
        min_value=0,
        max_value=86400,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="CPU time limit (seconds)",
        help_text="Per-run CPU-time cap (RLIMIT_CPU). 0 = unlimited. POSIX only.",
    )

    sandbox_rlimit_nproc = forms.IntegerField(
        min_value=0,
        max_value=100000,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Process limit",
        help_text="Per-run process/thread cap (RLIMIT_NPROC, fork-bomb guard). "
        "0 = unlimited. POSIX only, and only for scripts that don't run as root.",
    )

    sandbox_rlimit_fsize_mb = forms.IntegerField(
        min_value=0,
        max_value=1048576,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Max file size (MB)",
        help_text="Per-run largest single-file write (RLIMIT_FSIZE). 0 = unlimited. POSIX only.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["sandbox_default"].initial = instance.sandbox_default
            self.fields["sandbox_fail_closed"].initial = instance.sandbox_fail_closed
            self.fields["sandbox_rlimit_memory_mb"].initial = instance.sandbox_rlimit_memory_mb
            self.fields["sandbox_rlimit_cpu_seconds"].initial = instance.sandbox_rlimit_cpu_seconds
            self.fields["sandbox_rlimit_nproc"].initial = instance.sandbox_rlimit_nproc
            self.fields["sandbox_rlimit_fsize_mb"].initial = instance.sandbox_rlimit_fsize_mb

    def save(self, instance):
        """Save the isolation settings to the GlobalSettings instance."""
        instance.sandbox_default = self.cleaned_data["sandbox_default"]
        instance.sandbox_fail_closed = self.cleaned_data.get("sandbox_fail_closed", False)
        instance.sandbox_rlimit_memory_mb = self.cleaned_data.get("sandbox_rlimit_memory_mb") or 0
        instance.sandbox_rlimit_cpu_seconds = self.cleaned_data.get("sandbox_rlimit_cpu_seconds") or 0
        instance.sandbox_rlimit_nproc = self.cleaned_data.get("sandbox_rlimit_nproc") or 0
        instance.sandbox_rlimit_fsize_mb = self.cleaned_data.get("sandbox_rlimit_fsize_mb") or 0
        instance.save(
            update_fields=[
                "sandbox_default",
                "sandbox_fail_closed",
                "sandbox_rlimit_memory_mb",
                "sandbox_rlimit_cpu_seconds",
                "sandbox_rlimit_nproc",
                "sandbox_rlimit_fsize_mb",
                "updated_at",
            ]
        )
        return instance


class BackupCreateForm(forms.Form):
    """Form for configuring backup creation."""

    backup_format = forms.ChoiceField(
        choices=[
            ("gzip", "Compressed (recommended) - .json.gz"),
            ("json", "Plain JSON - .json"),
        ],
        initial="gzip",
        widget=forms.RadioSelect(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Backup format",
        help_text="Compressed backups are 80-95% smaller",
    )

    include_datastores = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include DataStores",
        help_text="Include all DataStores and their key-value entries",
    )

    include_runs = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include run history",
        help_text="Include execution history (stdout/stderr)",
    )

    max_runs = forms.IntegerField(
        initial=1000,
        min_value=0,
        max_value=10000,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "1000",
            }
        ),
        label="Maximum runs to include",
        help_text="Limit run history to most recent N runs (0 = all runs)",
    )

    include_package_operations = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include package operations",
        help_text="Include pip installation history",
    )

    include_plugins = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include installed plugins",
        help_text="With their files. A restore installs the ones that server doesn't have, turned off.",
    )


class BackupRestoreForm(forms.Form):
    """Form for restoring from backup."""

    backup_file = forms.FileField(
        widget=forms.FileInput(
            attrs={
                "class": FILE_CLASS,
                "accept": ".json,.json.gz,.gz",
            }
        ),
        label="Backup file",
        help_text="JSON or compressed backup file (.json or .json.gz)",
    )

    restore_runs = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Restore run history",
        help_text="Import execution history from backup",
    )

    confirm_delete = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-4 h-4 rounded text-fail bg-ink border-line focus:ring-fail/40 focus:ring-2",
            }
        ),
        label="I understand all existing data will be deleted",
        help_text="This action cannot be undone without the automatic backup",
    )


# =============================================================================
# Data Store Forms
# =============================================================================


class DataStoreForm(forms.ModelForm):
    """Form for creating and editing data stores."""

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the name-uniqueness check (tenancy: names
        # are unique per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)

    class Meta:
        model = DataStore
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "my_data_store",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What is this data store used for?",
                }
            ),
        }
        labels = {
            "name": "Store Name",
            "description": "Description",
        }
        help_texts = {
            "name": "Used in scripts as: DataStore(\"name\")",
        }

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Store name is required.")
        # Check for valid identifier-like name
        if not name.replace("_", "").replace("-", "").isalnum():
            raise forms.ValidationError(
                "Name can only contain letters, numbers, underscores, and hyphens."
            )
        # Check uniqueness within the active workspace (names are per-workspace).
        qs = DataStore.objects.filter(name__iexact=name)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A data store with this name already exists in this workspace."
            )
        return name


class DatabaseForm(forms.ModelForm):
    """Form for creating and editing managed databases."""

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the name-uniqueness check (tenancy: names
        # are unique per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)

    class Meta:
        model = Database
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "my_database",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What is this database used for?",
                }
            ),
        }
        labels = {
            "name": "Database Name",
            "description": "Description",
        }
        help_texts = {
            "name": 'Used in scripts as: pyrunner_db.connect("name")',
        }

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Database name is required.")
        # Identifier-like names only: the Postgres schema/role name is derived
        # from this (lowercased, hyphens folded to underscores).
        if not name.replace("_", "").replace("-", "").isalnum():
            raise forms.ValidationError(
                "Name can only contain letters, numbers, underscores, and hyphens."
            )
        # Check uniqueness within the active workspace (names are per-workspace).
        qs = Database.objects.filter(name__iexact=name)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A database with this name already exists in this workspace."
            )
        return name


class DataStoreEntryForm(forms.Form):
    """Form for creating and editing data store entries."""

    key = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "my_key",
            }
        ),
        label="Key",
        help_text="Unique identifier for this entry",
    )

    value = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 6,
                "placeholder": '{"example": "value"}\nor just a string\nor a number like 42',
            }
        ),
        label="Value (JSON)",
        help_text="JSON value: string, number, boolean, array, or object",
    )

    def __init__(self, *args, datastore=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.datastore = datastore
        self.instance = instance

        # Pre-populate for editing
        if instance:
            self.fields["key"].initial = instance.key
            self.fields["value"].initial = instance.value_json

    def clean_key(self):
        key = self.cleaned_data.get("key", "").strip()
        if not key:
            raise forms.ValidationError("Key is required.")
        if len(key) > 255:
            raise forms.ValidationError("Key cannot exceed 255 characters.")

        # Check uniqueness within the data store
        if self.datastore:
            qs = DataStoreEntry.objects.filter(datastore=self.datastore, key=key)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f"Key '{key}' already exists in this data store."
                )
        return key

    def clean_value(self):
        import json

        value = self.cleaned_data.get("value", "").strip()
        if not value:
            raise forms.ValidationError("Value is required.")

        try:
            # Validate it's valid JSON
            json.loads(value)
        except json.JSONDecodeError as e:
            raise forms.ValidationError(f"Invalid JSON: {e}")

        return value


# =============================================================================
# API Token Forms
# =============================================================================


class PermissionCheckboxes(forms.CheckboxSelectMultiple):
    """Permission checkboxes; the values in ``disabled_values`` can't be ticked."""

    disabled_values = frozenset()

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        if value in self.disabled_values:
            option["attrs"]["disabled"] = True
        return option


class APITokenForm(forms.ModelForm):
    """Form for creating API tokens (datastore, all-datastores, or plugin scope)."""

    # Plugin choices are the plugins loaded RIGHT NOW (validated at creation
    # time only — plugin_slug is not an FK; see the model).
    plugin_slug = forms.ChoiceField(
        required=False,
        label="Plugin",
        help_text="The plugin whose API this token can call",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    permissions = forms.MultipleChoiceField(
        choices=APIToken.API_PERMISSIONS, required=False, widget=PermissionCheckboxes,
    )
    kind = forms.ChoiceField(choices=APIToken.Kind.choices, required=False,
                             widget=forms.Select(attrs={"class": INPUT_CLASS}))

    class Meta:
        model = APIToken
        fields = ["name", "scope", "datastore", "plugin_slug", "expires_at",
                  "permissions", "kind", "brain", "brain_tier", "script", "table"]
        widgets = {
            "brain": forms.Select(attrs={"class": INPUT_CLASS}),
            "script": forms.Select(attrs={"class": INPUT_CLASS}),
            "table": forms.Select(attrs={"class": INPUT_CLASS}),
            "brain_tier": forms.Select(attrs={"class": INPUT_CLASS}),
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "My Dashboard Token",
                }
            ),
            "scope": forms.Select(
                attrs={
                    "class": INPUT_CLASS,
                }
            ),
            "datastore": forms.Select(
                attrs={
                    "class": INPUT_CLASS,
                }
            ),
            "expires_at": forms.DateTimeInput(
                attrs={
                    "class": INPUT_CLASS,
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
        }
        labels = {
            "name": "Token Name",
            "scope": "Scope",
            "datastore": "Datastore",
            "expires_at": "Expires At",
        }
        help_texts = {
            "name": "A friendly name to identify this token",
            "scope": "What this token grants access to",
            "datastore": (
                "Required for single-datastore scope. Optional for a unified API "
                "token: it then reaches this datastore only."
            ),
            "expires_at": "Optional. Leave empty for no expiration.",
        }

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import Brain

        self.instance.workspace = workspace
        self.fields["brain"].queryset = Brain.objects.for_workspace(workspace)
        # Binding to one script (2.5): the active workspace's live scripts.
        self.fields["script"].queryset = Script.objects.for_workspace(workspace).filter(
            archived_at__isnull=True
        ).order_by("name")
        self.fields["script"].empty_label = "Any script in this workspace"
        # Binding to one table (2.5): the active workspace's tables.
        from core.models import Table

        self.fields["table"].queryset = Table.objects.for_workspace(workspace).order_by("name")
        self.fields["table"].empty_label = "Any table in this workspace"
        self.fields["brain_tier"].required = False
        # Make datastore optional with a clear empty choice
        self.fields["datastore"].required = False
        self.fields["datastore"].empty_label = "Select a datastore…"
        self.fields["expires_at"].required = False
        # Scope the datastore choices to the active workspace (tenancy Stage 3),
        # so a token can't be bound to another workspace's datastore.
        if workspace is not None:
            self.fields["datastore"].queryset = DataStore.objects.for_workspace(
                workspace
            )
        # Offer only currently-loaded plugins for plugin scope. Lazy import:
        # forms.py must stay importable without the plugin registry populated.
        from core.plugins import all_plugins

        self.fields["plugin_slug"].choices = [("", "Select a plugin…")] + [
            (p.slug, f"{p.name} ({p.slug})") for p in sorted(all_plugins(), key=lambda p: p.slug)
        ]
        # Plan D8: write permissions show, disabled with the reason, while API
        # writes are off; the model refuses them too (a crafted POST).
        from core.api.registry import registry, writes_enabled

        self.fields["permissions"].choices = APIToken.API_PERMISSIONS
        write_perms = {p for p, _ in APIToken.API_PERMISSIONS} & registry.write_permissions()
        self.writes_off_reason = ""
        if write_perms and not writes_enabled():
            self.fields["permissions"].widget.disabled_values = frozenset(write_perms)
            self.writes_off_reason = APIToken.WRITES_OFF_GRANT

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get("scope")
        if scope == APIToken.Scope.DATASTORE and not cleaned.get("datastore"):
            self.add_error("datastore", "Select the datastore this token is scoped to.")
        if scope == APIToken.Scope.PLUGIN and not cleaned.get("plugin_slug"):
            self.add_error("plugin_slug", "Select the plugin this token is scoped to.")
        # Blank out the fields the chosen scope doesn't use (matches the model's
        # CheckConstraints — a stray selection must not survive a scope switch).
        # A unified API token may keep a datastore as its binding (2.5).
        if scope not in (APIToken.Scope.DATASTORE, APIToken.Scope.API):
            cleaned["datastore"] = None
        if scope != APIToken.Scope.PLUGIN:
            cleaned["plugin_slug"] = ""
        cleaned["kind"] = cleaned.get("kind") or "bearer"
        cleaned["brain_tier"] = cleaned.get("brain_tier") or "public"
        if scope != APIToken.Scope.API:
            cleaned.update(permissions=[], kind="bearer", brain=None, brain_tier="public",
                           script=None, table=None)
        elif cleaned["kind"] == "url":
            from core.models import GlobalSettings

            # URL transport is enabled explicitly by the operator with MCP.
            if not getattr(GlobalSettings.get_settings(), "mcp_url_tokens_enabled", False):
                self.add_error("kind", "MCP URL tokens are disabled in Connect settings.")
            if not cleaned.get("expires_at"):
                from datetime import timedelta
                from django.utils import timezone

                cleaned["expires_at"] = timezone.now() + timedelta(days=90)
        return cleaned


# Historical name — kept importable alongside the model alias.
DataStoreAPITokenForm = APITokenForm


# =============================================================================
# Authentication Forms
# =============================================================================



class PasswordLoginForm(forms.Form):
    """Form for password-based login."""

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "you@example.com",
                "autocomplete": "email",
            }
        ),
        label="Email address",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Your password",
                "autocomplete": "current-password",
            }
        ),
        label="Password",
    )


class SetPasswordForm(forms.Form):
    """Form for setting or changing password.

    Enforces the AUTH_PASSWORD_VALIDATORS policy — the settings-declared
    validators only apply through an explicit validate_password() call, which
    lives here. Pass ``user`` so the similarity validator can compare the
    password against the account's email.
    """

    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "New password",
                "autocomplete": "new-password",
            }
        ),
        label="New Password",
        help_text="At least 8 characters, not entirely numeric, and not a common password",
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Confirm new password",
                "autocomplete": "new-password",
            }
        ),
        label="Confirm Password",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        from django.contrib.auth import password_validation

        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise forms.ValidationError("Passwords do not match.")
        if password:
            try:
                password_validation.validate_password(password, user=self.user)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned_data


class ChangePasswordForm(SetPasswordForm):
    """Change a signed-in user's password, confirming the current one if set.

    Without it, anyone holding a signed-in session (an unlocked laptop, a
    copied cookie) could take the account over by setting a new password. An
    account with no usable password yet (e.g. from an invite) just sets one.
    """

    current_password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Current password",
                "autocomplete": "current-password",
            }
        ),
        label="Current Password",
    )
    field_order = ["current_password", "password", "password_confirm"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        if user is None or not user.has_usable_password():
            del self.fields["current_password"]

    def clean_current_password(self):
        value = self.cleaned_data.get("current_password", "")
        if not self.user.check_password(value):
            raise forms.ValidationError("Your current password is incorrect.")
        return value


class AdminSetupForm(forms.Form):
    """Form for initial admin setup with password.

    Enforces AUTH_PASSWORD_VALIDATORS (see SetPasswordForm) — the instance's
    first superuser deserves at least the standard policy.
    """

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "admin@example.com",
                "autocomplete": "email",
            }
        ),
        label="Admin Email",
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Create a strong password",
                "autocomplete": "new-password",
            }
        ),
        label="Password",
        help_text="At least 8 characters, not entirely numeric, and not a common password",
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Confirm password",
                "autocomplete": "new-password",
            }
        ),
        label="Confirm Password",
    )

    def clean(self):
        from django.contrib.auth import password_validation

        from core.models import User

        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise forms.ValidationError("Passwords do not match.")
        if password:
            # Transient (unsaved) user so the similarity validator can compare
            # against the email being registered.
            probe = User(email=cleaned_data.get("email") or "")
            try:
                password_validation.validate_password(password, user=probe)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned_data


# =============================================================================
# Services Forms
# =============================================================================


class StorageConnectionForm(forms.Form):
    """Create/edit one StorageConnection (credentials stored encrypted).

    Twin of ``AIProviderForm``: many credentialed rows, one selected per role
    (``is_default`` for backups, ``GlobalSettings.assets_storage`` for plugins).
    """

    from core.models import StorageConnection as _SC

    provider_type = forms.ChoiceField(
        choices=_SC.ProviderType.choices,
        initial=_SC.ProviderType.R2,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "e.g. Backups (R2)"}
        ),
        label="Name",
    )

    endpoint_url = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://…"}
        ),
        label="Endpoint URL",
        help_text="Leave empty for AWS S3. Required for R2, MinIO, Spaces, B2, etc.",
    )

    region = forms.CharField(
        required=False,
        max_length=50,
        initial="us-east-1",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "us-east-1"}),
        label="Region",
    )

    bucket = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "my-bucket"}
        ),
        label="Bucket",
    )

    access_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Paste the access key ID",
                "autocomplete": "new-password",
            }
        ),
        label="Access Key",
    )

    secret_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Paste the secret access key",
                "autocomplete": "new-password",
            }
        ),
        label="Secret Key",
    )

    public_base_url = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://assets.example.com"}
        ),
        label="Public base URL",
        help_text="Set this to serve objects at permanent, hot-linkable URLs. Blank = "
        "objects are served via presigned URLs, which EXPIRE (default 1 hour).",
    )

    use_ssl = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Use SSL/TLS",
        help_text="Recommended for security. Disable only for local development.",
    )

    path_style = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Path-style addressing",
        help_text="Required for MinIO and some S3-compatible providers.",
    )

    is_default = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Use for backups",
        help_text="Scheduled and manual backups upload through this connection.",
    )

    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enabled",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["endpoint_url"].initial = instance.endpoint_url
            self.fields["region"].initial = instance.region or "us-east-1"
            self.fields["bucket"].initial = instance.bucket
            self.fields["public_base_url"].initial = instance.public_base_url
            self.fields["use_ssl"].initial = instance.use_ssl
            self.fields["path_style"].initial = instance.path_style
            self.fields["is_default"].initial = instance.is_default
            self.fields["enabled"].initial = instance.enabled

    def clean_name(self):
        from core.models import StorageConnection

        name = self.cleaned_data["name"].strip()
        qs = StorageConnection.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A connection with this name already exists.")
        return name

    def clean_public_base_url(self):
        url = (self.cleaned_data.get("public_base_url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise forms.ValidationError("Must start with http:// or https://")
        return url

    def clean(self):
        from core.models import STORAGE_PRESETS, StorageConnection
        from core.netguard import is_safe_endpoint_url

        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if not ptype:
            return cleaned
        preset = STORAGE_PRESETS.get(ptype, {})

        # Endpoint: AWS derives its own from the region; everything else needs one.
        endpoint = (cleaned.get("endpoint_url") or "").strip()
        if ptype == StorageConnection.ProviderType.S3:
            endpoint = ""
        elif not endpoint:
            self.add_error("endpoint_url", "An endpoint URL is required for this provider.")
        if endpoint:
            # Validate here as well as in get_client so a bad endpoint is a form
            # error at save time, not a mystery failure on the next backup run.
            is_safe, error_msg = is_safe_endpoint_url(endpoint)
            if not is_safe:
                self.add_error("endpoint_url", error_msg)
        cleaned["endpoint_url"] = endpoint

        if not cleaned.get("region"):
            cleaned["region"] = preset.get("region_default", "us-east-1")

        # Credentials: required on create; blank on edit keeps the stored ones.
        has_saved = bool(
            self.instance
            and self.instance.access_key_encrypted
            and self.instance.secret_key_encrypted
        )
        if not has_saved:
            if not cleaned.get("access_key"):
                self.add_error("access_key", "An access key is required.")
            if not cleaned.get("secret_key"):
                self.add_error("secret_key", "A secret key is required.")

        return cleaned

    def save(self):
        """Create or update the StorageConnection row."""
        from core.models import StorageConnection
        from core.services.encryption_service import EncryptionService

        connection = self.instance or StorageConnection()
        connection.provider_type = self.cleaned_data["provider_type"]
        connection.name = self.cleaned_data["name"]
        connection.endpoint_url = self.cleaned_data.get("endpoint_url") or ""
        connection.region = self.cleaned_data.get("region") or "us-east-1"
        connection.bucket = self.cleaned_data["bucket"]
        connection.public_base_url = self.cleaned_data.get("public_base_url") or ""
        connection.use_ssl = self.cleaned_data.get("use_ssl", True)
        connection.path_style = self.cleaned_data.get("path_style", False)
        connection.is_default = self.cleaned_data.get("is_default", False)
        connection.enabled = self.cleaned_data.get("enabled", True)

        # Only overwrite credentials when new values are provided.
        access_key = self.cleaned_data.get("access_key")
        if access_key:
            connection.access_key_encrypted = EncryptionService.encrypt(access_key)
        secret_key = self.cleaned_data.get("secret_key")
        if secret_key:
            connection.secret_key_encrypted = EncryptionService.encrypt(secret_key)

        # StorageConnection.save() enforces the single-default invariant.
        connection.save()
        return connection


class AssetsStorageForm(forms.Form):
    """Pick which connection the plugin storage seam (StorageAPI) uses.

    Separate from the backup default on purpose: blank here means the seam is
    unavailable, and there is deliberately no fallback to the backup connection.
    """

    assets_storage = forms.ModelChoiceField(
        required=False,
        queryset=None,  # set in __init__ (avoids import-time DB access)
        empty_label="Not configured — storage seam unavailable",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Assets connection",
        help_text="Plugins read and write here through StorageAPI, each confined to "
        "its own apps/<plugin>/ prefix.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        from django.db.models import Q
        from core.models import StorageConnection

        super().__init__(*args, **kwargs)

        # Enabled rows, PLUS whatever is currently selected even if it has since
        # been disabled. Without the second half, disabling the assets connection
        # drops it out of the queryset, so the card renders as "not configured"
        # and the next save of this form silently clears the selection — losing
        # the choice rather than just its availability (and re-submitting the
        # disabled id would fail with an opaque "not a valid choice").
        selected_id = instance.assets_storage_id if instance else None
        self.fields["assets_storage"].queryset = StorageConnection.objects.filter(
            Q(enabled=True) | Q(pk=selected_id)
        ).order_by("name")
        if instance:
            self.fields["assets_storage"].initial = selected_id

    def save(self, instance):
        instance.assets_storage = self.cleaned_data.get("assets_storage")
        instance.save(update_fields=["assets_storage"])
        return instance


class AISettingsForm(forms.Form):
    """Master AI toggle + active provider selection (Services → AI Provider)."""

    claude_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enable AI for scripts",
    )

    active_provider = forms.UUIDField(
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Active provider",
        help_text="Used by scripts, Py AI, and connection tests.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import AIProvider

        choices = [("", "— none —")] + [
            (str(p.id), f"{p.name} ({p.get_provider_type_display()})")
            for p in AIProvider.objects.all()
        ]
        self.fields["active_provider"].widget.choices = choices
        if instance:
            self.fields["claude_enabled"].initial = instance.claude_enabled
            self.fields["active_provider"].initial = instance.active_ai_provider_id

    def clean_active_provider(self):
        # A valid-UUID-but-nonexistent id (e.g. the provider was deleted in
        # another tab between render and submit) must surface as a form error —
        # otherwise save() would silently store None ("AI switched off") while
        # the view reports success.
        from core.models import AIProvider

        provider_id = self.cleaned_data.get("active_provider")
        if provider_id and not AIProvider.objects.filter(pk=provider_id).exists():
            raise forms.ValidationError(
                "That provider no longer exists — it may have been deleted in "
                "another tab. Refresh and pick an existing provider."
            )
        return provider_id

    def save(self, instance):
        """Save AI settings to the GlobalSettings instance."""
        from core.models import AIProvider

        instance.claude_enabled = self.cleaned_data.get("claude_enabled", False)
        provider_id = self.cleaned_data.get("active_provider")
        instance.active_ai_provider = (
            AIProvider.objects.filter(pk=provider_id).first() if provider_id else None
        )
        instance.save()
        return instance


class AIProviderForm(forms.Form):
    """Create/edit one AIProvider profile (credential stored encrypted)."""

    from core.models import AIProvider as _AIP

    provider_type = forms.ChoiceField(
        choices=_AIP.ProviderType.choices,
        initial=_AIP.ProviderType.ANTHROPIC,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "e.g. My Z.AI plan"}
        ),
        label="Name",
    )

    base_url = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://…"}
        ),
        label="Endpoint URL",
        help_text="Prefilled per provider; required for custom endpoints. Not used for Anthropic.",
    )

    auth_method = forms.ChoiceField(
        required=False,
        choices=_AIP.AuthMethod.choices,
        initial=_AIP.AuthMethod.SUBSCRIPTION,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Authentication method",
        help_text="Anthropic only: subscription token from `claude setup-token` (starts with sk-ant-oat01-) or an API key from console.anthropic.com.",
    )

    credential = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Paste the API key or token",
                "autocomplete": "new-password",
            }
        ),
        label="Credential",
    )

    default_model = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "optional"}
        ),
        label="Default model",
        help_text="Used whenever this provider is active. Blank = account/endpoint default.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["base_url"].initial = instance.base_url
            self.fields["auth_method"].initial = instance.auth_method
            self.fields["default_model"].initial = instance.default_model

    def clean_name(self):
        from core.models import AIProvider

        name = self.cleaned_data["name"].strip()
        qs = AIProvider.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A provider with this name already exists.")
        return name

    def clean(self):
        from core.models import AIProvider, PROVIDER_PRESETS

        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if not ptype:
            return cleaned
        preset = PROVIDER_PRESETS.get(ptype, {})

        # Endpoint URL: prefill from preset; custom must supply one.
        base_url = (cleaned.get("base_url") or "").strip()
        if ptype == AIProvider.ProviderType.ANTHROPIC:
            base_url = ""
        elif not base_url:
            base_url = preset.get("base_url", "")
            if not base_url:
                self.add_error("base_url", "An endpoint URL is required for this provider.")
        cleaned["base_url"] = base_url

        # Auth method only means something for Anthropic.
        if ptype != AIProvider.ProviderType.ANTHROPIC:
            cleaned["auth_method"] = AIProvider.AuthMethod.API_KEY
        elif not cleaned.get("auth_method"):
            cleaned["auth_method"] = AIProvider.AuthMethod.SUBSCRIPTION

        # Credential: required on create except when the preset has a default
        # (Ollama). On edit, blank keeps the stored one.
        has_saved = bool(self.instance and self.instance.credential_encrypted)
        if not cleaned.get("credential") and not has_saved:
            if not preset.get("default_credential"):
                self.add_error("credential", "A credential is required for this provider.")

        return cleaned

    def save(self):
        """Create or update the AIProvider row."""
        from core.models import AIProvider
        from core.services.encryption_service import EncryptionService

        provider = self.instance or AIProvider()
        provider.provider_type = self.cleaned_data["provider_type"]
        provider.name = self.cleaned_data["name"]
        provider.base_url = self.cleaned_data.get("base_url") or ""
        provider.auth_method = self.cleaned_data["auth_method"]
        provider.default_model = self.cleaned_data.get("default_model") or ""

        # Only overwrite the credential when a new value is provided.
        credential = self.cleaned_data.get("credential")
        if credential:
            provider.credential_encrypted = EncryptionService.encrypt(credential)

        provider.save()
        return provider


class SecretProviderForm(forms.Form):
    """Create/edit one SecretProvider profile (External Secret Providers).

    The adapter-specific credential/config inputs are NOT declared as Django
    fields — they arrive as ``f_<name>`` POST keys and are validated against the
    selected backend's declarative ``fields`` specs. So adding a new adapter needs
    zero form changes: the registry drives the type dropdown, the rendered inputs,
    and this validation. Credential fields follow the same preserve-on-edit rule
    as AIProviderForm (blank keeps the stored value).
    """

    FIELD_PREFIX = "f_"

    provider_type = forms.ChoiceField(
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )
    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "e.g. Prod Vault"}
        ),
        label="Name",
    )
    cache_ttl = forms.IntegerField(
        min_value=0,
        initial=300,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS}),
        label="Cache TTL (seconds)",
        help_text="How long to cache a fetched value in-process. 0 disables caching.",
    )
    on_error = forms.ChoiceField(
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="On fetch error",
        help_text="Fail the run, or serve the last cached value if one exists.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        from core.services.secret_backends import list_backends

        super().__init__(*args, **kwargs)
        self.instance = instance
        self.fields["provider_type"].choices = [
            (b.provider_key, b.label or b.provider_key) for b in list_backends()
        ]
        self.fields["on_error"].choices = SecretProvider.OnError.choices
        if instance is not None and not self.is_bound:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["cache_ttl"].initial = instance.cache_ttl
            self.fields["on_error"].initial = instance.on_error

    def _selected_backend(self):
        from core.services.secret_backends import SecretResolutionError, get_backend

        ptype = None
        if self.is_bound:
            ptype = self.data.get("provider_type")
        if not ptype and self.instance is not None:
            ptype = self.instance.provider_type
        if not ptype:
            return None
        try:
            return get_backend(ptype)
        except SecretResolutionError:
            return None

    def clean_provider_type(self):
        from core.services.secret_backends import SecretResolutionError, get_backend

        ptype = self.cleaned_data["provider_type"]
        try:
            get_backend(ptype)
        except SecretResolutionError:
            raise forms.ValidationError("Unknown provider type.")
        return ptype

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = SecretProvider.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A provider with this name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        backend = self._selected_backend()
        if backend is None:
            return cleaned

        config = {}
        # Preserve-on-edit: start from the stored credentials and override only the
        # fields the user re-typed, so editing config without re-entering secrets works.
        creds = dict(self.instance.get_credentials()) if self.instance else {}
        for spec in backend.fields:
            raw = (self.data.get(self.FIELD_PREFIX + spec["name"], "") or "").strip()
            if spec.get("kind") == "credential":
                if raw:
                    creds[spec["name"]] = raw
                if spec.get("required") and not creds.get(spec["name"]):
                    self.add_error(None, f"{spec['label']} is required.")
            else:  # config
                config[spec["name"]] = raw
                if spec.get("required") and not raw:
                    self.add_error(None, f"{spec['label']} is required.")
        cleaned["config"] = config
        cleaned["credentials"] = creds
        return cleaned

    def save(self):
        provider = self.instance or SecretProvider()
        provider.provider_type = self.cleaned_data["provider_type"]
        provider.name = self.cleaned_data["name"]
        provider.cache_ttl = self.cleaned_data["cache_ttl"]
        provider.on_error = self.cleaned_data["on_error"]
        provider.config = self.cleaned_data.get("config", {})
        provider.set_credentials(self.cleaned_data.get("credentials", {}))
        provider.save()
        return provider


class RecaptchaSettingsForm(forms.Form):
    """Form for Google reCAPTCHA v2 login-protection configuration."""

    recaptcha_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Require reCAPTCHA on login",
    )

    recaptcha_site_key = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "6Lc...",
                "autocomplete": "off",
            }
        ),
        label="Site key",
        help_text="The public site key from the reCAPTCHA admin console (v2 “I'm not a robot”).",
    )

    recaptcha_secret_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Secret key",
        help_text="The secret key (kept server-side, encrypted at rest).",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["recaptcha_enabled"].initial = instance.recaptcha_enabled
            self.fields["recaptcha_site_key"].initial = instance.recaptcha_site_key
        self._instance = instance

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("recaptcha_enabled"):
            if not cleaned_data.get("recaptcha_site_key"):
                self.add_error(
                    "recaptcha_site_key",
                    "Site key is required to enable reCAPTCHA.",
                )
            # Secret key must be provided now or already saved.
            has_saved_secret = bool(
                self._instance and self._instance.recaptcha_secret_key_encrypted
            )
            if not cleaned_data.get("recaptcha_secret_key") and not has_saved_secret:
                self.add_error(
                    "recaptcha_secret_key",
                    "Secret key is required to enable reCAPTCHA.",
                )
        return cleaned_data

    def save(self, instance):
        """Save reCAPTCHA settings to the GlobalSettings instance."""
        from core.services.encryption_service import EncryptionService

        instance.recaptcha_enabled = self.cleaned_data.get("recaptcha_enabled", False)
        instance.recaptcha_site_key = self.cleaned_data.get("recaptcha_site_key") or ""

        # Only overwrite the secret when a new value is provided.
        secret_key = self.cleaned_data.get("recaptcha_secret_key")
        if secret_key:
            instance.recaptcha_secret_key_encrypted = EncryptionService.encrypt(secret_key)

        instance.save(
            update_fields=[
                "recaptcha_enabled",
                "recaptcha_site_key",
                "recaptcha_secret_key_encrypted",
                "updated_at",
            ]
        )
        return instance


class S3BackupScheduleForm(forms.Form):
    """Form for S3 scheduled backup configuration."""

    from core.models import GlobalSettings

    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    s3_backup_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Enable Scheduled Backups",
    )

    s3_backup_schedule = forms.ChoiceField(
        choices=GlobalSettings.S3BackupSchedule.choices,
        initial=GlobalSettings.S3BackupSchedule.DISABLED,
        widget=forms.Select(
            attrs={
                "class": INPUT_CLASS,
            }
        ),
        label="Schedule Frequency",
    )

    s3_backup_time = forms.TimeField(
        initial="02:00",
        widget=forms.TimeInput(
            attrs={
                "class": INPUT_CLASS,
                "type": "time",
            }
        ),
        label="Backup Time",
        help_text="Time to run backups (in instance timezone)",
    )

    s3_backup_day = forms.ChoiceField(
        choices=WEEKDAY_CHOICES,
        initial=0,
        widget=forms.Select(
            attrs={
                "class": INPUT_CLASS,
            }
        ),
        label="Day of Week",
        help_text="For weekly backups",
    )

    s3_backup_prefix = forms.CharField(
        required=False,
        max_length=255,
        initial="pyrunner-backups/",
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "pyrunner-backups/",
            }
        ),
        label="S3 Path Prefix",
        help_text="Path prefix for backup files in the bucket",
    )

    s3_backup_retention_count = forms.IntegerField(
        min_value=0,
        initial=7,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": "0",
            }
        ),
        label="Retention Count",
        help_text="Keep the last N backups (0 = keep all)",
    )

    s3_backup_include_runs = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include Run History",
    )

    s3_backup_max_runs = forms.IntegerField(
        min_value=0,
        initial=1000,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": "0",
            }
        ),
        label="Max Runs",
        help_text="Maximum runs to include (0 = all)",
    )

    s3_backup_include_datastores = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include DataStores",
    )

    s3_backup_include_plugins = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include installed plugins",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["s3_backup_enabled"].initial = instance.s3_backup_enabled
            self.fields["s3_backup_schedule"].initial = instance.s3_backup_schedule
            self.fields["s3_backup_time"].initial = instance.s3_backup_time
            self.fields["s3_backup_day"].initial = instance.s3_backup_day
            self.fields["s3_backup_prefix"].initial = instance.s3_backup_prefix or "pyrunner-backups/"
            self.fields["s3_backup_retention_count"].initial = instance.s3_backup_retention_count
            self.fields["s3_backup_include_runs"].initial = instance.s3_backup_include_runs
            self.fields["s3_backup_max_runs"].initial = instance.s3_backup_max_runs
            self.fields["s3_backup_include_datastores"].initial = instance.s3_backup_include_datastores
            self.fields["s3_backup_include_plugins"].initial = instance.s3_backup_include_plugins

    def save(self, instance):
        """Save the backup schedule settings."""
        from core.services.backup_schedule_service import BackupScheduleService

        instance.s3_backup_enabled = self.cleaned_data.get("s3_backup_enabled", False)
        instance.s3_backup_schedule = self.cleaned_data.get("s3_backup_schedule")
        instance.s3_backup_time = self.cleaned_data.get("s3_backup_time")
        instance.s3_backup_day = int(self.cleaned_data.get("s3_backup_day", 0))
        instance.s3_backup_prefix = self.cleaned_data.get("s3_backup_prefix") or "pyrunner-backups/"
        instance.s3_backup_retention_count = self.cleaned_data.get("s3_backup_retention_count", 7)
        instance.s3_backup_include_runs = self.cleaned_data.get("s3_backup_include_runs", False)
        instance.s3_backup_max_runs = self.cleaned_data.get("s3_backup_max_runs", 1000)
        instance.s3_backup_include_datastores = self.cleaned_data.get("s3_backup_include_datastores", True)
        instance.s3_backup_include_plugins = self.cleaned_data.get("s3_backup_include_plugins", False)

        instance.save()

        # Sync the django-q2 schedule
        BackupScheduleService.sync_schedule()

        return instance


class ChannelForm(forms.Form):
    """Create / edit a chat Channel (Channels subsystem).

    A plain Form (like StorageConnectionForm) because credentials are encrypted +
    fingerprinted on save. Provider choices are limited to *registered* providers,
    so the picker grows automatically as providers are added.

    The credential fields come from the provider (``credential_fields``): one
    form field per credential, named after it, all stored in the encrypted
    blob. A bound form builds them for the provider it was posted with; an
    unbound Add form for ``provider`` (the ``?provider=`` choice), else the
    first registered one; an edit form for the channel's own provider.
    """

    name = forms.CharField(
        max_length=120,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "Ops Alerts"}
        ),
        help_text="A label for this connection.",
    )
    provider = forms.ChoiceField(
        choices=[],  # populated in __init__ from registered providers
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    default_target = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS + " font-mono"}),
    )
    daily_send_cap = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Daily send cap",
        help_text="Max messages this channel sends per day: notifications, replies and "
        "script sends together (0 = unlimited). Sends over it are recorded as failed; "
        "PyRun's own notices (approval pending, daily limit) don't count.",
    )
    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
    )

    def __init__(self, *args, instance=None, workspace=None, provider=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        self.workspace = workspace

        from core.models import Channel
        from core.services.channels import get_provider, list_providers

        registered = set(list_providers())
        self.fields["provider"].choices = [
            (value, label)
            for value, label in Channel.Provider.choices
            if value in registered
        ]
        keys = [value for value, _ in self.fields["provider"].choices]

        # Which provider's credential fields this form carries.
        if instance is not None:
            key = instance.provider
        elif self.is_bound and self.data.get("provider") in keys:
            key = self.data.get("provider")
        elif provider in keys:
            key = provider
        else:
            key = keys[0] if keys else ""
        self.provider_key = key
        self.provider_obj = get_provider(key) if key in registered else None
        self.credential_specs = tuple(getattr(self.provider_obj, "credential_fields", ()))
        if instance is None:
            self.fields["provider"].initial = key

        stored = self._stored_credentials()
        for spec in self.credential_specs:
            self.fields[spec.name] = self._credential_field(
                spec, stored, editing=instance is not None, unreadable=self.stored_unreadable,
            )

        target = self.fields["default_target"]
        target.label = getattr(self.provider_obj, "default_target_label", "Default recipient")
        target.help_text = getattr(self.provider_obj, "default_target_help", "")
        placeholder = getattr(self.provider_obj, "default_target_placeholder", "")
        if placeholder:
            target.widget.attrs["placeholder"] = placeholder

        if instance is not None:
            self.fields["name"].initial = instance.name
            self.fields["provider"].initial = instance.provider
            self.fields["default_target"].initial = instance.default_target
            self.fields["daily_send_cap"].initial = instance.daily_send_cap
            self.fields["enabled"].initial = instance.enabled
            # Provider is fixed on edit — changing it would orphan the credentials.
            self.fields["provider"].disabled = True

    def _stored_credentials(self) -> dict:
        """The edited channel's stored credentials ({} on create).

        A blob that can't be decrypted (ENCRYPTION_KEY changed) must not break
        the page where the credentials are entered again: it reads as empty and
        ``stored_unreadable`` makes every required secret required again.
        """
        self.stored_unreadable = False
        if self.instance is None:
            return {}
        import logging

        from core.services.encryption_service import EncryptionError

        try:
            return self.instance.get_credentials()
        except (EncryptionError, ValueError):
            logging.getLogger(__name__).warning("Channel %s: stored credentials can't be decrypted", self.instance.pk)
            self.stored_unreadable = True
            return {}

    @staticmethod
    def _credential_field(spec, stored, *, editing, unreadable=False):
        # Only an existing channel has a stored secret to keep (U50), and only
        # when it can still be decrypted: an unreadable one must be entered again.
        keeps = spec.secret and editing and not unreadable
        if spec.secret:
            placeholder = "Leave blank to keep current" if keeps else spec.placeholder
            widget = forms.PasswordInput(
                attrs={
                    "class": INPUT_CLASS + " font-mono",
                    "placeholder": placeholder,
                    "autocomplete": "new-password",
                }
            )
        else:
            widget = forms.TextInput(
                attrs={"class": INPUT_CLASS + " font-mono", "placeholder": spec.placeholder}
            )
        help_text = spec.help
        if spec.secret and editing and unreadable:
            help_text = (
                f"The stored {spec.label.lower()} can't be decrypted (the server's encryption key may have changed); "
                "enter it again."
            )
        field = forms.CharField(required=False, widget=widget, label=spec.label, help_text=help_text)
        field.keeps_stored = keeps
        if editing and not spec.secret:
            field.initial = stored.get(spec.name, "")
        return field

    @property
    def credential_bound_fields(self):
        """The provider's credential fields, in order (for the templates)."""
        return [self[spec.name] for spec in self.credential_specs]

    def clean_name(self):
        from core.models import Channel

        name = (self.cleaned_data.get("name") or "").strip()
        qs = Channel.objects.filter(workspace=self.workspace, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A channel with this name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        if self.provider_obj is None:
            return cleaned

        # Merge what was typed over what is stored: a blank secret on edit keeps
        # the stored one; a non-secret field always takes the typed value.
        creds = self._stored_credentials()
        changed = False
        for spec in self.credential_specs:
            value = (cleaned.get(spec.name) or "").strip()
            if spec.secret and not value:
                if spec.required and self.instance is None:
                    self.add_error(spec.name, spec.missing_error)
                elif spec.required and self.stored_unreadable:
                    self.add_error(
                        spec.name,
                        f"The stored {spec.label.lower()} can't be decrypted "
                        "(the server's encryption key may have changed); enter it again.",
                    )
                continue
            if not value and spec.required:
                self.add_error(spec.name, spec.missing_error)
                continue
            if creds.get(spec.name, "") != value:
                changed = True
            if value:
                creds[spec.name] = value
            else:
                creds.pop(spec.name, None)
        self.credentials = creds
        self.credentials_changed = changed or self.instance is None
        if self.errors or not self.credentials_changed:
            return cleaned

        from core.models import Channel

        identity = self.provider_obj.identity_for_fingerprint(creds)
        fingerprint = Channel.fingerprint_for(self.provider_key, identity)
        if fingerprint:
            clash = Channel.objects.filter(creds_fingerprint=fingerprint)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                field = self.provider_obj.identity_field or None
                self.add_error(field, self.provider_obj.duplicate_identity_error)
        return cleaned

    def save(self, *, created_by=None):
        from core.models import Channel

        instance = self.instance or Channel(
            workspace=self.workspace, created_by=created_by
        )
        if self.instance is None:
            instance.provider = self.cleaned_data["provider"]
        instance.name = self.cleaned_data["name"]
        instance.enabled = self.cleaned_data.get("enabled", False)
        instance.daily_send_cap = self.cleaned_data.get("daily_send_cap") or 0

        config = dict(instance.config or {})
        config["default_target"] = (self.cleaned_data.get("default_target") or "").strip()
        instance.config = config

        if self.credentials_changed:
            identity = self.provider_obj.identity_for_fingerprint(self.credentials)
            instance.set_credentials(self.credentials, identity=identity)

        instance.save()
        return instance


class ChannelInboundForm(forms.Form):
    """Configure a channel's inbound handler + approval/cap settings.

    Both inbound handlers are available: ``script`` (run a script) and ``pyai``
    (the read-only Py AI assistant).
    """

    inbound_enabled = forms.BooleanField(
        required=False, widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS})
    )
    inbound_handler = forms.ChoiceField(
        required=False,
        choices=[("", "Notify only (no inbound handling)"), ("script", "Run a script")],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="When a message arrives",
    )
    inbound_target_id = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Script to run",
    )
    inbound_access = forms.ChoiceField(
        choices=[
            ("approval", "Approval inbox (recommended) — only approved senders"),
            ("open", "Open — anyone who passes signature verification"),
        ],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Who can use it",
    )
    daily_reply_cap = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Daily reply cap",
        help_text="Max replies to incoming messages per day (0 = unlimited). Run notifications and plain script sends don't count.",
    )
    # Shown for providers that can download attachments (WhatsApp).
    download_media = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Download media",
        help_text="Off: the script gets attachments as metadata only. On: images, documents, audio, video "
        "and stickers are downloaded when the message arrives and the script gets a file path (or a 24-hour "
        "link when a private assets storage connection is set). Py AI is only told what was attached.",
    )
    media_max_mb = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=100,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 1, "max": 100}),
        label="Largest attachment (MB)",
        help_text="Bigger files are not downloaded; the script gets their metadata and an error.",
    )

    def __init__(self, *args, channel=None, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel = channel

        from core.models import GlobalSettings, Script

        # Offer Py AI as a handler only when it's enabled.
        if GlobalSettings.get_settings().pyai_enabled:
            self.fields["inbound_handler"].choices = self.fields["inbound_handler"].choices + [
                ("pyai", "Ask Py AI")
            ]

        scripts = (
            Script.objects.for_workspace(workspace)
            .filter(archived_at__isnull=True)
            .order_by("name")
        )
        self.fields["inbound_target_id"].choices = [("", "— select a script —")] + [
            (str(s.id), s.name) for s in scripts
        ]

        if channel is not None and not self.is_bound:
            self.fields["inbound_enabled"].initial = channel.inbound_enabled
            self.fields["inbound_handler"].initial = channel.inbound_handler
            self.fields["inbound_target_id"].initial = (
                str(channel.inbound_target_id) if channel.inbound_target_id else ""
            )
            self.fields["inbound_access"].initial = channel.inbound_access
            self.fields["daily_reply_cap"].initial = channel.daily_reply_cap
            self.fields["download_media"].initial = channel.download_media
            self.fields["media_max_mb"].initial = channel.media_max_mb

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("inbound_enabled") and cleaned.get("inbound_handler") == "script":
            if not cleaned.get("inbound_target_id"):
                self.add_error("inbound_target_id", "Choose a script to run.")
        return cleaned


class PyAISettingsForm(forms.Form):
    """Configure the built-in Py AI assistant (instance-global, superuser-only)."""

    pyai_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enable Py AI",
    )
    pyai_model = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "claude-sonnet-4-6 (optional)"}
        ),
        label="Model",
        help_text="Optional. Blank uses the Claude default model.",
    )
    pyai_system_prompt = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"class": INPUT_CLASS, "rows": 3,
                   "placeholder": "Optional extra instruction for Py AI"}
        ),
        label="Extra system instruction",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._describe_model_field(instance)
        if instance is not None and not self.is_bound:
            self.fields["pyai_enabled"].initial = instance.pyai_enabled
            self.fields["pyai_model"].initial = instance.pyai_model
            self.fields["pyai_system_prompt"].initial = instance.pyai_system_prompt

    def _describe_model_field(self, settings):
        """Placeholder/help from the ACTIVE provider (U53), not always Claude's."""
        from core.models import PROVIDER_PRESETS

        provider = getattr(settings, "active_ai_provider", None) if settings else None
        if provider is None:
            return
        preset = PROVIDER_PRESETS.get(provider.provider_type, {})
        example = provider.default_model or preset.get("model_placeholder", "")
        example = example.replace(" (optional)", "")
        field = self.fields["pyai_model"]
        field.widget.attrs["placeholder"] = f"{example} (optional)" if example else "optional"
        if provider.default_model:
            field.help_text = (
                f"Optional. Blank uses {provider.name}'s default model, {provider.default_model}."
            )
        else:
            field.help_text = f"Optional. Blank uses {provider.name}'s default model."

    def save(self, instance):
        instance.pyai_enabled = self.cleaned_data.get("pyai_enabled", False)
        instance.pyai_model = self.cleaned_data.get("pyai_model") or ""
        instance.pyai_system_prompt = self.cleaned_data.get("pyai_system_prompt") or ""
        instance.save(update_fields=["pyai_enabled", "pyai_model", "pyai_system_prompt", "updated_at"])
        return instance


class LibraryForm(forms.ModelForm):
    """Create/edit a Library's metadata (its CODE lives in revisions, not here).

    The ``key`` stays editable after creation, matching DataStore's name: both are
    identifiers scripts reference. The help text carries the warning, because
    renaming a key breaks every `from <key> import ...` already written.
    """

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the key-uniqueness check (keys are unique
        # per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)

    class Meta:
        model = Library
        fields = ["key", "name", "description"]
        widgets = {
            "key": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS + " font-mono",
                    "placeholder": "my_helpers",
                }
            ),
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "My shared helpers",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What does this library provide?",
                }
            ),
        }
        labels = {
            "key": "Import name",
            "name": "Display name",
            "description": "Description",
        }
        help_texts = {
            "key": "Scripts import it as: from <key>.module import thing. "
            "Changing this breaks imports in scripts already using it.",
        }

    def clean_key(self):
        from core.models.library import validate_library_key

        key = self.cleaned_data.get("key", "").strip()
        # The shared validator the SDK also calls — identifier rules plus the
        # pyrunner*/stdlib reserved bans, so form and SDK can never disagree.
        validate_library_key(key)

        qs = Library.objects.filter(key__iexact=key)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A library with this import name already exists in this workspace."
            )
        return key


# =============================================================================
# Vector seam (Stage 1): embedding providers, vector connections, collections
# =============================================================================


class EmbeddingProviderForm(forms.Form):
    """Create/edit one EmbeddingProvider profile (API key stored encrypted).

    Twin of ``AIProviderForm``. Changing the model or the endpoint resets the
    recorded ``dimension`` so the row must be re-Tested before a collection can
    pin it — a stale dimension would silently mis-size every new collection.
    """

    from core.models import EmbeddingProvider as _EP

    provider_type = forms.ChoiceField(
        choices=_EP.ProviderType.choices,
        initial=_EP.ProviderType.OPENAI,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )
    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "e.g. OpenAI small"}),
        label="Name",
    )
    base_url = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "https://…"}),
        label="Endpoint base URL",
        help_text="Prefilled per provider; the embeddings path is appended automatically.",
    )
    credential = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Paste the API key",
                "autocomplete": "new-password",
            }
        ),
        label="API key",
    )
    model = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "prefilled per provider"}),
        label="Model",
        help_text="Collections pin this model when created; a different model later means a new collection.",
    )
    max_batch = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=2048,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "placeholder": "preset default"}),
        label="Texts per request",
    )
    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enabled",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["base_url"].initial = instance.base_url
            self.fields["model"].initial = instance.model
            self.fields["max_batch"].initial = instance.max_batch
            self.fields["enabled"].initial = instance.enabled

    def clean_name(self):
        from core.models import EmbeddingProvider

        name = self.cleaned_data["name"].strip()
        qs = EmbeddingProvider.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("An embedding provider with this name already exists.")
        return name

    def clean(self):
        from core.models import EMBEDDING_PRESETS
        from core.netguard import is_safe_endpoint_url

        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if not ptype:
            return cleaned
        preset = EMBEDDING_PRESETS.get(ptype, {})

        base_url = (cleaned.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            base_url = preset.get("base_url", "")
        if not base_url:
            self.add_error("base_url", "An endpoint base URL is required for this provider.")
        elif not base_url.startswith(("http://", "https://")):
            self.add_error("base_url", "Must start with http:// or https://")
        else:
            # The server posts your texts to this URL — same SSRF stance as every
            # other user-supplied endpoint (PYRUNNER_ALLOW_PRIVATE_NETWORK opens
            # localhost / compose siblings such as Ollama).
            is_safe, why = is_safe_endpoint_url(base_url)
            if not is_safe:
                self.add_error("base_url", why)
        cleaned["base_url"] = base_url

        model = (cleaned.get("model") or "").strip() or preset.get("default_model", "")
        if not model:
            self.add_error("model", "A model id is required for this provider.")
        cleaned["model"] = model

        if not cleaned.get("max_batch"):
            cleaned["max_batch"] = preset.get("max_batch", 64)

        has_saved = bool(self.instance and self.instance.credential_encrypted)
        if preset.get("credential_required") and not cleaned.get("credential") and not has_saved:
            self.add_error("credential", "An API key is required for this provider.")
        return cleaned

    def save(self):
        from core.models import EmbeddingProvider

        provider = self.instance or EmbeddingProvider()
        space_changed = bool(
            self.instance
            and (
                self.instance.model != self.cleaned_data["model"]
                or self.instance.base_url != self.cleaned_data["base_url"]
                or self.instance.provider_type != self.cleaned_data["provider_type"]
            )
        )
        provider.provider_type = self.cleaned_data["provider_type"]
        provider.name = self.cleaned_data["name"]
        provider.base_url = self.cleaned_data["base_url"]
        provider.model = self.cleaned_data["model"]
        provider.max_batch = self.cleaned_data["max_batch"]
        provider.enabled = bool(self.cleaned_data.get("enabled"))
        credential = self.cleaned_data.get("credential")
        if credential:
            provider.set_credential(credential)
        if space_changed:
            # A different model/endpoint may produce differently sized vectors.
            provider.dimension = None
            provider.last_tested_at = None
        provider.save()
        return provider


class VectorConnectionForm(forms.Form):
    """Create/edit one VectorConnection (Local needs nothing; Qdrant a URL + key)."""

    from core.models import VectorConnection as _VC

    PREFIX_PATTERN = re.compile(r"^[a-z0-9_-]{0,40}$")

    provider_type = forms.ChoiceField(
        choices=_VC.ProviderType.choices,
        initial=_VC.ProviderType.LOCAL,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Backend",
    )
    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "e.g. Qdrant (compose)"}),
        label="Name",
    )
    url = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "http://qdrant:6333"}),
        label="Qdrant URL",
        help_text="REST endpoint of the Qdrant server. A compose sibling or localhost needs PYRUNNER_ALLOW_PRIVATE_NETWORK=true.",
    )
    collection_prefix = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "optional"}),
        label="Collection prefix",
        help_text="Prepended to physical collection names on a shared Qdrant (lowercase, digits, '_' and '-').",
    )
    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Paste the API key, if the server needs one",
                "autocomplete": "new-password",
            }
        ),
        label="API key",
    )
    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enabled",
    )
    is_default = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Default for new collections",
    )
    expose_credentials = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Share credentials with runs",
        help_text=(
            "Lets a script's credentials() call receive this Qdrant's URL and API key for "
            "native qdrant-client use. The key is server-wide: any script that can open a "
            "collection here can then read every collection on the server."
        ),
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["url"].initial = instance.url
            self.fields["collection_prefix"].initial = instance.collection_prefix
            self.fields["enabled"].initial = instance.enabled
            self.fields["is_default"].initial = instance.is_default
            self.fields["expose_credentials"].initial = instance.exposes_credentials

    def clean_name(self):
        from core.models import VectorConnection

        name = self.cleaned_data["name"].strip()
        qs = VectorConnection.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A vector connection with this name already exists.")
        return name

    def clean_collection_prefix(self):
        prefix = (self.cleaned_data.get("collection_prefix") or "").strip()
        if not self.PREFIX_PATTERN.match(prefix):
            raise forms.ValidationError(
                "Only lowercase letters, digits, '_' and '-' (max 40 characters)."
            )
        return prefix

    def clean(self):
        from core.models import VectorConnection
        from core.netguard import is_safe_endpoint_url

        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if (
            self.instance is not None
            and ptype
            and ptype != self.instance.provider_type
            and self.instance.collections.exists()
        ):
            # Existing collections were created on this backend; switching it
            # would point every one of them at a store that doesn't have them.
            self.add_error(
                "provider_type",
                "This connection holds collections, so its backend can't change. "
                "Add a new connection instead.",
            )
        if ptype == VectorConnection.ProviderType.QDRANT:
            url = (cleaned.get("url") or "").strip().rstrip("/")
            if not url:
                self.add_error("url", "A Qdrant URL is required.")
            elif not url.startswith(("http://", "https://")):
                self.add_error("url", "Must start with http:// or https://")
            else:
                is_safe, why = is_safe_endpoint_url(url)
                if not is_safe:
                    self.add_error("url", why)
            cleaned["url"] = url
        else:
            cleaned["url"] = ""
            cleaned["collection_prefix"] = ""
            cleaned["api_key"] = ""
            cleaned["expose_credentials"] = False
        return cleaned

    def save(self):
        from core.models import VectorConnection

        connection = self.instance or VectorConnection()
        connection.provider_type = self.cleaned_data["provider_type"]
        connection.name = self.cleaned_data["name"]
        connection.enabled = bool(self.cleaned_data.get("enabled"))
        connection.is_default = bool(self.cleaned_data.get("is_default"))
        if connection.provider_type == VectorConnection.ProviderType.QDRANT:
            connection.config = {
                "url": self.cleaned_data["url"],
                "collection_prefix": self.cleaned_data.get("collection_prefix") or "",
                "expose_credentials": bool(self.cleaned_data.get("expose_credentials")),
            }
            api_key = self.cleaned_data.get("api_key")
            if api_key:
                connection.set_credentials({"api_key": api_key})
        else:
            connection.config = {}
            connection.credentials_encrypted = ""
        connection.save()
        return connection


class VectorCollectionForm(forms.Form):
    """Create a VectorCollection in the active workspace.

    Field-level checks live here (so errors land on the right input); the
    cross-field rules (provider tested, dimension pinned, backend implemented)
    are ``VectorService.create_collection``'s and surface as non-field errors.
    """

    name = forms.CharField(
        max_length=64,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "docs"}),
        label="Collection name",
        help_text='Used in scripts as: get_collection("name") — lowercase letters, digits, "_" and "-".',
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"class": INPUT_CLASS, "rows": 2, "placeholder": "What is indexed here?"}
        ),
        label="Description",
    )
    connection = forms.ModelChoiceField(
        queryset=None,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Vector store",
    )
    embedding_provider = forms.ModelChoiceField(
        queryset=None,
        required=False,
        empty_label="None — I will supply vectors myself",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Embedding provider",
        help_text="Pins the provider's model and dimension to this collection.",
    )
    dimension = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=8192,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "placeholder": "e.g. 1536"}),
        label="Dimension",
        help_text="Only for a vectors-only collection; taken from the provider otherwise.",
    )
    metric = forms.ChoiceField(
        choices=(),
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Similarity metric",
    )
    include_in_backup = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Include points in backups",
    )

    def __init__(self, *args, workspace=None, **kwargs):
        from core.models import EmbeddingProvider, VectorCollection
        from core.services import VectorService

        self._workspace = workspace
        super().__init__(*args, **kwargs)
        self.fields["connection"].queryset = VectorService.supported_connections()
        default = VectorService.default_connection()
        if default is not None:
            self.fields["connection"].initial = default.pk
        providers = EmbeddingProvider.objects.filter(enabled=True)
        self.fields["embedding_provider"].queryset = providers
        # Preselect the most recently tested provider (U45): "None" as the default
        # made hurried users create vectors-only collections the console can't search.
        tested = (
            providers.filter(dimension__isnull=False, last_tested_at__isnull=False)
            .order_by("-last_tested_at")
            .first()
        )
        if tested is not None:
            self.fields["embedding_provider"].initial = tested.pk
        self.fields["metric"].choices = VectorCollection.Metric.choices
        self.fields["metric"].initial = VectorCollection.Metric.COSINE

    def clean_name(self):
        from core.models import VectorCollection
        from core.models.vector_collection import NAME_RE, NAME_RULE

        name = (self.cleaned_data.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise forms.ValidationError(f"Name must be {NAME_RULE}.")
        qs = VectorCollection.objects.filter(name__iexact=name)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if qs.exists():
            raise forms.ValidationError(
                "A collection with this name already exists in this workspace."
            )
        return name

    def clean(self):
        cleaned = super().clean()
        provider = cleaned.get("embedding_provider")
        if provider is None and not cleaned.get("dimension"):
            self.add_error("dimension", "Give a dimension, or pick an embedding provider.")
        if provider is not None and provider.dimension is None:
            self.add_error(
                "embedding_provider",
                "This provider has not been tested yet — run Test on it under Services so its "
                "dimension is known.",
            )
        return cleaned


class _WallClockDateTimeField(forms.DateTimeField):
    """A date and time as typed, with no time zone (naive).

    Django's DateTimeField reads the value in the console's time zone, which
    refuses a time in that zone's daylight-saving gap or overlap. A
    maintenance window's times belong to the window's own zone, which the
    form attaches in ``clean``.
    """

    input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.DateTimeInput(
            attrs={"class": INPUT_CLASS, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
        ))
        super().__init__(**kwargs)

    def to_python(self, value):
        if value in self.empty_values:
            return None
        # BaseTemporalField's parsing (input_formats), without the zone step.
        return forms.fields.BaseTemporalField.to_python(self, value)


class MaintenanceWindowForm(forms.ModelForm):
    """Create or edit a maintenance window (global or one workspace's).

    A one-off window's two moments are typed as local date-times in the
    window's time zone; a weekly window takes a time of day and weekdays.
    The fields of the other kind are cleared on save.
    """

    WEEKDAY_CHOICES = ScheduleForm.WEEKDAY_CHOICES

    timezone = forms.ChoiceField(
        choices=get_timezone_choices,
        initial="UTC",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Time zone",
        help_text="The times below are in this time zone.",
    )
    starts_at_local = _WallClockDateTimeField(label="Starts")
    ends_at_local = _WallClockDateTimeField(label="Ends")
    weekdays = forms.TypedMultipleChoiceField(
        required=False,
        choices=WEEKDAY_CHOICES,
        coerce=int,
        widget=forms.CheckboxSelectMultiple(attrs={"class": CHECK_CLASS}),
        label="Days",
        help_text="The days the window starts on.",
    )
    start_time = forms.TimeField(
        required=False,
        widget=forms.TimeInput(attrs={"class": INPUT_CLASS, "type": "time"}, format="%H:%M"),
        label="From",
    )
    end_time = forms.TimeField(
        required=False,
        widget=forms.TimeInput(attrs={"class": INPUT_CLASS, "type": "time"}, format="%H:%M"),
        label="To",
        help_text="An end at or before the start is the next day: 23:00 to 01:00 crosses midnight.",
    )
    blocks = forms.MultipleChoiceField(
        choices=MaintenanceWindow.Block.choices,
        widget=forms.CheckboxSelectMultiple(attrs={"class": CHECK_CLASS}),
        label="While it is on, don't start",
        error_messages={"required": "Choose at least one kind of trigger to block."},
    )

    class Meta:
        model = MaintenanceWindow
        fields = ["name", "kind", "timezone", "weekdays", "start_time", "end_time", "blocks", "enabled"]
        widgets = {
            "name": forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "Nightly database upgrade"}),
            "kind": forms.RadioSelect(),
            "enabled": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        }
        labels = {"kind": "Happens", "enabled": "Enabled"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        window = self.instance
        if not self.is_bound:
            if window.pk is None:
                self.initial.setdefault("blocks", list(MaintenanceWindow.DEFAULT_BLOCKS))
            elif window.kind == MaintenanceWindow.Kind.ONE_OFF and window.starts_at and window.ends_at:
                tz = window.tzinfo
                self.initial["starts_at_local"] = window.starts_at.astimezone(tz).replace(tzinfo=None)
                self.initial["ends_at_local"] = window.ends_at.astimezone(tz).replace(tzinfo=None)

    def clean(self):
        from zoneinfo import ZoneInfo

        from django.utils import timezone as dj_timezone

        cleaned = super().clean()
        kind = cleaned.get("kind")
        tz_name = cleaned.get("timezone")
        if kind == MaintenanceWindow.Kind.ONE_OFF:
            starts, ends = cleaned.get("starts_at_local"), cleaned.get("ends_at_local")
            if starts is None:
                self.add_error("starts_at_local", "Enter when the window starts.")
            if ends is None:
                self.add_error("ends_at_local", "Enter when the window ends.")
            if starts is not None and ends is not None and tz_name:
                tz = ZoneInfo(tz_name)
                starts, ends = starts.replace(tzinfo=tz), ends.replace(tzinfo=tz)
                if ends <= starts:
                    self.add_error("ends_at_local", "The end must be after the start.")
                elif ends <= dj_timezone.now():
                    self.add_error("ends_at_local", "This window would already be over.")
                cleaned["starts_at"], cleaned["ends_at"] = starts, ends
        elif kind == MaintenanceWindow.Kind.WEEKLY:
            if not cleaned.get("weekdays"):
                self.add_error("weekdays", "Choose at least one day.")
            if cleaned.get("start_time") is None:
                self.add_error("start_time", "Enter the time the window starts.")
            if cleaned.get("end_time") is None:
                self.add_error("end_time", "Enter the time the window ends.")
        return cleaned

    def save(self, commit=True):
        window = super().save(commit=False)
        if window.kind == MaintenanceWindow.Kind.ONE_OFF:
            window.starts_at = self.cleaned_data["starts_at"]
            window.ends_at = self.cleaned_data["ends_at"]
            window.start_time = window.end_time = None
            window.weekdays = []
        else:
            window.starts_at = window.ends_at = None
            window.weekdays = sorted(set(window.weekdays))
        if commit:
            window.save()
        return window


class WebhookSettingsForm(forms.ModelForm):
    """A script's webhook reply mode, signature and rate limits (Script →
    Webhook card).

    The signing secret is write-only: it is never shown again, a blank field
    keeps the stored one, and choosing no signature clears it.
    """

    signature_secret = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASS, "autocomplete": "new-password"}),
        label="Signing secret",
    )

    class Meta:
        model = Script
        fields = [
            "webhook_mode",
            "webhook_signature",
            "webhook_signature_header",
            "webhook_signature_prefix",
            "webhook_signature_encoding",
            "webhook_signature_timestamp_header",
            "webhook_signature_tolerance",
            "webhook_rate_per_minute",
            "webhook_rate_burst",
        ]
        widgets = {
            "webhook_mode": forms.Select(attrs={"class": INPUT_CLASS}),
            "webhook_signature": forms.Select(attrs={"class": INPUT_CLASS}),
            "webhook_signature_header": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "X-Signature"}
            ),
            "webhook_signature_prefix": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "sha256="}
            ),
            "webhook_signature_encoding": forms.Select(attrs={"class": INPUT_CLASS}),
            "webhook_signature_timestamp_header": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "X-Signature-Timestamp"}
            ),
            "webhook_signature_tolerance": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 1, "max": Script.MAX_WEBHOOK_SIGNATURE_TOLERANCE}
            ),
            "webhook_rate_per_minute": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 1, "max": Script.MAX_WEBHOOK_RATE_PER_MINUTE}
            ),
            "webhook_rate_burst": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 1, "max": Script.MAX_WEBHOOK_RATE_PER_MINUTE}
            ),
        }
        labels = {
            "webhook_mode": "Reply",
            "webhook_signature": "Signature",
            "webhook_signature_header": "Signature header",
            "webhook_signature_prefix": "Prefix",
            "webhook_signature_encoding": "Encoding",
            "webhook_signature_timestamp_header": "Timestamp header",
            "webhook_signature_tolerance": "Timestamp tolerance (seconds)",
            "webhook_rate_per_minute": "Calls per minute",
            "webhook_rate_burst": "Calls per second (burst)",
        }
        help_texts = {
            "webhook_mode": "Sync waits for the run (at most the script's timeout and 55 "
            "seconds) and answers with what the script sets with pyrunner_run.respond(). "
            "A call can also ask for it with ?sync=1.",
            "webhook_signature_prefix": "Text before the signature in the header, if any.",
            "webhook_signature_timestamp_header": "Optional. When set, the signed text is "
            "<timestamp>.<body> and the timestamp (Unix seconds) must be recent.",
            "webhook_signature_tolerance": "How far a signed timestamp may be from this "
            "server's clock (Stripe, and a custom signature with a timestamp header).",
            "webhook_rate_per_minute": "Calls over it get 429 and no run.",
            "webhook_rate_burst": "The most calls taken within one second.",
        }

    def clean(self):
        from core.services.webhook_signatures import HEADER_NAME_RE

        cleaned = super().clean()
        preset = cleaned.get("webhook_signature")
        if preset and preset != Script.WebhookSignature.NONE:
            if not cleaned.get("signature_secret") and not self.instance.webhook_signature_secret_encrypted:
                self.add_error("signature_secret", "Enter the signing secret.")
        if preset == Script.WebhookSignature.HMAC_SHA256:
            if not cleaned.get("webhook_signature_header"):
                self.add_error("webhook_signature_header", "Enter the header that carries the signature.")
            # Checked only for this preset: the page hides these fields for
            # the others, so an error there couldn't be seen.
            for name in ("webhook_signature_header", "webhook_signature_timestamp_header"):
                value = cleaned.get(name)
                if value and not HEADER_NAME_RE.match(value):
                    self.add_error(name, "Use letters, digits and hyphens only (e.g. X-Signature).")
        return cleaned

    def save(self, commit=True):
        script = super().save(commit=False)
        if script.webhook_signature == Script.WebhookSignature.NONE:
            script.set_webhook_signature_secret("")
        elif self.cleaned_data.get("signature_secret"):
            script.set_webhook_signature_secret(self.cleaned_data["signature_secret"])
        if commit:
            script.save(update_fields=[*self.Meta.fields, "webhook_signature_secret_encrypted", "updated_at"])
        return script
