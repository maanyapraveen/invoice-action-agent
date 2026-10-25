"""Invoice action A2A HTTP service."""
import json
import os
import hashlib
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from storage import connect, canonical_json, message_hash
from decision_engine import decide_batch, cache_key

app = FastAPI(title="Invoice Action Agent", version="1.0.0")
BASE_URL = os.getenv("A2A_BASE_URL", "http://localhost:8000/a2a/")
API_TOKEN = os.getenv("A2A_BEARER_TOKEN", "")
APP_ENV = os.getenv("APP_ENV", "development").lower()
try:
    TOKEN_PRINCIPALS = json.loads(os.getenv("A2A_BEARER_TOKENS", "{}"))
    if not isinstance(TOKEN_PRINCIPALS, dict): TOKEN_PRINCIPALS = {}
except ValueError:
    TOKEN_PRINCIPALS = {}
PROPOSALS_MODE = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPTS_MODE = "application/vnd.ga5.invoice-action-receipts+json"
BATCH_MODE = "application/vnd.ga5.invoice-claim-batch+json"
RESULTS_MODE = "application/vnd.ga5.invoice-action-results+json"
A2A_JSON = "application/a2a+json"
MAX_BODY = 512 * 1024
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}

def validate_production_config():
    """Fail closed when production is selected but required service settings are absent."""
    if APP_ENV != "production":
        return
    if not API_TOKEN:
        raise RuntimeError("A2A_BEARER_TOKEN must be configured")
    if len(API_TOKEN)<32:
        raise RuntimeError("A2A_BEARER_TOKEN must contain at least 32 characters in production")
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY must be configured")
    parsed=urlparse(BASE_URL)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/")!="/a2a":
        raise RuntimeError("A2A_BASE_URL must be the public HTTPS A2A base URL ending in /a2a/")

app.router.on_startup.append(validate_production_config)

def now(): return datetime.now(timezone.utc).isoformat()

def err(status, code, message):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}}, media_type=A2A_JSON)

def a2a_json(payload, status=200):
    response=JSONResponse(status_code=status, content=payload, media_type=A2A_JSON)
    if len(response.body)>MAX_BODY:
        return err(500,"RESPONSE_TOO_LARGE","Response exceeds the protocol size limit")
    return response

def authenticated_principal(authorization):
    token=authorization[7:] if isinstance(authorization,str) and authorization.startswith("Bearer ") else ""
    if token in TOKEN_PRINCIPALS and isinstance(TOKEN_PRINCIPALS[token],str): return hashlib.sha256(token.encode()).hexdigest()
    if API_TOKEN and token==API_TOKEN: return hashlib.sha256(token.encode()).hexdigest()
    return None

