import {state} from '../stores/session.js';
import {esc} from '../utils/html.js';
import {badge} from '../components/common/status.js';
import {field,select} from '../components/common/fields.js';

function monitorFor(rec){
  return state.monitors.find(m=>m.recommendation_id===rec.id);
}

export function render(){
  return `<div class=split><section class=panel><h2>从证据到行动</h2>
  <p class=notice>高相关性只触发现场复核。签核人需结合线路、货种、停靠时长核实改善方案。签核后自动建立货损监测，持续发现反弹。</p>
  ${state.recs.map(r=>{
    let m=monitorFor(r);
    return `<article class=rec><h3>研究 #${r.study_id} ${badge(r.status)}</h3>
    <p>${esc(r.note)}</p>
    <p>责任人：${esc(r.owner)} ${r.status==='draft'&&state.user.role==='admin'?`<button data-approve=${r.id}>负责人签核</button>`:''}</p>
    ${r.status==='approved'?`<p>货损监测：${m?`#${m.id} · ${esc(m.phase)} · 警戒线 ${Number(m.threshold).toFixed(2)}%；<a href="#4" data-goto-monitor=${m.id}>查看详情</a>`:'尚未建立'}</p>`:''}
    </article>`;}).join('')||'<p>尚无改善建议。先在关联分析完成研究，再起草行动。</p>'}
  </section><aside class=side><h2>起草改善建议</h2>
  <form id=rec>${select('已完成研究','study_id',state.studies.filter(s=>s.state==='done').map(s=>[s.id,s.name]))}${field('责任人','owner','text','required')}<label>现场复核与改善动作<textarea name=note required minlength=10 rows=6></textarea></label><button class=primary>保存待签核建议</button></form>
  </aside></div>`}
