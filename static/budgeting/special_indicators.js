(() => {
  const source = document.getElementById('si-data');
  if (!source) return;
  const datasets = JSON.parse(source.textContent);
  const initialParams = new URLSearchParams(window.location.search);
  for (const key of ['indicator', 'scope']) {
    const select = document.getElementById(`si-${key}`);
    if (Array.from(select.options).some(option => option.value === initialParams.get(key))) {
      select.value = initialParams.get(key);
    }
  }
  const monthly = echarts.init(document.getElementById('si-monthly'));
  const annual = echarts.init(document.getElementById('si-annual'));
  const colors = ['#adb6c1', '#72879b', '#bfa06a', '#243d56'];
  function render() {
    const currentUrl = new URL(window.location.href);
    for (const key of ['indicator', 'scope', 'unit']) {
      currentUrl.searchParams.set(key, document.getElementById(`si-${key}`).value);
    }
    window.history.replaceState(null, '', currentUrl);
    const indicator = document.getElementById('si-indicator').value;
    const divisor = document.getElementById('si-unit').value === 'WAN' ? 10000 : 1;
    const unit = divisor === 1 ? '元' : '万元';
    const fmt = v => v === null || v === undefined ? '—' : (v / divisor).toLocaleString('zh-CN', {maximumFractionDigits: 2});
    const maps = datasets.map(d => new Map(d.data.rows.filter(r => r.indicator === indicator).map(r => [r.project_id, r])));
    const projects = datasets[0]?.data.projects || [];
    const common = projects.filter(p => maps.every(m => m.get(p.id)?.complete));
    const annualComplete = v => v?.annual_complete ?? v?.complete;
    const annualCommon = projects.filter(p => maps.every(m => annualComplete(m.get(p.id))));
    document.getElementById('si-coverage').textContent = `年度共同项目 ${annualCommon.length} / ${projects.length} · 月度共同项目 ${common.length} / ${projects.length} · 单位：${unit}`;
    document.getElementById('si-empty').hidden = common.length > 0;
    const strict = document.getElementById('si-scope').value === 'common';
    const annualScopes = maps.map(m => strict ? annualCommon : projects.filter(p => annualComplete(m.get(p.id))));
    const monthScopes = maps.map(m => strict ? common : projects.filter(p => m.get(p.id)?.complete));
    const sums = maps.map((m, i) => annualScopes[i].length ? annualScopes[i].reduce((n, p) => n + m.get(p.id).total, 0) : null);
    const labels = datasets.map((d, i) => `${d.label}（${annualScopes[i].length}/${projects.length}）`);
    document.getElementById('si-empty').hidden = annualScopes.some(ps => ps.length) || monthScopes.some(ps => ps.length);
    monthly.setOption({color: colors, tooltip: {trigger: 'axis'}, legend: {bottom: 0}, grid: {left: 65, right: 25, top: 30, bottom: 65}, xAxis: {type: 'category', data: Array.from({length: 12}, (_, i) => `${i+1}月`)}, yAxis: {type: 'value', name: unit}, series: datasets.map((d, i) => ({name: `${d.label}（${monthScopes[i].length}/${projects.length}）`, type: 'line', connectNulls: false, data: Array.from({length: 12}, (_, month) => monthScopes[i].length ? monthScopes[i].reduce((v, p) => v + maps[i].get(p.id).values[month], 0) / divisor : null)}))}, true);
    annual.setOption({color: colors, tooltip: {trigger: 'axis'}, grid: {left: 65, right: 25, top: 30, bottom: 60}, xAxis: {type: 'category', data: labels, axisLabel: {interval: 0, formatter: value => value.replace('（', '\n（')}}, yAxis: {type: 'value', name: unit}, series: [{type: 'bar', barMaxWidth: 48, data: sums.map((v, i) => ({value: v === null ? null : v / divisor, itemStyle: {color: colors[i]}}))}]}, true);
    const head = document.getElementById('si-head'), body = document.getElementById('si-body');
    head.replaceChildren(); body.replaceChildren();
    function row(parent, values, header = false) {const tr = document.createElement('tr'); values.forEach(value => {const cell = document.createElement(header ? 'th' : 'td'); cell.textContent = value; tr.appendChild(cell);}); parent.appendChild(tr);}
    row(head, ['项目', ...datasets.map(d => d.label), '预算较预测', '完整期间'], true);
    for (const p of projects) {
      const values = maps.map(m => m.get(p.id));
      const base = values[2], budget = values[3];
      const delta = annualComplete(base) && annualComplete(budget) && base.total !== 0 ? `${((budget.total / base.total - 1) * 100).toFixed(1)}%` : '—';
      row(body, [p.name, ...values.map(v => annualComplete(v) ? fmt(v.total) : '—'), delta, `${values.filter(annualComplete).length} / 4`]);
    }
    const monthHead = document.getElementById('si-month-head'), monthBody = document.getElementById('si-month-body');
    monthHead.replaceChildren(); monthBody.replaceChildren();
    row(monthHead, ['项目 / 期间', ...Array.from({length: 12}, (_, i) => `${i + 1}月`), '来源'], true);
    for (const p of projects) datasets.forEach((d, i) => {
      const v = maps[i].get(p.id);
      row(monthBody, [`${p.name} · ${d.label}`, ...Array.from({length: 12}, (_, m) => fmt(v?.values[m])), v?.source_message || (v?.source === 'special_indicator_template' ? '专项指标上传模板' : '未读取到数据')]);
    });
    const samePair = annualScopes[2].map(p => p.id).join(',') === annualScopes[3].map(p => p.id).join(',');
    row(body, [strict ? `年度共同项目合计（${annualCommon.length}）` : '各期已读取项目合计', ...sums.map(fmt), samePair && sums[2] && sums[3] !== null ? `${((sums[3] / sums[2] - 1) * 100).toFixed(1)}%` : '—', strict ? '同范围' : '见图例覆盖数']);
  }
  document.getElementById('si-scope').addEventListener('change', render);
  document.getElementById('si-indicator').addEventListener('change', render);
  document.getElementById('si-unit').addEventListener('change', render);
  window.addEventListener('resize', () => {monthly.resize(); annual.resize();});
  render();
})();
