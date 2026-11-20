# gb-3102 货损监测验证记录（2026-11-20）

环境：本机无 Docker/MySQL/Redis，用 SQLite 临时库跑领域层；MySQL 行锁并发用例需 `TEST_MYSQL_URL` 才会执行（当前 skipped）。

## 覆盖点与结果

| 要求 | 用例 |
|---|---|
| 警戒线 = 研究内各班次平均货损的均值 | test_threshold_is_mean_of_shipment_means、test_threshold_uses_per_shipment_means_not_pooled |
| 原研究不变 | test_original_study_is_not_mutated、test_read_views_do_not_create_budget_rows |
| 后续班次收齐后聚合、缺报不当零 | test_missing_reports_are_not_zero、test_confirm_rejects_batch_with_no_reported_loss、test_later_shipments_are_pooled_across_batches、test_second_look_pools_only_new_batches |
| 每温区全年共用 5%、任意查看/提前停止守住上限 | test_budget_is_shared_per_zone_and_year、test_per_zone_false_alarm_rate_within_five_percent、test_more_looks_make_each_look_stricter |
| 年中新增监测共享剩余额度 | test_midyear_new_monitor_uses_remaining_budget |
| 补改后旧监测失效、新监测另占额度、旧预警留存依据、停止不退额度 | test_sample_edit_invalidates_old_monitor_and_keeps_alert_evidence、test_stop_and_invalidation_do_not_refund、test_replacement_requires_reconfirmation、test_shipment_zone_change_invalidates_monitor |
| 收齐与补改并发不读半批 | test_batch_confirmed_after_edit_reads_whole_batch、test_look_aborts_if_batch_changed_after_confirmation、test_concurrent_looks_serialize_on_mysql（MySQL 专用） |
| 中断可续跑、同次重试不重复纳入/占额 | test_confirm_is_idempotent_per_key、test_retry_of_same_look_does_not_double_charge_or_reinclude、test_interrupted_look_resumes_without_new_charge |
| 页面状态：待监测/进行中/预警/失效原因 | test_phase_transitions_in_view、test_clear_then_alert_keeps_history、test_unreported_shipments_are_visible |

## 运行

```sh
cd backend && DATABASE_URL=sqlite:////tmp/t.db python -m pytest tests -q
# 27 passed, 1 skipped
```

API 冒烟（TestClient + 角色权限、签核幂等、查看幂等、失效接续、停止）已跑通；前端页面用 Node 做了语法检查与渲染快照。

## 已知边界

- 误报预算按「单次查看额度 = 5% / 查看上限」扣减，属于保守的 Bonferroni 型核算：任何一次查看、提前停止、失效都计入，全年最多消耗 5%。
- 查看的统计量为单边 z 检验（新均值高于警戒线）；真实环境 MySQL 用 `SELECT ... FOR UPDATE` 串行化同一监测的查看与样本补改。