@app.get("/.well-known/agent-card.json")
async def agent_card():
    return {"name":"Invoice Action Agent", "description":"Evidence-based invoice action proposals with approval-gated execution.", "version":"1.0.0", "capabilities":{}, "supportedInterfaces":[{"url":BASE_URL,"protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}], "defaultInputModes":[BATCH_MODE,RESULTS_MODE], "defaultOutputModes":[PROPOSALS_MODE,RECEIPTS_MODE], "skills":[{"id":"invoice_action_agent","name":"Invoice Action Agent","description":"Reviews invoice claim batches and accepts decision results to produce receipts.","tags":["invoice","finance","approval","evidence"],"inputModes":[BATCH_MODE,RESULTS_MODE],"outputModes":[PROPOSALS_MODE,RECEIPTS_MODE]}]}

@app.middleware("http")
async def protocol(request: Request, call_next):
    if request.url.path == "/.well-known/agent-card.json": return await call_next(request)
    if request.url.path.startswith("/a2a/"):
        if not API_TOKEN and not TOKEN_PRINCIPALS: return err(503,"CONFIGURATION_ERROR","Bearer authentication is not configured")
        if authenticated_principal(request.headers.get("authorization")) is None: return err(401,"UNAUTHENTICATED","Authentication required")
        if request.headers.get("a2a-version") != "1.0": return err(400,"INVALID_VERSION","A2A-Version 1.0 required")
        if request.method == "POST":
            if request.headers.get("content-type","").split(";")[0].strip().lower() != A2A_JSON: return err(415,"UNSUPPORTED_MEDIA_TYPE",f"{A2A_JSON} required")
            length=request.headers.get("content-length")
            if length and length.isdigit() and int(length)>MAX_BODY: return err(413,"PAYLOAD_TOO_LARGE","Request body exceeds 512 KiB")
            body=await request.body()
            if len(body)>MAX_BODY: return err(413,"PAYLOAD_TOO_LARGE","Request body exceeds 512 KiB")
            async def receive(): return {"type":"http.request","body":body,"more_body":False}
            request._receive=receive
    return await call_next(request)

def parse_body(raw):
    try: value=json.loads(raw)
    except (ValueError,UnicodeDecodeError): raise ValueError("INVALID_JSON")
    if not isinstance(value,dict): raise ValueError("INVALID_MESSAGE")
    m=value.get("message")
    if not isinstance(m,dict) or m.get("role")!="ROLE_USER" or not isinstance(m.get("messageId"),str) or not m["messageId"] or not isinstance(m.get("parts"),list) or not all(isinstance(p,dict) for p in m["parts"]): raise ValueError("INVALID_MESSAGE")
    return m

def create_initial_task(principal,message,batch):
    digest=message_hash(message); mid=message["messageId"]; stamp=now()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old=c.execute("SELECT message_hash,response_json FROM idempotency WHERE principal=? AND message_id=?",(principal,mid)).fetchone()
        if old:
            if old["message_hash"]!=digest: c.rollback(); raise ValueError("IDEMPOTENCY_CONFLICT")
            response=json.loads(old["response_json"]); c.commit(); return response
        decisions=[]
        for package in batch["packages"]:
            key=cache_key(package,batch.get("policyRevision"))
            row=c.execute("SELECT decision_json FROM decision_cache WHERE content_hash=?",(key,)).fetchone()
            decisions.append(json.loads(row[0]) if row else None)
        if all(d is not None for d in decisions):
            proposals=[dict(d) for d in decisions]
        else:
            uncached={**batch,"packages":[p for p,d in zip(batch["packages"],decisions) if d is None]}
            generated=decide_batch(uncached)["proposals"]
            it=iter(generated); proposals=[d if d is not None else next(it) for d in decisions]
            for package,decision in zip(batch["packages"],proposals):
                c.execute("INSERT OR IGNORE INTO decision_cache(content_hash,decision_json,created_at) VALUES(?,?,?)",(cache_key(package,batch.get("policyRevision")),canonical_json(decision),now()))
        for package,proposal in zip(batch["packages"],proposals):
            key=cache_key(package,batch.get("policyRevision"))
            proposal["packageId"]=package["packageId"]
            proposal["actionId"]=str(uuid.uuid5(uuid.NAMESPACE_URL,f"invoice-action:{key}:{package['packageId']}"))
        task_id=str(uuid.uuid4()); context=str(uuid.uuid4())
        task={"id":task_id,"contextId":context,"status":{"state":"TASK_STATE_INPUT_REQUIRED","timestamp":stamp},"history":[message],"artifacts":[{"artifactId":str(uuid.uuid4()),"parts":[{"data":{"batchId":batch["batchId"],"proposals":proposals},"mediaType":PROPOSALS_MODE}]}]}
        c.execute("INSERT INTO tasks(task_id,principal,context_id,batch_id,state,task_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(task_id,principal,context,batch["batchId"],"TASK_STATE_INPUT_REQUIRED",canonical_json(task),stamp,stamp))
        response={"task":task}
        c.execute("INSERT INTO idempotency(principal,message_id,message_hash,task_id,response_json) VALUES(?,?,?,?,?)",(principal,mid,digest,task_id,canonical_json(response)))
        c.commit()
    return response

def valid_batch(message):
    p=message["parts"]
    if len(p)!=1 or not isinstance(p[0],dict) or p[0].get("mediaType")!=BATCH_MODE or not isinstance(p[0].get("data"),dict): raise ValueError("INVALID_BATCH")
    b=p[0]["data"]
    packages=b.get("packages")
    if not isinstance(b.get("batchId"),str) or not b["batchId"] or not isinstance(b.get("policyRevision"),str) or not b["policyRevision"] or not isinstance(packages,list) or not packages: raise ValueError("INVALID_BATCH")
    if not all(isinstance(package,dict) and isinstance(package.get("packageId"),str) and package["packageId"] for package in packages): raise ValueError("INVALID_BATCH")
    ids=[package["packageId"] for package in packages]
    if len(ids)!=len(set(ids)): raise ValueError("INVALID_BATCH")
    return b

@app.post("/a2a/message:send")
async def send_message(request: Request):
    principal=authenticated_principal(request.headers.get("authorization"))
    try:
        m=parse_body(await request.body()); p=m["parts"]
        if len(p)==1 and p[0].get("mediaType")==BATCH_MODE:
            b=valid_batch(m)
            result=create_initial_task(principal,m,b)
            return a2a_json(result)
        if len(p)==1 and p[0].get("mediaType")==RESULTS_MODE:
            result=continue_task(principal,m,p[0].get("data"))
            return a2a_json(result)
        return err(400,"INVALID_MESSAGE","Unsupported A2A message")
    except ValueError as e:
        code=str(e)
        mapping={"INVALID_JSON":(400,"INVALID_JSON"),"INVALID_MESSAGE":(400,"INVALID_MESSAGE"),"INVALID_BATCH":(400,"INVALID_BATCH"),"IDEMPOTENCY_CONFLICT":(409,code),"INVALID_RESULTS":(400,code),"TASK_NOT_FOUND":(404,code),"INVALID_STATE":(409,code),"INVALID_DECISION":(400,code)}
        status,out=mapping.get(code,(422,"DECISION_VALIDATION_FAILED"))
        return err(status,out,"Request could not be processed")
    except Exception:
        # Do not return provider exception strings (they may contain configuration details).
        return err(503,"DECISION_UNAVAILABLE","Decision service is temporarily unavailable")

def continue_task(principal,message,data):
    task_id=message.get("taskId"); context_id=message.get("contextId")
    if not isinstance(task_id,str) or not isinstance(context_id,str) or not isinstance(data,dict) or not isinstance(data.get("batchId"),str) or not isinstance(data.get("results"),list): raise ValueError("INVALID_RESULTS")
    digest=message_hash(message)
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        prior=c.execute("SELECT message_hash,response_json FROM idempotency WHERE principal=? AND message_id=?",(principal,message["messageId"])).fetchone()
        if prior:
            if prior["message_hash"]!=digest: c.rollback(); raise ValueError("IDEMPOTENCY_CONFLICT")
            response=json.loads(prior["response_json"]); c.commit(); return response
        row=c.execute("SELECT * FROM tasks WHERE task_id=? AND principal=?",(task_id,principal)).fetchone()
        if not row: c.rollback(); raise ValueError("TASK_NOT_FOUND")
        task=json.loads(row["task_json"])
        if row["context_id"]!=context_id or row["batch_id"]!=data["batchId"]: c.rollback(); raise ValueError("INVALID_RESULTS")
        if row["state"] in TERMINAL:
            c.rollback(); raise ValueError("INVALID_STATE")
        if row["state"]!="TASK_STATE_INPUT_REQUIRED": c.rollback(); raise ValueError("INVALID_STATE")
        props=task["artifacts"][0]["parts"][0]["data"]["proposals"]
        if len(data["results"])!=len(props): c.rollback(); raise ValueError("INVALID_RESULTS")
        byid={p["actionId"]:p for p in props}; seen=set(); receipts=[]
        for item in data["results"]:
            if not isinstance(item,dict): c.rollback(); raise ValueError("INVALID_RESULTS")
            aid=item.get("actionId"); pid=item.get("packageId"); outcome=item.get("outcome")
            if aid not in byid or aid in seen or byid[aid]["packageId"]!=pid or item.get("action")!=byid[aid]["action"] or outcome not in {"ACCEPTED","REJECTED"}: c.rollback(); raise ValueError("INVALID_RESULTS")
            seen.add(aid)
            if outcome=="ACCEPTED":
                nonce=item.get("receiptNonce")
                if not isinstance(nonce,str) or not nonce: c.rollback(); raise ValueError("INVALID_RESULTS")
                proposal=byid[aid]
                receipt={"packageId":proposal["packageId"],"actionId":proposal["actionId"],"action":proposal["action"],"receiptNonce":nonce,"facts":proposal["facts"],"evidenceRefs":proposal["evidenceRefs"]}
                receipts.append(receipt)
        task["history"].append(message)
        task["status"]={"state":"TASK_STATE_COMPLETED","timestamp":now()}
        task["artifacts"].append({"artifactId":str(uuid.uuid4()),"parts":[{"data":{"batchId":data["batchId"],"executions":receipts},"mediaType":RECEIPTS_MODE}]})
        for receipt in receipts:
            c.execute("INSERT INTO executions(execution_id,task_id,action_id,receipt_nonce,receipt_json,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),row["task_id"],receipt["actionId"],receipt["receiptNonce"],canonical_json(receipt),now()))
        c.execute("UPDATE tasks SET state=?,task_json=?,updated_at=? WHERE task_id=? AND state=?",("TASK_STATE_COMPLETED",canonical_json(task),now(),row["task_id"],"TASK_STATE_INPUT_REQUIRED"))
        response={"task":task}
        c.execute("UPDATE idempotency SET response_json=? WHERE task_id=?",(canonical_json(response),row["task_id"]))
        c.execute("INSERT INTO idempotency(principal,message_id,message_hash,task_id,response_json) VALUES(?,?,?,?,?)",(principal,message["messageId"],digest,row["task_id"],canonical_json(response)))
        c.commit()
    return response

@app.get("/a2a/tasks")
async def list_tasks(request:Request):
    principal=authenticated_principal(request.headers.get("authorization"))
    with connect() as c: rows=c.execute("SELECT task_json FROM tasks WHERE principal=? ORDER BY created_at DESC",(principal,)).fetchall()
    return a2a_json({"tasks":[json.loads(r[0]) for r in rows]})

@app.get("/a2a/tasks/{task_id}")
async def read_task(task_id:str,request:Request):
    principal=authenticated_principal(request.headers.get("authorization"))
    with connect() as c: row=c.execute("SELECT task_json FROM tasks WHERE task_id=? AND principal=?",(task_id,principal)).fetchone()
    if not row: return err(404,"TASK_NOT_FOUND","Task not found")
    return a2a_json(json.loads(row[0]))

@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id:str,request:Request):
    principal=authenticated_principal(request.headers.get("authorization"))
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT task_json,state FROM tasks WHERE task_id=? AND principal=?",(task_id,principal)).fetchone()
        if not row: c.rollback(); return err(404,"TASK_NOT_FOUND","Task not found")
        task=json.loads(row["task_json"])
        if row["state"] in TERMINAL:
            c.rollback(); return err(409,"INVALID_STATE","Task is already terminal")
        task["status"]={"state":"TASK_STATE_CANCELED","timestamp":now()}
        c.execute("UPDATE tasks SET state=?,task_json=?,updated_at=? WHERE task_id=? AND state=?",("TASK_STATE_CANCELED",canonical_json(task),now(),task_id,row["state"]))
        c.execute("UPDATE idempotency SET response_json=? WHERE task_id=?",(canonical_json({"task":task}),task_id))
        c.commit()
    return a2a_json(task)
