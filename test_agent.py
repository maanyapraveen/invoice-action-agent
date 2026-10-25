import json
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import sleep
import httpx
import pytest
from fastapi.testclient import TestClient
import main
import storage

def setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage,"DB_PATH",tmp_path/"agent.db")
    storage.initialize_database()
    monkeypatch.setattr(main,"API_TOKEN","secret-test-token")
    monkeypatch.setattr(main,"TOKEN_PRINCIPALS",{"secret-test-token":"principal-a","other-test-token":"principal-b"})
    monkeypatch.setattr(main,"decide_batch",lambda batch:{"proposals":[{"packageId":p["packageId"],"actionId":"stable-action-12345","action":"hold_invoice","facts":{"vendorName":"Acme","invoiceNumber":"A1","amountMinor":100,"currency":"INR"},"evidenceRefs":["e1","e2","e3"],"rationale":"Hold pending confirmation because the provided documents do not demonstrate approved receipt. e1 e2"} for p in batch["packages"]]})
    return TestClient(main.app),{"Authorization":"Bearer secret-test-token","A2A-Version":"1.0","Content-Type":"application/a2a+json"}

def initial(message_id="m1", batch_id="b1"):
    return {"message":{"messageId":message_id,"role":"ROLE_USER","parts":[{"mediaType":main.BATCH_MODE,"data":{"batchId":batch_id,"policyRevision":"policy1","packages":[{"packageId":"p1","documents":[]}]}}]}}

