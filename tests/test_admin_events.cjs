"use strict";

const {test} = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {pathToFileURL} = require("node:url");

// Real browser modules, loaded without npm or a bundler.
const moduleUrl = pathToFileURL(path.join(__dirname, "../static/admin-events.js")).href;

function element(tag="div"){
  const attrs=new Map();
  const node={tagName:tag.toUpperCase(),children:[],textContent:"",className:"",value:"",open:false,
    disabled:false,checked:false,events:{}};
  const classes=()=>new Set(node.className.split(/\s+/).filter(Boolean));
  node.classList={
    contains(name){return classes().has(name);},
    add(name){const next=classes();next.add(name);node.className=[...next].join(" ");},
    remove(name){const next=classes();next.delete(name);node.className=[...next].join(" ");},
    toggle(name,force){const next=classes();const enable=force??!next.has(name);
      if(enable)next.add(name);else next.delete(name);node.className=[...next].join(" ");return enable;}
  };
  node.append=(...children)=>node.children.push(...children);
  node.replaceChildren=(...children)=>{node.children=[...children];};
  node.addEventListener=(type,callback)=>{node.events[type]=callback;};
  node.getAttribute=(name)=>attrs.get(name)??null;
  node.setAttribute=(name,value)=>attrs.set(name,String(value));
  node.removeAttribute=(name)=>attrs.delete(name);
  return node;
}

async function harness(){
  const previous={document:global.document,fetch:global.fetch,location:global.location,
    setTimeout:global.setTimeout,clearTimeout:global.clearTimeout,
    confirm:global.confirm,prompt:global.prompt};
  const nodes=new Map();
  const byId=id=>{
    if(!nodes.has(id))nodes.set(id,element());
    return nodes.get(id);
  };
  const links=new Map();
  let route="settings-event";
  let eventChanges=0,refreshes=0,queueInvalidations=0;
  const accounts=[];
  const calls=[];
  const active={id:7,name:"新场次",code:"123456",status:"open",legacy:false};
  const closed={id:4,name:"旧场次",status:"closed",legacy:false,media_released_at:null};
  const state={role:"global",username:"admin",active:7,events:[active,closed],
    songs:[{id:8,title:"演唱记录",requester:"Alice",uploader:"歌手",status:"played"}],
    account:{name:null,role:null},confirm:true,prompt:"旧场次"};
  const settings={queue_policy:"fair",subtitle_style:"default",video_quality:720,
    allow_unsubtitled:true,max_bandwidth_mbps:4,orders_paused:false,
    tv_html_overlay:false,force_h264_originals:false};
  global.document={getElementById:byId,createElement:element,
    querySelector:selector=>{
      if(!links.has(selector))links.set(selector,element("a"));
      return links.get(selector);
    },activeElement:null};
  global.location={hash:""};
  global.setTimeout=()=>42;
  global.clearTimeout=()=>{};
  global.confirm=()=>state.confirm;
  global.prompt=()=>state.prompt;
  global.fetch=async (url,opts)=>{
    const method=opts?.method??"GET";
    calls.push([method,url]);
    let result={ok:true};
    if(url==="/api/admin/events"&&method==="GET")result={
      role:state.role,username:state.username,active:state.active,events:state.events
    };
    else if(url==="/api/admin/settings"&&method==="GET")result=settings;
    else if(url==="/api/admin/accounts")result={accounts};
    else if(url==="/api/admin/netease/status")result={connected:false,pending:false};
    else if(url.endsWith("/songs")&&method==="GET")result={songs:state.songs};
    else if(url.endsWith("/release-media")){
      const event=state.events.find(e=>e.id===Number(url.split("/")[4]));
      event.media_released_at=1234;
      result={freed_bytes:2*1048576};
    }else if(url==="/api/admin/events"&&method==="POST"){
      const {name,code}=JSON.parse(opts.body);
      state.events.push({id:9,name,code,status:"open",legacy:false});
      state.active=9;
    }else if(url==="/api/admin/events/close"){
      const event=state.events.find(e=>e.id===state.active);
      event.status="closed";state.active=null;
    }else if(method==="DELETE"){
      const id=Number(url.split("/")[4]);
      state.events=state.events.filter(e=>e.id!==id);
    }
    return {ok:true,async json(){return result;}};
  };
  const {setupAdminEvents}=await import(moduleUrl);
  const {refreshEvents,leaveNeteasePage}=setupAdminEvents({
    refresh:async()=>{refreshes++;},
    currentRoute:()=>route,
    onEventChanged:()=>{eventChanges++;},
    updateAccount:(_,name,role)=>{state.account={name,role};},
    addSourceTag:(parent)=>parent.append(element("source")),
    songState:(song)=>song.status==="played"?"已唱":"待播",
    invalidateQueueState:()=>{queueInvalidations++;}
  });
  return {nodes,byId,links,state,accounts,calls,active,closed,refreshEvents,leaveNeteasePage,
    setRoute(value){route=value;},
    get eventChanges(){return eventChanges;},get refreshes(){return refreshes;},
    get queueInvalidations(){return queueInvalidations;},
    cleanup(){Object.assign(global,previous);}};
}

