"use strict";
const assert=require("node:assert/strict");
const {readFileSync}=require("node:fs");
const {runInNewContext}=require("node:vm");
const {test}=require("node:test");
function element(){
  const e={children:[],dataset:{},attributes:{},style:{setProperty(k,v){this[k]=v;}},hidden:true,textContent:"",className:""};
  e.append=(...children)=>e.children.push(...children);
  e.replaceChildren=()=>{e.children=[];};
  e.setAttribute=(key,value)=>{e.attributes[key]=value;};
  return e;
}
function harness(){
  const overlay=runInNewContext(readFileSync("static/tv-overlay.js","utf8")+"\nTVOverlay",{
    document:{createElement:element},Map,Number,Object,Array,Math
  });
  return {overlay,root:element()};
}
const fill=row=>row.children[1];
const outline=row=>row.children[0];
const words=row=>fill(row).children;
const plan={first:{start:1,end:7,slot:1}};
const timeline={lines:[{id:"first",text:"AB",units:[
  {source_end:1,start:2,end:4},{source_end:2,start:4,end:6}
]}]};
const style={mode:"double",size:48,position:.86,outline:3,font:"Noto Sans CJK SC",highlight:"#FFD53D",base:"#FFFFFF"};
test("HTML karaoke uses separate dark outline and live white/yellow fill",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline,plan,style},root);
  assert.equal(root.hidden,false);
  assert.equal(root.children.length,2);
  overlay.draw(3);
  const row=root.children[1];
  assert.equal(outline(row).className,"html-caption-outline");
  assert.equal(fill(row).className,"html-caption-fill");
  assert.equal(outline(row).attributes["aria-hidden"],"true");
  assert.equal(words(row).map(x=>x.textContent).join(""),"AB");
  assert.equal(outline(row).children.map(x=>x.textContent).join(""),"AB");
  assert.match(words(row)[0].style.backgroundImage,/50%/);
  assert.match(words(row)[1].style.backgroundImage,/0%/);
  assert.equal(row.style["--ktv-base"],"#FFFFFF");
  assert.equal(row.style["--ktv-stroke"],"6px");
  assert.equal(row.style.fontFamily,'"Noto Sans CJK SC",sans-serif');
  overlay.setOffset(10);
  overlay.draw(3);
  assert.match(words(row)[0].style.backgroundImage,/0%/);
  overlay.setOffset(-10);
  overlay.draw(3);
  assert.match(words(row)[0].style.backgroundImage,/100%/);
  overlay.reset();assert.equal(root.hidden,true);assert.equal(root.children.length,0);
});
test("an untimed word remains static and white without invented highlight",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline:{lines:[{id:"first",text:"AB",units:[{source_end:2,start:null,end:null}]}]},plan,style},root);
  overlay.draw(4);
  const row=root.children[1];
  assert.equal(words(row)[0].textContent,"AB");
  assert.equal(words(row)[0].className,"");
  assert.equal(words(row)[0].style.backgroundImage,undefined);
});
test("three admin presets inherit exactly the saved ASS style settings",()=>{
  for(const preset of [
    {...style,mode:"double",size:48,position:.86,outline:3,highlight:"#FFD53D"},
    {...style,mode:"double",size:62,position:.84,outline:5,highlight:"#FFE45E"},
    {...style,mode:"single",size:38,position:.91,outline:2,highlight:"#FFD53D"},
  ]){
    const {overlay,root}=harness();
    overlay.setup({timeline,plan:{first:{...plan.first,slot:0}},style:preset},root);
    const viewport={videoWidth:1280,videoHeight:720,getBoundingClientRect:()=>({left:0,top:0,width:1280,height:720})};
    overlay.draw(3,viewport);
    assert.equal(root.children[0].style.fontSize,preset.size+"px");
    assert.equal(root.children[0].style["--ktv-stroke"],(preset.outline*2)+"px");
    assert.equal(root.children[0].style["--ktv-base"],preset.base);
    assert.match(words(root.children[0])[0].style.backgroundImage,new RegExp(preset.highlight));
    assert.equal(root.children[0].style.top,`${720*preset.position-(preset.mode==="double"?preset.size*1.4:0)}px`);
    assert.equal(root.children[1].children.length,0);
  }
});
test("subtitle geometry stays within letterboxed original video",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline,plan,style},root);
  const tall={videoWidth:1920,videoHeight:1080,getBoundingClientRect:()=>({left:0,top:0,width:400,height:800})};
  overlay.draw(3,tall);
  assert.equal(root.children[1].style.left,"16px");
  assert.equal(root.children[1].style.width,"368px");
  assert.equal(root.children[1].style.top,"481px");
  assert.equal(words(root.children[1]).map(x=>x.textContent).join(""),"AB");
});
test("Python codepoint character spans survive emoji in lyric lines",()=>{
  const {overlay,root}=harness();
  const emojiTimeline={lines:[{id:"first",text:"A🎵B",units:[
    {source_end:1,start:2,end:3},
    {source_end:2,start:3,end:4},
    {source_end:3,start:4,end:5},
  ]}]};
  overlay.setup({timeline:emojiTimeline,plan,style},root);
  overlay.draw(3);
  assert.equal(words(root.children[1]).map(x=>x.textContent).join(""),"A🎵B");
  assert.equal(words(root.children[1])[1].textContent,"🎵");
});
test("changing time updates gradient but does not rebuild text DOM each frame",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline,plan,style},root);
  overlay.draw(3);
  const before=words(root.children[1])[0];
  overlay.draw(4);
  assert.equal(words(root.children[1])[0],before);
  assert.match(before.style.backgroundImage,/100%/);
});
