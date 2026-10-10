import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
const html=readFileSync(new URL('../../src/assets/static/index.html',import.meta.url),'utf8');
function context(api){
 const elements={};
 const element=()=>({textContent:'',hidden:false,disabled:false,children:[],replaceChildren(){this.children=[];},append(child){this.children.push(child);}});
 const c={api,historyItems:[],historyCursor:null,historyLoading:false,historyGeneration:0,releaseApplication:null,releaseCursor:null,releaseLoading:false,releaseGeneration:0,busy:false,statuses:{},open(){},Map,Date,encodeURIComponent,
 el:id=>elements[id]||(elements[id]=element()),document:{createElement:element}};
 const start=html.indexOf('function deploymentPage('),end=html.indexOf('async function loadRollbackTargets(',start);
 runInNewContext(html.slice(start,end),c);return c;
}
test('legacy arrays and database envelopes both render; more appends and refresh resets',async()=>{
 const calls=[];let responses=[{items:[{id:'first'}],next_cursor:'next/+'},{items:[{id:'older'}],next_cursor:null},[{id:'new'}]];
 const c=context(async path=>{calls.push(path);return responses.shift();});
 await c.history();assert.equal(c.el('history').children.length,1);assert.equal(c.el('historyMore').hidden,false);
 await c.history(true);assert.equal(c.el('history').children.length,2);assert.equal(calls[1],'/api/jobs?cursor=next%2F%2B');
 assert.equal(c.el('historyMore').hidden,true);await c.history();assert.equal(c.el('history').children.length,1);
 assert.equal(c.historyItems[0].id,'new');
});
test('failed next page preserves visible rows and cursor for retry',async()=>{
 let fail=false;const c=context(async()=>{if(fail)throw Error('offline');return {items:[{id:'first'}],next_cursor:'next'};});
 await c.history();fail=true;await assert.rejects(()=>c.history(true),/offline/);
 assert.equal(c.el('history').children.length,1);assert.equal(c.historyCursor,'next');assert.equal(c.historyLoading,false);
});
test('release pagination ignores a stale response when selection changes',async()=>{
 let resolve;const c=context(path=>path.includes('app1')?new Promise(r=>resolve=r):Promise.resolve({items:[{id:'new'}],next_cursor:null}));
 const old=c.readReleases('app1');await c.readReleases('app2');resolve({items:[{id:'old'}],next_cursor:'stale'});await old;
 assert.equal(c.el('releaseHistory').children.length,1);assert.ok(c.el('releaseHistory').children[0].textContent.startsWith('new'));
 assert.equal(c.releaseApplication,'app2');assert.equal(c.releaseCursor,null);
});
test('invalid page envelopes fail without clearing visible content',async()=>{
 const c=context(async()=>({items:'wrong',next_cursor:null}));await assert.rejects(()=>c.history(),/목록 응답/);
 assert.equal(c.historyLoading,false);
});
