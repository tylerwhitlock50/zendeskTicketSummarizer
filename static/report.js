/* Repair report charts (Chart.js). Data comes from the JSON block the page
   renders; the tables under "All numbers as tables" hold the same figures. */
(function () {
  "use strict";
  var el = document.getElementById("report-data");
  if (!el || !window.Chart) return;  // no chart library (offline): the tables still work
  var D = JSON.parse(el.textContent);
  var C = D.charts;

  var css = getComputedStyle(document.documentElement);
  function v(name, fallback) { return (css.getPropertyValue(name) || "").trim() || fallback; }
  var INK = v("--ink", "#12130F"), INK2 = v("--ink-2", "#3A3D35"), MUTED = v("--muted", "#6E6A61");
  var LINE = v("--line", "#DFDBD2"), SURFACE = v("--surface", "#FFFFFF");
  var BRAND = v("--brand", "#1F3A2E"), EMBER = v("--accent", "#C9662A");

  // One fixed colour per stage, the same in every chart (colour follows the stage).
  var STAGE_COLOR = {
    added: "#2a78d6", received: "#eb6834", inspected: "#1baf7a",
    repaired: "#eda100", tested: "#e87ba4", shipped: "#008300"
  };
  // Days in stage: light to dark blue, then ember for the ones that are late.
  var AGE_COLOR = ["#9ec5f4", "#5598e7", "#256abf", "#104281", EMBER];

  function label(key) {
    if (key === "added") return "Added to queue";
    if (key === "queued") return "Waiting to receive";
    return D.stages[key] || key;
  }

  Chart.defaults.font.family = '"Barlow", "Segoe UI", system-ui, sans-serif';
  Chart.defaults.font.size = 13;
  Chart.defaults.color = INK2;
  Chart.defaults.borderColor = LINE;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation.duration = 300;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.pointStyle = "rectRounded";
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  Chart.defaults.plugins.legend.labels.padding = 14;
  Chart.defaults.plugins.legend.labels.color = INK2;
  Chart.defaults.plugins.legend.position = "bottom";
  Chart.defaults.plugins.tooltip.backgroundColor = INK;
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 2;
  Chart.defaults.plugins.tooltip.boxPadding = 4;
  Chart.defaults.plugins.tooltip.usePointStyle = true;

  function axisX(extra) {
    return Object.assign({ grid: { display: false }, border: { color: LINE }, ticks: { color: MUTED } }, extra || {});
  }
  function axisCount(extra) {
    return Object.assign({
      beginAtZero: true,
      grid: { color: "#EEEBE5" },
      border: { display: false },
      ticks: { color: MUTED, precision: 0 }
    }, extra || {});
  }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  function fmtDay(iso) {
    var d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function addDays(iso, n) {
    var d = new Date(iso + "T12:00:00");
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  }
  function drill(start, end) {
    window.location = D.report_url + "?start=" + start + "&end=" + end;
  }

  // Segmented toggle buttons: mark the active one, call back on change.
  function seg(attr, initial, onChange) {
    var buttons = document.querySelectorAll("[data-" + attr + "]");
    function set(value) {
      buttons.forEach(function (b) { b.setAttribute("aria-pressed", b.dataset[attr] === value ? "true" : "false"); });
    }
    buttons.forEach(function (b) {
      b.addEventListener("click", function () { set(b.dataset[attr]); onChange(b.dataset[attr]); });
    });
    set(initial);
  }

  function empty(canvasId, text) {
    var box = document.getElementById(canvasId).parentNode;
    box.classList.add("chart-empty");
    box.innerHTML = '<p class="empty">' + text + "</p>";
  }

  // ------------------------------------------------------------ activity
  (function () {
    var keys = ["added"].concat(D.stage_keys);
    var grain = C.days.length > 45 ? "week" : "day";
    var style = C.days.length < 3 ? "bar" : "line";
    var chart;
    // Start with what came in against what went out; the rest are a click away in the key.
    var hidden = { added: true, inspected: true, repaired: true, tested: true };

    function series() {
      if (grain === "day") {
        return {
          labels: C.days.map(fmtDay),
          ranges: C.days.map(function (d) { return [d, d]; }),
          data: keys.map(function (k) { return C.daily[k]; })
        };
      }
      // Weeks start Monday; the first and last may be partial.
      var labels = [], ranges = [], data = keys.map(function () { return []; });
      C.days.forEach(function (day, i) {
        var dow = (new Date(day + "T12:00:00").getDay() + 6) % 7;
        if (i === 0 || dow === 0) {
          var weekEnd = addDays(day, 6 - dow);
          var last = C.days[C.days.length - 1];
          ranges.push([day, weekEnd < last ? weekEnd : last]);
          labels.push("Week of " + fmtDay(addDays(day, -dow)));
          data.forEach(function (arr) { arr.push(0); });
        }
        keys.forEach(function (k, s) { data[s][data[s].length - 1] += C.daily[k][i]; });
      });
      return { labels: labels, ranges: ranges, data: data };
    }

    function draw() {
      var s = series();
      if (chart) {
        hidden = {};
        chart.data.datasets.forEach(function (ds, i) { hidden[ds.key] = !chart.isDatasetVisible(i); });
        chart.destroy();
      }
      var isLine = style === "line";
      chart = new Chart(document.getElementById("chart-activity"), {
        type: isLine ? "line" : "bar",
        data: {
          labels: s.labels,
          datasets: keys.map(function (k, i) {
            return {
              key: k,
              label: label(k),
              data: s.data[i],
              hidden: !!hidden[k],
              borderColor: isLine ? STAGE_COLOR[k] : SURFACE,
              backgroundColor: STAGE_COLOR[k],
              borderWidth: 2,
              cubicInterpolationMode: "monotone",
              pointRadius: s.labels.length > 40 ? 0 : 3,
              pointHoverRadius: 5,
              pointBackgroundColor: STAGE_COLOR[k],
              pointBorderColor: SURFACE,
              borderRadius: isLine ? 0 : 3,
              maxBarThickness: 18
            };
          })
        },
        options: {
          interaction: { mode: "index", intersect: false },
          scales: {
            x: axisX({ stacked: !isLine, ticks: { color: MUTED, maxRotation: 0, autoSkipPadding: 16 } }),
            y: axisCount({ stacked: !isLine })
          },
          plugins: {
            tooltip: {
              callbacks: {
                title: function (items) {
                  var r = s.ranges[items[0].dataIndex];
                  if (r[0] === r[1]) return fmtDay(r[0]);
                  var full = addDays(r[0], 6) === r[1];
                  return fmtDay(r[0]) + " – " + fmtDay(r[1]) + (full ? "" : " (part week)");
                },
                footer: function (items) {
                  var total = items.reduce(function (a, it) { return a + it.parsed.y; }, 0);
                  return plural(total, "scan") + " · click to zoom in";
                }
              }
            }
          },
          onHover: function (evt, els) { evt.native.target.style.cursor = els.length ? "pointer" : "default"; },
          onClick: function (evt, els) {
            if (!els.length) return;
            var r = s.ranges[els[0].index];
            drill(r[0], r[1]);
          }
        }
      });
    }

    seg("grain", grain, function (g) { grain = g; draw(); });
    seg("style", style, function (st) { style = st; draw(); });
    draw();
  })();

  // ------------------------------------------------------------ where the time goes
  (function () {
    var t = D.turnaround;
    var steps = [
      ["Waiting for the customer to ship", t.wait],
      [label("received") + " → " + label("inspected"), t.inspect],
      [label("inspected") + " → " + label("repaired"), t.repair],
      [label("repaired") + " → " + label("shipped"), t.ship]
    ];
    if (steps.every(function (s) { return s[1] === null; })) {
      return empty("chart-steps", "Nothing shipped in this period.");
    }
    new Chart(document.getElementById("chart-steps"), {
      type: "bar",
      data: {
        labels: steps.map(function (s) { return s[0]; }),
        datasets: [{
          label: "Average days",
          data: steps.map(function (s) { return s[1] === null ? null : Math.max(0, s[1]); }),
          backgroundColor: BRAND,
          borderRadius: 4,
          borderSkipped: "start",
          maxBarThickness: 26
        }]
      },
      options: {
        indexAxis: "y",
        scales: {
          x: axisCount({ title: { display: true, text: "days", color: MUTED } }),
          y: axisX({ ticks: { color: INK2 } })
        },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function (it) { return " " + it.parsed.x.toFixed(1) + " days on average"; } } }
        }
      }
    });
  })();

  // ------------------------------------------------------------ turnaround spread
  (function () {
    var measure = "shop";
    var total = function (m) { return C.spread[m].reduce(function (a, b) { return a + b; }, 0); };
    if (!total("shop") && !total("total")) {
      document.querySelectorAll("[data-spread]").forEach(function (b) { b.hidden = true; });
      return empty("chart-spread", "Nothing shipped in this period.");
    }
    var chart = new Chart(document.getElementById("chart-spread"), {
      type: "bar",
      data: {
        labels: C.turnaround_buckets,
        datasets: [{
          label: "Shipped RMAs",
          data: C.spread[measure],
          backgroundColor: BRAND,
          borderRadius: 4,
          borderSkipped: "start",
          categoryPercentage: 0.9,
          barPercentage: 0.95
        }]
      },
      options: {
        scales: {
          x: axisX({ title: { display: true, text: "days", color: MUTED } }),
          y: axisCount()
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) { return items[0].label + " days"; },
              label: function (it) {
                var n = it.parsed.y, all = total(measure);
                return " " + plural(n, "RMA") + (all ? " (" + Math.round(100 * n / all) + "%)" : "");
              }
            }
          }
        }
      }
    });
    seg("spread", measure, function (m) {
      measure = m;
      chart.data.datasets[0].data = C.spread[m];
      chart.update();
    });
  })();

  // ------------------------------------------------------------ open now, aging
  (function () {
    // Board order (the JSON arrives with its keys sorted A-Z).
    var stageKeys = ["queued"].concat(D.stage_keys).filter(function (k) { return k in C.aging; });
    var any = stageKeys.some(function (k) { return C.aging[k].some(function (n) { return n > 0; }); });
    if (!any) return empty("chart-aging", "Nothing open right now.");
    new Chart(document.getElementById("chart-aging"), {
      type: "bar",
      data: {
        labels: stageKeys.map(label),
        datasets: C.age_buckets.map(function (bucket, b) {
          return {
            label: bucket,
            data: stageKeys.map(function (k) { return C.aging[k][b]; }),
            backgroundColor: AGE_COLOR[b],
            borderColor: SURFACE,
            borderWidth: { left: 0, right: 2, top: 0, bottom: 0 },
            borderSkipped: false,
            maxBarThickness: 26
          };
        })
      },
      options: {
        indexAxis: "y",
        interaction: { mode: "index", axis: "y", intersect: false },
        scales: {
          x: axisCount({ stacked: true, title: { display: true, text: "open RMAs", color: MUTED } }),
          y: axisX({ stacked: true, ticks: { color: INK2 } })
        },
        plugins: {
          tooltip: {
            filter: function (it) { return it.parsed.x > 0; },
            callbacks: {
              label: function (it) { return " " + it.dataset.label + " in stage: " + it.parsed.x; },
              footer: function (items) {
                var n = items.reduce(function (a, it) { return a + it.parsed.x; }, 0);
                return plural(n, "open RMA") + " · click to open the queue";
              }
            }
          }
        },
        onHover: function (evt, els) { evt.native.target.style.cursor = els.length ? "pointer" : "default"; },
        onClick: function (evt, els) { if (els.length) window.location = D.queue_url; }
      }
    });
  })();

  // ------------------------------------------------------------ by person
  (function () {
    var names = Object.keys(D.techs);
    if (!names.length) return empty("chart-techs", "No activity in this period.");
    var keys = ["added"].concat(D.stage_keys);
    names.sort(function (a, b) { return D.techs[b].total - D.techs[a].total; });
    var other = names.map(function (n) { return (D.techs[n].action || 0) + (D.techs[n].note || 0); });
    document.getElementById("box-techs").style.height = Math.max(200, names.length * 40 + 90) + "px";
    var datasets = keys.map(function (k) {
      return {
        label: label(k),
        data: names.map(function (n) { return D.techs[n][k] || 0; }),
        backgroundColor: STAGE_COLOR[k]
      };
    });
    if (other.some(function (n) { return n > 0; })) {
      datasets.push({ label: "Notes & quick actions", data: other, backgroundColor: "#C6C2B8" });
    }
    datasets.forEach(function (ds) {
      ds.borderColor = SURFACE;
      ds.borderWidth = { left: 0, right: 2, top: 0, bottom: 0 };
      ds.borderSkipped = false;
      ds.maxBarThickness = 24;
    });
    new Chart(document.getElementById("chart-techs"), {
      type: "bar",
      data: { labels: names, datasets: datasets },
      options: {
        indexAxis: "y",
        interaction: { mode: "index", axis: "y", intersect: false },
        scales: {
          x: axisCount({ stacked: true }),
          y: axisX({ stacked: true, ticks: { color: INK2 } })
        },
        plugins: {
          tooltip: {
            filter: function (it) { return it.parsed.x > 0; },
            callbacks: {
              footer: function (items) { return plural(D.techs[items[0].label].total, "scan") + " in all"; }
            }
          }
        }
      }
    });
  })();

  // ------------------------------------------------------------ ranked lists
  var TOP = 8;
  function ranked(key, canvasId, boxId, unit, emptyText) {
    var rows = D[key] || [];
    if (!rows.length) return empty(canvasId, emptyText);
    var showAll = false;
    var btn = document.querySelector('.show-all[data-target="' + key + '"]');
    var chart;
    function draw() {
      var shown = showAll ? rows : rows.slice(0, TOP);
      document.getElementById(boxId).style.height = Math.max(160, shown.length * 32 + 40) + "px";
      var data = {
        labels: shown.map(function (r) { return r.label; }),
        datasets: [{
          label: unit,
          data: shown.map(function (r) { return r.n; }),
          backgroundColor: shown.map(function (r) {
            return r.label.indexOf("Other: ") === 0 || r.label === "(nothing recorded)" ? "#C6C2B8" : BRAND;
          }),
          borderRadius: 4,
          borderSkipped: "start",
          maxBarThickness: 22
        }]
      };
      if (chart) { chart.data = data; chart.update(); chart.resize(); return; }
      chart = new Chart(document.getElementById(canvasId), {
        type: "bar",
        data: data,
        options: {
          indexAxis: "y",
          scales: {
            x: axisCount(),
            y: axisX({ ticks: { color: INK2, autoSkip: false, callback: function (val) {
              var s = this.getLabelForValue(val);
              return s.length > 34 ? s.slice(0, 33) + "…" : s;
            } } })
          },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function (it) { return " " + plural(it.parsed.x, unit); } } }
          }
        }
      });
    }
    if (btn && rows.length > TOP) {
      btn.hidden = false;
      btn.textContent = "Show all " + rows.length;
      btn.addEventListener("click", function () {
        showAll = !showAll;
        btn.textContent = showAll ? "Show top " + TOP : "Show all " + rows.length;
        draw();
      });
    }
    draw();
  }
  ranked("problems", "chart-problems", "box-problems", "RMA", "No inspections in this period.");
  ranked("solutions", "chart-solutions", "box-solutions", "RMA", "No repairs in this period.");
  ranked("points", "chart-points", "box-points", "RMA", "No issues marked in this period.");

  // ------------------------------------------------------------ quality
  (function () {
    var a = D.accuracy, c = D.complaint, arr = D.arrivals;
    var rows = [
      ["Accuracy test passed", a.passed, BRAND],
      ["Accuracy test failed", a.failed, EMBER],
      ["Test not required", a.not_required, "#C6C2B8"],
      ["Matched the complaint", c.yes || 0, BRAND],
      ["Partly matched", c.partly || 0, "#C6C2B8"],
      ["Did not match the complaint", c.no || 0, EMBER],
      ["Arrived damaged (of " + arr.received + " received)", arr.damaged, EMBER]
    ];
    if (rows.every(function (r) { return !r[1]; })) {
      return empty("chart-quality", "No tests or inspections in this period.");
    }
    new Chart(document.getElementById("chart-quality"), {
      type: "bar",
      data: {
        labels: rows.map(function (r) { return r[0]; }),
        datasets: [{
          label: "RMAs",
          data: rows.map(function (r) { return r[1]; }),
          backgroundColor: rows.map(function (r) { return r[2]; }),
          borderRadius: 4,
          borderSkipped: "start",
          maxBarThickness: 20
        }]
      },
      options: {
        indexAxis: "y",
        scales: { x: axisCount(), y: axisX({ ticks: { color: INK2, autoSkip: false } }) },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function (it) { return " " + plural(it.parsed.x, "RMA"); } } }
        }
      }
    });
  })();
})();
