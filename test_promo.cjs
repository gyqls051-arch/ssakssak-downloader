const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/web_ui/index.html','utf8');
test('one inline promo; no launch/exit advertisements',()=>{
 assert.equal((html.match(/class="adbar"/g)||[]).length,1);
 assert.doesNotMatch(html,/id="ovStart"|class="exit-ad"/);
});
test('idle exit is immediate; downloads retain confirmation',()=>{
 const code=html.slice(html.indexOf('function showExit()'),html.indexOf('function openPromo()'));
 for(const active of [0,2]){
  let closed=0,shown=false;const elements={};
  const ctx={tally:()=>({active}),api:()=>({exit_confirmed:()=>closed++}),$:id=>elements[id]||(elements[id]={textContent:'',style:{},classList:{add(){shown=true;}}})};
  vm.createContext(ctx);vm.runInContext(code,ctx);ctx.showExit();
  assert.equal(closed,active===0?1:0);assert.equal(shown,active>0);
 }
});
test('startup update modal shows 업데이트 해야합니다 prompt',()=>{
 assert.match(html,/id="ovUpdate"/);
 assert.match(html,/업데이트 해야합니다/);
 const code=html.slice(html.indexOf('let updUrl ='),html.indexOf('/* ── 키보드:'));
 const elements={};
 const makeEl=()=>({textContent:'',title:'',disabled:false,style:{},onclick:null,
  classes:new Set(),classList:{add(c){this.s.add(c);},remove(c){this.s.delete(c);}}});
 const ctx={api:()=>({start_app_update:async()=>({ok:true}),open_url:()=>{}}),
  $:id=>{if(!elements[id]){const e=makeEl();e.classList.s=e.classes;elements[id]=e;}return elements[id];}};
 vm.createContext(ctx);vm.runInContext(code,ctx);
 ctx.updateAvailable('1.11.2','https://github.com/gyqls051-arch/ssakssak-downloader/releases/tag/v1.11.2','',false);
 assert.equal(elements.ovUpdate.classes.has('on'),true);
 assert.equal(elements.updModalVer.textContent,'v1.11.2');
});
