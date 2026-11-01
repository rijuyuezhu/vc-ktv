"use strict";
// Uses the same timestamp/slot/style bundle as the burned ASS output.
// libass/ASS PlayRes uses a different effective font scale from CSS pixels.
// Calibrated against the same timeline rendered by ffmpeg/libass at 1280x720.
const ASS_FONT_TO_CSS=0.69;
// CSS text ink sits slightly below libass's bottom-aligned glyphs.
const ASS_BASELINE_ADJUST=0.075;
const TVOverlay={
  timeline:null,plan:null,style:null,offset:0,root:null,
  reset(){
    this.timeline=null;this.plan=null;this.style=null;this.offset=0;
    if(this.root){this.root.hidden=true;this.root.replaceChildren();}
  },
  setup(bundle,root){
    if(!Array.isArray(bundle?.timeline?.lines)||!bundle?.plan||!bundle?.style)throw Error("字幕时间轴无效");
    this.timeline=new Map(bundle.timeline.lines.map(line=>[line.id,line]));
    this.plan=bundle.plan;this.style=bundle.style;this.root=root;this.offset=0;
    this.root.replaceChildren();
    for(let i=0;i<2;i++){
      const row=document.createElement("div");row.className="html-caption-row";
      row.dataset.slot=String(i);this.root.append(row);
    }
    this.root.hidden=false;this.draw(0);
  },
  setOffset(tenths){this.offset=Math.max(-600,Math.min(600,tenths))/10;},
  draw(time,media=null){
    if(!this.timeline||!this.root||this.root.hidden)return;
    const t=time-this.offset;
    const selected=[null,null];
    for(const [id,window] of Object.entries(this.plan)){
      if(!Number.isFinite(window.start)||!Number.isFinite(window.end)||t<window.start||t>=window.end)continue;
      if((window.slot===0||window.slot===1)&&this.timeline.has(id))selected[window.slot]=this.timeline.get(id);
    }
    const fontSize=this.style.size||48;
    const viewport=media?.getBoundingClientRect?.()||{left:0,top:0,width:1280,height:720};
    const sourceWidth=media?.videoWidth||viewport.width;
    const sourceHeight=media?.videoHeight||viewport.height;
    const scale=Math.min(viewport.width/sourceWidth,viewport.height/sourceHeight);
    const width=sourceWidth*scale,height=sourceHeight*scale;
    const left=viewport.left+(viewport.width-width)/2;
    const top=viewport.top+(viewport.height-height)/2;
    for(const row of this.root.children){
      const slot=Number(row.dataset.slot);
      const line=selected[slot];
      const y=this.style.position??.86;
      const scaledFontSize=fontSize*ASS_FONT_TO_CSS*height/720;
      const assY=top+height*y-(this.style.mode==="double"&&slot===0?fontSize*1.4*height/720:0);
      row.style.top=(assY-scaledFontSize*ASS_BASELINE_ADJUST)+"px";
      row.style.left=(left+width*.04)+"px";
      row.style.width=(width*.92)+"px";
      row.style.right="auto";
      row.style.fontSize=scaledFontSize+"px";
      row.style.fontFamily=`"${(this.style.font||"Noto Sans CJK SC").replace(/["'\\]/g,"")}",sans-serif`;
      row.style.setProperty("--ktv-base",this.style.base||"#FFFFFF");
      row.style.setProperty("--ktv-stroke",Math.max(0,(this.style.outline??3)*2*height/720)+"px");
      const id=line?.id||"";
      if(row.dataset.lineId!==id){
        row.dataset.lineId=id;
        row.replaceChildren();
        row._timed=[];
        if(line){
          const shadow=document.createElement("div");shadow.className="html-caption-outline";
          shadow.setAttribute("aria-hidden","true");
          const fill=document.createElement("div");fill.className="html-caption-fill";
          const text=Array.from(line.text||"");
          let pos=0;
          for(const unit of line.units||[]){
            const end=Math.max(pos,Math.min(text.length,unit.source_end||pos));
            const value=text.slice(pos,end).join("");
            const outlinePart=document.createElement("span");outlinePart.textContent=value;
            shadow.append(outlinePart);
            const span=document.createElement("span");span.textContent=value;
            if(Number.isFinite(unit.start)&&Number.isFinite(unit.end)&&unit.end>unit.start){
              span.className="html-caption-sweep";
              row._timed.push({span,unit});
            }
            fill.append(span);pos=end;
          }
          if(pos<text.length){
            const value=text.slice(pos).join("");
            const outlinePart=document.createElement("span");outlinePart.textContent=value;
            const tail=document.createElement("span");tail.textContent=value;
            shadow.append(outlinePart);fill.append(tail);
          }
          row.append(shadow,fill);
        }
      }
      for(const {span,unit} of row._timed||[]){
        const percent=Math.max(0,Math.min(100,100*(t-unit.start)/(unit.end-unit.start)));
        span.style.backgroundImage=`linear-gradient(90deg,${this.style.highlight||"#FFD53D"} ${percent}%,${this.style.base||"#FFFFFF"} ${percent}%)`;
      }
    }
  },
};
