// Script editor: the Env vars tab.
//
// Each row is a plain pair of inputs (env_vars_name / env_vars_value) the form
// posts as is; the server pairs them up in order, drops blank rows and checks
// the names (core.forms.EnvVarsField). This only adds and removes rows, keeps
// the tab's count, and warns while you type when a name matches a secret the
// script gets: in All mode every workspace secret, in Selected mode the
// attached ones (the chips in the Secrets box) plus the plugin's own.
(function () {
    'use strict';

    function init(root) {
        var list = root.querySelector('[data-env-list]');
        var template = root.querySelector('template[data-env-row-template]');
        var count = document.querySelector('[data-env-count]');
        var names = { all: [], owned: [] };
        var data = document.getElementById('env-secret-names');
        if (data) {
            try { names = JSON.parse(data.textContent); } catch (e) { /* keep the empty lists */ }
        }

        function visibleSecrets() {
            var mode = document.querySelector('input[name="injection_mode"]:checked');
            if (!mode || mode.value !== 'selected') return names.all || [];
            var visible = (names.owned || []).slice();
            document.querySelectorAll('#secret-chips .secret-chip').forEach(function (chip) {
                var first = chip.firstChild;
                if (first && first.nodeType === Node.TEXT_NODE) visible.push(first.textContent.trim());
            });
            return visible;
        }

        function refresh() {
            var visible = visibleSecrets();
            var filled = 0;
            list.querySelectorAll('[data-env-row]').forEach(function (row) {
                var name = row.querySelector('[data-env-name]').value.trim();
                if (name || row.querySelector('[data-env-value]').value) filled += 1;
                row.querySelector('[data-env-warning]').hidden = !(name && visible.indexOf(name) !== -1);
            });
            if (count) {
                count.textContent = String(filled);
                count.classList.toggle('hidden', !filled);
            }
        }

        function addRow(focus) {
            var row = template.content.firstElementChild.cloneNode(true);
            list.appendChild(row);
            if (focus) row.querySelector('[data-env-name]').focus();
            refresh();
        }

        root.addEventListener('click', function (event) {
            var remove = event.target.closest('[data-env-remove]');
            if (remove) {
                remove.closest('[data-env-row]').remove();
                // Keep one empty row to type into.
                if (!list.querySelector('[data-env-row]')) addRow(false);
                refresh();
            } else if (event.target.closest('[data-env-add]')) {
                addRow(true);
            }
        });
        root.addEventListener('input', refresh);
        document.addEventListener('change', function (event) {
            if (event.target.name === 'injection_mode') refresh();
        });
        var chips = document.getElementById('secret-chips');
        if (chips && window.MutationObserver) {
            new MutationObserver(refresh).observe(chips, { childList: true });
        }
        refresh();
    }

    document.addEventListener('DOMContentLoaded', function () {
        var root = document.querySelector('[data-env-editor]');
        if (root) init(root);
    });
})();
