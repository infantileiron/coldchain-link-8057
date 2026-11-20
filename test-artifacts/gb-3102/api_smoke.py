import os,sys,tempfile,json
sys.path.insert(0,'/workspace/backend')
db=tempfile.mktemp(suffix='.db')
os.environ['DATABASE_URL']='sqlite:///'+db
os.environ['REDIS_URL']='redis://localhost:6379/0';os.environ['JWT_SECRET']='test'
os.environ['SEED_DEMO']='false'
import src.config.database as cd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
cd.engine=create_engine(cd.engine.url,poolclass=StaticPool,connect_args={'check_same_thread':False})
cd.Session=sessionmaker(bind=cd.engine,expire_on_commit=False)
import src.main
src.main.engine=cd.engine
from src.models import entities as E
E.Base.metadata.create_all(cd.engine)
from src.utils.security import hash_password
s=cd.Session()
for role in ('admin','analyst','viewer'):
    s.add(E.User(email=role+'@x',role=role,password=hash_password('pw')))
s.commit()
# stub redis
import redis
class R:
    def __init__(self):self.q=[]
    def rpush(self,k,v):self.q.append(v)
    def blpop(self,*a,**k):return None
r=R();redis.Redis.from_url=staticmethod(lambda *a,**k:r)
from fastapi.testclient import TestClient
c=TestClient(src.main.app)
def tok(role):return c.post('/auth/login',json={'email':role+'@x','password':'pw'}).json()['token']
H=lambda role:{'Authorization':'Bearer '+tok(role)}
A=H('analyst');AD=H('admin');V=H('viewer')
# create baseline shipments/samples
for i in range(4):
    sh=c.post('/shipments',json={'code':f'S{i}','route':'r','zone':'冷藏','driver':'d'},headers=A).json()
    c.post('/samples',json={'shipment_id':sh['id'],'code':f'S{i}-1','exposure':[10,20,30,40][i],'excursions':1,'loss':[2.0,4.0,3.0,5.0][i]},headers=A)
st=c.post('/studies',json={'name':'r','zone':'冷藏','feature':'exposure'},headers=A).json()
print('study',st['id'],st['state'])
# compute for the study as worker would
from src.services.analysis import analyze
row=s.get(E.Study,st['id']);row.result={**analyze(row.result['snapshot'],row.feature),'snapshot':row.result['snapshot']};row.state='done';s.commit()
r=c.post('/recommendations',json={'study_id':st['id'],'note':'x'*12,'owner':'o'},headers=A).json()
approve=c.post(f"/recommendations/{r['id']}/approve",headers=AD)
print('approve',approve.status_code,approve.json()['monitor_id'])
mid=approve.json()['monitor_id']
# idempotent approve
print('approve again',c.post(f"/recommendations/{r['id']}/approve",headers=AD).json()['monitor_id'],s.query(E.Monitor).count())
print('viewer look',c.post(f'/monitors/{mid}/look',json={},headers=V).status_code)
print('detail',c.get(f'/monitors/{mid}',headers=V).json()['phase'])
# new batch
sh=c.post('/shipments',json={'code':'N1','route':'r','zone':'冷藏','driver':'d'},headers=A).json()
c.post('/samples',json={'shipment_id':sh['id'],'code':'N1-1','exposure':10,'excursions':1,'loss':9.0},headers=A)
print('confirm',c.post('/batches/confirm',json={'shipment_id':sh['id']},headers=A).status_code)
lo=c.post(f'/monitors/{mid}/look',json={'idempotency_key':'k1'},headers=A)
print('look',lo.status_code,lo.json()['look']['decision'])
lo2=c.post(f'/monitors/{mid}/look',json={'idempotency_key':'k1'},headers=A)
print('retry same look',lo2.json()['look']['id']==lo.json()['look']['id'])
ov=c.get('/monitors/overview',headers=V).json()
print('overview',ov['zones'][0]['alert_count'],ov['zones'][0]['budget']['remaining'])
# modify sample -> invalidation
c.put(f"/samples/{s.query(E.Sample).filter_by(code='N1-1').one().id}",json={'shipment_id':sh['id'],'code':'N1-1','exposure':10,'excursions':1,'loss':9.5},headers=A)
d=c.get(f'/monitors/{mid}',headers=V).json()
print('invalidated',d['state'],d['reason'],'replacement',d['replaced_by'])
print('old alerts kept',len(d['alerts']),d['alerts'][0]['invalidated'])
newm=c.get(f"/monitors/{d['replaced_by']}",headers=V).json()
print('new pending',newm['phase'],newm['pending_count'],newm['budget']['remaining'])
print('look invalidated monitor',c.post(f'/monitors/{mid}/look',json={},headers=A).json())
print('stop',c.post(f"/monitors/{d['replaced_by']}/stop",json={'reason':'r'},headers=AD).status_code)
print('stop again',c.post(f"/monitors/{d['replaced_by']}/stop",json={},headers=AD).status_code)
print('analyst stop',c.post(f'/monitors/{mid}/stop',json={},headers=A).status_code)
print('look no body',c.post(f'/monitors/{mid}/look',headers=A).status_code)
print('stop no body',c.post(f"/monitors/{d['replaced_by']}/stop",headers=AD).status_code)
print('list',len(c.get('/monitors',headers=V).json()),'overview zones',len(c.get('/monitors/overview',headers=V).json()['zones']))
print('finished')
# explicit mid-year new monitor after stop
print('create while stopped',c.post('/monitors',json={'recommendation_id':r['id']},headers=A).status_code)
stopped_id=s.query(E.Monitor).filter_by(state='stopped').first().id
nm=c.post('/monitors',json={'recommendation_id':r['id']},headers=A)
print('create again while stopped',nm.status_code,nm.json().get('id'),nm.json().get('source_monitor_id'))
print('duplicate',c.post('/monitors',json={'recommendation_id':r['id']},headers=A).status_code)
print('create for unapproved',c.post('/monitors',json={'recommendation_id':999},headers=A).status_code)
print('viewer create',c.post('/monitors',json={'recommendation_id':r['id']},headers=V).status_code)
ov=c.get('/monitors/overview',headers=V).json();print('remaining',ov['zones'][0]['budget']['remaining'])
