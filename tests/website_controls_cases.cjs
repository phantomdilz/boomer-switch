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
  contains(node) { return this===node || this.children.some(child=>child.contains(node)); }
  focus() { this.focused=true; }
  scrollIntoView() { this.scrolled=true; }
  async click() { if(!this.disabled) for(const fn of this.listeners.click??[]) await fn({preventDefault(){}}); }
}
async function fixture(access={status:"approved",home_ready:true,home_status:"ready",lights:catalog,csrf:"synthetic"}, automatic=false) {
  const roots=[];
  for(const id of ["status","details","verification","controls","connection","rooms","room-navigation","room-picker","retry-access","entry-recovery","name","email","code","resend"]) {
    const element=new Element(["details","verification"].includes(id)?"form":"div");element.id=id;roots.push(element);
  }
  const all=()=>{const out=[];const walk=node=>{out.push(node);node.children.forEach(walk);};roots.forEach(walk);return out;};
  const fieldsets=[new Element("fieldset"),new Element("fieldset")];
  const document={hidden:false,listeners:{},createElement:tag=>new Element(tag),
    querySelector:selector=>all().find(node=>node.id===selector.slice(1))??null,
    getElementById:id=>all().find(node=>node.id===id)??null,
    querySelectorAll:selector=>selector==="form fieldset"?fieldsets:[],
    addEventListener(name,fn){this.listeners[name]=fn;}};
  const f={now:100000,access,posts:[],autoPosts:[],requests:[],states:{desk:"off",window:"off",hall:"off"},reports:{},good:{},performReads:true,result:null,document,generation:1};
  f.sample=(id,state=f.states[id],availability="online",at=f.now)=>{
    f.reports[id]={state,availability,at};if(availability==="online")f.good[id]={state,at};
  };
  f.accessResult=()=>{
    const value=structuredClone(f.access);
    if(automatic && value.status==="approved" && value.home_ready) {
      Object.assign(value,{direct_commands:true,automatic_updates:true,state_generation:f.generation,state_time:f.now/1000});
      value.states=(value.lights??[value.light]).map(item=>{
        const report=f.reports[item.light_id],good=f.good[item.light_id];const age=report?f.now-report.at:null;
        return {light_id:item.light_id,state:report?.state??"unavailable",availability:report?.availability==="online"&&age>=45000?"stale":report?.availability??"unchecked",
          age_ms:age,sample:report?report.at/1000:null,last_confirmed:good?{state:good.state,age_ms:f.now-good.at}:null};
      });
    }
    return value;
  };
  let sequence=0,interval;
  const operations=new Map();
  const fetch=async(path,options)=>{
    const body=options.body?JSON.parse(options.body):null;f.requests.push({path,body});
    let value;
    if(path==="api/access") value=f.accessResult();
    else if(path==="api/lights") {
      if(body.background) {
        f.autoPosts.push({body,at:f.now});
        if(f.autoHook)value=await f.autoHook(body);
        else {
          if(f.performReads) {
            const ids=(f.access.lights??[f.access.light]).map(item=>item.light_id).filter(id=>!f.reports[id]||f.now-f.reports[id].at>=10000);
            ids.sort((a,b)=>(f.reports[a]?.at??0)-(f.reports[b]?.at??0)||a.localeCompare(b));
            if(ids.length)f.sample(ids[0]);
          }
          value=f.accessResult();
        }
      } else {
        f.posts.push(body);
        if(f.loseAck)throw Error("Synthetic acknowledgement lost before acceptance");
        operations.set(body.operation_id,body);value={operation_id:body.operation_id,status:"queued"};
      }
    } else if(path.startsWith("api/operations/")) {
      const op=operations.get(path.split("/").at(-1));assert.ok(op,"lookup uses its accepted UUID");
      const id=op.light_id??f.access.light?.light_id;
      value=f.result?await f.result(op):null;
      if(value===null) {
        if(op.action!=="read") f.states[id]=op.action;
        if(automatic)f.sample(id);
        value={status:"confirmed",state:f.states[id],availability:"online",light_id:id};
        if(f.access.light&&!f.access.lights) { delete value.availability;delete value.light_id; }
      }
      value={operation_id:op.operation_id,...value};
    } else throw Error("Unexpected endpoint: "+path);
    return {ok:true,json:async()=>value};
  };
  const entry={canSubmit:true,recoveryHint:false,bootstrap:async(fn,path)=>fn(path),invalidate(){this.canSubmit=false;}};
  class Clock extends Date { static now(){return f.now;} }
  const window={HouseLightsEntry:entry,innerHeight:740,listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
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
test("a server without automatic-update capability keeps the explicit-read fallback",async()=>{
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
test("direct command capability skips redundant reads but keeps unknown recovery explicit",async()=>{
  const f=await fixture({status:"approved",home_ready:true,lights:catalog,direct_commands:true});
  await f.light("desk").on.click();
  assert.deepEqual(f.posts.map(p=>[p.action,p.light_id]),[["on","desk"]]);
  assert.equal(f.light("desk").stateLine.textContent,"Checking","never show optimistic On");
  await f.tick();assert.equal(f.light("desk").stateLine.textContent,"On");
  for(let i=0;i<9;i++)await f.tick();
  assert.equal(f.posts.length,1,"no automatic reads after state expires");
  f.result=async op=>({status:"unknown",state:"unavailable",availability:"unknown",light_id:op.light_id});
  await f.light("desk").off.click();await f.tick();
  assert.deepEqual(f.posts.map(p=>p.action),["on","off"]);
  assert.equal(f.light("desk").stateLine.textContent,"Unknown");assert.ok(f.light("desk").on.disabled);
  await f.light("desk").on.click();assert.equal(f.posts.length,2,"unknown result never causes automatic retry");
  f.result=null;await f.light("desk").refresh.click();await f.tick();
  assert.deepEqual(f.posts.map(p=>p.action),["on","off","read"]);
  assert.equal(f.light("desk").on.disabled,false);
});
test("only an explicit direct capability bypasses the legacy preread",async()=>{
  for(const flag of [undefined,false,"true"]) {
    const f=await fixture({status:"approved",home_ready:true,lights:catalog,direct_commands:flag});
    await f.light("desk").on.click();assert.deepEqual(f.posts.map(p=>p.action),["read"]);
  }
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

test("automatic updates read serially, show external changes and retain age labels",async()=>{
  const f=await fixture(undefined,true);
  for(let i=0;i<7;i++)await f.tick();
  assert.equal(f.posts.length,0,"automatic work never submits On or Off");
  for(const item of catalog)assert.equal(f.light(item.light_id).stateLine.textContent,"Off");
  assert.ok(f.autoPosts.every(({body})=>body.action==="read"&&body.background===true));
  assert.ok(f.autoPosts.every((item,i)=>!i||item.at-f.autoPosts[i-1].at>=2000));
  f.states.desk="on";
  for(let i=0;i<10;i++)await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"On");
  f.performReads=false;for(let i=0;i<50;i++)await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"On","old reports keep the last actual state legible");
  assert.equal(f.light("desk").stateLine.dataset.state,"stale");
  assert.match(f.light("desk").detail.textContent,/Last confirmed: On .* ago.*Stale/);
  f.sample("desk","unavailable","unavailable");await f.tick(2);
  assert.equal(f.light("desk").stateLine.textContent,"Unavailable");
  assert.match(f.light("desk").detail.textContent,/Last confirmed: On/);
});

test("automatic HTTP never blocks a tap or paints its old reply over a command",async()=>{
  const f=await fixture(undefined,true);let release;
  f.autoHook=()=>new Promise(resolve=>{release=()=>resolve(f.accessResult());});
  await f.tick(2);assert.equal(typeof release,"function");
  assert.equal(f.light("desk").on.disabled,false);
  await f.light("desk").on.click();
  assert.deepEqual(f.posts.map(p=>[p.action,p.light_id]),[["on","desk"]]);
  release();await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"On");
  assert.equal(f.posts.length,1);
});

test("ambiguous dispatch stays unknown until evidence is newer than its recovery barrier",async()=>{
  const f=await fixture(undefined,true);await f.tick(2);f.performReads=false;f.loseAck=true;
  await f.light("desk").on.click();assert.equal(f.light("desk").stateLine.textContent,"Unknown");
  for(let i=0;i<6;i++)await f.tick();
  assert.equal(f.light("desk").stateLine.textContent,"Unknown","an old cached Off cannot clear ambiguity");
  assert.equal(f.light("desk").on.disabled,true);assert.equal(f.posts.length,1);
  f.sample("desk","off","online",f.eval('lights.get("desk").recoveryBarrier')*1000);await f.tick(2);
  assert.equal(f.light("desk").stateLine.textContent,"Unknown","an equal clock sample is not newer evidence");
  f.sample("desk","off");await f.tick(2);
  assert.equal(f.light("desk").stateLine.textContent,"Off");assert.equal(f.light("desk").on.disabled,false);
  assert.equal(f.posts.length,1,"recovery never retries the uncertain On");
});

test("automatic updates stop hidden or offsite, and late responses cannot restore controls",async()=>{
  const f=await fixture(undefined,true);await f.tick(2);
  f.document.hidden=true;f.document.listeners.visibilitychange();const count=f.autoPosts.length;
  for(let i=0;i<10;i++)await f.tick();
  assert.equal(f.autoPosts.length,count);assert.equal(f.document.querySelector("#rooms").children.length,0);
  f.document.hidden=false;f.document.listeners.visibilitychange();await f.tick(3);
  f.access={status:"approved",home_ready:false,home_status:"source_mismatch"};await f.tick(3);
  assert.equal(f.document.querySelector("#controls").hidden,true);assert.equal(f.document.querySelector("#rooms").children.length,0);
  const stopped=f.autoPosts.length;await f.tick(10);assert.equal(f.autoPosts.length,stopped);
});

test("room visibility hints and updates preserve picker focus and catalog identity",async()=>{
  const f=await fixture(undefined,true);const picker=f.document.querySelector("#room-picker");
  const heading=f.document.getElementById("light-room-1");picker.value=heading.id;picker.listeners.change[0]();
  assert.equal(heading.focused,true);
  for(const id of ["desk","window"])f.light(id).card.getBoundingClientRect=()=>({top:100,bottom:300});
  const card=f.light("desk").card;await f.tick(2);
  assert.deepEqual([...f.autoPosts[0].body.preferred].sort(),["desk","window"]);
  assert.equal(f.light("desk").card,card);assert.equal(heading.focused,true);
  assert.equal(f.document.querySelector("#room-navigation").hidden,false);
});
