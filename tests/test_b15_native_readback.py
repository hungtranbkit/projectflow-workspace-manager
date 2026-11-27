import asyncio, contextlib, sys, tempfile, unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from scripts import harness_monitor_bridge as bridge
from app.services.agent_harness_mvp import HarnessStore, HarnessError
TASK="dt_0123456789abcdef"
NATIVE=("DISPATCHING","EXECUTION_ACCEPTED","DISPATCH_UNCERTAIN","STUCK_COMPOSER")
def reply(state,tid=TASK):
 return {"status":"OK","result":{"status":"OK","task":{"task_id":tid,"mode":"agent","agent":"codex","state":state,"target":"b15-fixture","dispatches":1,"running_confirmed":False,"turn_state":"IDLE"}}}
def verify(response):
 calls=[]
 @contextlib.asynccontextmanager
 async def transport(endpoint):
  assert endpoint=="http://127.0.0.1:8766/mcp"
  yield (None,None)
 class Session:
  def __init__(self,*args):pass
  async def __aenter__(self):return self
  async def __aexit__(self,*args):pass
  async def initialize(self):pass
  async def call_tool(self,name,args):
   calls.append((name,args))
   assert name=="terminal_turn" and args=={"action":"task","task_id":TASK,"compact":True}
   return SimpleNamespace(is_error=False,structured_content=response)
 m=ModuleType("mcp");m.ClientSession=Session
 c=ModuleType("mcp.client");h=ModuleType("mcp.client.streamable_http");h.streamable_http_client=transport
 with patch.dict(sys.modules,{"mcp":m,"mcp.client":c,"mcp.client.streamable_http":h}):
  out=asyncio.run(bridge.verify_external_task(TASK))
 assert len(calls)==1
 return out
class NativeReceipt(unittest.TestCase):
 def test_native_states_are_known_identity_not_execution(self):
  for state in NATIVE:
   with self.subTest(state=state):
    response=reply(state);self.assertEqual(verify(response),response)
 def test_unknown_wrong_identity_and_denial_stay_fail_closed(self):
  for response in (reply("UNRECOGNIZED"),reply("EXECUTION_ACCEPTED","dt_ffffffffffffffff"),{"status":"FAILED","result":{"error":"UNKNOWN_TASK_ID"}},{"status":"FAILED","result":{"error":"DIRECT_TASK_STORE_UNAVAILABLE"}}):
   with self.subTest(response=response),self.assertRaises(HarnessError):verify(response)
 def test_existing_states_still_readable(self):
  for state in ("RUNNING","DONE","BLOCKED","WAITING_INPUT","FAILED"):
   self.assertEqual(verify(reply(state))["result"]["task"]["state"],state)
 def test_attested_native_states_never_claim_running_or_done(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/"state.db";HarnessStore(db);registry=bridge.load_registry();bridge.bootstrap(registry,db)
   owner=next(k for k,v in registry.items() if v["configured_enabled"] and v["role"]=="parent")
   record=bridge.register(registry,db,owner,child_goal="fixture",dod="independent test",agent="codex",node="fixture",session_name="b15-fixture",task_id=TASK,ttl_seconds=120,backend=reply("EXECUTION_ACCEPTED"),provenance="CENTRAL_ATTESTED")
   for state in NATIVE:
    with self.subTest(state=state):
     out=bridge.observe_attested(registry,db,owner,TASK,reply(state));self.assertEqual(out["state"],"UNKNOWN")
   self.assertEqual(len(HarnessStore(db).list_tasks()),1)
   self.assertNotEqual(HarnessStore(db).get(record["task_id"]).state,"RUNNING_VERIFIED")
if __name__=="__main__":unittest.main()
