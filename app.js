const KEY = 'hust-campus-ai-demo-v1';
const initial = () => ({ loggedIn:false, mode:'email', identity:'', balance:0, spent:0, ledger:[], accounts:{} });
const stored = JSON.parse(localStorage.getItem(KEY) || 'null');
let state = stored || initial();
// Keep accounts by identity so logging out and back in does not issue a second grant.
// Migrate the original single-account shape when an older demo is opened.
if (!state.accounts) {
  state.accounts = {};
  if (stored && stored.identity) {
    const key = `${stored.mode || 'email'}:${stored.identity}`;
    state.accounts[key] = { balance:stored.balance || 0, spent:stored.spent || 0, ledger:stored.ledger || [] };
  }
}
const $ = id => document.getElementById(id);
const save = () => {
  if (state.loggedIn && state.identity) {
    state.accounts[`${state.mode}:${state.identity}`] = { balance:state.balance, spent:state.spent, ledger:state.ledger };
  }
  localStorage.setItem(KEY, JSON.stringify(state));
};
const fmt = n => Number(n).toLocaleString('zh-CN');
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2400)}
function render(){
  $('loginView').classList.toggle('hidden', state.loggedIn); $('appView').classList.toggle('hidden', !state.loggedIn);
  if(!state.loggedIn)return;
  $('userIdentity').textContent=state.identity; $('balance').textContent=fmt(state.balance); $('spent').textContent=fmt(state.spent);
  $('ledger').innerHTML=state.ledger.length?state.ledger.map(x=>`<div class="ledger-row"><div class="ledger-title"><span class="ledger-icon">${x.type==='grant'?'＋':'✦'}</span><div><b>${x.title}</b><small>${x.time}</small></div></div><span class="amount ${x.amount>0?'plus':'minus'}">${x.amount>0?'+':''}${fmt(x.amount)}</span></div>`).join(''):'<p class="muted">暂无流水记录</p>';
}
function addLedger(title,amount,type){state.ledger.unshift({title,amount,type,time:new Date().toLocaleString('zh-CN',{hour12:false})})}
document.querySelectorAll('.tab').forEach(btn=>btn.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));btn.classList.add('active');state.mode=btn.dataset.mode;$('identityLabel').textContent=state.mode==='email'?'校园邮箱':'学号';$('identity').placeholder=state.mode==='email'?'例如 2024001001@mail.hust.edu.cn':'例如 2024001001';$('identityHint').textContent=state.mode==='email'?'原型演示可输入任意校园邮箱':'原型演示可输入任意学号'}));
$('loginBtn').onclick=()=>{const value=$('identity').value.trim();if(!value){toast('请输入校园邮箱或学号');return}const key=`${state.mode}:${value}`;let account=state.accounts[key];const firstLogin=!account;if(!account){account={balance:100000,spent:0,ledger:[]};state.accounts[key]=account;state.balance=account.balance;state.spent=account.spent;state.ledger=account.ledger;addLedger('新用户注册赠送',100000,'grant');account.balance=state.balance;account.spent=state.spent;account.ledger=state.ledger}else{state.balance=account.balance;state.spent=account.spent;state.ledger=account.ledger}state.loggedIn=true;state.identity=value;save();render();toast(firstLogin?'登录成功，已领取 100,000 Token':'登录成功，已恢复账户数据')};
function reset(){state=initial();save();render();toast('演示数据已重置')}
$('logoutBtn').onclick=()=>{state.loggedIn=false;save();render()};$('resetBtn').onclick=reset;$('resetFromLogin').onclick=reset;
$('demoUseBtn').onclick=()=>{if(state.balance<1200){toast('Token 余额不足');return}state.balance-=1200;state.spent+=1200;addLedger('AI 创作体验',-1200,'use');save();render();toast('已模拟消耗 1,200 Token')};
$('createBtn').onclick=()=>$('demoUseBtn').click();
function openModal(title,text){$('modalTitle').textContent=title;$('modalText').textContent=text;$('modal').classList.remove('hidden')}$('rechargeBtn').onclick=()=>openModal('充值额度','充值功能将在正式版开放，原型阶段仅展示入口。');$('closeModal').onclick=()=>$('modal').classList.add('hidden');$('modalAction').onclick=()=>$('modal').classList.add('hidden');
render();
