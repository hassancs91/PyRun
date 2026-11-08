"""
Setup form for the SDK Showcase plugin.

Plain ``forms.Form`` (no model) — the plugin persists everything through the SDK,
so there is nothing to ModelForm. Inputs reuse the console token classes so the
page matches the rest of PyRun.
"""

from django import forms

INPUT_CLASS = (
    "w-full px-3.5 py-2.5 bg-ink border border-line rounded-lg text-text text-sm "
    "placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 "
    "focus:border-ok/60 transition-colors"
)

# form-field name -> the clean env-var the secret injects under (one demo secret).
SECRET_FIELDS = {"demo_token": "DEMO_TOKEN"}

# The demo script declares "message" and "steps" as inputs with these limits,
# and the saved settings become their defaults, so the setup form enforces the
# same limits (a default outside them would be refused).
MESSAGE_MAX_LENGTH = 200
STEPS_MIN, STEPS_MAX = 1, 30


class SetupForm(forms.Form):
    environment = forms.ChoiceField(
        choices=[], widget=forms.Select(attrs={"class": INPUT_CLASS}),
        help_text="Any environment works — the demo worker uses only the standard library.",
    )
    demo_token = forms.CharField(
        label="Demo secret (DEMO_TOKEN)", required=False,
        widget=forms.TextInput(attrs={
            "class": INPUT_CLASS,
            "placeholder": "any value — stored encrypted, injected into the script",
        }),
        help_text="Stored as an owner-scoped Secret and injected into the demo script as $DEMO_TOKEN.",
    )
    message = forms.CharField(
        label="Demo message", required=False, initial="hello from the data store",
        max_length=MESSAGE_MAX_LENGTH,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="Non-secret config saved to the data store; the worker prints it.",
    )
    steps = forms.IntegerField(
        label="Worker steps", min_value=STEPS_MIN, max_value=STEPS_MAX, initial=5,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS}),
        help_text="How many heartbeat steps the worker loops — so you can watch progress and test Stop.",
    )

    def __init__(self, *args, environments=None, has_secret=False, **kwargs):
        super().__init__(*args, **kwargs)
        names = [e.name for e in (environments or [])]
        self.fields["environment"].choices = [(n, n) for n in names]
        if not names:
            self.fields["environment"].required = False
        # The secret is required only on first setup; once stored, blank keeps it.
        if has_secret:
            self.fields["demo_token"].widget.attrs["placeholder"] = "configured — leave blank to keep"


class RunInputsForm(forms.Form):
    """Values for one run of the demo script, sent as ``queue_run(inputs=...)``.

    Only turns the posted text into the right JSON types. The limits (message
    length, 1 to 30 steps) are deliberately NOT repeated here: they live in the
    script's declared inputs, and the SDK checks them, so an out-of-range value
    shows the SDK's own error on the field. A blank field is left out, and the
    input's default (the saved setting) fills it in.
    """

    message = forms.CharField(
        label="Message", required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )
    steps = forms.IntegerField(
        label="Steps", required=False,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS}),
    )

    def inputs(self):
        """The filled-in fields as an inputs dict (call after ``is_valid()``)."""
        return {k: v for k, v in self.cleaned_data.items() if v not in (None, "")}
