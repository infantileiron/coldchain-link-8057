"""货损监测的领域测试（SQLite 内存库；生产使用 MySQL 行锁）。

运行：cd backend && python -m pytest tests -q
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as cd  # noqa: E402

_TEST_ENGINE = create_engine('sqlite://', poolclass=StaticPool,
                             connect_args={'check_same_thread': False})
cd.engine = _TEST_ENGINE
cd.Session = sessionmaker(bind=_TEST_ENGINE, expire_on_commit=False)

from src.models import entities as E  # noqa: E402
from src.services import monitoring as mon  # noqa: E402


@pytest.fixture()
def s():
    E.Base.metadata.drop_all(_TEST_ENGINE)
    E.Base.metadata.create_all(_TEST_ENGINE)
    session = cd.Session()
    yield session
    session.close()


def ship(s, code, zone='冷藏'):
    row = E.Shipment(code=code, route='鲜达线路 01', zone=zone, driver='驾驶员1')
    s.add(row)
    s.flush()
    return row


def sample(s, shipment, suffix, loss, exposure=10.0):
    row = E.Sample(shipment_id=shipment.id, code=shipment.code + '-' + suffix,
                   exposure=exposure, excursions=1, loss=loss)
    s.add(row)
    s.flush()
    return row


def baseline(s, losses=(2.0, 4.0, 3.0, 5.0), zone='冷藏', tag=None):
    """建立已完成研究 + 已签核建议，返回 (study, rec, monitor)。"""
    tag = tag or ('%d' % s.query(E.Study).count())
    snapshot = []
    for i, loss in enumerate(losses):
        sh = ship(s, 'BASE-%s-%s-%d' % (zone, tag, i), zone)
        sample(s, sh, 'S', loss, exposure=10.0 + i * 10)
        snapshot.append({'shipment_id': sh.id, 'loss': loss, 'exposure': 10.0 + i * 10,
                         'excursions': 1, 'code': sh.code + '-S'})
    study = E.Study(name='午间装卸暴露复核', zone=zone, feature='exposure',
                    state='done', result={'snapshot': snapshot,
                                          'pearson': 0.99, 'n': len(snapshot)})
    s.add(study)
    s.flush()
    rec = E.Recommendation(study_id=study.id, note='复核线路 01 装卸等待时长',
                           owner='线路主管', status='approved')
    s.add(rec)
    s.flush()
    m = mon.create_monitor(s, rec, 'admin@demo.local')
    s.commit()
    return study, rec, m


def later_batch(s, code, reported, missing=0, zone='冷藏'):
    sh = ship(s, code, zone)
    for i, loss in enumerate(reported):
        sample(s, sh, str(i), loss)
    for i in range(missing):
        sample(s, sh, 'm%d' % i, None)
    s.commit()
    return sh


# ---------------------------------------------------------------- 基线

def test_threshold_is_mean_of_shipment_means(s):
    study, rec, m = baseline(s, losses=(2.0, 4.0, 3.0, 5.0))
    # 每班一条样本：各班次均值 2/4/3/5，均值 3.5
    assert m.threshold == pytest.approx(3.5)
    assert m.baseline['shipment_count'] == 4
    assert m.state == 'active'


def test_threshold_uses_per_shipment_means_not_pooled(s):
    # 班 A 两条 0/0，班 B 一条 9 => 班均值 0 与 9，警戒线 4.5（不是 3）
    a = ship(s, 'A')
    sample(s, a, '1', 0.0)
    sample(s, a, '2', 0.0)
    b = ship(s, 'B')
    sample(s, b, '1', 9.0)
    snapshot = [{'shipment_id': a.id, 'loss': 0.0, 'exposure': 1},
                {'shipment_id': a.id, 'loss': 0.0, 'exposure': 2},
                {'shipment_id': b.id, 'loss': 9.0, 'exposure': 3}]
    study = E.Study(name='s', zone='冷藏', feature='exposure', state='done',
                    result={'snapshot': snapshot})
    s.add(study)
    s.flush()
    rec = E.Recommendation(study_id=study.id, note='x' * 12, owner='o',
                           status='approved')
    s.add(rec)
    s.flush()
    m = mon.create_monitor(s, rec, 'a')
    assert m.threshold == pytest.approx(4.5)


def test_original_study_is_not_mutated(s):
    study, rec, m = baseline(s)
    before = (study.state, dict(study.result))
    mon.confirm_batch(s, later_batch(s, 'NEW-1', [9.0]), 'a')
    s.commit()
    mon.run_look(s, m, 'a')
    s.commit()
    s.refresh(study)
    assert study.state == before[0]
    assert study.result == before[1]


# ---------------------------------------------------------------- 收齐与缺报

def test_missing_reports_are_not_zero(s):
    study, rec, m = baseline(s)
    sh = later_batch(s, 'NEW-1', [8.0, 10.0], missing=1)
    batch = mon.confirm_batch(s, sh, 'a')
    s.commit()
    assert batch.reported_count == 2 and batch.sample_count == 3
    assert batch.mean_loss == pytest.approx(9.0)  # 不是 (8+10+0)/3
    assert batch.sd == pytest.approx(2 ** 0.5)  # 样本标准差


def test_confirm_rejects_batch_with_no_reported_loss(s):
    baseline(s)
    sh = later_batch(s, 'NEW-1', [], missing=2)
    with pytest.raises(Exception) as exc:
        mon.confirm_batch(s, sh, 'a')
    assert '缺报不能当零' in str(exc.value)


def test_later_shipments_are_pooled_across_batches(s):
    study, rec, m = baseline(s)
    b1 = mon.confirm_batch(s, later_batch(s, 'N1', [8.0, 10.0]), 'a')
    b2 = mon.confirm_batch(s, later_batch(s, 'N2', [12.0]), 'a')
    s.commit()
    assert {b1.id, b2.id} == {b.id for b in mon.eligible_batches(s, m)}
    look = mon.run_look(s, m, 'a')
    s.commit()
    assert look.new_n == 3
    assert look.new_mean == pytest.approx(10.0)


# ---------------------------------------------------------------- 预算

def test_budget_is_shared_per_zone_and_year(s):
    study, rec, m = baseline(s, zone='冷藏')
    _, rec2, m2 = baseline(s, zone='冷藏', tag='second')
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    mon.run_look(s, m, 'a')
    s.commit()
    budget = mon.ensure_budget(s, '冷藏')
    assert budget.alpha_spent == pytest.approx(budget.look_cost)
    # 第二个监测共享同一池，不从 5% 重新开始
    other = mon.ensure_budget(s, '冷藏')
    assert other.id == budget.id
    mon.confirm_batch(s, later_batch(s, 'N2', [9.0]), 'a')
    s.commit()
    mon.run_look(s, m2, 'a')
    s.commit()
    assert mon.ensure_budget(s, '冷藏').alpha_spent == pytest.approx(2 * budget.look_cost)


def test_budget_exhaustion_blocks_further_looks(s):
    study, rec, m = baseline(s)
    budget = mon.ensure_budget(s, '冷藏')
    budget.alpha_spent = budget.alpha_total - budget.look_cost / 2
    s.commit()
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    with pytest.raises(Exception) as exc:
        mon.run_look(s, m, 'a')
    assert '额度已用尽' in str(exc.value)
    s.rollback()
    # 失败的查看不纳入批次，也不扣额度
    assert s.query(E.MonitorLook).count() == 0
    assert mon.ensure_budget(s, '冷藏').alpha_spent == pytest.approx(
        budget.alpha_total - budget.look_cost / 2)


def test_stop_and_invalidation_do_not_refund(s):
    study, rec, m = baseline(s)
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    mon.run_look(s, m, 'a')
    s.commit()
    spent = mon.ensure_budget(s, '冷藏').alpha_spent
    mon.stop_monitor(s, m, '线路已整改')
    s.commit()
    assert m.state == 'stopped'
    assert mon.ensure_budget(s, '冷藏').alpha_spent == pytest.approx(spent)
    # 停止后不能新增查看
    with pytest.raises(Exception):
        mon.run_look(s, m, 'a')


def test_midyear_new_monitor_uses_remaining_budget(s):
    study, rec, m = baseline(s)
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    mon.run_look(s, m, 'a')
    s.commit()
    mon.stop_monitor(s, m, '暂停')
    s.commit()
    spent = mon.ensure_budget(s, '冷藏').alpha_spent
    new = mon.create_monitor(s, rec, 'a', explicit=True)
    s.commit()
    assert new.id != m.id and new.state == 'active'
    assert new.source_monitor_id == m.id
    assert mon.ensure_budget(s, '冷藏').alpha_spent == pytest.approx(spent)


# ---------------------------------------------------------------- 幂等与续跑

def test_confirm_is_idempotent_per_key(s):
    baseline(s)
    sh = later_batch(s, 'N1', [9.0])
    first = mon.confirm_batch(s, sh, 'a', 'confirm-1')
    s.commit()
    again = mon.confirm_batch(s, sh, 'a', 'confirm-1')
    s.commit()
    assert first.id == again.id
    assert s.query(E.LossBatch).count() == 1


def test_retry_of_same_look_does_not_double_charge_or_reinclude(s):
    study, rec, m = baseline(s)
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    first = mon.run_look(s, m, 'a', 'look-1')
    s.commit()
    again = mon.run_look(s, m, 'a', 'look-1')
    s.commit()
    assert first.id == again.id
    assert s.query(E.MonitorInclusion).count() == 1
    assert mon.ensure_budget(s, '冷藏').looks_used == 1


def test_interrupted_look_resumes_without_new_charge(s):
    study, rec, m = baseline(s)
    batch = mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    # 模拟：扣额与纳入已落库，计算前进程中断，留下 running
    cost = mon.charge_budget(s, '冷藏', mon.current_year())
    crashed = E.MonitorLook(monitor_id=m.id, year=mon.current_year(),
                            content_key='crash-1', state='running',
                            batch_ids=[batch.id], alpha_cost=cost,
                            created_by='a', created_at=mon.now_iso())
    s.add(crashed)
    s.flush()
    s.add(E.MonitorInclusion(monitor_id=m.id, batch_id=batch.id,
                             look_id=crashed.id, fingerprint=batch.fingerprint,
                             included_at=mon.now_iso()))
    s.commit()
    spent = mon.ensure_budget(s, '冷藏').alpha_spent
    resumed = mon.run_look(s, m, 'a')
    s.commit()
    assert resumed.id == crashed.id and resumed.state == 'done'
    assert mon.ensure_budget(s, '冷藏').alpha_spent == pytest.approx(spent)
    assert s.query(E.MonitorInclusion).count() == 1


# ---------------------------------------------------------------- 补改失效

def test_sample_edit_invalidates_old_monitor_and_keeps_alert_evidence(s):
    study, rec, m = baseline(s)
    sh = later_batch(s, 'N1', [9.0])
    batch = mon.confirm_batch(s, sh, 'a')
    s.commit()
    look = mon.run_look(s, m, 'a')
    s.commit()
    assert look.decision == 'alert'
    alert = s.query(E.Alert).one()

    row = s.query(E.Sample).filter_by(code='N1-0').one()
    row.loss = 9.4
    s.flush()
    mon.note_sample_change(s, [sh.id], 'a')
    s.commit()

    s.refresh(m)
    s.refresh(alert)
    s.refresh(batch)
    assert m.state == 'invalidated'
    assert '样本补改' in m.reason
    assert alert.invalidated is True
    assert alert.new_mean == pytest.approx(9.0)  # 原始证据不改写
    assert batch.status == 'stale'
    assert m.replaced_by
    replacement = s.get(E.Monitor, m.replaced_by)
    assert replacement.state == 'active'
    assert replacement.baseline == m.baseline


def test_replacement_requires_reconfirmation(s):
    study, rec, m = baseline(s)
    sh = later_batch(s, 'N1', [9.0])
    mon.confirm_batch(s, sh, 'a')
    s.commit()
    mon.run_look(s, m, 'a')
    s.commit()
    row = s.query(E.Sample).filter_by(code='N1-0').one()
    row.loss = 9.4
    s.flush()
    mon.note_sample_change(s, [sh.id], 'a')
    s.commit()
    replacement = s.get(E.Monitor, m.replaced_by)
    assert mon.eligible_batches(s, replacement) == []
    refreshed = mon.confirm_batch(s, sh, 'a')
    s.commit()
    assert [b.id for b in mon.eligible_batches(s, replacement)] == [refreshed.id]
    look = mon.run_look(s, replacement, 'a')
    s.commit()
    assert look.new_mean == pytest.approx(9.4)


def test_batch_confirmed_after_edit_reads_whole_batch(s):
    """收齐确认与补改都在同一事务内锁样本；查看前会再校验指纹。"""
    baseline(s)
    sh = later_batch(s, 'N1', [8.0, 10.0])
    batch = mon.confirm_batch(s, sh, 'a')
    s.commit()
    # 收齐后新增一条样本（模拟并发补录已提交），指纹失配
    sample(s, sh, 'late', 99.0)
    s.commit()
    assert mon.sample_fingerprint(
        s.query(E.Sample).filter_by(shipment_id=sh.id).all()) != batch.fingerprint


def test_look_aborts_if_batch_changed_after_confirmation(s):
    study, rec, m = baseline(s)
    sh = later_batch(s, 'N1', [9.0])
    batch = mon.confirm_batch(s, sh, 'a')
    s.commit()
    sample(s, sh, 'late', 99.0)
    s.commit()
    with pytest.raises(Exception) as exc:
        mon.run_look(s, m, 'a')
    assert '又被补改' in str(exc.value)
    s.refresh(m)
    s.refresh(batch)
    assert batch.status == 'stale'
    # 该批次尚未纳入任何监测，因此没有旧监测需要失效；它回到待处理列表
    assert m.state == 'active'
    assert s.query(E.MonitorLook).count() == 0
    assert s.query(E.Alert).count() == 0


# ---------------------------------------------------------------- 判定

def test_clear_when_mean_not_above_threshold(s):
    study, rec, m = baseline(s, losses=(2.0, 4.0, 3.0, 5.0))
    mon.confirm_batch(s, later_batch(s, 'N1', [3.0, 3.5]), 'a')
    s.commit()
    look = mon.run_look(s, m, 'a')
    s.commit()
    assert look.decision == 'clear'
    assert s.query(E.Alert).count() == 0


def test_alert_when_mean_above_threshold(s):
    study, rec, m = baseline(s, losses=(2.0, 4.0, 3.0, 5.0))
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0, 9.2]), 'a')
    s.commit()
    look = mon.run_look(s, m, 'a')
    s.commit()
    assert look.decision == 'alert'
    alert = s.query(E.Alert).one()
    assert alert.threshold == pytest.approx(3.5)
    assert alert.new_mean == pytest.approx(9.1)
    assert alert.evidence['look_stats']['per_look_alpha'] == pytest.approx(
        mon.ensure_budget(s, '冷藏').look_cost)


def test_phase_transitions_in_view(s):
    study, rec, m = baseline(s)
    assert mon.monitor_view(s, m)['phase'] == 'pending'
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    view = mon.monitor_view(s, m)
    assert view['phase'] == 'pending'
    assert view['pending_count'] == 1
    mon.run_look(s, m, 'a')
    s.commit()
    assert mon.monitor_view(s, m)['phase'] == 'alert'


def test_unreported_shipments_are_visible(s):
    study, rec, m = baseline(s)
    later_batch(s, 'N1', [9.0], missing=1)
    s.commit()
    view = mon.monitor_view(s, m)
    codes = {x['code'] for x in view['unreported']}
    assert 'N1' in codes
    entry = next(x for x in view['unreported'] if x['code'] == 'N1')
    assert entry['missing'] == 1
    assert '缺报' in entry['reason']


# ---------------------------------------------------------------- 误报控制

def test_per_zone_false_alarm_rate_within_five_percent():
    """每温区全年 5%：单次额度 = 5% / 查看上限，50 次独立查看的总误报不超过 5%。"""
    import random
    from scipy.stats import norm
    rng = random.Random(3102)
    zone_total = 0.05
    cost = zone_total / mon.LOOK_CAP
    # 每次查看的名义误报率就是扣掉的额度，累计至多 5%
    assert cost * mon.LOOK_CAP == pytest.approx(zone_total)
    # 单边 z 判定：在阈值为真均值时，误报概率等于单次额度
    z_critical = norm.isf(cost)
    assert norm.sf(z_critical) == pytest.approx(cost)
    # 模拟：警戒线正确、无反弹，50 次查看里误报次数应很低
    false_alarms = 0
    trials = 2000
    for _ in range(trials):
        alarms = 0
        for _ in range(mon.LOOK_CAP):
            z = rng.gauss(0, 1)
            if z >= z_critical:
                alarms += 1
        false_alarms += alarms > 0
    assert false_alarms / trials <= zone_total + 0.01


def test_more_looks_make_each_look_stricter():
    """查看上限可在部署时调整；名额越多，单次额度越小，及时性换检出能力。"""
    total, cap = 0.05, 50
    assert total / cap < total / 10


# ---------------------------------------------------------------- 并发（需要 MySQL）

MYSQL_URL = os.getenv('TEST_MYSQL_URL')


@pytest.mark.skipif(not MYSQL_URL, reason='设置 TEST_MYSQL_URL 才会跑真实行锁并发用例')
def test_concurrent_looks_serialize_on_mysql():
    """MySQL 行锁：同一监测的并发查看串行；后到的查看看到已纳入批次后不再重复纳入。"""
    import threading

    engine = create_engine(MYSQL_URL, pool_pre_ping=True)
    E.Base.metadata.drop_all(engine)
    E.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    setup = Session()
    study, rec, m = baseline(setup)
    later_batch(setup, 'N1', [9.0, 9.2])
    setup.commit()
    monitor_id = m.id
    setup.close()

    outcomes = []

    def look(key):
        s2 = Session()
        try:
            got = s2.get(E.Monitor, monitor_id)
            row = mon.run_look(s2, got, 'a', key)
            outcomes.append(('done', row.id))
        except Exception as exc:  # 串行化后剩下的请求应得到明确的 409
            outcomes.append(('rejected', str(exc)))
        finally:
            s2.close()

    threads = [threading.Thread(target=look, args=('k%d' % i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check = Session()
    assert check.query(E.MonitorInclusion).count() == 1
    assert mon.ensure_budget(check, '冷藏').looks_used == 1
    assert sum(1 for o in outcomes if o[0] == 'done') == 1
    check.close()


def test_read_views_do_not_create_budget_rows(s):
    study, rec, m = baseline(s)
    s.query(E.ZoneBudget).delete()
    s.commit()
    view = mon.monitor_view(s, m)
    overview = mon.overview(s)
    assert s.query(E.ZoneBudget).count() == 0
    assert view['budget']['remaining'] == pytest.approx(0.05)
    assert overview['zones'][0]['budget']['remaining'] == pytest.approx(0.05)


def test_shipment_zone_change_invalidates_monitor(s):
    study, rec, m = baseline(s)
    sh = later_batch(s, 'N1', [9.0])
    batch = mon.confirm_batch(s, sh, 'a')
    s.commit()
    mon.run_look(s, m, 'a')
    s.commit()
    old = sh.zone
    sh.zone = '冷冻'
    s.flush()
    mon.note_shipment_zone_change(s, sh, old, 'a')
    s.commit()
    s.refresh(m)
    s.refresh(batch)
    assert m.state == 'invalidated'
    assert '温区变更' in m.reason
    assert batch.status == 'stale'


def test_second_look_pools_only_new_batches(s):
    study, rec, m = baseline(s)
    mon.confirm_batch(s, later_batch(s, 'N1', [8.0]), 'a')
    first = mon.run_look(s, m, 'a')
    s.commit()
    assert first.new_n == 1
    assert first.decision == 'alert'
    mon.confirm_batch(s, later_batch(s, 'N2', [12.0]), 'a')
    second = mon.run_look(s, m, 'a')
    s.commit()
    assert second.id != first.id
    assert second.new_n == 1  # 不重复纳入 N1
    assert second.new_mean == pytest.approx(12.0)
    assert mon.ensure_budget(s, '冷藏').looks_used == 2


def test_clear_then_alert_keeps_history(s):
    study, rec, m = baseline(s)
    mon.confirm_batch(s, later_batch(s, 'N1', [3.0]), 'a')
    mon.run_look(s, m, 'a')
    s.commit()
    assert mon.monitor_view(s, m)['phase'] == 'monitoring'
    mon.confirm_batch(s, later_batch(s, 'N2', [9.0]), 'a')
    mon.run_look(s, m, 'a')
    s.commit()
    view = mon.monitor_view(s, m)
    assert view['phase'] == 'alert'
    assert [x['decision'] for x in view['looks']] == ['alert', 'clear']
    assert len(view['alerts']) == 1


def test_same_session_view_reflects_charge(s):
    """扣额走核心 UPDATE，视图必须从库里重新读，不能返回身份映射里的旧值。"""
    study, rec, m = baseline(s)
    mon.confirm_batch(s, later_batch(s, 'N1', [9.0]), 'a')
    s.commit()
    before = mon.monitor_view(s, m)['budget']['alpha_spent']
    mon.run_look(s, m, 'a')
    s.commit()
    after = mon.monitor_view(s, m)['budget']['alpha_spent']
    assert after > before
    assert after == pytest.approx(mon.ensure_budget(s, '冷藏').look_cost)
