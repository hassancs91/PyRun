// The shared data grid (templates/cpanel/_grid.html, core/grid.py).
//
// - The add-filter form offers only the operators the chosen column takes and
//   folds its three fields into one `f=<column>:<op>:<value>` parameter.
// - An editable cell (td[data-edit]) opens an inline editor on click or Enter:
//   a text/number/date/link input, a Yes/No select for booleans, a dialog with
//   Format for JSON. Saving POSTs {"column", "value"} as JSON to the row's
//   data-cell-url (the JSON dialog adds "text", the JSON as typed, so the server
//   can read numbers JavaScript would round); the server checks and converts
//   the value against the column and answers {"html", "raw"} (the re-rendered
//   cell) or {"error"}. The dialog shows and formats the JSON text with
//   window.JsonText.reindent (static/js/json_editor.js), which keeps literals.
// Without JavaScript the grid still sorts, filters, pages, adds and deletes.
(function () {
    'use strict';

    var INPUT = 'w-full min-w-[8rem] px-2 py-1 bg-ink border border-ok/60 rounded-md text-text text-sm ' +
        'focus:outline-none focus:ring-2 focus:ring-ok/30';

    function toast(message) {
        if (window.Toast) window.Toast.show(message, 'error');
        else window.alert(message);
    }

    // ------------------------------------------------------------ filter form
    document.querySelectorAll('[data-grid-filter]').forEach(function (form) {
        var ops = JSON.parse(form.dataset.ops || '{}');
        var valueless = JSON.parse(form.dataset.valueless || '[]');
        var column = form.querySelector('[name=fc]');
        var op = form.querySelector('[name=fo]');
        var value = form.querySelector('[name=fv]');

        function syncOps() {
            var list = ops[column.value] || [];
            var keep = op.value;
            op.innerHTML = '';
            list.forEach(function (pair) {
                var option = document.createElement('option');
                option.value = pair[0];
                option.textContent = pair[1];
                op.appendChild(option);
            });
            if (list.some(function (pair) { return pair[0] === keep; })) op.value = keep;
            syncValue();
        }

        function syncValue() {
            var none = valueless.indexOf(op.value) !== -1;
            value.disabled = none;
            value.classList.toggle('opacity-40', none);
        }

        column.addEventListener('change', syncOps);
        op.addEventListener('change', syncValue);
        syncOps();

        form.addEventListener('submit', function () {
            var hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = 'f';
            hidden.value = column.value + ':' + op.value + ':' + (value.disabled ? '' : value.value);
            form.appendChild(hidden);
            [column, op, value].forEach(function (field) { field.disabled = true; });
        });
    });

    // ----------------------------------------------------------- cell editing
    var grids = document.querySelectorAll('[data-grid]');
    if (!grids.length) return;
    var dialog = document.querySelector('[data-grid-json]');
    var editing = null;

    function save(td, value, text) {
        var row = td.closest('tr');
        var grid = td.closest('[data-grid]');
        var payload = { column: td.dataset.col, value: value };
        if (text !== undefined) payload.text = text;
        td.classList.add('opacity-60');
        return fetch(row.dataset.cellUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': grid.dataset.csrf, 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }).then(function (response) {
            return response.json().catch(function () { return { error: 'The server answered ' + response.status + '.' }; })
                .then(function (body) {
                    td.classList.remove('opacity-60');
                    if (!response.ok || body.error) throw new Error(body.error || ('Save failed (' + response.status + ').'));
                    td.innerHTML = body.html;
                    td.dataset.raw = body.raw;
                    return body;
                });
        }, function () {
            td.classList.remove('opacity-60');
            throw new Error('Could not reach PyRun.');
        });
    }

    function current(td) {
        try { return JSON.parse(td.dataset.raw); } catch (e) { return null; }
    }

    function finish(td, html) {
        if (html !== undefined) td.innerHTML = html;
        if (editing === td) editing = null;
    }

    function openInline(td) {
        var type = td.dataset.type;
        var before = td.innerHTML;
        var value = current(td);
        var field;
        if (type === 'boolean') {
            field = document.createElement('select');
            [['', '—'], ['true', 'Yes'], ['false', 'No']].forEach(function (pair) {
                var option = document.createElement('option');
                option.value = pair[0];
                option.textContent = pair[1];
                field.appendChild(option);
            });
            field.value = value === true ? 'true' : (value === false ? 'false' : '');
        } else {
            field = document.createElement('input');
            field.type = { number: 'number', date: 'date', link: 'url' }[type] || 'text';
            if (type === 'number') field.step = 'any';
            if (type === 'datetime') field.placeholder = '2026-11-12T09:30:00Z';
            field.value = value === null || value === undefined ? '' : String(value);
        }
        field.className = INPUT;
        field.setAttribute('aria-label', 'Edit ' + td.dataset.col);
        td.innerHTML = '';
        td.appendChild(field);
        field.focus();
        if (field.select) field.select();
        var done = false;

        function commit() {
            if (done) return;
            done = true;
            var raw = field.value;
            var original = value === null || value === undefined ? '' : String(value);
            if (raw === original) {
                finish(td, before);
                return;
            }
            var next = raw === '' ? null : raw;
            if (type === 'boolean' && raw !== '') next = raw === 'true';
            save(td, next).then(function () { finish(td); }, function (err) {
                toast(err.message);
                finish(td, before);
            });
        }

        function cancel() {
            if (done) return;
            done = true;
            finish(td, before);
            td.focus();
        }

        field.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') { event.preventDefault(); commit(); }
            else if (event.key === 'Escape') { event.preventDefault(); cancel(); }
        });
        field.addEventListener('blur', commit);
        if (type === 'boolean') field.addEventListener('change', commit);
    }

    function openJson(td) {
        if (!dialog) return;
        var text = dialog.querySelector('[data-grid-json-text]');
        var error = dialog.querySelector('[data-grid-json-error]');
        var value = current(td);
        dialog.querySelector('[data-grid-json-title]').textContent = 'Edit ' + td.dataset.col;
        text.value = value === null || value === undefined ? '' : pretty(td.dataset.raw, value);
        error.textContent = '';
        dialog._td = td;
        dialog.showModal();
        text.focus();
    }

    // The JSON text re-indented as typed (no number rounded); JSON.stringify without json_editor.js.
    function pretty(raw, value) {
        return window.JsonText ? window.JsonText.reindent(raw, '  ') : JSON.stringify(value, null, 2);
    }

    if (dialog) {
        var text = dialog.querySelector('[data-grid-json-text]');
        var error = dialog.querySelector('[data-grid-json-error]');

        function parsed() {
            if (text.value.trim() === '') return { value: null };
            try { return { value: JSON.parse(text.value) }; }
            catch (e) { error.textContent = 'Not valid JSON: ' + e.message; return null; }
        }

        dialog.querySelector('[data-grid-json-format]').addEventListener('click', function () {
            var result = parsed();
            if (result && result.value !== null) { text.value = pretty(text.value, result.value); error.textContent = ''; }
        });
        dialog.querySelector('[data-grid-json-cancel]').addEventListener('click', function () { dialog.close(); });
        dialog.querySelector('[data-grid-json-save]').addEventListener('click', function () {
            var result = parsed();
            if (!result) return;
            var td = dialog._td;
            save(td, result.value, text.value.trim()).then(function () { dialog.close(); }, function (err) {
                error.textContent = err.message;
            });
        });
        dialog.addEventListener('close', function () {
            if (dialog._td) { dialog._td.focus(); editing = null; dialog._td = null; }
        });
    }

    function open(td) {
        if (editing) return;
        editing = td;
        if (td.dataset.type === 'json') openJson(td);
        else openInline(td);
    }

    grids.forEach(function (grid) {
        grid.addEventListener('click', function (event) {
            if (event.target.closest('a, button, input, select, textarea')) return;
            var td = event.target.closest('td[data-edit]');
            if (td && td.closest('[data-grid]') === grid) open(td);
        });
        grid.addEventListener('keydown', function (event) {
            if (event.key !== 'Enter') return;
            var td = event.target;
            if (td.matches && td.matches('td[data-edit]')) { event.preventDefault(); open(td); }
        });
    });
})();
