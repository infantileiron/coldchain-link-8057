import {state} from '../stores/session.js';
import {esc} from '../utils/html.js';
import {badge} from '../components/common/status.js';
import {select} from '../components/common/fields.js';

const PHASE={pending:['待监测','新班次尚未确认货损收齐'],monitoring:['进行中','已纳入收齐班次，等待下一次查看'],alert:['预警','反弹超过警戒线，已保留证据'],invalidated:['已失效','纳入样本被补改，需重新确认收齐'],stopped:['已停止','人工停止，额度不退']};
const DECISION={alert:'预警',clear:'未见反弹'};

function pct(x){return x===null||x===undefined?'—':(Number(x)*100).toFixed(2)+'%'}
function money(x){return x===null||x===undefined?'—':Number(x).toFixed(2)+'%'}

function budgetCard(b){
  let used=Math.min(100,Number(b.alpha_spent)/Number(b.alpha_total)*100);
  return `<div class=budget><div class=eyebrow>${esc(b.zone)} · ${b.year} 误报预算</div>
  <div class=meter><span style="width:${used.toFixed(1)}%"></span></div>
  <p>已用 <strong>${pct(b.alpha_spent)}</strong> / 全年 ${pct(b.alpha_total)}；剩余 ${pct(b.remaining)}；查看 ${b.looks_used}/${b.look_cap} 次，每次 ${pct(b.look_cost)}。</p>
  <p class=eyebrow>${esc(b.tradeoff)}</p></div>`}

function monitorRow(m){
  let p=PHASE[m.phase]||[m.phase,''];
  return `<tr><td>#${m.id}</td><td>${esc(m.zone)}</td><td>${badge(p[0])}</td>
  <td>${money(m.threshold)}</td><td>${m.baseline_n} 条 / ${m.baseline?.shipment_count||0} 班</td>
  <td>${m.pending_count}</td><td>${m.included_count}</td>
  <td><button data-monitor=${m.id}>展开</button></td></tr>
  <tr><td colspan=8 class=hint>${esc(m.reason||p[1])}</td></tr>`}

function detail(m){
  let alerts=(m.alerts||[]).map(a=>`<article class="rec ${a.invalidated?'faded':''}">
    <h3>预警 · 新均值 ${money(a.new_mean)} ＞ 警戒线 ${money(a.threshold)}</h3>
    <p>${a.invalidated?`<span class=tag>已失效</span> ${esc(a.invalidated_reason)}`:`<span class=tag>有效</span>`} 查看时 p=${Number(a.p_value).toExponential(2)} ≤ 单次额度 ${pct(a.alpha_cost)}，纳入 ${a.new_n} 条上报样本。</p>
    <p class=eyebrow>批次 ${esc((a.batch_ids||[]).join(', '))}；证据保留，不随失效删除。</p></article>`).join('')||'<p>尚无预警。</p>';
  let looks=(m.looks||[]).map(l=>`<tr><td>#${l.id}</td><td>${badge(DECISION[l.decision]||l.state)}</td>
    <td>${l.new_n}</td><td>${money(l.new_mean)}</td><td>${l.p_value===null?'—':Number(l.p_value).toExponential(2)}</td>
    <td>${pct(l.alpha_cost)}</td><td>${l.invalidated?'失效':'—'}</td></tr>`).join('')||'<tr><td colspan=7>还没有查看记录。</td></tr>';
  let pending=[...(m.pending||[]).map(b=>`<li>班次 #${b.shipment_id} 已收齐，均值 ${money(b.mean_loss)}（${b.reported} 条）待纳入</li>`),
    ...(m.awaiting||[]).map(b=>`<li>班次 ${esc(b.code)} 样本齐全，等待确认货损收齐</li>`),
    ...(m.unreported||[]).map(b=>`<li>班次 ${esc(b.code)}：${esc(b.reason)}（缺 ${b.missing} 条）</li>`)].join('')||'<li>没有待处理班次。</li>';
  return `<section class=panel><h2>监测 #${m.id} · ${esc(m.zone)} ${badge(PHASE[m.phase]?.[0]||m.phase)}</h2>
    <p class=notice>警戒线 ${money(m.threshold)} 冻结自研究 #${m.study_id} 的快照：各班次平均货损取均值，原研究不被改写。${m.reason?esc(m.reason):''}</p>
    <p>${esc(m.reason||PHASE[m.phase]?.[1]||'')}</p>
    ${budgetCard(m.budget)}
    <h3>预警证据</h3>${alerts}
    <h3>待监测班次</h3><ul class=hints>${pending}</ul>
    <h3>查看记录</h3><div class=scroll><table><thead><tr><th>查看</th><th>结果</th><th>样本 n</th><th>新均值</th><th>p</th><th>额度</th><th>失效</th></tr></thead><tbody>${looks}</tbody></table></div>
    <h3>操作</h3>
    ${m.state==='active'&&state.user.role!=='viewer'?`<button class=primary data-look=${m.id}>查看一次（扣额度）</button> <button data-stop=${m.id}>停止监测</button>`:''}
    ${m.replaced_by?`<p>已由监测 #${m.replaced_by} 接续；旧预警留存依据。</p>`:''}
    ${m.source_monitor_id?`<p>接续自监测 #${m.source_monitor_id}；额度沿用温区共享池，不重置。</p>`:''}
  </section>`}

export function render(){
  let m=state.monitorDetail||state.monitors.find(x=>x.id===state.monitorId);
  let active=state.monitors.filter(x=>x.state==='active');
  let zones=state.overview?.zones||[];
  return `<div class=split><section class=panel>
    <h2>持续发现货损反弹</h2>
    <p class=notice>${esc(state.overview?.tradeoff||'')}</p>
    ${zones.map(z=>`<div class=zoneblock><div class=eyebrow>${esc(z.zone)}</div>${budgetCard(z.budget)}
      <p>有效预警 ${z.alert_count} 次；进行中监测 ${z.active_monitors.length} 个。</p></div>`).join('')}
    <h3>监测台账</h3><div class=scroll><table><thead><tr><th>监测</th><th>温区</th><th>状态</th><th>警戒线</th><th>基线</th><th>待监测</th><th>已纳入</th><th></th></tr></thead><tbody>${state.monitors.map(monitorRow).join('')||'<tr><td colspan=8>签核改善建议后自动建立监测。</td></tr>'}</tbody></table></div>
    ${m?detail(m):''}
  </section><aside class=side><h2>确认货损收齐</h2>
    <p>只聚合已上报的货损值；缺报保留为空，不当零。确认后形成不可变批次，样本一旦补改，该批次与已纳入它的监测都会失效。</p>
    <form id=batch>${select('班次','shipment_id',state.ships.map(s=>[s.id,s.code+' · '+s.zone]))}<button class=primary>确认本班次已收齐</button></form>
    <p class=eyebrow>进行中监测 ${active.length} 个 · ${state.overview?.year||''}</p></aside></div>`}
