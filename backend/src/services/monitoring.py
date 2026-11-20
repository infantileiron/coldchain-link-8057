"""建议签核后的货损反弹持续监测。

口径：
- 警戒线：研究快照内各班次平均货损的均值，创建监测时冻结，原研究不再改动。
- 后续班次确认「货损收齐」后形成不可变批次快照，再按温区聚合样本。
- 缺报不当零：只聚合已上报的货损值，整批无上报不能确认收齐。
- 误报预算：每个温区每年共用 5%，任意一次查看都从共享池扣一份额度；
  提前停止、监测失效都不退款，年中新增监测也只从剩余额度支取。
"""
import hashlib
import json
import math
import os
import statistics
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from src.models.entities import (Alert, LossBatch, Monitor, MonitorInclusion,
                                 MonitorLook, Sample, Shipment, Study,
                                 ZoneBudget)

ALPHA_TOTAL = float(os.getenv('MONITOR_ALPHA_TOTAL', '0.05'))
LOOK_CAP = int(os.getenv('MONITOR_LOOK_CAP', '50'))
DEFAULT_LOOK_COST = ALPHA_TOTAL / LOOK_CAP
ALERT_WARNING = ('相关性不代表因果；预警只触发现场复核，不自动处罚线路。'
                 '单次查看越频繁、越早停止，单次阈值越严，及时性提高但检出能力下降。')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def current_year():
    return datetime.now(timezone.utc).year


