"use strict";

const {test} = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {pathToFileURL} = require("node:url");
const url = pathToFileURL(path.join(__dirname,"../static/song-detail.js")).href;

function element(tag="div") {
  const attrs=new Map();
  const node={tagName:tag.toUpperCase(),children:[],textContent:"",className:"",value:"",
    style:{},open:false,files:[],disabled:false,hidden:false};
  const classes=()=>new Set(node.className.split(/\s+/).filter(Boolean));
  node.classList={
    contains(name){return classes().has(name);},
    add(name){const s=classes();s.add(name);node.className=[...s].join(" ");},
    remove(name){const s=classes();s.delete(name);node.className=[...s].join(" ");}
  };
  node.append=(...items)=>{for(const child of items){node.children.push(child);child.parent=node;}};
  node.prepend=(child)=>{node.children.unshift(child);child.parent=node;};
  node.replaceChildren=(...items)=>{node.children=[];node.append(...items);};
  node.remove=()=>{if(node.parent)node.parent.children=node.parent.children.filter(x=>x!==node);};
  node.replaceWith=(other)=>{if(node.parent){const i=node.parent.children.indexOf(node);
    node.parent.children.splice(i,1,other);other.parent=node.parent;}};
  node.setAttribute=(name,value)=>attrs.set(name,String(value));
  node.getAttribute=name=>attrs.get(name)??null;
  node.removeAttribute=name=>attrs.delete(name);
  node.querySelector=(selector)=>find(node,x=>selector.startsWith(".")?
    x.classList.contains(selector.slice(1)):x.tagName.toLowerCase()===selector);
  node.showModal=()=>{node.open=true;};
  node.close=()=>{node.open=false;};
  return node;
}
function find(node,predicate){
  for(const child of node.children){if(predicate(child))return child;
    const nested=find(child,predicate);if(nested)return nested;}
  return null;
}
function all(node,predicate){
  return node.children.flatMap(child=>[
    ...(predicate(child)?[child]:[]),...all(child,predicate)
  ]);
}
function button(root,label){
  const found=find(root,x=>x.tagName==="BUTTON"&&x.textContent===label);
  assert.ok(found,`expected button ${label}`);return found;
}
function baseSong(overrides={}){
  return {id:17,source_type:"bilibili",bvid:"BV12xxxyyyy",page:1,
    title:"演唱歌曲",uploader:"歌手",nickname:"点歌人",status:"ready",prep_state:"done",
    duration:190,mine:true,playable:true,has_media:true,downloadable:true,
    source_downloadable:true,lyric_selected:true,lyric_query:"演唱歌曲",priority_next:false,
    skip_subtitles:false,awaiting_subtitles:false,stage_seconds:{download:1.4},
    ...overrides};
}
async function harness(admin=false){
  const originalNavigator=Object.getOwnPropertyDescriptor(global,"navigator");
  const originals={document:global.document,window:global.window,fetch:global.fetch,
    confirm:global.confirm,prompt:global.prompt,
    setTimeout:global.setTimeout,clearTimeout:global.clearTimeout};
  const nodes=new Map(),calls=[],copied=[],opened=[],cancelled=[],messages=[];
  const byId=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  const state={song:baseSong(),admin,refreshes:0,allow:true,detailCount:0,
    vcpedia:{candidates:[{text:"参考一行\n参考二行"}]}};
  global.document={getElementById:byId,createElement:element,body:element("body"),
    execCommand:()=>true};
  global.window={location:{pathname:"/",hash:"song-17"}};
  Object.defineProperty(global,"navigator",{configurable:true,
    value:{clipboard:{writeText:async text=>{copied.push(text);}}}});
  global.confirm=()=>state.allow;
  global.prompt=(label,text)=>{messages.push([label,text]);return text;};
  global.setTimeout=()=>1;global.clearTimeout=()=>{};
  global.fetch=async(url,opts)=>{
    const method=opts?.method??"GET";
    calls.push({url,method,opts});
    let data={ok:true};
    if(url==="/api/songs/17/detail"){
      state.detailCount++;data=state.song;
    }else if(url==="/api/admin/songs/17/lyrics"&&method==="GET"){
      data={text:"旧歌词"};
    }else if(url.startsWith("/api/admin/lyrics/vcpedia")){
      data=state.vcpedia;
    }else if(url.startsWith("/api/admin/songs/17/alignment/import")){
      data={lyrics:"新对齐歌词",report:{semantic_units:4}};
    }else if(url==="/api/songs/17/replay"){
      data={id:18};
    }
    return {ok:true,async json(){return data;}};
  };
  const {setupSongDetail}=await import(url);
  const controller=setupSongDetail({
    refresh:async()=>{state.refreshes++;},
    currentRoute:()=>global.window.location.hash,
    isAdminPath:()=>state.admin,
    songState:s=>s.status==="played"?"已唱":s.status==="waiting"?"下载失败":
      s.status==="ready"?"待播":s.status,
    addSourceTag:(parent,song)=>{const tag=element("source");tag.title=song.source_type;parent.append(tag);},
    openLyricPicker:async(...args)=>opened.push(args),
    cancelGuestSong:async id=>cancelled.push(id)
  });
  return {state,nodes,calls,copied,opened,cancelled,byId,...controller,
    cleanup(){Object.assign(global,originals);
      Object.defineProperty(global,"navigator",originalNavigator);}};
}

