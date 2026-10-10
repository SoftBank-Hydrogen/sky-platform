'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {discover,validateDiscovery}=require('../discovery.cjs');
const source={name:'sky',kind:'sky',discovery:{allowedHosts:['*.example.test'],applicationIds:['tug-demo']}};
function job(id,url) {return {id,status:'succeeded',deployment_state:'active',application_id:'tug-demo',target:'aws-ecs-express',
 result:{url},application_ir:{hypotheses:[{kind:'sky-probe-protocol'}]}};}
test('discover and remove only active completed game deployments; stable name on redeploy',async()=>{
 const before=job('a'.repeat(16),'https://old.example.test/');
 const after=job('b'.repeat(16),'https://new.example.test/');
 const first=await discover(source,[before],async()=>before);
 const next=await discover(source,[{...before,deployment_state:'superseded'},after],async()=>after);
 assert.equal(first.targets.length,1);assert.equal(first.targets[0].name,next.targets[0].name);
 assert.equal(next.targets[0].wsUrl,'wss://new.example.test/ws');
 assert.deepEqual((await discover(source,[{...after,deployment_state:'deleted'}],async()=>{throw new Error();})).targets,[]);
});
test('reject untrusted URL and skip apps without the game protocol; never pass Sky cookies',async()=>{
 const bad=job('c'.repeat(16),'http://169.254.169.254/');
 assert.equal((await discover(source,[bad],async()=>bad)).rejected,1);
 const other={...bad,application_ir:{hypotheses:[]}};
 assert.equal((await discover(source,[other],async()=>other)).targets.length,0);
 const good=job('d'.repeat(16),'https://ok.example.test/');
 const result=await discover({...source,cookieFile:'/run/secret'},[good],async()=>good);
 assert.equal(result.targets[0].cookieFile,undefined);
});
test('discovery API failures do not produce an empty successful reconciliation',async()=>{
 const candidate=job('e'.repeat(16),'https://ok.example.test/');
 await assert.rejects(discover(source,[candidate],async()=>{throw new Error('offline');}));
 assert.throws(()=>validateDiscovery({...source,discovery:{allowedHosts:['*']}}));
});

test('non-game deployments do not consume discovery capacity; detail concurrency is bounded',async()=>{
 const many=Array.from({length:41},(_,i)=>({...job(i.toString(16).padStart(16,'0'),`https://app${i}.example.test/`),
  application_id:`app-${i}`,application_ir:{hypotheses:i===40?[{kind:'sky-probe-protocol'}]:[]}}));
 const all={...source,discovery:{allowedHosts:['*.example.test']}};
 let active=0,peak=0,loaded=0;
 const result=await discover(all,many,async id=>{
  active++;peak=Math.max(peak,active);loaded++;
  await new Promise(resolve=>setImmediate(resolve));active--;
  return many.find(item=>item.id===id);
 });
 assert.equal(result.targets.length,1);assert.equal(result.targets[0].httpUrl,'https://app40.example.test');
 assert.equal(result.rejected,0);assert.equal(loaded,41);assert.ok(peak<=20);
});
test('failure in a later detail batch aborts the entire reconciliation',async()=>{
 const many=Array.from({length:21},(_,i)=>({...job(i.toString(16).padStart(16,'0'),`https://app${i}.example.test/`),application_id:`app-${i}`}));
 const all={...source,discovery:{allowedHosts:['*.example.test']}};
 await assert.rejects(discover(all,many,async id=>{
  if(id===many[20].id)throw new Error('offline');return many.find(item=>item.id===id);
 }));
});
