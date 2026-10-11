"""One action starts the reviewed hosted workflow and reports actual execution."""

import shutil
import subprocess

import pytest

from assets import ASSET_ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required for page workflow")
def test_one_action_upload_approval_submission_and_http_result():
    program = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8'),script=html.match(/<script>([\s\S]*)<\/script>/)[1];
assert(html.includes('app-shell'));assert(!html.includes('id="approve"'));assert(!html.includes('id="submit"'));
function setup(mode){
 const ids=['app','file','port','health','deploy','reset','message','jobSection','stage','visit','details','jobId','history'];
 const els=Object.fromEntries(ids.map(id=>[id,{value:'',disabled:false,hidden:false,textContent:'',files:[]}]));
 els.port.value='3000';els.health.value='/';els.file.files=[{name:'web.zip',size:100,lastModified:1}];
 const saved=new Map(),calls=[],timers=[];let first=true;
 const preview={id:'11111111-1111-1111-1111-111111111111',status:'ready',preview_digest:'a'.repeat(64),blockers:mode==='blocked'?[{message:'DB 연결 필요'}]:[]};
 const ctx={document:{getElementById:id=>els[id]},crypto:{randomUUID:()=> 'upload-key'},URL,
 sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
 setTimeout:fn=>timers.push(fn),fetch:async(path,options={})=>{
 calls.push({path,options});let data;
 if(path==='/api/jobs')data={items:[]};
 else if(path==='/api/deployment-previews'){
  if(mode==='retry'&&first){first=false;throw Error('upload response lost');}data=preview;
 }else if(path.endsWith('/approve'))data={id:'approval1'};
 else if(path.endsWith('/submit'))data={job_id:'a'.repeat(16)};
 else if(path.startsWith('/api/jobs/'))data={status:mode==='waiting'?'deploying':'succeeded',result:{url:mode==='unsafe'?'javascript:alert(1)':'https://example.com'}};
 else throw Error('Unexpected endpoint '+path);
 return {ok:true,headers:{get:()=> 'application/json'},json:async()=>data};}};
 vm.runInNewContext(script,ctx);return {els,saved,calls,timers};
}
(async()=>{
 const s=setup('success');await s.els.deploy.onclick();
 const write=s.calls.filter(c=>c.options.method==='POST');assert.equal(write.length,3);
 assert.equal(write[0].options.headers['X-Application-Id'],'app-web');
 assert.equal(write[1].options.body,JSON.stringify({preview_digest:'a'.repeat(64)}));
 assert.equal(write[2].options.body,JSON.stringify({request_key:'preview:11111111-1111-1111-1111-111111111111'}));
 assert.equal(s.els.stage.textContent,'배포 완료');assert.equal(s.els.visit.hidden,false);assert.equal(s.els.visit.href,'https://example.com/');
 const b=setup('blocked');await b.els.deploy.onclick();assert(b.els.message.textContent.includes('DB 연결 필요'));assert.equal(b.calls.filter(c=>c.options.method==='POST').length,1);
 const r=setup('retry');await r.els.deploy.onclick();assert.equal(r.els.message.textContent,'upload response lost');await r.els.deploy.onclick();
 assert.equal(r.calls.filter(c=>c.path==='/api/deployment-previews').length,2);
 assert(r.calls.filter(c=>c.path==='/api/deployment-previews').every(c=>c.options.headers['Idempotency-Key']==='upload-key'));
 const w=setup('waiting');await w.els.deploy.onclick();assert.equal(w.els.visit.hidden,true);assert.notEqual(w.els.stage.textContent,'배포 완료');assert.equal(w.timers.length,1);assert(w.els.deploy.disabled);
 const u=setup('unsafe');await u.els.deploy.onclick();assert.equal(u.els.visit.hidden,true);assert(!u.els.visit.href);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(
        ["node", "-e", program, str(ASSET_ROOT / "static/deployment-preparation.html")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
