import os,json
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Shipment,Sample,Study,Recommendation,Audit,LossBatch,Monitor
from src.schemas.business import Login,ShipmentInput,SampleInput,StudyInput,RecommendationInput,BatchInput,LookInput,StopInput,MonitorInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.controllers.business import ensure_shipment,ensure_study,study_rows
from src.services import monitoring as mon
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/shipments')
def ships(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Shipment).all()]
@router.post('/shipments')
def ship(data:ShipmentInput,u=Depends(editor),s=Depends(db)):
 r=Shipment(**data.model_dump());s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'班次编号重复')
 audit(s,u,'创建班次 '+r.code);return dump(r)
@router.get('/samples')
def samples(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Sample).all()]
@router.post('/samples')
def sample(data:SampleInput,u=Depends(editor),s=Depends(db)):
 ensure_shipment(s,data.shipment_id);r=Sample(**data.model_dump());s.add(r)
 try:s.flush()
 except IntegrityError:s.rollback();raise HTTPException(409,'样本编号重复')
 mon.note_sample_change(s,[data.shipment_id],u.email)
 s.commit()
 audit(s,u,'录入样本 '+r.code);return dump(r)
@router.get('/studies')
def studies(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Study).order_by(Study.id.desc()).all()]
@router.post('/studies')
def study(data:StudyInput,u=Depends(editor),s=Depends(db)):
 rows=study_rows(s,data.zone);r=Study(**data.model_dump(),result={'snapshot':rows});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('studies',r.id)
 except redis.RedisError:
  r.state='failed';r.error='队列暂不可用，请重新创建研究';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交研究 '+str(r.id));return dump(r)
@router.get('/studies/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_study(s,i))
@router.get('/studies/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_study(s,i)
 if r.state!='done':raise HTTPException(409,'研究未成功完成')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="coldchain-link-{i}.json"'})
@router.get('/recommendations')
def recs(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Recommendation).order_by(Recommendation.id.desc()).all()]
@router.post('/recommendations')
def rec(data:RecommendationInput,u=Depends(editor),s=Depends(db)):
 if ensure_study(s,data.study_id).state!='done':raise HTTPException(409,'仅已完成研究可以起草建议')
 r=Recommendation(**data.model_dump());s.add(r);s.commit();audit(s,u,'起草建议 '+str(r.id));return dump(r)
@router.post('/recommendations/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 # 锁定建议行，重复签核不会建立两个监测
 r=s.query(Recommendation).filter_by(id=i).with_for_update().first()
 if not r:raise HTTPException(404,'建议不存在')
 created=None
 if r.status!='approved':
  r.status='approved';s.flush()
  created=mon.create_monitor(s,r,u.email)
  # 原研究不变：监测只读取研究快照，不写回 Study
 s.commit()
 if created:audit(s,u,'签核建议 '+str(i));audit(s,u,'建立货损监测 '+str(created.id))
 out=dump(r)
 existing=created or s.query(Monitor).filter_by(recommendation_id=r.id).order_by(Monitor.id).first()
 if existing:out['monitor_id']=existing.id
 return out

@router.post('/monitors')
def monitor_create(data:MonitorInput,u=Depends(editor),s=Depends(db)):
 rec=s.get(Recommendation,data.recommendation_id)
 if not rec:raise HTTPException(404,'建议不存在')
 if rec.status!='approved':raise HTTPException(409,'建议尚未签核，不能新增监测')
 active=s.query(Monitor).filter_by(recommendation_id=rec.id,state='active').first()
 if active:raise HTTPException(409,'该建议已有进行中监测 #%s' % active.id)
 m=mon.create_monitor(s,rec,u.email,explicit=True)
 if m.state!='active':
  s.rollback();raise HTTPException(409,'该建议当前监测状态为 %s，不能新增' % m.state)
 s.commit();audit(s,u,'新增货损监测 '+str(m.id))
 return mon.monitor_view(s,m,deep=False)

@router.get('/monitors/overview')
def monitor_overview(u=Depends(current),s=Depends(db)):return mon.overview(s)

@router.get('/monitors')
def monitor_list(u=Depends(current),s=Depends(db)):
 return [mon.monitor_view(s,m,deep=False)
         for m in s.query(Monitor).order_by(Monitor.id.desc()).limit(200).all()]

@router.get('/monitors/{i}')
def monitor_detail(i:int,u=Depends(current),s=Depends(db)):
 m=s.get(Monitor,i)
 if not m:raise HTTPException(404,'监测不存在')
 return mon.monitor_view(s,m)

@router.post('/monitors/{i}/look')
def monitor_look(i:int,data:LookInput|None=None,u=Depends(editor),s=Depends(db)):
 m=s.get(Monitor,i)
 if not m:raise HTTPException(404,'监测不存在')
 look=mon.run_look(s,m,u.email,(data.idempotency_key if data else None))
 audit(s,u,'查看监测 '+str(i)+' / look '+str(look.id))
 return {'look':dump(look),'monitor':mon.monitor_view(s,s.get(Monitor,i))}

@router.post('/monitors/{i}/stop')
def monitor_stop(i:int,data:StopInput|None=None,u=Depends(admin),s=Depends(db)):
 m=s.get(Monitor,i)
 if not m:raise HTTPException(404,'监测不存在')
 mon.stop_monitor(s,m,(data.reason if data else None))
 s.commit();audit(s,u,'停止监测 '+str(i))
 return mon.monitor_view(s,m)

@router.get('/batches')
def batches(u=Depends(current),s=Depends(db)):
 return [dump(r) for r in s.query(LossBatch).order_by(LossBatch.id.desc()).limit(200)]

@router.post('/batches/confirm')
def confirm(data:BatchInput,u=Depends(editor),s=Depends(db)):
 ensure_shipment(s,data.shipment_id)
 ship=s.get(Shipment,data.shipment_id)
 batch=mon.confirm_batch(s,ship,u.email,data.idempotency_key)
 s.commit();audit(s,u,'确认货损收齐 '+ship.code)
 out=dump(batch)
 out['affected_monitors']=[m.id for m in s.query(Monitor).filter_by(zone=ship.zone,state='active').all()]
 return out
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]

@router.put('/shipments/{i}')
def edit_ship(i:int,data:ShipmentInput,u=Depends(editor),s=Depends(db)):
 row=s.get(Shipment,i)
 if not row:raise HTTPException(404,'班次不存在')
 old_zone=row.zone
 for k,v in data.model_dump().items():setattr(row,k,v)
 try:s.flush()
 except IntegrityError:s.rollback();raise HTTPException(409,'班次编号重复')
 mon.note_shipment_zone_change(s,row,old_zone,u.email)
 s.commit()
 audit(s,u,'修改班次 '+str(i));return dump(row)
@router.put('/samples/{i}')
def edit_sample(i:int,data:SampleInput,u=Depends(editor),s=Depends(db)):
 row=s.get(Sample,i)
 if not row:raise HTTPException(404,'样本不存在')
 ensure_shipment(s,data.shipment_id)
 old_shipment=row.shipment_id
 for k,v in data.model_dump().items():setattr(row,k,v)
 try:s.flush()
 except IntegrityError:s.rollback();raise HTTPException(409,'样本编号重复')
 mon.note_sample_change(s,[old_shipment,data.shipment_id],u.email)
 s.commit()
 audit(s,u,'修改样本 '+str(i));return dump(row)
