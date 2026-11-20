from sqlalchemy import Column,Integer,String,Float,JSON,Text,Boolean,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True);password=Column(String(200));role=Column(String(20))
class Shipment(Base):
 __tablename__='shipments'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True);route=Column(String(60));zone=Column(String(20));driver=Column(String(60))
class Sample(Base):
 __tablename__='samples'
 id=Column(Integer,primary_key=True);shipment_id=Column(Integer,index=True);code=Column(String(60),unique=True);exposure=Column(Float,nullable=True);excursions=Column(Integer,nullable=True);loss=Column(Float,nullable=True)
class Study(Base):
 __tablename__='studies'
 id=Column(Integer,primary_key=True);name=Column(String(100));zone=Column(String(20));feature=Column(String(30));state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
class Recommendation(Base):
 __tablename__='recommendations'
 id=Column(Integer,primary_key=True);study_id=Column(Integer);note=Column(Text);owner=Column(String(60));status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))

# --- 货损监测（建议签核后持续复核） ---
class LossBatch(Base):
 """一个班次「货损收齐」的不可变确认快照。样本补改后旧快照作废，需重新确认。"""
 __tablename__='loss_batches'
 id=Column(Integer,primary_key=True)
 shipment_id=Column(Integer,index=True,nullable=False)
 zone=Column(String(20),index=True)
 fingerprint=Column(String(64),nullable=False)
 sample_count=Column(Integer,default=0)
 reported_count=Column(Integer,default=0)
 mean_loss=Column(Float,nullable=False)
 sd=Column(Float,default=0.0)
 status=Column(String(20),default='active',index=True)
 reason=Column(Text,default='')
 confirmed_by=Column(String(100),default='')
 confirmed_at=Column(String(40),index=True)
 superseded_at=Column(String(40),nullable=True)
 idempotency_key=Column(String(80),unique=True,nullable=True)
class ZoneBudget(Base):
 """温区 × 年度共享误报预算账本；查看一次扣一次，停止/失效不退款。"""
 __tablename__='zone_budgets'
 id=Column(Integer,primary_key=True)
 zone=Column(String(20),nullable=False)
 year=Column(Integer,nullable=False)
 alpha_total=Column(Float,default=0.05)
 alpha_spent=Column(Float,default=0.0)
 look_cost=Column(Float,default=0.001)
 look_cap=Column(Integer,default=50)
 looks_used=Column(Integer,default=0)
 created_at=Column(String(40))
 updated_at=Column(String(40))
 __table_args__=(UniqueConstraint('zone','year',name='uq_zone_year'),)
class Monitor(Base):
 """一次持续监测。失效后由新监测接续；旧监测的证据不被改写，只标记失效原因。"""
 __tablename__='monitors'
 id=Column(Integer,primary_key=True)
 study_id=Column(Integer,index=True)
 recommendation_id=Column(Integer,index=True,nullable=True)
 zone=Column(String(20),index=True)
 year=Column(Integer,index=True)
 state=Column(String(20),default='active',index=True)
 reason=Column(Text,default='')
 baseline=Column(JSON,nullable=False)
 threshold=Column(Float,nullable=True)
 baseline_n=Column(Integer,default=0)
 baseline_sd=Column(Float,default=0.0)
 window_start=Column(String(40))
 created_by=Column(String(100),default='')
 created_at=Column(String(40))
 stopped_at=Column(String(40),nullable=True)
 replaced_by=Column(Integer,nullable=True)
 source_monitor_id=Column(Integer,nullable=True)
class MonitorLook(Base):
 """一次「查看」。内容键保证同次重试不重复纳入班次，也不重复扣额度。"""
 __tablename__='monitor_looks'
 id=Column(Integer,primary_key=True)
 monitor_id=Column(Integer,index=True)
 year=Column(Integer,index=True)
 content_key=Column(String(120),unique=True)
 state=Column(String(20),default='running',index=True)
 decision=Column(String(20),default='')
 alpha_cost=Column(Float,default=0.0)
 batch_ids=Column(JSON,nullable=False)
 new_n=Column(Integer,default=0)
 new_mean=Column(Float,nullable=True)
 statistic=Column(Float,nullable=True)
 p_value=Column(Float,nullable=True)
 error=Column(Text,default='')
 invalidated=Column(Boolean,default=False)
 invalidated_reason=Column(Text,default='')
 created_by=Column(String(100),default='')
 created_at=Column(String(40))
 completed_at=Column(String(40),nullable=True)
class MonitorInclusion(Base):
 """查看与批次的对应关系；唯一约束保证同批只纳入同一监测一次。"""
 __tablename__='monitor_inclusions'
 id=Column(Integer,primary_key=True)
 monitor_id=Column(Integer,index=True)
 batch_id=Column(Integer,index=True)
 look_id=Column(Integer,index=True)
 fingerprint=Column(String(64))
 included_at=Column(String(40))
 __table_args__=(UniqueConstraint('monitor_id','batch_id',name='uq_monitor_batch'),)
class Alert(Base):
 """预警证据。失效只追加原因，不删除、不改写原始统计值。"""
 __tablename__='alerts'
 id=Column(Integer,primary_key=True)
 monitor_id=Column(Integer,index=True)
 look_id=Column(Integer,unique=True)
 zone=Column(String(20),index=True)
 year=Column(Integer,index=True)
 threshold=Column(Float)
 new_mean=Column(Float)
 new_n=Column(Integer)
 p_value=Column(Float)
 alpha_cost=Column(Float)
 batch_ids=Column(JSON)
 evidence=Column(JSON)
 created_at=Column(String(40))
 invalidated=Column(Boolean,default=False)
 invalidated_reason=Column(Text,default='')
 invalidated_at=Column(String(40),nullable=True)
