// JSON text helpers and the JSON editor of the DataStore entry pages.
//
// window.JsonText.reindent(text, indent) re-spaces valid JSON text without
// parsing it into JavaScript values, so every literal stays as typed: a long
// integer (JavaScript rounds past 2^53) or 1.0 (JavaScript prints 1) is kept.
// indent '' gives the compact form. The data grid's JSON dialog uses it too.
//
// A [data-json-editor] block (templates/cpanel/datastores/_value_editor.html)
// gets Format / Compact buttons and a live "valid JSON" line, and its form
// isn't sent while the text isn't valid JSON (the server checks it again).
(function () {
    'use strict';

    function reindent(text, indent) {
        var out = '', depth = 0, inString = false, n = text.length;
        function pad(d) { return indent ? '\n' + new Array(d + 1).join(indent) : ''; }
        for (var i = 0; i < n; i++) {
            var ch = text[i];
            if (inString) {
                out += ch;
                if (ch === '\\') { out += text[++i]; } else if (ch === '"') { inString = false; }
                continue;
            }
            if (ch === '"') { inString = true; out += ch; continue; }
            if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') continue;
            if (ch === '{' || ch === '[') {
                var j = i + 1;
                while (j < n && /\s/.test(text[j])) j++;
                if (text[j] === (ch === '{' ? '}' : ']')) { out += ch + text[j]; i = j; continue; }
                depth++;
                out += ch + pad(depth);
                continue;
            }
            if (ch === '}' || ch === ']') { depth--; out += pad(depth) + ch; continue; }
            if (ch === ',') { out += ',' + pad(depth); continue; }
            if (ch === ':') { out += indent ? ': ' : ':'; continue; }
            out += ch;
        }
        return out;
    }

    function kind(value) {
        if (value === null) return 'null';
        if (Array.isArray(value)) return 'array';
        return typeof value === 'object' ? 'object' : typeof value;
    }

    // {value} for valid JSON, {error} otherwise; {empty: true} for blank text.
    function check(text) {
        if (text.trim() === '') return { empty: true };
        try { return { value: JSON.parse(text) }; } catch (e) { return { error: e.message }; }
    }

    window.JsonText = { reindent: reindent, check: check, kind: kind };

    document.querySelectorAll('[data-json-editor]').forEach(function (box) {
        var area = box.querySelector('textarea');
        var status = box.querySelector('[data-json-status]');
        if (!area || !status) return;
        box.querySelectorAll('[data-json-format], [data-json-compact]').forEach(function (b) { b.hidden = false; });

        function show() {
            var result = check(area.value);
            status.classList.toggle('text-fail', !!result.error);
            status.classList.toggle('text-faint', !result.error);
            if (result.empty) status.textContent = '';
            else if (result.error) status.textContent = 'Not valid JSON: ' + result.error;
            else status.textContent = 'Valid JSON · ' + kind(result.value) + ' · ' + area.value.length + ' characters';
            return result;
        }

        function respace(indent) {
            if (show().value === undefined) return;
            area.value = reindent(area.value, indent);
            show();
        }

        box.querySelector('[data-json-format]').addEventListener('click', function () { respace('  '); });
        box.querySelector('[data-json-compact]').addEventListener('click', function () { respace(''); });
        var timer = null;
        area.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(show, 250); });
        var form = area.form;
        if (form) {
            form.addEventListener('submit', function (event) {
                if (show().error) { event.preventDefault(); area.focus(); }
            });
        }
        show();
    });
})();