// Tests use one browser-global harness at a time, as in a single actual tab.
test("event state updates its panel, account, and cache only when active event changes",async()=>{
  const h=await harness();
  try{
    await h.refreshEvents();
    assert.equal(h.byId("event-status").textContent,"♫ 新场次");
    assert.equal(h.byId("event-join-code").textContent,"加入码  123456");
    assert.deepEqual(h.state.account,{name:"管理员",role:"全局管理员"});
    assert.equal(h.eventChanges,1);
    const first=h.byId("event-history").children[0];
    await h.refreshEvents();
    assert.equal(h.eventChanges,1);
    assert.equal(h.byId("event-history").children[0],first,"unchanged list keeps its DOM");
    h.state.active=4;
    await h.refreshEvents();
    assert.equal(h.eventChanges,2);
    assert.equal(h.byId("event-status").textContent,"♫ 旧场次");
    assert.equal(h.byId("event-history").children[0],first,"switching event does not rebuild unchanged history");
  }finally{h.cleanup();}
});

test("global admin can view archive songs, export CSV, release media, and delete records",async()=>{
  const h=await harness();
  try{
    await h.refreshEvents();
    let row=h.byId("event-history").children[1];
    let [info,actions,details]=row.children;
    assert.equal(info.children[0].textContent,"旧场次");
    assert.equal(actions.children.length,4);
    assert.equal(actions.children[0].href,"/api/admin/events/4/songs.csv");
    await actions.children[1].onclick();
    assert.equal(details.classList.contains("hidden"),false);
    assert.equal(details.children[0].children[1].children[0].textContent,"演唱记录");
    assert.equal(details.children[0].children[2].textContent,"已唱");
    await actions.children[1].onclick();
    assert.equal(details.classList.contains("hidden"),true);
    await actions.children[2].onclick();
    row=h.byId("event-history").children[1];
    actions=row.children[1];
    assert.equal(row.children[0].children[1].textContent,"已释放媒体");
    assert.equal(actions.children.length,3,"released archives hide release button");
    assert.ok(h.calls.some(([m,url])=>m==="POST"&&url.endsWith("/4/release-media")));
    await actions.children[2].onclick();
    assert.equal(h.byId("event-history").children.length,1);
    assert.ok(h.calls.some(([m,url])=>m==="DELETE"&&url==="/api/admin/events/4"));
  }finally{h.cleanup();}
});

test("event admin has no global-only archive, accounts or NetEase controls",async()=>{
  const h=await harness();
  try{
    h.state.role="event";h.state.username="operator";h.setRoute("settings-netease");
    await h.refreshEvents();
    assert.equal(global.location.hash,"settings");
    assert.equal(h.byId("admin-role").textContent,"本场管理员：operator");
    assert.equal(h.byId("admin-accounts-panel").classList.contains("hidden"),true);
    assert.equal(h.byId("netease-admin-panel").classList.contains("hidden"),true);
    assert.equal(h.links.get('a[href="#settings-accounts"]').classList.contains("hidden"),true);
    assert.equal(h.links.get('a[href="#settings-netease"]').classList.contains("hidden"),true);
    assert.equal(h.byId("event-history").children[1].children[1].children.length,2);
    assert.ok(!h.calls.some(([,url])=>url==="/api/admin/netease/status"));
  }finally{h.cleanup();}
});

test("NetEase login page is fetched once per visit and reset on navigation",async()=>{
  const h=await harness();
  try{
    h.setRoute("settings-netease");
    await h.refreshEvents();await h.refreshEvents();
    const statusCalls=()=>h.calls.filter(([,url])=>url==="/api/admin/netease/status").length;
    assert.equal(statusCalls(),1);
    h.leaveNeteasePage();
    await h.refreshEvents();
    assert.equal(statusCalls(),2);
  }finally{h.cleanup();}
});

test("event create, close and queue settings retain their original handlers",async()=>{
  const h=await harness();
  try{
    h.byId("event-name").value="今晚";
    h.byId("event-code").value="654321";
    await h.byId("event-create-btn").onclick();
    assert.equal(h.state.active,9);
    assert.equal(h.refreshes,1);
    assert.equal(h.byId("event-name").value,"");
    await h.byId("event-close-btn").onclick();
    assert.equal(h.state.active,null);
    assert.equal(h.refreshes,2);
    await h.byId("queue-settings-save").onclick();
    assert.equal(h.queueInvalidations,1);
    assert.equal(h.refreshes,3);
    assert.ok(h.calls.some(([m,url])=>m==="PUT"&&url==="/api/admin/settings"));
  }finally{h.cleanup();}
});
