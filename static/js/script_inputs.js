// Script editor: the Code | Inputs tabs and the Inputs field builder.
//
// The textarea `inputs_schema` is the only thing the form submits. In Fields
// mode the builder rewrites it on every change; Raw JSON mode edits it
// directly. A schema the builder can't represent (arrays, nested objects,
// type lists, non-text choices) opens in Raw JSON mode. Keywords the builder
// doesn't edit (maxLength, minimum, ...) are kept as they are. The server
// checks the schema again on save (core.services.script_inputs).
(function () {
    'use strict';

    var INPUT = 'w-full px-3 py-2 bg-ink border border-line rounded-lg text-text text-sm ' +
        'placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 focus:border-ok/60 transition-colors';
    var CHECK = 'w-4 h-4 rounded text-ok bg-ink border-line focus:ring-ok/40 focus:ring-2';
    var NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
    var NAME_MAX = 64;
    var KINDS = [
        ['text', 'Text'],
        ['number', 'Number'],
        ['boolean', 'Yes / no'],
        ['select', 'Select'],
        ['json', 'JSON object'],
    ];
    var HANDLED = ['type', 'title', 'description', 'default', 'enum'];

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        Object.keys(attrs || {}).forEach(function (key) {
            if (key === 'text') node.textContent = attrs[key];
            else if (key === 'className') node.className = attrs[key];
            else node.setAttribute(key, attrs[key]);
        });
        (children || []).forEach(function (child) { if (child) node.appendChild(child); });
        return node;
    }

    function omit(obj, keys) {
        var out = {};
        Object.keys(obj).forEach(function (k) { if (keys.indexOf(k) === -1) out[k] = obj[k]; });
        return out;
    }

    // ---- schema <-> rows --------------------------------------------------

    function defaultText(kind, prop) {
        if (!('default' in prop)) return '';
        var value = prop['default'];
        if (kind === 'json') return JSON.stringify(value, null, 2);
        if (kind === 'boolean') return value ? 'true' : 'false';
        return String(value);
    }

    // Rows for a schema, or {error} when the builder can't represent it.
    function parseSchema(schema) {
        if (schema === null) return { rows: [], top: { additionalProperties: false } };
        if (typeof schema !== 'object' || Array.isArray(schema) || schema.type !== 'object') {
            return { error: 'The schema is not an object schema.' };
        }
        var props = schema.properties || {};
        if (typeof props !== 'object' || Array.isArray(props)) return { error: 'properties is not an object.' };
        var required = Array.isArray(schema.required) ? schema.required : [];
        var rows = [];
        var names = Object.keys(props);
        for (var i = 0; i < names.length; i++) {
            var name = names[i];
            var prop = props[name];
            if (!prop || typeof prop !== 'object' || Array.isArray(prop)) return { error: '"' + name + '" is not a schema object.' };
            var kind = null;
            var whole = false;
            if ('enum' in prop) {
                var textChoices = prop.type === 'string' && Array.isArray(prop['enum']) &&
                    prop['enum'].every(function (v) { return typeof v === 'string'; });
                if (!textChoices) return { error: '"' + name + '" has choices that are not text.' };
                kind = 'select';
            } else if (prop.type === 'string') {
                kind = 'text';
            } else if (prop.type === 'number' || prop.type === 'integer') {
                kind = 'number';
                whole = prop.type === 'integer';
            } else if (prop.type === 'boolean') {
                kind = 'boolean';
            } else if (prop.type === 'object' && !('properties' in prop)) {
                kind = 'json';
            } else {
                return { error: '"' + name + '" is an array, a nested object or a list of types.' };
            }
            rows.push({
                name: name,
                kind: kind,
                whole: whole,
                label: prop.title || '',
                description: prop.description || '',
                required: required.indexOf(name) !== -1,
                defaultText: defaultText(kind, prop),
                options: kind === 'select' ? prop['enum'].join('\n') : '',
                extra: omit(prop, HANDLED),
            });
        }
        return { rows: rows, top: omit(schema, ['type', 'properties', 'required']) };
    }

    function optionList(row) {
        var seen = {};
        return row.options.split('\n').map(function (s) { return s.trim(); }).filter(function (s) {
            if (!s || seen[s]) return false;
            seen[s] = true;
            return true;
        });
    }

    // {schema, errors}: schema is null when there are no rows (no inputs).
    function buildSchema(state) {
        var errors = [];
        var properties = {};
        var required = [];
        var exported = {};
        state.rows.forEach(function (row, index) {
            row.error = '';
            var name = row.name.trim();
            var fail = function (message) { if (!row.error) { row.error = message; errors.push({ index: index, message: message }); } };
            if (!name) fail('Give this input a name.');
            else if (name.length > NAME_MAX || !NAME_RE.test(name)) fail('Names start with a letter or underscore and use only letters, digits and underscores (at most 64).');
            else if (exported[name.toUpperCase()]) fail('Clashes with "' + exported[name.toUpperCase()] + '": both would be INPUT_' + name.toUpperCase() + '.');
            if (name) exported[name.toUpperCase()] = name;

            var prop = {};
            var choices = [];
            if (row.kind === 'text') prop.type = 'string';
            else if (row.kind === 'number') prop.type = row.whole ? 'integer' : 'number';
            else if (row.kind === 'boolean') prop.type = 'boolean';
            else if (row.kind === 'select') {
                prop.type = 'string';
                choices = optionList(row);
                if (!choices.length) fail('Add at least one option.');
            } else prop.type = 'object';
            if (row.label.trim()) prop.title = row.label.trim();
            if (row.description.trim()) prop.description = row.description.trim();
            if (row.kind === 'select') prop['enum'] = choices;
            Object.keys(row.extra).forEach(function (k) { prop[k] = row.extra[k]; });

            var text = row.defaultText;
            if (text.trim() !== '') {
                if (row.kind === 'text') prop['default'] = text;
                else if (row.kind === 'boolean') prop['default'] = text === 'true';
                else if (row.kind === 'select') {
                    if (choices.indexOf(text) === -1) fail('The default must be one of the options.');
                    else prop['default'] = text;
                } else if (row.kind === 'number') {
                    var number = Number(text.trim());
                    if (!isFinite(number)) fail('The default is not a number.');
                    else if (row.whole && !Number.isInteger(number)) fail('The default must be a whole number.');
                    else prop['default'] = number;
                } else {
                    try {
                        var value = JSON.parse(text);
                        if (!value || typeof value !== 'object' || Array.isArray(value)) fail('The default must be a JSON object.');
                        else prop['default'] = value;
                    } catch (e) {
                        fail("The default isn't valid JSON.");
                    }
                }
            }
            if (name) {
                properties[name] = prop;
                if (row.required) required.push(name);
            }
        });
        if (!state.rows.length) return { schema: null, errors: errors };
        var schema = { type: 'object', properties: properties };
        if (required.length) schema.required = required;
        Object.keys(state.top).forEach(function (k) { schema[k] = state.top[k]; });
        return { schema: schema, errors: errors };
    }

    // ---- the editor -------------------------------------------------------

    function init(root) {
        var form = root.closest('form');
        var textarea = root.querySelector('textarea[name="inputs_schema"]');
        var list = root.querySelector('[data-inputs-list]');
        var empty = root.querySelector('[data-inputs-empty]');
        var builder = root.querySelector('[data-inputs-builder]');
        var raw = root.querySelector('[data-inputs-raw]');
        var note = root.querySelector('[data-inputs-note]');
        var count = document.querySelector('[data-inputs-count]');
        var modeButtons = root.querySelectorAll('[data-inputs-mode]');
        if (!form || !textarea || !list) return;

        var state = { rows: [], top: { additionalProperties: false } };
        var mode = 'fields';

        function setNote(message) {
            note.textContent = message || '';
            note.classList.toggle('hidden', !message);
        }

        function updateCount() {
            var n = 0;
            if (mode === 'fields') n = state.rows.length;
            else {
                try {
                    var parsed = JSON.parse(textarea.value || 'null');
                    n = parsed && parsed.properties ? Object.keys(parsed.properties).length : 0;
                } catch (e) { n = 0; }
            }
            if (!count) return;
            count.textContent = String(n);
            count.classList.toggle('hidden', n === 0);
        }

        function sync() {
            var result = buildSchema(state);
            textarea.value = result.schema ? JSON.stringify(result.schema, null, 2) : '';
            list.querySelectorAll('[data-row-error]').forEach(function (p, i) {
                var message = state.rows[i] ? state.rows[i].error : '';
                p.textContent = message || '';
                p.classList.toggle('hidden', !message);
            });
            updateCount();
            return result;
        }

        function parseTextarea() {
            var text = textarea.value.trim();
            try {
                return parseSchema(text ? JSON.parse(text) : null);
            } catch (e) {
                return { error: "The JSON isn't valid." };
            }
        }

        function showMode() {
            builder.classList.toggle('hidden', mode !== 'fields');
            raw.classList.toggle('hidden', mode !== 'raw');
            modeButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-inputs-mode') === mode)); });
            updateCount();
        }

        function setMode(next) {
            if (next === mode) return;
            if (next === 'fields') {
                var parsed = parseTextarea();
                if (parsed.error) {
                    setNote('The field builder can\'t show this schema: ' + parsed.error + ' Keep editing it as raw JSON.');
                    return;
                }
                state = parsed;
                render();
            } else {
                sync();
            }
            mode = next;
            setNote('');
            showMode();
        }

        function labelled(text, control, extraClass) {
            return el('label', { className: 'block ' + (extraClass || '') }, [
                el('span', { className: 'block text-xs font-medium text-muted mb-1', text: text }),
                control,
            ]);
        }

        function iconButton(label, path, onClick) {
            var button = el('button', {
                type: 'button',
                'aria-label': label,
                title: label,
                className: 'inline-grid place-items-center w-8 h-8 rounded-lg border border-line text-muted hover:text-text hover:border-faint transition-colors',
            }, [el('span', { 'aria-hidden': 'true', className: 'text-sm leading-none', text: path })]);
            button.addEventListener('click', onClick);
            return button;
        }

        function defaultControl(row, onChange) {
            var control;
            if (row.kind === 'boolean' || row.kind === 'select') {
                control = el('select', { className: INPUT });
                var options = row.kind === 'boolean'
                    ? [['', 'No default'], ['true', 'Yes (ticked)'], ['false', 'No (unticked)']]
                    : [['', 'No default']].concat(optionList(row).map(function (o) { return [o, o]; }));
                options.forEach(function (o) {
                    var option = el('option', { value: o[0], text: o[1] });
                    if (o[0] === row.defaultText) option.selected = true;
                    control.appendChild(option);
                });
            } else if (row.kind === 'json') {
                control = el('textarea', { className: INPUT + ' font-mono text-xs', rows: '3', spellcheck: 'false', placeholder: '{"key": "value"}' });
                control.value = row.defaultText;
            } else {
                control = el('input', { className: INPUT, type: row.kind === 'number' ? 'number' : 'text', placeholder: 'No default' });
                if (row.kind === 'number') control.setAttribute('step', row.whole ? '1' : 'any');
                control.value = row.defaultText;
            }
            control.addEventListener(control.tagName === 'SELECT' ? 'change' : 'input', function () {
                row.defaultText = control.value;
                onChange();
            });
            return control;
        }

        function renderRow(row, index) {
            var card = el('div', { className: 'rounded-xl border border-line bg-ink/40 p-4 space-y-3', 'data-row': '' });

            var name = el('input', { className: INPUT + ' font-mono', type: 'text', placeholder: 'city', maxlength: String(NAME_MAX), spellcheck: 'false', autocomplete: 'off' });
            name.value = row.name;
            name.addEventListener('input', function () { row.name = name.value; sync(); });

            var kind = el('select', { className: INPUT });
            KINDS.forEach(function (k) {
                var option = el('option', { value: k[0], text: k[1] });
                if (k[0] === row.kind) option.selected = true;
                kind.appendChild(option);
            });
            kind.addEventListener('change', function () {
                row.kind = kind.value;
                row.whole = false;
                row.defaultText = '';
                row.extra = {};  // maxLength & co. belonged to the old type
                render();
                sync();
            });

            var tools = el('div', { className: 'flex items-center gap-1' }, [
                iconButton('Move up', '↑', function () { move(index, -1); }),
                iconButton('Move down', '↓', function () { move(index, 1); }),
                iconButton('Remove input', '✕', function () { state.rows.splice(index, 1); render(); sync(); }),
            ]);
            card.appendChild(el('div', { className: 'grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_10rem_auto] gap-3 items-end' }, [
                labelled('Name', name), labelled('Type', kind), tools,
            ]));

            var label = el('input', { className: INPUT, type: 'text', placeholder: 'Shown on the Run now form' });
            label.value = row.label;
            label.addEventListener('input', function () { row.label = label.value; sync(); });
            var defaultSlot = el('div');
            var refreshDefault = function () {
                defaultSlot.textContent = '';
                defaultSlot.appendChild(labelled('Default', defaultControl(row, sync)));
            };
            refreshDefault();
            card.appendChild(el('div', { className: 'grid grid-cols-1 sm:grid-cols-2 gap-3' }, [labelled('Label', label), defaultSlot]));

            if (row.kind === 'select') {
                var options = el('textarea', { className: INPUT, rows: '3', placeholder: 'One option per line' });
                options.value = row.options;
                options.addEventListener('input', function () { row.options = options.value; sync(); });
                options.addEventListener('change', refreshDefault);
                card.appendChild(labelled('Options', options));
            }

            var description = el('input', { className: INPUT, type: 'text', placeholder: 'Help text under the field (optional)' });
            description.value = row.description;
            description.addEventListener('input', function () { row.description = description.value; sync(); });
            card.appendChild(labelled('Description', description));

            var flags = el('div', { className: 'flex flex-wrap items-center gap-x-5 gap-y-2' });
            var required = el('input', { type: 'checkbox', className: CHECK });
            required.checked = row.required;
            required.addEventListener('change', function () { row.required = required.checked; sync(); });
            flags.appendChild(el('label', { className: 'inline-flex items-center gap-2 text-xs text-muted cursor-pointer' }, [required, el('span', { text: 'Required' })]));
            if (row.kind === 'number') {
                var whole = el('input', { type: 'checkbox', className: CHECK });
                whole.checked = row.whole;
                whole.addEventListener('change', function () { row.whole = whole.checked; refreshDefault(); sync(); });
                flags.appendChild(el('label', { className: 'inline-flex items-center gap-2 text-xs text-muted cursor-pointer' }, [whole, el('span', { text: 'Whole numbers only' })]));
            }
            card.appendChild(flags);

            var extraKeys = Object.keys(row.extra);
            if (extraKeys.length) {
                card.appendChild(el('p', { className: 'text-xs text-faint', text: 'Also set (edit in Raw JSON): ' + extraKeys.join(', ') }));
            }
            card.appendChild(el('p', { className: 'hidden text-xs text-fail', 'data-row-error': '', role: 'alert' }));
            return card;
        }

        function move(index, delta) {
            var target = index + delta;
            if (target < 0 || target >= state.rows.length) return;
            var row = state.rows.splice(index, 1)[0];
            state.rows.splice(target, 0, row);
            render();
            sync();
        }

        function render() {
            list.textContent = '';
            state.rows.forEach(function (row, i) { list.appendChild(renderRow(row, i)); });
            empty.classList.toggle('hidden', state.rows.length > 0);
        }

        root.querySelector('[data-inputs-add]').addEventListener('click', function () {
            state.rows.push({ name: '', kind: 'text', whole: false, label: '', description: '', required: false, defaultText: '', options: '', extra: {} });
            render();
            sync();
            var last = list.lastElementChild && list.lastElementChild.querySelector('input');
            if (last) last.focus();
        });

        modeButtons.forEach(function (b) {
            b.addEventListener('click', function () { setMode(b.getAttribute('data-inputs-mode')); });
        });
        textarea.addEventListener('input', updateCount);

        // A builder error would otherwise be saved as a silently different
        // schema: stop the submit and point at it.
        form.addEventListener('submit', function (event) {
            if (mode !== 'fields') return;
            var result = sync();
            if (!result.errors.length) return;
            event.preventDefault();
            window.selectEditorTab('inputs');
            setNote('Not saved: fix the highlighted inputs first.');
            var bad = list.children[result.errors[0].index];
            var field = bad && bad.querySelector('input, select, textarea');
            if (field) field.focus();
        });

        // Start in Fields mode when the saved schema fits the builder, else show
        // the text untouched in Raw JSON mode.
        var initial = parseTextarea();
        if (initial.error) {
            mode = 'raw';
            setNote('Showing raw JSON: ' + initial.error);
        } else {
            state = initial;
            render();
        }
        showMode();
    }

    // ---- tabs ---------------------------------------------------------------

    function initTabs(root) {
        var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-editor-tab]'));
        function select(name, focus) {
            tabs.forEach(function (tab) {
                var on = tab.getAttribute('data-editor-tab') === name;
                tab.setAttribute('aria-selected', String(on));
                tab.tabIndex = on ? 0 : -1;
                var panel = document.getElementById(tab.getAttribute('aria-controls'));
                if (panel) panel.hidden = !on;
                if (on && focus) tab.focus();
            });
        }
        window.selectEditorTab = function (name) { select(name, false); };
        tabs.forEach(function (tab, i) {
            tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
            tab.addEventListener('click', function () { select(tab.getAttribute('data-editor-tab'), false); });
            tab.addEventListener('keydown', function (event) {
                var step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
                if (!step) return;
                event.preventDefault();
                select(tabs[(i + step + tabs.length) % tabs.length].getAttribute('data-editor-tab'), true);
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var tabs = document.querySelector('[data-editor-tabs]');
        if (tabs) initTabs(tabs);
        var editor = document.querySelector('[data-inputs-editor]');
        if (editor) init(editor);
    });
})();
