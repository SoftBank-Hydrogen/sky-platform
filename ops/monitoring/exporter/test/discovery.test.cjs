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
