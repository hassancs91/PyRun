// Tables: the column builder on the create/edit form.
//
// The `columns` textarea (a JSON list of {key, type, required, default}) is
// what the form submits. The builder shows one line per column and rewrites
// the textarea on every change; "Edit as JSON" switches to the textarea
// itself. A list the builder can't read opens as JSON. The server checks the
// columns again on save (core.tables.columns.clean_columns).
(function () {
    'use strict';

    var builder = document.querySelector('[data-columns-builder]');
    var rawBox = document.querySelector('[data-columns-raw]');
    var toggle = document.querySelector('[data-columns-mode]');
    var textarea = document.querySelector('textarea[data-table-columns]');
    if (!builder || !rawBox || !toggle || !textarea) return;

    var TYPES = JSON.parse(builder.dataset.types || '[]');
    var INPUT = 'px-2.5 py-1.5 bg-ink border border-line rounded-lg text-text text-sm placeholder-faint/60 ' +
        'focus:outline-none focus:ring-2 focus:ring-ok/30 focus:border-ok/60';
    var columns = [];

    function el(tag, attrs) {
        var node = document.createElement(tag);
        Object.keys(attrs || {}).forEach(function (key) {
            if (key === 'text') node.textContent = attrs[key];
            else if (key === 'className') node.className = attrs[key];
            else node.setAttribute(key, attrs[key]);
        });
        return node;
    }

    function write() {
        textarea.value = JSON.stringify(columns.map(function (c) {
            var def = c.default;
            if (def === '') def = null;
            return { key: c.key, type: c.type, required: !!c.required, default: def };
        }), null, 2);
    }

    function readable(list) {
        return Array.isArray(list) && list.every(function (c) {
            return c && typeof c === 'object' && typeof c.key === 'string' &&
                TYPES.some(function (t) { return t[0] === (c.type || 'text'); }) &&
                (c.default === undefined || c.default === null || typeof c.default !== 'object');
        });
    }

    function line(column, index) {
        var row = el('div', { className: 'flex flex-wrap items-center gap-2' });
        var key = el('input', { type: 'text', className: INPUT + ' font-mono w-40', placeholder: 'key', 'aria-label': 'Column key' });
        key.value = column.key;
        key.addEventListener('input', function () { column.key = key.value.trim(); write(); });
        var type = el('select', { className: INPUT, 'aria-label': 'Column type' });
        TYPES.forEach(function (t) { var o = el('option', { value: t[0], text: t[1] }); type.appendChild(o); });
        type.value = column.type;
        type.addEventListener('change', function () { column.type = type.value; write(); });
        var reqLabel = el('label', { className: 'inline-flex items-center gap-1.5 text-xs text-muted' });
        var req = el('input', { type: 'checkbox', className: 'w-4 h-4 rounded text-ok bg-ink border-line' });
        req.checked = !!column.required;
        req.addEventListener('change', function () { column.required = req.checked; write(); });
        reqLabel.appendChild(req);
        reqLabel.appendChild(document.createTextNode('required'));
        var def = el('input', { type: 'text', className: INPUT + ' w-32', placeholder: 'default', 'aria-label': 'Default value' });
        def.value = column.default === null || column.default === undefined ? '' : String(column.default);
        def.addEventListener('input', function () { column.default = def.value; write(); });
        var remove = el('button', {
            type: 'button', text: 'Remove', 'aria-label': 'Remove column',
            className: 'text-xs px-2 py-1 rounded-md border border-fail/30 bg-fail/10 text-fail hover:bg-fail/20 transition-colors',
        });
        remove.addEventListener('click', function () { columns.splice(index, 1); render(); write(); });
        [key, type, reqLabel, def, remove].forEach(function (node) { row.appendChild(node); });
        return row;
    }

    function render() {
        builder.innerHTML = '';
        columns.forEach(function (column, index) { builder.appendChild(line(column, index)); });
        var add = el('button', {
            type: 'button', text: 'Add column',
            className: 'text-[13px] px-3 py-1.5 rounded-lg border border-line text-muted hover:text-text hover:border-faint transition-colors',
        });
        add.addEventListener('click', function () {
            columns.push({ key: '', type: 'text', required: false, default: null });
            render();
            write();
            var keys = builder.querySelectorAll('input[aria-label="Column key"]');
            if (keys.length) keys[keys.length - 1].focus();
        });
        builder.appendChild(add);
    }

    function showBuilder() {
        var list;
        try { list = JSON.parse(textarea.value || '[]'); } catch (e) { list = null; }
        if (!readable(list)) return false;
        columns = list.map(function (c) {
            return { key: c.key, type: c.type || 'text', required: !!c.required, default: c.default === undefined ? null : c.default };
        });
        render();
        builder.classList.remove('hidden');
        rawBox.classList.add('hidden');
        toggle.textContent = 'Edit as JSON';
        return true;
    }

    function showRaw() {
        builder.classList.add('hidden');
        rawBox.classList.remove('hidden');
        toggle.textContent = 'Use the builder';
    }

    toggle.classList.remove('hidden');
    toggle.addEventListener('click', function () {
        if (rawBox.classList.contains('hidden')) showRaw();
        else if (!showBuilder()) window.alert('The builder can\'t show this JSON; fix it here first.');
    });
    if (!showBuilder()) showRaw();
})();
