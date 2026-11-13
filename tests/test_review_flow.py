# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio,json,sys,tempfile,unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock,patch
from arena.config import Config
from arena.evaluation import Plan,Check,Criterion,prepare_plan
from arena.harness_tests import MANIFEST_NAME
from arena.service import Service
from arena.store import Store, now
from arena.test_review import TestProposal,TestScenario,ReviewQuestion,digest,propose_tests,review_alignment_issues
from arena.worker import Worker, _TEST_PHASES


def proposal(version=1):
 return TestProposal(summary=f'Test proposal version {version}',test_cases=[TestScenario(id='hello',title='Greeting from the public CLI',steps=['Run the CLI with a name.'],expected_result='Print the expected greeting.',test_data='Local deterministic inputs; no external API')],questions=[ReviewQuestion(id='punctuation',question='Should the greeting end with punctuation?',suggested_answer='Yes')]).model_dump()

def generated(review):
 return Plan(criteria=[Criterion(id='greeting',requirement='Greeting',check_ids=['cli'])],checks=[Check(id='cli',group='acceptance',command='python main.py')],review_coverage={'hello':['cli']},excluded_groups={'project':'No build','candidate':'Supplementary suite absent','regression':'Empty baseline'})

# Minimal manifest that maps to the 'hello' scenario in proposal().
# Uses a legitimate test-runner command so that build_plan accepts it.
_MANIFEST = {
    "version": 1,
    "checks": [{"id": "cli", "scenario_id": "hello", "command": "python -m pytest tests/test_cli.py -q", "junit": None}],
    "test_files": ["tests/test_cli.py"],
    "python_requirements": [],
}

def _write_test_files(root):
    """Write test manifest + test file to workspace for the new-flow test phase."""
    (root / MANIFEST_NAME).write_text(json.dumps(_MANIFEST))
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_cli.py").write_text("def test_cli(): assert True\n")

