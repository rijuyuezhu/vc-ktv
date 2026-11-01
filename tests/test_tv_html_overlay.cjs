"use strict";
const assert=require("node:assert/strict");
const {readFileSync}=require("node:fs");
const {runInNewContext}=require("node:vm");
const {test}=require("node:test");
function element(){
  const e={children:[],dataset:{},style:{},hidden:true,textContent:"",className:""};
  e.append=child=>e.children.push(child);
  e.replaceChildren=()=>{e.children=[];};
  return e;
}
function harness(){
  const overlay=runInNewContext(readFileSync("static/tv-overlay.js","utf8")+"\nTVOverlay",{
    document:{createElement:element},Map,Number,Object,Array,Math
  });
  return {overlay,root:element()};
}
const plan={first:{start:1,end:7,slot:1}};
const timeline={lines:[{id:"first",text:"AB",units:[
  {source_end:1,start:2,end:4},{source_end:2,start:4,end:6}
]}]};
const style={mode:"double",size:48,position:.86,highlight:"#FFD53D",base:"#FFFFFF"};
test("HTML karaoke reuses saved unit timestamps and display plan",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline,plan,style},root);
  assert.equal(root.hidden,false);
  assert.equal(root.children.length,2);
  overlay.draw(3);
  const song=root.children[1];
  assert.equal(song.children.map(x=>x.textContent).join(""),"AB");
  assert.match(song.children[0].style.backgroundImage,/50%/);
  assert.match(song.children[1].style.backgroundImage,/0%/);
  overlay.setOffset(10);
  overlay.draw(3);
  assert.match(song.children[0].style.backgroundImage,/0%/);
  overlay.setOffset(-10);
  overlay.draw(3);
  assert.match(song.children[0].style.backgroundImage,/100%/);
  overlay.reset();assert.equal(root.hidden,true);assert.equal(root.children.length,0);
});
test("an untimed word remains static without invented highlight",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline:{lines:[{id:"first",text:"AB",units:[{source_end:2,start:null,end:null}]}]},plan,style},root);
  overlay.draw(4);
  assert.equal(root.children[1].children[0].textContent,"AB");
  assert.equal(root.children[1].children[0].style.backgroundImage,undefined);
});


test("subtitle geometry stays within letterboxed original video",()=>{
  const {overlay,root}=harness();
  overlay.setup({timeline,plan,style},root);
  const tall={videoWidth:1920,videoHeight:1080,getBoundingClientRect:()=>({left:0,top:0,width:400,height:800})};
  overlay.draw(3,tall);
  // 400px wide 16:9 video is centered within the 800px-high TV view.
  assert.equal(root.children[1].style.left,"16px");
  assert.equal(root.children[1].style.width,"368px");
  assert.equal(root.children[1].style.top,"481px");
  assert.equal(root.children[1].children.map(x=>x.textContent).join(""),"AB");
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
  assert.equal(root.children[1].children.map(x=>x.textContent).join(""),"A🎵B");
  assert.equal(root.children[1].children[1].textContent,"🎵");
});
