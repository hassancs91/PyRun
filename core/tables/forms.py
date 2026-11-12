"""Console form for a table's name, slug, description and columns."""

import json

from django import forms

from core.forms import INPUT_CLASS
from core.models import Table

from .columns import clean_columns
from .errors import TableError
from .service import SLUG_MAX, SLUG_RE, slug_for


class TableForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "Leads"}),
    )
    slug = forms.CharField(
        max_length=SLUG_MAX, required=False,
        help_text='What scripts use: table("<slug>"). Lowercase letters, digits, - and _; '
                  "left empty, it is made from the name.",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS + " font-mono", "placeholder": "leads"}),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 2, "placeholder": "What is this table for?"}),
    )
    columns = forms.CharField(
        help_text='A JSON list of {"key", "type", "required", "default"}.',
        widget=forms.Textarea(attrs={
            "class": INPUT_CLASS + " font-mono", "rows": 8, "spellcheck": "false", "data-table-columns": "",
        }),
    )

    def __init__(self, *args, workspace=None, instance: Table | None = None, **kwargs):
        self.workspace = workspace
        self.instance = instance
        if instance is not None and not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "name": instance.name, "slug": instance.slug, "description": instance.description,
                "columns": json.dumps(instance.columns, indent=2),
            }
        elif instance is None and "initial" not in kwargs:
            kwargs["initial"] = {"columns": json.dumps(
                [{"key": "name", "type": "text", "required": True, "default": None}], indent=2,
            )}
        super().__init__(*args, **kwargs)

    def _others(self):
        qs = Table.objects.filter(workspace=self.workspace)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        return qs

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("A table needs a name.")
        if self._others().filter(name__iexact=name).exists():
            raise forms.ValidationError("This workspace already has a table with this name.")
        return name

    def clean_slug(self):
        slug = self.cleaned_data.get("slug", "").strip()
        if slug and not SLUG_RE.match(slug):
            raise forms.ValidationError("Use lowercase letters, digits, - and _ (starting with a letter or digit).")
        return slug

    def clean_columns(self):
        try:
            columns = json.loads(self.cleaned_data["columns"])
        except ValueError as exc:
            raise forms.ValidationError(f"Columns must be valid JSON: {exc}") from None
        try:
            return clean_columns(columns)
        except TableError as exc:
            raise forms.ValidationError(exc.message) from None

    def clean(self):
        data = super().clean()
        if "name" in data and "slug" in data:
            slug = data["slug"] or slug_for(data["name"])
            if self._others().filter(slug=slug).exists():
                self.add_error("slug", f'This workspace already has a table with the slug "{slug}".')
            data["slug"] = slug
        return data