def test_protocol_auth_card_and_limit(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    monkeypatch.setattr(main,"BASE_URL","https://agent.example.test/a2a/")
    card=client.get("/.well-known/agent-card.json")
    assert card.status_code==200
    assert card.json()["supportedInterfaces"][0]["url"]=="https://agent.example.test/a2a/"
    assert main.BATCH_MODE in card.json()["defaultInputModes"]
    assert main.PROPOSALS_MODE in card.json()["defaultOutputModes"]
    assert card.json()["skills"]
    assert client.get("/a2a/tasks").status_code==401
    assert client.get("/a2a/tasks",headers={**h,"A2A-Version":"2.0"}).status_code==400
    assert client.post("/a2a/message:send",headers={**h,"Content-Type":"text/plain"},content="{}").status_code==415
    assert client.post("/a2a/message:send",headers=h,content=b" "*(512*1024+1)).status_code==413
    assert client.get("/a2a/tasks",headers=h).headers["content-type"].startswith(main.A2A_JSON)
    malformed={"message":{"messageId":"bad","role":"ROLE_USER","parts":[None]}}
    assert client.post("/a2a/message:send",headers=h,json=malformed).status_code==400

@pytest.mark.parametrize("scheme",["Bearer","bearer","BEARER","bEaReR"])
def test_task_list_accepts_case_insensitive_bearer_scheme(tmp_path,monkeypatch,scheme):
    client,h=setup_client(tmp_path,monkeypatch)
    headers={**h,"Authorization":f"{scheme} secret-test-token"}
    assert client.get("/a2a/tasks",headers=headers).status_code==200

@pytest.mark.parametrize("authorization",[None,"", "Basic secret-test-token", "Bearer", "Bearer wrong-token", "Bearer secret-test-token extra"])
def test_task_list_rejects_missing_or_invalid_bearer_credentials(tmp_path,monkeypatch,authorization):
    client,h=setup_client(tmp_path,monkeypatch)
    headers={**h}
    if authorization is None:
        headers.pop("Authorization")
    else:
        headers["Authorization"]=authorization
    assert client.get("/a2a/tasks",headers=headers).status_code==401

def test_bearer_parser_allows_surrounding_header_whitespace_without_trimming_token(monkeypatch):
    monkeypatch.setattr(main,"API_TOKEN","secret-test-token")
    monkeypatch.setattr(main,"TOKEN_PRINCIPALS",{})
    principal=main.authenticated_principal("  bEaReR\tsecret-test-token  ")
    assert principal is not None
    assert main.authenticated_principal("Bearer secret-test-token ") == principal
    assert main.authenticated_principal("Bearer secret-test-token\textra") is None

def test_idempotency_isolation_and_cancel_terminal(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    r=client.post("/a2a/message:send",headers=h,json=initial())
    assert r.status_code==200
    assert r.headers["content-type"].startswith(main.A2A_JSON)
    task=r.json()["task"]; tid=task["id"]
    assert task["status"]["state"]=="TASK_STATE_INPUT_REQUIRED"
    assert len(task["artifacts"])==1
    assert task["history"]==[initial()["message"]]
    proposal_part=task["artifacts"][0]["parts"][0]
    assert proposal_part["mediaType"]==main.PROPOSALS_MODE
    assert proposal_part["data"]["batchId"]=="b1"
    assert "executions" not in proposal_part["data"]
    replay=client.post("/a2a/message:send",headers=h,json=initial())
    assert replay.json()==r.json()
    configured=initial(); configured["configuration"]={"returnImmediately":True,"historyLength":20}
    assert client.post("/a2a/message:send",headers=h,json=configured).json()==r.json()
    changed=initial(batch_id="changed")
    assert client.post("/a2a/message:send",headers=h,json=changed).status_code==409
    other={**h,"Authorization":"Bearer other-test-token"}
    assert client.get(f"/a2a/tasks/{tid}",headers=other).status_code==404
    assert client.post(f"/a2a/tasks/{tid}:cancel",headers=other).status_code==404
    canceled=client.post(f"/a2a/tasks/{tid}:cancel",headers=h).json()
    assert canceled["status"]["state"]=="TASK_STATE_CANCELED"
    assert len(canceled["artifacts"])==1
    assert client.post(f"/a2a/tasks/{tid}:cancel",headers=h).status_code==409
    assert client.post("/a2a/message:send",headers=h,json=initial()).json()["task"]==canceled
    proposal=task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    msg={"message":{"messageId":"late-result","taskId":tid,"contextId":task["contextId"],"role":"ROLE_USER","parts":[{"mediaType":main.RESULTS_MODE,"data":{"batchId":"b1","results":[{"packageId":"p1","actionId":proposal["actionId"],"action":proposal["action"],"outcome":"ACCEPTED","receiptNonce":"n"}]}}]}}
    assert client.post("/a2a/message:send",headers=h,json=msg).status_code==409
    assert client.post("/a2a/message:send",headers=other,json=msg).status_code==404

def test_continuation_executes_accepted_only_with_nonce(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    created=client.post("/a2a/message:send",headers=h,json=initial()).json()["task"]
    proposal=created["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    data={"batchId":"b1","results":[{"packageId":"p1","actionId":proposal["actionId"],"action":proposal["action"],"outcome":"ACCEPTED","receiptNonce":"grader-nonce-verbatim"}]}
    msg={"message":{"messageId":"m2","taskId":created["id"],"contextId":created["contextId"],"role":"ROLE_USER","parts":[{"mediaType":main.RESULTS_MODE,"data":data}]}}
    done=client.post("/a2a/message:send",headers=h,json=msg)
    assert done.status_code==200
    task=done.json()["task"]
    assert task["status"]["state"]=="TASK_STATE_COMPLETED"
    artifact=task["artifacts"][1]
    assert artifact["parts"][0]["mediaType"]==main.RECEIPTS_MODE
    execution=artifact["parts"][0]["data"]["executions"][0]
    assert execution["receiptNonce"]=="grader-nonce-verbatim"
    assert {k:execution[k] for k in ("packageId","actionId","action","facts","evidenceRefs")}=={k:proposal[k] for k in ("packageId","actionId","action","facts","evidenceRefs")}
    assert client.post("/a2a/message:send",headers=h,json=msg).json()==done.json()
    assert client.post("/a2a/message:send",headers=h,json=initial()).json()==done.json()
    with storage.connect() as c: assert c.execute("select count(*) from executions").fetchone()[0]==1
    storage.initialize_database()  # simulated process restart: persisted task remains readable
    assert client.get(f"/a2a/tasks/{created['id']}",headers=h).json()["status"]["state"]=="TASK_STATE_COMPLETED"

def test_rejected_proposal_creates_no_execution_and_no_nonce(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    task=client.post("/a2a/message:send",headers=h,json=initial()).json()["task"]
    proposal=task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    msg={"message":{"messageId":"reject-result","taskId":task["id"],"contextId":task["contextId"],"role":"ROLE_USER","parts":[{"mediaType":main.RESULTS_MODE,"data":{"batchId":"b1","results":[{"packageId":"p1","actionId":proposal["actionId"],"action":proposal["action"],"outcome":"REJECTED"}]}}]}}
    response=client.post("/a2a/message:send",headers=h,json=msg)
    assert response.status_code==200
    receipt_part=response.json()["task"]["artifacts"][1]["parts"][0]
    assert receipt_part["mediaType"]==main.RECEIPTS_MODE
    assert receipt_part["data"]["executions"]==[]
    with storage.connect() as c: assert c.execute("select count(*) from executions").fetchone()[0]==0

def test_continuation_action_must_match_stored_proposal(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    task=client.post("/a2a/message:send",headers=h,json=initial()).json()["task"]
    proposal=task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    msg={"message":{"messageId":"wrong-action","taskId":task["id"],"contextId":task["contextId"],"role":"ROLE_USER","parts":[{"mediaType":main.RESULTS_MODE,"data":{"batchId":"b1","results":[{"packageId":"p1","actionId":proposal["actionId"],"action":"settle_invoice","outcome":"ACCEPTED","receiptNonce":"nonce"}]}}]}}
    assert client.post("/a2a/message:send",headers=h,json=msg).status_code==400
    unchanged=client.get(f"/a2a/tasks/{task['id']}",headers=h).json()
    assert unchanged["status"]["state"]=="TASK_STATE_INPUT_REQUIRED"
    assert len(unchanged["artifacts"])==1

def test_a2a_success_response_size_is_bounded():
    response=main.a2a_json({"data":"x"*(main.MAX_BODY+100)})
    assert response.status_code==500
    assert len(response.body)<main.MAX_BODY

def test_production_startup_requires_secrets_and_public_https_url(monkeypatch):
    monkeypatch.setattr(main,"APP_ENV","production")
    monkeypatch.setattr(main,"API_TOKEN","")
    monkeypatch.setattr(main,"TOKEN_PRINCIPALS",{})
    monkeypatch.delenv("GROQ_API_KEY",raising=False)
    with pytest.raises(RuntimeError,match="A2A_BEARER_TOKEN"):
        main.validate_production_config()
    monkeypatch.setattr(main,"API_TOKEN","dev")
    with pytest.raises(RuntimeError,match="32 characters"):
        main.validate_production_config()
    monkeypatch.setattr(main,"API_TOKEN","configured-test-token-012345678901234567890")
    with pytest.raises(RuntimeError,match="GROQ_API_KEY"):
        main.validate_production_config()
    monkeypatch.setenv("GROQ_API_KEY","configured-test-key")
    with pytest.raises(RuntimeError,match="A2A_BASE_URL"):
        main.validate_production_config()
    monkeypatch.setattr(main,"BASE_URL","https://agent.example.test/a2a/")
    main.validate_production_config()

def test_concurrent_duplicate_message_calls_model_once_and_race_has_one_winner(tmp_path,monkeypatch):
    async def scenario():
        monkeypatch.setattr(storage,"DB_PATH",tmp_path/"concurrent.db")
        storage.initialize_database()
        monkeypatch.setattr(main,"API_TOKEN","secret-test-token")
        monkeypatch.setattr(main,"TOKEN_PRINCIPALS",{"secret-test-token":"principal-a"})
        calls=Counter()
        def model(batch):
            calls["count"]+=1
            return {"proposals":[{"packageId":p["packageId"],"actionId":"stable-action-12345","action":"hold_invoice","facts":{"vendorName":"Acme","invoiceNumber":"A1","amountMinor":100,"currency":"INR"},"evidenceRefs":["e1","e2","e3"],"rationale":"Hold pending confirmation because the provided documents do not demonstrate approved receipt. e1 e2"} for p in batch["packages"]]}
        monkeypatch.setattr(main,"decide_batch",model)
        headers={"Authorization":"Bearer secret-test-token","A2A-Version":"1.0","Content-Type":main.A2A_JSON}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="http://test") as client:
            first,second=await asyncio.gather(client.post("/a2a/message:send",headers=headers,json=initial("parallel")),client.post("/a2a/message:send",headers=headers,json=initial("parallel")))
            assert first.status_code==second.status_code==200
            assert first.json()==second.json()
            assert calls["count"]==1
            task=first.json()["task"]
            p=task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
            continuation={"message":{"messageId":"race-result","taskId":task["id"],"contextId":task["contextId"],"role":"ROLE_USER","parts":[{"mediaType":main.RESULTS_MODE,"data":{"batchId":"b1","results":[{"packageId":"p1","actionId":p["actionId"],"action":p["action"],"outcome":"ACCEPTED","receiptNonce":"race-nonce"}]}}]}}
            cancel,continue_response=await asyncio.gather(client.post(f"/a2a/tasks/{task['id']}:cancel",headers=headers),client.post("/a2a/message:send",headers=headers,json=continuation))
            assert sorted([cancel.status_code,continue_response.status_code])==[200,409]
            fetched=(await client.get(f"/a2a/tasks/{task['id']}",headers=headers)).json()
            assert fetched["status"]["state"] in {"TASK_STATE_CANCELED","TASK_STATE_COMPLETED"}
            if fetched["status"]["state"]=="TASK_STATE_CANCELED":
                assert len(fetched["artifacts"])==1
                with storage.connect() as c: assert c.execute("select count(*) from executions").fetchone()[0]==0
            else:
                assert len(fetched["artifacts"])==2
                with storage.connect() as c: assert c.execute("select count(*) from executions").fetchone()[0]==1
            # Continuation replay must be stable or rejected if cancellation won.
            replay=await client.post("/a2a/message:send",headers=headers,json=continuation)
            assert replay.status_code in {200,409}
            if fetched["status"]["state"]=="TASK_STATE_COMPLETED": assert replay.json()["task"]==fetched
    asyncio.run(scenario())

def test_sqlite_serializes_same_message_across_connections(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,"DB_PATH",tmp_path/"cross-connection.db")
    storage.initialize_database()
    entered=Event(); release=Event(); calls=Counter(); guard=Lock()
    def model(batch):
        with guard: calls["count"]+=1
        entered.set()
        assert release.wait(timeout=5)
        return {"proposals":[{"packageId":"p1","actionId":"stable-action-12345","action":"hold_invoice","facts":{"vendorName":"Acme","invoiceNumber":"A1","amountMinor":100,"currency":"INR"},"evidenceRefs":["e1","e2","e3"],"rationale":"Hold pending confirmation because the provided documents do not demonstrate approved receipt. e1 e2"}]}
    monkeypatch.setattr(main,"decide_batch",model)
    message=initial("cross-worker") ["message"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(main.create_initial_task,"principal-a",message,message["parts"][0]["data"])
        assert entered.wait(timeout=5)
        second=pool.submit(main.create_initial_task,"principal-a",message,message["parts"][0]["data"])
        sleep(.1)
        assert calls["count"]==1
        release.set()
        a,b=first.result(timeout=5),second.result(timeout=5)
    assert a==b
    assert calls["count"]==1

def test_decision_cache_uses_content_and_policy_not_batch_or_package_ids(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    calls=Counter()
    def model(batch):
        calls["count"]+=1
        return {"proposals":[{"packageId":p["packageId"],"actionId":"model-action-12345","action":"hold_invoice","facts":{"vendorName":"Acme","invoiceNumber":"A1","amountMinor":100,"currency":"INR"},"evidenceRefs":["e1","e2","e3"],"rationale":"Hold pending confirmation because the provided documents do not demonstrate approved receipt. e1 e2"} for p in batch["packages"]]}
    monkeypatch.setattr(main,"decide_batch",model)
    first=client.post("/a2a/message:send",headers=h,json=initial("cache-1","batch-a")).json()["task"]
    second_request=initial("cache-2","batch-b")
    second_request["message"]["parts"][0]["data"]["packages"][0]["packageId"]="p2"
    second=client.post("/a2a/message:send",headers=h,json=second_request).json()["task"]
    assert calls["count"]==1
    proposal1=first["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    proposal2=second["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    assert proposal1["packageId"]=="p1" and proposal2["packageId"]=="p2"
    assert proposal1["actionId"]!=proposal2["actionId"]
    third_request=initial("cache-3","batch-c")
    third_request["message"]["parts"][0]["data"]["policyRevision"]="policy2"
    assert client.post("/a2a/message:send",headers=h,json=third_request).status_code==200
    assert calls["count"]==2

def test_completed_task_is_immutable_and_changed_continuation_replay_conflicts(tmp_path,monkeypatch):
    client,h=setup_client(tmp_path,monkeypatch)
    task=client.post("/a2a/message:send",headers=h,json=initial()).json()["task"]
    proposal=task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    msg={"message":{"messageId":"final-result","taskId":task["id"],"contextId":task["contextId"],"role":"ROLE_USER","parts":[{"mediaType":main.RESULTS_MODE,"data":{"batchId":"b1","results":[{"packageId":"p1","actionId":proposal["actionId"],"action":proposal["action"],"outcome":"ACCEPTED","receiptNonce":"nonce-1"}]}}]}}
    completed=client.post("/a2a/message:send",headers=h,json=msg).json()["task"]
    assert client.post(f"/a2a/tasks/{task['id']}:cancel",headers=h).status_code==409
    modified=json.loads(json.dumps(msg)); modified["message"]["parts"][0]["data"]["results"][0]["receiptNonce"]="nonce-2"
    response=client.post("/a2a/message:send",headers=h,json=modified)
    assert response.status_code==409
    assert client.get(f"/a2a/tasks/{task['id']}",headers=h).json()==completed

def test_message_hash_ignores_object_key_order():
    from storage import message_hash
    assert message_hash({"messageId":"m","parts":[{"a":1,"b":2}]})==message_hash({"parts":[{"b":2,"a":1}],"messageId":"m"})
