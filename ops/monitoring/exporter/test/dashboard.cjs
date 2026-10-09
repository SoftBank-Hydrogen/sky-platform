'use strict';
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../');
const dashboard=JSON.parse(fs.readFileSync(path.join(root,'grafana/dashboards/sky-game.json'),'utf8'));
const sourceFile=fs.readFileSync(path.join(root,'grafana/provisioning/datasources/datasources.yml'),'utf8');
const ids=new Set();
for(const panel of dashboard.panels) {
  assert.ok(!ids.has(panel.id),'duplicate panel ID');ids.add(panel.id);
  assert.ok(sourceFile.includes(`uid: ${panel.datasource.uid}`),'data source not provisioned');
  for(const query of panel.targets) {
    if(panel.datasource.type==='prometheus') assert.ok(query.expr,'missing PromQL');
    else assert.ok(query.metricName && query.namespace && query.dimensions,'missing AWS metric fields');
  }
}
assert.ok(dashboard.panels.some(panel=>panel.targets.some(query=>query.expr?.includes('sky_observed_collection_error'))), 'failure reason panel missing');
console.log('Dashboard panel IDs, data sources and query structure validated');