class ReviewFlow(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.cfg=Config(data_dir=str(Path(self.temp.name)/'data'),artifact_s3_uri=None)
  self.w=Worker(self.cfg); self.s=self.w.store
  self.c=self.w.service.create('Build a greeting CLI.');self.id=self.c['id']
  self.calls=Counter();self.prepare_gate=None;self.fail_generation=False;self.task=None
  async def phase(rid,phase,c,plan,**kwargs):
   self.calls[phase]+=1
   if phase in _TEST_PHASES:
    self.s.transition(rid,'testing',spec_version=c['spec_version'])
   else:
    self.s.transition(rid,'clarifying' if phase=='clarify' else 'developing',spec_version=c['spec_version'])
   if phase=='clarify':
    self.s.update('run',rid,clarified=True);return 'ready'
   root=self.w.run_root(self.s.get('run',rid))/'workspace';root.mkdir(parents=True,exist_ok=True)
   (root/'main.py').write_text("print('Hello')\n")
   if phase in _TEST_PHASES:
    _write_test_files(root)
    # Record a minimal attempt so budget accounting works.
    r=self.s.get('run',rid)
    r['attempts'].append({'id':'t-'+phase,'phase':phase,'active_seconds':10,'outcome':None})
    self.s.update('run',rid,attempts=r['attempts'])
    self.s.transition(rid,'implemented');return 'tests_written'
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
  async def score(*args,**kwargs):
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
 async def test_no_test_stage_or_evaluation_before_approval(self):
  # Formerly test_no_generation_or_evaluation_before_approval; updated for new-flow
  # (harnesses write their own tests — no shared prepare_plan step).
  self.start();await self.ready()
  # Before approval: develops ran but no test phase and no checks yet.
  self.assertEqual(self.calls['develop'],3);self.assertEqual(self.calls['test'],0);self.assertEqual(self.calls['checks'],0)
  self.assertIsNone(self.s.get('comparison',self.id)['plan'])
  self.approve();await asyncio.wait_for(self.task,10)
  # After approval: test phase ran for each harness; no shared plan generated.
  self.assertEqual(self.calls['prepare'],0)   # prepare_plan never called in new flow
  self.assertEqual(self.calls['test'],3)      # one write-tests turn per harness
  self.assertGreater(self.calls['checks'],0)  # sandbox verification + evaluation
  self.assertEqual(self.calls['score'],3)
  self.assertEqual(self.s.get('comparison',self.id)['status'],'complete')
  self.assertIsNone(self.s.get('comparison',self.id)['plan'])  # per-harness plans, not shared
 async def test_questions_and_stale_approval_are_rejected(self):
  self.start();await self.ready();old=self.review()['id']
  with self.assertRaises(ValueError):self.w.service.review_tests(self.id,old,answers={})
  self.w.service.review_tests(self.id,old,feedback='Also consider empty input.')
  with self.assertRaises(ValueError):self.w.service.review_tests(self.id,old,answers={'punctuation':'Yes'})
  await self.ready();self.assertEqual(self.review()['revision'],2);self.assertEqual(self.calls['prepare'],0)
  self.approve();await asyncio.wait_for(self.task,10)
  self.assertEqual(self.calls['develop'],3);self.assertEqual(len(self.review()['history']),1)
 async def test_feedback_before_approval_creates_new_revision_without_recoding(self):
  # In the new flow there is no cancellable prepare step; feedback before the
  # first approval should create a new review revision without re-running
  # the development phase.
  self.start();await self.ready();old=self.review()['id']
  self.w.service.review_tests(self.id,old,feedback='Use the real CLI entrypoint.')
  await self.ready()
  self.assertNotEqual(self.review()['id'],old)
  self.assertEqual(self.review()['revision'],2)
  self.assertEqual(self.calls['develop'],3)   # no recoding triggered
  self.assertEqual(self.calls['prepare'],0)   # prepare_plan never called in new flow
  self.approve();await asyncio.wait_for(self.task,10)
  self.assertEqual(self.calls['develop'],3)
  self.assertEqual(self.s.get('comparison',self.id)['status'],'complete')
 async def test_coverage_change_is_recorded_in_review_history_and_applied(self):
  # Coverage change via feedback should update the review revision and preserve
  # history; implementations are reused (no recoding in new flow).
  self.start();await self.ready();old=self.review()['id']
  before=self.s.detail(self.id)
  self.assertEqual(self.review()['test_coverage'],'focused')
  self.w.service.review_tests(self.id,old,feedback='Cover every stated requirement.',
                             test_coverage='comprehensive')
  await self.ready()
  self.assertNotEqual(self.review()['id'],old)
  self.assertEqual(self.review()['test_coverage'],'comprehensive')
  self.assertEqual(self.review()['history'][-1]['test_coverage'],'focused')
  self.assertIsNone(self.review().get('approved_at'))
  self.approve();await asyncio.wait_for(self.task,10)
  after=self.s.detail(self.id)
  self.assertEqual(self.calls['develop'],3)   # implementations reused — no recoding
  self.assertEqual(self.calls['prepare'],0)   # prepare_plan never called in new flow
  self.assertEqual(before['spec_version'],after['spec_version'])
  self.assertEqual(after['specifications'][-1]['test_review']['test_coverage'],'comprehensive')
 async def test_harness_test_failure_records_stop_reason(self):
  # In the new flow, if a harness's test phase returns harness_failed the
  # worker stores a structured test_stop_reason so the UX can display the root cause.
  orig=self.w.phase;fail_once={'done':False}
  async def one_failing(rid,ph,c,plan,**kw):
   if ph=='test' and not fail_once['done']:
    fail_once['done']=True
    self.s.transition(rid,'testing',spec_version=c['spec_version'])
    self.s.transition(rid,'implemented')
    return 'harness_failed'
   return await orig(rid,ph,c,plan,**kw)
  self.w.phase=one_failing
  self.start();await self.ready();self.approve()
  await asyncio.wait_for(self.task,20)
  runs=self.s.detail(self.id)['runs']
  # All new-flow runs get test_stop_reason; filter for the one that actually failed.
  failed=[r for r in runs if (r.get('test_stop_reason') or {}).get('code')=='harness_failed']
  self.assertEqual(len(failed),1,'Expected exactly 1 run with harness_failed stop reason')
  self.assertEqual(self.s.get('comparison',self.id)['status'],'complete')
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
  # In the new flow prepare_plan is not called; harnesses re-run the test stage instead.
  self.assertEqual(self.calls['prepare'],0);self.assertEqual(self.calls['develop'],3)
  self.approve();await asyncio.wait_for(self.task,10)
  self.assertTrue(self.w.service.report(self.id)['comparison_complete'])
  self.assertEqual(self.calls['develop'],3)
  self.assertEqual([r['artifacts']['sha256'] for r in before['runs']],
                   [r['artifacts']['sha256'] for r in self.s.detail(self.id)['runs']])
 async def test_review_stored_with_status_in_test_stages(self):
  # I1: review dict stored in test_stages must have a 'status' key ('complete' or 'failed')
  # so that the UI can read stageEntry.review.status (interface 3).
  # Without a planning model review_harness_tests returns a 'failure' dict, so
  # status is expected to be 'failed' here.
  self.start();await self.ready();self.approve()
  await asyncio.wait_for(self.task,20)
  runs=self.s.detail(self.id)['runs']
  for r in runs:
   stages=r.get('test_stages') or []
   for entry in stages:
    rev=entry.get('review')
    if rev is not None:
     self.assertIn('status',rev,
      "review in test_stages must have a 'status' field (interface 3 / I1)")
     self.assertIn(rev['status'],('complete','failed'),
      "review.status must be 'complete' or 'failed'")
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
