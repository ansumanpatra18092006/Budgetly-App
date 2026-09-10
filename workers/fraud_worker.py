"""Run with: python -m workers.fraud_worker"""
import json, logging, os, time, urllib.request
from utils.db import get_db
from services.fraud_job_service import claim_jobs, mark_done, mark_failed, recover_stale_jobs

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("fraud_worker")

def _send_soc(payload):
    url=os.getenv("FRAUDSHIELD_SOC_WEBHOOK_URL")
    if not url or os.getenv("FRAUDSHIELD_ALLOW_OUTBOUND_WEBHOOKS","0") != "1":
        return
    body=json.dumps({"event":"fraudshield.critical_alert","evidence":payload}).encode()
    req=urllib.request.Request(url,data=body,headers={"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=5) as resp:
        if not 200 <= int(resp.status) < 300:
            raise RuntimeError(f"SOC webhook HTTP {resp.status}")

def run_once():
    conn=get_db()
    try:
        recover_stale_jobs(conn)
        jobs=claim_jobs(conn,20)
        conn.commit()
        for job in jobs:
            try:
                if job['job_type']=='SOC_NOTIFY': _send_soc(dict(job['payload'] or {}))
                else: raise RuntimeError(f"Unknown job type {job['job_type']}")
                mark_done(conn,job['id'])
            except Exception as exc:
                log.exception("job failed id=%s",job['id']); mark_failed(conn,job,str(exc))
            conn.commit()
        return len(jobs)
    finally: conn.close()

if __name__=='__main__':
    while True:
        if run_once()==0: time.sleep(2)
