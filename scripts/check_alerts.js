// Loads the real dashboard in headless Chromium and reads its own ZONE_LOG,
// so alert logic = exactly what the page shows (no duplicated threshold code).
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto('http://localhost:8000/index.html');
  await p.waitForTimeout(4000);
  const log = await p.evaluate(() => ZONE_LOG.map(r => ({id:r.id,label:r.label,zone:r.zone,value:r.value,unit:r.unit,tier:r.tier})));
  await b.close();
  const prev = fs.existsSync('alert_state.json') ? JSON.parse(fs.readFileSync('alert_state.json')) : null;
  const cur = {}; log.forEach(r => { if (r.zone && ['live-api','event-driven'].includes(r.tier)) cur[r.label] = r.zone; });
  const rank = {normal:0, watch:1, danger:2};
  const lines = [];
  if (prev) for (const r of log) {
    if (cur[r.label] === 'danger' && prev[r.label] !== 'danger')
      lines.push(`- **${r.label}**: ${(prev[r.label]||'new').toUpperCase()} → **DANGER** (value ${r.value}${r.unit||''})`);
  }
  // keep prior zone if an indicator is temporarily missing, so a data gap doesn't re-fire later
  const next = Object.assign({}, prev || {}, cur);
  fs.writeFileSync('alert_state.json', JSON.stringify(next, null, 1));
  fs.writeFileSync('alert_body.md', lines.length
    ? `New DANGER readings (zone logic = dashboard bands, analyst-proposed starting points):\n\n${lines.join('\n')}\n\nDashboard: https://joshlimhx.github.io/Josh/\n`
    : '');
  console.log(prev ? `checked ${Object.keys(cur).length}, new danger: ${lines.length}` : `baseline saved (${Object.keys(cur).length} indicators), no alert on first run`);
})();
