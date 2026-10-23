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
