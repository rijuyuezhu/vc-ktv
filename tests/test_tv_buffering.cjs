"use strict";
const assert=require("node:assert/strict");
const {readFileSync}=require("node:fs");
const {runInNewContext}=require("node:vm");
const {test}=require("node:test");

function tvHarness(){
  const nodes=new Map();
  let now=0,nextTimer=1;
  const timers=new Map();
  const calls={plays:[],preloads:0,nexts:0,queue:[],current:null};
  const storage=new Map([["vcktv-smooth-buffer","1"]]);
  function el(id){
    if(!nodes.has(id)){
      const attrs=new Map();
      const classes=new Set();
      const styles=new Map();
      const element={id,
        style:{setProperty(name,value){styles.set(name,value);},getPropertyValue(name){return styles.get(name)||"";}},hidden:false,textContent:"",value:"100",paused:true,
        currentTime:0,duration:180,bufferEnd:0,error:null,
        buffered:{length:1,start:()=>0,end:()=>element.bufferEnd},
        classList:{add(name){classes.add(name);},remove(name){classes.delete(name);},contains(name){return classes.has(name);}},
        replaceChildren(){},append(){},
        addEventListener(){},matches(){return false;},
        setAttribute(k,v){attrs.set(k,v);},
        getAttribute(k){return attrs.get(k)||null;},
        hasAttribute(k){return attrs.has(k);},
        removeAttribute(k){attrs.delete(k);},
        load(){},pause(){this.paused=true;},
        async play(){this.paused=false;calls.plays.push(this.id);},
      };
      nodes.set(id,element);
    }
    return nodes.get(id);
  }
  const context={
    document:{getElementById:el,createElement:()=>({textContent:""})},location:{search:"?token=test"},
    URLSearchParams,performance:{now:()=>now},
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,String(value))},
    setTimeout:(fn,delay)=>{const id=nextTimer++;timers.set(id,{fn,at:now+delay});return id;},
    clearTimeout:(id)=>timers.delete(id),setInterval:()=>0,
    fetch:async(url)=>({ok:true,async json(){
      if(url.includes("/api/tv/next"))return {active:++calls.nexts===1?{id:1,title:"A",nickname:"guest",media_url:"/media/1",media_file:"1.mp4"}:null};
      if(url.includes("/api/tv/preload")){calls.preloads++;return {candidate:{id:2,media_url:"/media/2",media_file:"2.mp4"}};}
      if(url.includes("/api/tv/state"))return {queue:calls.queue,playback_mode:"browser",current:calls.current};
      return {ok:true};
    }}),
  };
  function evaluate(js){return runInNewContext(js,context,{filename:"tv.js"});}
  evaluate(readFileSync("static/tv.js","utf8"));
  async function advance(ms){
    const end=now+ms;
    while(true){
      const pending=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!pending)break;
      const [id,timer]=pending;timers.delete(id);now=timer.at;timer.fn();
      await Promise.resolve();
    }
    now=end;
    await Promise.resolve();
  }
  return {el,calls,storage,evaluate,advance};
}

test("new song waits for native 12-second buffer instead of starting at one frame",async()=>{
  const h=tvHarness();
  await h.el("start").onclick();
  assert.deepEqual(h.calls.plays,[]);
  h.el("video").bufferEnd=11;
  await h.advance(500);
  assert.deepEqual(h.calls.plays,[]);
  h.el("video").bufferEnd=12;
  await h.advance(250);
  assert.deepEqual(h.calls.plays,["video"]);
  await h.advance(2000);
  assert.equal(h.calls.plays.length,1);
});

test("bounded timeout allows a non-preloading browser to play",async()=>{
  const h=tvHarness();
  await h.el("start").onclick();
  await h.advance(11999);
  assert.equal(h.calls.plays.length,0);
  await h.advance(250);
  assert.deepEqual(h.calls.plays,["video"]);
});

test("manual resume bypasses buffer; a skipped track cannot play from a stale timer",async()=>{
  const h=tvHarness();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  assert.deepEqual(h.calls.plays,["video"]);
  await h.advance(13000);
  assert.equal(h.calls.plays.length,1);
  const next=tvHarness();
  await next.el("start").onclick();
  await next.evaluate("complete('skip')");
  await next.advance(13000);
  assert.equal(next.calls.plays.length,0);
});

test("preload only when current song has a reserve; cancel when it runs low",async()=>{
  const h=tvHarness();
  await h.el("start").onclick();
  h.el("video").duration=200;
  h.el("video").currentTime=100;
  h.el("video").bufferEnd=112;
  await h.evaluate("checkPreload()");
  assert.equal(h.calls.preloads,0);
  h.el("video").bufferEnd=123;
  await h.evaluate("checkPreload()");
  assert.equal(h.calls.preloads,1);
  assert.equal(h.evaluate("preloaded.id"),2);
  h.el("video").currentTime=120;
  await h.evaluate("checkPreload()");
  assert.equal(h.evaluate("preloaded"),null);
});


test("default TV mode starts immediately without extra caching delay",async()=>{
  const h=tvHarness();
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  assert.deepEqual(h.calls.plays,["video"]);
});


test("next song stays visible with empty state and highlights only when the first item changes",async()=>{
  const h=tvHarness();
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent,"暂无下一首可播放歌曲");
  assert.equal(h.el("next-up-row").classList.contains("highlight"),false);
  h.calls.queue=[{id:2,title:"Second",nickname:"B"},{id:3,title:"Third",nickname:"C"}];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent,"Second  B");
  assert.equal(h.el("next-up-row").classList.contains("highlight"),true);
  await h.advance(2201);
  assert.equal(h.el("next-up-row").classList.contains("highlight"),false);
  h.calls.queue[1]={id:4,title:"Fourth",nickname:"D"};
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up-row").classList.contains("highlight"),false);
  h.calls.queue=[h.calls.queue[1],h.calls.queue[0]];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent,"Fourth  D");
  assert.equal(h.el("next-up-row").classList.contains("highlight"),true);
  h.calls.queue=[];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent,"暂无下一首可播放歌曲");
  await h.advance(2201);
  assert.equal(h.el("next-up-row").classList.contains("highlight"),false);
});


test("TV bottom banner transparency updates background live and persists the browser choice",()=>{
  const h=tvHarness();
  const opacity=()=>h.el("song-banner").style.getPropertyValue("--banner-opacity");
  assert.equal(h.el("banner-transparency").value,"15");
  assert.equal(h.el("banner-transparency-value").textContent,"15%");
  assert.equal(opacity(),"0.85");
  h.el("banner-transparency").value="65";
  h.el("banner-transparency").oninput();
  assert.equal(opacity(),"0.35");
  assert.equal(h.el("banner-transparency-value").textContent,"65%");
  assert.equal(h.storage.get("vcktv-banner-transparency"),"65");
  h.evaluate("setBannerTransparency(100)");
  assert.equal(opacity(),"0");
  h.evaluate("setBannerTransparency(0)");
  assert.equal(opacity(),"1");
  h.evaluate("setBannerTransparency(150)");
  assert.equal(h.el("banner-transparency").value,"100");
});


test("current and next song share a separator-free title and nickname format",async()=>{
  const h=tvHarness();
  h.calls.current={id:1,title:"Now",nickname:"Alice"};
  h.calls.queue=[{id:2,title:"Next",nickname:"Bob"}];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("status").textContent,"Now  Alice");
  assert.equal(h.el("next-up").textContent,"Next  Bob");
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  assert.equal(h.el("status").textContent,"A  guest");
});
