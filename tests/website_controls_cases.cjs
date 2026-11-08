"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {test} = require("node:test");
const source = fs.readFileSync(process.argv[2], "utf8");
const catalog = [
  {light_id:"desk", name:"Desk lamp", room:"Study"},
  {light_id:"window", name:"Window lamp", room:"Study"},
  {light_id:"hall", name:"Hall lamp", room:"Hall"},
];
class Element {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.attributes={}; this.listeners={}; this.hidden=false; this.disabled=false; this._text=""; this.value=""; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(child=>child.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text=""; this.children=children; }
  setAttribute(name,value) { this.attributes[name]=String(value); }
  addEventListener(name,callback) { (this.listeners[name]??=[]).push(callback); }
  async click() { if(!this.disabled) for(const fn of this.listeners.click??[]) await fn({preventDefault(){}}); }
}
async function fixture(access={status:"approved",home_ready:true,home_status:"ready",lights:catalog,csrf:"synthetic"}) {
  const roots=[];
  for(const id of ["status","details","verification","controls","connection","rooms","retry-access","entry-recovery","name","email","code","resend"]) {
    const element=new Element(["details","verification"].includes(id)?"form":"div");element.id=id;roots.push(element);
  }
  const all=()=>{const out=[];const walk=node=>{out.push(node);node.children.forEach(walk);};roots.forEach(walk);return out;};
  const fieldsets=[new Element("fieldset"),new Element("fieldset")];
  const document={hidden:false,listeners:{},createElement:tag=>new Element(tag),
    querySelector:selector=>all().find(node=>node.id===selector.slice(1))??null,
    querySelectorAll:selector=>selector==="form fieldset"?fieldsets:[],
    addEventListener(name,fn){this.listeners[name]=fn;}};
  const f={now:100000,access,posts:[],requests:[],states:{desk:"off",window:"off",hall:"off"},result:null,document};
  let sequence=0,interval;
  const operations=new Map();
  const fetch=async(path,options)=>{
    const body=options.body?JSON.parse(options.body):null;f.requests.push({path,body});
    let value;
    if(path==="api/access") value=structuredClone(f.access);
    else if(path==="api/lights") {
      f.posts.push(body);operations.set(body.operation_id,body);value={operation_id:body.operation_id,status:"queued"};
    } else if(path.startsWith("api/operations/")) {
      const op=operations.get(path.split("/").at(-1));assert.ok(op,"lookup uses its accepted UUID");
      const id=op.light_id??f.access.light?.light_id;
      value=f.result?await f.result(op):null;
      if(value===null) {
        if(op.action!=="read") f.states[id]=op.action;
        value={status:"confirmed",state:f.states[id],availability:"online",light_id:id};
        if(f.access.light&&!f.access.lights) { delete value.availability;delete value.light_id; }
      }
      value={operation_id:op.operation_id,...value};
    } else throw Error("Unexpected endpoint: "+path);
    return {ok:true,json:async()=>value};
  };
  const entry={canSubmit:true,recoveryHint:false,bootstrap:async(fn,path)=>fn(path),invalidate(){this.canSubmit=false;}};
  class Clock extends Date { static now(){return f.now;} }
  const window={HouseLightsEntry:entry,listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
  const context={document,window,fetch,Date:Clock,
    crypto:{randomUUID:()=>"operation-"+(++sequence)},AbortSignal:{timeout:()=>undefined},
    setInterval(fn){interval=fn;},console};
  vm.createContext(context);vm.runInContext(source,context);
  const settle=()=>new Promise(resolve=>setImmediate(resolve));await settle();
  f.entryChange=()=>window.listeners["house-lights-entry"]();
  f.eval=code=>vm.runInContext(code,context);f.context=context;
  f.tick=async(seconds=1)=>{f.now+=seconds*1000;await interval();await settle();};
  f.light=id=>f.eval("lights.get("+JSON.stringify(id)+")");
  return f;
}
test("all authorized lights are grouped, safely labeled, and never read in the background",async()=>{
  const f=await fixture();
  assert.equal(f.eval("lights.size"),3);
  assert.equal(f.document.querySelector("#rooms").children.length,2);
  assert.deepEqual(f.document.querySelector("#rooms").children.map(n=>n.dataset.room),["Hall","Study"]);
  for(let i=0;i<15;i++)await f.tick();
  assert.equal(f.posts.length,0,"access polling never fans out device reads");
  assert.equal(f.light("desk").stateLine.textContent,"Not checked");
  const hostile=await fixture({status:"approved",home_ready:true,lights:[{light_id:"safe",name:"<img src=x>",room:"<script>"}]});
  assert.equal(hostile.light("safe").card.children[0].textContent,"<img src=x>");
  assert.equal(hostile.light("safe").card.children[0].children.length,0,"labels are text, not HTML");
});
test("one explicit On request checks only its target, then dispatches once",async()=>{
  const f=await fixture();await f.light("desk").on.click();
  assert.deepEqual(f.posts.map(p=>[p.action,p.light_id]),[["read","desk"]]);
  assert.ok(f.light("hall").refresh.disabled,"a pending request cannot silently queue another target");
  await f.tick();assert.deepEqual(f.posts.map(p=>[p.action,p.light_id]),[["read","desk"],["on","desk"]]);
  await f.tick();assert.equal(f.light("desk").stateLine.textContent,"On");
  assert.equal(f.light("hall").stateLine.textContent,"Not checked");
  for(let i=0;i<9;i++)await f.tick();assert.equal(f.posts.length,2,"idle and stale displays do not read devices");
  await f.light("desk").off.click();await f.tick();await f.tick();
  assert.deepEqual(f.posts.slice(2).map(p=>[p.action,p.light_id]),[["read","desk"],["off","desk"]]);
});
test("unavailable and unknown are independent and block follow-up commands",async()=>{
  const f=await fixture();f.result=async op=>({status:"not_started",state:"unavailable",availability:"unavailable",light_id:op.light_id});
  await f.light("window").refresh.click();await f.tick();
  assert.equal(f.light("window").stateLine.textContent,"Unavailable");assert.ok(f.light("window").on.disabled);
  assert.equal(f.light("desk").stateLine.textContent,"Not checked");assert.equal(f.light("desk").on.disabled,false);
  f.result=async op=>({status:"unknown",state:"unavailable",availability:"unknown",light_id:op.light_id});
  await f.light("desk").on.click();await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"Unknown");assert.ok(f.light("desk").on.disabled);
  assert.deepEqual(f.posts.map(p=>p.action),["read","read"],"no automatic On follows an unconfirmed read");
  f.result=null;await f.light("desk").refresh.click();await f.tick();assert.equal(f.light("desk").on.disabled,false);
});
test("a mismatched or missing multi-light receipt cannot update another card",async()=>{
  for(const wrong of ["hall",undefined]) {
    const f=await fixture();f.result=async()=>({status:"confirmed",state:"on",availability:"online",light_id:wrong});
    await f.light("desk").on.click();await f.tick();
    assert.equal(f.light("desk").stateLine.textContent,"Unknown");assert.equal(f.light("hall").stateLine.textContent,"Not checked");
    assert.equal(f.posts.length,1);
  }
});
test("another operation, late replies and targeted singleton gaps cannot authorize follow-up",async()=>{
  for(const bad of ["identity","late","singleton"]) {
    const f=await fixture(bad==="singleton"?{status:"approved",home_ready:true,lights:[catalog[0]]}:undefined);
    f.result=async op=>{
      if(bad==="late")f.now+=9000;
      return {operation_id:bad==="identity"?"another-request":op.operation_id,status:"confirmed",state:"off",light_id:op.light_id,
        availability:bad==="singleton"?undefined:"online"};
    };
    await f.light("desk").on.click();await f.tick();
    assert.equal(f.posts.length,1);assert.equal(f.light("desk").stateLine.textContent,"Unknown");
  }
});
test("offsite, denial, catalog changes and hidden tabs cancel any pending intent",async()=>{
  for(const change of ["offsite","denied","catalog","hidden","entry"]) {
    const f=await fixture();f.result=async op=>({status:"queued",light_id:op.light_id});
    await f.light("desk").on.click();
    if(change==="hidden") {f.document.hidden=true;f.document.listeners.visibilitychange();}
    else if(change==="entry")f.entryChange();
    else {
      f.access=change==="offsite"?{status:"approved",home_ready:false,home_status:"source_mismatch"}:change==="denied"?{status:"denied"}:
        {status:"approved",home_ready:true,lights:[catalog[2]]};
      await f.tick(3);
    }
    f.result=null;for(let i=0;i<4;i++)await f.tick();
    assert.equal(f.posts.length,1,"superseded read never leads to a command");
    if(!["catalog","entry"].includes(change))assert.equal(f.document.querySelector("#rooms").children.length,0);
    if(change==="offsite")assert.equal(f.document.querySelector("#connection").attributes.role,"alert");
  }
});
test("expired operations stop polling and stale confirmed receipts cannot trigger commands",async()=>{
  const f=await fixture();f.result=async op=>({status:"queued",light_id:op.light_id});
  await f.light("desk").on.click();for(let i=0;i<10;i++)await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"Unknown");
  const lookups=f.requests.filter(r=>r.path.startsWith("api/operations/")).length;
  for(let i=0;i<10;i++)await f.tick();
  assert.equal(f.requests.filter(r=>r.path.startsWith("api/operations/")).length,lookups);
  assert.equal(f.posts.length,1);
  f.result=async op=>({status:"confirmed",state:"on",availability:"unknown",light_id:op.light_id});
  await f.light("desk").refresh.click();await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"Unknown");assert.ok(f.light("desk").on.disabled);
});
test("legacy singleton metadata and receipts remain safe and usable",async()=>{
  const f=await fixture({status:"approved",home_ready:true,light:catalog[0]});
  await f.light("desk").on.click();await f.tick();await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"On");assert.ok(f.posts.every(p=>!Object.hasOwn(p,"light_id")));
});
test("invalid catalogs fail closed without rendering partial labels",async()=>{
  for(const items of [[],[catalog[0],catalog[0]],Array.from({length:33},(_,i)=>({...catalog[0],light_id:"lamp-"+i})),[{...catalog[0],room:"bad\nroom"}]]) {
    const f=await fixture({status:"approved",home_ready:true,lights:items});
    assert.equal(f.document.querySelector("#rooms").children.length,0);
    assert.equal(f.document.querySelector("#controls").hidden,true);
  }
});
