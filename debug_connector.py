import sys, asyncio
sys.path.insert(0,"/Users/marshmellow/GitRep/lead-email-verifier")
from verifier.job_service import JobService
async def main():
 s=JobService("/Users/marshmellow/GitRep/lead-email-verifier/data/debug.sqlite3")
 job=s.submit_emails(["jane@example.test"])
 await asyncio.sleep(.1)
 print(s.store.job(job))
 await s.close()
asyncio.run(main())