def _json_key(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def sample_fingerprint(samples):
    """批次指纹：样本新增、补改、删除都会改变，用于识别「半批」或被改写的批次。"""
    payload = [(r.id, r.code, r.exposure, r.excursions, r.loss)
               for r in sorted(samples, key=lambda r: r.id)]
    return hashlib.sha256(_json_key(payload).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------- 预算

def ensure_budget(s, zone, year=None):
    year = year or current_year()
    row = (s.query(ZoneBudget).filter_by(zone=zone, year=year)
           .populate_existing().first())
    if row:
        return row
    try:
        with s.begin_nested():
            row = ZoneBudget(zone=zone, year=year, alpha_total=ALPHA_TOTAL,
                             alpha_spent=0.0, look_cost=DEFAULT_LOOK_COST,
                             look_cap=LOOK_CAP, looks_used=0,
                             created_at=now_iso(), updated_at=now_iso())
            s.add(row)
            s.flush()
        return row
    except IntegrityError:
        return s.query(ZoneBudget).filter_by(zone=zone, year=year).one()


def peek_budget(s, zone, year=None):
    """只读视图使用：没有账本时返回等价的临时对象，不在 GET 请求里写库。"""
    year = year or current_year()
    row = (s.query(ZoneBudget).filter_by(zone=zone, year=year)
           .populate_existing().first())
    if row:
        return row
    return ZoneBudget(zone=zone, year=year, alpha_total=ALPHA_TOTAL,
                      alpha_spent=0.0, look_cost=DEFAULT_LOOK_COST,
                      look_cap=LOOK_CAP, looks_used=0)


def budget_view(row):
    spent = round(float(row.alpha_spent or 0.0), 6)
    remaining = round(max(0.0, float(row.alpha_total) - spent), 6)
    return {'zone': row.zone, 'year': row.year, 'alpha_total': row.alpha_total,
            'alpha_spent': spent, 'remaining': remaining,
            'look_cost': row.look_cost, 'look_cap': row.look_cap,
            'looks_used': row.looks_used,
            'tradeoff': ('本温区本年度全部监测共用 %.0f%% 误报预算；每次查看从同一池中扣除 %.2f%%，'
                         '任意查看与提前停止都计入，停止或失效不退额度。查看越频繁，单次阈值越严，'
                         '预警更及时但检出能力下降；年中新增监测共享剩余额度，不重置预算。'
                         % (float(row.alpha_total) * 100, float(row.look_cost) * 100))}


def charge_budget(s, zone, year):
    budget = ensure_budget(s, zone, year)
    cost = float(budget.look_cost)
    stmt = (update(ZoneBudget)
            .where(ZoneBudget.id == budget.id,
                   ZoneBudget.alpha_spent + cost <= ZoneBudget.alpha_total + 1e-9)
            .values(alpha_spent=ZoneBudget.alpha_spent + cost,
                    looks_used=ZoneBudget.looks_used + 1,
                    updated_at=now_iso()))
    result = s.execute(stmt)
    if result.rowcount != 1:
        raise HTTPException(409, '本温区本年度 5% 误报额度已用尽，无法再查看；'
                                 '已停止或已失效的监测不会退还额度')
    return cost


# ---------------------------------------------------------------- 基线 / 监测

def _baseline_from_study(study):
    snapshot = (study.result or {}).get('snapshot') or []
    per = {}
    for row in snapshot:
        if row.get('loss') is None:
            continue
        per.setdefault(row['shipment_id'], []).append(float(row['loss']))
    if not per:
        raise HTTPException(409, '研究快照没有可用于警戒线的货损样本')
    shipment_means = [statistics.fmean(v) for v in per.values()]
    values = [v for rows in per.values() for v in rows]
    sd = statistics.stdev(values) if len(values) >= 2 else 0.0
    threshold = statistics.fmean(shipment_means)
    return {
        'source': 'study_snapshot',
        'study_id': study.id,
        'threshold': threshold,
        'baseline_mean': statistics.fmean(values),
        'baseline_n': len(values),
        'baseline_sd': sd,
        'shipment_count': len(per),
        'per_shipment': [{'shipment_id': sid, 'mean_loss': statistics.fmean(v),
                          'reported': len(v)} for sid, v in sorted(per.items())],
        'snapshot_hash': hashlib.sha256(_json_key(snapshot).encode('utf-8')).hexdigest(),
        'formula': '各班次平均货损取均值；只使用已上报货损，缺失不当零',
    }


def baseline_shipment_ids(monitor):
    return {int(x['shipment_id']) for x in (monitor.baseline or {}).get('per_shipment', [])}


def create_monitor(s, rec, user_email, study=None, explicit=False):
    """签核建议时创建监测；同一建议同时只有一个进行中监测。

    explicit=True 表示年中主动新增监测（例如上一监测已停止），沿用同一基线、
    同一温区年度误报预算，不重置任何额度。
    """
    study = study or s.get(Study, rec.study_id)
    if not study or study.state != 'done':
        raise HTTPException(409, '仅已完成研究可以建立货损监测')
    latest = (s.query(Monitor).filter_by(recommendation_id=rec.id)
              .order_by(Monitor.id.desc()).first())
    if latest and latest.state in ('active', 'invalidated'):
        return latest
    if latest and latest.state == 'stopped' and not explicit:
        return latest
    if latest and latest.state == 'stopped':
        baseline = dict(latest.baseline or {})
        year = latest.year
        if year != current_year():
            year = current_year()
    else:
        baseline = _baseline_from_study(study)
        year = current_year()
    ensure_budget(s, study.zone, year)
    monitor = Monitor(study_id=study.id, recommendation_id=rec.id, zone=study.zone,
                      year=year, state='active', reason='', baseline=baseline,
                      threshold=baseline['threshold'], baseline_n=baseline['baseline_n'],
                      baseline_sd=baseline['baseline_sd'], window_start=now_iso(),
                      created_by=user_email, created_at=now_iso(),
                      source_monitor_id=(latest.id if latest else None))
    s.add(monitor)
    s.flush()
    return monitor


def invalidate_monitor(s, monitor, reason, user_email):
    """旧监测失效：预警与查看证据保留，只标记失效原因；新监测不继承旧额度。"""
    if monitor.state != 'active':
        return None
    monitor.state = 'invalidated'
    monitor.reason = reason
    monitor.stopped_at = now_iso()
    for alert in s.query(Alert).filter_by(monitor_id=monitor.id, invalidated=False).all():
        alert.invalidated = True
        alert.invalidated_reason = reason
        alert.invalidated_at = now_iso()
    for look in s.query(MonitorLook).filter_by(monitor_id=monitor.id, state='running').all():
        look.state = 'failed'
        look.error = '监测已失效：' + reason
        look.invalidated = True
        look.invalidated_reason = reason
        look.completed_at = now_iso()
    replacement = Monitor(study_id=monitor.study_id,
                          recommendation_id=monitor.recommendation_id,
                          zone=monitor.zone, year=monitor.year, state='active',
                          reason='', baseline=json.loads(json.dumps(monitor.baseline)),
                          threshold=monitor.threshold, baseline_n=monitor.baseline_n,
                          baseline_sd=monitor.baseline_sd, window_start=now_iso(),
                          created_by=user_email, created_at=now_iso(),
                          source_monitor_id=monitor.id)
    s.add(replacement)
    s.flush()
    monitor.replaced_by = replacement.id
    return replacement


def invalidate_for_batch(s, batch, reason, user_email):
    """批次被补改后，所有已纳入它的进行中监测失效（同一原因只失效一次）。"""
    monitor_ids = [r[0] for r in
                   s.query(MonitorInclusion.monitor_id)
                   .join(Monitor, Monitor.id == MonitorInclusion.monitor_id)
                   .filter(MonitorInclusion.batch_id == batch.id,
                           Monitor.state == 'active').distinct().all()]
    latest = None
    for monitor_id in monitor_ids:
        monitor = s.get(Monitor, monitor_id)
        if monitor and monitor.state == 'active':
            latest = invalidate_monitor(s, monitor, reason, user_email) or latest
    return latest


def note_sample_change(s, shipment_ids, user_email):
    """样本新增或补改后调用：当前批次指纹变化即作废批次并失效相关监测。"""
    s.flush()
    for shipment_id in {int(x) for x in shipment_ids if x is not None}:
        # 与收齐确认互斥：两边都锁同一批样本行，不会读到半批
        samples = (s.query(Sample).filter_by(shipment_id=shipment_id)
                   .populate_existing().with_for_update().all())
        fingerprint = sample_fingerprint(samples)
        for batch in s.query(LossBatch).filter_by(shipment_id=shipment_id,
                                                  status='active').all():
            if batch.fingerprint == fingerprint:
                continue
            batch.status = 'stale'
            batch.reason = '样本已补改，需重新确认货损收齐'
            batch.superseded_at = now_iso()
            invalidate_for_batch(s, batch,
                                 '已纳入班次 %s 的样本补改，旧监测失效' % shipment_id,
                                 user_email)


def note_shipment_zone_change(s, shipment, old_zone, user_email):
    """班次改温区后，旧批次不再属于原温区，按「已纳入数据改变」处理。"""
    if not old_zone or old_zone == shipment.zone:
        return
    s.flush()
    for batch in (s.query(LossBatch).filter_by(shipment_id=shipment.id,
                                               status='active')
                  .populate_existing().with_for_update().all()):
        batch.status = 'stale'
        batch.reason = '班次温区由 %s 改为 %s，需重新确认收齐' % (old_zone, shipment.zone)
        batch.superseded_at = now_iso()
        invalidate_for_batch(s, batch,
                             '已纳入班次 %s 温区变更，旧监测失效' % shipment.code,
                             user_email)


# ---------------------------------------------------------------- 批次收齐

def confirm_batch(s, shipment, user_email, idempotency_key=None):
    if idempotency_key:
        prior = (s.query(LossBatch).filter_by(idempotency_key=idempotency_key)
                 .with_for_update().first())
        if prior:
            return prior
    # 锁定样本，避免收齐确认与补改并发时读到半批
    samples = (s.query(Sample).filter_by(shipment_id=shipment.id)
               .populate_existing().with_for_update()
               .order_by(Sample.id).all())
    fingerprint = sample_fingerprint(samples)
    reported = [r.loss for r in samples if r.loss is not None]
    if not reported:
        raise HTTPException(422, '该班次没有已上报的货损值，不能确认收齐；缺报不能当零')
    mean_loss = statistics.fmean(reported)
    existing = (s.query(LossBatch)
                .filter_by(shipment_id=shipment.id, fingerprint=fingerprint,
                           status='active')
            .populate_existing().with_for_update().first())
    if existing:
        return existing
    for old in (s.query(LossBatch).filter_by(shipment_id=shipment.id,
                                             status='active')
                .populate_existing().with_for_update()
                .order_by(LossBatch.id).all()):
        old.status = 'superseded'
        old.reason = '重新确认货损收齐'
        old.superseded_at = now_iso()
        invalidate_for_batch(s, old,
                             '已纳入班次 %s 重新确认收齐，旧监测失效' % shipment.code,
                             user_email)
    sd = statistics.stdev(reported) if len(reported) >= 2 else 0.0
    batch = LossBatch(shipment_id=shipment.id, zone=shipment.zone,
                      fingerprint=fingerprint, sample_count=len(samples),
                      reported_count=len(reported), mean_loss=mean_loss, sd=sd,
                      status='active', reason='', confirmed_by=user_email,
                      confirmed_at=now_iso(), idempotency_key=idempotency_key)
    s.add(batch)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        return (s.query(LossBatch)
                .filter_by(shipment_id=shipment.id, fingerprint=fingerprint,
                           status='active').one())
    return batch


def eligible_batches(s, monitor):
    baseline_ids = baseline_shipment_ids(monitor)
    included = {r[0] for r in s.query(MonitorInclusion.batch_id)
                .filter_by(monitor_id=monitor.id).all()}
    rows = (s.query(LossBatch)
            .filter(LossBatch.zone == monitor.zone, LossBatch.status == 'active')
            .order_by(LossBatch.confirmed_at, LossBatch.id).all())
    return [b for b in rows if b.id not in included and b.shipment_id not in baseline_ids]


# ---------------------------------------------------------------- 查看 / 检验

def _p_value(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _finish_look(s, monitor, look, batches):
    """用已落库的 batch_ids 计算，可在中断后按相同输入续跑，不再扣额度。"""
    total_n = 0
    weighted = 0.0
    evidence = []
    for batch in batches:
        try:
            s.refresh(batch)
        except Exception:  # 已删除
            raise HTTPException(409, '批次 %s 已不存在，本次查看中止' % batch.id)
        if batch.status != 'active':
            raise HTTPException(409, '批次 %s 已因样本补改失效，本次查看中止' % batch.id)
        total_n += batch.reported_count
        weighted += batch.mean_loss * batch.reported_count
        evidence.append({'batch_id': batch.id, 'shipment_id': batch.shipment_id,
                         'mean_loss': batch.mean_loss, 'reported': batch.reported_count,
                         'sample_count': batch.sample_count,
                         'fingerprint': batch.fingerprint,
                         'confirmed_at': batch.confirmed_at})
    if total_n <= 0:
        raise HTTPException(422, '没有已上报的货损样本，不能计算')
    new_mean = weighted / total_n
    threshold = float(monitor.threshold)
    sd = float(monitor.baseline_sd or 0.0)
    if sd <= 0:
        variances = [float(b.sd or 0.0) ** 2 for b in batches if (b.reported_count or 0) >= 2]
        sd = math.sqrt(statistics.fmean(variances)) if variances else 0.0
    if sd > 0:
        z = (new_mean - threshold) / (sd / math.sqrt(total_n))
        p = _p_value(z)
    else:
        z = math.inf if new_mean > threshold else 0.0
        p = 0.0 if new_mean > threshold else 1.0
    alert = p <= look.alpha_cost and new_mean > threshold
    look.state = 'done'
    look.decision = 'alert' if alert else 'clear'
    look.new_n = total_n
    look.new_mean = new_mean
    look.statistic = z
    look.p_value = p
    look.completed_at = now_iso()
    if alert:
        s.add(Alert(monitor_id=monitor.id, look_id=look.id, zone=monitor.zone,
                    year=look.year, threshold=threshold, new_mean=new_mean,
                    new_n=total_n, p_value=p, alpha_cost=look.alpha_cost,
                    batch_ids=[b.id for b in batches], created_at=now_iso(),
                    evidence={'threshold_formula': (monitor.baseline or {}).get('formula'),
                              'baseline_n': monitor.baseline_n,
                              'baseline_sd': monitor.baseline_sd,
                              'look_stats': {'z': z, 'p': p,
                                             'per_look_alpha': look.alpha_cost},
                              'batches': evidence,
                              'note': ALERT_WARNING}))
    return look


def _load_batches(s, look):
    batches = [s.get(LossBatch, i) for i in (look.batch_ids or [])]
    return None if any(b is None for b in batches) else batches


def run_look(s, monitor, user_email, idempotency_key=None):
    if monitor.state != 'active':
        raise HTTPException(409, '监测%s：%s' % (
            '已失效' if monitor.state == 'invalidated' else '已停止',
            monitor.reason or '不再接受查看'))
    # 同一监测的查看串行化，避免两个查看同时改动纳入集合
    monitor = (s.query(Monitor).filter_by(id=monitor.id)
               .populate_existing().with_for_update().one())
    # 误报预算按查看所在自然年计，跨年沿用旧监测也会消耗新年度额度
    year = current_year()
    # 显式内容键：同一次查看的重试直接复用，不重复纳入、不重复占额
    if idempotency_key:
        existing = s.query(MonitorLook).filter_by(content_key=idempotency_key).first()
        if existing and existing.monitor_id != monitor.id:
            raise HTTPException(409, '该重试键已用于其他监测')
        if existing:
            batches = _load_batches(s, existing)
            if batches is None:
                existing.state = 'failed'
                existing.error = '续跑时批次不存在'
                s.commit()
                return existing
            if existing.invalidated:
                raise HTTPException(409, '本次查看已因监测失效作废：' + existing.invalidated_reason)
            if existing.state in ('running', 'failed'):
                return _resume(s, monitor, existing, batches)
            return existing
    # 中断续跑：上次进程在扣额并落库后、计算完成前中断，留下 running
    running = (s.query(MonitorLook)
               .filter_by(monitor_id=monitor.id, state='running')
               .order_by(MonitorLook.id.desc()).first())
    if running:
        batches = _load_batches(s, running)
        if batches is None:
            running.state = 'failed'
            running.error = '续跑时批次不存在'
            s.commit()
            return running
        return _resume(s, monitor, running, batches)
    candidates = eligible_batches(s, monitor)
    content_key = idempotency_key or ('auto:%s:%s' % (
        monitor.id,
        hashlib.sha256(_json_key([(b.id, b.fingerprint) for b in candidates])
                       .encode('utf-8')).hexdigest()))
    existing = s.query(MonitorLook).filter_by(content_key=content_key).first()
    if existing:
        return existing
    if not candidates:
        raise HTTPException(409, '暂无新增「已收齐」班次可查看；请先确认后续班次货损收齐')

    # 重新锁定每批样本并校验指纹，绝不读取补改中的半批
    for batch in candidates:
        samples = (s.query(Sample).filter_by(shipment_id=batch.shipment_id)
                   .populate_existing().with_for_update()
                   .order_by(Sample.id).all())
        if sample_fingerprint(samples) != batch.fingerprint:
            batch.status = 'stale'
            batch.reason = '样本在收齐确认后又被补改'
            batch.superseded_at = now_iso()
            invalidate_for_batch(s, batch,
                                 '已纳入班次 %s 的样本补改，旧监测失效' % batch.shipment_id,
                                 user_email)
            s.commit()
            raise HTTPException(409, '班次 %s 的样本在收齐后又被补改，旧监测已失效；'
                                     '请重新确认收齐后再查看' % batch.shipment_id)

    look = MonitorLook(monitor_id=monitor.id, year=year, content_key=content_key,
                       state='running', batch_ids=[b.id for b in candidates],
                       created_by=user_email, created_at=now_iso())
    s.add(look)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        existing = s.query(MonitorLook).filter_by(content_key=content_key).first()
        if not existing:
            raise HTTPException(409, '同一次查看正在处理中，请稍后重试')
        return existing
    try:
        cost = charge_budget(s, monitor.zone, year)
    except Exception:
        s.rollback()
        raise
    look.alpha_cost = cost
    for batch in candidates:
        s.add(MonitorInclusion(monitor_id=monitor.id, batch_id=batch.id,
                               look_id=look.id, fingerprint=batch.fingerprint,
                               included_at=now_iso()))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        existing = s.query(MonitorLook).filter_by(content_key=content_key).first()
        if existing:
            return existing
        raise HTTPException(409, '同一次查看正在处理中，请稍后重试')
    return _resume(s, monitor, look, candidates)


def _resume(s, monitor, look, batches):
    try:
        _finish_look(s, monitor, look, batches)
        s.commit()
    except HTTPException as exc:
        look.state = 'failed'
        look.error = str(exc.detail)
        s.commit()
        raise
    except Exception as exc:  # 保留额度占用，允许同键续跑
        s.rollback()
        look = s.get(MonitorLook, look.id)
        look.state = 'failed'
        look.error = str(exc)
        s.commit()
        raise
    return look


def stop_monitor(s, monitor, reason=''):
    monitor = (s.query(Monitor).filter_by(id=monitor.id)
               .populate_existing().with_for_update().one())
    if monitor.state != 'active':
        raise HTTPException(409, '只有进行中的监测可以停止；已停止或失效不退额度')
    monitor.state = 'stopped'
    monitor.reason = reason or '人工停止监测'
    monitor.stopped_at = now_iso()
    s.flush()
    return monitor


# ---------------------------------------------------------------- 视图

def _sample_missing(s, shipment_id):
    rows = s.query(Sample).filter_by(shipment_id=shipment_id).all()
    return {'sample_count': len(rows),
            'reported': sum(1 for r in rows if r.loss is not None),
            'missing': sum(1 for r in rows if r.loss is None)}


def monitor_view(s, monitor, deep=True):
    baseline = monitor.baseline or {}
    included = {r[0] for r in s.query(MonitorInclusion.batch_id)
                .filter_by(monitor_id=monitor.id).all()}
    active_batches = (s.query(LossBatch)
                      .filter(LossBatch.zone == monitor.zone,
                              LossBatch.status == 'active').all())
    baseline_ids = baseline_shipment_ids(monitor)
    # 待监测 = 已收齐但尚未纳入任何查看的后续班次
    pending = [b for b in active_batches
               if b.id not in included and b.shipment_id not in baseline_ids]
    unreported = []
    awaiting = []
    for ship in (s.query(Shipment).filter_by(zone=monitor.zone)
                 .order_by(Shipment.id).all()):
        if ship.id in baseline_ids:
            continue
        if any(b.shipment_id == ship.id for b in active_batches):
            continue
        rows = s.query(Sample).filter_by(shipment_id=ship.id).all()
        reported = [r.loss for r in rows if r.loss is not None]
        missing = len(rows) - len(reported)
        if not rows:
            unreported.append({'shipment_id': ship.id, 'code': ship.code,
                               'reported': 0, 'missing': 0,
                               'reason': '尚未录入样本，不能确认货损收齐'})
        elif missing:
            unreported.append({'shipment_id': ship.id, 'code': ship.code,
                               'reported': len(reported), 'missing': missing,
                               'reason': '样本未收齐或货损缺报，不能确认收齐；缺报不当零'})
        else:
            awaiting.append({'shipment_id': ship.id, 'code': ship.code,
                             'reported': len(reported),
                             'mean_loss': statistics.fmean(reported)})
    alerts = (s.query(Alert).filter_by(monitor_id=monitor.id)
              .order_by(Alert.id.desc()).all())
    if monitor.state == 'invalidated':
        phase = 'invalidated'
    elif monitor.state == 'stopped':
        phase = 'stopped'
    elif any(not a.invalidated for a in alerts):
        phase = 'alert'
    elif s.query(MonitorInclusion).filter_by(monitor_id=monitor.id).count():
        phase = 'monitoring'
    else:
        phase = 'pending'
    view = {
        'baseline_batches_excluded': len([b for b in active_batches
                                          if b.shipment_id in baseline_ids]),
        'id': monitor.id, 'study_id': monitor.study_id,
        'recommendation_id': monitor.recommendation_id, 'zone': monitor.zone,
        'year': monitor.year, 'state': monitor.state, 'phase': phase,
        'reason': monitor.reason, 'threshold': monitor.threshold,
        'baseline_n': monitor.baseline_n, 'baseline_sd': monitor.baseline_sd,
        'baseline': baseline, 'created_at': monitor.created_at,
        'created_by': monitor.created_by, 'stopped_at': monitor.stopped_at,
        'replaced_by': monitor.replaced_by, 'source_monitor_id': monitor.source_monitor_id,
        'pending_count': len(pending) + len(unreported) + len(awaiting),
        'pending': [{'batch_id': b.id, 'shipment_id': b.shipment_id,
                     'mean_loss': b.mean_loss, 'reported': b.reported_count,
                     'confirmed_at': b.confirmed_at} for b in pending],
        'unreported': unreported,
        'awaiting': awaiting,
        'awaiting_count': len(awaiting),
        'waiting_count': len(pending),
        'included_count': len(included),
    }
    if deep:
        looks = (s.query(MonitorLook).filter_by(monitor_id=monitor.id)
                 .order_by(MonitorLook.id.desc()).all())
        view['looks'] = [{'id': r.id, 'year': r.year, 'state': r.state,
                          'decision': r.decision,
                          'alpha_cost': r.alpha_cost, 'new_n': r.new_n,
                          'new_mean': r.new_mean, 'statistic': r.statistic,
                          'p_value': r.p_value, 'batch_ids': r.batch_ids,
                          'error': r.error, 'invalidated': r.invalidated,
                          'invalidated_reason': r.invalidated_reason,
                          'created_at': r.created_at} for r in looks]
        view['alerts'] = [{'id': a.id, 'threshold': a.threshold,
                           'new_mean': a.new_mean, 'new_n': a.new_n,
                           'p_value': a.p_value, 'alpha_cost': a.alpha_cost,
                           'batch_ids': a.batch_ids, 'created_at': a.created_at,
                           'invalidated': a.invalidated,
                           'invalidated_reason': a.invalidated_reason,
                           'evidence': a.evidence} for a in alerts]
    # 额度按当前自然年展示；跨年查看会从新年度池中支取
    view['budget'] = budget_view(peek_budget(s, monitor.zone, current_year()))
    view['budget']['used_by_this'] = round(
        sum(float(r.alpha_cost or 0) for r in
            s.query(MonitorLook).filter_by(monitor_id=monitor.id).all()), 6)
    return view


def overview(s):
    zones = []
    for zone in ('冷藏', '冷冻', '常温对照'):
        budget = peek_budget(s, zone)
        active = (s.query(Monitor).filter_by(zone=zone, state='active')
                  .order_by(Monitor.id).all())
        alerts = (s.query(Alert).filter_by(zone=zone, invalidated=False,
                                           year=current_year())
                  .order_by(Alert.id.desc()).all())
        zones.append({'zone': zone, 'budget': budget_view(budget),
                      'active_monitors': [monitor_view(s, m, deep=False)
                                          for m in active],
                      'alert_count': len(alerts),
                      'latest_alert': ({'id': alerts[0].id,
                                        'monitor_id': alerts[0].monitor_id,
                                        'new_mean': alerts[0].new_mean,
                                        'threshold': alerts[0].threshold,
                                        'created_at': alerts[0].created_at}
                                       if alerts else None)})
    return {'year': current_year(), 'zones': zones, 'tradeoff': ALERT_WARNING}
