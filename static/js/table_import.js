// Tables -> Import CSV (templates/cpanel/tables/import.html).
//
// - Reads the chosen file's first line and shows one "fills column" select per
//   CSV column, prefilled the way the server matches names (core/tables/csv_io.py
//   match_header): case and the characters in the form's data-separators
//   (spaces, dashes, underscores) are ignored; id / created_at / updated_at are
//   skipped. A header that matches two columns, or two headers that match one
//   column, are left on "Skip" with a note, never guessed. The choice is sent as
//   the `mapping` field (one column key or null per header cell).
// - A dry run answers in place, so the chosen file stays for the import.
// Without JavaScript the form still imports; the server matches the names itself
// and refuses an ambiguous match.
(function () {
    'use strict';

    var form = document.getElementById('csv-import');
    if (!form) return;
    var columns = JSON.parse(form.dataset.columns || '[]');
    var system = JSON.parse(form.dataset.system || '[]');
    var separators = new RegExp(form.dataset.separators || '\\s+', 'g');
    var file = document.getElementById('id_file');
    var box = document.getElementById('csv-mapping');
    var rows = document.getElementById('csv-mapping-rows');
    var hidden = document.getElementById('id_mapping');
    var report = document.getElementById('csv-report');
    var header = [];

    function norm(s) { return (s || '').replace(separators, '').toLowerCase(); }

    // The first record of a CSV text (quotes and doubled quotes handled).
    function firstRecord(text) {
        var cells = [], cell = '', quoted = false, i = 0;
        if (text.charCodeAt(0) === 0xFEFF) { i = 1; }
        for (; i < text.length; i++) {
            var ch = text[i];
            if (quoted) {
                if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
                else if (ch === '"') { quoted = false; }
                else { cell += ch; }
            } else if (ch === '"') { quoted = true; }
            else if (ch === ',') { cells.push(cell); cell = ''; }
            else if (ch === '\n' || ch === '\r') { break; }
            else { cell += ch; }
        }
        cells.push(cell);
        return cells;
    }

    // For each header cell: {key} (the column it fills, '' for none) or {note} (ambiguous).
    function matchHeader() {
        var skipped = system.map(norm);
        var candidates = header.map(function (name) {
            if (skipped.indexOf(norm(name)) >= 0) return [];
            return columns.filter(function (key) { return norm(key) === norm(name); });
        });
        var headersByKey = {};
        candidates.forEach(function (keys) {
            if (keys.length === 1) headersByKey[keys[0]] = (headersByKey[keys[0]] || 0) + 1;
        });
        return candidates.map(function (keys) {
            if (keys.length > 1) return { key: '', note: 'Matches more than one column (' + keys.join(', ') + '): choose one.' };
            if (keys.length && headersByKey[keys[0]] > 1) {
                return { key: '', note: 'Another CSV column also matches "' + keys[0] + '": choose which fills it.' };
            }
            return { key: keys[0] || '', note: '' };
        });
    }

    // One column key (or null: skipped) per header cell, in order, so repeated header names stay apart.
    function syncMapping() {
        var mapping = [];
        rows.querySelectorAll('select').forEach(function (sel) {
            mapping.push(sel.value || null);
        });
        hidden.value = header.length ? JSON.stringify(mapping) : '';
    }

    function buildMapping() {
        rows.textContent = '';
        matchHeader().forEach(function (match, position) {
            var name = header[position];
            var line = document.createElement('div');
            line.className = 'grid grid-cols-1 sm:grid-cols-2 gap-2 items-center';
            var title = document.createElement('label');
            title.className = 'text-sm font-mono text-text truncate';
            title.htmlFor = 'csv-map-' + position;
            title.textContent = name || '(no name)';
            var sel = document.createElement('select');
            sel.id = 'csv-map-' + position;
            sel.className = 'px-2.5 py-1.5 bg-ink border border-line rounded-lg text-text text-sm focus:outline-none focus:ring-2 focus:ring-ok/30';
            var skip = document.createElement('option');
            skip.value = ''; skip.textContent = 'Skip';
            sel.appendChild(skip);
            columns.forEach(function (key) {
                var opt = document.createElement('option');
                opt.value = key; opt.textContent = key;
                if (key === match.key) { opt.selected = true; }
                sel.appendChild(opt);
            });
            sel.addEventListener('change', syncMapping);
            line.appendChild(title);
            line.appendChild(sel);
            if (match.note) {
                var note = document.createElement('p');
                note.id = 'csv-map-' + position + '-note';
                note.className = 'text-xs text-warn sm:col-span-2';
                note.textContent = match.note;
                sel.setAttribute('aria-describedby', note.id);
                line.appendChild(note);
            }
            rows.appendChild(line);
        });
        box.hidden = !header.length;
        syncMapping();
    }

    file.addEventListener('change', function () {
        header = [];
        report.textContent = '';
        if (!file.files.length) { buildMapping(); return; }
        var reader = new FileReader();
        reader.onload = function () { header = firstRecord(String(reader.result)); buildMapping(); };
        reader.readAsText(file.files[0].slice(0, 65536));
    });

    form.addEventListener('submit', function (event) {
        if (!document.getElementById('id_dry_run').checked || !window.fetch) { return; }
        event.preventDefault();
        report.textContent = 'Checking…';
        fetch(form.action || window.location.href, {
            method: 'POST', body: new FormData(form), headers: { 'Accept': 'application/json' }, credentials: 'same-origin'
        }).then(function (resp) { return resp.json(); }).then(function (data) {
            report.textContent = '';
            var p = document.createElement('p');
            p.className = 'text-sm text-text rounded-xl border border-line-soft bg-ink p-4';
            if (data.error) {
                p.textContent = data.error;
            } else if (data.error_count) {
                p.textContent = data.rows + ' rows checked; ' + data.error_count + " can't be imported. " +
                    data.errors.map(function (e) { return (e.line ? 'Line ' + e.line + ': ' : '') + e.message; }).join(' ');
            } else {
                p.textContent = data.rows + ' rows would be imported. Untick Dry run and import.';
            }
            report.appendChild(p);
        }).catch(function () { report.textContent = 'The check failed. Try again.'; });
    });
})();