test("guest song detail keeps source links, download actions, lyric selection and cancellation",async()=>{
  const h=await harness();
  try{
    await h.refreshSongDetail();
    const page=h.byId("page-song");
    assert.equal(find(page,x=>x.tagName==="H2").textContent,"演唱歌曲");
    assert.equal(find(page,x=>x.tagName==="SOURCE").title,"bilibili");
    const links=all(page,x=>x.tagName==="A");
    assert.ok(links.some(x=>x.href==="/api/songs/17/download"));
    assert.ok(links.some(x=>x.href==="/api/songs/17/download/source"));
    assert.ok(links.some(x=>x.href==="https://www.bilibili.com/video/BV12xxxyyyy/?p=1"));
    await button(page,"复制 BV").onclick();
    assert.deepEqual(h.copied,["BV12xxxyyyy"]);
    await button(page,"更换歌词").onclick();
    assert.deepEqual(h.opened,[[17,"演唱歌曲","演唱歌曲"]]);
    await button(page,"取消点歌").onclick();
    assert.deepEqual(h.cancelled,[17]);
    assert.equal(h.calls.some(c=>c.url.startsWith("/api/admin/")),false);
  }finally{h.cleanup();}
});

test("role and status gate admin actions, and manual render uses editable lead time",async()=>{
  const h=await harness(true);
  try{
    await h.refreshSongDetail();
    const page=h.byId("page-song");
    assert.equal(find(page,x=>x.tagName==="H3"&&x.textContent==="歌曲管理")!==null,true);
    assert.equal(find(page,x=>x.tagName==="BUTTON"&&x.textContent==="取消点歌")!==null,true);
    const input=find(page,x=>x.id==="detail-lead-seconds");
    assert.ok(input);
    input.value="0.7";input.oninput();
    await button(page,"手工制作 KTV").onclick();
    const call=h.calls.find(x=>x.url==="/api/admin/songs/17/render");
    assert.deepEqual(JSON.parse(call.opts.body),{lead:0.7});
    await button(page,"下一首播放").onclick();
    assert.ok(h.calls.some(x=>x.url==="/api/admin/songs/17/pin-next"));
    await button(page,"按新风格重制").onclick();
    assert.ok(h.calls.some(x=>x.url==="/api/admin/songs/17/subtitles/retry"));
    h.state.song=baseSong({status:"waiting",prep_state:"unavailable",downloadable:false});
    await h.refreshSongDetail();
    assert.ok(find(page,x=>x.tagName==="INPUT"&&x.type==="file"));
    await button(page,"重试下载").onclick();
    assert.ok(h.calls.some(x=>x.url==="/api/admin/songs/17/download/retry"));
    assert.equal(find(page,x=>x.tagName==="BUTTON"&&x.textContent==="手工制作 KTV"),null);
  }finally{h.cleanup();}
});

