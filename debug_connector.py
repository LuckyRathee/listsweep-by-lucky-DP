import sys, asyncio
sys.path.insert(0,"/Users/marshmellow/GitRep/listsweep-by-lucky-DP")
from verifier.job_service import JobService
async def main():
 s=JobService("/Users/marshmellow/GitRep/listsweep-by-lucky-DP/data/debug.sqlite3")
 job=s.submit_emails(["jane@example.test"])
 await asyncio.sleep(.1)
 print(s.store.job(job))
 await s.close()
asyncio.run(main())
