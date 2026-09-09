(function () {
  function readPayload(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent); } catch (error) { return null; }
  }

  function valueLabel(value) {
    return value === null || value === undefined ? "暂无" : value;
  }

  function tooltipFormatter(params) {
    var items = Array.isArray(params) ? params : [params];
    if (!items.length) return "";
    var axisLabel = items[0].axisValueLabel || items[0].axisValue || "";
    var lines = items.map(function (item) {
      var data = item.data && typeof item.data === "object" ? item.data : {};
      return (item.marker || "") + item.seriesName + "：" + valueLabel(data.display || item.value);
    });
    return axisLabel ? axisLabel + "<br>" + lines.join("<br>") : lines.join("<br>");
  }

  function axisFormatter(payload) {
    return function (value) {
      var text = Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
      if (payload.unit === "RATIO") return text + "%";
      if (payload.unit === "MONEY") return text + " " + (payload.unit_label || "元");
      if (String(payload.unit).toLowerCase() === "wan") return text + " 万元";
      return text;
    };
  }

  function kindColor(kind) {
    if (kind === "BUDGET") return "#c98627";
    if (kind === "FORECAST") return "#4479bd";
    return "#8094a8";
  }

  function plotData(values, displays) {
    return (values || []).map(function (value, index) {
      return { value: value, display: displays && displays[index] ? displays[index] : valueLabel(value) };
    });
  }

  function renderTrend(payload) {
    if (!payload || !window.echarts) return;
    var trendNode = document.getElementById("cockpit-trend-chart");
    if (trendNode && payload.series && payload.series.length) {
      var chart = window.echarts.init(trendNode);
      chart.setOption({
        color: payload.series.map(function (item) { return kindColor(item.kind); }),
        tooltip: { trigger: "axis", formatter: tooltipFormatter },
        legend: { type: "scroll", top: 0 },
        grid: { left: 28, right: 22, top: 42, bottom: 22, containLabel: true },
        xAxis: { type: "category", boundaryGap: false, data: payload.months || [] },
        yAxis: { type: "value", axisLabel: { formatter: axisFormatter(payload) }, splitLine: { lineStyle: { color: "#eceeef" } } },
        series: payload.series.map(function (item, index) {
          return { name: item.label || item.name, type: "line", connectNulls: false, data: plotData(item.chart_values || item.values, item.display_values), smooth: true, symbolSize: 6, itemStyle: { color: kindColor(item.kind) }, lineStyle: { color: kindColor(item.kind), width: index < 2 ? 2 : 1.5 } };
        })
      });
      window.addEventListener("resize", function () { chart.resize(); });
    }
    var annualNode = document.getElementById("cockpit-annual-chart");
    if (annualNode && payload.annual && payload.annual.length) {
      var annual = window.echarts.init(annualNode);
      annual.setOption({
        tooltip: { trigger: "axis", formatter: tooltipFormatter },
        grid: { left: 28, right: 22, top: 24, bottom: 60, containLabel: true },
        xAxis: { type: "category", axisLabel: { rotate: 24 }, data: payload.annual.map(function (item) { return item.label; }) },
        yAxis: { type: "value", axisLabel: { formatter: axisFormatter(payload) } },
        series: [{ name: payload.metric_label || payload.metric, type: "bar", barMaxWidth: 42, data: payload.annual.map(function (item) { return { value: item.chart_value === undefined ? item.value : item.chart_value, display: item.display || valueLabel(item.value), itemStyle: { color: kindColor(item.kind) } }; }) }]
      });
      window.addEventListener("resize", function () { annual.resize(); });
    }
  }

  function renderScenario() {
    var node = document.getElementById("scenario-monthly-chart");
    var monthly = readPayload("scenario-monthly-data");
    if (!node || !window.echarts || !monthly || !monthly.length) return;
    var original = monthly.map(function (item) { return Number(item.room_rev_before_cents || 0) / 1000000; });
    var adjusted = monthly.map(function (item) { return Number(item.room_rev_after_cents || 0) / 1000000; });
    var nonzero = original.concat(adjusted).filter(function (value) { return Number.isFinite(value) && value !== 0; });
    var axisRange = {};
    if (nonzero.length) {
      var low = Math.min.apply(null, nonzero);
      var high = Math.max.apply(null, nonzero);
      var spread = high - low;
      var padding = spread > 0 ? spread * 0.12 : Math.max(Math.abs(high) * 0.08, 1);
      axisRange.min = Math.max(0, Math.floor((low - padding) * 100) / 100);
      axisRange.max = Math.ceil((high + padding) * 100) / 100;
    }
    var chart = window.echarts.init(node);
    chart.setOption({
      color: ["#4479bd", "#c98627"], tooltip: { trigger: "axis" }, legend: { top: 0, data: ["原预算", "调整后预算"] },
      grid: { left: 20, right: 18, top: 34, bottom: 16, containLabel: true },
      xAxis: { type: "category", boundaryGap: false, data: monthly.map(function (item) { return Number(item.period) + "月"; }) },
      yAxis: { type: "value", min: axisRange.min, max: axisRange.max, scale: true, axisLabel: { formatter: function (value) { return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 }); } }, splitLine: { lineStyle: { color: "#eceeef" } } },
      series: [
        { name: "原预算", type: "line", smooth: false, symbolSize: 5, data: original, itemStyle: { color: "#4479bd" }, lineStyle: { color: "#4479bd", width: 2 } },
        { name: "调整后预算", type: "line", smooth: false, symbolSize: 5, data: adjusted, itemStyle: { color: "#c98627" }, lineStyle: { color: "#c98627", width: 2 } }
      ]
    });
    window.addEventListener("resize", function () { chart.resize(); });
  }

  function setupScenarioDriverFields() {
    var driver = document.getElementById("id_driver");
    if (!driver) return;
    var groups = document.querySelectorAll("[data-scenario-driver]");
    function refresh() {
      var roomRevenue = driver.value === "ROOM_REV";
      groups.forEach(function (group) {
        var visible = group.getAttribute("data-scenario-driver") === (roomRevenue ? "room-rev" : "standard");
        group.hidden = !visible;
        group.querySelectorAll("input, select, textarea").forEach(function (control) { control.disabled = !visible; });
        if (!visible && group.tagName === "DETAILS") group.open = false;
      });
    }
    driver.addEventListener("change", refresh);
    refresh();
  }

  renderTrend(readPayload("cockpit-trend-data") || readPayload("cockpit-dashboard-data"));
  renderScenario();
  setupScenarioDriverFields();
}());
