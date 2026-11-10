"use strict";
const {test}=require("node:test");
const assert=require("node:assert/strict");
const path=require("node:path");
const {pathToFileURL}=require("node:url");
const url=pathToFileURL(path.join(__dirname,"../static/song-requests.js")).href;

function element(){
  const el={value:"",textContent:"",hidden:false,children:[],listeners:{},attrs:{},disabled:false,src:""};
  const classes=new Set();
  el.classList={add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n),toggle(n,flag){if(flag)classes.add(n);else classes.delete(n);}};
  el.addEventListener=(name,fn)=>{el.listeners[name]=fn;};
  el.setAttribute=(name,value)=>{el.attrs[name]=value;};
  el.replaceChildren=(...children)=>{el.children=[...children];};
  el.append=(...children)=>{el.children.push(...children);};
  el.querySelector=(selector)=>selector===".netease-load-more"?el.children.find(x=>x.className?.includes("netease-load-more"))??null:null;
  el.remove=()=>{};
  return el;
}
async function harness(){
  const original={document:global.document,fetch:global.fetch,
    setTimeout:global.setTimeout,clearTimeout:global.clearTimeout};
  const nodes=new Map(),calls=[],pickers=[];
  const byId=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  let rejectOrder=false;
  global.document={getElementById:byId,createElement:element};
  global.setTimeout=()=>1;global.clearTimeout=()=>{};
  global.fetch=async (uri,opts={})=>{
    calls.push({uri,opts});
    if(uri==="/api/netease/songs"){
      if(rejectOrder)return {ok:false,status:409,json:async()=>({detail:"submission failed"})};
      return {ok:true,json:async()=>({id:3,title:"Track",lyric_query:"Track"})};
    }
    if(uri.startsWith("/api/netease/preview"))return {ok:true,json:async()=>({source_id:"7788",title:"Track",uploader:"Artist",duration:90})};
    if(uri.startsWith("/api/netease/search"))return {ok:true,json:async()=>({songs:[{source_id:"7788",title:"Track",uploader:"Artist",duration:90}]})};
    return {ok:true,json:async()=>({id:5,title:"B Track",lyric_query:"B Track"})};
  };
  const {setupSongRequests}=await import(url);
  let refreshes=0;
  setupSongRequests({refresh:async()=>{refreshes++;},openLyricPicker:async(...args)=>pickers.push(args)});
  return {byId,calls,pickers,get refreshes(){return refreshes;},set rejectOrder(value){rejectOrder=value;},
    cleanup(){Object.assign(global,original);}};
}
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));};
async function search(h,query){
  h.byId("netease-song-query").value=query;
  h.byId("netease-search-form").listeners.submit({preventDefault(){}});
  await flush();
  return h.byId("netease-search-results").children[0];
}
test("NetEase successful song order clears the long search input and stale results",async()=>{
  const h=await harness();
  try{
    const item=await search(h,"https://music.163.com/song?id=7788&long="+"a".repeat(700));
    assert.ok(item);
    assert.equal(h.byId("netease-search-results").children.length,1);
    await item.children.at(-1).onclick();
    assert.equal(h.byId("netease-song-query").value,"");
    assert.equal(h.byId("netease-search-results").children.length,0);
    assert.match(h.byId("netease-search-message").textContent,/继续搜索下一首/);
    assert.equal(h.refreshes,1);
    assert.deepEqual(h.pickers,[[3,"Track","Track"]]);
  }finally{h.cleanup();}
});
test("NetEase rejected order preserves search input and results to retry",async()=>{
  const h=await harness();
  try{
    const item=await search(h,"Track");
    h.rejectOrder=true;
    await item.children.at(-1).onclick();
    assert.equal(h.byId("netease-song-query").value,"Track");
    assert.equal(h.byId("netease-search-results").children.length,1);
    assert.equal(h.refreshes,0);
    assert.equal(h.pickers.length,0);
  }finally{h.cleanup();}
});
test("Bilibili both quick and preview submissions clear their own input",async()=>{
  const h=await harness();
  try{
    h.byId("bvid").value="BV1234567890";
    await h.byId("quick-submit-btn").onclick();
    assert.equal(h.byId("bvid").value,"");
    h.byId("bvid").value="BV9876543210";
    await h.byId("preview-btn").onclick();
    await h.byId("submit-btn").onclick();
    assert.equal(h.byId("bvid").value,"");
    assert.equal(h.refreshes,2);
  }finally{h.cleanup();}
});