test("original media skip, replay and upload retain their exact API methods",async()=>{
  const h=await harness(true);
  try{
    h.state.song=baseSong({prep_state:"original"});
    await h.refreshSongDetail();
    await button(h.byId("page-song"),"原片入队").onclick();
    assert.ok(h.calls.some(x=>x.method==="POST"&&x.url==="/api/admin/songs/17/subtitles/skip"));
    h.state.song=baseSong({status:"waiting",prep_state:"original"});
    await h.refreshSongDetail();
    const upload=find(h.byId("page-song"),x=>x.type==="file");
    assert.ok(upload);
    upload.files=[{name:"new.mp4"}];await upload.onchange();
    assert.ok(h.calls.some(x=>x.method==="POST"&&x.url==="/api/admin/songs/17/media"&&x.opts.body instanceof FormData));
    h.state.song=baseSong({status:"played"});
    await h.refreshSongDetail();
    await button(h.byId("page-song"),"↻ 下一首重唱").onclick();
    assert.ok(h.calls.some(x=>x.url==="/api/admin/songs/17/replay"));
  }finally{h.cleanup();}
});

test("unchanged detail updates running elapsed time without destroying DOM; invalidation refreshes",async()=>{
  const h=await harness(true);
  try{
    h.state.song=baseSong({status:"waiting",prep_state:"aligning",prep_stage_at:Math.floor(Date.now()/1000)-12});
    await h.refreshSongDetail();
    const page=h.byId("page-song"),first=page.children[0];
    const progress=find(page,x=>x.classList.contains("stage-current-time"));
    assert.ok(progress);
    progress.textContent="stale";
    await h.refreshSongDetail();
    assert.strictEqual(page.children[0],first);
    assert.match(progress.textContent,/^\d+ 秒$/);
    const details=find(page,x=>x.classList.contains("timing-details"));
    details.open=true;
    h.state.song=baseSong({status:"waiting",prep_state:"aligning",prep_stage_at:Math.floor(Date.now()/1000)-12,
      stage_seconds:{download:3.5,extract:1.7}});
    await h.refreshSongDetail();
    assert.notStrictEqual(page.children[0],first);
    assert.equal(find(page,x=>x.classList.contains("timing-details")).open,true);
    const previous=page.children[0];
    h.invalidateSongDetail();await h.refreshSongDetail();
    assert.notStrictEqual(page.children[0],previous);
  }finally{h.cleanup();}
});

test("administrator lyric editor retrieves, searches, imports TextGrid and saves",async()=>{
  const h=await harness(true);
  try{
    await h.refreshSongDetail();
    await button(h.byId("page-song"),"编辑歌词").onclick();
    assert.equal(h.byId("lyrics-dialog").open,true);
    assert.equal(h.byId("lyrics-textarea").value,"旧歌词");
    assert.equal(h.byId("lyric-source-title").value,"演唱歌曲");
    await h.byId("lyric-source-btn").onclick();
    assert.equal(h.byId("lyric-source-results").children.length,1);
    // Existing content asks for replacement confirmation before applying a candidate.
    await h.byId("lyric-source-results").children[0].onclick();
    assert.equal(h.byId("lyrics-textarea").value,"参考一行\n参考二行");
    h.byId("textgrid-file").files=[{name:"alignment.TextGrid"}];
    h.byId("alignment-offset").value="-0.3";
    await h.byId("textgrid-file").onchange();
    assert.equal(h.byId("lyrics-textarea").value,"新对齐歌词");
    assert.ok(h.calls.some(c=>c.url==="/api/admin/songs/17/alignment/import?offset=-0.3"));
    await h.byId("lyrics-save").onclick();
    assert.equal(h.byId("lyrics-dialog").open,false);
    assert.ok(h.calls.some(c=>c.method==="POST"&&c.url==="/api/admin/songs/17/lyrics"&&
      JSON.parse(c.opts.body).text==="新对齐歌词"));
  }finally{h.cleanup();}
});

test("NetEase song details retain source and guest replay navigation",async()=>{
  const h=await harness(false);
  try{
    h.state.song=baseSong({status:"played",source_type:"netease",source_id:"5832",bvid:"",
      source_downloadable:true});
    await h.refreshSongDetail();
    const page=h.byId("page-song");
    const links=all(page,x=>x.tagName==="A");
    assert.ok(links.some(x=>x.href==="https://music.163.com/#/song?id=5832"));
    assert.ok(find(page,x=>x.tagName==="A"&&x.textContent==="下载源视频"));
    assert.equal(find(page,x=>x.textContent==="复制 BV"),null);
    await button(page,"↻ 再来一首").onclick();
    assert.equal(global.window.location.hash,"song-18");
    assert.equal(h.state.refreshes,1);
  }finally{h.cleanup();}
});
