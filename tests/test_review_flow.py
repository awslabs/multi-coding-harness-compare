# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio,json,sys,tempfile,unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock,patch
from arena.config import Config
from arena.evaluation import Plan,Check,Criterion,prepare_plan
from arena.service import Service
from arena.store import Store
from arena.test_review import TestProposal,TestScenario,ReviewQuestion,digest,propose_tests,review_alignment_issues
from arena.worker import Worker


def proposal(version=1):
 return TestProposal(summary=f'Test proposal version {version}',test_cases=[TestScenario(id='hello',title='Greeting from the public CLI',steps=['Run the CLI with a name.'],expected_result='Print the expected greeting.',test_data='Local deterministic inputs; no external API')],questions=[ReviewQuestion(id='punctuation',question='Should the greeting end with punctuation?',suggested_answer='Yes')]).model_dump()

def generated(review):
 return Plan(criteria=[Criterion(id='greeting',requirement='Greeting',check_ids=['cli'])],checks=[Check(id='cli',group='acceptance',command='python main.py')],review_coverage={'hello':['cli']},excluded_groups={'project':'No build','candidate':'Supplementary suite absent','regression':'Empty baseline'})

class ReviewFlow(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.cfg=Config(data_dir=str(Path(self.temp.name)/'data'),artifact_s3_uri=None)
  self.w=Worker(self.cfg); self.s=self.w.store
  self.c=self.w.service.create('Build a greeting CLI.');self.id=self.c['id']
  self.calls=Counter();self.prepare_gate=None;self.fail_generation=False;self.task=None
  async def phase(rid,phase,c,plan,readiness_reminder=False):
   self.calls[phase]+=1
   self.s.transition(rid,'clarifying' if phase=='clarify' else 'developing',spec_version=c['spec_version'])
   if phase=='clarify':
    self.s.update('run',rid,clarified=True);return 'ready'
   root=self.w.run_root(self.s.get('run',rid))/'workspace';root.mkdir(parents=True,exist_ok=True)
   (root/'main.py').write_text("print('Hello')\n")
   self.s.transition(rid,'implemented');return 'implemented'
  self.phase=phase;self.w.phase=phase
  async def draft(task,baseline,config,review):
   self.calls['proposal']+=1
   assert not list(baseline.iterdir())
   return proposal(review['revision']),{'source':'controlled proposal'}
  async def prepare(task,baseline,config):
   self.calls['prepare']+=1
   assert task['approved_test_review']['answers']=={'punctuation':'Yes'}
   if self.fail_generation:
    self.fail_generation=False;raise RuntimeError('Controlled generation failure')
   if self.prepare_gate is not None:
    await self.prepare_gate.wait()
   return generated(task['approved_test_review']),{'source':'controlled generation'}
  async def checks(snapshot,plan,path,*args,**kwargs):
   self.calls['checks']+=1
   return {'checks':[{'id':'cli','group':'acceptance','status':'passed','exit_code':0,'log':'Hello','counts':{}}],'environment':{}}
  async def score(*args):
   self.calls['score']+=1
   return {'overall':100,'verified_complete':True}
  self.score=score;self.w.score_candidate=score
  for target,func in [('arena.worker.propose_tests',draft),('arena.worker.prepare_plan',prepare),('arena.worker.CandidateTestRunner.run',checks)]:
   p=patch(target,side_effect=func);p.start();self.addCleanup(p.stop)
 async def asyncTearDown(self):
  if self.task and not self.task.done():self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
 async def wait(self,predicate):
  for _ in range(150):
   if predicate():return
   if self.task and self.task.done():self.task.result()
   await asyncio.sleep(.05)
  self.fail('Timed out waiting for flow state: '+json.dumps(self.s.detail(self.id),default=str)[:700])
 def review(self):return self.s.get('comparison',self.id)['test_review']
 def start(self):self.task=asyncio.create_task(self.w.compare(self.id))
 async def ready(self):
  await self.wait(lambda:self.review()['status']=='awaiting_review')
  await self.wait(lambda:all(r['status']=='waiting_test_review' for r in self.s.detail(self.id)['runs']))
 def approve(self):return self.w.service.review_tests(self.id,self.review()['id'],answers={'punctuation':'Yes'})
 async def test_no_generation_or_evaluation_before_approval(self):
  self.start();await self.ready()
  self.assertEqual(self.calls['develop'],3);self.assertEqual(self.calls['prepare'],0);self.assertEqual(self.calls['checks'],0)
  self.assertIsNone(self.s.get('comparison',self.id)['plan'])
  self.approve();await asyncio.wait_for(self.task,10)
  self.assertEqual(self.calls['prepare'],1);self.assertEqual(self.calls['checks'],4);self.assertEqual(self.calls['score'],3)
  self.assertEqual(self.s.get('comparison',self.id)['status'],'complete')
  self.assertEqual(self.s.get('comparison',self.id)['plan']['review_id'],self.review()['id'])
 async def test_questions_and_stale_approval_are_rejected(self):
  self.start();await self.ready();old=self.review()['id']
  with self.assertRaises(ValueError):self.w.service.review_tests(self.id,old,answers={})
  self.w.service.review_tests(self.id,old,feedback='Also consider empty input.')
  with self.assertRaises(ValueError):self.w.service.review_tests(self.id,old,answers={'punctuation':'Yes'})
  await self.ready();self.assertEqual(self.review()['revision'],2);self.assertEqual(self.calls['prepare'],0)
  self.approve();await asyncio.wait_for(self.task,10)
  self.assertEqual(self.calls['develop'],3);self.assertEqual(len(self.review()['history']),1)
 async def test_feedback_cancels_inflight_generation(self):
  self.prepare_gate=asyncio.Event();self.start();await self.ready();self.approve()
  await self.wait(lambda:self.calls['prepare']==1)
  self.w.service.review_tests(self.id,self.review()['id'],feedback='Use the real CLI entrypoint.')
  await self.ready();self.assertEqual(self.calls['checks'],0)
  self.prepare_gate=None;self.approve();await asyncio.wait_for(self.task,10)
  self.assertEqual(self.calls['develop'],3);self.assertEqual(self.calls['prepare'],2)
 async def test_coverage_change_cancels_old_generation_and_reuses_implementations(self):
  self.prepare_gate=asyncio.Event();self.start();await self.ready();self.approve()
  await self.wait(lambda:self.calls['prepare']==1)
  before=self.s.detail(self.id);old=self.review()['id']
  self.assertEqual(self.review()['test_coverage'],'focused')
  self.w.service.review_tests(self.id,old,feedback='Cover every stated requirement.',
                             test_coverage='comprehensive')
  await self.ready()
  self.assertNotEqual(self.review()['id'],old)
  self.assertEqual(self.review()['test_coverage'],'comprehensive')
  self.assertEqual(self.review()['history'][-1]['test_coverage'],'focused')
  self.assertIsNone(self.review().get('approved_at'))
  self.prepare_gate=None;self.approve();await asyncio.wait_for(self.task,10)
  after=self.s.detail(self.id)
  self.assertEqual(self.calls['develop'],3);self.assertEqual(self.calls['prepare'],2)
  self.assertEqual(before['spec_version'],after['spec_version'])
  self.assertEqual(after['specifications'][-1]['test_review']['test_coverage'],'comprehensive')
 async def test_generation_failure_waits_for_explicit_retry(self):
  self.fail_generation=True;self.start();await self.ready();self.approve()
  await self.wait(lambda:self.review()['status']=='generation_failed')
  self.assertEqual(self.calls['checks'],0);self.assertIsNone(self.s.get('comparison',self.id)['plan'])
  approved_at=self.review()['approved_at'];approval_hash=self.review()['approval_sha256']
  self.approve();await asyncio.wait_for(self.task,10);self.assertEqual(self.calls['prepare'],2)
  self.assertEqual(self.review()['approved_at'],approved_at)
  self.assertEqual(self.review()['approval_sha256'],approval_hash)
  events=self.s.events(self.id)
  self.assertEqual(sum(e['type']=='tests_approved' for e in events),1)
  self.assertEqual(sum(e['type']=='test_generation_retry_requested' for e in events),1)
 async def test_requirement_revision_invalidates_review(self):
  self.start();await self.ready();old=self.review()['id']
  self.w.service.revise(self.id,'Use a friendly greeting.')
  with self.assertRaises(ValueError):self.w.service.review_tests(self.id,old,answers={'punctuation':'Yes'})
  await self.ready();self.assertEqual(self.review()['spec_version'],2)
  self.approve();await asyncio.wait_for(self.task,10);self.assertEqual(self.calls['develop'],6)
 async def test_cancel_one_harness_does_not_cancel_review(self):
  self.start();await self.ready();runs=self.s.detail(self.id)['runs']
  self.w.service.cancel(self.id,runs[0]['id']);self.approve();await asyncio.wait_for(self.task,10)
  states=[r['status'] for r in self.s.detail(self.id)['runs']]
  self.assertEqual(states.count('cancelled'),1);self.assertEqual(states.count('verified_complete'),2)
 async def test_restart_preserves_review_and_never_recodes_saved_snapshots(self):
  self.start();await self.ready();rid=self.review()['id']
  self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
  w=Worker(self.cfg);w.phase=self.phase;w.score_candidate=self.score;self.w=w
  with patch.object(w.runtime,'cleanup_owned',new=AsyncMock()):await w.recovery()
  self.assertEqual(self.review()['id'],rid);self.assertEqual(self.s.get('comparison',self.id)['status'],'waiting_test_review')
  self.assertFalse(w.ready_to_dispatch(self.s.get('comparison',self.id)))
  self.start();await self.ready();self.approve();await asyncio.wait_for(self.task,10)
  self.assertEqual(self.calls['develop'],3)
 async def test_completed_test_feedback_reuses_code_and_preserves_evidence(self):
  self.start();await self.ready();self.approve();await asyncio.wait_for(self.task,10)
  before=self.s.detail(self.id);paths={r['id']:r['artifacts']['case'] for r in before['runs']}
  self.assertTrue(self.w.service.report(self.id)['comparison_complete'])
  self.w.service.review_tests(self.id,self.review()['id'],feedback='Strengthen the CLI behavior checks.')
  self.assertFalse(self.w.service.report(self.id)['comparison_complete'])
  self.task=asyncio.create_task(self.w.review_saved_code(self.id))
  await self.wait(lambda:self.review()['status']=='awaiting_review');self.assertEqual(self.calls['develop'],3)
  self.approve();await asyncio.wait_for(self.task,10)
  after=self.s.detail(self.id);self.assertEqual(after['status'],'complete',str(after.get('error'))+str([(r['harness'],r.get('error')) for r in after['runs']]));self.assertEqual(self.calls['develop'],3)
  self.assertTrue(self.w.service.report(self.id)['comparison_complete'])
  for run in after['runs']:
   self.assertNotEqual(run['artifacts']['case'],paths[run['id']]);self.assertTrue(Path(paths[run['id']]).is_file())
   self.assertTrue(run['artifact_history'])
 async def test_existing_comparison_can_review_saved_code(self):
  self.start();await self.ready();self.approve();await asyncio.wait_for(self.task,10)
  before=self.s.detail(self.id);self.s.update('comparison',self.id,test_review=None)
  self.w.service.start_test_review(self.id)
  self.assertFalse(self.w.service.report(self.id)['comparison_complete'])
  self.assertEqual(self.review()['mode'],'saved_code')
  self.task=asyncio.create_task(self.w.review_saved_code(self.id))
  await self.wait(lambda:self.review()['status']=='awaiting_review')
  self.assertEqual(self.calls['prepare'],1);self.assertEqual(self.calls['develop'],3)
  self.approve();await asyncio.wait_for(self.task,10)
  self.assertTrue(self.w.service.report(self.id)['comparison_complete'])
  self.assertEqual(self.calls['develop'],3)
  self.assertEqual([r['artifacts']['sha256'] for r in before['runs']],
                   [r['artifacts']['sha256'] for r in self.s.detail(self.id)['runs']])
 async def test_alignment_rejects_dropped_or_unreviewed_cases(self):
  review={'proposal':proposal()};p=generated(review)
  self.assertFalse(review_alignment_issues(p,review))
  p.review_coverage={};self.assertTrue(review_alignment_issues(p,review))
  p.review_coverage={'hello':['cli']};p.checks.append(Check(id='extra',group='acceptance',command='python extra.py'))
  self.assertTrue(review_alignment_issues(p,review))

class SuppliedPlanReview(unittest.IsolatedAsyncioTestCase):
 async def test_supplied_tests_need_approval_but_no_planning_model(self):
  with tempfile.TemporaryDirectory() as directory:
   cfg=Config(data_dir=str(Path(directory)/'data'));w=Worker(cfg)
   p=generated({});p.review_coverage={}
   c=w.service.create('A CLI',validation_plan=p.model_dump());spec=w.freeze_requirements(c['id'])
   baseline=cfg.root/'comparisons'/c['id']/'baseline'
   item,meta=await propose_tests(spec,baseline,cfg,c['test_review'])
   self.assertEqual(meta['source'],'supplied plan')
   review={'id':'explicit-approval','proposal':item,'answers':{},'supplied_plan_sha256':meta['plan_sha256']}
   compiled,_=await prepare_plan({**spec,'approved_test_review':review},baseline,cfg)
   self.assertEqual(compiled.checks[0].command,p.checks[0].command);self.assertEqual(compiled.review_id,'explicit-approval')

if __name__=='__main__':unittest.main(verbosity=2)
