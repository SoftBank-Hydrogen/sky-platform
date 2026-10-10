"""Run the preparation page workflow with a DOM/fetch double, no browser or app code."""

import shutil
import subprocess

import pytest

from assets import ASSET_ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required for page workflow")
def test_preparation_page_reuses_upload_keys_and_binds_approval_submission():
    program = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8');const script=html.match(/<script>([\s\S]*)<\/script>/)[1];
const ids=['app','file','port','health','upload','reset','message','review','summary','blockers','approve','submit','receipt'];
const els=Object.fromEntries(ids.map(id=>[id,{value:'',disabled:false,hidden:false,textContent:'',files:[]}]));
els.app.value='tug-test';els.port.value='8080';els.health.value='/health';els.file.files=[{name:'game.zip',size:123,lastModified:1}];
let calls=[],fail=true,blocked=false,next=0;const saved=new Map();
const view={id:'11111111-1111-1111-1111-111111111111',status:'ready',application_id:'tug-test',plan:{port:8080},inspection:{},blockers:[],preview_digest:'a'.repeat(64),approval_id:null};
const context={document:{getElementById:id=>els[id]},crypto:{randomUUID:()=>`upload${++next}`},
 sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
 history:{replaceState(){}},location:{search:''},URLSearchParams,
 fetch:async(path,options)=>{calls.push({path,options});if(path==='/api/deployment-previews'){
  if(fail){fail=false;throw Error('temporary failure');}return {ok:true,json:async()=>({...view,blockers:blocked?[{code:'sqlite_conversion_required'}]:[]})};}
  if(path.endsWith('/approve'))return {ok:true,json:async()=>({id:'approval1'})};
  if(path.endsWith('/submit'))return {ok:true,json:async()=>({job_id:'a'.repeat(16),operation_id:'operation1'})};throw Error('unexpected path');}};
(async()=>{vm.runInNewContext(script,context);
 await els.upload.onclick();assert.equal(els.message.textContent,'temporary failure');
 const first=calls[0].options.headers['Idempotency-Key'];assert(saved.size);
 await els.upload.onclick();assert.equal(calls[1].options.headers['Idempotency-Key'],first);assert.equal(els.approve.disabled,false);
 await els.approve.onclick();assert.equal(calls[2].options.body,JSON.stringify({preview_digest:view.preview_digest}));
 assert.equal(els.submit.disabled,false);await els.submit.onclick();
 assert.equal(calls[3].path,'/api/deployment-approvals/approval1/submit');
 assert.equal(calls[3].options.body,JSON.stringify({request_key:'preview:'+view.id}));assert(els.receipt.textContent.includes('a'.repeat(16)));
 els.reset.onclick();assert.equal(saved.size,0);blocked=true;await els.upload.onclick();
 assert.equal(els.approve.disabled,true);assert.equal(els.submit.disabled,true);assert(els.blockers.textContent.includes('sqlite_conversion_required'));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(
        ["node", "-e", program, str(ASSET_ROOT / "static/deployment-preparation.html")],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
