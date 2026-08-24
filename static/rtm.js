/* RTM Tracker — vanilla JS helpers. No dependencies. */
'use strict';

window.RTM = {
  fetchSerial: function (s) {
    return fetch('/rtm/api/serial/' + encodeURIComponent(s)).then(function (r) { return r.json(); });
  },
  validatePart: function (p) {
    return fetch('/rtm/api/part/' + encodeURIComponent(p)).then(function (r) { return r.json(); });
  }
};

var INTAKE_FIELDS = ['part_id', 'model', 'caliber', 'barrel_length', 'build_date', 'ship_date', 'original_customer_id'];

function setHidden(el, hidden) {
  if (el) el.hidden = !!hidden;
}

function wireIntake() {
  var form = document.querySelector('form[data-rtm-intake]');
  if (!form) return;

  var serialInput = form.querySelector('input[data-serial-input], input[name="serial_no"]');
  if (!serialInput) return;

  var preview = form.querySelector('[data-serial-preview]');
  var notFound = form.querySelector('[data-serial-not-found]');
  var repeatWarn = form.querySelector('[data-repeat-warning]');
  var priorList = form.querySelector('[data-prior-rtms]');

  var lastLooked = null;

  function applyResult(data) {
    setHidden(preview, false);
    var found = !!(data && data.found);
    setHidden(notFound, found);
    setHidden(repeatWarn, !(data && data.repeat));

    INTAKE_FIELDS.forEach(function (name) {
      var val = (data && data[name] != null) ? String(data[name]) : '';
      form.querySelectorAll('[data-fill="' + name + '"]').forEach(function (el) {
        if ('value' in el && el.tagName !== 'DIV' && el.tagName !== 'SPAN' && el.tagName !== 'DD') {
          el.value = val;
        } else {
          el.textContent = val || '—';
        }
      });
    });

    if (priorList) {
      priorList.textContent = '';
      ((data && data.prior_rtms) || []).forEach(function (p) {
        var li = document.createElement('li');
        var a = document.createElement('a');
        a.href = '/rtm/' + p.rtm_id;
        a.textContent = p.rtm_number || ('RTM ' + p.rtm_id);
        li.appendChild(a);
        var extra = ' — ' + (p.status || '') + (p.resolution ? ' (' + p.resolution + ')' : '');
        li.appendChild(document.createTextNode(extra));
        priorList.appendChild(li);
      });
    }
  }

  function lookup() {
    var serial = serialInput.value.trim();
    if (!serial || serial === lastLooked) return;
    lastLooked = serial;
    window.RTM.fetchSerial(serial).then(applyResult).catch(function () {
      lastLooked = null;
      setHidden(preview, false);
      setHidden(notFound, false);
      setHidden(repeatWarn, true);
    });
  }

  serialInput.addEventListener('keydown', function (e) {
    // Barcode scanners terminate with Enter; look up instead of submitting.
    if (e.key === 'Enter') {
      e.preventDefault();
      lookup();
    }
  });
  serialInput.addEventListener('blur', lookup);
}

function wirePartForm() {
  var form = document.querySelector('form[data-part-form], form[data-rtm-partform]');
  if (!form) return;

  var partInput = form.querySelector('input[data-part-input], input[name="part_id"]');
  var preview = form.querySelector('[data-part-preview]');
  var submit = form.querySelector('button[type="submit"], input[type="submit"]');
  if (!partInput) return;

  var partFound = false;
  var checkedValue = null;

  function showPreview(text, danger) {
    if (!preview) return;
    preview.hidden = !text;
    preview.textContent = text;
    preview.classList.toggle('danger', !!danger);
  }

  function check() {
    var partId = partInput.value.trim();
    if (!partId) {
      partFound = false;
      checkedValue = null;
      showPreview('', false);
      return;
    }
    if (partId === checkedValue) return;
    checkedValue = partId;
    showPreview('Checking part…', false);
    window.RTM.validatePart(partId).then(function (data) {
      if (partInput.value.trim() !== partId) return; // stale response
      partFound = !!(data && data.found);
      if (partFound) {
        var cost = data.unit_cost != null ? ' — $' + data.unit_cost : '';
        showPreview((data.description || data.part_id) + cost, false);
      } else {
        showPreview('Part not found in VISUAL', true);
      }
    }).catch(function () {
      partFound = false;
      checkedValue = null;
      showPreview('Part lookup failed', true);
    });
  }

  partInput.addEventListener('change', check);
  partInput.addEventListener('blur', check);
  partInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      check();
    }
  });

  form.addEventListener('submit', function (e) {
    if (!partFound) {
      if (!window.confirm('Part was not found in VISUAL. Add it anyway?')) {
        e.preventDefault();
      }
    }
  });
}

document.addEventListener('DOMContentLoaded', function () {
  wireIntake();
  wirePartForm();
});
